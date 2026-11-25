import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE = 16 * 1024 * 1024
IMAGE_SUFFIXES = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
ART_NAME = re.compile(r"[a-f0-9]{64}\.(?:webp|png|jpg)")
PS_STORE_HOST = "image.api.playstation.com"
PS_STORE_SOURCE = "PlayStation Store"
STORE_PRODUCT_SETTING = "store_product:"
PS_STORE_PRODUCT = re.compile(r"[A-Z]{2}[0-9]{4}-(?:PPSA|CUSA)[0-9]{5}_00-[A-Z0-9]{16}")
LANGUAGE_PACK = re.compile(
    r"language pack|voice pack|voice[- ]?over pack|audio language|языков\w* пакет|пакет (?:язык\w*|озвучки)|"
    r"sprachpaket|pack de (?:idiomas?|langues?)|pacote de idioma|pacchetto lingu|言語パック|语言包|語言包|언어 팩", re.I,
)
IMAGE_HOSTS = {"images.igdb.com", "cdn.steamgriddb.com", "cdn2.steamgriddb.com", PS_STORE_HOST}
ARTWORK_KEYS = ("IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET", "IGDB_ACCESS_TOKEN", "STEAMGRIDDB_API_KEY")
logger = logging.getLogger(__name__)


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
                image.thumbnail((1000, 1500) if kind == "cover" else (1920, 1200))
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


def playstation_url(content_id, locale="en-us"):
    if content_id and PS_STORE_PRODUCT.fullmatch(content_id):
        return f"https://store.playstation.com/{locale}/product/{content_id}"
    return None


def playstation_locales(content_id):
    preferred = {"EP": "en-gb", "JP": "ja-jp", "HP": "en-hk"}.get(content_id[:2], "en-us")
    return tuple(dict.fromkeys(("ru-ua", preferred, "en-us")))


def same_store_title(content_id, candidate):
    return bool(candidate and PS_STORE_PRODUCT.fullmatch(candidate)
                and candidate.partition("_")[0] == content_id.partition("_")[0])


class PlayStationEditionLinks(HTMLParser):
    def __init__(self, title, content_id):
        super().__init__()
        self.content_id = content_id
        self.labels = {title, "standard edition", title + " standard edition", "通常版"}
        self.product = None
        self.text = []
        self.matches = set()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            url = urlsplit(dict(attrs).get("href") or "")
            product = url.path.rstrip("/").rsplit("/", 1)[-1]
            if (url.scheme == "https" and url.netloc == "store.playstation.com"
                    and re.fullmatch(r"/[a-z]{2}-[a-z]{2}/product/[^/]+/?", url.path)
                    and product != self.content_id and same_store_title(self.content_id, product)):
                self.product = product
                self.text = []

    def handle_data(self, data):
        if self.product:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.product:
            if comparable(" ".join(self.text)) in self.labels:
                self.matches.add(self.product)
            self.product = None


def find_store_product(client, title, content_id):
    name = comparable(title)
    if not name or name == content_id[7:16].lower():
        return None
    locale = {"EP": "en-gb", "JP": "ja-jp", "HP": "en-hk"}.get(content_id[:2], "en-us")
    slug = quote(name.replace(" ", "-"), safe="-")
    links = PlayStationEditionLinks(name, content_id)
    try:
        page = bounded_get(client, f"https://www.playstation.com/{locale}/games/{slug}/", 4_000_000)
        links.feed(page.decode("utf-8", errors="replace"))
        links.close()
    except (httpx.HTTPError, ValueError) as error:
        logger.info("PlayStation: поиск страницы для %s не удался (%s)", content_id, type(error).__name__)
        return None
    return next(iter(links.matches)) if len(links.matches) == 1 else None


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


