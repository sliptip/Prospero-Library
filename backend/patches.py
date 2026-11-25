"""Retrieve patch metadata over HTTPS from Sony and the PlayStation patch archives."""
import json
import logging
import os
import re
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
import truststore

from . import __version__
from .artwork import bounded_body
from .catalog import library, version_key
from .database import now
from .scanner import decode_bcd

logger = logging.getLogger(__name__)

SOURCE = "Sony CDN"
PATCH_SITE = "ProsperoPatches"
PATCH_SITE_HOST = "prosperopatches.com"
ORBIS_PATCH_SITE = "OrbisPatches"
ORBIS_PATCH_HOST = "orbispatches.com"
PATCH_ARCHIVES = {"PPSA": (PATCH_SITE, PATCH_SITE_HOST), "CUSA": (ORBIS_PATCH_SITE, ORBIS_PATCH_HOST)}
PATCH_HOST_NAMES = {host: name for name, host in PATCH_ARCHIVES.values()}
PS5_HOST = "sgst.prod.dl.playstation.net"
PS4_HOST = "gs-sec.ww.np.dl.playstation.net"
MAX_MANIFEST = 1_500_000
URL_CREDENTIALS = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@")


def manifest_verifier():
    custom_ca = os.getenv("SSL_CERT_FILE") or os.getenv("SSL_CERT_DIR")
    if sys.platform == "win32" and not custom_ca:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return True


def certificate_error(error):
    while error is not None:
        if isinstance(error, ssl.SSLCertVerificationError):
            detail = getattr(error, "verify_message", None) or error.strerror or str(error)
            return " ".join(detail.split())[:200]
        error = error.__cause__
    return None


def transport_details(error):
    details = []
    for _ in range(5):
        if error is None:
            break
        message = URL_CREDENTIALS.sub(r"\1[скрыто]@", " ".join(str(error).split()))
        item = f"{type(error).__name__}: {message[:240]}"
        if item not in details:
            details.append(item)
        error = error.__cause__
    return " → ".join(details)


def log_response(response):
    source = PATCH_HOST_NAMES.get(response.url.host, SOURCE)
    logger.info("%s: HTTP %s, Content-Type %r", source, response.status_code,
                response.headers.get("content-type", "")[:120])


def fetch_manifest(client, url, kind, *, source=SOURCE, method="GET", **options):
    started = time.monotonic()
    logger.info("%s: %s %s %s", source, method, kind, url)
    try:
        with client.stream(method, url, **options) as response:
            data = bounded_body(response, MAX_MANIFEST)
    except httpx.HTTPStatusError as error:
        logger.warning("%s: запрос %s завершён за %.0f мс, HTTP %s",
                       source, kind, (time.monotonic() - started) * 1000, error.response.status_code)
        raise
    except (httpx.RequestError, ValueError) as error:
        logger.warning("%s: запрос %s завершён за %.0f мс, %s",
                       source, kind, (time.monotonic() - started) * 1000, transport_details(error))
        raise
    logger.info("%s: ответ (%s), %s байт за %.0f мс", source, kind, len(data), (time.monotonic() - started) * 1000)
    return data


def safe_manifest_url(url, title, manifest=False):
    clean = url.strip()
    try:
        parts = urlsplit(clean)
    except ValueError:
        return False
    if any(char in clean for char in ("\r", "\n", "\t")) or parts.scheme != "https" or parts.query or parts.fragment or not re.fullmatch(r"(PPSA|CUSA)\d{5}", title):
        return False
    if parts.netloc == PS5_HOST and title.startswith("PPSA"):
        if manifest:
            pattern = rf"/sgst/prod/00/{title}_00/app/info/\d+/f_[0-9a-fA-F]+/[A-Z0-9_-]+\.json"
        else:
            pattern = rf"/sgst/prod/00/np/{title}_00/[a-fA-F0-9-]+-version\.xml"
        return re.fullmatch(pattern, parts.path) is not None
    return (not manifest and parts.netloc == PS4_HOST and title.startswith("CUSA") and
            re.fullmatch(rf"/plo/np/{title}/(?:[a-fA-F0-9]{{64}}/)?{title}-ver\.xml", parts.path))


