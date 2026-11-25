"""Read package metadata through the configured local CLI."""
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from .artwork import MAX_IMAGE, PS_STORE_PRODUCT, PS_STORE_SOURCE, Artwork, bounded_get, providers, store_image
from .catalog import version_key
from .database import BASE, now
from .seed import PUBLISHER_METADATA

TITLE_ID = re.compile(r"(?<![A-Z0-9])(?:PPSA|CUSA)[0-9]{5}", re.I)
FILE_TITLE = re.compile(r"^(.*?)\s+\[(?:PPSA|CUSA)\d{5}\]", re.I)
ROLE_FIELDS = ("category", "apptype", "packagecategory", "packagetype", "contenttype", "pkgtype", "type", "kind")
EXTRA_CONTENT = re.compile(
    r"\b(?:dlc|patch|update|add[- ]?on|additional content|addcont|ac|al|gp|gdp|"
    r"ps[45](?:ac|al|dp)|0x0*(?:1b|1c|1e|21|22))\b", re.I,
)
BASE_GAME = re.compile(r"\b(?:app|application|base|game|gd|gda|ps[45]gd)\b", re.I)
WORK_DIRECTORY = "_workhard"
APP_DIRECTORY = Path(sys.executable).parent if getattr(sys, "frozen", False) else BASE
LOCAL_CLI_DIRECTORY = APP_DIRECTORY / "tools" / "fpkg-cli"
CLI_FILENAME = "fpkg-cli.exe" if os.name == "nt" else "fpkg-cli"
logger = logging.getLogger(__name__)


class MetadataError(Exception):
    pass


def local_cli():
    candidate = LOCAL_CLI_DIRECTORY / CLI_FILENAME
    return candidate if candidate.is_file() else None


def cli_path(configured):
    return local_cli() or external_cli(configured)


def external_cli(configured):
    configured = configured or os.getenv("PROSPERO_CLI", "")
    allowed = {"fpkg-cli.exe"} if os.name == "nt" else {"fpkg-cli", "fpkg-cli.exe"}
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and path.name.lower() in allowed:
            return path.resolve()
        if path.is_dir():
            if os.name == "nt" and not path.is_absolute():
                return None
            try:
                return next((p.resolve() for p in path.rglob("fpkg-cli.exe") if p.is_file()), None)
            except OSError:
                return None
        return None
    found = shutil.which("fpkg-cli")
    return Path(found).resolve() if found else None


def copy_local_cli(configured):
    existing = local_cli()
    if existing:
        return existing
    source = external_cli(configured)
    if not source:
        raise MetadataError("Сначала сохрани путь к установленному fpkg-cli")
    if LOCAL_CLI_DIRECTORY.exists():
        raise MetadataError("Папка tools/fpkg-cli уже существует. Проверь её содержимое.")

    parent = LOCAL_CLI_DIRECTORY.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cli-", dir=parent) as temporary:
        staged = Path(temporary, "fpkg-cli")
        staged.mkdir()
        executable = staged / CLI_FILENAME
        shutil.copy2(source, executable)
        for entry in source.parent.iterdir():
            name = entry.name.casefold()
            if entry.is_file() and (
                name.endswith((".dll", ".so", ".dylib", ".deps.json", ".runtimeconfig.json", ".exe.config"))
                or name.startswith(("license", "notice", "thirdpartynotices"))
            ):
                shutil.copy2(entry, staged / entry.name)
        runtimes = source.parent / "runtimes"
        if runtimes.is_dir():
            shutil.copytree(runtimes, staged / runtimes.name)

        code, _ = invoke(executable, ["--help"], timeout=20)
        if code != 0:
            raise MetadataError(f"Локальная копия fpkg-cli не запустилась (код {code})")
        staged.rename(LOCAL_CLI_DIRECTORY)
    return LOCAL_CLI_DIRECTORY / CLI_FILENAME


