import hashlib
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from backend import __version__
from backend.app_updates import RELEASE_API, REPOSITORY, Release, UpdateError, download_release, latest_release
from backend.main import create_app
from launcher.app_update import UpdateRolledBack, activate_update

VERSION = "1.2.10"
FILE = f"Prospero-Library-{VERSION}-Windows-x64.exe"
DOWNLOAD = f"https://github.com/{REPOSITORY}/releases/download/v{VERSION}/{FILE}"
CDN = "https://release-assets.githubusercontent.com/preview"
EXE = b"MZ test executable"
DIGEST = hashlib.sha256(EXE).hexdigest()


class SelfUpdateTest(unittest.TestCase):
    def test_download_verifies_release_metadata_and_bytes_across_redirect(self):
        def respond(request):
            if str(request.url) == RELEASE_API:
                return httpx.Response(200, json={"tag_name": f"v{VERSION}", "assets": [{
                    "name": FILE, "browser_download_url": DOWNLOAD, "size": len(EXE), "digest": "sha256:" + DIGEST,
                }]})
            if str(request.url) == DOWNLOAD:
                return httpx.Response(302, headers={"Location": CDN})
            self.assertEqual(str(request.url), CDN)
            return httpx.Response(200, content=EXE)

        with TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(respond)) as client:
            destination = Path(directory) / "new.exe"
            release = latest_release(client, "1.2.9")
            self.assertEqual(release, Release(VERSION, DOWNLOAD, len(EXE), DIGEST))
            self.assertIsNone(latest_release(client, VERSION))
            seen = []
            download_release(client, release, destination, seen.append)
            self.assertEqual(seen[-1], len(EXE))
            self.assertEqual(destination.read_bytes(), EXE)
            destination.unlink()
            with self.assertRaisesRegex(UpdateError, "SHA-256"):
                download_release(client, Release(VERSION, DOWNLOAD, len(EXE), "0" * 64), destination)
            self.assertFalse(destination.exists())

    def test_unexpected_download_redirect_is_rejected(self):
        def respond(request):
            self.assertEqual(str(request.url), DOWNLOAD)
            return httpx.Response(302, headers={"Location": "https://unexpected.example/file.exe"})

        with TemporaryDirectory() as directory, httpx.Client(transport=httpx.MockTransport(respond)) as client:
            destination = Path(directory) / "new.exe"
            with self.assertRaisesRegex(UpdateError, "неожиданный адрес"):
                download_release(client, Release(VERSION, DOWNLOAD, len(EXE), DIGEST), destination)
            self.assertFalse(destination.exists())

    def test_local_update_schedules_restart_only_after_preparation(self):
        updater = Mock()
        updater.prepare.return_value = None
        with TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(root / "data", app_updater=updater)
            with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client:
                self.assertTrue(client.get("/api/health").json()["app_update"])
                check = client.post("/api/app-update")
                self.assertEqual(check.json(), {"version": __version__, "restart": False})
                updater.launch.assert_not_called()
                updater.shutdown.assert_not_called()
                stage = root / "staging"
                stage.mkdir()
                updater.prepare.return_value = (VERSION, stage)
                check = client.post("/api/app-update")
                self.assertEqual(check.json(), {"version": VERSION, "restart": True})
                updater.launch.assert_called_once_with((VERSION, stage))
                updater.shutdown.assert_called_once_with()
                self.assertEqual(client.post("/api/app-update").status_code, 409)
            with TestClient(app, base_url="http://localhost", client=("192.0.2.5", 12345)) as client:
                self.assertFalse(client.get("/api/health").json()["app_update"])
                self.assertEqual(client.post("/api/app-update").status_code, 403)

    def test_download_progress_is_scoped_and_cleared_after_failure(self):
        started, finish = Event(), Event()

        def interrupted_download(current, progress):
            progress(VERSION, 10, 20)
            started.set()
            if not finish.wait(5):
                raise AssertionError("Timed out while simulating a download")
            raise UpdateError("Скачивание прервано")

        updater = Mock()
        updater.prepare.side_effect = interrupted_download
        with TemporaryDirectory() as directory:
            app = create_app(Path(directory), app_updater=updater)
            with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)) as client, \
                    ThreadPoolExecutor(max_workers=1) as pool:
                path = "/api/app-update?operation=download"
                pending = pool.submit(client.post, path)
                try:
                    self.assertTrue(started.wait(5))
                    self.assertEqual(client.get(path).json(), {"version": VERSION, "received": 10, "total": 20})
                    self.assertIsNone(client.get("/api/app-update?operation=other").json())
                    self.assertEqual(client.post(path).status_code, 409)
                finally:
                    finish.set()
                self.assertEqual(pending.result(timeout=5).status_code, 502)
                self.assertIsNone(client.get(path).json())
                updater.launch.assert_not_called()

    def test_replacement_restarts_and_reverts_exe_and_database_on_failure(self):
        for successful in (True, False):
            with self.subTest(successful=successful), TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "Prospero.exe"
                target.write_bytes(b"previous exe")
                staging = root / ".prospero-update-test"
                staging.mkdir()
                (staging / "pending.exe").write_bytes(EXE)
                database = root / "library.db"
                settings = root / ".env"
                settings.write_text("settings fixture")
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute("CREATE TABLE marker (message TEXT)")
                    db.execute("INSERT INTO marker VALUES ('before')")

                def launch(executable, argument):
                    self.assertEqual((executable, argument), (target, "--background"))
                    if executable.read_bytes() == EXE:
                        with closing(sqlite3.connect(database)) as db, db:
                            db.execute("UPDATE marker SET message='after'")
                    return Mock()

                with patch("launcher.app_update.start_exe", side_effect=launch) as start, \
                        patch("launcher.app_update.stop_child") as stop:
                    if successful:
                        activate_update(staging, target, database, VERSION, lambda *_: True)
                        stop.assert_not_called()
                        self.assertEqual(start.call_count, 1)
                    else:
                        with self.assertRaises(UpdateRolledBack):
                            activate_update(staging, target, database, VERSION, lambda *_: False)
                        stop.assert_called_once()
                        self.assertEqual(start.call_count, 2)
                self.assertEqual(target.read_bytes(), EXE if successful else b"previous exe")
                self.assertEqual(settings.read_text(), "settings fixture")
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual(db.execute("SELECT message FROM marker").fetchone(),
                                     ("after" if successful else "before",))
