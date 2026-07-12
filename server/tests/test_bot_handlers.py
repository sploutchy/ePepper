"""Tests for bot/handlers.py: the Telegram interface's pure formatting
helpers, the access-control gate (`_is_allowed`), and the async command /
callback handlers driven with lightweight fake Update/CallbackQuery
objects instead of real python-telegram-bot wire objects.

`ALLOWED_USERS` / `WEB_URL` / `BACKUP_CHAT_ID` are bound as plain values
in bot.handlers' module namespace at import time (`from config import
...`), so tests control access by monkeypatching those names directly on
the `handlers` module rather than on `config`.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot.handlers as handlers


def _fake_update(user_id):
    message = SimpleNamespace(reply_text=AsyncMock())
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return SimpleNamespace(effective_user=user, message=message)


def _fake_callback_update(user_id, data):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    update = SimpleNamespace(effective_user=user, callback_query=query)
    return update, query


# ---------------------------------------------------------------------------
# _is_allowed / _alert_recipients
# ---------------------------------------------------------------------------

def test_is_allowed_denies_all_when_empty(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [])
    assert handlers._is_allowed(123) is False


def test_is_allowed_permits_listed_user_only(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42, 99])
    assert handlers._is_allowed(42) is True
    assert handlers._is_allowed(99) is True
    assert handlers._is_allowed(1) is False


def test_alert_recipients_prefers_allowed_users(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [1, 2])
    monkeypatch.setattr(handlers, "BACKUP_CHAT_ID", 999)
    assert handlers._alert_recipients() == [1, 2]


def test_alert_recipients_falls_back_to_backup_chat(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [])
    monkeypatch.setattr(handlers, "BACKUP_CHAT_ID", 999)
    assert handlers._alert_recipients() == [999]


def test_alert_recipients_empty_when_neither_configured(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [])
    monkeypatch.setattr(handlers, "BACKUP_CHAT_ID", None)
    assert handlers._alert_recipients() == []


# ---------------------------------------------------------------------------
# _stash_pending / _stash_search — bounded LRU eviction
# ---------------------------------------------------------------------------

def test_stash_pending_evicts_oldest_beyond_max():
    handlers._pending.clear()
    tokens = [
        handlers._stash_pending(f"url{i}", {"title": str(i)})
        for i in range(handlers._PENDING_MAX + 5)
    ]
    assert len(handlers._pending) == handlers._PENDING_MAX
    for token in tokens[:5]:
        assert token not in handlers._pending
    for token in tokens[5:]:
        assert token in handlers._pending


def test_stash_search_evicts_oldest_beyond_max():
    handlers._search_queries.clear()
    tokens = [
        handlers._stash_search(f"query{i}")
        for i in range(handlers._SEARCH_QUERIES_MAX + 3)
    ]
    assert len(handlers._search_queries) == handlers._SEARCH_QUERIES_MAX
    for token in tokens[:3]:
        assert token not in handlers._search_queries
    for token in tokens[3:]:
        assert token in handlers._search_queries


# ---------------------------------------------------------------------------
# Pure formatting helpers
# ---------------------------------------------------------------------------

def test_web_app_line_empty_when_unset(monkeypatch):
    monkeypatch.setattr(handlers, "WEB_URL", "")
    assert handlers._web_app_line() == ""


def test_web_app_line_contains_url(monkeypatch):
    monkeypatch.setattr(handlers, "WEB_URL", "https://epepper.example")
    assert "https://epepper.example/app/" in handlers._web_app_line()


def test_format_tomorrow_anniversary_multi_year():
    preview = {
        "anniversary": {"id": 1, "title": "Tarte", "url": None},
        "anniversary_years_ago": 2,
        "fooby": None,
    }
    text = handlers._format_tomorrow_html(preview)
    assert "Tarte" in text
    assert "cooked 2 years ago" in text


def test_format_tomorrow_anniversary_singular_last_year():
    preview = {
        "anniversary": {"id": 1, "title": "Soup", "url": None},
        "anniversary_years_ago": 1,
        "fooby": None,
    }
    assert "cooked last year" in handlers._format_tomorrow_html(preview)


def test_format_tomorrow_fooby_fallback():
    preview = {
        "anniversary": None,
        "fooby": {"title": "Quiche", "url": "https://fooby.ch/quiche"},
    }
    text = handlers._format_tomorrow_html(preview)
    assert "Quiche" in text
    assert "Fooby" in text


def test_format_tomorrow_nothing_scheduled():
    preview = {"anniversary": None, "fooby": None}
    assert "No past cook lands" in handlers._format_tomorrow_html(preview)


def test_cooked_label_never_cooked():
    assert handlers._cooked_label({"last_displayed_at": None, "tags": []}) == "never cooked"


def test_cooked_label_with_tags(monkeypatch):
    monkeypatch.setattr(handlers, "humanize_date", lambda ts: "yesterday")
    label = handlers._cooked_label({"last_displayed_at": 123, "tags": ["fast", "veg"]})
    assert label == "cooked yesterday (fast, veg)"


def test_format_source_html_none_for_missing_url():
    assert handlers._format_source_html(None) == ""


def test_format_source_html_links_http_urls():
    out = handlers._format_source_html("https://fooby.ch/recipe/1")
    assert 'href="https://fooby.ch/recipe/1"' in out
    assert "Fooby" in out


def test_format_source_html_plain_for_non_http():
    out = handlers._format_source_html("cookbook://ottolenghi/simple")
    assert "<a href" not in out
    assert "Ottolenghi" in out


def test_format_push_reply_single_page_has_no_page_count():
    body = handlers._format_push_reply("Tarte", "https://fooby.ch/tarte", 1)
    assert "Tarte" in body
    assert "pages)" not in body


def test_format_push_reply_multi_page_shows_count():
    body = handlers._format_push_reply("Tarte", None, 3)
    assert "(3 pages)" in body


def test_push_inline_actions_pending_only(monkeypatch):
    monkeypatch.setattr(handlers, "WEB_URL", "")
    markup = handlers._push_inline_actions(recipe_id=None, pending_token="abc123")
    assert markup.inline_keyboard[0][0].callback_data == "save:abc123"


def test_push_inline_actions_saved_with_web_url(monkeypatch):
    monkeypatch.setattr(handlers, "WEB_URL", "https://epepper.example")
    markup = handlers._push_inline_actions(recipe_id=5, pending_token=None)
    button = markup.inline_keyboard[0][0]
    assert button.url == "https://epepper.example/app/recipes/5"


def test_push_inline_actions_none_when_nothing_to_show(monkeypatch):
    monkeypatch.setattr(handlers, "WEB_URL", "")
    assert handlers._push_inline_actions(recipe_id=5, pending_token=None) is None


# ---------------------------------------------------------------------------
# _render_search_page
# ---------------------------------------------------------------------------

def test_render_search_page_no_matches_returns_none(monkeypatch):
    monkeypatch.setattr(handlers.library, "search", lambda query, limit, offset: [])
    assert handlers._render_search_page("nope", 0) is None


def test_render_search_page_builds_body_and_push_buttons(monkeypatch):
    rows = [
        {"id": i, "title": f"Recipe {i}", "url": None, "last_displayed_at": None, "tags": []}
        for i in range(1, 4)
    ]
    monkeypatch.setattr(handlers.library, "search", lambda query, limit, offset: rows)
    rendered = handlers._render_search_page("soup", 0)
    assert rendered is not None
    body, keyboard = rendered
    assert "Recipe 1" in body
    push_row = keyboard.inline_keyboard[0]
    assert [b.callback_data for b in push_row] == ["push:1", "push:2", "push:3"]


def test_render_search_page_has_more_shows_next_button_only(monkeypatch):
    rows = [
        {"id": i, "title": f"R{i}", "url": None, "last_displayed_at": None, "tags": []}
        for i in range(1, handlers._SEARCH_PAGE_SIZE + 2)
    ]
    monkeypatch.setattr(handlers.library, "search", lambda query, limit, offset: rows)
    _, keyboard = handlers._render_search_page("soup", 0)
    nav_labels = [b.text for b in keyboard.inline_keyboard[-1]]
    assert "Next »" in nav_labels
    assert "« Prev" not in nav_labels


def test_render_search_page_walking_off_the_end_shows_prev_and_notice(monkeypatch):
    monkeypatch.setattr(handlers.library, "search", lambda query, limit, offset: [])
    rendered = handlers._render_search_page("soup", handlers._SEARCH_PAGE_SIZE)
    assert rendered is not None
    body, keyboard = rendered
    assert "No more matches." in body
    nav_labels = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "« Prev" in nav_labels


# ---------------------------------------------------------------------------
# _build_status_text
# ---------------------------------------------------------------------------

def test_build_status_text_idle_never_seen(monkeypatch):
    monkeypatch.setattr(
        handlers.display_state, "get",
        lambda: {"title": "", "type": "idle", "total_pages": 1, "url": None},
    )
    monkeypatch.setattr(handlers.device_telemetry, "get_device_status", lambda: {"last_seen": None})
    monkeypatch.setattr(handlers.library, "count_saved", lambda: 0)
    monkeypatch.setattr(handlers.backup, "is_enabled", lambda: False)
    monkeypatch.setattr(handlers, "tomorrow_preview", lambda: {"anniversary": None, "fooby": None})

    text = handlers._build_status_text()
    assert "idle" in text
    assert "0 saved recipes" in text
    assert "never seen" in text
    assert "Last backup" not in text


def test_build_status_text_full_snapshot(monkeypatch):
    monkeypatch.setattr(
        handlers.display_state, "get",
        lambda: {
            "title": "Tarte aux pommes", "type": "recipe",
            "total_pages": 3, "url": "https://fooby.ch/tarte",
        },
    )
    now = int(time.time())
    monkeypatch.setattr(
        handlers.device_telemetry, "get_device_status",
        lambda: {
            "last_seen": now - handlers.device_telemetry.STALE_HEARTBEAT_S - 10,
            "battery_mv": 3400,
            "rssi": -75,
            "temperature_c": 21.4,
            "humidity_pct": 55,
            "firmware_version": 3,
        },
    )
    monkeypatch.setattr(handlers, "get_firmware_server_version", lambda: 4)
    monkeypatch.setattr(handlers.library, "count_saved", lambda: 12)
    monkeypatch.setattr(handlers.backup, "is_enabled", lambda: True)
    monkeypatch.setattr(handlers.backup, "get_last_backup_at", lambda: now - 3600)
    monkeypatch.setattr(handlers, "tomorrow_preview", lambda: {"anniversary": None, "fooby": None})

    text = handlers._build_status_text()
    assert "Tarte aux pommes" in text
    assert "(3 pages)" in text
    assert "❗ overdue" in text
    assert "Battery: <b>low</b>" in text
    assert "Signal: <b>weak</b>" in text
    assert "Temp: <b>21.4 °C</b>" in text
    assert "Humidity: <b>55%</b>" in text
    assert "update pending" in text
    assert "v3 → v4" in text


# ---------------------------------------------------------------------------
# notify_low_battery
# ---------------------------------------------------------------------------

def test_notify_low_battery_noop_before_bot_initialised(monkeypatch):
    monkeypatch.setattr(handlers, "_bot_app", None)
    asyncio.run(handlers.notify_low_battery(3000))  # must not raise


def test_notify_low_battery_sends_to_all_recipients(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [1, 2])
    monkeypatch.setattr(handlers, "BACKUP_CHAT_ID", None)
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

    monkeypatch.setattr(handlers, "_bot_app", SimpleNamespace(bot=FakeBot()))
    asyncio.run(handlers.notify_low_battery(3200))
    assert [chat_id for chat_id, _ in sent] == [1, 2]
    assert "battery is low" in sent[0][1]


def test_notify_low_battery_continues_after_one_recipient_fails(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [1, 2])
    sent = []

    class FakeBot:
        async def send_message(self, chat_id, text):
            if chat_id == 1:
                raise RuntimeError("blocked by user")
            sent.append(chat_id)

    monkeypatch.setattr(handlers, "_bot_app", SimpleNamespace(bot=FakeBot()))
    asyncio.run(handlers.notify_low_battery(3200))
    assert sent == [2]


# ---------------------------------------------------------------------------
# Command handler access gating
# ---------------------------------------------------------------------------

def test_cmd_help_denies_unlisted_user(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    update = _fake_update(1)
    asyncio.run(handlers.cmd_help(update, None))
    update.message.reply_text.assert_not_called()


def test_cmd_help_replies_for_allowed_user(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    monkeypatch.setattr(handlers, "WEB_URL", "")
    update = _fake_update(42)
    asyncio.run(handlers.cmd_help(update, None))
    update.message.reply_text.assert_awaited_once()
    body = update.message.reply_text.call_args.args[0]
    assert "ePepper" in body


def test_cmd_clear_denies_unauthorized(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    cleared = []
    monkeypatch.setattr(handlers.display_state, "clear", lambda: cleared.append(True))
    update = _fake_update(1)
    asyncio.run(handlers.cmd_clear(update, None))
    assert cleared == []
    update.message.reply_text.assert_not_called()


def test_cmd_clear_clears_display_and_replies(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    cleared = []
    monkeypatch.setattr(handlers.display_state, "clear", lambda: cleared.append(True))
    update = _fake_update(42)
    asyncio.run(handlers.cmd_clear(update, None))
    assert cleared == [True]
    update.message.reply_text.assert_awaited_once()


def test_on_unknown_command_replies_when_allowed(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    update = _fake_update(42)
    asyncio.run(handlers.on_unknown_command(update, None))
    update.message.reply_text.assert_awaited_once_with("Unknown command — try /help.")


# ---------------------------------------------------------------------------
# on_save_button
# ---------------------------------------------------------------------------

def test_on_save_button_denies_unauthorized(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    update, query = _fake_callback_update(1, "save:abc123")
    asyncio.run(handlers.on_save_button(update, None))
    query.answer.assert_awaited_once_with("Not authorized.", show_alert=True)


def test_on_save_button_expired_token(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    update, query = _fake_callback_update(42, "save:doesnotexist")
    asyncio.run(handlers.on_save_button(update, None))
    query.answer.assert_awaited_once_with(
        "Session expired — paste the link again to save it.", show_alert=True,
    )


def test_on_save_button_persists_pending_recipe(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    monkeypatch.setattr(handlers, "WEB_URL", "")
    handlers._pending.clear()
    token = handlers._stash_pending("https://fooby.ch/x", {"title": "Tarte"})

    async def fake_persist(url, recipe):
        assert url == "https://fooby.ch/x"
        return (99, ["dessert"])
    monkeypatch.setattr(handlers, "persist_recipe", fake_persist)

    touched = []
    monkeypatch.setattr(handlers.library, "touch_displayed", lambda rid: touched.append(rid))
    monkeypatch.setattr(handlers.display_state, "get", lambda: {"total_pages": 1})

    update, query = _fake_callback_update(42, f"save:{token}")
    asyncio.run(handlers.on_save_button(update, None))

    assert touched == [99]
    assert token not in handlers._pending
    query.answer.assert_awaited_once_with("Saved")
    body = query.edit_message_text.call_args.args[0]
    assert "Tarte" in body
    assert "dessert" in body


# ---------------------------------------------------------------------------
# on_push_button
# ---------------------------------------------------------------------------

def test_on_push_button_denies_unauthorized(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    update, query = _fake_callback_update(1, "push:5")
    asyncio.run(handlers.on_push_button(update, None))
    query.answer.assert_awaited_once_with("Not authorized.", show_alert=True)


def test_on_push_button_malformed_data(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    update, query = _fake_callback_update(42, "push:notanumber")
    asyncio.run(handlers.on_push_button(update, None))
    query.answer.assert_awaited_once_with("That didn't go through — try again.", show_alert=True)


def test_on_push_button_missing_recipe(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    monkeypatch.setattr(handlers.library, "get_recipe", lambda rid: None)
    update, query = _fake_callback_update(42, "push:999")
    asyncio.run(handlers.on_push_button(update, None))
    query.answer.assert_awaited_once_with("Recipe missing — was it deleted?", show_alert=True)


def test_on_push_button_render_failure(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    monkeypatch.setattr(handlers.library, "get_recipe", lambda rid: {"id": 5, "title": "X", "url": None})
    monkeypatch.setattr(handlers, "push_recipe_to_display", lambda r: False)
    update, query = _fake_callback_update(42, "push:5")
    asyncio.run(handlers.on_push_button(update, None))
    query.answer.assert_awaited_once_with("Couldn't render that recipe to the display.", show_alert=True)


def test_on_push_button_pushes_recipe(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    row = {"id": 5, "title": "Soup", "url": None}
    monkeypatch.setattr(handlers.library, "get_recipe", lambda rid: row if rid == 5 else None)
    pushed = []
    monkeypatch.setattr(handlers, "push_recipe_to_display", lambda r: pushed.append(r) or True)
    monkeypatch.setattr(handlers.display_state, "get", lambda: {"total_pages": 2})

    update, query = _fake_callback_update(42, "push:5")
    asyncio.run(handlers.on_push_button(update, None))

    assert pushed == [row]
    query.answer.assert_awaited_once_with("Pushed: Soup")
    body = query.edit_message_text.call_args.args[0]
    assert "Soup" in body
    assert "2 pages" in body


# ---------------------------------------------------------------------------
# on_search_nav
# ---------------------------------------------------------------------------

def test_on_search_nav_denies_unauthorized(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    update, query = _fake_callback_update(1, "search:abc123:0")
    asyncio.run(handlers.on_search_nav(update, None))
    query.answer.assert_awaited_once_with("Not authorized.", show_alert=True)


def test_on_search_nav_expired_session(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    update, query = _fake_callback_update(42, "search:zzzzzz:0")
    asyncio.run(handlers.on_search_nav(update, None))
    query.answer.assert_awaited_once_with(
        "Search session expired — re-run /search.", show_alert=True,
    )


def test_on_search_nav_renders_next_page(monkeypatch):
    monkeypatch.setattr(handlers, "ALLOWED_USERS", [42])
    handlers._search_queries.clear()
    token = handlers._stash_search("soup")
    rows = [
        {"id": i, "title": f"R{i}", "url": None, "last_displayed_at": None, "tags": []}
        for i in range(1, 3)
    ]
    monkeypatch.setattr(handlers.library, "search", lambda query, limit, offset: rows)

    update, query = _fake_callback_update(42, f"search:{token}:0")
    asyncio.run(handlers.on_search_nav(update, None))

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
