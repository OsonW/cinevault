import app as app_module


def _media(**over):
    base = {
        "id": 7,
        "title": "Abc",
        "media_type": "movie",
        "status": "watchlist",
        "last_season": 0,
        "last_episode": 0,
        "last_volume": 0,
        "last_chapter": 0,
        "current_page": 0,
        "total_pages": 0,
    }
    base.update(over)
    return base


def test_card_context_prefers_client_status_over_db():
    # DB still says watchlist, but the user has just moved the item to watching.
    # The insight must reflect what the user is looking at (watching), not the
    # not-yet-persisted DB row.
    db_media = _media(status="watchlist")
    ctx = app_module._card_context(db_media, {"status": "watching"})

    assert ctx["status"] == "watching"
    assert "watching" in app_module._ai_card_cache_key(ctx)
    assert app_module._ai_card_cache_key(ctx) != app_module._ai_card_cache_key(db_media)
    assert "currently watching" in app_module._ai_card_prompt(ctx).lower()


def test_card_context_ignores_invalid_status():
    db_media = _media(status="watchlist")
    ctx = app_module._card_context(db_media, {"status": "bogus"})
    assert ctx["status"] == "watchlist"


def test_card_context_falls_back_to_db_when_no_client_status():
    db_media = _media(status="finished")
    ctx = app_module._card_context(db_media, {})
    assert ctx["status"] == "finished"


def test_card_context_overrides_tv_progress():
    db_media = _media(media_type="tv", status="watching", last_season=1, last_episode=1)
    ctx = app_module._card_context(
        db_media, {"status": "watching", "last_season": 4, "last_episode": 10}
    )
    assert "S4E10" in app_module._ai_card_cache_key(ctx)


def test_card_context_does_not_mutate_input():
    db_media = _media(status="watchlist")
    app_module._card_context(db_media, {"status": "watching", "last_season": 3})
    assert db_media["status"] == "watchlist"
    assert db_media["last_season"] == 0
