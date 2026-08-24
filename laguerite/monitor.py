"""Main loop: check, compare against remembered state, notify, sleep, repeat."""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import time
from dataclasses import asdict
from datetime import datetime, time as _time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .availability import CheckResult, CheckStatus, Slot, parse_slot_time
from .config import PROJECT_ROOT, Config, ConfigError, load_config
from .health import start_health_server
from .logging_setup import setup_logging
from .notifier import Notifier
from .sevenrooms_api import SevenRoomsClient
from .state import MonitorState, load_state, save_state

logger = logging.getLogger("laguerite")

# How long `--telegram-setup` waits for you to message the bot.
TELEGRAM_WAIT_SECONDS = 120


class Monitor:
    def __init__(self, config: Config):
        self.config = config
        self.client = SevenRoomsClient(config)
        self.notifier = Notifier(config)
        self.state = load_state(config.state_file, config)
        self._stopping = False
        self._wake = None  # set to a threading.Event in run()

    # ------------------------------------------------------------------ util

    def venue_now(self) -> str:
        try:
            tz = ZoneInfo(self.config.venue_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ""
        return datetime.now(tz).strftime("%H:%M %Z")

    def request_stop(self, *_args) -> None:
        if self._stopping:
            return
        self._stopping = True
        logger.info("Stop requested — shutting down after the current step")
        if self._wake is not None:
            self._wake.set()

    # ----------------------------------------------------------------- check

    def perform_check(self) -> CheckResult:
        """Run the API check, falling back to Playwright when configured."""
        result = self.client.check()

        if result.status is CheckStatus.OK or not self.config.use_playwright_fallback:
            return result

        logger.info(
            "Primary API check unusable (%s) — trying the Playwright fallback", result.error
        )
        try:
            from .browser_check import PlaywrightUnavailable, check_with_browser

            fallback = check_with_browser(self.config)
        except Exception as exc:
            logger.warning("Playwright fallback unavailable: %s", exc)
            return result

        if fallback.status is CheckStatus.OK:
            return fallback
        # Keep whichever error is more informative.
        return fallback if result.status is CheckStatus.TRANSIENT_ERROR else result

    # ---------------------------------------------------------------- notify

    def handle_result(self, result: CheckResult) -> bool:
        """Log the check, notify if warranted, persist state.

        Returns True if the monitor should stop (booked).
        """
        cfg = self.config
        state = self.state
        state.total_checks += 1
        state.last_check_at = datetime.now().isoformat(timespec="seconds")

        # ---------------- failures -----------------
        if result.status is not CheckStatus.OK:
            state.consecutive_failures += 1
            loaded = "page loaded but unreadable" if result.page_loaded else "page did NOT load"
            logger.warning(
                "Check failed (%s, attempt streak %d) — %s",
                loaded,
                state.consecutive_failures,
                result.error,
            )
            self._maybe_notify_error(result)
            save_state(cfg.state_file, state)
            return False

        state.consecutive_failures = 0
        state.last_success_at = datetime.now().isoformat(timespec="seconds")

        current = result.qualifying_keys
        previously = state.notified
        new_keys = current - previously
        gone = previously - current

        # A slot that vanished is forgotten, so it can alert again if it returns.
        if gone:
            logger.info(
                "No longer available: %s (will alert again if they come back)",
                ", ".join(sorted(gone)),
            )

        notified_note = ""
        if new_keys:
            new_slots = sorted(s for s in result.qualifying if s.key in new_keys)
            outcome = self.notifier.notify_availability(new_slots)
            if outcome.any_sent:
                state.total_notifications += 1
                # Only remember what we actually managed to deliver, so a failed
                # send is retried on the next check instead of being swallowed.
                state.set_notified((previously & current) | new_keys)
                notified_note = f" — Notification sent ({outcome.describe()})"
                if cfg.stop_after_first_notification:
                    state.booked = True
            else:
                state.set_notified(previously & current)
                notified_note = f" — NOTIFICATION FAILED ({outcome.describe()}); will retry"
        else:
            state.set_notified(previously & current)
            if current:
                notified_note = " — already notified"

        if result.qualifying:
            logger.info(
                "AVAILABLE: %s%s",
                ", ".join(s.label for s in result.qualifying),
                notified_note,
            )
        else:
            detail = ""
            if result.all_slots:
                request_only = [s for s in result.all_slots if s.slot_type != "book"]
                in_window = [
                    s
                    for s in request_only
                    if cfg.earliest_time <= s.time <= cfg.latest_time
                ]
                if in_window:
                    detail = (
                        f" ({len(in_window)} time(s) in window are request-only, not bookable)"
                    )
            logger.info("Checked successfully — No availability%s [%s]", detail, result.source)

        save_state(cfg.state_file, state)
        return bool(state.booked)

    def _maybe_heartbeat(self, result: CheckResult | None) -> None:
        """Send an occasional 'still watching' message.

        Without this, no news is indistinguishable from a dead process.
        """
        cfg, state = self.config, self.state
        if cfg.heartbeat_hours <= 0:
            return

        now = time.time()
        interval = cfg.heartbeat_hours * 3600
        if state.last_heartbeat_at is None:
            # Start the clock at first run rather than firing immediately.
            state.last_heartbeat_at = now
            save_state(cfg.state_file, state)
            return
        if now - state.last_heartbeat_at < interval:
            return

        summary = result.summary() if result is not None else "check did not complete"
        outcome = self.notifier.notify_heartbeat(state, summary)
        if outcome.any_sent:
            state.last_heartbeat_at = now
            logger.info("Heartbeat sent (%s)", outcome.describe())
        else:
            # Don't retry in a tight loop if the channel is down.
            state.last_heartbeat_at = now - interval + 600
            logger.warning("Heartbeat failed (%s)", outcome.describe())
        save_state(cfg.state_file, state)

    def _maybe_notify_error(self, result: CheckResult) -> None:
        cfg, state = self.config, self.state
        if result.status is not CheckStatus.STRUCTURE_ERROR:
            # Plain network trouble only escalates once it becomes persistent.
            if state.consecutive_failures < cfg.error_notification_after_failures:
                return
        elif state.consecutive_failures < 2:
            # Even a structural problem gets one free retry, to ride out a blip.
            return

        now = time.time()
        last = state.last_error_notified_at
        if last is not None and now - last < cfg.error_notification_cooldown_seconds:
            return

        reason = result.error or "unknown"
        outcome = self.notifier.notify_error(reason, state.consecutive_failures)
        if outcome.any_sent:
            state.last_error_notified_at = now
            logger.warning("Error notification sent (%s)", outcome.describe())
        else:
            logger.error("Could not send error notification (%s)", outcome.describe())

    # ------------------------------------------------------------------- run

    def in_active_hours(self, now: _time | None = None) -> bool:
        """Is it currently inside the venue's active window?

        Always True when ACTIVE_HOURS_* is unset. Handles windows that wrap past
        midnight (e.g. 08:00-02:00).
        """
        cfg = self.config
        if cfg.active_hours_start is None or cfg.active_hours_end is None:
            return True
        if now is None:
            try:
                now = datetime.now(ZoneInfo(cfg.venue_timezone)).time()
            except (ZoneInfoNotFoundError, ValueError):
                return True
        start, end = cfg.active_hours_start, cfg.active_hours_end
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end   # window wraps midnight

    def next_interval(self) -> int:
        cfg = self.config
        if self.in_active_hours():
            return random.randint(
                cfg.min_check_interval_seconds, cfg.max_check_interval_seconds
            )
        return random.randint(
            cfg.offpeak_min_check_interval_seconds,
            cfg.offpeak_max_check_interval_seconds,
        )

    def run(self) -> int:
        import threading

        cfg = self.config
        self._wake = threading.Event()

        if self.state.booked:
            logger.info(
                "State says this search already completed (STOP_AFTER_FIRST_NOTIFICATION). "
                "Delete %s to start hunting again.",
                cfg.state_file,
            )
            return 0

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError):  # not on the main thread / unsupported
                pass

        if cfg.health_port:
            try:
                start_health_server(cfg.health_port, lambda: self.state)
            except OSError as exc:
                logger.warning("Could not start health endpoint on %s: %s", cfg.health_port, exc)

        self._log_banner()

        while not self._stopping:
            result: CheckResult | None = None
            try:
                result = self.perform_check()
                should_stop = self.handle_result(result)
            except Exception:
                # Never let an unexpected error kill the monitor.
                self.state.consecutive_failures += 1
                logger.exception("Unexpected error during check — continuing")
                try:
                    save_state(cfg.state_file, self.state)
                except Exception:
                    logger.exception("Could not persist state")
                should_stop = False

            self._maybe_heartbeat(result)

            if should_stop:
                logger.info("STOP_AFTER_FIRST_NOTIFICATION is set — monitor stopping. Go book!")
                return 0
            if self._stopping:
                break

            delay = self.next_interval()
            logger.info("Next check in %d min %02d sec", delay // 60, delay % 60)
            self._wake.wait(delay)
            self._wake.clear()

        logger.info(
            "Monitor stopped after %d checks (%d notifications).",
            self.state.total_checks,
            self.state.total_notifications,
        )
        return 0

    def _log_banner(self) -> None:
        cfg = self.config
        logger.info("=" * 62)
        logger.info("Monitoring %s", cfg.restaurant)
        logger.info("  Date         : %s", cfg.target_date.strftime("%A, %B %d, %Y"))
        logger.info("  Party size   : %d", cfg.party_size)
        logger.info(
            "  Time window  : %s–%s (%s, currently %s)",
            cfg.earliest_time.strftime("%H:%M"),
            cfg.latest_time.strftime("%H:%M"),
            cfg.venue_timezone,
            self.venue_now() or "unknown",
        )
        logger.info(
            "  Check every  : %d–%d s (random)",
            cfg.min_check_interval_seconds,
            cfg.max_check_interval_seconds,
        )
        if cfg.active_hours_start and cfg.active_hours_end:
            logger.info(
                "    active hours: %s–%s %s (currently %s)",
                cfg.active_hours_start.strftime("%H:%M"),
                cfg.active_hours_end.strftime("%H:%M"),
                cfg.venue_timezone,
                "ACTIVE" if self.in_active_hours() else "off-peak",
            )
            logger.info(
                "    off-peak    : %d–%d s (random)",
                cfg.offpeak_min_check_interval_seconds,
                cfg.offpeak_max_check_interval_seconds,
            )
        logger.info("  Notify via   : %s", ", ".join(cfg.notify_channels))
        logger.info("  Counts as available: slot type(s) %s", ", ".join(cfg.bookable_slot_types))
        logger.info("  Booking page : %s", cfg.deep_link())
        logger.info("  State file   : %s", cfg.state_file)
        if self.state.notified_times:
            logger.info("  Already alerted about: %s", ", ".join(self.state.notified_times))
        logger.info("=" * 62)


# --------------------------------------------------------------------- CLI


def _simulate(monitor: Monitor, raw: str) -> int:
    """Inject fake availability through the real detect → dedupe → notify path."""
    times: list[Slot] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parsed = parse_slot_time(chunk)
        if parsed is None:
            logger.error("Could not parse simulated time %r (use e.g. 14:30 or '2:30 PM')", chunk)
            return 2
        times.append(Slot(time=parsed, slot_type="book", shift_name="SIMULATED"))

    if not times:
        logger.error("No simulated times given")
        return 2

    cfg = monitor.config
    out_of_window = [s for s in times if not (cfg.earliest_time <= s.time <= cfg.latest_time)]
    if out_of_window:
        logger.warning(
            "These simulated times fall outside %s–%s and would NOT qualify in real life: %s",
            cfg.earliest_time.strftime("%H:%M"),
            cfg.latest_time.strftime("%H:%M"),
            ", ".join(s.label for s in out_of_window),
        )

    qualifying = sorted(s for s in times if cfg.earliest_time <= s.time <= cfg.latest_time)
    logger.info("SIMULATION — pretending the site returned bookable slots")
    result = CheckResult(
        status=CheckStatus.OK,
        source="simulated",
        qualifying=qualifying,
        all_slots=sorted(times),
    )
    monitor.handle_result(result)
    logger.info(
        "Simulation complete. Remembered times are now: %s",
        ", ".join(monitor.state.notified_times) or "(none)",
    )
    logger.info(
        "Run the same command again to confirm it does NOT re-notify "
        "(that is the duplicate-suppression working)."
    )
    return 0


def _update_env_file(path, updates: dict[str, str]) -> list[str]:
    """Set KEY=value lines in a .env file, preserving everything else.

    Returns the keys that were changed. Existing comments and ordering survive;
    keys that are not present yet are appended.
    """
    from pathlib import Path

    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    remaining = dict(updates)
    changed: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            new_value = remaining.pop(key)
            if line != f"{key}={new_value}":
                lines[index] = f"{key}={new_value}"
                changed.append(key)

    for key, value in remaining.items():
        lines.append(f"{key}={value}")
        changed.append(key)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def _telegram_setup(token: str | None) -> int:
    """Walk the user through finding their Telegram chat ID."""
    from .notifier import telegram_discover

    if not token:
        logger.error("No bot token. Either set TELEGRAM_BOT_TOKEN in .env, or run:")
        logger.error("    python -m laguerite.monitor --telegram-setup 123456:ABC-your-token")
        logger.error("")
        logger.error("To create a bot: open Telegram, message @BotFather, send /newbot,")
        logger.error("pick a name, and copy the token it gives you.")
        return 2

    # First pass: validate the token and pick up anything already queued.
    username, chats, error = telegram_discover(token)
    if username:
        logger.info("Bot token is valid — you are talking to @%s", username)
    if error:
        logger.error("%s", error)
        return 1

    # Nothing queued: wait, so the user can send a message right now. Telegram
    # only holds a message until it is consumed, so an older conversation can
    # leave nothing to find — a fresh message always works.
    if not chats:
        logger.info("")
        logger.info("No messages are queued for this bot yet.")
        logger.info("")
        logger.info("  >> In Telegram, open @%s and send it any message now. <<", username)
        logger.info("")
        logger.info("Waiting up to %d seconds...", TELEGRAM_WAIT_SECONDS)
        username, chats, error = telegram_discover(
            token, wait_seconds=TELEGRAM_WAIT_SECONDS
        )
        if error:
            logger.error("%s", error)
            return 1

    if not chats:
        logger.error("")
        logger.error("Still nothing received from @%s.", username)
        logger.error("Send the bot a message in Telegram, then run this again:")
        logger.error("    python -m laguerite.monitor --telegram-setup")
        return 1

    logger.info("")
    logger.info("Found %d chat(s) that have messaged this bot:", len(chats))
    for chat_id, name in chats:
        logger.info("    %-16s (%s)", chat_id, name)

    chat_id = chats[0][0]
    env_path = PROJECT_ROOT / ".env"
    try:
        changed = _update_env_file(
            env_path,
            {
                "TELEGRAM_BOT_TOKEN": token,
                "TELEGRAM_CHAT_ID": chat_id,
                "NOTIFY_CHANNELS": "telegram",
            },
        )
    except OSError as exc:
        logger.error("Could not write %s: %s", env_path, exc)
        logger.error("Add these lines yourself:")
        logger.error("    TELEGRAM_BOT_TOKEN=%s", token)
        logger.error("    TELEGRAM_CHAT_ID=%s", chat_id)
        return 1

    logger.info("")
    if changed:
        logger.info("Saved to %s (updated: %s)", env_path, ", ".join(changed))
    else:
        logger.info("%s was already up to date", env_path)
    logger.info("Using chat %s (%s).", chat_id, chats[0][1])
    if len(chats) > 1:
        logger.info("To use a different one, edit TELEGRAM_CHAT_ID in .env.")
    logger.info("")
    logger.info("You're done. Now run:")
    logger.info("    python -m laguerite.monitor --test-notification")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m laguerite.monitor",
        description="Monitor La Guérite Cannes for a bookable reservation.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="run a single check and exit")
    group.add_argument(
        "--test-notification",
        action="store_true",
        help="send a test notification to every configured channel and exit",
    )
    group.add_argument(
        "--simulate",
        metavar="TIMES",
        help="pretend these times are bookable, e.g. --simulate '14:30,15:00'",
    )
    group.add_argument(
        "--status", action="store_true", help="is the monitor alive? (checks recency of last check)"
    )
    group.add_argument("--show-state", action="store_true", help="print the saved state and exit")
    group.add_argument("--reset-state", action="store_true", help="forget all remembered slots")
    group.add_argument(
        "--telegram-setup",
        nargs="?",
        const="",
        metavar="BOT_TOKEN",
        help="look up your Telegram chat ID (uses TELEGRAM_BOT_TOKEN if no token given)",
    )
    group.add_argument(
        "--check-config", action="store_true", help="validate configuration and exit"
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="force the Playwright browser check instead of the direct API",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        setup_logging("INFO", None)
        logger.error("Configuration error: %s", exc)
        return 2

    setup_logging(config.log_level, config.log_file)

    if args.telegram_setup is not None:
        return _telegram_setup(args.telegram_setup or config.telegram_bot_token)

    # These only touch local state, so they must work before notifications are
    # configured. Everything else needs a channel that can actually reach you.
    if args.status or args.show_state or args.reset_state:
        # These only read/write the local state file, so they must keep working
        # before (or independently of) any notification setup.
        object.__setattr__(config, "notify_channels", ("console",))

    try:
        config.validate()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        logger.error("Copy .env.example to .env and fill it in, then try again.")
        logger.error("For Telegram, run:  python -m laguerite.monitor --telegram-setup")
        return 2

    if args.check_config:
        logger.info("Configuration OK.")
        for key, value in asdict(config).items():
            if any(secret in key for secret in ("key", "token", "password")):
                value = "***set***" if value else "(unset)"
            logger.info("  %-34s %s", key, value)
        return 0

    monitor = Monitor(config)

    if args.status:
        state = monitor.state
        if not state.last_check_at:
            logger.warning("NOT RUNNING — no check has ever completed.")
            logger.info("Start it with:  python -m laguerite.monitor")
            return 1
        last = datetime.fromisoformat(state.last_check_at)
        age = (datetime.now() - last).total_seconds()
        # Anything older than a few max-intervals means it is not ticking.
        stale_after = max(600, config.max_check_interval_seconds * 3)
        logger.info("Last check   : %s (%.0f min ago)", state.last_check_at, age / 60)
        logger.info("Checks run   : %d", state.total_checks)
        logger.info("Alerts sent  : %d", state.total_notifications)
        logger.info("Watching for : %s, %d guests, %s-%s",
                    config.target_date.isoformat(), config.party_size,
                    config.earliest_time.strftime("%H:%M"),
                    config.latest_time.strftime("%H:%M"))
        if state.notified_times:
            logger.info("Currently available: %s", ", ".join(state.notified_times))
        if age > stale_after:
            logger.warning("")
            logger.warning("STALE — the monitor does not appear to be running.")
            logger.warning("Start it with:  python -m laguerite.monitor")
            return 1
        logger.info("")
        logger.info("ALIVE — the monitor is running and checking.")
        return 0

    if args.show_state:
        logger.info("State file: %s", config.state_file)
        for key, value in asdict(monitor.state).items():
            logger.info("  %-28s %s", key, value)
        return 0

    if args.reset_state:
        monitor.state = MonitorState(criteria_fingerprint=monitor.state.criteria_fingerprint)
        save_state(config.state_file, monitor.state)
        logger.info("State reset — the next matching slot will notify you again.")
        return 0

    if args.test_notification:
        logger.info("Sending a test notification via: %s", ", ".join(config.notify_channels))
        outcome = monitor.notifier.notify_test()
        logger.info("Result: %s", outcome.describe())
        return 0 if outcome.any_sent else 1

    if args.simulate:
        return _simulate(monitor, args.simulate)

    if args.browser:
        object.__setattr__(config, "use_playwright_fallback", True)
        from .browser_check import check_with_browser

        result = check_with_browser(config)
        logger.info("Browser check [%s] — %s", result.source, result.summary())
        if result.all_slots:
            logger.info(
                "  all slots seen: %s",
                ", ".join(f"{s.label}({s.slot_type})" for s in result.all_slots),
            )
        if args.once:
            return 0 if result.status is CheckStatus.OK else 1
        monitor.handle_result(result)
        return 0

    if args.once:
        result = monitor.perform_check()
        if result.all_slots:
            logger.info(
                "All slots the venue returned: %s",
                ", ".join(f"{s.label}({s.slot_type})" for s in result.all_slots),
            )
        monitor.handle_result(result)
        # --once is how scheduled runners (cron, GitHub Actions) drive the
        # monitor, so the heartbeat has to live here too, not only in run().
        monitor._maybe_heartbeat(result)
        return 0 if result.status is CheckStatus.OK else 1

    return monitor.run()


if __name__ == "__main__":
    sys.exit(main())
