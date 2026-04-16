"""
Regression tests for update_user_settings_all_sessions.

Pins the bug where toggling one setting (e.g. dry_run) in the no-session
state silently reset every *other* previously-saved preference (e.g.
use_undo_window) to its hardcoded default. The response from PUT
/settings was a fresh copy of DEFAULT_SETTINGS with only the one
changed field overlaid — which the frontend then merged into UI state
(making toggles mysteriously flip) and the endpoint wrote back to
Supabase (silently corrupting the user's stored prefs).

Run:  cd backend && python -m pytest tests/test_settings_update.py -v
"""

import pytest

from app import trader
from app.market_selector import DEFAULT_SETTINGS
from app.trader import update_user_settings_all_sessions


@pytest.fixture(autouse=True)
def isolate_sessions():
    """Make sure no leftover sessions from other tests leak in. All tests
    in this module exercise the no-session branch."""
    with trader._registry_lock:
        snapshot = dict(trader._sessions)
        trader._sessions.clear()
    yield
    with trader._registry_lock:
        trader._sessions.clear()
        trader._sessions.update(snapshot)


class TestNoSessionMergesSavedPrefs:
    def test_saved_pref_preserved_when_toggling_another_setting(self):
        """Pinned bug: user had use_undo_window=False saved, then toggled
        dry_run off. Before the fix, the response flipped use_undo_window
        back to True (its default)."""
        saved = {"use_undo_window": False}
        result = update_user_settings_all_sessions(
            "user-a", saved_prefs=saved, dry_run=False,
        )
        assert result["dry_run"] is False
        assert result["use_undo_window"] is False, (
            "saved use_undo_window=False must survive an unrelated change"
        )

    def test_multiple_saved_prefs_preserved(self):
        saved = {
            "use_undo_window": False,
            "use_stop_loss": False,
            "blowout_filter": False,
            "alpha": 0.9,
            "max_dollars": 250.0,
        }
        result = update_user_settings_all_sessions(
            "user-b", saved_prefs=saved, dry_run=False,
        )
        for k, v in saved.items():
            assert result[k] == v, f"{k} should remain {v!r}, got {result[k]!r}"
        assert result["dry_run"] is False

    def test_change_wins_over_saved(self):
        """If the user is actively turning undo_window back on, the new
        value must beat the saved one."""
        saved = {"use_undo_window": False}
        result = update_user_settings_all_sessions(
            "user-c", saved_prefs=saved, use_undo_window=True,
        )
        assert result["use_undo_window"] is True

    def test_no_saved_prefs_falls_back_to_defaults(self):
        """Brand-new user with nothing saved should still get sensible
        defaults + their change."""
        result = update_user_settings_all_sessions(
            "user-d", saved_prefs=None, dry_run=False,
        )
        assert result["dry_run"] is False
        # Every other key matches the frozen defaults.
        for k, v in DEFAULT_SETTINGS.items():
            if k == "dry_run":
                continue
            assert result[k] == v

    def test_empty_saved_prefs_falls_back_to_defaults(self):
        result = update_user_settings_all_sessions(
            "user-e", saved_prefs={}, dry_run=False,
        )
        for k, v in DEFAULT_SETTINGS.items():
            if k == "dry_run":
                continue
            assert result[k] == v

    def test_unknown_keys_in_saved_prefs_are_dropped(self):
        """Saved prefs came through Supabase which could theoretically
        have stale columns. The whitelist + clamp_settings keeps them
        from leaking into the returned dict."""
        saved = {
            "use_undo_window": False,
            "legacy_feature": True,    # not in whitelist
            "_internal_flag": "x",     # not in whitelist
        }
        result = update_user_settings_all_sessions(
            "user-f", saved_prefs=saved, dry_run=False,
        )
        assert "legacy_feature" not in result
        assert "_internal_flag" not in result
        assert result["use_undo_window"] is False

    def test_saved_prefs_are_clamped(self):
        """Corrupt / out-of-range saved values don't slip through."""
        saved = {"alpha": 5.0, "max_dollars": -100.0}
        result = update_user_settings_all_sessions(
            "user-g", saved_prefs=saved, dry_run=False,
        )
        assert 0.0 <= result["alpha"] <= 1.0
        assert result["max_dollars"] >= 1.0


