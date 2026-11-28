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
SERVER = """
from unittest.mock import patch
from backend import patches
from backend.__main__ import main

with patch.object(patches, "retrieve_patch_site", return_value=[
        {"version": "01.005.000", "firmware": "9.60", "size_bytes": None, "date": ("archived", "2026-11-17")}
]):
    main()
"""


@contextmanager
def server(directory):
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-c", SERVER], cwd=ROOT,
        env={**os.environ, "PROSPERO_DATA": str(directory), "PROSPERO_PORT": str(port), "PROSPERO_HOST": "127.0.0.1",
             "PROSPERO_EMPTY_LIBRARY": "0", "no_proxy": "*", "NO_PROXY": "*"},
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
            expect(page.locator(".sidebar-version")).to_have_text(re.compile(r"^v\d+\.\d+\.\d+$"))
            expect(page.get_by_role("button", name="Открыть карточку")).to_be_visible()
            expect(page.locator(".collection-patch-status")).to_have_count(0)
            capture(page, screenshots, "Prospero-Library.png")

            running_version = page.locator(".sidebar-version span").inner_text().removeprefix("v")
            mocked_health = {"version": running_version, "app_update": True}
            page.route("**/api/health", lambda route: route.fulfill(json=mocked_health))
            page.reload(wait_until="networkidle")
            update = page.locator(".sidebar-version").get_by_role("button", name="Проверить обновление Prospero")
            update_url = re.compile(r"/api/app-update\?operation=")
            page.route(update_url, lambda route:
                       route.fulfill(json={"version": running_version, "restart": False} if route.request.method == "POST" else None))
            update.click()
            expect(page.get_by_text("Установлена последняя версия Prospero")).to_be_visible()

            pending = []

            def slow_download(route):
                if route.request.method == "POST":
                    pending.append(route)
                else:
                    route.fulfill(json={"version": "99.1.2", "received": 13, "total": 25})

            page.unroute(update_url)
            page.route(update_url, slow_download)
            update.click()
            expect(page.get_by_text("Скачиваю версию 99.1.2… 52%")).to_be_visible()
            with page.expect_event("framenavigated"):
                mocked_health["version"] = "99.1.2"
                pending[0].fulfill(json={"version": "99.1.2", "restart": True})
            expect(page.locator(".sidebar-version span")).to_have_text("v99.1.2")
            page.unroute(update_url)
            page.unroute("**/api/health")
            page.reload(wait_until="networkidle")

            original = page.request.get(base + "/api/games").json()[0]
            sorted_page = browser.new_page()
            store_date = {"id": 102, "title": "Store date", "release_date": None, "store_release_date": "2024-01-01"}
            manual_date = {"id": 103, "title": "Manual date", "release_date": "2020-01-01", "store_release_date": "2026-01-01"}
            no_date = {"id": 104, "title": "No date", "release_date": None, "store_release_date": None}
            patch = {**original["patch"], "state": "available", "cached": True, "check": {"last_success": "2026-11-27"}}
            sorted_page.route("**/api/games", lambda route: route.fulfill(json=[
                {**original, **store_date, "file_state": "offline", "patch": patch},
                {**original, **no_date}, {**original, **manual_date},
            ]))
            sorted_page.goto(base, wait_until="networkidle")
            sorted_page.get_by_label("Сортировка").select_option("release_date")
            sorting_button = sorted_page.locator(".sort-direction")
            for mode, selector in (("Шкаф", ".game-tile strong"), ("Список", ".game-row strong")):
                sorted_page.get_by_role("button", name=mode, exact=True).click()
                titles = sorted_page.locator(selector)
                expect(titles).to_have_text(["Store date", "Manual date", "No date"])
                sorting_button.click()
                expect(titles).to_have_text(["Manual date", "Store date", "No date"])
                sorting_button.click()
                expect(titles).to_have_text(["Store date", "Manual date", "No date"])
                expect(sorted_page.get_by_role("img", name="Есть обновление")).to_have_attribute("title", re.compile("По данным проверки"))
            sorted_page.close()

            tile = page.locator(".game-tile").filter(has_text="Castlevania")
            tile.click()
            page.get_by_role("button", name="Указать патч вручную").click()
            dialog = page.get_by_role("dialog", name="Указать патч вручную")
            dialog.get_by_label("Версия патча").fill("01.000.000")
            dialog.get_by_role("button", name="Сохранить").click()
            expect(dialog).not_to_be_visible()
            expect(page.get_by_text("Актуальная версия", exact=True)).to_be_visible()
            expect(page.get_by_text("Версия для сравнения · Вручную")).to_be_visible()
            page.get_by_role("button", name="К библиотеке").click()
            expect(tile.get_by_role("img", name="Актуальная версия")).to_have_attribute("title", re.compile("вручную"))

            tile.click()
            page.get_by_role("button", name="Изменить ручной патч").click()
            dialog = page.get_by_role("dialog", name="Изменить ручной патч")
            expect(dialog.get_by_label("Версия патча")).to_have_value("01.000.000")
            dialog.get_by_label("Версия патча").fill("01.009.000")
            dialog.get_by_label("Требуемая прошивка").fill("13.60")
            dialog.get_by_role("button", name="Сохранить").click()
            expect(dialog).not_to_be_visible()
            expect(page.get_by_text("Есть обновление", exact=True)).to_be_visible()
            expect(page.get_by_text("Совместимый патч · 13.60")).to_be_visible()
            expect(page.get_by_text("01.009.000 · Вручную", exact=True)).to_be_visible()
            page.reload(wait_until="networkidle")
            expect(page.get_by_text("Версия для сравнения · Вручную")).to_be_visible()
            page.get_by_role("button", name="Изменить ручной патч").click()
            expect(dialog.get_by_label("Требуемая прошивка")).to_have_value("13.60")
            dialog.get_by_role("button", name="Удалить ручную запись").click()
            expect(dialog).not_to_be_visible()
            expect(page.get_by_role("button", name="Указать патч вручную")).to_be_visible()
            page.get_by_role("button", name="К библиотеке").click()
            expect(tile.get_by_role("img", name="Есть обновление")).not_to_be_visible()

            page.get_by_role("link", name="Настройки").click()
            page.get_by_role("button", name="Задать ключ SteamGridDB").click()
            artwork_dialog = page.get_by_role("dialog", name="Настройки SteamGridDB")
            key = artwork_dialog.get_by_label("Ключ SteamGridDB")
            key.fill("local-fixture-key")
            artwork_dialog.get_by_role("button", name="Сохранить", exact=True).click()
            expect(page.get_by_text("Ключи сохранены", exact=True)).to_be_visible()
            expect(artwork_dialog).to_have_count(0)
            page.get_by_role("button", name="Изменить ключ SteamGridDB").click()
            expect(key).to_have_value("")
            key.fill("discarded-fixture-key")
            artwork_dialog.get_by_role("button", name="Отмена", exact=True).click()
            page.get_by_role("button", name="Задать ключ IGDB").click()
            igdb = page.get_by_role("dialog", name="Настройки IGDB")
            igdb.get_by_label("Client ID").fill("fixture-client")
            igdb.get_by_label("Client Secret").fill("fixture-secret")
            igdb.get_by_role("button", name="Сохранить", exact=True).click()
            expect(page.get_by_role("button", name="Изменить ключ IGDB")).to_be_visible()
            page.get_by_role("button", name="Изменить ключ SteamGridDB").click()
            key.fill("replacement-fixture-key")
            artwork_dialog.get_by_role("button", name="Сохранить", exact=True).click()
            expect(artwork_dialog).to_have_count(0)
            stored_keys = json.loads((directory / "data" / "artwork-keys.json").read_text(encoding="utf-8"))
            assert stored_keys == {"STEAMGRIDDB_API_KEY": "replacement-fixture-key",
                                   "IGDB_CLIENT_ID": "fixture-client", "IGDB_CLIENT_SECRET": "fixture-secret",
                                   "IGDB_ACCESS_TOKEN": ""}
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
            expect(page.get_by_text("В образе", exact=True)).to_have_count(2)
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
            dlc_status = editor.get_by_role("combobox", name="Статус DLC")
            expect(dlc_status.first.locator("option")).to_have_text(["В образе", "Не добавлено"])
            expect(dlc_status.first).to_have_value("included")
            expect(editor.get_by_role("combobox", name="Русский текст").locator("option:checked")).to_have_text("Авто · Есть")
            page.set_viewport_size({"width": 390, "height": 620})
            descriptions = editor.get_by_role("textbox", name="Описание", exact=True)
            descriptions.first.evaluate("field => { field.style.height = '420px' }")
            assert editor.evaluate("""dialog => {
              const last = [...dialog.querySelectorAll('.dlc-form')].at(-1).getBoundingClientRect()
              const actions = dialog.querySelector('.dlc-actions').getBoundingClientRect()
              const footer = dialog.querySelector('.dialog-footer').getBoundingClientRect()
              return last.bottom <= actions.top && actions.bottom <= footer.top
            }""")
            editor.get_by_role("button", name="Добавить DLC", exact=True).click()
            new_name = editor.get_by_role("textbox", name="Название DLC").last
            expect(new_name).to_be_focused()
            expect(new_name).to_be_in_viewport(ratio=1)
            expect(dlc_status.last).to_have_value("included")
            new_name.fill("DLC вручную")
            dlc_status.last.select_option("not_added")
            page.set_viewport_size({"width": 1440, "height": 1000})
            expansion = "JP0101-PPSA35399_00-EXPANSION0000001"
            local_expansion = "JP0101-CUSA35399_00-EXPANSION0000001"
            page.route("**/api/games/1/store-dlc", lambda route: route.fulfill(json=[
                {"id": expansion, "content_id": local_expansion, "kind": "dlc", "name": "DLC из магазина",
                 "description": None, "locale": "ru-ua"},
                {"id": "bonus", "content_id": None, "kind": "edition", "name": "Special Edition",
                 "description": "Цифровой артбук", "locale": "ru-ua"},
            ]))
            page.route("**/api/store-dlc/" + expansion + "?*", lambda route:
                       route.fulfill(json={"description": "Описание загружено из магазина"}))
            editor.get_by_role("button", name="Найти в магазине").click()
            choices = editor.get_by_role("list", name="Результаты PlayStation Store")
            expect(choices.get_by_text("Состав издания")).to_be_visible()
            expect(choices.get_by_role("listitem").first).to_be_in_viewport()
            choices.get_by_role("button", name="Добавить Special Edition").click()
            expect(editor.get_by_role("textbox", name="Название DLC").last).to_have_value("Бонусы Special Edition")
            expect(dlc_status.last).to_have_value("included")
            choices.get_by_role("button", name="Добавить DLC из магазина").click()
            expect(editor.get_by_role("textbox", name="Название DLC").last).to_have_value("DLC из магазина")
            expect(dlc_status.last).to_have_value("included")
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
            expect(page.locator(".dlc-row").filter(has_text="DLC вручную").get_by_text("Не добавлено", exact=True)).to_be_visible()
            with sqlite3.connect(directory / "data" / "library.db") as db:
                self_saved_id = db.execute("SELECT content_id FROM dlc WHERE name='DLC из магазина'").fetchone()
                assert self_saved_id == (local_expansion,)

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
            expect(page.locator(".detail-title .cover img")).to_have_css("object-fit", "contain")

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
            with page.expect_response(lambda response: response.url.endswith("/api/backups") and response.status == 201) as created:
                page.get_by_role("button", name="Создать копию").click()
            disposable = created.value.json()["name"]
            remove_backup = page.get_by_role("button", name="Удалить " + disposable, exact=True)
            remove_backup.click()
            confirmation = page.get_by_role("dialog", name="Удалить резервную копию")
            expect(confirmation.get_by_text(disposable, exact=True)).to_be_visible()
            confirmation.get_by_role("button", name="Отмена").click()
            expect(remove_backup).to_be_visible()
            remove_backup.click()
            confirmation.get_by_role("button", name="Удалить", exact=True).click()
            expect(page.get_by_text("Резервная копия удалена")).to_be_visible()
            expect(remove_backup).to_have_count(0)
            assert not (directory / "data" / "backups" / disposable).exists()

            page.get_by_role("button", name="Добавить игру", exact=True).first.click()
            new_editor = page.get_by_role("dialog", name="Добавить игру")
            new_editor.get_by_role("button", name="Добавить DLC", exact=True).click()
            expect(new_editor.get_by_role("textbox", name="Название DLC")).to_be_focused()
            expect(new_editor.get_by_role("combobox", name="Статус DLC")).to_have_value("included")
            new_editor.get_by_role("button", name="Удалить строку DLC").click()
            page.get_by_role("textbox", name="Название", exact=True).fill("Тестовая карточка")
            page.get_by_role("textbox", name="Title ID").fill("PPSA99999")
            page.get_by_label("Версия в коллекции").fill("01.000.000")
            page.get_by_role("dialog").get_by_role("button", name="Сохранить", exact=True).click()
            expect(page.get_by_role("heading", name="Тестовая карточка")).to_be_visible()
            official_patch = page.get_by_text("Последний официальный патч", exact=True).locator("..")
            expect(official_patch.get_by_text("01.005.000", exact=True)).to_be_visible()
            patch_date = page.get_by_text("Добавлен в PP", exact=True).locator("..")
            expect(patch_date).to_contain_text("17 ноября 2026")
            expect(page.get_by_text("Есть обновление", exact=True)).to_be_visible()
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
                build_id = db.execute("INSERT INTO builds (game_id,identity,version) VALUES (?,'base-fixture','01.00')",
                                      (game_id,)).lastrowid
                db.execute("INSERT INTO files (root_id,relpath,size_bytes,mtime,build_id) VALUES (?,'base.pkg',100,0,?)",
                           (root_id, build_id))
                db.execute("""INSERT INTO ps4_extras (root_id,relpath,size_bytes,mtime,title_id,kind,title,version)
                              VALUES (?,'update.pkg',100,0,'CUSA20469','patch','Update','01.05')""", (root_id,))
                db.execute("INSERT INTO checks VALUES (?,'ok','OrbisPatches',?,?,NULL,'01.05')", (game_id, stamp, stamp))
                db.execute("INSERT INTO patch_history VALUES (?,'01.05','9.60','OrbisPatches',?,NULL)", (game_id, stamp))
                package_id = "EP8254-CUSA20469_00-CUPHEADDLC000000"
                db.execute("""INSERT INTO dlc (game_id,name,description,status,content_id)
                              VALUES (?,'Delicious Last Course','Описание дополнения','included',?)""",
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
            expect(cuphead.get_by_role("img", name="Актуальная версия")).to_be_visible()
            expect(page.locator(".game-tile").filter(has_text="Castlevania").get_by_role("img", name="Есть обновление")).to_be_visible()
            page.get_by_role("button", name="Список", exact=True).click()
            expect(page.locator(".game-row").filter(has_text="Cuphead").get_by_role("img", name="Актуальная версия")).to_be_visible()
            page.get_by_role("button", name="Шкаф", exact=True).click()
            cuphead.click()
            expansion_row = page.locator(".dlc-row").filter(has_text="Delicious Last Course")
            filename = expansion_row.locator(".package-file")
            expect(filename).to_contain_text("delicious.pkg")
            expect(filename).to_have_css("border-top-width", "1px")
            with sqlite3.connect(directory / "data" / "library.db") as db:
                db.execute("UPDATE roots SET state='offline' WHERE id=?", (root_id,))
            page.goto(restarted, wait_until="networkidle")
            expect(cuphead.get_by_role("img", name="Актуальная версия")).to_be_visible()
            expect(cuphead.get_by_text("01.05", exact=True)).to_be_visible()
            expect(page.locator(".game-tile").filter(has_text="Castlevania")
                   .get_by_role("img", name="Есть обновление")).to_be_visible()
            page.get_by_role("button", name="Список", exact=True).click()
            cuphead_row = page.locator(".game-row").filter(has_text="Cuphead")
            expect(cuphead_row.get_by_role("img", name="Актуальная версия")).to_be_visible()
            cuphead_row.click()
            collection_version = page.get_by_text("Версия с патчем в коллекции", exact=True).locator("..")
            expect(collection_version.get_by_text("01.05", exact=True)).to_be_visible()
            expect(page.get_by_text("На диске", exact=True).locator("..")).to_contain_text("Файл недоступен")
            assert not errors, errors
    print("Browser smoke passed: desktop/tablet/phone, manual patch, journal, edit, artwork, manual order, restart, backup and restore.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--chromium", default=os.getenv("PROSPERO_CHROMIUM"))
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    run(args.screenshots, args.chromium)
