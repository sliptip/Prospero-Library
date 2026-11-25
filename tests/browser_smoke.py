"""Exercise the built app in Chromium against a real temporary SQLite database."""
import argparse
import json
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

            page.get_by_role("link", name="Настройки").click()
            hide_recent = page.get_by_label("Скрывать недавно добавленную игру")
            with page.expect_response("**/api/settings/library"):
                hide_recent.check()
            expect(hide_recent).to_be_checked()
            page.get_by_role("link", name="Библиотека").click()
            page.reload(wait_until="networkidle")
            expect(page.get_by_role("button", name="Открыть карточку")).to_have_count(0)
            expect(page.get_by_role("heading", name="Коллекция", exact=True)).to_be_visible()
            page.get_by_role("link", name="Настройки").click()
            with page.expect_response("**/api/settings/library"):
                hide_recent.uncheck()
            expect(hide_recent).not_to_be_checked()
            page.get_by_role("link", name="Библиотека").click()
            expect(page.get_by_role("button", name="Открыть карточку")).to_be_visible()

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

            about_requests = []

            def store_about(route):
                about_requests.append(route.request.url)
                content_id = "JP0101-PPSA35399_00-CVBCAPP000000000"
                details = {"description": "Первый абзац.\n\nВторой абзац.", "language": "ru",
                           "publisher": "KONAMI", "genres": ["Ролевые игры", "Боевики"],
                           "release_date": "2026-10-15", "players": "1",
                           "localization": {"screen": ["en", "ja", "ru"], "audio": ["en", "ja"]},
                           "url": f"https://store.playstation.com/ru-ua/product/{content_id}"}
                with sqlite3.connect(directory / "data" / "library.db") as db:
                    db.execute("INSERT OR REPLACE INTO store_about VALUES (?,?,?)", (content_id, "ru", json.dumps(details)))
                route.fulfill(json=details)

            page.route("**/api/games/1/about", store_about)
            page.get_by_role("button", name="Открыть карточку").click()
            expect(page.get_by_role("heading", name="Игра в коллекции")).to_be_visible()
            expect(page.get_by_text("01.000.000", exact=True)).to_be_visible()
            expect(page.get_by_text("Preorder Alucard Skin DLC", exact=True)).to_be_visible()
            expect(page.get_by_text("Совместимый патч", exact=False)).to_have_count(0)
            expect(page.get_by_text("Официальная", exact=True)).to_be_visible()
            expect(page.get_by_text("Английский, Японский", exact=True)).to_be_visible()
            expect(page.get_by_text("Английский, Японский, Русский", exact=True)).to_be_visible()
            header = page.locator(".detail-hero")
            expect(header.get_by_text("Ролевые игры", exact=True)).to_be_visible()
            expect(header.get_by_text("KONAMI", exact=True)).to_be_visible()
            expect(header.get_by_text("Игроки · 1", exact=True)).to_be_visible()
            about_button = header.get_by_role("button", name="Об игре")
            about_dialog = page.get_by_role("dialog", name="Об игре")
            expect(about_dialog).to_have_count(0)
            assert len(about_requests) == 1
            about_button.press("Enter")
            description = about_dialog.locator(".about-game-description")
            expect(description).to_be_visible()
            expect(description).to_have_attribute("lang", "ru")
            assert description.text_content() == "Первый абзац.\n\nВторой абзац."
            page.keyboard.press("Escape")
            expect(about_dialog).to_have_count(0)
            about_button.click()
            expect(description).to_be_visible()
            assert len(about_requests) == 1
            about_dialog.get_by_role("button", name="Закрыть").click()
            page.set_viewport_size({"width": 390, "height": 844})
            expect(about_button).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            about_button.click()
            expect(description).to_be_visible()
            page.keyboard.press("Escape")
            page.set_viewport_size({"width": 1440, "height": 1000})
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
            editor = page.get_by_role("dialog", name="Редактировать карточку")
            expect(editor.get_by_role("combobox", name="Русский текст").locator("option:checked")).to_have_text("Авто · Есть")
            expansion = "JP0101-PPSA35399_00-EXPANSION0000001"
            page.route("**/api/games/1/store-dlc", lambda route: route.fulfill(json=[
                {"id": expansion, "content_id": expansion, "kind": "dlc", "name": "DLC из магазина",
                 "description": None, "locale": "ru-ua"},
                {"id": "bonus", "content_id": None, "kind": "edition", "name": "Special Edition",
                 "description": "Цифровой артбук", "locale": "ru-ua"},
            ]))
            page.route("**/api/store-dlc/" + expansion + "?*", lambda route:
                       route.fulfill(json={"description": "Описание загружено из магазина"}))
            editor.get_by_role("button", name="Найти в магазине").click()
            choices = editor.get_by_role("list", name="Результаты PlayStation Store")
            expect(choices.get_by_text("Состав издания")).to_be_visible()
            choices.get_by_role("button", name="Добавить Special Edition").click()
            expect(editor.get_by_role("textbox", name="Название DLC").last).to_have_value("Бонусы Special Edition")
            choices.get_by_role("button", name="Добавить DLC из магазина").click()
            expect(editor.get_by_role("textbox", name="Название DLC").last).to_have_value("DLC из магазина")
            expect(editor.get_by_role("textbox", name="Описание", exact=True).last).to_have_value("Описание загружено из магазина")
            editor.get_by_role("button", name="Добавить локализацию").click()
            editor.get_by_role("button", name="Добавить локализацию").click()
            groups = editor.locator(".localization-form")
            groups.first.get_by_role("textbox", name="Название или автор").fill("Фанатская озвучка")
            groups.first.get_by_role("textbox", name="Языки озвучки").fill("Русский")
            groups.last.get_by_role("textbox", name="Название или автор").fill("Отдельный перевод")
            groups.last.get_by_role("textbox", name="Языки субтитров").fill("Украинский")
            page.set_viewport_size({"width": 390, "height": 844})
            assert editor.evaluate("el => el.scrollWidth <= el.clientWidth")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.get_by_role("dialog").get_by_role("button", name="Сохранить", exact=True).click()
            expect(page.get_by_text("Проверка сохранения в SQLite")).to_be_visible()
            expect(page.get_by_text("01.000.001", exact=True)).to_be_visible()
            expect(page.get_by_text("Официальная (текст)", exact=True)).to_be_visible()
            expect(page.get_by_role("heading", name="Фанатская озвучка")).to_be_visible()
            expect(page.get_by_role("heading", name="Отдельный перевод")).to_be_visible()
            expect(page.get_by_text("Бонусы Special Edition", exact=True)).to_be_visible()
            expect(page.get_by_text("Описание загружено из магазина", exact=True)).to_be_visible()

            full_description = ("Подробное описание. " * 60) + "Последняя строка из магазина."
            page.route("**/api/store-dlc/" + expansion + "?*", lambda route:
                       route.fulfill(json={"description": full_description}))
            page.get_by_role("button", name="Редактировать").click()
            editor.get_by_role("button", name="Найти в магазине").click()
            choices.get_by_role("button", name="Обновить описание DLC из магазина").click()
            expect(editor.get_by_role("textbox", name="Описание", exact=True).last).to_have_value(full_description)
            editor.get_by_role("button", name="Сохранить", exact=True).click()
            expect(page.get_by_text("Последняя строка из магазина.", exact=False)).to_be_visible()
            expect(page.get_by_text("DLC из магазина", exact=True)).to_have_count(1)

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
            expect(page.get_by_role("button", name="Об игре")).to_have_count(0)
            expect(page.get_by_text("Источник русификации")).to_have_count(0)
            page.get_by_role("link", name="Библиотека").click()
            page.get_by_role("button", name="Фильтры", exact=True).click()
            page.get_by_label("Русский текст").check()
            page.get_by_label("Русская озвучка").check()
            expect(page.locator(".game-tile strong")).to_contain_text("Castlevania")
            expect(page.locator(".game-tile")).to_have_count(1)
            page.get_by_role("button", name="Сбросить").click()
            page.get_by_role("button", name="Закрыть фильтры").click()
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
            expect(page.get_by_role("heading", name="Фанатская озвучка")).to_be_visible()
            expect(page.get_by_text("Официальная (текст)", exact=True)).to_be_visible()
            expect(page.locator(".detail-title .cover img")).to_have_attribute("src", re.compile("/media/"))
            page.get_by_role("link", name="Настройки").click()
            page.get_by_role("button", name="Восстановить", exact=True).click()
            page.get_by_role("dialog").get_by_role("button", name="Восстановить", exact=True).click()
            expect(page.get_by_text("Библиотека восстановлена")).to_be_visible()
            page.get_by_role("link", name="Библиотека").click()
            expect(page.locator(".game-tile")).to_have_count(1)
            with sqlite3.connect(directory / "data" / "library.db") as db:
                db.execute("INSERT INTO games (platform,title_id,title,added_at) VALUES ('PS4','CUSA20469','Cuphead','2026-11-25')")
                game_id = db.execute("SELECT id FROM games WHERE title_id='CUSA20469'").fetchone()[0]
                root_id = db.execute("INSERT INTO roots (path,state) VALUES ('PS4 fixtures','online')").lastrowid
                package_id = "EP8254-CUSA20469_00-CUPHEADDLC000000"
                db.execute("INSERT INTO dlc (game_id,name,status,content_id) VALUES (?,'Delicious Last Course','configured',?)",
                           (game_id, package_id))
                db.executemany(
                    """INSERT INTO ps4_extras (root_id,relpath,size_bytes,mtime,title_id,kind,title,content_id,available)
                       VALUES (?, ?, 100, 0, 'CUSA20469', 'dlc', 'Extra', ?, ?)""",
                    [(root_id, "delicious.pkg", package_id, 1), (root_id, "bonus.pkg", None, 1),
                     (root_id, "removed.pkg", None, 0)],
                )
            page.reload(wait_until="networkidle")
            cuphead = page.locator(".game-tile").filter(has_text="Cuphead")
            expect(cuphead.get_by_text("2 DLC", exact=True)).to_be_visible()
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
