"""Exercise the built app in Chromium against a real temporary SQLite database."""
import argparse
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTTP = build_opener(ProxyHandler({}))


@contextmanager
def server(directory):
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend"], cwd=ROOT,
        env={**os.environ, "PROSPERO_DATA": str(directory), "PROSPERO_PORT": str(port), "PROSPERO_HOST": "127.0.0.1",
             "PROSPERO_EMPTY_LIBRARY": "0"},
    )
    try:
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError("Backend stopped during startup")
            try:
                with HTTP.open(base + "/api/health", timeout=.2):
                    break
            except URLError:
                time.sleep(.1)
        else:
            raise RuntimeError("Backend did not start")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=15)


def capture(page, directory, filename):
    if directory:
        page.screenshot(path=directory / filename, full_page=True, animations="disabled")


def run(screenshots, browser_executable):
    with TemporaryDirectory() as temporary, sync_playwright() as playwright:
        directory = Path(temporary)
        cover = directory / "cover.png"
        Image.new("RGB", (320, 520), color="#70432b").save(cover)
        browser = playwright.chromium.launch(executable_path=browser_executable, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        with server(directory / "data") as base:
            page.goto(base, wait_until="networkidle")
            expect(page.get_by_role("heading", name="Моя библиотека")).to_be_visible()
            expect(page.get_by_role("button", name="Открыть карточку")).to_be_visible()
            capture(page, screenshots, "Prospero-Library.png")

            page.get_by_role("button", name="Сканировать", exact=True).click()
            report = page.get_by_role("dialog", name="Сканирование коллекции")
            expect(report.get_by_text("Укажите папки коллекции в настройках")).to_be_visible()
            report.get_by_role("button", name="Журнал", exact=True).click()
            journal = page.get_by_role("dialog", name="Журнал диагностики")
            expect(journal.get_by_role("textbox", name="Записи журнала")).to_have_value(
                re.compile("Укажите папки коллекции в настройках")
            )
            page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
            journal.get_by_role("button", name="Скопировать").click()
            expect(journal.get_by_text("Журнал скопирован")).to_be_visible()
            assert "Укажите папки коллекции" in page.evaluate("navigator.clipboard.readText()")
            with page.expect_download() as info:
                journal.get_by_role("link", name="Скачать TXT").click()
            report_file = info.value
            assert report_file.suggested_filename == "prospero-log.txt"
            assert "Укажите папки коллекции" in Path(report_file.path()).read_text(encoding="utf-8")
            page.once("dialog", lambda confirmation: confirmation.accept())
            journal.get_by_role("button", name="Очистить журнал").click()
            expect(journal.get_by_text("Журнал очищен")).to_be_visible()
            expect(journal.get_by_role("textbox", name="Записи журнала")).to_have_count(0)
            journal.get_by_role("button", name="Закрыть").click()
            report.get_by_role("button", name="Закрыть", exact=True).last.click()

            for width, height in [(768, 1024), (390, 844)]:
                compact = browser.new_page(viewport={"width": width, "height": height}, has_touch=True)
                compact.goto(base, wait_until="networkidle")
                expect(compact.get_by_role("heading", name="Моя библиотека")).to_be_visible()
                assert compact.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if width == 390:
                    capture(compact, screenshots, "Prospero-Mobile.png")
                compact.close()

            page.get_by_role("button", name="Список", exact=True).click()
            expect(page.locator(".game-row")).to_have_count(1)
            page.reload(wait_until="networkidle")
            expect(page.locator(".game-row")).to_have_count(1)
            search = page.get_by_role("textbox", name="Поиск по библиотеке")
            search.fill("ppsa35399")
            expect(page.locator(".game-row")).to_have_count(1)
            search.fill("missing")
            expect(page.get_by_text("Игры не найдены")).to_be_visible()
            page.get_by_role("button", name="Очистить поиск").click()
            page.get_by_role("button", name="Фильтры", exact=True).click()
            page.get_by_label("Русский текст").check()
            expect(page.get_by_text("Игры не найдены")).to_be_visible()
            page.get_by_role("button", name="Сбросить").click()
            page.get_by_role("button", name="Закрыть фильтры").click()
            page.get_by_role("button", name="Шкаф", exact=True).click()
            expect(page.locator(".game-tile")).to_have_count(1)

            page.get_by_role("button", name="Открыть карточку").click()
            expect(page.get_by_role("heading", name="Игра в коллекции")).to_be_visible()
            expect(page.get_by_text("01.000.000", exact=True)).to_be_visible()
            expect(page.get_by_text("Preorder Alucard Skin DLC", exact=True)).to_be_visible()
            expect(page.get_by_text("Совместимый патч", exact=False)).to_have_count(0)
            about_requests = []

            def store_about(route):
                about_requests.append(route.request.url)
                route.fulfill(json={"description": "First paragraph.\n\nSecond paragraph.",
                                    "publisher": "KONAMI", "genres": ["Action"],
                                    "url": "https://store.playstation.com/en-us/product/JP0101-PPSA35399_00-CVBCAPP000000000"})

            page.route("**/api/games/1/about", store_about)
            about = page.locator("details.about-game")
            summary = about.locator("summary")
            expect(summary).to_have_text("Об игре")
            assert about.get_attribute("open") is None and not about_requests
            summary.press("Enter")
            description = about.locator(".about-game-description")
            expect(description).to_be_visible()
            assert description.text_content() == "First paragraph.\n\nSecond paragraph."
            expect(about.get_by_text("KONAMI", exact=True)).to_be_visible()
            summary.click()
            expect(description).to_be_hidden()
            summary.click()
            expect(description).to_be_visible()
            assert len(about_requests) == 1
            stamp = "2026-11-25T12:00:00+00:00"
            with sqlite3.connect(directory / "data" / "library.db") as db:
                db.execute("INSERT INTO builds (game_id,identity,version,firmware,size_bytes) VALUES (1,'test-pkg','01.000.000','13.40',4043482656)")
                db.execute("INSERT INTO checks VALUES (1,'ok','ProsperoPatches',?,?,NULL,'01.001.004')", (stamp, stamp))
                db.execute("INSERT INTO patch_history VALUES (1,'01.001.004','14.20.00.07','ProsperoPatches',?,NULL)", (stamp,))
            page.reload(wait_until="networkidle")
            expect(page.get_by_text("14.20", exact=True)).to_have_attribute("title", "14.20.00.07")
            expect(page.get_by_text("Нет в полученном списке", exact=True)).to_be_visible()
            capture(page, screenshots, "Prospero-Card.png")
            page.get_by_role("link", name="Обновления", exact=True).click()
            expect(page.get_by_text("Нет в полученном списке", exact=True)).to_be_visible()
            page.get_by_role("button", name="История").click()
            expect(page.get_by_text("FW 14.20", exact=True)).to_have_attribute("title", "14.20.00.07")
            with sqlite3.connect(directory / "data" / "library.db") as db:
                db.execute("INSERT INTO patch_history VALUES (1,'01.001.000','13.55','ProsperoPatches',?,NULL)", (stamp,))
            page.goto(base + "/games/1", wait_until="networkidle")
            expect(page.get_by_text("01.001.000", exact=True)).to_be_visible()
            page.get_by_role("button", name="Редактировать").click()
            checkbox = page.get_by_role("checkbox", name="Разрешить редактирование полей из PKG")
            version = page.get_by_role("textbox", name="Версия в коллекции")
            expect(checkbox).not_to_be_checked()
            expect(version).not_to_be_editable()
            platform = page.get_by_role("combobox", name="Платформа", exact=True)
            expect(platform).to_be_disabled()
            checkbox.check()
            expect(version).to_be_editable()
            expect(page.get_by_role("textbox", name="Title ID")).to_be_editable()
            expect(platform).to_be_enabled()
            version.fill("01.000.001")
            checkbox.uncheck()
            expect(version).to_have_value("01.000.000")
            checkbox.check()
            page.get_by_role("textbox", name="Примечания").fill("Проверка сохранения в SQLite")
            page.get_by_role("dialog").get_by_role("button", name="Сохранить", exact=True).click()
            expect(page.get_by_text("Проверка сохранения в SQLite")).to_be_visible()
            expect(page.get_by_text("01.000.001", exact=True)).to_be_visible()

            page.get_by_role("button", name="Оформление").click()
            upload = page.get_by_label("Загрузить обложку")
            upload.set_input_files(cover)
            expect(page.get_by_text("Текущая обложка · Вручную")).to_be_visible()
            page.get_by_role("button", name="Закрыть", exact=True).click()
            expect(page.locator(".detail-title .cover img")).to_have_attribute("src", re.compile("/media/"))

            page.get_by_role("link", name="Настройки").click()
            page.get_by_role("button", name="Добавить папки").click()
            roots = page.get_by_role("dialog", name="Добавить папки")
            paths = [str(directory / "Коллекция"), str(directory / "Съёмный диск")]
            roots.get_by_role("textbox", name="Полные пути, по одному на строку").fill("\n".join(paths))
            roots.get_by_role("button", name="Сохранить", exact=True).click()
            for path in paths:
                expect(page.get_by_text(path, exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Сканировать все")).to_be_enabled()
            page.get_by_role("button", name="Создать копию").click()
            expect(page.get_by_text("Резервная копия создана")).to_be_visible()

            page.get_by_role("button", name="Добавить игру", exact=True).first.click()
            page.get_by_role("textbox", name="Название", exact=True).fill("Тестовая карточка")
            page.get_by_role("textbox", name="Title ID").fill("PPSA99999")
            page.get_by_role("dialog").get_by_role("button", name="Сохранить", exact=True).click()
            expect(page.get_by_role("heading", name="Тестовая карточка")).to_be_visible()
            expect(page.locator("details.about-game")).to_have_count(0)
            page.get_by_role("link", name="Библиотека").click()
            page.get_by_label("Сортировка").select_option("manual")
            tiles = page.locator(".game-tile")
            tile_titles = tiles.locator("strong")
            expect(tile_titles.first).to_contain_text("Castlevania")
            handle = page.get_by_role("button", name="Поменять местами · Тестовая карточка")
            handle.scroll_into_view_if_needed()
            source_box = handle.bounding_box()
            target_box = tiles.first.locator(".tile-art").bounding_box()
            source_x, source_y = source_box["x"] + source_box["width"] / 2, source_box["y"] + source_box["height"] / 2
            target_x, target_y = target_box["x"] + target_box["width"] / 2, target_box["y"] + 45
            page.mouse.move(source_x, source_y)
            page.mouse.down()
            page.mouse.move(source_x - 20, source_y + 4, steps=5)
            expect(page.locator(".game-item.reordering")).to_have_count(1)
            page.mouse.move(target_x, target_y, steps=15)
            expect(page.locator(".game-item.reorder-target")).to_have_count(1)
            page.mouse.up()
            expect(tile_titles.first).to_have_text("Тестовая карточка")
            page.get_by_role("button", name="Список", exact=True).click()
            row_titles = page.locator(".game-row strong")
            expect(row_titles.first).to_have_text("Тестовая карточка")
            page.get_by_role("button", name="Поменять местами · Тестовая карточка").click()
            page.locator(".game-row").last.click()
            expect(row_titles.first).to_contain_text("Castlevania")
            page.get_by_role("button", name="Поменять местами · Тестовая карточка").click()
            page.locator(".game-row").first.click()
            expect(row_titles.first).to_have_text("Тестовая карточка")
            page.reload(wait_until="networkidle")
            expect(row_titles.first).to_have_text("Тестовая карточка")
            assert not errors, errors

        with server(directory / "data") as restarted:
            page.goto(restarted, wait_until="networkidle")
            page.get_by_label("Сортировка").select_option("manual")
            expect(page.locator(".game-tile strong").first).to_have_text("Тестовая карточка")
            page.goto(restarted + "/games/1", wait_until="networkidle")
            expect(page.get_by_text("Проверка сохранения в SQLite")).to_be_visible()
            expect(page.locator(".detail-title .cover img")).to_have_attribute("src", re.compile("/media/"))
            page.get_by_role("link", name="Настройки").click()
            page.get_by_role("button", name="Восстановить", exact=True).click()
            page.get_by_role("dialog").get_by_role("button", name="Восстановить", exact=True).click()
            expect(page.get_by_text("Библиотека восстановлена")).to_be_visible()
            page.get_by_role("link", name="Библиотека").click()
            expect(page.locator(".game-tile")).to_have_count(1)
            assert not errors, errors
    print("Browser smoke passed: desktop/tablet/phone, journal, edit, artwork, manual order, restart, backup and restore.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--chromium", default=os.getenv("PROSPERO_CHROMIUM"))
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    run(args.screenshots, args.chromium)