def normalized(key):
    return re.sub(r"[^a-z0-9]", "", key.lower())


def decode_manifest_firmware(key, value, title):
    text = str(value).strip()
    if title.startswith("PPSA") and key == "systemver" and re.fullmatch(r"[0-9]{1,10}", text):
        packed = int(text)
        if not 0 < packed <= 0xFFFFFFFF:
            return None
        digits = f"{packed:08x}"
        if not digits[:4].isdigit() or digits[:4] == "0000":
            return None
        short = f"{int(digits[:2])}.{digits[2:4]}"
        if digits[4:].isdigit() and digits[4:] != "0000":
            return f"{short}.{digits[4:6]}.{digits[6:]}"
        return short
    return decode_bcd(text)


def read_patch(fields, title):
    source = {normalized(key): val for key, val in fields.items()}
    version = next((str(source[key]) for key in (
        "contentversion", "contentver", "appversion", "appver", "version"
    ) if key in source and version_key(str(source[key]))), None)
    if version is None:
        return None
    firmware = next((decoded for key in (
        "requiredsystemsoftwareversion", "requiredsystemversion", "requiredsystemver",
        "systemsoftwareversion", "systemversion", "systemver", "requiredfirmware"
    ) if source.get(key) is not None and (decoded := decode_manifest_firmware(key, source[key], title))), None)
    size = str(source.get("size", source.get("sizebytes", "")))
    return {"version": version, "firmware": firmware, "size_bytes": int(size) if size.isdigit() and len(size) <= 15 else None}


def parse_xml(data, title):
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data.replace(b"\0", b""), re.I):
        raise ValueError("Недопустимая XML-структура")
    root = ET.fromstring(data)
    declared = {value.removesuffix("_00").upper() for key, value in root.attrib.items()
                if normalized(key) in {"titleid", "nptitleid"}}
    if declared and declared != {title}:
        raise ValueError(f"Title ID в XML должен совпадать с карточкой {title}")
    patches, manifests = [], set()
    for node in root.iter():
        tag = normalized(node.tag.rsplit("}", 1)[-1])
        fields = dict(node.attrib)
        fields.update({child.tag.rsplit("}", 1)[-1]: child.text for child in node
                       if len(child) == 0 and child.text})
        urls = [*fields.values(), node.text or ""]
        manifests.update(value.strip() for value in urls if safe_manifest_url(value, title, manifest=True))
        if tag in {"package", "patch", "update", "hybridpackage", "latestpatch"}:
            patch = read_patch(fields, title)
            if patch:
                patches.append(patch)
    return patches, manifests


def parse_json(data, title):
    root = json.loads(data)
    if not isinstance(root, dict):
        raise ValueError("Неизвестный формат манифеста")
    nodes = [root]
    for key in ("patch", "latestPatch", "package", "application", "params", "param", "paramJson"):
        value = root.get(key)
        if isinstance(value, dict):
            nodes.append(value)
    for key in ("patches", "updates"):
        if isinstance(root.get(key), list):
            nodes.extend(item for item in root[key] if isinstance(item, dict))
    return [row for node in nodes if (row := read_patch(node, title))]


def retrieve(client, uri, title):
    if not safe_manifest_url(uri, title):
        return []
    rows, manifests = parse_xml(fetch_manifest(client, uri.strip(), "XML"), title)
    logger.info("Sony CDN: %s, XML содержит %s записей патчей и %s ссылок на JSON",
                title, len(rows), len(manifests))
    if rows and all(row["firmware"] and row["size_bytes"] for row in rows):
        return ordered_patches(rows)
    first_error = None
    for manifest in sorted(manifests)[:4]:
        try:
            rows.extend(parse_json(fetch_manifest(client, manifest, "JSON"), title))
        except (json.JSONDecodeError, UnicodeError) as error:
            logger.warning("Sony CDN: %s, ошибка разбора JSON: %s", title, transport_details(error))
            first_error = first_error or error
        except (httpx.HTTPError, ValueError) as error:
            first_error = first_error or error
    if not rows and first_error:
        raise first_error
    return ordered_patches(rows)