class TestSaveUserSettingsUpsert:
    """Guards the UPDATE → UPSERT fix in `db.save_user_settings`.

    The original bug: `.update({"settings": ...}).eq("id", user_id)` affects
    0 rows when the `public.users` row doesn't exist (legacy users who
    pre-date the `handle_new_auth_user` trigger). The save silently no-ops,
    `get_user_settings` returns `{}`, and every new Session seeds from
    `DEFAULT_SETTINGS` — surfacing as "I toggled a setting but the backend
    ignored it." These tests pin the call shape so a future revert back to
    UPDATE would fail loudly at test time."""

    def _install_fake_client(self, monkeypatch):
        """Mock the Supabase client's chained `.table().upsert().execute()`
        sequence with call-recording fakes so tests can assert exactly how
        `save_user_settings` interacts with the SDK."""
        from unittest.mock import MagicMock
        from app import database as db

        table_mock = MagicMock()
        # `execute()` on the upsert chain returns an object with `.data`;
        # upsert normally echoes the row back, which our logging inspects.
        table_mock.upsert.return_value.execute.return_value = MagicMock(
            data=[{"id": "user-upsert", "settings": {}}]
        )
        client_mock = MagicMock()
        client_mock.table.return_value = table_mock

        monkeypatch.setattr(db, "_get_client", lambda: client_mock)
        return client_mock, table_mock

    def test_uses_upsert_not_update(self, monkeypatch):
        """Primary regression guard: a revert to `.update(...).eq(...)`
        would not exercise `.upsert(...)` and this assertion would fail."""
        from app import database as db

        _, table_mock = self._install_fake_client(monkeypatch)

        db.save_user_settings("user-upsert", {"use_undo_window": False})

        table_mock.upsert.assert_called_once()
        # `.update()` must NOT have been called — otherwise the legacy
        # silent-0-row path is back.
        assert not table_mock.update.called, (
            "save_user_settings reverted to UPDATE — legacy users "
            "without a public.users row will silently lose their saves"
        )

    def test_upsert_payload_and_conflict_key(self, monkeypatch):
        """The `public.users.id` PK must be in the payload and the
        conflict target must be `id`. Missing either breaks the upsert."""
        from app import database as db

        _, table_mock = self._install_fake_client(monkeypatch)

        db.save_user_settings("user-upsert", {"alpha": 0.65, "dry_run": False})

        args, kwargs = table_mock.upsert.call_args
        payload = args[0] if args else kwargs.get("json") or kwargs
        # Row body must carry both the PK and the settings jsonb.
        assert payload.get("id") == "user-upsert", (
            "upsert payload missing id — UPSERT would insert with a null "
            "PK and fail the FK to auth.users"
        )
        assert payload.get("settings") == {"alpha": 0.65, "dry_run": False}
        # on_conflict tells Supabase which unique constraint to merge on.
        assert kwargs.get("on_conflict") == "id", (
            f"expected on_conflict='id', got kwargs={kwargs}"
        )

    def test_no_client_is_a_silent_noop(self, monkeypatch):
        """`database.py`'s never-raise contract: if the service client
        isn't configured, save_user_settings returns without raising and
        without hitting any fake call path."""
        from app import database as db

        monkeypatch.setattr(db, "_get_client", lambda: None)

        # Must not raise — the endpoint handler calls this in a
        # fire-and-forget thread; an exception here would abort the
        # response path the user already got back.
        db.save_user_settings("user-upsert", {"alpha": 0.65})

    def test_empty_user_id_is_a_silent_noop(self, monkeypatch):
        """Second never-raise path: empty user_id never reaches the SDK
        (which would error on `.eq("id", "")` or `.upsert({"id": ""})`)."""
        from app import database as db

        _, table_mock = self._install_fake_client(monkeypatch)

        db.save_user_settings("", {"alpha": 0.65})

        assert not table_mock.upsert.called


class TestSessionBranchUnchanged:
    """When the user HAS an active session, the saved_prefs arg must not
    matter — session.settings is already seeded from DB at creation and
    is the source of truth."""

    def test_existing_session_ignores_saved_prefs(self):
        from app.trader import Session
        import threading
        s = Session.__new__(Session)
        s.user_id = "user-with-session"
        s.game_id = 42
        s._lock = threading.Lock()
        # Session already has use_undo_window=False from its DB load
        s.settings = {
            **DEFAULT_SETTINGS,
            "use_undo_window": False,
            "alpha": 0.7,
        }
        with trader._registry_lock:
            trader._sessions[(s.user_id, s.game_id)] = s

        # Pass conflicting saved_prefs — session path must ignore them.
        result = update_user_settings_all_sessions(
            s.user_id,
            saved_prefs={"use_undo_window": True, "alpha": 0.3},
            dry_run=False,
        )
        assert result["dry_run"] is False
        # Session's existing values win, not saved_prefs.
        assert result["use_undo_window"] is False
        assert result["alpha"] == 0.7