def playstation_media(props, content_id, title):
    role_urls = {}
    for slot in ("media-carousel", "background-image", "game-title", "upsells"):
        product = playstation_slot(props, slot, content_id)
        for item in product.get("media") or []:
            if (isinstance(item, dict) and item.get("type") == "IMAGE"
                    and isinstance(item.get("url"), str) and image_url(item["url"])
                    and urlsplit(item["url"]).netloc == PS_STORE_HOST):
                role_urls.setdefault(item.get("role"), item["url"])
    roles = (
        ("cover", "PORTRAIT_BANNER"), ("cover", "MASTER"),
        ("background", "GAMEHUB_COVER_ART"), ("background", "SIXTEEN_BY_NINE_BANNER"),
    )
    return [
        {"title": title, "id": f"{content_id}/{role}", "kind": kind,
         "source": PS_STORE_SOURCE, "url": role_urls[role], "exact": True}
        for kind, role in roles if role in role_urls
    ]


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


def playstation_dlc_description(client, content_id, locale):
    fallback = None
    for region in dict.fromkeys(("ru-ua", locale)):
        url = playstation_url(content_id, region)
        try:
            info = playstation_description(bounded_get(client, url, 3_000_000), content_id, region)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
            continue
        if info and info["description"]:
            if info["language"] == "ru" or region == locale:
                return info["description"]
            fallback = info["description"]
    return fallback


def playstation_dlc_candidates(props, content_id):
    platform = "PS5" if "-PPSA" in content_id else "PS4"
    addons = playstation_cache(props, "add-ons")
    related = []
    for key, value in addons.get("ROOT_QUERY", {}).items():
        if key.startswith("addOnProductsByProductIdRetrieve(") and f'"productId":"{content_id}"' in key:
            related.extend(value.get("addOnProducts", []))
    locale = props.get("locale")
    if locale not in ("ru-ua", "en-us", "en-gb", "ja-jp", "en-hk"):
        locale = "en-us"
    candidates = []
    for ref in dict.fromkeys(item.get("__ref", "") for item in related):
        item = addons.get(ref, {})
        product_id = item.get("id")
        name = item.get("name")
        if (product_id != content_id and playstation_url(product_id) and isinstance(name, str) and name.strip()
                and platform in (item.get("platforms") or [])
                and item.get("storeDisplayClassification") != "LANGUAGE_PACK"
                and not any(LANGUAGE_PACK.search(text or "") for text in (name, item.get("invariantName")))):
            candidates.append({"id": product_id, "kind": "dlc", "name": name[:256], "description": None,
                               "content_id": product_id, "locale": locale})

    editions = playstation_cache(props, "upsells")
    product = editions.get("Product:" + content_id, {})
    concept = editions.get((product.get("concept") or {}).get("__ref"), {})
    products = [editions.get(ref.get("__ref"), {}) for ref in concept.get("products", [])]
    standard = next((item for item in products if (item.get("edition") or {}).get("type") == "STANDARD"
                     and platform in (item.get("platforms") or [])), product)
    names = (concept.get("name"), standard.get("name"), standard.get("invariantName"),
             *((standard.get("edition") or {}).get("features") or []))
    base_names = {comparable(name) for name in names if isinstance(name, str)}
    base_names.update(("main game", "base game", "full game", "основная игра", "полная игра"))
    for item in products:
        edition = item.get("edition") or {}
        name = edition.get("name")
        edition_id = item.get("id")
        if (not playstation_url(edition_id) or not isinstance(name, str) or not name.strip()
                or platform not in (item.get("platforms") or [])
                or item.get("storeDisplayClassification") in ("DEMO", "TRIAL")
                or edition.get("type") == "STANDARD"):
            continue
        features = [feature.strip() for feature in (edition.get("features") or [])
                    if isinstance(feature, str) and feature.strip() and comparable(feature) not in base_names
                    and not LANGUAGE_PACK.search(feature)]
        if features:
            candidates.append({"id": edition_id, "kind": "edition", "name": name[:245],
                               "description": "\n".join("• " + feature for feature in features),
                               "content_id": None, "locale": locale})
    return candidates


def playstation_dlc(client, content_id):
    failure = None
    source = None
    candidates = []
    for locale in playstation_locales(content_id):
        url = playstation_url(content_id, locale)
        try:
            props = playstation_props(bounded_get(client, url, 3_000_000), content_id)
            if props:
                source = props, locale
                candidates = playstation_dlc_candidates(props, content_id)
                if candidates:
                    break
        except httpx.HTTPStatusError as error:
            if not (error.response.is_redirect or error.response.status_code in (404, 410)):
                failure = error
        except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError) as error:
            failure = error
    if source is None:
        if failure:
            raise failure
        return None
    props, locale = source
    items = {(item["kind"], item["id"]): item for item in candidates}
    for item in related_store_dlc(client, props, content_id, locale):
        items.setdefault((item["kind"], item["id"]), item)
    return list(items.values())


