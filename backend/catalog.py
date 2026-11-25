import json
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

from .database import now
from .seed import LEGACY_ART, PUBLISHER_METADATA

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
Version = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}$")]


class DLC(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    description: str | None = Field(default=None, max_length=1000)
    status: Literal["configured", "verified", "unknown", "absent"] = "unknown"
    content_id: str | None = Field(default=None, max_length=80)


class GameInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: Name
    platform: Literal["PS4", "PS5"]
    title_id: str = Field(pattern=r"^(CUSA|PPSA)[0-9]{5}$")
    edition: str | None = Field(default=None, max_length=256)
    version: Version | None = None
    firmware: Version | None = None
    size_bytes: int | None = Field(default=None, ge=1, le=10**15)
    release_date: date | None = None
    ru_text: bool | None = None
    ru_audio: bool | None = None
    text_languages: str | None = Field(default=None, max_length=512)
    audio_languages: str | None = Field(default=None, max_length=512)
    localization_source: str | None = Field(default=None, max_length=512)
    notes: str | None = Field(default=None, max_length=12000)
    dlc: list[DLC] = Field(default_factory=list, max_length=100)
    official_version: Version | None = None
    official_firmware: Version | None = None
    manifest_uri: str | None = Field(default=None, max_length=512)
    edit_pkg_fields: bool | None = None

    @model_validator(mode="after")
    def platform_id(self):
        if self.title_id.startswith("PPSA") != (self.platform == "PS5"):
            raise ValueError("Title ID не соответствует платформе")
        if self.official_firmware and not self.official_version:
            raise ValueError("Укажите версию официального патча")
        if self.manifest_uri:
            from .patches import safe_manifest_url
            if not safe_manifest_url(self.manifest_uri, self.title_id):
                raise ValueError("Нужен HTTPS XML-манифест Sony для указанного Title ID")
        return self


GAME_FIELDS = (
    "title", "platform", "title_id", "edition", "release_date", "ru_text", "ru_audio",
    "text_languages", "audio_languages", "localization_source", "notes",
)
BUILD_FIELDS = ("version", "firmware", "size_bytes")


def version_key(value):
    if not value or not re.fullmatch(r"\d{1,4}(?:\.\d{1,4}){1,3}", value):
        return ()
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * (4 - len(parts))


def settings(db):
    return dict(db.execute("SELECT key,value FROM settings").fetchall())


def set_display_identity(db, game_id, display, package):
    aliases = json.loads(settings(db).get("card_identities", "{}"))
    key = str(game_id)
    if display != package and display is not None:
        other = db.execute("SELECT 1 FROM games WHERE platform=? AND title_id=? AND id<>?", (*display, game_id)).fetchone()
        if other or any(tuple(value) == display for owner, value in aliases.items() if owner != key):
            raise ValueError("Карточка с таким Title ID уже есть")
        aliases[key] = display
    elif aliases.pop(key, None) is None:
        return
    db.execute("INSERT INTO settings VALUES ('card_identities',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (json.dumps(aliases),))


def game_order(ids, saved):
    remaining = dict.fromkeys(ids)
    ordered = []
    for game_id in json.loads(saved):
        if game_id in remaining:
            ordered.append(game_id)
            del remaining[game_id]
    return [*ordered, *remaining]


def store_game_order(db, ordered):
    db.execute("INSERT INTO settings VALUES ('game_order',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (json.dumps(ordered),))


def swap_games(db, source_id, target_id):
    db.execute("BEGIN IMMEDIATE")
    ordered = game_order((row[0] for row in db.execute("SELECT id FROM games ORDER BY id")),
                         settings(db).get("game_order", "[]"))
    source, target = ordered.index(source_id), ordered.index(target_id)
    ordered[source], ordered[target] = ordered[target], ordered[source]
    store_game_order(db, ordered)
    return ordered


def patch_view(db, game, target):
    check = db.execute("SELECT * FROM checks WHERE game_id=?", (game["id"],)).fetchone()
    history = [dict(row) for row in db.execute(
        """SELECT version, firmware, source, checked_at, size_bytes FROM patch_history
           WHERE game_id=? ORDER BY source=? DESC, checked_at DESC""",
        (game["id"], check["source"] if check else None),
    )]
    history.sort(key=lambda row: version_key(row["version"]), reverse=True)
    manual = next((row for row in history if row["source"] == "Вручную"), None)
    history = [row for row in history if row["source"] != "Вручную"]
    reported = version_key(check["latest_version"]) if check else ()
    latest = next((row for row in history if reported and version_key(row["version"]) == reported), None)
    if latest and target:
        limit = version_key(target)
        candidates = [row for row in history if version_key(row["version"]) <= version_key(latest["version"])]
        compatible = next((row for row in candidates if version_key(row["firmware"]) and version_key(row["firmware"]) <= limit), None)
        if compatible and any(
            not version_key(row["firmware"]) and version_key(row["version"]) > version_key(compatible["version"])
            for row in candidates
        ):
            compatible = None
    else:
        compatible = None
    state = "unknown"
    if latest and version_key(game["version"]):
        state = "available" if version_key(latest["version"]) > version_key(game["version"]) else "current"
    if check and check["state"] == "error":
        state = "error"
    return {"state": state, "latest": latest, "manual": manual, "compatible": compatible, "history": history,
            "target": target or None, "check": dict(check) if check else None}


def library(db, game_id=None):
    params = () if game_id is None else (game_id,)
    where = "" if game_id is None else "WHERE id=?"
    games = [dict(row) for row in db.execute(f"SELECT * FROM games {where} ORDER BY id", params)]
    config = settings(db)
    aliases = json.loads(config.get("card_identities", "{}"))
    if game_id is None and "game_order" in config:
        by_id = {game["id"]: game for game in games}
        games = [by_id[key] for key in game_order(by_id, config["game_order"])]
    for game in games:
        overrides = set(json.loads(game.pop("overrides")))
        builds = [dict(row) for row in db.execute(
            """SELECT b.*, EXISTS (SELECT 1 FROM files f JOIN roots r ON r.id=f.root_id
                          WHERE f.build_id=b.id AND f.available=1 AND r.state IN ('online','partial')) online,
                      EXISTS (SELECT 1 FROM files f WHERE f.build_id=b.id) linked
                FROM builds b WHERE b.game_id=?""", (game["id"],),
        )]
        imported = max((row for row in builds if row["identity"] not in ("manual", "seed")),
                       key=lambda row: (version_key(row["version"]), row["online"], row["id"]), default={})
        game["package_identity"] = {key: game[key] for key in ("platform", "title_id")} if imported else None
        game["package_fields"] = []
        if imported:
            game["package_fields"] = ["platform", "title_id", *(key for key in BUILD_FIELDS if imported.get(key) is not None)]
            if "title" not in overrides:
                game["package_fields"].append("title")
        publisher = PUBLISHER_METADATA.get((game["platform"], game["title_id"]))
        if publisher:
            if imported and game["release_date"] is None and "release_date" not in overrides:
                game["release_date"] = publisher["release_date"]
            for kind in ("cover", "background"):
                if (imported and game[kind] is None) or game[kind] in LEGACY_ART:
                    game[kind] = publisher[kind]
                    game[kind + "_source"] = publisher["source"]
        defaults = next((row for row in builds if row["identity"] == "seed"), {})
        manual = next((row for row in builds if row["identity"] == "manual"), {})
        build = max((row for row in (imported, defaults, manual) if row),
                    key=lambda row: (version_key(row["version"]), row.get("online", 0), row["identity"] != "manual"),
                    default={})
        for key in BUILD_FIELDS:
            if key in overrides and key in manual:
                game[key] = manual[key]
            else:
                game[key] = build.get(key) if build.get(key) is not None else defaults.get(key)
        game["manifest_uri"] = manual.get("version_uri")
        game["version_uri"] = game["manifest_uri"] or imported.get("version_uri") or defaults.get("version_uri")
        game["content_id"] = imported.get("content_id") or defaults.get("content_id")
        file_build = build if build.get("linked") else imported
        game["file_state"] = "online" if file_build.get("online") else "offline" if file_build.get("linked") else "unlinked"
        game["dlc"] = [dict(row) for row in db.execute(
            "SELECT name,description,status,content_id FROM dlc WHERE game_id=? ORDER BY id", (game["id"],),
        )]
        for key in ("ru_text", "ru_audio"):
            game[key] = None if game[key] is None else bool(game[key])
        game["patch"] = patch_view(db, game, config.get(game["platform"].lower() + "_firmware"))
        if imported and (alias := aliases.get(str(game["id"]))):
            game["platform"], game["title_id"] = alias
    return games


def save_game(db, item, game_id=None):
    values = item.model_dump(mode="json")
    if game_id is None:
        fields = ", ".join(GAME_FIELDS)
        slots = ", ".join("?" for _ in GAME_FIELDS)
        game_id = db.execute(f"INSERT INTO games ({fields},added_at) VALUES ({slots},?)",
                             (*(values[key] for key in GAME_FIELDS), now())).lastrowid
        existing = {}
    else:
        existing = library(db, game_id)[0]
        if item.edit_pkg_fields is False:
            values.update({key: existing[key] for key in existing["package_fields"]})
        identity = (values["platform"], values["title_id"])
        package = existing["package_identity"]
        if package:
            package_identity = (package["platform"], package["title_id"])
            if identity != (existing["platform"], existing["title_id"]):
                if not item.edit_pkg_fields:
                    raise ValueError("Включите редактирование полей из PKG")
                set_display_identity(db, game_id, identity, package_identity)
            values.update(package)
        elif identity != (existing["platform"], existing["title_id"]):
            raise ValueError("Платформа и Title ID существующей карточки закреплены")
        fields = ", ".join(f"{key}=?" for key in GAME_FIELDS)
        db.execute(f"UPDATE games SET {fields} WHERE id=?", (*(values[key] for key in GAME_FIELDS), game_id))
        if "manifest_uri" not in item.model_fields_set:
            values["manifest_uri"] = existing["manifest_uri"]
    pinned = set(json.loads(db.execute("SELECT overrides FROM games WHERE id=?", (game_id,)).fetchone()[0]))
    pinned.update(key for key in (*GAME_FIELDS, *BUILD_FIELDS, "dlc")
                  if key not in ("platform", "title_id") and values[key] != existing.get(key))
    db.execute("UPDATE games SET overrides=? WHERE id=?", (json.dumps(sorted(pinned)), game_id))
    db.execute(
        """INSERT INTO builds (game_id,identity,version,firmware,size_bytes,version_uri) VALUES (?,'manual',?,?,?,?)
           ON CONFLICT(game_id,identity) DO UPDATE SET version=excluded.version,
              firmware=excluded.firmware,size_bytes=excluded.size_bytes,version_uri=excluded.version_uri""",
        (game_id, *(values[key] for key in BUILD_FIELDS), values["manifest_uri"]),
    )
    if values["dlc"] != existing.get("dlc", []):
        db.execute("DELETE FROM dlc WHERE game_id=?", (game_id,))
        db.executemany("INSERT INTO dlc (game_id,name,description,status,content_id) VALUES (?,?,?,?,?)", [
            (game_id, row.name, row.description, row.status, row.content_id) for row in item.dlc
        ])
    if "official_version" in item.model_fields_set:
        db.execute("DELETE FROM patch_history WHERE game_id=? AND source='Вручную'", (game_id,))
        if item.official_version:
            db.execute("INSERT INTO patch_history (game_id,version,firmware,source,checked_at) VALUES (?,?,?,?,?)",
                       (game_id, item.official_version, item.official_firmware, "Вручную", now()))
    return library(db, game_id)[0]
