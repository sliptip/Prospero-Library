VERSION_URI = (
    "https://sgst.prod.dl.playstation.net/sgst/prod/00/np/PPSA35399_00/"
    "5c2cf8ed-34cf-44e0-8093-666033d7069c-version.xml"
)
PUBLISHER_METADATA = {
    ("PS5", "PPSA35399"): {
        "release_date": "2026-10-15",
        "cover": "/art/castlevania-cover.jpg",
        "background": "/art/castlevania-background.jpg",
        "source": "PlayStation Store",
    },
}
LEGACY_ART = {
    "/art/castlevania-cover.webp": "/art/castlevania-cover.jpg",
    "/art/castlevania-background.webp": "/art/castlevania-background.jpg",
}


def initial_game(db):
    publisher = PUBLISHER_METADATA["PS5", "PPSA35399"]
    game = db.execute(
        """INSERT INTO games (platform,title_id,title,edition,release_date,added_at,notes,
                              cover,background,cover_source,background_source,overrides)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("PS5", "PPSA35399", "Castlevania: Belmont's Curse", "Midnight Edition",
         publisher["release_date"], "2026-11-25T00:00:00+03:00",
         "All-in-One. В конфигурацию сборки включены Midnight Edition и Preorder Alucard Skin. "
         "FPKG собран 25.11.2026. Проверки целостности пройдены. "
         "Запуск, доступность дополнений и сохранения на PS5 Pro 13.60 ещё не проверены.",
         publisher["cover"], publisher["background"],
         publisher["source"], publisher["source"], '["title","edition","release_date","notes","dlc"]'),
    ).lastrowid
    db.execute(
        """INSERT INTO builds (game_id,identity,version,firmware,size_bytes,content_id,version_uri,sdk,sha256)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (game, "seed", "01.000.000", "13.40", 4043482656, "JP0101-PPSA35399_00-CVBCAPP000000000",
         VERSION_URI, "11.00", "97BB5F608751A361F1372D31A708F9B73D1BA113DBB956F54F9A2A469FBB3B3A"),
    )
    db.executemany(
        "INSERT INTO dlc (game_id,name,description,status,content_id) VALUES (?,?,?,?,?)",
        [(game, "Midnight Edition DLC", "Костюмы в стиле Тревора и Сифы и реликвия Family’s Grace.",
          "configured", "JP0101-PPSA35399_00-DLCBONUSCONTENTS"),
         (game, "Preorder Alucard Skin DLC", "Костюм Розы в стиле Алукарда.",
          "configured", "JP0101-PPSA35399_00-DLCALUCARDSTYLEC")],
    )
