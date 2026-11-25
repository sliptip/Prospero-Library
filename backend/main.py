import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal
from zipfile import BadZipFile

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, catalog, patches, scanner
from .artwork import (ART_NAME, MAX_IMAGE, PS_STORE_HOST, PS_STORE_SOURCE, Artwork, playstation_about,
                      playstation_url, providers, store_image)
from .backup import BUNDLE_NAME, create_backup, restore_backup
from .database import BASE, DEFAULT_DATA, Database
from .jobs import Jobs

TITLE = "Prospero Library"
LOG_TAIL_BYTES = 256 * 1024
loopback = ["localhost", "127.0.0.1", "::1"]
logger = logging.getLogger(__name__)


class SettingsInput(BaseModel):
    cli_path: str = Field(max_length=2048)
    ps5_firmware: catalog.Version | None = None
    ps4_firmware: catalog.Version | None = None


class RootInput(BaseModel):
    path: str = Field(min_length=1, max_length=2048)


class RootsInput(BaseModel):
    roots: list[RootInput] = Field(min_length=1)


class ScanInput(BaseModel):
    force: bool = False


class BackupInput(BaseModel):
    name: str = Field(max_length=80)


class ArtworkInput(BaseModel):
    token: str = Field(max_length=80)


class GameSwap(BaseModel):
    source_id: int = Field(gt=0)
    target_id: int = Field(gt=0)