def invoke(executable, args, timeout=45, stop=None):
    env = {**os.environ, "FPKG_LANG": "en"}
    process = subprocess.Popen(
        [str(executable), *map(str, args)],
        cwd=executable.parent, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            if (stop and stop.is_set()) or time.monotonic() >= deadline:
                raise TimeoutError("Чтение прервано" if stop and stop.is_set() else "Истёк таймаут fpkg-cli")
            try:
                data, _ = process.communicate(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    decoded = data.decode("utf-8-sig", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        decoded = data.decode("utf-16", errors="replace")
    elif "\ufffd" in decoded and os.name == "nt":
        decoded = data.decode("oem", errors="replace")
    return process.returncode, decoded


def decode_bcd(value):
    if not value:
        return None
    value = value.strip().lower()
    if version_key(value):
        parts = value.split(".")
        return ".".join(parts[:2]) if all(int(part) == 0 for part in parts[2:]) else value
    match = re.search(r"\b0x([a-f0-9]{8}|[a-f0-9]{16})\b", value)
    if not match:
        return None
    digits = match[1] if len(match[1]) == 16 else match[1].zfill(8)[2:]
    if not digits[:4].isdigit() or digits[:4] == "0000":
        return None
    return f"{int(digits[:2])}.{digits[2:4]}"


def metadata_fields(raw):
    values = {}
    for line in re.sub(r"\x1b\[[0-9;]*m", "", raw).splitlines():
        label = re.match(r'^\s*["“]?([\w .()/-]+?)["”]?\s*[:=]\s*(\S.*?)\s*,?\s*$', line)
        if label:
            key = re.sub(r"[^a-z0-9]", "", label[1].lower())
            values[key] = label[2].strip().strip('"')
    return values


def parse_metadata(raw, path):
    values = metadata_fields(raw)

    def get(*keys):
        return next((values[key] for key in keys if values.get(key)), None)

    content_id = get("contentid", "pkgcontentid")
    identifier = TITLE_ID.search(get("titleid", "nptitleid") or content_id or "")
    # fpkg-cli reports the container format and the content role on separate lines.
    roles = [values[key] for key in ROLE_FIELDS if key in values]
    if any(EXTRA_CONTENT.search(role) for role in roles):
        return None
    if not identifier:
        raise MetadataError("В выводе fpkg-cli отсутствует Title ID")
    if not any(BASE_GAME.search(role) for role in roles):
        raise MetadataError("fpkg-cli не подтвердил, что это самостоятельная игра")
    title_id = identifier[0].upper()
    version = get("contentversion", "appver", "applicationversion", "version")
    if version:
        match = re.search(r"\b[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}\b", version)
        version = match[0] if match else None
    filename = FILE_TITLE.match(path.stem)
    return {
        "title_id": title_id, "platform": "PS5" if title_id.startswith("PPSA") else "PS4",
        "title": (get("title", "titleen", "titleenus", "titlenameenus", "defaulttitle", "name") or
                  (filename[1].strip() if filename else title_id))[:256],
        "version": version, "firmware": decode_bcd(get("requiredfirmware", "requiredsystemsoftwareversion",
                                                     "requiredsystemsoftware", "systemsoftwareversion", "systemver",
                                                     "systemversion", "firmware")),
        "version_uri": get("versionfileuri", "versionuri"), "content_id": content_id,
        "sdk": decode_bcd(get("sdk", "sdkversion")),
    }


def read_metadata(executable, path, stop):
    first = "ps4-info" if re.search(r"CUSA\d{5}", path.name, re.I) else "pkg-info"
    commands = (first, "ps4-info" if first == "pkg-info" else "pkg-info")
    parse_error = None
    for command in commands:
        code, raw = invoke(executable, [command, path], stop=stop)
        if code != 0:
            logger.warning("%r — fpkg-cli %s: код %s", path.name, command, code)
            continue
        try:
            metadata = parse_metadata(raw, path)
        except MetadataError as error:
            parse_error = error
            fields = metadata_fields(raw)
            diagnostics = ", ".join(f"{key}={fields[key][:160]!r}" for key in ROLE_FIELDS if key in fields)
            logger.warning("%r — fpkg-cli %s: код 0, %s; %s",
                           path.name, command, error, diagnostics or "поля типа отсутствуют")
            continue
        if metadata is None:
            logger.info("%r — fpkg-cli %s: код 0, дополнительный контент пропущен", path.name, command)
        else:
            logger.info("%r — fpkg-cli %s: код 0, %s %s", path.name, command,
                        metadata["title_id"], metadata["version"] or "версия неизвестна")
        return metadata
    raise parse_error or MetadataError("fpkg-cli не смог прочитать PKG")


def import_file(db, root_id, relpath, metadata, info):
    game = db.execute("SELECT id,title,overrides FROM games WHERE platform=? AND title_id=?",
                      (metadata["platform"], metadata["title_id"])).fetchone()
    if game:
        game_id = game["id"]
        if "title" not in json.loads(game["overrides"]) and game["title"] == metadata["title_id"]:
            db.execute("UPDATE games SET title=? WHERE id=?", (metadata["title"], game_id))
    else:
        game_id = db.execute("INSERT INTO games (platform,title_id,title,added_at) VALUES (?,?,?,?)",
                             (metadata["platform"], metadata["title_id"], metadata["title"], now())).lastrowid
    identity = json.dumps([metadata["content_id"], version_key(metadata["version"]),
                           info.st_size, metadata["firmware"]], separators=(",", ":"))
    record = db.execute(
        """INSERT INTO builds (game_id,identity,version,firmware,size_bytes,content_id,version_uri,sdk)
           VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(game_id,identity) DO UPDATE SET
           version_uri=COALESCE(excluded.version_uri,builds.version_uri), sdk=COALESCE(excluded.sdk,builds.sdk)
           RETURNING id""",
        (game_id, identity, metadata["version"], metadata["firmware"], info.st_size,
         metadata["content_id"], metadata["version_uri"], metadata["sdk"]),
    ).fetchone()[0]
    db.execute(
        """INSERT INTO files (root_id,relpath,size_bytes,mtime,build_id) VALUES (?,?,?,?,?)
           ON CONFLICT(root_id,relpath) DO UPDATE SET size_bytes=excluded.size_bytes,mtime=excluded.mtime,
                   build_id=excluded.build_id,available=1""",
        (root_id, relpath, info.st_size, info.st_mtime_ns, record),
    )
    return ("updated" if game else "added"), game_id, record


def scan(database, state, stop, publish, *, force=False):
    with database.connect() as db:
        roots = [dict(row) for row in db.execute("SELECT * FROM roots ORDER BY id")]
        executable = cli_path(db.execute("SELECT value FROM settings WHERE key='cli_path'").fetchone()[0])
    if not roots:
        raise MetadataError("Укажите папки коллекции в настройках")
    if executable is None:
        raise MetadataError("Укажите путь к fpkg-cli в настройках")

    art = Artwork()
    for root in roots:
        if stop.is_set():
            return
        base = Path(root["path"])
        seen = set()
        walk_errors = []
        try:
            accessible = base.is_dir()
            if accessible:
                base = base.resolve()
        except (OSError, RuntimeError):
            accessible = False
        if not accessible:
            with database.connect() as db:
                db.execute("UPDATE roots SET state='offline' WHERE id=?", (root["id"],))
            state["issues"].append(f"Папка коллекции недоступна — {base}")
            logger.warning("Папка коллекции недоступна: корень №%s", root["id"])
            publish()
            continue
        if WORK_DIRECTORY in (part.casefold() for part in base.parts):
            state["issues"].append("Рабочая папка _WorkHard исключена")
            publish()
            continue
        for directory, subdirs, names in os.walk(base, onerror=walk_errors.append, followlinks=False):
            def real_directory(name):
                entry = Path(directory, name)
                return name.casefold() != WORK_DIRECTORY and not entry.is_symlink() and not entry.is_junction()
            subdirs[:] = sorted(filter(real_directory, subdirs))
            for name in sorted(names):
                if stop.is_set():
                    return
                path = Path(directory, name)
                if path.suffix.casefold() != ".pkg":
                    continue
                relative = path.relative_to(base).as_posix()
                seen.add(relative)
                state["found"] += 1
                state["current"] = path.name
                publish()
                try:
                    result = scan_file(database, root["id"], base, path, relative, executable, stop, force, art)
                    state[result] += 1
                except (OSError, MetadataError, TimeoutError) as error:
                    if stop.is_set():
                        return
                    with database.connect() as db:
                        db.execute("UPDATE files SET available=0 WHERE root_id=? AND relpath=?",
                                   (root["id"], relative))
                    state["errors"] += 1
                    if len(state["issues"]) < 100:
                        message = str(error) if isinstance(error, (MetadataError, TimeoutError)) else "ошибка чтения файла"
                        state["issues"].append(f"{path.name} — {message}")
                    logger.warning("%r — %s", path.name, error.__class__.__name__)
                publish()
        if stop.is_set():
            return
        if walk_errors:
            state["issues"].append("Часть папки недоступна. Состояния пропущенных файлов сохранены.")
            state["errors"] += len(walk_errors)
        with database.connect() as db:
            db.execute("UPDATE roots SET state=? WHERE id=?",
                       ("partial" if walk_errors else "online", root["id"]))
            if not walk_errors:
                missing = [row["id"] for row in db.execute("SELECT id,relpath FROM files WHERE root_id=?", (root["id"],))
                           if row["relpath"] not in seen]
                db.executemany("UPDATE files SET available=0 WHERE id=?", [(item,) for item in missing])
    state.pop("current", None)


def scan_file(database, root_id, base, path, relative, executable, stop, force, art):
    if path.is_symlink() or not path.resolve().is_relative_to(base):
        return "skipped"
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size == 0 or time.time() - before.st_mtime < 2:
        raise MetadataError("Пустой файл, копирование или запись ещё не завершены")
    with database.connect() as db:
        existing = db.execute("SELECT id,size_bytes,mtime FROM files WHERE root_id=? AND relpath=?",
                              (root_id, relative)).fetchone()
        if not force and existing and (existing["size_bytes"], existing["mtime"]) == (before.st_size, before.st_mtime_ns):
            db.execute("UPDATE files SET available=1 WHERE id=?", (existing["id"],))
            return "skipped"
    metadata = read_metadata(executable, path, stop)
    after = path.stat()
    if stop.is_set():
        raise TimeoutError("Чтение прервано")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise MetadataError("Файл изменился во время чтения")
    if metadata is None:
        with database.connect() as db:
            db.execute("UPDATE files SET available=0 WHERE root_id=? AND relpath=?", (root_id, relative))
        return "skipped"
    with database.connect() as db:
        result, game_id, build_id = import_file(db, root_id, relative, metadata, after)
    if not stop.is_set():
        enrich_import(database, game_id, build_id, executable, path, metadata, art, stop)
    return result


def enrich_import(database, game_id, build_id, executable, path, metadata, art, stop):
    """Read selected PS5 metadata and fill only empty artwork fields."""
    with database.connect() as db:
        game = db.execute("SELECT title,platform,cover,background FROM games WHERE id=?", (game_id,)).fetchone()
    if not game:
        return

    publisher = PUBLISHER_METADATA.get((game["platform"], metadata["title_id"]), {})
    missing = {kind for kind in ("cover", "background") if not game[kind] and not publisher.get(kind)}
    candidates = {}
    content_id = metadata["content_id"]
    if missing and content_id and PS_STORE_PRODUCT.fullmatch(content_id):
        try:
            with httpx.Client(timeout=7, follow_redirects=False) as client:
                for asset in art.playstation(client, game["title"], content_id):
                    kind = asset["kind"]
                    if stop.is_set():
                        return
                    if kind in missing and kind not in candidates:
                        try:
                            candidates[kind] = (bounded_get(client, asset["url"], MAX_IMAGE), PS_STORE_SOURCE)
                        except (httpx.HTTPError, ValueError):
                            logger.info("Изображение PlayStation Store недоступно: %s, %s", content_id, asset["id"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError) as error:
            logger.info("Оформление PlayStation Store недоступно: %s, %s", content_id, type(error).__name__)

    if game["platform"] == "PS5":
        paths = []
        if "background" in missing and "background" not in candidates:
            paths.extend(("sce_sys/pic0.png", "sce_sys/pic1.png"))
        if not all(metadata.get(key) for key in ("version_uri", "version", "firmware")):
            paths.append("sce_sys/param.json")
        if paths:
            candidates.update(ps5_assets(database, build_id, executable, path, paths, stop))

    if ("cover" in missing and "cover" not in candidates and providers()["IGDB"]
            and game["title"] != metadata["title_id"] and not stop.is_set()):
        try:
            with httpx.Client(timeout=6, follow_redirects=False) as client:
                found = art.igdb(client, game["title"], game["platform"])
            exact = [item for item in found if item["kind"] == "cover" and item["exact"]]
            if len(exact) == 1:
                candidate = exact[0]
                candidates["cover"] = (art.download(candidate), f"IGDB · {candidate['id']}")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            logger.info("Обложка IGDB недоступна для карточки %s", game_id)
        stop.wait(.25)

    if candidates and not stop.is_set():
        with database.connect() as db:
            for kind, (data, source) in candidates.items():
                try:
                    url = store_image(database, data, kind, original=source == PS_STORE_SOURCE)
                    db.execute(f"UPDATE games SET {kind}=?,{kind}_source=? WHERE id=? AND {kind} IS NULL",
                               (url, source, game_id))
                except (OSError, ValueError):
                    logger.info("Изображение пропущено для карточки %s", game_id)


def ps5_assets(database, build_id, executable, path, paths, stop):
    candidates = {}
    try:
        with tempfile.TemporaryDirectory(dir=database.directory) as output:
            args = ["pkg-extract", path, "--output", output]
            for asset in paths:
                args.extend(("--include", asset))
            code, _ = invoke(executable, args, timeout=15, stop=stop)
            if code != 0 or stop.is_set():
                return {}
            output = Path(output).resolve()
            for asset in paths:
                file = output / asset
                if not file.is_file() or not file.resolve().is_relative_to(output):
                    continue
                with file.open("rb") as stream:
                    if asset.endswith(".png") and "background" not in candidates:
                        candidates["background"] = (stream.read(MAX_IMAGE + 1), "PKG")
                    if asset.endswith(".json"):
                        populate_params(database, build_id, stream.read(256 * 1024 + 1))
    except (OSError, TimeoutError):
        logger.info("Извлечённые метаданные PKG недоступны для сборки %s", build_id)
    return candidates


def populate_params(database, build_id, data):
    if len(data) > 256 * 1024:
        return
    try:
        params = json.loads(data)
        if not isinstance(params, dict):
            return
    except (ValueError, UnicodeError):
        return
    firmware = decode_bcd(str(params.get("requiredSystemSoftwareVersion", "")))
    version = params.get("contentVersion")
    if not isinstance(version, str) or not version_key(version):
        version = None
    uri = params.get("versionFileUri")
    if not isinstance(uri, str) or len(uri) > 512:
        uri = None
    with database.connect() as db:
        db.execute("""UPDATE builds SET version_uri=COALESCE(version_uri,?), version=COALESCE(version,?),
                         firmware=COALESCE(firmware,?) WHERE id=?""", (uri, version, firmware, build_id))
