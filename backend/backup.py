import hashlib
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from contextlib import closing
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from .artwork import ART_NAME, MAX_IMAGE
from .database import APP_ID, SCHEMA, now
from .seed import LEGACY_ART

BUNDLE_NAME = re.compile(r"prospero-[0-9]{8}-[0-9]{6}-[0-9]{6}\.zip")
MEDIA_LIMIT = 1024 * 1024 * 1024


def create_backup(database):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = database.backups / f"prospero-{stamp}.zip"
    with database.lock, tempfile.TemporaryDirectory(dir=database.directory) as temporary:
        snapshot = Path(temporary, "library.db")
        database.snapshot(snapshot)
        with database.connect() as db:
            art = referenced_art(db)
        staged = Path(temporary, "backup.zip")
        with ZipFile(staged, "w", compression=ZIP_DEFLATED) as archive:
            archive.write(snapshot, "library.db")
            for name in sorted(art):
                artwork = database.media / name
                if not artwork.is_file():
                    raise ValueError("Файл изображения отсутствует. Копия не создана.")
                archive.write(artwork, "artwork/" + name)
        staged.replace(destination)
    return destination


def referenced_art(db):
    art = set()
    for cover, background in db.execute("SELECT cover,background FROM games"):
        for url in (cover, background):
            if url and url.startswith("/media/") and ART_NAME.fullmatch(name := url[7:]):
                art.add(name)
            elif url is not None and url not in LEGACY_ART and url not in LEGACY_ART.values():
                raise ValueError("Некорректная ссылка на изображение в базе")
    return art


def schema(db):
    return dict(db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"))


def restore_backup(database, file):
    with database.lock, tempfile.TemporaryDirectory(dir=database.directory) as temporary:
        snapshot = Path(temporary, "library.db")
        with ZipFile(file) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if ("library.db" not in names or len(names) != len(set(names)) or len(names) > 10000 or
                sum(entry.file_size for entry in entries) > MEDIA_LIMIT):
                raise ValueError("Некорректный состав архива или слишком большой размер")
            arts = set()
            for entry in entries:
                if entry.filename == "library.db":
                    if entry.file_size > 128 * 1024 * 1024:
                        raise ValueError("База в архиве слишком большая")
                    with archive.open(entry) as data, snapshot.open("wb") as output:
                        shutil.copyfileobj(data, output)
                    continue
                name = entry.filename.removeprefix("artwork/")
                if name == entry.filename or not ART_NAME.fullmatch(name) or entry.file_size > MAX_IMAGE:
                    raise ValueError("Архив содержит неожиданный файл")
                content = archive.read(entry)
                if hashlib.sha256(content).hexdigest() != name.split(".", 1)[0]:
                    raise ValueError("Изображение в архиве повреждено")
                Path(temporary, name).write_bytes(content)
                arts.add(name)

        with closing(sqlite3.connect(snapshot)) as source, closing(sqlite3.connect(":memory:")) as expected:
            try:
                expected.executescript(SCHEMA)
                if (source.execute("PRAGMA application_id").fetchone()[0] != APP_ID or
                    source.execute("PRAGMA user_version").fetchone()[0] != 1 or schema(source) != schema(expected) or
                    source.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or
                    source.execute("PRAGMA foreign_key_check").fetchone() or arts != referenced_art(source)):
                    raise ValueError("Некорректная или неподдерживаемая база в архиве")
            except sqlite3.Error as error:
                raise ValueError("Не удалось прочитать базу в архиве") from error
            protective = create_backup(database)
            for name in arts:
                Path(temporary, name).replace(database.media / name)
            source.execute("UPDATE jobs SET status='interrupted',finished_at=? WHERE status IN ('running','cancelling')", (now(),))
            source.commit()
            with database.connect() as target:
                source.backup(target)
    return protective
