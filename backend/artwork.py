import hashlib
import io
import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import date
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE = 16 * 1024 * 1024
IMAGE_SUFFIXES = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
ART_NAME = re.compile(r"[a-f0-9]{64}\.(?:webp|png|jpg)")
PS_STORE_HOST = "image.api.playstation.com"
PS_STORE_SOURCE = "PlayStation Store"
PS_STORE_PRODUCT = re.compile(r"[A-Z]{2}[0-9]{4}-(?:PPSA|CUSA)[0-9]{5}_00-[A-Z0-9]{16}")
IMAGE_HOSTS = {"images.igdb.com", "cdn.steamgriddb.com", PS_STORE_HOST}


def store_image(database, content, kind, *, original=False):
    if len(content) > MAX_IMAGE:
        raise ValueError("Изображение больше 16 МБ")
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in IMAGE_SUFFIXES or image.width * image.height > 25_000_000:
                raise ValueError("Нужен PNG, JPEG или WebP до 25 мегапикселей")
            if min(image.size) < 32:
                raise ValueError("Изображение слишком маленькое")
            if original:
                suffix = IMAGE_SUFFIXES[image.format]
                image.load()
                data = content
            else:
                image = ImageOps.exif_transpose(image).convert("RGB")
                if kind == "cover":
                    width = min(image.width, 1000, image.height * 2 // 3)
                    image = ImageOps.fit(image, (width, width * 3 // 2))
                else:
                    image.thumbnail((1920, 1200))
                converted = io.BytesIO()
                image.save(converted, format="WEBP", quality=91)
                data, suffix = converted.getvalue(), ".webp"
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("Не удалось прочитать изображение") from error
    name = hashlib.sha256(data).hexdigest() + suffix
    destination = database.media / name
    if not destination.exists():
        with tempfile.NamedTemporaryFile(dir=database.media, delete=False) as temp:
            temp.write(data)
            path = temp.name
        try:
            os.replace(path, destination)
        finally:
            if os.path.exists(path):
                os.unlink(path)
    return "/media/" + name


def bounded_get(client, url, maximum):
    with client.stream("GET", url) as response:
        return bounded_body(response, maximum)


def bounded_body(response, maximum):
    response.raise_for_status()
    data = bytearray()
    for piece in response.iter_bytes():
        if len(data) + len(piece) > maximum:
            raise ValueError("Ответ источника слишком большой")
        data.extend(piece)
    return bytes(data)


def image_url(url):
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.netloc in IMAGE_HOSTS and
            not parts.fragment and len(parts.path) < 512)


def comparable(title):
    title = unicodedata.normalize("NFKC", title).casefold()
    title = re.sub(r"[’'™®]", "", title)
    return re.sub(r"[^\w]+", " ", title).strip()


def providers():
    return {
        PS_STORE_SOURCE: True,
        "IGDB": bool(os.getenv("IGDB_CLIENT_ID") and (os.getenv("IGDB_ACCESS_TOKEN") or os.getenv("IGDB_CLIENT_SECRET"))),
        "SteamGridDB": bool(os.getenv("STEAMGRIDDB_API_KEY")),
    }


def playstation_url(content_id, locale="en-us"):
    if content_id and PS_STORE_PRODUCT.fullmatch(content_id):
        return f"https://store.playstation.com/{locale}/product/{content_id}"
    return None


def playstation_props(page, content_id=None):
    script = re.search(rb'<script\b[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not script:
        return {}
    props = json.loads(script[1])["props"]["pageProps"]
    return props if content_id is None or props.get("productId") == content_id else {}


def playstation_cache(props, slot):
    fragment = props.get("batarangs", {}).get(slot, {}).get("text", "")
    payload = re.search(r"<script\b[^>]*>(.*?)</script>", fragment, re.S)
    if not payload:
        return {}
    return json.loads(payload[1]).get("cache", {})


def playstation_slot(props, slot, content_id):
    product = playstation_cache(props, slot).get("Product:" + content_id, {})
    return product if product.get("id") == content_id else {}


def playstation_media(page, content_id, title):
    props = playstation_props(page, content_id)
    for slot in ("media-carousel", "background-image"):
        product = playstation_slot(props, slot, content_id)
        media = product.get("media", [])
        if not media:
            continue
        role_urls = {
            item.get("role"): item["url"] for item in media
            if isinstance(item, dict) and item.get("type") == "IMAGE"
            and isinstance(item.get("url"), str) and image_url(item["url"])
            and urlsplit(item["url"]).netloc == PS_STORE_HOST
        }
        roles = (
            ("cover", "PORTRAIT_BANNER"), ("cover", "MASTER"),
            ("background", "GAMEHUB_COVER_ART"), ("background", "SIXTEEN_BY_NINE_BANNER"),
        )
        return [
            {"title": title, "id": f"{content_id}/{role}", "kind": kind,
             "source": PS_STORE_SOURCE, "url": role_urls[role], "exact": True}
            for kind, role in roles if role in role_urls
        ]
    return []


class DescriptionText(HTMLParser):
    blocks = {"p", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.ignored_tag = None

    def handle_starttag(self, tag, attrs):
        if self.ignored_tag:
            return
        if tag in {"script", "style", "template"}:
            self.ignored_tag = tag
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in self.blocks:
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        if self.ignored_tag:
            if tag == self.ignored_tag:
                self.ignored_tag = None
        elif tag in self.blocks:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self.ignored_tag:
            self.parts.append(data)

    def text(self):
        lines = "\n".join(line.strip() for line in "".join(self.parts).splitlines())
        return re.sub(r"\n{3,}", "\n\n", lines).strip()


def description_text(descriptions):
    for kind in ("LONG", "SHORT"):
        for entry in descriptions:
            if not isinstance(entry, dict) or entry.get("type") != kind:
                continue
            value = entry.get("value")
            if not isinstance(value, str) or not value.strip():
                continue
            parser = DescriptionText()
            parser.feed(value)
            parser.close()
            if text := parser.text():
                return text
    return None


def store_languages(info, field, platform):
    by_platform = info.get(field + "ByPlatform") or []
    codes = next((row[field] for row in by_platform if isinstance(row, dict)
                  and row.get("platform") == platform and isinstance(row.get(field), list) and row[field]), None)
    if codes is None and (not info.get("platforms") or info["platforms"] == [platform]):
        codes = info.get(field)
    if not isinstance(codes, list):
        return None
    aliases = {"ch": "zh-Hant", "zh": "zh-Hans"}
    normalized = list(dict.fromkeys(aliases.get(code.lower(), code.replace("_", "-")) for code in codes
                                   if isinstance(code, str) and re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*", code)))
    return normalized or None


def prefer_languages(about, metadata):
    if metadata:
        for key, languages in metadata["localization"].items():
            about["localization"][key] = languages or about["localization"][key]


def playstation_details(props, content_id, locale):
    info = playstation_slot(props, "info", content_id)
    title = playstation_slot(props, "game-title", content_id)
    if not (info or title):
        return None

    description = description_text([*(info.get("descriptions") or []), *(title.get("descriptions") or [])])
    genre_values = info.get("localizedGenres") or title.get("localizedGenres") or []
    genres = list(dict.fromkeys(genre["value"] for genre in genre_values
                                if isinstance(genre, dict) and isinstance(genre.get("value"), str) and genre["value"]))
    publisher = info.get("publisherName") or title.get("publisherName")
    release = info.get("releaseDate") or title.get("releaseDate")
    try:
        release_date = date.fromisoformat(release[:10]).isoformat() if isinstance(release, str) else None
    except ValueError:
        release_date = None
    notices = playstation_slot(props, "compatibility-notices", content_id).get("compatibilityNoticesByPlatform") or {}
    players = next((entry["value"] for entry in (notices.get("Common") or [])
                    if isinstance(entry, dict) and entry.get("type") == "NO_OF_PLAYERS"
                    and isinstance(entry.get("value"), str) and re.fullmatch(r"\d+(?:[-–]\d+)?", entry["value"])), None)
    actual_locale = props.get("locale") or locale
    russian = actual_locale.startswith("ru-") and bool(description and
                  len(re.findall("[А-Яа-яЁё]", description)) > len(re.findall("[A-Za-z]", description)))
    platform = "PS5" if "-PPSA" in content_id else "PS4"
    return {
        "description": description, "language": "ru" if russian else "en",
        "publisher": publisher if isinstance(publisher, str) else None, "genres": genres,
        "release_date": release_date, "players": players,
        "localization": {
            "screen": store_languages(info, "screenLanguages", platform) or store_languages(title, "screenLanguages", platform),
            "audio": store_languages(info, "spokenLanguages", platform) or store_languages(title, "spokenLanguages", platform),
        },
        "url": playstation_url(content_id, actual_locale if actual_locale in ("ru-ua", "en-us") else locale),
    }


def playstation_description(page, content_id, locale="en-us"):
    return playstation_details(playstation_props(page, content_id), content_id, locale)


def russian_concept(client, product_props, content_id):
    concept = playstation_slot(product_props, "game-title", content_id).get("concept") or {}
    ref = concept.get("__ref", "")
    if not re.fullmatch(r"Concept:[0-9]{1,12}", ref):
        return None
    concept_id = ref.split(":", 1)[1]
    url = f"https://store.playstation.com/ru-ua/concept/{concept_id}"
    props = playstation_props(bounded_get(client, url, 3_000_000))
    if str(props.get("conceptId")) != concept_id:
        return None
    entry = playstation_cache(props, "info").get(ref, {})
    default = (entry.get("defaultProduct") or {}).get("__ref", "")
    localized_id = default.removeprefix("Product:")
    if not playstation_url(localized_id) or localized_id[7:11] != content_id[7:11]:
        return None
    result = playstation_details(props, localized_id, "ru-ua")
    if result and result["language"] == "ru":
        result["url"] = url
        if localized_id != content_id:
            result["localization"] = {"screen": None, "audio": None}
        return result
    return None


def playstation_about(client, content_id):
    metadata, failure = None, None
    for locale in ("ru-ua", "en-us"):
        url = playstation_url(content_id, locale)
        try:
            props = playstation_props(bounded_get(client, url, 3_000_000), content_id)
            about = playstation_details(props, content_id, locale)
        except httpx.HTTPStatusError as error:
            if not (error.response.is_redirect or error.response.status_code in (404, 410)):
                failure = error
            continue
        except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError) as error:
            failure = error
            continue
        if not about:
            continue
        if locale == "ru-ua" and about["language"] != "ru":
            metadata = about
            continue
        if locale == "en-us":
            try:
                localized = russian_concept(client, props, content_id)
            except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
                localized = None
            if localized:
                for key in ("release_date", "players"):
                    localized[key] = about[key] or localized[key]
                prefer_languages(localized, about)
                prefer_languages(localized, metadata)
                return localized
        if about["description"]:
            if metadata:
                for key in ("publisher", "genres", "release_date", "players"):
                    about[key] = metadata[key] or about[key]
                prefer_languages(about, metadata)
            return about
        metadata = metadata or about
    if metadata is None and failure:
        raise failure
    return metadata


class Artwork:
    def __init__(self):
        self.token = None
        self.token_expiry = 0

    def igdb(self, client, title, platform):
        client_id = os.getenv("IGDB_CLIENT_ID")
        token = os.getenv("IGDB_ACCESS_TOKEN")
        if not token:
            if not self.token or self.token_expiry <= time.monotonic():
                response = client.post("https://id.twitch.tv/oauth2/token", data={
                    "client_id": client_id, "client_secret": os.getenv("IGDB_CLIENT_SECRET"),
                    "grant_type": "client_credentials",
                })
                response.raise_for_status()
                data = response.json()
                self.token = data["access_token"]
                self.token_expiry = time.monotonic() + max(0, data["expires_in"] - 60)
            token = self.token
        platform_id = 167 if platform == "PS5" else 48
        query = (
            "search " + json.dumps(title, ensure_ascii=False) + "; "
            "fields id,name,cover.image_id,artworks.image_id,first_release_date; "
            "where platforms = (" + str(platform_id) + "); limit 10;"
        )
        try:
            response = client.post("https://api.igdb.com/v4/games", content=query.encode(),
                                   headers={"Client-ID": client_id, "Authorization": "Bearer " + token})
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 401:
                self.token = None
            raise
        result = []
        for row in response.json():
            cover = (row.get("cover") or {}).get("image_id")
            images = [("cover", cover)] + [("background", art.get("image_id")) for art in row.get("artworks", [])[:4]]
            for kind, frame in images:
                if frame and re.fullmatch(r"[A-Za-z0-9_-]+", frame):
                    size = "t_cover_big" if kind == "cover" else "t_1080p"
                    result.append({"title": row["name"], "id": str(row["id"]) + "/" + frame, "source": "IGDB",
                                   "kind": kind, "url": f"https://images.igdb.com/igdb/image/upload/{size}/{frame}.jpg",
                                   "exact": comparable(title) == comparable(row["name"])})
        return result

    def steamgrid(self, client, title):
        headers = {"Authorization": "Bearer " + os.environ["STEAMGRIDDB_API_KEY"]}
        base = "https://www.steamgriddb.com/api/v2"
        response = client.get(base + "/search/autocomplete/" + quote(title, safe=""), headers=headers)
        response.raise_for_status()
        matches = response.json().get("data", [])
        exact = [row for row in matches if comparable(row.get("name", "")) == comparable(title)]
        if exact:
            matches = exact
        result = []
        for game in matches[:2]:
            game_id = int(game["id"])
            grids = client.get(f"{base}/grids/game/{game_id}", headers=headers,
                               params={"dimensions": "600x900", "types": "static", "nsfw": "false", "humor": "false"})
            grids.raise_for_status()
            for art in grids.json().get("data", [])[:6]:
                if image_url(art.get("url", "")):
                    result.append({"title": game["name"], "id": str(art["id"]), "kind": "cover",
                                   "source": "SteamGridDB", "url": art["url"],
                                   "exact": len(exact) == 1})
            time.sleep(0.3)
        return result

    def playstation(self, client, title, content_id):
        if not (url := playstation_url(content_id)):
            return []
        page = bounded_get(client, url, 3_000_000)
        return playstation_media(page, content_id, title)

    def search(self, title, platform, content_id=None):
        available = providers()
        found, issues = [], []
        with httpx.Client(timeout=7, follow_redirects=False) as client:
            sources = ((PS_STORE_SOURCE, lambda: self.playstation(client, title, content_id)),
                       ("IGDB", lambda: self.igdb(client, title, platform)),
                       ("SteamGridDB", lambda: self.steamgrid(client, title)))
            for label, obtain in sources:
                if not available[label]:
                    continue
                try:
                    found.extend(obtain())
                except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
                    issues.append(f"{label} временно недоступен или вернул ошибку")
        return found, issues

    def download(self, candidate):
        url = candidate["url"]
        if not image_url(url):
            raise ValueError("Неизвестный источник изображения")
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            return bounded_get(client, url, MAX_IMAGE)