def product_concept(props, content_id):
    concept = playstation_slot(props, "game-title", content_id).get("concept") or {}
    ref = concept.get("__ref", "")
    return ref if re.fullmatch(r"Concept:[0-9]{1,12}", ref) else None


def concept_default_product(props, ref):
    if str(props.get("conceptId")) != ref.split(":", 1)[1]:
        return None
    for slot in ("info", "game-title"):
        entry = playstation_cache(props, slot).get(ref, {})
        product_ref = (entry.get("defaultProduct") or {}).get("__ref", "")
        if product_ref.startswith("Product:"):
            return product_ref.removeprefix("Product:")
    return None


def related_store_dlc(client, props, content_id, source_locale):
    ref = product_concept(props, content_id)
    if not ref:
        return []
    concept_id = ref.split(":", 1)[1]
    for locale in playstation_locales(content_id):
        if locale == source_locale:
            continue
        try:
            url = f"https://store.playstation.com/{locale}/concept/{concept_id}"
            concept = playstation_props(bounded_get(client, url, 3_000_000))
            related_id = concept_default_product(concept, ref)
            if not playstation_url(related_id) or related_id[:11] != content_id[:11]:
                continue
            url = playstation_url(related_id, locale)
            related = playstation_props(bounded_get(client, url, 3_000_000), related_id)
            if product_concept(related, related_id) != ref:
                continue
            candidates = [item for item in playstation_dlc_candidates(related, related_id)
                          if item["kind"] == "dlc" and item["id"][:6] == content_id[:6]]
            if candidates:
                return candidates
        except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
            continue
    return []