def ordered_patches(rows):
    selected = {}
    for item in rows:
        key = version_key(item["version"])
        previous = selected.get(key)
        if previous is None:
            selected[key] = item
        else:
            selected[key] = {**previous, **{field: previous[field] or item[field] for field in ("firmware", "size_bytes")}}
    return sorted(selected.values(), key=lambda row: version_key(row["version"]), reverse=True)


def save_check(db, game_id, state, stamp, message, latest, rows=(), *, source=SOURCE):
    if state == "ok":
        db.executemany(
            """INSERT INTO patch_history (game_id,version,firmware,size_bytes,source,checked_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(game_id,version,source) DO UPDATE SET firmware=COALESCE(excluded.firmware,patch_history.firmware),
                 size_bytes=COALESCE(excluded.size_bytes,patch_history.size_bytes), checked_at=excluded.checked_at""",
            [(game_id, row["version"], row["firmware"], row["size_bytes"], source, stamp) for row in rows],
        )
    db.execute(
        """INSERT INTO checks VALUES (?,?,?,?,?,?,?) ON CONFLICT(game_id) DO UPDATE SET state=excluded.state,
             source=excluded.source, checked_at=excluded.checked_at,
             last_success=COALESCE(excluded.last_success,checks.last_success), message=excluded.message,
             latest_version=CASE WHEN excluded.state='error' THEN checks.latest_version ELSE excluded.latest_version END""",
        (game_id, state, source, stamp, stamp if state == "ok" else None, message, latest),
    )


class PatchPage(HTMLParser):
    def __init__(self, title):
        super().__init__()
        self.title = title
        self.key = None

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag != "div" or fields.get("id") != "dynpatch":
            return
        title, key = fields.get("data-titleid"), fields.get("data-key")
        if params := fields.get("data-loadparams"):
            try:
                values = json.loads(params.replace("'", '"'))
            except ValueError:
                return
            if isinstance(values, dict):
                title, key = values.get("titleid"), values.get("key")
        if title == self.title and isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key):
            self.key = key


def retrieve_patch_site(client, title):
    archive = PATCH_ARCHIVES.get(title[:4])
    if not archive or not re.fullmatch(r"(?:CUSA|PPSA)[0-9]{5}", title):
        return []
    source, host = archive
    site_url = f"https://{host}"
    url = f"{site_url}/{title}"
    page = PatchPage(title)
    page.feed(fetch_manifest(client, url, "страница Title ID", source=source).decode("utf-8"))
    if not page.key:
        raise ValueError("Страница не содержит публичного списка патчей для этого Title ID")
    # This is the public version list requested when the title page opens.
    data = fetch_manifest(client, f"{site_url}/api/internal/loadpatches", "список патчей",
                          source=source, method="POST", content=json.dumps({"titleid": title, "key": page.key}),
                          headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                   "Accept": "application/json", "Origin": site_url, "Referer": url})
    result = json.loads(data)
    if (not isinstance(result, dict) or result.get("success") is not True or result.get("titleid") != title or
            not isinstance(result.get("patches"), list)):
        raise ValueError("Ответ списка патчей не подтверждает Title ID")
    entries = result["patches"]
    rows = [row for entry in entries if isinstance(entry, dict) and (row := read_patch(entry, title))]
    if len(rows) != len(entries):
        raise ValueError("Список содержит патчи в неизвестном формате")
    return ordered_patches(rows)


