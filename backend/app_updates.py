"""Discover and verify the Windows executable published in GitHub Releases."""
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

REPOSITORY = "sliptip/Prospero-Library"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2}")
MAX_EXE = 200 * 1024 * 1024


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    size: int
    digest: str


def version_tuple(version):
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise UpdateError("Некорректный номер версии Prospero.")
    return tuple(map(int, version.split(".")))


def latest_release(client, current):
    response = client.get(RELEASE_API, headers={"Accept": "application/vnd.github+json"})
    if response.status_code == 404:
        raise UpdateError("Публичный релиз Prospero недоступен на GitHub.")
    if response.status_code == 403 or response.status_code == 429:
        raise UpdateError("GitHub временно ограничил запросы. Попробуй позже.")
    response.raise_for_status()
    data = response.json()
    tag = data.get("tag_name", "")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("GitHub вернул релиз с неизвестным форматом версии.")
    version = tag[1:]
    if version_tuple(version) <= version_tuple(current):
        return None
    name = f"Prospero-Library-{version}-Windows-x64.exe"
    url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    asset = next((item for item in data.get("assets", []) if item.get("name") == name), None)
    if not asset or asset.get("browser_download_url") != url:
        raise UpdateError("В релизе нет Windows EXE для обновления.")
    digest = asset.get("digest")
    size = asset.get("size")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise UpdateError("GitHub ещё не опубликовал контрольную сумму файла обновления.")
    if type(size) is not int or not 0 < size <= MAX_EXE:
        raise UpdateError("Размер файла обновления выходит за допустимые пределы.")
    return Release(version, url, size, digest[7:])


def download_release(client, release, destination):
    url = release.url
    digest = hashlib.sha256()
    total = 0
    for _ in range(5):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc not in DOWNLOAD_HOSTS:
            raise UpdateError("GitHub вернул неожиданный адрес файла обновления.")
        with client.stream("GET", url) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            response.raise_for_status()
            try:
                with destination.open("xb") as output:
                    for block in response.iter_bytes():
                        total += len(block)
                        if total > release.size:
                            raise UpdateError("Размер скачанного файла не совпадает с данными GitHub.")
                        digest.update(block)
                        output.write(block)
                if total != release.size or digest.hexdigest() != release.digest:
                    raise UpdateError("Файл обновления не прошёл проверку SHA-256.")
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            return
    raise UpdateError("GitHub вернул слишком много перенаправлений.")
