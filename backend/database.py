import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

BASE = Path(__file__).resolve().parent.parent
DEFAULT_DATA = Path(os.getenv("LOCALAPPDATA", Path.home() / ".local" / "share")) / "Prospero Library"
APP_ID = 1347571527
SCHEMA = """
CREATE TABLE games (
    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, title_id TEXT NOT NULL,
    title TEXT NOT NULL, edition TEXT, release_date TEXT, added_at TEXT NOT NULL,
    ru_text INTEGER, ru_audio INTEGER, text_languages TEXT, audio_languages TEXT,
    localization_source TEXT, notes TEXT, cover TEXT, background TEXT,
    cover_source TEXT, background_source TEXT, overrides TEXT NOT NULL DEFAULT '[]',
    UNIQUE (platform, title_id)
);
CREATE TABLE builds (
    id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    identity TEXT NOT NULL, version TEXT, firmware TEXT, size_bytes INTEGER,
    content_id TEXT, version_uri TEXT, sdk TEXT, sha256 TEXT,
    UNIQUE (game_id, identity)
);
CREATE TABLE dlc (
    id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    name TEXT NOT NULL, description TEXT, status TEXT NOT NULL, content_id TEXT
);
CREATE TABLE roots (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'unknown');
CREATE TABLE files (
    id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL, size_bytes INTEGER NOT NULL, mtime INTEGER NOT NULL,
    build_id INTEGER NOT NULL REFERENCES builds(id) ON DELETE CASCADE, available INTEGER NOT NULL DEFAULT 1,
    UNIQUE (root_id, relpath)
);
CREATE TABLE patch_history (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE, version TEXT NOT NULL,
    firmware TEXT, source TEXT NOT NULL, checked_at TEXT NOT NULL, size_bytes INTEGER,
    PRIMARY KEY (game_id, version, source)
);
CREATE TABLE checks (
    game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    state TEXT NOT NULL, source TEXT, checked_at TEXT, last_success TEXT, message TEXT,
    latest_version TEXT
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.path = self.directory / "library.db"
        self.media = self.directory / "artwork"
        self.backups = self.directory / "backups"
        self.lock = RLock()

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def initialize(self):
        self.media.mkdir(parents=True, exist_ok=True)
        self.backups.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                existing = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()
                if existing:
                    raise RuntimeError("Неизвестная схема базы Prospero Library")
                from .seed import initial_game
                db.executescript(f"BEGIN IMMEDIATE; {SCHEMA}")
                db.execute(f"PRAGMA application_id = {APP_ID}")
                db.execute("PRAGMA user_version = 1")
                db.executemany("INSERT INTO settings VALUES (?, ?)", [
                    ("ps5_firmware", "13.60"), ("ps4_firmware", ""), ("cli_path", ""),
                ])
                if os.getenv("PROSPERO_EMPTY_LIBRARY") != "1":
                    initial_game(db)
            elif version != 1 or db.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
                raise RuntimeError("Неподдерживаемая версия базы Prospero Library")
            db.execute("UPDATE jobs SET status='interrupted', finished_at=? WHERE status IN ('running','cancelling')", (now(),))

    def snapshot(self, destination):
        with self.connect() as db:
            target = sqlite3.connect(destination)
            try:
                db.backup(target)
            finally:
                target.close()