def check_all(database, report, stop, publish, *, game_id=None, game_ids=None):
    with database.connect() as db:
        games = (library(db, game_id) if game_ids is None
                 else [game for new_id in game_ids for game in library(db, new_id)])
    verifier = manifest_verifier()
    if verifier is not True:
        logger.info("Проверка TLS через системное хранилище Windows")
    with httpx.Client(timeout=8, follow_redirects=False, verify=verifier,
                      headers={"User-Agent": f"Prospero-Library/{__version__}"},
                      event_hooks={"response": [log_response]}) as client:
        for game in games:
            if stop.is_set():
                return
            report["found"] += 1
            report["current"] = game["title"]
            publish()
            checked = check_one(database, client, game, stop)
            if checked is None:
                return
            state, message = checked
            report[{"ok": "updated", "unknown": "skipped", "error": "errors"}[state]] += 1
            if message:
                logger.log(logging.WARNING if state == "error" else logging.INFO,
                           "Проверка %s: %s", (game.get("package_identity") or game)["title_id"], message)
            if message and len(report["issues"]) < 100:
                report["issues"].append(f"{game['title']} — {message}")
            publish()
            if stop.wait(.25):
                return


def check_sony(client, uri, title):
    rows = []
    state = "unknown"
    message = "Ссылка на манифест Sony отсутствует"
    if uri and safe_manifest_url(uri, title):
        try:
            rows = retrieve(client, uri, title)
            state = "ok" if rows else "unknown"
            message = None if rows else "Манифест не содержит подтверждённой версии"
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 404:
                message = "Манифест не найден (HTTP 404)"
            else:
                message = f"Ошибка источника (HTTP {status})"
                state = "error"
        except httpx.TimeoutException:
            state, message = "error", "Sony CDN не ответил вовремя"
        except httpx.ConnectError as error:
            reason = str(error).casefold()
            detail = certificate_error(error)
            if "untrusted root" in reason or ("корневом сертификате" in reason and "довер" in reason):
                message = "Windows не доверяет корневому сертификату Sony CDN"
            elif detail or "certificate_verify_failed" in reason:
                message = "Не удалось проверить TLS-сертификат Sony CDN"
                if detail:
                    message += f" ({detail})"
            elif any(text in reason for text in ("getaddrinfo failed", "name or service not known", "nodename nor servname")):
                message = "DNS не смог разрешить адрес Sony CDN"
            else:
                message = "Не удалось подключиться к Sony CDN (ConnectError)"
            state = "error"
        except httpx.RequestError as error:
            state, message = "error", f"Сбой при получении манифеста Sony CDN ({type(error).__name__})"
        except ET.ParseError as error:
            line, column = error.position
            state, message = "error", f"Некорректный XML манифеста Sony CDN (строка {line}, столбец {column + 1})"
        except json.JSONDecodeError as error:
            state, message = "error", f"Некорректный JSON манифеста Sony CDN (строка {error.lineno}, столбец {error.colno})"
        except (ValueError, TypeError, AttributeError) as error:
            state, message = "error", f"Не удалось разобрать манифест Sony CDN ({type(error).__name__})"
    return rows, state, message


def check_one(database, client, game, stop):
    stamp = now()
    title = (game.get("package_identity") or game)["title_id"]
    source = SOURCE
    rows, state, message = check_sony(client, game["version_uri"], title)
    archive = PATCH_ARCHIVES.get(title[:4])
    if not rows and archive and not stop.is_set():
        source = archive[0]
        logger.info("%s: %s. Проверяем %s", title, message, source)
        try:
            rows = retrieve_patch_site(client, title)
            if rows:
                state, message = "ok", None
            else:
                message += f". В {source} нет патчей для {title}"
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status == 404:
                message += f". В {source} не найдена карточка {title} (HTTP 404)"
            else:
                state, message = "error", f"{message}. {source}: HTTP {status}"
        except (httpx.RequestError, ValueError) as error:
            state, message = "error", f"{message}. {source}: {transport_details(error)}"
    if stop.is_set():
        return None
    if rows:
        logger.info("%s: %s, версий в ответе %s, последняя %s, прошивка %s",
                    source, title, len(rows), rows[0]["version"], rows[0]["firmware"] or "не указана")
    with database.connect() as db:
        if db.execute("SELECT 1 FROM games WHERE id=?", (game["id"],)).fetchone():
            save_check(db, game["id"], state, stamp, message, rows[0]["version"] if rows else None, rows, source=source)
    return state, message
