"""Test suite — run with:  python -m unittest discover -s tests -v

Uses only the standard library plus the project's own dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, time as _time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from laguerite.availability import CheckStatus, Slot, format_12h, parse_slot_time
from laguerite.config import Config, ConfigError, _detect_channels
from laguerite.notifier import Notifier
from laguerite.sevenrooms_api import SevenRoomsClient
from laguerite.state import MonitorState, load_state, save_state


def make_config(**overrides) -> Config:
    base = dict(
        restaurant="La Guerite Cannes",
        venue_slug="lagueritecannes",
        target_date=date(2026, 9, 3),
        party_size=6,
        earliest_time=_time(14, 0),
        latest_time=_time(16, 0),
        venue_timezone="Europe/Paris",
        min_check_interval_seconds=60,
        max_check_interval_seconds=300,
        active_hours_start=None,
        active_hours_end=None,
        offpeak_min_check_interval_seconds=300,
        offpeak_max_check_interval_seconds=900,
        request_timeout_seconds=10,
        max_attempts_per_check=1,
        stop_after_first_notification=False,
        bookable_slot_types=("book",),
        use_playwright_fallback=False,
        playwright_timeout_ms=45000,
        notify_channels=("console",),
        notify_email_to="someone@example.com",
        notify_email_from="alerts@example.com",
        resend_api_key="re_test_key",
        telegram_bot_token="123:ABC",
        telegram_chat_id="999",
        pushover_token="ptok",
        pushover_user_key="puser",
        error_notification_after_failures=5,
        error_notification_cooldown_seconds=3600,
        heartbeat_hours=24,
        state_file=Path("state/test.json"),
        log_file=None,
        log_level="INFO",
        user_agent="test-agent",
        health_port=None,
    )
    base.update(overrides)
    return Config(**base)


def payload(times, date_key="2026-09-03", closed=False):
    """Build a SevenRooms-shaped payload from (time, type) pairs."""
    return {
        "status": 200,
        "data": {
            "availability": {
                date_key: [
                    {
                        "name": "Day Lunch",
                        "shift_category": "LUNCH",
                        "is_closed": closed,
                        "times": [
                            {
                                "type": t,
                                "time": hhmm,
                                "time_iso": f"2026-09-03 {hhmm}:00",
                                "public_time_slot_description": "Sitting",
                            }
                            for hhmm, t in times
                        ],
                    }
                ]
            }
        },
    }


# --------------------------------------------------------------------------- #
class TestTimeParsing(unittest.TestCase):
    def test_24_hour(self):
        self.assertEqual(parse_slot_time("14:30"), _time(14, 30))

    def test_12_hour(self):
        self.assertEqual(parse_slot_time("2:30 PM"), _time(14, 30))
        self.assertEqual(parse_slot_time("2:30PM"), _time(14, 30))
        self.assertEqual(parse_slot_time("12:15 AM"), _time(0, 15))
        self.assertEqual(parse_slot_time("12:15 PM"), _time(12, 15))

    def test_iso_datetime(self):
        self.assertEqual(parse_slot_time("2026-09-03 14:30:00"), _time(14, 30))

    def test_garbage(self):
        for bad in (None, "", "   ", "not a time", "99:99"):
            self.assertIsNone(parse_slot_time(bad), bad)

    def test_format_12h(self):
        self.assertEqual(format_12h(_time(14, 30)), "2:30 PM")
        self.assertEqual(format_12h(_time(0, 5)), "12:05 AM")
        self.assertEqual(format_12h(_time(12, 0)), "12:00 PM")


# --------------------------------------------------------------------------- #
class TestAvailabilityParsing(unittest.TestCase):
    def setUp(self):
        self.client = SevenRoomsClient(make_config())

    def test_request_only_is_not_availability(self):
        """The real-world case: every slot in the window is request-only."""
        result = self.client.parse(
            payload([("14:00", "request"), ("14:30", "request"), ("15:00", "request")])
        )
        self.assertIs(result.status, CheckStatus.OK)
        self.assertEqual(result.qualifying, [])
        self.assertEqual(len(result.all_slots), 3)

    def test_bookable_in_window_qualifies(self):
        result = self.client.parse(
            payload([("13:45", "book"), ("14:30", "book"), ("15:00", "request")])
        )
        self.assertIs(result.status, CheckStatus.OK)
        self.assertEqual([s.key for s in result.qualifying], ["14:30"])

    def test_window_boundaries_are_inclusive(self):
        result = self.client.parse(
            payload([("14:00", "book"), ("16:00", "book"), ("16:15", "book")])
        )
        self.assertEqual([s.key for s in result.qualifying], ["14:00", "16:00"])

    def test_closed_shift_yields_nothing(self):
        result = self.client.parse(payload([("14:30", "book")], closed=True))
        self.assertIs(result.status, CheckStatus.OK)
        self.assertEqual(result.qualifying, [])

    def test_missing_date_is_no_availability_not_an_error(self):
        result = self.client.parse(payload([("14:30", "book")], date_key="2026-09-04"))
        self.assertIs(result.status, CheckStatus.OK)
        self.assertEqual(result.qualifying, [])

    def test_twelve_hour_venue_locale(self):
        data = payload([("2:30 PM", "book")])
        for entry in data["data"]["availability"]["2026-09-03"][0]["times"]:
            entry.pop("time_iso")
        result = self.client.parse(data)
        self.assertEqual([s.key for s in result.qualifying], ["14:30"])

    # ---- structural failures must be loud, not silent ---------------------
    def test_missing_data_object(self):
        result = self.client.parse({"status": 200})
        self.assertIs(result.status, CheckStatus.STRUCTURE_ERROR)

    def test_missing_availability_map(self):
        result = self.client.parse({"status": 200, "data": {}})
        self.assertIs(result.status, CheckStatus.STRUCTURE_ERROR)

    def test_unrecognised_slot_vocabulary(self):
        result = self.client.parse(payload([("14:30", "totally_new_type")]))
        self.assertIs(result.status, CheckStatus.STRUCTURE_ERROR)
        self.assertIn("unrecognised slot types", result.error)

    def test_unparsable_times(self):
        data = payload([("14:30", "book")])
        for entry in data["data"]["availability"]["2026-09-03"][0]["times"]:
            entry["time"] = "???"
            entry["time_iso"] = "???"
        result = self.client.parse(data)
        self.assertIs(result.status, CheckStatus.STRUCTURE_ERROR)

    def test_configurable_bookable_types(self):
        client = SevenRoomsClient(make_config(bookable_slot_types=("book", "request")))
        result = client.parse(payload([("14:30", "request")]))
        self.assertEqual([s.key for s in result.qualifying], ["14:30"])


# --------------------------------------------------------------------------- #
class TestState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_roundtrip(self):
        config = make_config()
        state = load_state(self.path, config)
        state.set_notified({"14:30", "15:00"})
        state.total_checks = 7
        save_state(self.path, state)

        reloaded = load_state(self.path, config)
        self.assertEqual(reloaded.notified, {"14:30", "15:00"})
        self.assertEqual(reloaded.total_checks, 7)

    def test_criteria_change_clears_memory(self):
        state = load_state(self.path, make_config())
        state.set_notified({"14:30"})
        save_state(self.path, state)

        reloaded = load_state(self.path, make_config(party_size=4))
        self.assertEqual(reloaded.notified, set())

    def test_corrupt_file_does_not_crash(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        state = load_state(self.path, make_config())
        self.assertEqual(state.notified, set())

    def test_unknown_future_fields_ignored(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"notified_times": ["14:30"], "brand_new": 1}), "utf-8")
        state = load_state(self.path, make_config())
        self.assertEqual(state.notified, {"14:30"})


# --------------------------------------------------------------------------- #
class _FakeAPIHandler(BaseHTTPRequestHandler):
    received: list[dict] = []
    status_to_return = 200

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"_raw": raw}
        type(self).received.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        code = type(self).status_to_return
        response = json.dumps({"id": "msg_123"} if code < 300 else {"error": "nope"}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *args):
        return


class _RewriteAdapter(HTTPAdapter):
    """Send real HTTP through the real requests stack, but to our local server."""

    def __init__(self, base: str):
        self.base = base
        super().__init__()

    def send(self, request, **kwargs):
        for host in ("https://api.resend.com", "https://api.telegram.org",
                     "https://api.pushover.net"):
            if request.url.startswith(host):
                request.url = self.base + request.url[len(host):]
        kwargs["verify"] = False
        return super().send(request, **kwargs)


class TestNotifierOverRealHTTP(unittest.TestCase):
    """Exercises the notifier end to end through actual HTTP requests."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAPIHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _notifier(self, channels):
        _FakeAPIHandler.received = []
        _FakeAPIHandler.status_to_return = 200
        session = requests.Session()
        adapter = _RewriteAdapter(self.base)
        for host in ("https://api.resend.com", "https://api.telegram.org",
                     "https://api.pushover.net"):
            session.mount(host, adapter)
        return Notifier(make_config(notify_channels=channels), session=session)

    def test_email_payload(self):
        notifier = self._notifier(("email",))
        slots = [Slot(_time(14, 30), "book"), Slot(_time(15, 0), "book")]
        outcome = notifier.notify_availability(slots)

        self.assertEqual(outcome.sent, ["email"], outcome.failed)
        self.assertEqual(len(_FakeAPIHandler.received), 1)
        request = _FakeAPIHandler.received[0]

        self.assertEqual(request["path"], "/emails")
        self.assertEqual(request["headers"]["Authorization"], "Bearer re_test_key")

        body = request["body"]
        self.assertEqual(body["from"], "alerts@example.com")
        self.assertEqual(body["to"], ["someone@example.com"])
        self.assertIn("2:30 PM", body["subject"])
        for fragment in ("La Guérite availability found!", "September", "Party: 6",
                         "2:30 PM", "3:00 PM", "sevenrooms.com/reservations/lagueritecannes"):
            self.assertIn(fragment, body["text"], fragment)
        self.assertIn("party_size=6", body["html"])

    def test_multiple_recipients(self):
        notifier = self._notifier(("email",))
        object.__setattr__(notifier.config, "notify_email_to", "a@x.com, b@y.com")
        notifier.notify_availability([Slot(_time(14, 30), "book")])
        self.assertEqual(_FakeAPIHandler.received[0]["body"]["to"], ["a@x.com", "b@y.com"])

    def test_telegram_payload(self):
        notifier = self._notifier(("telegram",))
        outcome = notifier.notify_availability([Slot(_time(14, 30), "book")])
        self.assertEqual(outcome.sent, ["telegram"], outcome.failed)
        request = _FakeAPIHandler.received[0]
        self.assertEqual(request["path"], "/bot123:ABC/sendMessage")
        self.assertEqual(request["body"]["chat_id"], "999")
        self.assertIn("2:30 PM", request["body"]["text"])

    def test_telegram_sends_no_parse_mode(self):
        """Markdown would mangle the underscores in the booking URL."""
        notifier = self._notifier(("telegram",))
        notifier.notify_availability([Slot(_time(14, 30), "book")])
        body = _FakeAPIHandler.received[0]["body"]
        self.assertNotIn("parse_mode", body)

    def test_telegram_url_survives_intact(self):
        notifier = self._notifier(("telegram",))
        notifier.notify_availability([Slot(_time(14, 30), "book")])
        text = _FakeAPIHandler.received[0]["body"]["text"]
        self.assertIn("default_party_size=6", text)
        self.assertIn("default_time=14:30", text)
        # No stray Markdown emphasis that Telegram could choke on.
        self.assertNotIn("*", text)

    def test_telegram_carries_every_qualifying_time(self):
        notifier = self._notifier(("telegram",))
        slots = [Slot(_time(14, 30), "book"), Slot(_time(15, 0), "book"),
                 Slot(_time(15, 45), "book")]
        notifier.notify_availability(slots)
        text = _FakeAPIHandler.received[0]["body"]["text"]
        for label in ("2:30 PM", "3:00 PM", "3:45 PM"):
            self.assertIn(label, text)

    def test_telegram_error_alert(self):
        notifier = self._notifier(("telegram",))
        outcome = notifier.notify_error("API shape changed", 6)
        self.assertEqual(outcome.sent, ["telegram"], outcome.failed)
        self.assertIn("API shape changed", _FakeAPIHandler.received[0]["body"]["text"])

    def test_telegram_bad_token_is_reported(self):
        notifier = self._notifier(("telegram",))
        _FakeAPIHandler.status_to_return = 401
        outcome = notifier.notify_availability([Slot(_time(14, 30), "book")])
        self.assertEqual(outcome.sent, [])
        self.assertIn("401", outcome.failed["telegram"])

    def test_pushover_payload(self):
        notifier = self._notifier(("pushover",))
        outcome = notifier.notify_availability([Slot(_time(14, 30), "book")])
        self.assertEqual(outcome.sent, ["pushover"], outcome.failed)
        body = _FakeAPIHandler.received[0]["body"]["_raw"]
        self.assertIn("ptok", body)
        self.assertIn("puser", body)

    def test_all_channels_at_once(self):
        notifier = self._notifier(("email", "telegram", "pushover"))
        outcome = notifier.notify_availability([Slot(_time(14, 30), "book")])
        self.assertEqual(outcome.sent, ["email", "telegram", "pushover"], outcome.failed)
        self.assertEqual(len(_FakeAPIHandler.received), 3)

    def test_api_rejection_is_reported_not_raised(self):
        notifier = self._notifier(("email",))
        _FakeAPIHandler.status_to_return = 422
        outcome = notifier.notify_availability([Slot(_time(14, 30), "book")])
        self.assertEqual(outcome.sent, [])
        self.assertIn("email", outcome.failed)
        self.assertIn("422", outcome.failed["email"])

    def test_one_channel_failing_does_not_block_others(self):
        notifier = self._notifier(("email", "telegram"))
        object.__setattr__(notifier.config, "resend_api_key", None)
        outcome = notifier.notify_availability([Slot(_time(14, 30), "book")])
        self.assertEqual(outcome.sent, ["telegram"])
        self.assertIn("email", outcome.failed)

    def test_error_notification(self):
        notifier = self._notifier(("email",))
        outcome = notifier.notify_error("API shape changed", 6)
        self.assertEqual(outcome.sent, ["email"], outcome.failed)
        body = _FakeAPIHandler.received[0]["body"]
        self.assertIn("cannot read availability", body["subject"])
        self.assertIn("API shape changed", body["text"])

    def test_test_notification(self):
        notifier = self._notifier(("email",))
        outcome = notifier.notify_test()
        self.assertEqual(outcome.sent, ["email"], outcome.failed)
        self.assertIn("test", _FakeAPIHandler.received[0]["body"]["subject"].lower())