def create_app(data_directory=None, frontend_directory=None):
    load_dotenv(BASE / ".env")
    database = Database(data_directory or os.getenv("PROSPERO_DATA") or DEFAULT_DATA)
    jobs = Jobs(database)
    artwork = Artwork()
    candidates = {}
    log_path = database.directory / "logs" / "prospero.log"
    front = Path(frontend_directory or os.getenv("PROSPERO_FRONTEND") or BASE / "frontend" / "dist")
    extra_hosts = os.getenv("PROSPERO_ALLOWED_HOSTS", "").split(",")
    allowed_hosts = [*loopback, *(value.strip() for value in extra_hosts if value.strip() and "*" not in value)]
    dev_origin = os.getenv("PROSPERO_DEV_ORIGIN")

    @asynccontextmanager
    async def lifespan(app):
        database.initialize()
        log_path.parent.mkdir(exist_ok=True)
        handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        app.state.log_handler = handler
        package_logger = logging.getLogger("backend")
        package_logger.addHandler(handler)
        package_logger.setLevel(logging.INFO)
        package_logger.info("Prospero Library запущена")
        try:
            yield
        finally:
            jobs.close()
            package_logger.info("Prospero Library остановлена")
            package_logger.removeHandler(handler)
            handler.close()

    app = FastAPI(title=TITLE, version=__version__, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts, www_redirect=False)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if (origin and origin not in {expected, dev_origin}) or (
                request.headers.get("sec-fetch-site") in {"cross-site", "same-site"} and
                (not dev_origin or origin != dev_origin)
            ):
                return JSONResponse({"detail": "Запрос поступил с другого сайта"}, status_code=403)
            limit = 1024 * 1024 + MAX_IMAGE if request.url.path.endswith("/artwork") else 1024 * 1024
            size = request.headers.get("content-length", "0")
            if not size.isascii() or not size.isdigit() or len(size) > 9 or int(size) > limit:
                return JSONResponse({"detail": "Слишком большой запрос"}, status_code=413)
            if "transfer-encoding" in request.headers:
                return JSONResponse({"detail": "Укажите размер запроса"}, status_code=411)
        try:
            response = await call_next(request)
        except Exception:
            route = request.scope.get("route")
            logger.exception("Ошибка HTTP %s %s", request.method, getattr(route, "path", ""))
            raise
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            route = request.scope.get("route")
            if route and response.status_code >= 400:
                logger.warning("HTTP %s %s — %s", request.method, route.path, response.status_code)
        if request.url.path not in {"/docs", "/redoc"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
                f"img-src 'self' https://images.igdb.com https://cdn.steamgriddb.com https://{PS_STORE_HOST} blob:; "
                "style-src 'self' 'unsafe-inline'"
            )
        return response

    def required_game(db, game_id):
        games = catalog.library(db, game_id)
        if not games:
            raise HTTPException(404, "Игра не найдена")
        return games[0]

    @app.get("/api/health")
    def health():
        return {"app": "prospero-library", "version": app.version}

    @app.get("/api/logs")
    def diagnostic_log(download: bool = False):
        try:
            with app.state.log_handler.lock, log_path.open("rb") as source:
                size = source.seek(0, os.SEEK_END)
                offset = max(0, size - LOG_TAIL_BYTES)
                source.seek(offset)
                data = source.read(LOG_TAIL_BYTES)
        except FileNotFoundError:
            data, offset = b"", 0
        except OSError as error:
            raise HTTPException(503, "Журнал временно недоступен") from error
        if offset:
            _, _, data = data.partition(b"\n")
            data = "… Показан конец журнала …\n".encode() + data
        text = data.decode("utf-8", errors="replace")
        if download:
            return PlainTextResponse(text, headers={"Content-Disposition": 'attachment; filename="prospero-log.txt"'})
        return {"text": text}

    @app.delete("/api/logs")
    def clear_diagnostic_log():
        handler = app.state.log_handler
        try:
            with handler.lock:
                for index in range(1, handler.backupCount + 1):
                    log_path.with_name(f"{log_path.name}.{index}").unlink(missing_ok=True)
                if handler.stream:
                    handler.flush()
                    handler.stream.seek(0)
                    handler.stream.truncate()
        except OSError as error:
            raise HTTPException(503, "Не удалось очистить журнал") from error
        return {"text": ""}

    @app.get("/api/games")
    def games():
        with database.connect() as db:
            return catalog.library(db)

    @app.post("/api/games/order")
    def swap_games(item: GameSwap):
        try:
            with database.connect() as db:
                return catalog.swap_games(db, item.source_id, item.target_id)
        except ValueError as error:
            raise HTTPException(404, "Одна из карточек отсутствует") from error

    @app.get("/api/games/{game_id}")
    def game(game_id: int):
        with database.connect() as db:
            return required_game(db, game_id)

    @app.get("/api/games/{game_id}/about")
    def game_about(game_id: int):
        with database.connect() as db:
            content_id = required_game(db, game_id)["content_id"]
            if not playstation_url(content_id):
                return None
            cached = db.execute("SELECT details FROM store_about WHERE content_id=?", (content_id,)).fetchone()
        if cached:
            return json.loads(cached["details"])

        try:
            with httpx.Client(timeout=httpx.Timeout(18, connect=5), follow_redirects=False) as client:
                details = playstation_about(client, content_id)
        except httpx.HTTPStatusError as error:
            raise HTTPException(502, "PlayStation Store временно недоступен") from error
        except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError, ImportError) as error:
            raise HTTPException(502, "Не удалось получить описание из PlayStation Store") from error
        if details:
            with database.connect() as db:
                db.execute(
                    """INSERT INTO store_about(content_id,language,details) VALUES (?,?,?)
                       ON CONFLICT(content_id) DO UPDATE SET language=excluded.language, details=excluded.details
                       WHERE store_about.language != 'ru' OR excluded.language = 'ru'""",
                    (content_id, details["language"], json.dumps(details, ensure_ascii=False)),
                )
        return details

    @app.post("/api/games", status_code=201)
    def add_game(item: catalog.GameInput):
        try:
            with database.connect() as db:
                return catalog.save_game(db, item)
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "Игра с таким Title ID уже есть") from error

    @app.put("/api/games/{game_id}")
    def update_game(game_id: int, item: catalog.GameInput):
        try:
            with database.connect() as db:
                required_game(db, game_id)
                return catalog.save_game(db, item, game_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.delete("/api/games/{game_id}")
    def delete_game(game_id: int):
        with jobs.lock, database.connect() as db:
            if jobs.active:
                raise HTTPException(409, "Дождитесь завершения текущей операции")
            if not db.execute("DELETE FROM games WHERE id=?", (game_id,)).rowcount:
                raise HTTPException(404, "Игра не найдена")
            order = catalog.settings(db).get("game_order")
            if order is not None:
                catalog.store_game_order(db, [value for value in json.loads(order) if value != game_id])
            catalog.set_display_identity(db, game_id, None, None)
        return {"id": game_id}

    @app.get("/media/{name}")
    def media(name: str):
        path = database.media / name
        if not ART_NAME.fullmatch(name) or not path.is_file() or path.is_symlink():
            raise HTTPException(404, "Изображение отсутствует")
        media_type = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg"}[path.suffix]
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/games/{game_id}/artwork")
    def upload_artwork(game_id: int, kind: Literal["cover", "background"], file: UploadFile = File()):
        try:
            with database.connect() as db:
                required_game(db, game_id)
                url = store_image(database, file.file.read(MAX_IMAGE + 1), kind)
                db.execute(f"UPDATE games SET {kind}=?, {kind}_source=? WHERE id=?", (url, "Вручную", game_id))
                return required_game(db, game_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        finally:
            file.file.close()

    @app.post("/api/games/{game_id}/artwork/search")
    def search_artwork(game_id: int):
        with database.connect() as db:
            item = required_game(db, game_id)
        found, issues = artwork.search(item["title"], (item["package_identity"] or item)["platform"], item["content_id"])
        stamp = time.monotonic()
        with database.lock:
            for key, (_, expires, _) in list(candidates.items()):
                if expires < stamp:
                    candidates.pop(key, None)
            for display in found:
                token = secrets.token_urlsafe(20)
                candidates[token] = (game_id, stamp + 900, dict(display))
                display["token"] = token
        return {"items": found, "issues": issues, "sources": providers()}

    @app.post("/api/games/{game_id}/artwork/select")
    def choose_artwork(game_id: int, value: ArtworkInput):
        with database.lock:
            candidate = candidates.get(value.token)
        if not candidate or candidate[0] != game_id or candidate[1] < time.monotonic():
            raise HTTPException(404, "Вариант устарел. Повторите поиск.")
        image = candidate[2]
        kind = image["kind"]
        try:
            content = artwork.download(image)
            with database.connect() as db:
                required_game(db, game_id)
                url = store_image(database, content, kind, original=image["source"] == PS_STORE_SOURCE)
                source = f"{image['source']} · {image['id']}"
                db.execute(f"UPDATE games SET {kind}=?, {kind}_source=? WHERE id=?", (url, source, game_id))
                return required_game(db, game_id)
        except httpx.HTTPError as error:
            raise HTTPException(502, "Источник изображения недоступен") from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/settings")
    def get_settings():
        with database.connect() as db:
            config = catalog.settings(db)
            config.pop("game_order", None)
            config.pop("card_identities", None)
            roots = [dict(row) for row in db.execute("SELECT * FROM roots ORDER BY id")]
        return {**config, "roots": roots, "providers": providers(), "cli_local": scanner.local_cli() is not None}

    @app.put("/api/settings")
    def save_settings(item: SettingsInput):
        with database.connect() as db:
            db.executemany("UPDATE settings SET value=? WHERE key=?", [
                (value.strip() if value else "", key) for key, value in item.model_dump().items()
            ])
        return get_settings()

    @app.post("/api/settings/cli")
    def check_cli():
        with database.connect() as db:
            path = catalog.settings(db)["cli_path"]
        cli = scanner.cli_path(path)
        if cli is None:
            return {"available": False, "summary": "fpkg-cli не найден"}
        try:
            result, output = scanner.invoke(cli, ["info"], timeout=12)
        except (OSError, TimeoutError):
            return {"available": False, "summary": "Не удалось запустить fpkg-cli"}
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return {"available": result == 0, "summary": "\n".join(lines[:8]) if result == 0 else "fpkg-cli вернул ошибку"}

    @app.post("/api/settings/cli/copy")
    def copy_cli():
        with jobs.lock:
            if jobs.active:
                raise HTTPException(409, "Дождитесь завершения текущей операции")
            with database.connect() as db:
                path = catalog.settings(db)["cli_path"]
            try:
                scanner.copy_local_cli(path)
            except scanner.MetadataError as error:
                logger.warning("Копирование fpkg-cli: %s", error)
                raise HTTPException(422, str(error)) from error
            except (OSError, TimeoutError) as error:
                logger.warning("Копирование fpkg-cli: %s", error)
                raise HTTPException(422, "Не удалось скопировать или запустить fpkg-cli") from error
            logger.info("fpkg-cli доступен из папки tools/fpkg-cli")
        return get_settings()

    def root_path(item):
        value = item.path.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        try:
            path = Path(value).expanduser()
            if not path.is_absolute() or scanner.WORK_DIRECTORY in (part.casefold() for part in path.parts):
                raise HTTPException(422, "Укажите абсолютный путь к готовой коллекции, вне _WorkHard")
            resolved = path.resolve()
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(422, "Не удалось проверить путь к папке") from error
        if scanner.WORK_DIRECTORY in (part.casefold() for part in resolved.parts):
            raise HTTPException(422, "Укажите папку вне _WorkHard")
        return str(resolved)

    def add_roots(items):
        try:
            with jobs.lock, database.connect() as db:
                if jobs.active:
                    raise HTTPException(409, "Дождитесь завершения текущей операции")
                paths = [root_path(item) for item in items]
                return [db.execute("INSERT INTO roots(path) VALUES(?)", (path,)).lastrowid for path in paths]
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "Эта папка уже добавлена") from error

    @app.post("/api/roots", status_code=201)
    def add_root(item: RootInput):
        return {"id": add_roots([item])[0]}

    @app.post("/api/roots/batch", status_code=201)
    def add_root_batch(item: RootsInput):
        return {"ids": add_roots(item.roots)}

    @app.put("/api/roots/{root_id}")
    def move_root(root_id: int, item: RootInput):
        try:
            with jobs.lock, database.connect() as db:
                if jobs.active:
                    raise HTTPException(409, "Дождитесь завершения текущей операции")
                updated = db.execute("UPDATE roots SET path=?,state='unknown' WHERE id=?", (root_path(item), root_id))
                if not updated.rowcount:
                    raise HTTPException(404, "Папка не найдена")
                return {"id": root_id}
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "Эта папка уже добавлена") from error

    @app.delete("/api/roots/{root_id}")
    def delete_root(root_id: int):
        with jobs.lock, database.connect() as db:
            if jobs.active:
                raise HTTPException(409, "Дождитесь завершения текущей операции")
            if not db.execute("DELETE FROM roots WHERE id=?", (root_id,)).rowcount:
                raise HTTPException(404, "Папка не найдена")
        return {"id": root_id}

    def job_start(kind, worker, **options):
        try:
            return jobs.start(kind, worker, **options)
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/scan", status_code=202)
    def start_scan(item: ScanInput):
        return job_start("scan", scanner.scan, force=item.force)

    @app.post("/api/updates", status_code=202)
    def start_updates(game_id: int | None = None):
        if game_id is not None:
            with database.connect() as db:
                required_game(db, game_id)
        return job_start("updates", patches.check_all, game_id=game_id)

    @app.get("/api/jobs/latest")
    def latest_job():
        return jobs.get()

    @app.get("/api/jobs/{job_id}")
    def read_job(job_id: int):
        result = jobs.get(job_id)
        if result is None:
            raise HTTPException(404, "Операция не найдена")
        return result

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int):
        result = jobs.cancel(job_id)
        if result is None:
            raise HTTPException(404, "Операция не найдена")
        return result

    def ready_backup(name):
        path = database.backups / name
        if not BUNDLE_NAME.fullmatch(name) or not path.is_file() or path.is_symlink():
            raise HTTPException(404, "Копия не найдена")
        return path

    @app.get("/api/backups")
    def list_backups():
        return sorted([p.name for p in database.backups.iterdir()
                       if BUNDLE_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()], reverse=True)

    @app.post("/api/backups", status_code=201)
    def backup_create():
        try:
            return {"name": create_backup(database).name}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/backups/{name}")
    def backup_download(name: str):
        return FileResponse(ready_backup(name), media_type="application/zip", filename=name)

    @app.post("/api/backups/restore")
    def backup_restore(item: BackupInput):
        with jobs.lock:
            if jobs.active:
                raise HTTPException(409, "Дождитесь завершения текущей операции")
            try:
                result = restore_backup(database, ready_backup(item.name))
                return {"protective_backup": result.name}
            except (ValueError, BadZipFile) as error:
                raise HTTPException(422, str(error)) from error

    for static in ("assets", "art"):
        directory = front / static
        if directory.is_dir():
            app.mount("/" + static, StaticFiles(directory=directory), name=static)

    @app.get("/", include_in_schema=False)
    @app.get("/games/{game_id}", include_in_schema=False)
    @app.get("/settings", include_in_schema=False)
    @app.get("/updates", include_in_schema=False)
    def ui(game_id: str | None = None):
        if not (front / "index.html").is_file():
            return JSONResponse({"detail": "Соберите frontend: npm ci && npm run build"}, status_code=503)
        return FileResponse(front / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
