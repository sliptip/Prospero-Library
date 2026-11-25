import hashlib
import io
import json
import os
import sqlite3
import ssl
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from backend import artwork, catalog, database, patches, scanner
from backend.main import create_app
from backend.seed import VERSION_URI

CASTLEVANIA_INFO = """[General]
  Package type: FIH Debug — installable on a PS5 in debug mode
  Content ID: JP0101-PPSA35399_00-CVBCAPP000000000
  Content type: Application / game (0x20)
  Title: Castlevania: Belmont's Curse
  App type: Game / application (gd) · DRM standard
  Version: content 01.000.000 · master 01.00
  SDK: 0x1100000000000000 (SDK 11)
  Required system software: 0x1340000000000000
[param.json]
  contentVersion: 01.000.000
  titleId: PPSA35399
  versionFileUri: """ + VERSION_URI


def game_body(game, **changes):
    fields = set(catalog.GameInput.model_fields)
    return {**{key: value for key, value in game.items() if key in fields}, **changes}


class PortableDataTest(unittest.TestCase):
    def test_default_data_moves_with_the_program_folder(self):
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {"PROSPERO_DATA": "", "PROSPERO_EMPTY_LIBRARY": "0"}):
            program = Path(temporary) / "Prospero Library"
            moved = Path(temporary) / "Другой диск"
            legacy = program.parent / "LocalAppData" / "Prospero Library"
            database.Database(legacy).initialize()
            with sqlite3.connect(legacy / "library.db") as db:
                db.execute("UPDATE games SET notes='Старая библиотека'")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(legacy.parent)}), \
                    patch.object(database, "APP_DIRECTORY", program), patch("backend.main.APP_DIRECTORY", program), \
                    TestClient(create_app(), base_url="http://localhost") as client:
                self.assertNotEqual(client.get("/api/games/1").json()["notes"], "Старая библиотека")
                self.assertEqual(client.get("/api/logs").status_code, 200)
            data = program / "data"
            self.assertTrue((data / "library.db").is_file())
            self.assertTrue((data / "logs" / "prospero.log").is_file())
            self.assertTrue((data / "artwork").is_dir())
            self.assertTrue((data / "backups").is_dir())
            with sqlite3.connect(data / "library.db") as db:
                db.execute("UPDATE games SET notes='Переносимая библиотека'")
            program.rename(moved)
            with patch.object(database, "APP_DIRECTORY", moved), patch("backend.main.APP_DIRECTORY", moved), \
                    TestClient(create_app(), base_url="http://localhost") as client:
                self.assertEqual(client.get("/api/games/1").json()["notes"], "Переносимая библиотека")
            with sqlite3.connect(legacy / "library.db") as db:
                self.assertEqual(db.execute("SELECT notes FROM games").fetchone()[0], "Старая библиотека")

    def test_data_overrides_support_relative_and_absolute_paths(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            program = root / "Prospero"
            with patch.object(database, "APP_DIRECTORY", program):
                for configured, expected in (("collection/data", program / "collection/data"),
                                             (str(root / "other"), root / "other")):
                    with self.subTest(configured=configured), patch.dict(os.environ, {"PROSPERO_DATA": configured}):
                        self.assertEqual(database.app_data_directory(), expected)


class LocalCatalogTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.env = patch.dict(os.environ, {"PROSPERO_EMPTY_LIBRARY": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(create_app(self.data), base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def initial(self):
        return self.client.get("/api/games/1").json()

    def wait_job(self, response):
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        timeout = time.monotonic() + 8
        while job["status"] in {"running", "cancelling"} and time.monotonic() < timeout:
            time.sleep(.05)
            job = self.client.get(f"/api/jobs/{job['id']}").json()
        self.assertNotIn(job["status"], {"running", "cancelling"})
        return job

    def test_initial_metadata_and_manual_add(self):
        initial = self.initial()
        self.assertEqual((initial["version"], initial["firmware"], initial["size_bytes"]),
                         ("01.000.000", "13.40", 4043482656))
        self.assertEqual(len(initial["dlc"]), 2)
        self.assertTrue(all(dlc["status"] == "configured" for dlc in initial["dlc"]))
        self.assertIsNone(initial["ru_audio"])
        self.assertIsNone(initial["patch"]["latest"])
        self.assertEqual(self.client.post("/api/games", json={"title": "Ёж", "platform": "PS4",
                                                                             "title_id": "CUSA12345"}).status_code, 201)
        self.assertEqual(self.client.post("/api/games", json={"title": "Другой", "platform": "PS4",
                                                                             "title_id": "CUSA12345"}).status_code, 409)
        self.assertEqual(self.client.post("/api/games", json={"title": "Ошибка", "platform": "PS5",
                                                                             "title_id": "CUSA12345"}).status_code, 422)

    def test_remove_card(self):
        self.assertEqual(self.client.delete("/api/games/1").status_code, 200)
        self.assertEqual(self.client.get("/api/games").json(), [])
        self.assertEqual(self.client.get("/api/games/1").status_code, 404)

    def test_game_order_survives_restart_and_reused_id(self):
        def add(title_id):
            return self.client.post("/api/games", json={"title": title_id, "platform": "PS5", "title_id": title_id}).json()["id"]

        middle = add("PPSA12345")
        last = add("PPSA12346")
        self.assertEqual(self.client.post("/api/games/order", json={"source_id": 1, "target_id": last}).json(),
                         [last, middle, 1])
        self.assertEqual(self.client.post("/api/games/order", json={"source_id": 1, "target_id": 99}).status_code, 404)
        with TestClient(create_app(self.data), base_url="http://localhost") as restarted:
            self.assertEqual([game["id"] for game in restarted.get("/api/games").json()], [last, middle, 1])
        self.assertEqual(self.client.delete(f"/api/games/{last}").status_code, 200)
        self.assertEqual(add("PPSA12347"), last)
        self.assertEqual([game["id"] for game in self.client.get("/api/games").json()], [middle, 1, last])

    def test_clear_diagnostic_log_preserves_future_logging(self):
        previous = self.data / "logs" / "prospero.log.1"
        previous.write_text("Предыдущая ротация", encoding="utf-8")
        self.assertIn("Prospero Library запущена", self.client.get("/api/logs").json()["text"])
        rejected = self.client.delete("/api/logs", headers={"Origin": "http://foreign.example"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.client.delete("/api/logs").json(), {"text": ""})
        self.assertFalse(previous.exists())
        self.assertEqual(self.client.get("/api/logs?download=true").text, "")
        self.wait_job(self.client.post("/api/scan", json={}))
        after = self.client.get("/api/logs").json()["text"]
        self.assertNotIn("Prospero Library запущена", after)
        self.assertIn("Укажите папки коллекции в настройках", after)

    def test_game_about_prefers_russian_and_keeps_the_store_metadata(self):
        content_id = self.initial()["content_id"]
        product = {"id": content_id, "publisherName": "Publisher", "localizedGenres": [{"value": "Боевики"}],
                   "releaseDate": "2026-10-15T00:00:00Z",
                   "platforms": ["PS4", "PS5"], "screenLanguages": ["en", "ja"],
                   "screenLanguagesByPlatform": [{"platform": "PS4", "screenLanguages": ["ja"]},
                                                 {"platform": "PS5", "screenLanguages": ["ru", "pt_BR", "ch", "zh"]}],
                   "spokenLanguagesByPlatform": [{"platform": "PS4", "spokenLanguages": ["ru"]},
                                                 {"platform": "PS5", "spokenLanguages": ["en", "ja"]}],
                   "descriptions": [{"type": "SHORT", "value": "Краткое описание"},
                                    {"type": "LONG", "value": "<p>Вступление &amp; сюжет</p><h3>Особенности</h3>"
                                                               "<ul><li>Первый пункт</li><li>Второй пункт</li></ul>"
                                                               "<script>ignored text</script>"}]}
        product_key = "Product:" + content_id
        cache = json.dumps({"cache": {"Product:" + content_id: product}}).replace("<", "\\u003c")
        notices = json.dumps({"cache": {product_key: {"id": content_id, "compatibilityNoticesByPlatform": {
            "Common": [{"type": "NO_OF_PLAYERS", "value": "1-2"}]
        }}}})
        props = {"productId": content_id, "locale": "ru-ua", "batarangs": {
            "info": {"text": f"<script>{cache}</script>"}, "compatibility-notices": {"text": f"<script>{notices}</script>"},
        }}
        data = json.dumps({"props": {"pageProps": props}}).replace("<", "\\u003c")
        page = f'<script id="__NEXT_DATA__">{data}</script>'.encode()
        url = f"https://store.playstation.com/ru-ua/product/{content_id}"
        requests = []

        def store(request):
            requests.append(str(request.url))
            return httpx.Response(200, content=page)

        store_client = httpx.Client(transport=httpx.MockTransport(store))
        languages = {"screen": ["ru", "pt-BR", "zh-Hant", "zh-Hans"], "audio": ["en", "ja"]}
        with patch("backend.main.httpx.Client", return_value=store_client):
            response = self.client.get("/api/games/1/about")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"description": "Вступление & сюжет\n\nОсобенности\n\n• Первый пункт\n• Второй пункт",
                                               "language": "ru", "publisher": "Publisher", "genres": ["Боевики"],
                                               "release_date": "2026-10-15", "players": "1-2", "url": url,
                                               "localization": languages})
            self.assertEqual(self.initial()["official_localization"], languages)
            manual = self.client.post("/api/games", json={"title": "Manual", "platform": "PS5", "title_id": "PPSA12345"}).json()
            self.assertIsNone(self.client.get(f"/api/games/{manual['id']}/about").json())
        self.assertEqual(requests, [url])
        self.assertIsNone(artwork.playstation_description(page, "UP9000-CUSA07408_00-00000000GODOFWAR"))

        backup = self.client.post("/api/backups").json()["name"]
        with sqlite3.connect(self.data / "library.db") as db:
            db.execute("DELETE FROM store_about")
        self.assertEqual(self.client.post("/api/backups/restore", json={"name": backup}).status_code, 200)
        with patch("backend.main.httpx.Client", side_effect=AssertionError("Unexpected Store request")), \
                TestClient(create_app(self.data), base_url="http://localhost") as reopened:
            self.assertEqual(reopened.get("/api/games/1/about").json(), response.json())

    def test_game_about_falls_back_to_english_when_russian_is_unavailable(self):
        content_id = self.initial()["content_id"]
        product = {"id": content_id, "publisherName": "Publisher",
                   "descriptions": [{"type": "LONG", "value": "Official English description"}]}
        cache = json.dumps({"cache": {"Product:" + content_id: product}})
        props = {"productId": content_id, "locale": "en-us", "batarangs": {"info": {"text": f"<script>{cache}</script>"}}}
        payload = json.dumps({"props": {"pageProps": props}}).replace("<", r"\u003c")
        requests = []

        def store(request):
            requests.append(request.url.path)
            if not request.url.path.startswith("/en-us/"):
                return httpx.Response(404)
            return httpx.Response(200, content=f'<script id="__NEXT_DATA__">{payload}</script>'.encode())

        store_client = httpx.Client(transport=httpx.MockTransport(store))
        with patch("backend.main.httpx.Client", return_value=store_client):
            result = self.client.get("/api/games/1/about")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["language"], "en")
        self.assertEqual(result.json()["description"], "Official English description")
        self.assertEqual(result.json()["localization"], {"screen": None, "audio": None})
        self.assertEqual(requests, [f"/ru-ua/product/{content_id}", f"/ja-jp/product/{content_id}",
                                    f"/en-us/product/{content_id}"])

    def test_game_about_finds_a_russian_edition_of_the_same_concept(self):
        content_id = self.initial()["content_id"]
        localized_id = "EP0101-PPSA35400_00-CVBCAPP000000000"
        concept_key = "Concept:227770"

        def fragment(cache):
            return {"text": f'<script>{json.dumps({"cache": cache})}</script>'}

        def page(props):
            payload = json.dumps({"props": {"pageProps": props}}).replace("<", r"\u003c")
            return f'<script id="__NEXT_DATA__">{payload}</script>'.encode()

        english = {"id": content_id, "releaseDate": "2026-10-15", "concept": {"__ref": concept_key},
                   "spokenLanguages": ["en"],
                   "descriptions": [{"type": "LONG", "value": "English description"}]}
        localized = {"id": localized_id, "releaseDate": "2026-10-14", "localizedGenres": [{"value": "Боевики"}],
                     "spokenLanguages": ["ru"], "screenLanguages": ["ru"],
                     "descriptions": [{"type": "LONG", "value": "Официальное описание на русском"}]}
        requests = []

        def store(request):
            requests.append(request.url.path)
            if request.url.path in {f"/ru-ua/product/{content_id}", f"/ja-jp/product/{content_id}"}:
                return httpx.Response(404)
            if request.url.path == f"/en-us/product/{content_id}":
                return httpx.Response(200, content=page({"productId": content_id, "locale": "en-us",
                    "batarangs": {"game-title": fragment({f"Product:{content_id}": english})}}))
            return httpx.Response(200, content=page({"conceptId": "227770", "locale": "ru-ua", "batarangs": {
                "info": fragment({f"Product:{localized_id}": localized,
                                  concept_key: {"defaultProduct": {"__ref": f"Product:{localized_id}"}}})}}))

        store_client = httpx.Client(transport=httpx.MockTransport(store))
        with patch("backend.main.httpx.Client", return_value=store_client):
            response = self.client.get("/api/games/1/about")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["language"], "ru")
        self.assertEqual(response.json()["release_date"], "2026-10-15")
        self.assertEqual(response.json()["localization"], {"screen": None, "audio": ["en"]})
        self.assertEqual(requests, [f"/ru-ua/product/{content_id}", f"/ja-jp/product/{content_id}",
                                    f"/en-us/product/{content_id}", "/ru-ua/concept/227770"])

    def test_legacy_store_cache_survives_offline_and_picks_up_languages(self):
        content_id = self.initial()["content_id"]
        saved = {"language": "ru", "description": "Русское описание в базе"}
        with sqlite3.connect(self.data / "library.db") as db:
            db.execute("INSERT INTO store_about VALUES (?,?,?)", (content_id, "ru", json.dumps(saved)))
        offline = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
        with patch("backend.main.httpx.Client", return_value=offline):
            self.assertEqual(self.client.get("/api/games/1/about").json(), saved)
        languages = {"screen": ["ru", "en"], "audio": ["ja"]}
        with patch("backend.main.httpx.Client"), \
                patch("backend.main.playstation_about", return_value={"language": "en", "localization": languages}):
            self.assertEqual(self.client.get("/api/games/1/about").json(), {**saved, "localization": languages})
        self.assertEqual(self.initial()["official_localization"], languages)

    def test_store_dlc_excludes_language_packs_and_returns_optional_edition_contents(self):
        base_id = self.initial()["content_id"]
        expansion = "JP0101-PPSA35399_00-EXPANSION0000001"
        language = "JP0101-PPSA35399_00-LANGUAGE00000001"
        edition = "JP0101-PPSA35399_00-PREMIUM000000001"
        foreign = "UP0101-CUSA12345_00-EXPANSION0000001"
        concept = "Concept:10018186"
        addon_items = {f"Product:{content_id}": {"id": content_id, "name": name, "platforms": [platform]}
                       for content_id, name, platform in ((expansion, "Story expansion", "PS5"),
                                                          (language, "Spanish Language Pack", "PS5"),
                                                          (foreign, "Other platform", "PS4"))}
        query = f'addOnProductsByProductIdRetrieve({{"pageArgs":{{"size":48}},"productId":"{base_id}"}})'
        addons = {**addon_items, "ROOT_QUERY": {query: {"addOnProducts": [{"__ref": ref} for ref in addon_items]}}}
        editions = {
            f"Product:{base_id}": {"id": base_id, "name": "Base title", "platforms": ["PS5"],
                                  "concept": {"__ref": concept}, "edition": {"type": "STANDARD", "features": ["Base title"]}},
            concept: {"name": "Base title", "products": [{"__ref": f"Product:{edition}"}]},
            f"Product:{edition}": {"id": edition, "platforms": ["PS5"],
                                  "edition": {"name": "Midnight Edition", "features": ["Base title", "Digital Gallery"]}},
        }

        def page(content_id, **slots):
            batarangs = {name: {"text": f"<script>{json.dumps({'cache': cache})}</script>"} for name, cache in slots.items()}
            props = {"productId": content_id, "locale": "ru-ua", "batarangs": batarangs}
            payload = json.dumps({"props": {"pageProps": props}}).replace("<", r"\u003c")
            return f'<script id="__NEXT_DATA__">{payload}</script>'.encode()

        full_description = ("Описание дополнения. " * 64) + "Последняя строка."
        description = {"id": expansion, "descriptions": [{"type": "LONG", "value": full_description}]}
        responses = {base_id: page(base_id, **{"add-ons": addons, "upsells": editions}),
                     expansion: page(expansion, info={f"Product:{expansion}": description})}
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=responses[request.url.path.split("/")[-1]])
        )
        clients = [httpx.Client(transport=transport), httpx.Client(transport=transport)]
        with patch("backend.main.httpx.Client", side_effect=clients):
            candidates = self.client.get("/api/games/1/store-dlc")
            self.assertEqual(candidates.status_code, 200, candidates.text)
            items = candidates.json()
            self.assertEqual([(row["kind"], row["name"]) for row in items],
                             [("dlc", "Story expansion"), ("edition", "Midnight Edition")])
            self.assertEqual(items[1]["description"], "• Digital Gallery")
            self.assertIsNone(items[1]["content_id"])
            details = self.client.get(f"/api/store-dlc/{expansion}?locale=ru-ua")
            self.assertEqual(details.json(), {"description": full_description})
        game = self.initial()
        self.assertEqual(len(game["dlc"]), 2)
        updated = game_body(game, dlc=[*game["dlc"], {"name": items[0]["name"], "content_id": expansion,
                                                   "description": full_description}])
        response = self.client.put("/api/games/1", json=updated)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.initial()["dlc"][-1]["description"], full_description)

    def test_hide_recent_preference_survives_restart_and_settings_edits(self):
        self.assertFalse(self.client.get("/api/settings").json()["hide_recent"])
        self.assertTrue(self.client.patch("/api/settings/library", json={"hide_recent": True}).json()["hide_recent"])
        self.assertTrue(self.client.put("/api/settings", json={"cli_path": "fpkg-cli"}).json()["hide_recent"])
        with TestClient(create_app(self.data), base_url="http://localhost") as reopened:
            self.assertTrue(reopened.get("/api/settings").json()["hide_recent"])
        self.assertFalse(self.client.patch("/api/settings/library", json={"hide_recent": False}).json()["hide_recent"])

    def test_store_dlc_description_prefers_russian_and_falls_back_to_listing_region(self):
        content_id = "EP0001-PPSA01506_00-DLCEXPANSION0003"
        russian_path = f"/ru-ua/product/{content_id}"
        english_path = f"/en-gb/product/{content_id}"
        paths = []

        def page(locale, description):
            info = {"id": content_id, "descriptions": [{"type": "LONG", "value": description}]}
            cache = json.dumps({"cache": {f"Product:{content_id}": info}})
            props = {"productId": content_id, "locale": locale, "batarangs": {"info": {"text": f"<script>{cache}</script>"}}}
            payload = json.dumps({"props": {"pageProps": props}}).replace("<", r"\u003c")
            return f'<script id="__NEXT_DATA__">{payload}</script>'.encode()

        pages = {russian_path: page("ru-ua", "Описание на русском"),
                 english_path: page("en-gb", "English description")}

        def respond(request):
            path = request.url.path
            paths.append(path)
            return httpx.Response(200, content=pages[path]) if path in pages else httpx.Response(404)

        transport = httpx.MockTransport(respond)
        clients = [httpx.Client(transport=transport), httpx.Client(transport=transport)]
        with patch("backend.main.httpx.Client", side_effect=clients):
            url = f"/api/store-dlc/{content_id}?locale=en-gb"
            self.assertEqual(self.client.get(url).json(), {"description": "Описание на русском"})
            del pages[russian_path]
            self.assertEqual(self.client.get(url).json(), {"description": "English description"})
        self.assertEqual(paths, [russian_path, russian_path, english_path])

    def test_store_dlc_merges_same_concept_regional_lists(self):
        base = "EP4321-PPSA12345_00-BASE000000000001"
        related = "EP4321-PPSA12346_00-BASE000000000001"
        concept = "Concept:12345"
        existing = "EP4321-PPSA12346_00-EXPANSION0000001"
        additional = "EP4321-PPSA12346_00-EXPANSION0000002"
        other_publisher = "EP9876-PPSA12346_00-EXPANSION0000002"

        def page(locale, ids, **slots):
            batarangs = {slot: {"text": f"<script>{json.dumps({'cache': cache})}</script>"} for slot, cache in slots.items()}
            props = {"locale": locale, **ids, "batarangs": batarangs}
            payload = json.dumps({"props": {"pageProps": props}}).replace("<", r"\u003c")
            return f'<script id="__NEXT_DATA__">{payload}</script>'.encode()

        def product_page(content_id, locale, names, concept_ref=concept):
            items = {f"Product:{key}": {"id": key, "name": value, "platforms": ["PS5"]} for key, value in names.items()}
            query = f'addOnProductsByProductIdRetrieve({{"productId":"{content_id}"}})'
            addons = {**items, "ROOT_QUERY": {query: {"addOnProducts": [{"__ref": key} for key in items]}}}
            title = {f"Product:{content_id}": {"id": content_id, "concept": {"__ref": concept_ref}}}
            return page(locale, {"productId": content_id}, **{"add-ons": addons, "game-title": title})

        main_path = f"/ru-ua/product/{base}"
        regional_path = f"/en-gb/product/{related}"
        pages = {
            main_path: product_page(base, "ru-ua", {existing: "Первое DLC"}),
            "/en-gb/concept/12345": page("en-gb", {"conceptId": "12345"},
                                        info={concept: {"defaultProduct": {"__ref": f"Product:{related}"}}}),
            regional_path: product_page(related, "en-gb", {
                additional: "Second expansion", existing: "First expansion", other_publisher: "Unrelated",
            }),
        }
        transport = httpx.MockTransport(lambda req: httpx.Response(
            200, content=pages[req.url.path]) if req.url.path in pages else httpx.Response(404))
        with httpx.Client(transport=transport) as client:
            first = artwork.playstation_dlc(client, base)
            self.assertEqual([(item["id"], item["name"], item["locale"]) for item in first],
                             [(existing, "Первое DLC", "ru-ua"), (additional, "Second expansion", "en-gb")])
            pages[regional_path] = product_page(related, "en-gb", {additional: "Wrong game"}, "Concept:90000")
            self.assertEqual([item["id"] for item in artwork.playstation_dlc(client, base)], [existing])

    def test_custom_localization_groups_survive_unrelated_edits_and_backup(self):
        groups = [{"source": "Команда перевода", "interface": None, "subtitles": "Русский", "audio": None},
                  {"source": None, "interface": None, "subtitles": None, "audio": "Русский, Украинский"}]
        initial = self.initial()
        updated = self.client.put("/api/games/1", json=game_body(initial, custom_localizations=[*groups, {}]))
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["custom_localizations"], groups)
        unrelated_edit = game_body(initial, notes="Правка примечаний")
        unrelated_edit.pop("custom_localizations")
        saved = self.client.put("/api/games/1", json=unrelated_edit)
        self.assertEqual(saved.json()["custom_localizations"], groups)
        backup = self.client.post("/api/backups").json()["name"]
        cleared = self.client.put("/api/games/1", json=game_body(initial, custom_localizations=[]))
        self.assertEqual(cleared.json()["custom_localizations"], [])
        self.assertEqual(self.client.post("/api/backups/restore", json={"name": backup}).status_code, 200)
        self.assertEqual(self.initial()["custom_localizations"], groups)
        self.assertEqual(self.client.delete("/api/games/1").status_code, 200)
        with sqlite3.connect(self.data / "library.db") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM custom_localizations").fetchone()[0], 0)

    def test_version_one_database_and_backup_migrate(self):
        older = self.root / "old-data"
        older.mkdir()
        file = older / "library.db"
        with sqlite3.connect(self.data / "library.db") as current, sqlite3.connect(file) as legacy:
            current.backup(legacy)
            legacy.execute("DROP TABLE ps4_extras")
            legacy.execute("DROP TABLE custom_localizations")
            legacy.execute("DROP TABLE store_about")
            legacy.execute("PRAGMA user_version = 1")
            legacy.execute("UPDATE games SET notes='Проверка старой библиотеки' WHERE id=1")
        name = "prospero-20261125-120000-000000.zip"
        with ZipFile(self.data / "backups" / name, "w") as archive:
            archive.write(file, "library.db")
        with TestClient(create_app(older), base_url="http://localhost") as old_app:
            self.assertEqual(old_app.get("/api/games/1").json()["notes"], "Проверка старой библиотеки")
        result = self.client.post("/api/backups/restore", json={"name": name})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.initial()["notes"], "Проверка старой библиотеки")
        for path in (file, self.data / "library.db"):
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(db.execute("SELECT count(*) FROM store_about").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT count(*) FROM custom_localizations").fetchone()[0], 0)

    def test_version_two_database_migrates_without_losing_the_store_cache(self):
        content_id = self.initial()["content_id"]
        cached = {"language": "ru", "description": "Описание в старой базе"}
        with sqlite3.connect(self.data / "library.db") as db:
            db.execute("DROP TABLE ps4_extras")
            db.execute("DROP TABLE custom_localizations")
            db.execute("PRAGMA user_version = 2")
            db.execute("INSERT INTO store_about VALUES (?,?,?)", (content_id, "ru", json.dumps(cached)))
        with TestClient(create_app(self.data), base_url="http://localhost") as reopened:
            self.assertEqual(reopened.get("/api/games/1").json()["custom_localizations"], [])
        with sqlite3.connect(self.data / "library.db") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(json.loads(db.execute("SELECT details FROM store_about").fetchone()[0]), cached)

    def test_version_three_backup_restores_and_migrates(self):
        legacy = self.root / "previous.db"
        with sqlite3.connect(self.data / "library.db") as current, sqlite3.connect(legacy) as old:
            current.backup(old)
            old.execute("DROP TABLE ps4_extras")
            old.execute("PRAGMA user_version=3")
        name = "prospero-20261125-130000-000000.zip"
        with ZipFile(self.data / "backups" / name, "w") as archive:
            archive.write(legacy, "library.db")
        restored = self.client.post("/api/backups/restore", json={"name": name})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.initial()["local_packages"], [])

    def test_playstation_store_art_preserves_bytes_and_backup(self):
        content_id = self.initial()["content_id"]
        image = "https://image.api.playstation.com/vulcan/portrait.jpg"
        media = [
            {"role": "PORTRAIT_BANNER", "type": "IMAGE", "url": image},
            {"role": "MASTER", "type": "IMAGE", "url": "https://other.example/cover.jpg"},
        ]
        fragment = '<script>' + json.dumps({"cache": {f"Product:{content_id}": {"id": content_id, "media": media}}}) + '</script>'
        props = {"productId": content_id, "batarangs": {"media-carousel": {"text": fragment}}}
        payload = json.dumps({"props": {"pageProps": props}}).replace("<", r"\u003c")
        page = f'<script id="__NEXT_DATA__">{payload}</script>'.encode()
        candidates = artwork.playstation_media(artwork.playstation_props(page, content_id), content_id, "Castlevania")
        self.assertEqual([item["url"] for item in candidates], [image])
        self.assertEqual(artwork.playstation_props(page, "UP0000-PPSA99999_00-FAKEPRODUCT00000"), {})
        original = io.BytesIO()
        Image.new("RGB", (150, 150), "orange").save(original, "JPEG")
        content = original.getvalue()
        with patch.object(artwork.Artwork, "search", return_value=(candidates, [])), patch.object(
            artwork.Artwork, "download", return_value=content
        ):
            found = self.client.post("/api/games/1/artwork/search").json()
            art = self.client.post("/api/games/1/artwork/select", json={"token": found["items"][0]["token"]})
        self.assertEqual(art.status_code, 200, art.text)
        cover = self.client.get(art.json()["cover"])
        self.assertEqual(cover.headers["content-type"], "image/jpeg")
        self.assertEqual(cover.content, content)
        backup = self.client.post("/api/backups").json()["name"]
        self.assertEqual(self.client.post("/api/backups/restore", json={"name": backup}).status_code, 200)
        self.assertEqual(self.client.get(self.initial()["cover"]).content, content)

    def test_artwork_keys_are_saved_privately_and_used_by_providers(self):
        path = "/api/settings/artwork"
        keys = {"IGDB_CLIENT_ID": "test-art-client", "IGDB_CLIENT_SECRET": "test-art-secret",
                "STEAMGRIDDB_API_KEY": "test-art-grid"}
        with patch.dict(os.environ, {"IGDB_ACCESS_TOKEN": "older-token"}):
            response = self.client.patch(path, json=keys)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["providers"]["IGDB"])
            self.assertTrue(response.json()["providers"]["SteamGridDB"])
            settings = self.client.get("/api/settings").text
            for value in keys.values():
                self.assertNotIn(value, response.text)
                self.assertNotIn(value, settings)
            rejected = self.client.patch(path, json={"STEAMGRIDDB_API_KEY": "invalid secret"})
            self.assertEqual(rejected.status_code, 422)
            self.assertNotIn("invalid secret", rejected.text)

            paths = []

            def respond(request):
                paths.append(request.url.host)
                if request.url.host == "id.twitch.tv":
                    self.assertIn(b"client_secret=test-art-secret", request.content)
                    return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
                if request.url.host == "api.igdb.com":
                    self.assertEqual(request.headers["Authorization"], "Bearer fresh-token")
                    self.assertEqual(request.headers["Client-ID"], keys["IGDB_CLIENT_ID"])
                    return httpx.Response(200, json=[])
                self.assertEqual(request.headers["Authorization"], "Bearer test-art-grid")
                return httpx.Response(200, json={"data": []})

            with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                store = artwork.Artwork(self.data)
                store.igdb(client, "Cuphead", "PS4")
                store.steamgrid(client, "Cuphead")
            self.assertEqual(paths, ["id.twitch.tv", "api.igdb.com", "www.steamgriddb.com"])
        backup = self.client.post("/api/backups").json()["name"]
        with ZipFile(self.data / "backups" / backup) as archive:
            self.assertNotIn("artwork-keys.json", archive.namelist())

    def test_steamgriddb_cdn2_covers_can_be_previewed_and_saved(self):
        game = self.client.post("/api/games", json={"title": "Cuphead", "platform": "PS4",
                                                     "title_id": "CUSA20469"}).json()
        endpoint = f"/api/games/{game['id']}/artwork"
        self.client.patch("/api/settings/artwork", json={"STEAMGRIDDB_API_KEY": "fixture-key"})
        url = "https://cdn2.steamgriddb.com/grid/cuphead.png"
        image = io.BytesIO()
        Image.new("RGB", (120, 180), "yellow").save(image, "PNG")
        responses = {
            "/api/v2/search/autocomplete/Cuphead": {"data": [{"id": 15, "name": "Cuphead"}]},
            "/api/v2/grids/game/15": {"data": [
                {"id": 41, "url": "https://other.example/cover.png"}, {"id": 42, "url": url}
            ]},
        }

        def respond(request):
            if str(request.url) == url:
                self.assertNotIn("authorization", request.headers)
                return httpx.Response(200, content=image.getvalue())
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-key")
            return httpx.Response(200, json=responses[request.url.path])

        transport = httpx.MockTransport(respond)
        clients = [httpx.Client(transport=transport), httpx.Client(transport=transport)]
        with patch.dict(os.environ, {"IGDB_CLIENT_ID": ""}), patch("backend.artwork.httpx.Client", side_effect=clients):
            found = self.client.post(endpoint + "/search")
            self.assertEqual(found.status_code, 200, found.text)
            self.assertIn("https://cdn2.steamgriddb.com ", found.headers["content-security-policy"])
            items = found.json()["items"]
            self.assertEqual([item["url"] for item in items], [url])
            saved = self.client.post(endpoint + "/select", json={"token": items[0]["token"]})
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["cover_source"], "SteamGridDB · 42")

        denied = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401)))
        with patch.dict(os.environ, {"IGDB_CLIENT_ID": ""}), patch("backend.artwork.httpx.Client", return_value=denied):
            found = self.client.post(endpoint + "/search").json()
        self.assertIn("SteamGridDB отклонил ключ (HTTP 401)", found["issues"])

    def test_store_art_uses_regional_page_for_the_same_product(self):
        content_id = "EP9000-CUSA03173_00-BLOODBORNE0000EU"
        url = "https://image.api.playstation.com/vulcan/bloodborne.jpg"
        media = [{"role": "PORTRAIT_BANNER", "type": "IMAGE", "url": url}]
        requests = []

        def store(request):
            requests.append(request.url.path)
            if request.url.path.startswith("/ru-ua/"):
                return httpx.Response(302, headers={"Location": "/ru-ua/pages/latest"})
            product = {"id": content_id, "media": media}
            cache = json.dumps({"cache": {f"Product:{content_id}": product}})
            props = {"productId": content_id, "batarangs": {"media-carousel": {"text": f"<script>{cache}</script>"}}}
            payload = json.dumps({"props": {"pageProps": props}}).replace("<", r"\u003c")
            return httpx.Response(200, text=f'<script id="__NEXT_DATA__">{payload}</script>')

        with httpx.Client(transport=httpx.MockTransport(store)) as client:
            candidates = artwork.Artwork().playstation(client, "Bloodborne", content_id)
        self.assertEqual([item["url"] for item in candidates], [url])
        self.assertEqual(requests, [f"/ru-ua/product/{content_id}", f"/en-gb/product/{content_id}"])

    def test_fih_game_type_and_package_exclusions(self):
        metadata = scanner.parse_metadata(CASTLEVANIA_INFO, Path("Castlevania.pkg"))
        self.assertEqual((metadata["title_id"], metadata["title"], metadata["version"], metadata["firmware"]),
                         ("PPSA35399", "Castlevania: Belmont's Curse", "01.000.000", "13.40"))
        self.assertEqual(metadata["version_uri"], VERSION_URI)
        patch = CASTLEVANIA_INFO.replace("Game / application (gd)", "Game patch (gp)")
        dlc = CASTLEVANIA_INFO.replace("Application / game (0x20)", "Additional Content (0x00000021)")
        self.assertIsNone(scanner.parse_metadata(patch, Path("Update.pkg")))
        self.assertIsNone(scanner.parse_metadata(dlc, Path("DLC.pkg")))
        with self.assertRaises(scanner.MetadataError):
            scanner.parse_metadata("Package type: FIH Debug\nTitle ID: PPSA35399", Path("Unknown.pkg"))

    def test_imported_castlevania_uses_bundled_publisher_metadata(self):
        self.client.delete("/api/games/1")
        folder = self.root / "Games"
        folder.mkdir()
        file = folder / "Castlevania [PPSA35399].pkg"
        file.write_bytes(b"local parser test fixture")
        os.utime(file, (time.time() - 15,) * 2)
        self.client.post("/api/roots", json={"path": str(folder)})
        with patch.object(scanner, "cli_path", return_value=Path("fpkg-cli")), patch.object(
            scanner, "invoke", side_effect=[(0, CASTLEVANIA_INFO), (1, "No art")]
        ), patch.object(artwork.Artwork, "providers", return_value={"IGDB": False}):
            self.assertEqual(self.wait_job(self.client.post("/api/scan", json={}))["report"]["added"], 1)
        game = self.initial()
        self.assertEqual(game["file_state"], "online")
        self.assertEqual(game["release_date"], "2026-10-15")
        self.assertEqual(game["cover_source"], "PlayStation Store")
        self.assertEqual(game["cover"], "/art/castlevania-cover.jpg")
        self.assertEqual(game["background"], "/art/castlevania-background.jpg")
        self.assertEqual(self.client.get(game["cover"]).status_code, 200)
        self.assertEqual(self.client.get(game["background"]).status_code, 200)
        self.assertIsNone(game["notes"])
        self.assertEqual(game["dlc"], [])
        saved = self.client.put("/api/games/1", json=game_body(game, release_date=None))
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertIsNone(self.initial()["release_date"])

    def test_copy_cli_and_use_local_copy(self):
        installed = self.root / "FPKG Builder"
        installed.mkdir()
        source = installed / scanner.CLI_FILENAME
        source.write_bytes(b"local CLI test fixture")
        (installed / "LibProsperoPkg.dll").write_bytes(b"dependency fixture")
        self.client.put("/api/settings", json={"cli_path": str(source), "ps5_firmware": "13.60"})
        local = self.root / "Prospero Library" / "tools" / "fpkg-cli"
        with patch.object(scanner, "LOCAL_CLI_DIRECTORY", local), patch.object(scanner, "invoke", return_value=(0, "FPKG")) as invoke:
            response = self.client.post("/api/settings/cli/copy")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["cli_local"])
            self.assertEqual(invoke.call_args.args[1], ["--help"])
            self.assertEqual((local / scanner.CLI_FILENAME).read_bytes(), source.read_bytes())
            self.assertEqual((local / "LibProsperoPkg.dll").read_bytes(), b"dependency fixture")
            installed.rename(self.root / "Moved FPKG Builder")
            check = self.client.post("/api/settings/cli")
            self.assertTrue(check.json()["available"])
            self.assertEqual(invoke.call_args.args[0], local / scanner.CLI_FILENAME)

    def test_patch_diagnostics_distinguish_network_and_bad_xml(self):
        def dns_error(_):
            raise httpx.ConnectError("[Errno 11001] getaddrinfo failed")

        def tls_error(_):
            raise httpx.ConnectError("Handshake failed") from ssl.SSLCertVerificationError(1, "untrusted issuer")

        for transport, expected in (
            (dns_error, "DNS не смог разрешить адрес Sony CDN"),
            (tls_error, "Не удалось проверить TLS-сертификат Sony CDN (untrusted issuer)"),
            (lambda _: httpx.Response(200, content=b"<malformed"), "Некорректный XML манифеста Sony CDN"),
        ):
            with self.subTest(expected=expected):
                client = httpx.Client(transport=httpx.MockTransport(transport))
                with patch.object(patches.httpx, "Client", return_value=client):
                    job = self.wait_job(self.client.post("/api/updates?game_id=1"))
                self.assertEqual(job["report"]["errors"], 1)
                self.assertIn(expected, job["report"]["issues"][0])
                self.assertIn(expected, self.client.get("/api/logs").json()["text"])

    def test_manifest_log_includes_underlying_connection_error(self):
        def proxy_error(_):
            raise httpx.ConnectError("Proxy https://demo:secret@proxy.example failed") from OSError(
                10061, "Connection refused"
            )

        client = httpx.Client(transport=httpx.MockTransport(proxy_error))
        with patch.object(patches.httpx, "Client", return_value=client):
            self.wait_job(self.client.post("/api/updates?game_id=1"))
        log = self.client.get("/api/logs").json()["text"]
        self.assertIn("GET XML " + VERSION_URI, log)
        self.assertIn("[Errno 10061] Connection refused", log)
        self.assertIn("https://[скрыто]@proxy.example", log)
        self.assertNotIn("demo:secret", log)

    def test_windows_manifest_verifier_keeps_tls_checks(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(patches.sys, "platform", "win32"):
            verifier = patches.manifest_verifier()
            self.assertIsInstance(verifier, patches.truststore.SSLContext)
            self.assertEqual(verifier.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(verifier.check_hostname)
            with patch.dict(os.environ, {"SSL_CERT_FILE": "custom-ca.pem"}):
                self.assertIs(patches.manifest_verifier(), True)
            with patch.dict(os.environ, {"SSL_CERT_DIR": "custom-ca"}):
                self.assertIs(patches.manifest_verifier(), True)

    def test_origin_and_manifest_validation(self):
        data = game_body(self.initial(), notes="Не должно сохраниться")
        response = self.client.put("/api/games/1", json=data, headers={"Origin": "http://another.example"})
        self.assertEqual(response.status_code, 403)
        response = self.client.put("/api/games/1", json={**data, "manifest_uri": "https://example.org/manifest.xml"})
        self.assertEqual(response.status_code, 422)
        response = self.client.put("/api/games/1", json={**data, "manifest_uri": VERSION_URI})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["version_uri"], VERSION_URI)
        self.assertEqual(self.client.get("/api/health", headers={"Host": "another.example"}).status_code, 400)

    def test_backup_restore_and_integrity(self):
        item = self.initial()
        uploaded = io.BytesIO()
        Image.new("RGB", (280, 600), "orange").save(uploaded, "PNG")
        response = self.client.post("/api/games/1/artwork?kind=cover",
            files={"file": ("image.png", uploaded.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()["cover"]
        with Image.open(io.BytesIO(self.client.get(url).content)) as stored:
            self.assertEqual(stored.size, (280, 600))
        name = self.client.post("/api/backups").json()["name"]
        archive = self.client.get("/api/backups/" + name)
        self.assertEqual(archive.status_code, 200)
        with ZipFile(io.BytesIO(archive.content)) as zf:
            self.assertEqual(set(zf.namelist()), {"library.db", "artwork/" + url.split("/")[-1]})
        response = self.client.put("/api/games/1", json=game_body(item, notes="После бэкапа"))
        self.assertEqual(response.status_code, 200, response.text)
        restored = self.client.post("/api/backups/restore", json={"name": name})
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.initial()["notes"], item["notes"])
        self.assertEqual(self.initial()["cover"], url)
        self.assertEqual(len(self.client.get("/api/backups").json()), 2)

    def test_scan_import_relink_offline_and_preserve_edits(self):
        folder = self.root / "Коллекция"
        folder.mkdir()
        base = folder / "Castlevania. Belmont's Curse [PPSA35399] [01.001.000].pkg"
        ps4 = folder / "Ёж [CUSA12345] [01.00].PKG"
        separate_dlc = folder / "Дополнение [PPSA44444].pkg"
        invalid = folder / "Повреждённый.pkg"
        skipped = folder / "_WorkHard"
        skipped.mkdir()
        paths = (base, ps4, separate_dlc, invalid, skipped / "Ignore.pkg")
        for path in paths:
            path.write_bytes(b"local parser test fixture")
            os.utime(path, (time.time() - 15,) * 2)
        digests = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        self.assertEqual(self.client.post("/api/roots", json={"path": str(skipped)}).status_code, 422)
        root_id = self.client.post("/api/roots", json={"path": str(folder)}).json()["id"]
        saved = self.client.put("/api/games/1", json=game_body(self.initial(), title="Моя Castlevania",
                                                              notes="Ручное примечание"))
        self.assertEqual(saved.status_code, 200, saved.text)

        def mock_cli(_exe, arguments, **_):
            verb, target, *_ = arguments
            if target == base and verb == "pkg-info":
                return 0, CASTLEVANIA_INFO.replace("01.000.000", "01.001.000")
            if target == ps4 and verb == "ps4-info":
                return 0, "TITLE_ID = CUSA12345\nTITLE: Ёж\nCATEGORY: gd\nAPP_VER: 01.00\nSYSTEM_VER: 0x09000000"
            if target == separate_dlc and verb == "pkg-info":
                return 0, "Content ID: JP0101-PPSA44444_00-EXAMPLEOTHER00000\nType: AC (0x21)"
            if target == invalid and verb == "pkg-info":
                return 0, "Package type: FIH Debug\nTitle ID: PPSA99999"
            return 1, "Invalid test fixture"

        with patch.object(scanner, "cli_path", return_value=self.root / "fpkg-cli"), patch.object(scanner, "invoke", side_effect=mock_cli):
            first = self.wait_job(self.client.post("/api/scan", json={}))
            self.assertEqual(first["status"], "done", first)
            report = first["report"]
            self.assertEqual((report["found"], report["added"], report["updated"], report["skipped"], report["errors"]),
                             (4, 1, 1, 1, 1), report)
            card = self.initial()
            self.assertEqual(card["title"], "Моя Castlevania")
            self.assertEqual(card["notes"], "Ручное примечание")
            self.assertEqual(card["cover_source"], "PlayStation Store")
            self.assertEqual(card["version"], "01.001.000")
            self.assertEqual(card["file_state"], "online")
            journal = self.client.get("/api/logs").json()["text"]
            self.assertIn("fpkg-cli pkg-info: код 0, PPSA35399 01.001.000", journal)
            self.assertIn("packagetype='FIH Debug'", journal)
            self.assertIn("fpkg-cli ps4-info: код 1", journal)
            self.assertIn("ошибок 1", journal)
            download = self.client.get("/api/logs?download=true")
            self.assertIn("prospero-log.txt", download.headers["Content-Disposition"])
            self.assertEqual(download.text, journal)

            second = self.wait_job(self.client.post("/api/scan", json={}))
            self.assertEqual(second["report"]["added"], 0)
            self.assertEqual(len(self.client.get("/api/games").json()), 2)
            self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}, digests)
            self.assertEqual(set(card["package_fields"]), {"platform", "title_id", "version", "firmware", "size_bytes"})
            locked = self.client.put("/api/games/1", json=game_body(card, version="99.99", edit_pkg_fields=False)).json()
            self.assertEqual(locked["version"], "01.001.000")
            duplicate = self.client.put("/api/games/1", json=game_body(card, title_id="CUSA12345", platform="PS4",
                                                                     edit_pkg_fields=True))
            self.assertEqual(duplicate.status_code, 422)
            edited = self.client.put("/api/games/1", json=game_body(card, title_id="CUSA54321", platform="PS4",
                                                                  version="01.002.000", edit_pkg_fields=True))
            self.assertEqual(edited.status_code, 200, edited.text)
            self.assertEqual(edited.json()["package_identity"], {"platform": "PS5", "title_id": "PPSA35399"})
            forced = self.wait_job(self.client.post("/api/scan", json={"force": True}))
            self.assertEqual(forced["report"]["added"], 0)
            self.assertEqual(len(self.client.get("/api/games").json()), 2)
            self.assertEqual((self.initial()["title_id"], self.initial()["version"]), ("CUSA54321", "01.002.000"))

            def patch_response(request):
                self.assertEqual(str(request.url), VERSION_URI)
                return httpx.Response(200, content=b'<titlepatch><package content_ver="01.003.000" system_ver="337641479"/></titlepatch>')

            patch_client = httpx.Client(transport=httpx.MockTransport(patch_response))
            with patch.object(patches.httpx, "Client", return_value=patch_client):
                self.assertEqual(self.wait_job(self.client.post("/api/updates?game_id=1"))["report"]["updated"], 1)
            self.assertEqual(self.initial()["patch"]["target"], "13.60")
            moved = self.root / "Перенесённая коллекция"
            folder.rename(moved)
            self.wait_job(self.client.post("/api/scan", json={}))
            self.assertEqual(self.initial()["file_state"], "offline")
            self.assertEqual(len(self.client.get("/api/games").json()), 2)
            self.assertEqual(self.client.put(f"/api/roots/{root_id}", json={"path": str(moved)}).status_code, 200)
            self.wait_job(self.client.post("/api/scan", json={}))
            self.assertEqual(self.initial()["file_state"], "online")

    def test_scan_multiple_roots_and_continue_without_removable_disk(self):
        removable = self.root / "Съёмный диск"
        local = self.root / "Коллекция"

        def write_package(root, title_id):
            directory = root / "PS4" / "Games"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{title_id}.pkg"
            path.write_bytes(b"package fixture")
            os.utime(path, (time.time() - 15,) * 2)

        write_package(removable, "CUSA10001")
        write_package(local, "CUSA10002")
        routes = "/api/roots/batch"
        response = self.client.post(routes, json={"roots": [{"path": f'"{removable}"'}, {"path": str(local)}]})
        self.assertEqual(response.status_code, 201, response.text)
        removable_id, _ = response.json()["ids"]
        repeated = self.client.post(routes, json={"roots": [{"path": str(self.root / "Extra")}, {"path": str(local)}]})
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(len(self.client.get("/api/settings").json()["roots"]), 2)

        def read_metadata(_executable, path, _stop):
            return scanner.parse_metadata(f"Title ID: {path.stem}\nTitle: {path.stem}\nCategory: gd\nApp ver: 01.00", path)

        with patch.object(scanner, "cli_path", return_value=self.root / "fpkg-cli"), patch.object(
            scanner, "read_metadata", side_effect=read_metadata
        ) as parse, patch.object(scanner, "enrich_import"):
            def scan_collection():
                return self.wait_job(self.client.post("/api/scan", json={}))["report"]

            first = scan_collection()
            self.assertEqual((first["found"], first["added"]), (2, 2))
            moved = self.root / "Другой диск"
            removable.rename(moved)
            write_package(local, "CUSA10003")
            second = scan_collection()
            self.assertEqual((second["found"], second["added"]), (2, 1))
            self.assertIn(str(removable), " ".join(second["issues"]))
            games = {game["title_id"]: game for game in self.client.get("/api/games").json()}
            self.assertEqual(games["CUSA10001"]["file_state"], "offline")
            self.assertEqual(games["CUSA10003"]["file_state"], "online")
            self.assertEqual(self.client.put(f"/api/roots/{removable_id}", json={"path": str(moved)}).status_code, 200)
            resumed = scan_collection()
            self.assertEqual((resumed["found"], resumed["added"], parse.call_count), (3, 0, 3))
            restored = self.client.get(f"/api/games/{games['CUSA10001']['id']}").json()
            self.assertEqual(restored["file_state"], "online")

    def test_scan_ps4_base_patch_and_dlc_in_one_folder(self):
        folder = self.root / "PS4"
        folder.mkdir()
        content_id = "EP0101-CUSA12345_00-BASE000000000001"

        def ps4_info(title, title_id, package_id, category, version):
            fields = {"Title": title, "Title ID": title_id, "Content ID": package_id,
                      "Category": category, "VERSION": version, "VERSION (master)": "01.00",
                      "Size": "128 bytes", "Passcode matches": "Yes", "FPKG key recoverable": "Yes"}
            return "\n".join(f"  {label:26} {value}" for label, value in fields.items())

        examples = {
            "01 patch.pkg": ps4_info("Example: Edition", "CUSA12345", content_id, "gp", "01.10"),
            "02 dlc.pkg": ps4_info("Bonus: Level", "", "EP0101-CUSA12345_00-EXPANSION0000001", "ac", "01.00"),
            "03 language.pkg": ps4_info("French Language Pack", "", "EP0101-CUSA12345_00-LANGUAGE00000001", "ac", ""),
            "04 orphan.pkg": ps4_info("", "CUSA12346", "", "gp", "02.00"),
            "05 old update.pkg": ps4_info("", "CUSA12345", content_id, "gp", "01.02"),
        }
        for name in examples:
            path = folder / name
            path.write_bytes(b"\x7fCNT test fixture")
            os.utime(path, (time.time() - 15,) * 2)
        self.client.post("/api/roots", json={"path": str(folder)})

        def invoke(_exe, arguments, **_):
            command, path = arguments
            self.assertEqual(command, "ps4-info")
            return 0, examples[path.name]

        with patch.object(scanner, "cli_path", return_value=self.root / "fpkg-cli"), patch.object(
            scanner, "invoke", side_effect=invoke
        ) as cli, patch.object(scanner, "enrich_import"):
            def scan():
                return self.wait_job(self.client.post("/api/scan", json={}))["report"]

            self.assertEqual(scan()["added"], 0)
            self.assertEqual(len(self.client.get("/api/games").json()), 1)
            base = folder / "99 original.pkg"
            examples[base.name] = ps4_info("Example: Edition", "CUSA12345", content_id, "gd", "01.00")
            base.write_bytes(b"\x7fCNT base fixture")
            os.utime(base, (time.time() - 15,) * 2)
            self.assertEqual(scan()["added"], 1)
            game = next(game for game in self.client.get("/api/games").json() if game["title_id"] == "CUSA12345")
            url = f"/api/games/{game['id']}"
            self.assertEqual(game["title"], "Example: Edition")
            self.assertEqual((game["version"], game["collection_version"]), ("01.00", "01.10"))
            self.assertEqual(game["size_bytes"], base.stat().st_size)
            self.assertEqual([(item["kind"], item["relpath"], item["file_state"]) for item in game["local_packages"]],
                             [("dlc", "02 dlc.pkg", "online"), ("patch", "01 patch.pkg", "online"),
                              ("patch", "05 old update.pkg", "online")])
            self.assertEqual(game["local_packages"][0]["title"], "Bonus: Level")
            self.assertEqual(cli.call_count, 7)
            self.assertEqual(scan()["skipped"], 6)
            self.assertEqual(cli.call_count, 8)
            with sqlite3.connect(self.data / "library.db") as db:
                stamp = "2026-11-25T12:00:00+00:00"
                db.execute("INSERT INTO checks VALUES (?,'ok','Test',?,?,NULL,'01.10')", (game["id"], stamp, stamp))
                db.execute("INSERT INTO patch_history VALUES (?,'01.10',NULL,'Test',?,NULL)", (game["id"], stamp))
            self.assertEqual(self.client.get(url).json()["patch"]["state"], "current")
            (folder / "01 patch.pkg").unlink()
            scan()
            offline = self.client.get(url).json()
            self.assertEqual(offline["collection_version"], "01.02")
            self.assertEqual(offline["patch"]["state"], "available")
            self.assertEqual(offline["local_packages"][1]["file_state"], "offline")
            name = self.client.post("/api/backups").json()["name"]
            self.assertEqual(self.client.post("/api/backups/restore", json={"name": name}).status_code, 200)
            self.assertEqual(self.client.get(url).json()["local_packages"], offline["local_packages"])

    def test_patch_versions_and_network_errors(self):
        game = self.initial()
        self.assertTrue(catalog.version_key("01.010.000") > catalog.version_key("01.002.000"))
        self.assertEqual(catalog.version_key("1.02"), catalog.version_key("01.002.000"))
        xml = b'<titlepatch><patch version="01.010.000" system_ver="0x1361000000000000"/>' \
              b'<patch version="01.002.000" requiredSystemSoftwareVersion="13.40"/></titlepatch>'
        requests = []
        def response(req):
            requests.append(req.url)
            return httpx.Response(200, content=xml)
        client = httpx.Client(transport=httpx.MockTransport(response))
        with patch.object(patches.httpx, "Client", return_value=client):
            job = self.wait_job(self.client.post("/api/updates?game_id=1"))
        self.assertEqual(job["report"]["updated"], 1, job)
        self.assertEqual(str(requests[0]), VERSION_URI)
        update = self.initial()["patch"]
        self.assertEqual((update["state"], update["latest"]["version"], update["compatible"]["version"]),
                         ("available", "01.010.000", "01.002.000"))
        self.assertIsNotNone(update["check"]["last_success"])

        for status, expected in [(429, "error"), (404, "unknown")]:
            client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status)))
            with patch.object(patches.httpx, "Client", return_value=client):
                self.wait_job(self.client.post("/api/updates?game_id=1"))
            update = self.initial()["patch"]
            self.assertEqual(update["state"], expected)
            self.assertIsNotNone(update["check"]["last_success"])

        def timeout(_):
            raise httpx.ReadTimeout("fixture")
        client = httpx.Client(transport=httpx.MockTransport(timeout))
        with patch.object(patches.httpx, "Client", return_value=client):
            self.wait_job(self.client.post("/api/updates?game_id=1"))
        self.assertEqual(self.initial()["patch"]["state"], "error")

        xml = b'<titlepatch><patch version="01.012.000"/></titlepatch>'
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=xml)))
        with patch.object(patches.httpx, "Client", return_value=client):
            self.wait_job(self.client.post("/api/updates?game_id=1"))
        update = self.initial()["patch"]
        self.assertEqual(update["latest"]["version"], "01.012.000")
        self.assertIsNone(update["latest"]["firmware"])
        self.assertIsNone(update["compatible"])
        self.assertEqual(len(update["history"]), 3)
        response = self.client.put("/api/games/1", json=game_body(game, official_version="01.014.000",
                                                                   official_firmware="13.60"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["patch"]["manual"]["source"], "Вручную")
        self.assertEqual(response.json()["patch"]["latest"]["version"], "01.012.000")

    def test_prosperopatches_online_fallback(self):
        key = "a" * 64
        title = "PPSA35399"
        page = f'<div id="dynpatch" data-titleid="{title}" data-key="{key}"></div>'
        entries = [
            {"content_ver": "01.001.000", "required_firmware": "14.00.00.39"},
            {"content_ver": "01.001.004", "required_firmware": "14.20.00.07"},
            {"content_ver": "01.001.002", "required_firmware": "14.10.00.03"},
        ]

        requests = []

        def respond(request):
            requests.append((request.method, str(request.url)))
            if request.url.host == patches.PS5_HOST:
                raise httpx.ConnectError("untrusted root")
            if request.method == "GET":
                return httpx.Response(200, text=page)
            self.assertEqual(json.loads(request.content), {"titleid": title, "key": key})
            return httpx.Response(200, json={"success": True, "titleid": title, "patches": entries})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch.object(patches.httpx, "Client", return_value=client):
            self.assertEqual(self.wait_job(self.client.post("/api/updates?game_id=1"))["report"]["updated"], 1)
        site_url = f"https://{patches.PATCH_SITE_HOST}"
        self.assertEqual(requests, [("GET", VERSION_URI), ("GET", f"{site_url}/{title}"),
                                    ("POST", f"{site_url}/api/internal/loadpatches")])
        result = self.initial()["patch"]
        self.assertEqual(result["check"]["source"], "ProsperoPatches")
        self.assertEqual(result["latest"]["firmware"], "14.20.00.07")
        self.assertEqual(result["latest"]["version"], "01.001.004")
        self.assertEqual(len(result["history"]), 3)
        self.assertIsNone(result["compatible"])
        self.assertEqual(patches.decode_manifest_firmware("systemver", "337641479", title), "14.20.00.07")

    def test_orbispatches_checks_ps4_games_without_a_sony_manifest(self):
        title = "CUSA20469"
        key = "b" * 64
        saved = self.client.post("/api/games", json={"title": "Cuphead", "platform": "PS4",
                                                       "title_id": title, "version": "01.05"})
        self.assertEqual(saved.status_code, 201, saved.text)
        game_id = saved.json()["id"]
        entries = [{"version": "01.01", "required_firmware": "7.51"},
                   {"version": "01.05", "required_firmware": "9.60"}]
        paths = []

        def respond(request):
            self.assertEqual(request.url.host, patches.ORBIS_PATCH_HOST)
            paths.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(200, text=f"""<div id="dynpatch"
                    data-loadparams="{{ 'titleid': '{title}', 'key': '{key}' }}"></div>""")
            self.assertEqual(json.loads(request.content), {"titleid": title, "key": key})
            return httpx.Response(200, json={"success": True, "titleid": title, "patches": entries})

        client = httpx.Client(transport=httpx.MockTransport(respond))
        with patch.object(patches.httpx, "Client", return_value=client):
            report = self.wait_job(self.client.post(f"/api/updates?game_id={game_id}"))["report"]
        self.assertEqual((report["updated"], report["errors"]), (1, 0), report)
        self.assertEqual(paths, [("GET", "/" + title), ("POST", "/api/internal/loadpatches")])
        update = self.client.get(f"/api/games/{game_id}").json()["patch"]
        self.assertEqual(update["state"], "current")
        self.assertEqual(update["latest"]["source"], "OrbisPatches")
        self.assertEqual(update["latest"]["version"], "01.05")
        self.assertEqual(update["latest"]["firmware"], "9.60")
