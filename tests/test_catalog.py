import hashlib
import io
import json
import os
import ssl
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from backend import artwork, catalog, patches, scanner
from backend.main import create_app
from backend.seed import VERSION_URI

CASTLEVANIA_INFO = """[General]
  Package type: FIH Debug — installable on a PS5 in debug mode
  Content ID: JP0101-PPSA35399_00-CVBCAPP000000000
  Content type: Application / game (0x20)
  Title: Castlevania: Belmont's Curse
  App type: Game / application (gd) · DRM standard
  Version: content 01.000.000 · master 01.00
  SDK: 0x1100000000000000 (SDK 11)
  Required system software: 0x1340000000000000
[param.json]
  contentVersion: 01.000.000
  titleId: PPSA35399
  versionFileUri: """ + VERSION_URI


def game_body(game, **changes):
    fields = set(catalog.GameInput.model_fields)
    return {**{key: value for key, value in game.items() if key in fields}, **changes}


class LocalCatalogTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.env = patch.dict(os.environ, {"PROSPERO_EMPTY_LIBRARY": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(create_app(self.data), base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def initial(self):
        return self.client.get("/api/games/1").json()

    def wait_job(self, response):
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        timeout = time.monotonic() + 8
        while job["status"] in {"running", "cancelling"} and time.monotonic() < timeout:
            time.sleep(.05)
            job = self.client.get(f"/api/jobs/{job['id']}").json()
        self.assertNotIn(job["status"], {"running", "cancelling"})
        return job

    def test_initial_metadata_and_manual_add(self):
        initial = self.initial()
        self.assertEqual((initial["version"], initial["firmware"], initial["size_bytes"]),
                         ("01.000.000", "13.40", 4043482656))
        self.assertEqual(len(initial["dlc"]), 2)
        self.assertTrue(all(dlc["status"] == "configured" for dlc in initial["dlc"]))
        self.assertIsNone(initial["ru_audio"])
        self.assertIsNone(initial["patch"]["latest"])
        self.assertEqual(self.client.post("/api/games", json={"title": "Ёж", "platform": "PS4",
                                                                             "title_id": "CUSA12345"}).status_code, 201)
        self.assertEqual(self.client.post("/api/games", json={"title": "Другой", "platform": "PS4",
                                                                             "title_id": "CUSA12345"}).status_code, 409)
        self.assertEqual(self.client.post("/api/games", json={"title": "Ошибка", "platform": "PS5",
                                                                             "title_id": "CUSA12345"}).status_code, 422)

    def test_remove_card(self):
        self.assertEqual(self.client.delete("/api/games/1").status_code, 200)
        self.assertEqual(self.client.get("/api/games").json(), [])
        self.assertEqual(self.client.get("/api/games/1").status_code, 404)

    def test_game_order_survives_restart_and_reused_id(self):
        def add(title_id):
            return self.client.post("/api/games", json={"title": title_id, "platform": "PS5", "title_id": title_id}).json()["id"]

        middle = add("PPSA12345")
        last = add("PPSA12346")
        self.assertEqual(self.client.post("/api/games/order", json={"source_id": 1, "target_id": last}).json(),
                         [last, middle, 1])
        self.assertEqual(self.client.post("/api/games/order", json={"source_id": 1, "target_id": 99}).status_code, 404)
        with TestClient(create_app(self.data), base_url="http://localhost") as restarted:
            self.assertEqual([game["id"] for game in restarted.get("/api/games").json()], [last, middle, 1])
        self.assertEqual(self.client.delete(f"/api/games/{last}").status_code, 200)
        self.assertEqual(add("PPSA12347"), last)
        self.assertEqual([game["id"] for game in self.client.get("/api/games").json()], [middle, 1, last])

    def test_clear_diagnostic_log_preserves_future_logging(self):
        previous = self.data / "logs" / "prospero.log.1"
        previous.write_text("Предыдущая ротация", encoding="utf-8")
        self.assertIn("Prospero Library запущена", self.client.get("/api/logs").json()["text"])
        rejected = self.client.delete("/api/logs", headers={"Origin": "http://foreign.example"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.client.delete("/api/logs").json(), {"text": ""})
        self.assertFalse(previous.exists())
        self.assertEqual(self.client.get("/api/logs?download=true").text, "")
        self.wait_job(self.client.post("/api/scan", json={}))
        after = self.client.get("/api/logs").json()["text"]
        self.assertNotIn("Prospero Library запущена", after)
        self.assertIn("Укажите папки коллекции в настройках", after)

    def test_game_about_loads_exact_store_product(self):
        content_id = self.initial()["content_id"]
        product = {"id": content_id, "publisherName": "Publisher", "localizedGenres": [{"value": "Action"}],
                   "descriptions": [{"type": "SHORT", "value": "Short description"},
                                    {"type": "LONG", "value": "<p>Introduction &amp; setting</p><h3>Features</h3>"
                                                               "<ul><li>First</li><li>Second</li></ul>"
                                                               "<script>ignored text</script>"}]}
        cache = json.dumps({"cache": {"Product:" + content_id: product}}).replace("<", "\\u003c")
        props = {"productId": content_id, "batarangs": {"info": {"text": f"<script>{cache}</script>"}}}
        data = json.dumps({"props": {"pageProps": props}}).replace("<", "\\u003c")
        page = f'<script id="__NEXT_DATA__">{data}</script>'.encode()
        url = f"https://store.playstation.com/en-us/product/{content_id}"
        requests = []

        def store(request):
            requests.append(str(request.url))
            return httpx.Response(200, content=page)

        store_client = httpx.Client(transport=httpx.MockTransport(store))
        with patch("backend.main.httpx.Client", return_value=store_client):
            response = self.client.get("/api/games/1/about")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"description": "Introduction & setting\n\nFeatures\n\n• First\n• Second",
                                               "publisher": "Publisher", "genres": ["Action"], "url": url})
            manual = self.client.post("/api/games", json={"title": "Manual", "platform": "PS5", "title_id": "PPSA12345"}).json()
            self.assertIsNone(self.client.get(f"/api/games/{manual['id']}/about").json())
        self.assertEqual(requests, [url])
        self.assertIsNone(artwork.playstation_description(page, "UP9000-CUSA07408_00-00000000GODOFWAR"))

    def test_playstation_store_art_preserves_bytes_and_backup(self):
        content_id = self.initial()["content_id"]
        image = "https://image.api.playstation.com/vulcan/portrait.jpg"
        media = [
            {"role": "PORTRAIT_BANNER", "type": "IMAGE", "url": image},
            {"role": "MASTER", "type": "IMAGE", "url": "https://other.example/cover.jpg"},
        ]
        fragment = '<script>' + json.dumps({"cache": {f"Product:{content_id}": {"id": content_id, "media": media}}}) + '</script>'
        props = {"productId": content_id, "batarangs": {"media-carousel": {"text": fragment}}}
        payload = json.dumps({"props": {"pageProps": props}}).replace("<", r"\u003c")
        page = f'<script id="__NEXT_DATA__">{payload}</script>'.encode()
        candidates = artwork.playstation_media(page, content_id, "Castlevania")
        self.assertEqual([item["url"] for item in candidates], [image])
        self.assertEqual(artwork.playstation_media(page, "UP0000-PPSA99999_00-FAKEPRODUCT00000", "Чужая игра"), [])
        original = io.BytesIO()
        Image.new("RGB", (150, 150), "orange").save(original, "JPEG")
        content = original.getvalue()
        with patch.object(artwork.Artwork, "search", return_value=(candidates, [])), patch.object(
            artwork.Artwork, "download", return_value=content
        ):
            found = self.client.post("/api/games/1/artwork/search").json()
            art = self.client.post("/api/games/1/artwork/select", json={"token": found["items"][0]["token"]})
        self.assertEqual(art.status_code, 200, art.text)
        cover = self.client.get(art.json()["cover"])
        self.assertEqual(cover.headers["content-type"], "image/jpeg")
        self.assertEqual(cover.content, content)
        backup = self.client.post("/api/backups").json()["name"]
        self.assertEqual(self.client.post("/api/backups/restore", json={"name": backup}).status_code, 200)
        self.assertEqual(self.client.get(self.initial()["cover"]).content, content)

    def test_fih_game_type_and_package_exclusions(self):
        metadata = scanner.parse_metadata(CASTLEVANIA_INFO, Path("Castlevania.pkg"))
        self.assertEqual((metadata["title_id"], metadata["title"], metadata["version"], metadata["firmware"]),
                         ("PPSA35399", "Castlevania: Belmont's Curse", "01.000.000", "13.40"))
        self.assertEqual(metadata["version_uri"], VERSION_URI)
        patch = CASTLEVANIA_INFO.replace("Game / application (gd)", "Game patch (gp)")
        dlc = CASTLEVANIA_INFO.replace("Application / game (0x20)", "Additional Content (0x00000021)")
        self.assertIsNone(scanner.parse_metadata(patch, Path("Update.pkg")))
        self.assertIsNone(scanner.parse_metadata(dlc, Path("DLC.pkg")))
        with self.assertRaises(scanner.MetadataError):
            scanner.parse_metadata("Package type: FIH Debug\nTitle ID: PPSA35399", Path("Unknown.pkg"))

    def test_imported_castlevania_uses_bundled_publisher_metadata(self):
        self.client.delete("/api/games/1")
        folder = self.root / "Games"
        folder.mkdir()
        file = folder / "Castlevania [PPSA35399].pkg"
        file.write_bytes(b"local parser test fixture")
        os.utime(file, (time.time() - 15,) * 2)
        self.client.post("/api/roots", json={"path": str(folder)})
        with patch.object(scanner, "cli_path", return_value=Path("fpkg-cli")), patch.object(
            scanner, "invoke", side_effect=[(0, CASTLEVANIA_INFO), (1, "No art")]
        ), patch.object(scanner, "providers", return_value={"IGDB": False}):
            self.assertEqual(self.wait_job(self.client.post("/api/scan", json={}))["report"]["added"], 1)
        game = self.initial()
        self.assertEqual(game["file_state"], "online")
        self.assertEqual(game["release_date"], "2026-10-15")
        self.assertEqual(game["cover_source"], "PlayStation Store")
        self.assertEqual(game["cover"], "/art/castlevania-cover.jpg")
        self.assertEqual(game["background"], "/art/castlevania-background.jpg")
        self.assertEqual(self.client.get(game["cover"]).status_code, 200)
        self.assertEqual(self.client.get(game["background"]).status_code, 200)
        self.assertIsNone(game["notes"])
        self.assertEqual(game["dlc"], [])
        saved = self.client.put("/api/games/1", json=game_body(game, release_date=None))
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertIsNone(self.initial()["release_date"])

    def test_copy_cli_and_use_local_copy(self):
        installed = self.root / "FPKG Builder"
        installed.mkdir()
        source = installed / scanner.CLI_FILENAME
        source.write_bytes(b"local CLI test fixture")
        (installed / "LibProsperoPkg.dll").write_bytes(b"dependency fixture")
        self.client.put("/api/settings", json={"cli_path": str(source), "ps5_firmware": "13.60"})
        local = self.root / "Prospero Library" / "tools" / "fpkg-cli"
        with patch.object(scanner, "LOCAL_CLI_DIRECTORY", local), patch.object(scanner, "invoke", return_value=(0, "FPKG")) as invoke:
            response = self.client.post("/api/settings/cli/copy")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["cli_local"])
            self.assertEqual(invoke.call_args.args[1], ["--help"])
            self.assertEqual((local / scanner.CLI_FILENAME).read_bytes(), source.read_bytes())
            self.assertEqual((local / "LibProsperoPkg.dll").read_bytes(), b"dependency fixture")
            installed.rename(self.root / "Moved FPKG Builder")
            check = self.client.post("/api/settings/cli")
            self.assertTrue(check.json()["available"])
            self.assertEqual(invoke.call_args.args[0], local / scanner.CLI_FILENAME)

    def test_patch_diagnostics_distinguish_network_and_bad_xml(self):
        def dns_error(_):
            raise httpx.ConnectError("[Errno 11001] getaddrinfo failed")

        def tls_error(_):
            raise httpx.ConnectError("Handshake failed") from ssl.SSLCertVerificationError(1, "untrusted issuer")

        for transport, expected in (
            (dns_error, "DNS не смог разрешить адрес Sony CDN"),
            (tls_error, "Не удалось проверить TLS-сертификат Sony CDN (untrusted issuer)"),
            (lambda _: httpx.Response(200, content=b"<malformed"), "Некорректный XML манифеста Sony CDN"),
        ):
            with self.subTest(expected=expected):
                client = httpx.Client(transport=httpx.MockTransport(transport))
                with patch.object(patches.httpx, "Client", return_value=client):
                    job = self.wait_job(self.client.post("/api/updates?game_id=1"))
                self.assertEqual(job["report"]["errors"], 1)
                self.assertIn(expected, job["report"]["issues"][0])
                self.assertIn(expected, self.client.get("/api/logs").json()["text"])

    def test_manifest_log_includes_underlying_connection_error(self):
        def proxy_error(_):
            raise httpx.ConnectError("Proxy https://demo:secret@proxy.example failed") from OSError(
                10061, "Connection refused"
            )

        client = httpx.Client(transport=httpx.MockTransport(proxy_error))
        with patch.object(patches.httpx, "Client", return_value=client):
            self.wait_job(self.client.post("/api/updates?game_id=1"))
        log = self.client.get("/api/logs").json()["text"]
        self.assertIn("GET XML " + VERSION_URI, log)
        self.assertIn("[Errno 10061] Connection refused", log)
        self.assertIn("https://[скрыто]@proxy.example", log)
        self.assertNotIn("demo:secret", log)

    def test_windows_manifest_verifier_keeps_tls_checks(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(patches.sys, "platform", "win32"):
            verifier = patches.manifest_verifier()
            self.assertIsInstance(verifier, patches.truststore.SSLContext)
            self.assertEqual(verifier.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(verifier.check_hostname)
            with patch.dict(os.environ, {"SSL_CERT_FILE": "custom-ca.pem"}):
                self.assertIs(patches.manifest_verifier(), True)
            with patch.dict(os.environ, {"SSL_CERT_DIR": "custom-ca"}):
                self.assertIs(patches.manifest_verifier(), True)

    def test_origin_and_manifest_validation(self):
        data = game_body(self.initial(), notes="Не должно сохраниться")
        response = self.client.put("/api/games/1", json=data, headers={"Origin": "http://another.example"})
        self.assertEqual(response.status_code, 403)
        response = self.client.put("/api/games/1", json={**data, "manifest_uri": "https://example.org/manifest.xml"})
        self.assertEqual(response.status_code, 422)
        response = self.client.put("/api/games/1", json={**data, "manifest_uri": VERSION_URI})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version_uri"], VERSION_URI)
        self.assertEqual(self.client.get("/api/health", headers={"Host": "another.example"}).status_code, 400)

    def test_backup_restore_and_integrity(self):
        item = self.initial()
        uploaded = io.BytesIO()
        Image.new("RGB", (280, 600), "orange").save(uploaded, "PNG")
        response = self.client.post("/api/games/1/artwork?kind=cover",
            files={"file": ("image.png", uploaded.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()["cover"]
        with Image.open(io.BytesIO(self.client.get(url).content)) as stored:
            self.assertEqual(3 * stored.width, 2 * stored.height)
        name = self.client.post("/api/backups").json()["name"]
        archive = self.client.get("/api/backups/" + name)
        self.assertEqual(archive.status_code, 200)
        with ZipFile(io.BytesIO(archive.content)) as zf:
            self.assertEqual(set(zf.namelist()), {"library.db", "artwork/" + url.split("/")[-1]})
        response = self.client.put("/api/games/1", json=game_body(item, notes="После бэкапа"))
        self.assertEqual(response.status_code, 200, response.text)
        restored = self.client.post("/api/backups/restore", json={"name": name})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.initial()["notes"], item["notes"])
        self.assertEqual(self.initial()["cover"], url)
        self.assertEqual(len(self.client.get("/api/backups").json()), 2)

    def test_scan_import_relink_offline_and_preserve_edits(self):
        folder = self.root / "Коллекция"
        folder.mkdir()
        base = folder / "Castlevania. Belmont's Curse [PPSA35399] [01.001.000].pkg"
        ps4 = folder / "Ёж [CUSA12345] [01.00].PKG"
        separate_dlc = folder / "Дополнение [PPSA44444].pkg"
        invalid = folder / "Повреждённый.pkg"
        skipped = folder / "_WorkHard"
        skipped.mkdir()
        paths = (base, ps4, separate_dlc, invalid, skipped / "Ignore.pkg")
        for path in paths:
            path.write_bytes(b"local parser test fixture")
            os.utime(path, (time.time() - 15,) * 2)
        digests = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        self.assertEqual(self.client.post("/api/roots", json={"path": str(skipped)}).status_code, 422)
        root_id = self.client.post("/api/roots", json={"path": str(folder)}).json()["id"]
        saved = self.client.put("/api/games/1", json=game_body(self.initial(), title="Моя Castlevania",
                                                              notes="Ручное примечание"))
        self.assertEqual(saved.status_code, 200, saved.text)

        def mock_cli(_exe, arguments, **_):
            verb, target, *_ = arguments
            if target == base and verb == "pkg-info":
                return 0, CASTLEVANIA_INFO.replace("01.000.000", "01.001.000")
            if target == ps4 and verb == "ps4-info":
                return 0, "TITLE_ID = CUSA12345\nTITLE: Ёж\nCATEGORY: gd\nAPP_VER: 01.00\nSYSTEM_VER: 0x09000000"
            if target == separate_dlc and verb == "pkg-info":
                return 0, "Content ID: JP0101-PPSA44444_00-EXAMPLEOTHER00000\nType: AC (0x21)"
            if target == invalid and verb == "pkg-info":
                return 0, "Package type: FIH Debug\nTitle ID: PPSA99999"
            return 1, "Invalid test fixture"

        with patch.object(scanner, "cli_path", return_value=self.root / "fpkg-cli"), patch.object(scanner, "invoke", side_effect=mock_cli):
            first = self.wait_job(self.client.post("/api/scan", json={}))
            self.assertEqual(first["status"], "done", first)
            report = first["report"]
            self.assertEqual((report["found"], report["added"], report["updated"], report["skipped"], report["errors"]),
                             (4, 1, 1, 1, 1), report)
            card = self.initial()
            self.assertEqual(card["title"], "Моя Castlevania")
            self.assertEqual(card["notes"], "Ручное примечание")
            self.assertEqual(card["cover_source"], "PlayStation Store")
            self.assertEqual(card["version"], "01.001.000")
            self.assertEqual(card["file_state"], "online")
            journal = self.client.get("/api/logs").json()["text"]
            self.assertIn("fpkg-cli pkg-info: код 0, PPSA35399 01.001.000", journal)
            self.assertIn("packagetype='FIH Debug'", journal)
            self.assertIn("fpkg-cli ps4-info: код 1", journal)
            self.assertIn("ошибок 1", journal)
            download = self.client.get("/api/logs?download=true")
            self.assertIn("prospero-log.txt", download.headers["Content-Disposition"])
            self.assertEqual(download.text, journal)

            second = self.wait_job(self.client.post("/api/scan", json={}))
            self.assertEqual(second["report"]["added"], 0)
            self.assertEqual(len(self.client.get("/api/games").json()), 2)
            self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}, digests)
            self.assertEqual(set(card["package_fields"]), {"platform", "title_id", "version", "firmware", "size_bytes"})
            locked = self.client.put("/api/games/1", json=game_body(card, version="99.99", edit_pkg_fields=False)).json()
            self.assertEqual(locked["version"], "01.001.000")
            duplicate = self.client.put("/api/games/1", json=game_body(card, title_id="CUSA12345", platform="PS4",
                                                                     edit_pkg_fields=True))
            self.assertEqual(duplicate.status_code, 422)
            edited = self.client.put("/api/games/1", json=game_body(card, title_id="CUSA54321", platform="PS4",
                                                                  version="01.002.000", edit_pkg_fields=True))
            self.assertEqual(edited.status_code, 200, edited.text)
            self.assertEqual(edited.json()["package_identity"], {"platform": "PS5", "title_id": "PPSA35399"})
            forced = self.wait_job(self.client.post("/api/scan", json={"force": True}))
            self.assertEqual(forced["report"]["added"], 0)
            self.assertEqual(len(self.client.get("/api/games").json()), 2)
            self.assertEqual((self.initial()["title_id"], self.initial()["version"]), ("CUSA54321", "01.002.000"))

            def patch_response(request):
                self.assertEqual(str(request.url), VERSION_URI)
                return httpx.Response(200, content=b'<titlepatch><package content_ver="01.003.000" system_ver="337641479"/></titlepatch>')

            patch_client = httpx.Client(transport=httpx.MockTransport(patch_response))
            with patch.object(patches.httpx, "Client", return_value=patch_client):
                self.assertEqual(self.wait_job(self.client.post("/api/updates?game_id=1"))["report"]["updated"], 1)
            self.assertEqual(self.initial()["patch"]["target"], "13.60")
            moved = self.root / "Перенесённая коллекция"
            folder.rename(moved)
            self.wait_job(self.client.post("/api/scan", json={}))
            self.assertEqual(self.initial()["file_state"], "offline")
            self.assertEqual(len(self.client.get("/api/games").json()), 2)
            self.assertEqual(self.client.put(f"/api/roots/{root_id}", json={"path": str(moved)}).status_code, 200)
            self.wait_job(self.client.post("/api/scan", json={}))
            self.assertEqual(self.initial()["file_state"], "online")

    def test_scan_multiple_roots_and_continue_without_removable_disk(self):
        removable = self.root / "Съёмный диск"
        local = self.root / "Коллекция"

        def write_package(root, title_id):
            directory = root / "PS4" / "Games"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{title_id}.pkg"
            path.write_bytes(b"package fixture")
            os.utime(path, (time.time() - 15,) * 2)

        write_package(removable, "CUSA10001")
        write_package(local, "CUSA10002")
        routes = "/api/roots/batch"
        response = self.client.post(routes, json={"roots": [{"path": f'"{removable}"'}, {"path": str(local)}]})
        self.assertEqual(response.status_code, 201, response.text)
        removable_id, _ = response.json()["ids"]
        repeated = self.client.post(routes, json={"roots": [{"path": str(self.root / "Extra")}, {"path": str(local)}]})
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(len(self.client.get("/api/settings").json()["roots"]), 2)

        def read_metadata(_executable, path, _stop):
            return scanner.parse_metadata(f"Title ID: {path.stem}\nTitle: {path.stem}\nCategory: gd\nApp ver: 01.00", path)

        with patch.object(scanner, "cli_path", return_value=self.root / "fpkg-cli"), patch.object(
            scanner, "read_metadata", side_effect=read_metadata
        ) as parse, patch.object(scanner, "enrich_import"):
            def scan_collection():
                return self.wait_job(self.client.post("/api/scan", json={}))["report"]

            first = scan_collection()
            self.assertEqual((first["found"], first["added"]), (2, 2))
            moved = self.root / "Другой диск"
            removable.rename(moved)
            write_package(local, "CUSA10003")
            second = scan_collection()
            self.assertEqual((second["found"], second["added"]), (2, 1))
            self.assertIn(str(removable), " ".join(second["issues"]))
            games = {game["title_id"]: game for game in self.client.get("/api/games").json()}
            self.assertEqual(games["CUSA10001"]["file_state"], "offline")
            self.assertEqual(games["CUSA10003"]["file_state"], "online")
            self.assertEqual(self.client.put(f"/api/roots/{removable_id}", json={"path": str(moved)}).status_code, 200)
            resumed = scan_collection()
            self.assertEqual((resumed["found"], resumed["added"], parse.call_count), (3, 0, 3))
            restored = self.client.get(f"/api/games/{games['CUSA10001']['id']}").json()
            self.assertEqual(restored["file_state"], "online")

    def test_patch_versions_and_network_errors(self):
        game = self.initial()
        self.assertTrue(catalog.version_key("01.010.000") > catalog.version_key("01.002.000"))
        self.assertEqual(catalog.version_key("1.02"), catalog.version_key("01.002.000"))
        xml = b'<titlepatch><patch version="01.010.000" system_ver="0x1361000000000000"/>' \
              b'<patch version="01.002.000" requiredSystemSoftwareVersion="13.40"/></titlepatch>'
        requests = []
        def response(req):
            requests.append(req.url)
            return httpx.Response(200, content=xml)
        client = httpx.Client(transport=httpx.MockTransport(response))
        with patch.object(patches.httpx, "Client", return_value=client):
            job = self.wait_job(self.client.post("/api/updates?game_id=1"))
        self.assertEqual(job["report"]["updated"], 1, job)
        self.assertEqual(str(requests[0]), VERSION_URI)
        update = self.initial()["patch"]
        self.assertEqual((update["state"], update["latest"]["version"], update["compatible"]["version"]),
                         ("available", "01.010.000", "01.002.000"))
        self.assertIsNotNone(update["check"]["last_success"])

        for status, expected in [(429, "error"), (404, "unknown")]:
            client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status)))
            with patch.object(patches.httpx, "Client", return_value=client):
                self.wait_job(self.client.post("/api/updates?game_id=1"))
            update = self.initial()["patch"]
            self.assertEqual(update["state"], expected)
            self.assertIsNotNone(update["check"]["last_success"])

        def timeout(_):
            raise httpx.ReadTimeout("fixture")
        client = httpx.Client(transport=httpx.MockTransport(timeout))
        with patch.object(patches.httpx, "Client", return_value=client):
            self.wait_job(self.client.post("/api/updates?game_id=1"))
        self.assertEqual(self.initial()["patch"]["state"], "error")

        xml = b'<titlepatch><patch version="01.012.000"/></titlepatch>'
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=xml)))
        with patch.object(patches.httpx, "Client", return_value=client):
            self.wait_job(self.client.post("/api/updates?game_id=1"))
        update = self.initial()["patch"]
        self.assertEqual(update["latest"]["version"], "01.012.000")
        self.assertIsNone(update["latest"]["firmware"])
        self.assertIsNone(update["compatible"])
        self.assertEqual(len(update["history"]), 3)
        response = self.client.put("/api/games/1", json=game_body(game, official_version="01.014.000",
                                                                   official_firmware="13.60"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["patch"]["manual"]["source"], "Вручную")
        self.assertEqual(response.json()["patch"]["latest"]["version"], "01.012.000")

    def test_prosperopatches_online_fallback(self):
        key = "a" * 64
        title = "PPSA35399"
        page = f'<div id="dynpatch" data-titleid="{title}" data-key="{key}"></div>'
        entries = [
            {"content_ver": "01.001.000", "required_firmware": "14.00.00.39"},
            {"content_ver": "01.001.004", "required_firmware": "14.20.00.07"},
            {"content_ver": "01.001.002", "required_firmware": "14.10.00.03"},
        ]

        requests = []

        def respond(request):
            requests.append((request.method, str(request.url)))
            if request.url.host == patches.PS5_HOST:
                raise httpx.ConnectError("untrusted root")
            if request.method == "GET":
                return httpx.Response(200, text=page)
            self.assertEqual(json.loads(request.content), {"titleid": title, "key": key})
            return httpx.Response(200, json={"success": True, "titleid": title, "patches": entries})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch.object(patches.httpx, "Client", return_value=client):
            self.assertEqual(self.wait_job(self.client.post("/api/updates?game_id=1"))["report"]["updated"], 1)
        self.assertEqual(requests, [("GET", VERSION_URI), ("GET", f"{patches.PATCH_SITE_URL}/{title}"),
                                    ("POST", f"{patches.PATCH_SITE_URL}/api/internal/loadpatches")])
        result = self.initial()["patch"]
        self.assertEqual(result["check"]["source"], "ProsperoPatches")
        self.assertEqual(result["latest"]["firmware"], "14.20.00.07")
        self.assertEqual(result["latest"]["version"], "01.001.004")
        self.assertEqual(len(result["history"]), 3)
        self.assertIsNone(result["compatible"])
        self.assertEqual(patches.decode_manifest_firmware("systemver", "337641479", title), "14.20.00.07")