# --------------------------------------------------------------------------- #
class TestConfig(unittest.TestCase):
    def test_channel_autodetect_prefers_email(self):
        self.assertEqual(
            _detect_channels([], email_ready=True, telegram_ready=True, pushover_ready=True),
            ("email", "telegram", "pushover"),
        )

    def test_channel_autodetect_skips_incomplete(self):
        self.assertEqual(
            _detect_channels([], email_ready=False, telegram_ready=True, pushover_ready=False),
            ("telegram",),
        )

    def test_unknown_channel_rejected(self):
        with self.assertRaises(ConfigError):
            _detect_channels(["carrier-pigeon"], email_ready=True,
                             telegram_ready=False, pushover_ready=False)

    def test_validate_rejects_backwards_window(self):
        with self.assertRaises(ConfigError):
            make_config(earliest_time=_time(16, 0), latest_time=_time(14, 0)).validate()

    def test_validate_rejects_hammering(self):
        with self.assertRaises(ConfigError):
            make_config(min_check_interval_seconds=5).validate()

    def test_validate_requires_a_channel(self):
        with self.assertRaises(ConfigError):
            make_config(notify_channels=()).validate()

    def test_selected_channel_must_have_credentials(self):
        """The nastiest failure mode: looks fine, then cannot reach you."""
        with self.assertRaises(ConfigError) as ctx:
            make_config(notify_channels=("telegram",), telegram_bot_token=None).validate()
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            make_config(notify_channels=("telegram",), telegram_chat_id=None).validate()
        self.assertIn("TELEGRAM_CHAT_ID", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            make_config(notify_channels=("email",), resend_api_key=None).validate()
        self.assertIn("RESEND_API_KEY", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            make_config(notify_channels=("pushover",), pushover_user_key=None).validate()
        self.assertIn("PUSHOVER_USER_KEY", str(ctx.exception))

    def test_fully_credentialed_channel_passes(self):
        make_config(notify_channels=("telegram",)).validate()

    def test_console_channel_needs_no_credentials(self):
        make_config(
            notify_channels=("console",),
            telegram_bot_token=None,
            resend_api_key=None,
            pushover_token=None,
        ).validate()

    def test_unused_channel_credentials_are_not_required(self):
        """Selecting telegram must not demand Resend keys."""
        make_config(
            notify_channels=("telegram",), resend_api_key=None, notify_email_to=None
        ).validate()

    def test_deep_link(self):
        config = make_config()
        link = config.deep_link(_time(14, 30))
        self.assertIn("lagueritecannes", link)
        self.assertIn("default_party_size=6", link)
        self.assertIn("default_time=14:30", link)
        # With no specific slot it falls back to the window start.
        self.assertIn("default_time=14:00", config.deep_link())

    def test_deep_link_date_must_be_iso(self):
        """MM-DD-YYYY is silently ignored by the widget; ISO is honoured."""
        link = make_config().deep_link()
        self.assertIn("default_date=2026-09-03", link)
        self.assertNotIn("09-03-2026", link)


# --------------------------------------------------------------------------- #
class TestTelegramSetupHelper(unittest.TestCase):
    """`--telegram-setup` turns a bot token into a ready-to-paste chat ID."""

    def _patch(self, getme, updates):
        from unittest import mock

        class R:
            def __init__(self, code, data):
                self.status_code = code
                self._data = data
                self.text = json.dumps(data)

            def json(self):
                return self._data

        def fake_get(url, **kwargs):
            return R(*(getme if url.endswith("/getMe") else updates))

        return mock.patch("laguerite.notifier.requests.get", fake_get)

    def test_finds_chat_id(self):
        from laguerite.notifier import telegram_discover

        updates = {
            "result": [
                {"message": {"chat": {"id": 987654321, "first_name": "Dan",
                                      "username": "dan", "type": "private"}}}
            ]
        }
        with self._patch((200, {"result": {"username": "laguerite_bot"}}), (200, updates)):
            username, chats, error = telegram_discover("123:ABC")
        self.assertIsNone(error)
        self.assertEqual(username, "laguerite_bot")
        self.assertEqual(chats, [("987654321", "dan")])

    def test_deduplicates_repeated_chats(self):
        from laguerite.notifier import telegram_discover

        one = {"message": {"chat": {"id": 5, "first_name": "Dan", "type": "private"}}}
        with self._patch((200, {"result": {"username": "b"}}), (200, {"result": [one, one, one]})):
            _, chats, error = telegram_discover("123:ABC")
        self.assertIsNone(error)
        self.assertEqual(len(chats), 1)

    def test_bad_token_reported_clearly(self):
        from laguerite.notifier import telegram_discover

        with self._patch((401, {"description": "Unauthorized"}), (200, {"result": []})):
            username, chats, error = telegram_discover("bad")
        self.assertIsNone(username)
        self.assertIn("401", error)

    def test_no_messages_yet(self):
        from laguerite.notifier import telegram_discover

        with self._patch((200, {"result": {"username": "b"}}), (200, {"result": []})):
            username, chats, error = telegram_discover("123:ABC")
        self.assertEqual(username, "b")
        self.assertEqual(chats, [])
        self.assertIsNone(error)

    def test_group_chat_title_used(self):
        from laguerite.notifier import telegram_discover

        updates = {"result": [{"message": {"chat": {"id": -100123, "title": "Cannes trip",
                                                    "type": "group"}}}]}
        with self._patch((200, {"result": {"username": "b"}}), (200, updates)):
            _, chats, _ = telegram_discover("123:ABC")
        self.assertEqual(chats, [("-100123", "Cannes trip")])


# --------------------------------------------------------------------------- #
class TestEnvFileWriter(unittest.TestCase):
    """--telegram-setup edits .env in place; it must never mangle it."""

    def setUp(self):
        from laguerite.monitor import _update_env_file

        self.write = _update_env_file
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / ".env"

    def tearDown(self):
        self.dir.cleanup()

    def test_fills_blank_keys(self):
        self.path.write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n", encoding="utf-8")
        self.write(self.path, {"TELEGRAM_BOT_TOKEN": "123:ABC", "TELEGRAM_CHAT_ID": "999"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_BOT_TOKEN=123:ABC", text)
        self.assertIn("TELEGRAM_CHAT_ID=999", text)

    def test_preserves_comments_and_other_settings(self):
        original = (
            "# My notes\n"
            "DATE=2026-09-03\n"
            "\n"
            "# Telegram block\n"
            "TELEGRAM_BOT_TOKEN=\n"
            "PARTY_SIZE=6\n"
        )
        self.path.write_text(original, encoding="utf-8")
        self.write(self.path, {"TELEGRAM_BOT_TOKEN": "123:ABC"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# My notes", text)
        self.assertIn("# Telegram block", text)
        self.assertIn("DATE=2026-09-03", text)
        self.assertIn("PARTY_SIZE=6", text)
        self.assertIn("TELEGRAM_BOT_TOKEN=123:ABC", text)

    def test_appends_missing_keys(self):
        self.path.write_text("DATE=2026-09-03\n", encoding="utf-8")
        self.write(self.path, {"TELEGRAM_CHAT_ID": "999"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("DATE=2026-09-03", text)
        self.assertIn("TELEGRAM_CHAT_ID=999", text)

    def test_overwrites_a_stale_value(self):
        self.path.write_text("TELEGRAM_CHAT_ID=old\n", encoding="utf-8")
        self.write(self.path, {"TELEGRAM_CHAT_ID": "new"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_CHAT_ID=new", text)
        self.assertNotIn("old", text)

    def test_does_not_touch_commented_out_keys(self):
        self.path.write_text("# TELEGRAM_CHAT_ID=example\nDATE=2026-09-03\n", encoding="utf-8")
        self.write(self.path, {"TELEGRAM_CHAT_ID": "999"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# TELEGRAM_CHAT_ID=example", text)
        self.assertIn("TELEGRAM_CHAT_ID=999", text)

    def test_reports_what_changed(self):
        self.path.write_text("TELEGRAM_CHAT_ID=999\n", encoding="utf-8")
        changed = self.write(self.path, {"TELEGRAM_CHAT_ID": "999"})
        self.assertEqual(changed, [], "an unchanged value should not be reported")

    def test_creates_the_file_when_absent(self):
        changed = self.write(self.path, {"TELEGRAM_CHAT_ID": "999"})
        self.assertEqual(changed, ["TELEGRAM_CHAT_ID"])
        self.assertIn("TELEGRAM_CHAT_ID=999", self.path.read_text(encoding="utf-8"))

    def test_result_is_loadable_by_dotenv(self):
        from dotenv import dotenv_values

        self.path.write_text("# c\nDATE=2026-09-03\nTELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
        self.write(self.path, {"TELEGRAM_BOT_TOKEN": "123:ABC", "NOTIFY_CHANNELS": "telegram"})
        values = dotenv_values(self.path)
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "123:ABC")
        self.assertEqual(values["NOTIFY_CHANNELS"], "telegram")
        self.assertEqual(values["DATE"], "2026-09-03")


# --------------------------------------------------------------------------- #
class TestBrowserHelpers(unittest.TestCase):
    def test_month_header_english(self):
        from laguerite.browser_check import _parse_month_header

        self.assertEqual(_parse_month_header("September 2026"), (2026, 9))
        self.assertEqual(_parse_month_header("  august 2026 "), (2026, 8))

    def test_month_header_french(self):
        from laguerite.browser_check import _parse_month_header

        self.assertEqual(_parse_month_header("septembre 2026"), (2026, 9))
        self.assertEqual(_parse_month_header("décembre 2026"), (2026, 12))

    def test_month_header_unreadable(self):
        from laguerite.browser_check import _parse_month_header

        for bad in ("", "Date", "2026", "Blurgh 2026"):
            self.assertIsNone(_parse_month_header(bad), bad)


# --------------------------------------------------------------------------- #
class _StubClient:
    """Feeds the Monitor a scripted sequence of CheckResults."""

    def __init__(self, results):
        self.results = list(results)

    def check(self):
        return self.results.pop(0) if self.results else self.results[-1]


class _RecordingNotifier:
    def __init__(self):
        self.availability = []
        self.errors = []

    def notify_availability(self, slots):
        from laguerite.notifier import NotifyOutcome

        self.availability.append([s.key for s in slots])
        return NotifyOutcome(sent=["email"], failed={})

    def notify_error(self, reason, failures):
        from laguerite.notifier import NotifyOutcome

        self.errors.append((reason, failures))
        return NotifyOutcome(sent=["email"], failed={})


class TestMonitorBehaviour(unittest.TestCase):
    def setUp(self):
        from laguerite.availability import CheckResult
        from laguerite.monitor import Monitor

        self.CheckResult = CheckResult
        self.dir = tempfile.TemporaryDirectory()
        config = make_config(
            state_file=Path(self.dir.name) / "s.json",
            error_notification_after_failures=3,
            error_notification_cooldown_seconds=3600,
        )
        self.monitor = Monitor.__new__(Monitor)
        self.monitor.config = config
        self.monitor.state = load_state(config.state_file, config)
        self.monitor.notifier = _RecordingNotifier()
        self.monitor._stopping = False
        self.monitor._wake = None

    def tearDown(self):
        self.dir.cleanup()

    def _ok(self, keys):
        return self.CheckResult(
            status=CheckStatus.OK,
            qualifying=[Slot(_time(int(k[:2]), int(k[3:])), "book") for k in keys],
        )

    def _fail(self, status, error="boom"):
        return self.CheckResult(status=status, error=error)

    def test_notifies_once_then_stays_quiet(self):
        self.monitor.handle_result(self._ok(["14:30"]))
        self.monitor.handle_result(self._ok(["14:30"]))
        self.assertEqual(self.monitor.notifier.availability, [["14:30"]])

    def test_new_time_notifies_only_the_new_one(self):
        self.monitor.handle_result(self._ok(["14:30"]))
        self.monitor.handle_result(self._ok(["14:30", "15:00"]))
        self.assertEqual(self.monitor.notifier.availability, [["14:30"], ["15:00"]])

    def test_disappear_then_return_renotifies(self):
        self.monitor.handle_result(self._ok(["14:30"]))
        self.monitor.handle_result(self._ok([]))
        self.monitor.handle_result(self._ok(["14:30"]))
        self.assertEqual(self.monitor.notifier.availability, [["14:30"], ["14:30"]])

    def test_failed_send_is_retried_next_check(self):
        from laguerite.notifier import NotifyOutcome

        self.monitor.notifier.notify_availability = lambda slots: NotifyOutcome(
            sent=[], failed={"email": "smtp down"}
        )
        self.monitor.handle_result(self._ok(["14:30"]))
        self.assertEqual(self.monitor.state.notified, set())

        # Send works the second time round -> we still get told.
        self.monitor.notifier = _RecordingNotifier()
        self.monitor.handle_result(self._ok(["14:30"]))
        self.assertEqual(self.monitor.notifier.availability, [["14:30"]])

    def test_transient_errors_escalate_only_after_threshold(self):
        for _ in range(2):
            self.monitor.handle_result(self._fail(CheckStatus.TRANSIENT_ERROR))
        self.assertEqual(self.monitor.notifier.errors, [])

        self.monitor.handle_result(self._fail(CheckStatus.TRANSIENT_ERROR))
        self.assertEqual(len(self.monitor.notifier.errors), 1)

    def test_structure_error_escalates_quickly_but_not_instantly(self):
        self.monitor.handle_result(self._fail(CheckStatus.STRUCTURE_ERROR, "shape changed"))
        self.assertEqual(self.monitor.notifier.errors, [])

        self.monitor.handle_result(self._fail(CheckStatus.STRUCTURE_ERROR, "shape changed"))
        self.assertEqual(len(self.monitor.notifier.errors), 1)
        self.assertIn("shape changed", self.monitor.notifier.errors[0][0])

    def test_error_notifications_are_rate_limited(self):
        for _ in range(10):
            self.monitor.handle_result(self._fail(CheckStatus.STRUCTURE_ERROR))
        self.assertEqual(len(self.monitor.notifier.errors), 1)

    def test_recovery_resets_the_failure_counter(self):
        self.monitor.handle_result(self._fail(CheckStatus.TRANSIENT_ERROR))
        self.monitor.handle_result(self._fail(CheckStatus.TRANSIENT_ERROR))
        self.monitor.handle_result(self._ok([]))
        self.assertEqual(self.monitor.state.consecutive_failures, 0)

    def test_failure_does_not_erase_remembered_slots(self):
        self.monitor.handle_result(self._ok(["14:30"]))
        self.monitor.handle_result(self._fail(CheckStatus.TRANSIENT_ERROR))
        self.assertEqual(self.monitor.state.notified, {"14:30"})
        # ...and no duplicate alert once the site comes back.
        self.monitor.handle_result(self._ok(["14:30"]))
        self.assertEqual(self.monitor.notifier.availability, [["14:30"]])

    # ---- heartbeat --------------------------------------------------------
    def _hb_notifier(self):
        class N(_RecordingNotifier):
            beats = []

            def notify_heartbeat(self, state, summary):
                from laguerite.notifier import NotifyOutcome

                N.beats.append(summary)
                return NotifyOutcome(sent=["telegram"], failed={})

        N.beats = []
        self.monitor.notifier = N()
        return N

    def test_first_call_starts_the_clock_without_sending(self):
        N = self._hb_notifier()
        self.monitor._maybe_heartbeat(self._ok([]))
        self.assertEqual(N.beats, [])
        self.assertIsNotNone(self.monitor.state.last_heartbeat_at)

    def test_heartbeat_fires_once_due(self):
        import time as _t

        N = self._hb_notifier()
        self.monitor._maybe_heartbeat(self._ok([]))
        self.monitor.state.last_heartbeat_at = _t.time() - 25 * 3600
        self.monitor._maybe_heartbeat(self._ok([]))
        self.assertEqual(len(N.beats), 1)
        self.assertIn("No availability", N.beats[0])

    def test_heartbeat_not_sent_early(self):
        import time as _t

        N = self._hb_notifier()
        self.monitor.state.last_heartbeat_at = _t.time() - 3600
        self.monitor._maybe_heartbeat(self._ok([]))
        self.assertEqual(N.beats, [])

    def test_heartbeat_disabled(self):
        import time as _t

        N = self._hb_notifier()
        object.__setattr__(self.monitor.config, "heartbeat_hours", 0)
        self.monitor.state.last_heartbeat_at = _t.time() - 99 * 3600
        self.monitor._maybe_heartbeat(self._ok([]))
        self.assertEqual(N.beats, [])

    def test_heartbeat_reports_a_failed_check(self):
        import time as _t

        N = self._hb_notifier()
        self.monitor.state.last_heartbeat_at = _t.time() - 25 * 3600
        self.monitor._maybe_heartbeat(None)
        self.assertEqual(N.beats, ["check did not complete"])

    def test_stop_after_first_notification(self):
        object.__setattr__(self.monitor.config, "stop_after_first_notification", True)
        self.assertFalse(self.monitor.handle_result(self._ok([])))
        self.assertTrue(self.monitor.handle_result(self._ok(["14:30"])))
        self.assertTrue(self.monitor.state.booked)

    def test_interval_stays_within_configured_bounds(self):
        for _ in range(200):
            delay = self.monitor.next_interval()
            self.assertGreaterEqual(delay, 60)
            self.assertLessEqual(delay, 300)

    def test_interval_is_actually_randomised(self):
        values = {self.monitor.next_interval() for _ in range(100)}
        self.assertGreater(len(values), 10, "intervals should not be a fixed cadence")

    # ---- active-hours pacing ---------------------------------------------
    def _paced(self, start, end):
        cfg = make_config(
            state_file=self.monitor.config.state_file,
            min_check_interval_seconds=45,
            max_check_interval_seconds=120,
            active_hours_start=start,
            active_hours_end=end,
            offpeak_min_check_interval_seconds=300,
            offpeak_max_check_interval_seconds=900,
        )
        object.__setattr__(self.monitor, "config", cfg)
        return cfg

    def test_no_active_hours_means_always_active(self):
        self._paced(None, None)
        self.assertTrue(self.monitor.in_active_hours(_time(3, 0)))

    def test_daytime_window(self):
        self._paced(_time(8, 0), _time(23, 0))
        self.assertTrue(self.monitor.in_active_hours(_time(14, 0)))
        self.assertTrue(self.monitor.in_active_hours(_time(8, 0)))
        self.assertTrue(self.monitor.in_active_hours(_time(23, 0)))
        self.assertFalse(self.monitor.in_active_hours(_time(3, 0)))
        self.assertFalse(self.monitor.in_active_hours(_time(23, 30)))

    def test_window_wrapping_midnight(self):
        self._paced(_time(20, 0), _time(2, 0))
        self.assertTrue(self.monitor.in_active_hours(_time(23, 0)))
        self.assertTrue(self.monitor.in_active_hours(_time(1, 0)))
        self.assertFalse(self.monitor.in_active_hours(_time(12, 0)))

    def test_offpeak_uses_the_slower_range(self):
        cfg = self._paced(_time(8, 0), _time(23, 0))
        # Force off-peak by pinning the clock check.
        self.monitor.in_active_hours = lambda now=None: False
        for _ in range(100):
            delay = self.monitor.next_interval()
            self.assertGreaterEqual(delay, cfg.offpeak_min_check_interval_seconds)
            self.assertLessEqual(delay, cfg.offpeak_max_check_interval_seconds)

    def test_active_uses_the_faster_range(self):
        cfg = self._paced(_time(8, 0), _time(23, 0))
        self.monitor.in_active_hours = lambda now=None: True
        for _ in range(100):
            delay = self.monitor.next_interval()
            self.assertGreaterEqual(delay, cfg.min_check_interval_seconds)
            self.assertLessEqual(delay, cfg.max_check_interval_seconds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