def russian_concept(client, product_props, content_id):
    ref = product_concept(product_props, content_id)
    if not ref:
        return None
    concept_id = ref.split(":", 1)[1]
    url = f"https://store.playstation.com/ru-ua/concept/{concept_id}"
    props = playstation_props(bounded_get(client, url, 3_000_000))
    localized_id = concept_default_product(props, ref)
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
    for locale in playstation_locales(content_id):
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
        if locale != "ru-ua":
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
    def __init__(self, directory=None, *, database=None):
        self.database = database
        if database is not None:
            directory = database.directory
        self.keys_file = directory / "artwork-keys.json" if directory is not None else None
        self.igdb_token = None

    def saved_keys(self):
        if self.keys_file is None:
            return {}
        try:
            data = json.loads(self.keys_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError("Некорректный файл ключей оформления")
        return {key: data[key] for key in ARTWORK_KEYS if isinstance(data.get(key), str)}

    def save_keys(self, changes):
        data = self.saved_keys()
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        # A newly supplied secret takes precedence over an old token.
        if changes.get("IGDB_CLIENT_SECRET") and "IGDB_ACCESS_TOKEN" not in changes:
            data["IGDB_ACCESS_TOKEN"] = ""
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.keys_file.parent, delete=False) as stream:
            staged = Path(stream.name)
            try:
                json.dump(data, stream)
                stream.close()
                staged.replace(self.keys_file)
            finally:
                staged.unlink(missing_ok=True)

    def credentials(self):
        return {**{key: os.getenv(key) for key in ARTWORK_KEYS}, **self.saved_keys()}

    def providers(self):
        keys = self.credentials()
        return {
            PS_STORE_SOURCE: True,
            "IGDB": bool(keys["IGDB_CLIENT_ID"] and (keys["IGDB_ACCESS_TOKEN"] or keys["IGDB_CLIENT_SECRET"])),
            "SteamGridDB": bool(keys["STEAMGRIDDB_API_KEY"]),
        }

    def igdb(self, client, title, platform):
        keys = self.credentials()
        client_id = keys["IGDB_CLIENT_ID"]
        token = keys["IGDB_ACCESS_TOKEN"]
        if not token:
            identity = client_id, keys["IGDB_CLIENT_SECRET"]
            cached = self.igdb_token
            if cached and cached[0] == identity and cached[2] > time.monotonic():
                token = cached[1]
            else:
                response = client.post("https://id.twitch.tv/oauth2/token", data={
                    "client_id": client_id, "client_secret": identity[1],
                    "grant_type": "client_credentials",
                })
                response.raise_for_status()
                data = response.json()
                token = data["access_token"]
                self.igdb_token = identity, token, time.monotonic() + max(0, data["expires_in"] - 60)
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
                self.igdb_token = None
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
        headers = {"Authorization": "Bearer " + (self.credentials()["STEAMGRIDDB_API_KEY"] or "")}
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
            covers = [art for art in grids.json().get("data", []) if image_url(art.get("url", ""))]
            for art in covers[:6]:
                result.append({"title": game["name"], "id": str(art["id"]), "kind": "cover",
                               "source": "SteamGridDB", "url": art["url"], "exact": len(exact) == 1})
            time.sleep(0.3)
        return result

    def store_lookup(self, client, title, content_id, fetch):
        setting = STORE_PRODUCT_SETTING + content_id
        cached = None
        if self.database is not None:
            with self.database.connect() as db:
                row = db.execute("SELECT value FROM settings WHERE key=?", (setting,)).fetchone()
                if row and same_store_title(content_id, row["value"]):
                    cached = row["value"]
        failure = None
        result = None
        for product in dict.fromkeys((cached or content_id, content_id)):
            try:
                result = fetch(client, product)
            except httpx.HTTPStatusError as error:
                if not (error.response.is_redirect or error.response.status_code in (404, 410)):
                    raise
                failure = error
                result = None
            if result is not None:
                return result

        alternate = find_store_product(client, title, content_id)
        if alternate and alternate != cached:
            result = fetch(client, alternate)
            if result is not None and self.database is not None:
                with self.database.connect() as db:
                    db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (setting, alternate))
                logger.info("PlayStation Store: для PKG %s найдена карточка %s", content_id, alternate)
        if result is None and failure:
            raise failure
        return result

    def playstation(self, client, title, content_id):
        if not playstation_url(content_id):
            return []
        return self.store_lookup(client, title, content_id, lambda client, product: self.store_media(client, title, product)) or []

    def store_media(self, client, title, content_id):
        failure = None
        found = False
        for locale in playstation_locales(content_id):
            url = playstation_url(content_id, locale)
            try:
                page = bounded_get(client, url, 3_000_000)
            except httpx.HTTPStatusError as error:
                if not (error.response.is_redirect or error.response.status_code in (404, 410)):
                    raise
                failure = error
                continue
            if props := playstation_props(page, content_id):
                found = True
                if candidates := playstation_media(props, content_id, title):
                    return candidates
        if not found and failure:
            raise failure
        return [] if found else None

    def search(self, title, platform, content_id=None):
        available = self.providers()
        found, issues = [], []
        with httpx.Client(timeout=7, follow_redirects=False) as client:
            sources = ((PS_STORE_SOURCE, lambda: self.playstation(client, title, content_id)),
                       ("IGDB", lambda: self.igdb(client, title, platform)),
                       ("SteamGridDB", lambda: self.steamgrid(client, title)))
            for label, obtain in sources:
                if not available[label]:
                    continue
                try:
                    items = obtain()
                    found.extend(items)
                    if label == "SteamGridDB" and not items:
                        issues.append("SteamGridDB не нашёл подходящих обложек")
                except httpx.HTTPStatusError as error:
                    status = error.response.status_code
                    if label in ("SteamGridDB", "IGDB") and status == 401:
                        issues.append(f"{label} отклонил ключ (HTTP {status})")
                    else:
                        issues.append(f"{label} вернул HTTP {status}")
                except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
                    issues.append(f"{label} временно недоступен или вернул ошибку")
        return found, issues

    def download(self, candidate):
        url = candidate["url"]
        if not image_url(url):
            raise ValueError("Неизвестный источник изображения")
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            return bounded_get(client, url, MAX_IMAGE)
