"""Configuration, loaded from environment variables (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date as _date, time as _time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """Raised when the environment is missing or has malformed settings."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _parse_date(raw: str) -> _date:
    try:
        return _date.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError(f"DATE must be YYYY-MM-DD, got {raw!r}") from exc


def _parse_time(name: str, raw: str) -> _time:
    try:
        hh, mm = raw.split(":")
        return _time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{name} must be HH:MM (24-hour), got {raw!r}") from exc


def _split_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class Config:
    # --- What we are hunting for -------------------------------------------
    restaurant: str
    venue_slug: str
    target_date: _date
    party_size: int
    earliest_time: _time
    latest_time: _time
    venue_timezone: str

    # --- Polling behaviour --------------------------------------------------
    min_check_interval_seconds: int
    max_check_interval_seconds: int
    # Optional slower cadence outside the venue's waking hours. Cancellations
    # are made by people, so overnight polling is mostly wasted requests —
    # spending them during the day instead buys real coverage for free.
    active_hours_start: _time | None
    active_hours_end: _time | None
    offpeak_min_check_interval_seconds: int
    offpeak_max_check_interval_seconds: int
    request_timeout_seconds: int
    max_attempts_per_check: int
    stop_after_first_notification: bool

    # --- Availability semantics --------------------------------------------
    # SevenRooms marks each slot with a type. "book" is a real, bookable table.
    # "request" only lets you *ask* for a table, so it does not count.
    bookable_slot_types: tuple[str, ...]

    # --- Fallback -----------------------------------------------------------
    use_playwright_fallback: bool
    playwright_timeout_ms: int

    # --- Notifications ------------------------------------------------------
    notify_channels: tuple[str, ...]
    notify_email_to: str | None
    notify_email_from: str
    resend_api_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    pushover_token: str | None
    pushover_user_key: str | None
    error_notification_after_failures: int
    error_notification_cooldown_seconds: int
    # Periodic "still watching, nothing yet" message, so that silence is never
    # ambiguous. 0 disables it.
    heartbeat_hours: int

    # --- Plumbing -----------------------------------------------------------
    state_file: Path
    log_file: Path | None
    log_level: str
    user_agent: str
    health_port: int | None

    @property
    def booking_url(self) -> str:
        """Plain booking page for this venue."""
        return f"https://www.sevenrooms.com/reservations/{self.venue_slug}"

    def deep_link(self, slot_time: _time | None = None) -> str:
        """Booking page pre-filled with this date, party size and time.

        The widget honours all three of these, so the link lands on the search
        form already set up — one press of "Search" shows the slots. The date
        MUST be ISO (YYYY-MM-DD): the MM-DD-YYYY form is silently ignored and
        the calendar stays on the current month.
        """
        params = [
            f"default_date={self.target_date.isoformat()}",
            f"default_party_size={self.party_size}",
            f"default_time={(slot_time or self.earliest_time).strftime('%H:%M')}",
        ]
        return f"{self.booking_url}?{'&'.join(params)}"

    def validate(self) -> None:
        if self.party_size < 1:
            raise ConfigError("PARTY_SIZE must be at least 1")
        if self.earliest_time > self.latest_time:
            raise ConfigError("EARLIEST_TIME must not be after LATEST_TIME")
        if self.min_check_interval_seconds < 30:
            raise ConfigError(
                "MIN_CHECK_INTERVAL_SECONDS must be >= 30 to stay polite to the booking site"
            )
        if self.max_check_interval_seconds < self.min_check_interval_seconds:
            raise ConfigError(
                "MAX_CHECK_INTERVAL_SECONDS must be >= MIN_CHECK_INTERVAL_SECONDS"
            )
        if (self.active_hours_start is None) != (self.active_hours_end is None):
            raise ConfigError(
                "Set both ACTIVE_HOURS_START and ACTIVE_HOURS_END, or neither"
            )
        if self.offpeak_min_check_interval_seconds < 30:
            raise ConfigError("OFFPEAK_MIN_CHECK_INTERVAL_SECONDS must be >= 30")
        if self.offpeak_max_check_interval_seconds < self.offpeak_min_check_interval_seconds:
            raise ConfigError(
                "OFFPEAK_MAX_CHECK_INTERVAL_SECONDS must be >= OFFPEAK_MIN_CHECK_INTERVAL_SECONDS"
            )
        if not self.notify_channels:
            raise ConfigError(
                "No notification channel is configured. Set TELEGRAM_BOT_TOKEN + "
                "TELEGRAM_CHAT_ID, or RESEND_API_KEY + NOTIFY_EMAIL_TO, "
                "or PUSHOVER_TOKEN + PUSHOVER_USER_KEY."
            )

        # A channel selected but not credentialed is the worst possible bug: the
        # monitor looks healthy for days and then fails at the one moment it
        # matters. Catch it at startup instead.
        missing: dict[str, list[str]] = {
            "telegram": [
                name
                for name, value in (
                    ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
                    ("TELEGRAM_CHAT_ID", self.telegram_chat_id),
                )
                if not value
            ],
            "email": [
                name
                for name, value in (
                    ("RESEND_API_KEY", self.resend_api_key),
                    ("NOTIFY_EMAIL_TO", self.notify_email_to),
                )
                if not value
            ],
            "pushover": [
                name
                for name, value in (
                    ("PUSHOVER_TOKEN", self.pushover_token),
                    ("PUSHOVER_USER_KEY", self.pushover_user_key),
                )
                if not value
            ],
        }
        for channel in self.notify_channels:
            gaps = missing.get(channel)
            if gaps:
                hint = ""
                if channel == "telegram":
                    hint = "  Run `python -m laguerite.monitor --telegram-setup` for help."
                raise ConfigError(
                    f"NOTIFY_CHANNELS selects '{channel}' but {' and '.join(gaps)} "
                    f"{'is' if len(gaps) == 1 else 'are'} not set.{hint}"
                )


def _detect_channels(
    explicit: list[str],
    *,
    email_ready: bool,
    telegram_ready: bool,
    pushover_ready: bool,
) -> tuple[str, ...]:
    """Pick notification channels.

    If NOTIFY_CHANNELS is set we honour it verbatim. Otherwise we enable every
    channel that has complete credentials, in the user's preferred order.
    """
    if explicit:
        # "console" is a credential-free dry-run channel: it logs the message
        # instead of sending it. Useful for testing the pipeline.
        allowed = {"email", "telegram", "pushover", "console"}
        unknown = [c for c in explicit if c.lower() not in allowed]
        if unknown:
            raise ConfigError(
                f"NOTIFY_CHANNELS contains unknown channel(s): {', '.join(unknown)}. "
                f"Valid values: {', '.join(sorted(allowed))}"
            )
        return tuple(c.lower() for c in explicit)

    detected: list[str] = []
    if email_ready:
        detected.append("email")
    if telegram_ready:
        detected.append("telegram")
    if pushover_ready:
        detected.append("pushover")
    return tuple(detected)


def load_config() -> Config:
    resend_api_key = _env("RESEND_API_KEY")
    notify_email_to = _env("NOTIFY_EMAIL_TO")
    telegram_bot_token = _env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = _env("TELEGRAM_CHAT_ID")
    pushover_token = _env("PUSHOVER_TOKEN")
    pushover_user_key = _env("PUSHOVER_USER_KEY")

    channels = _detect_channels(
        _split_list(_env("NOTIFY_CHANNELS")),
        email_ready=bool(resend_api_key and notify_email_to),
        telegram_ready=bool(telegram_bot_token and telegram_chat_id),
        pushover_ready=bool(pushover_token and pushover_user_key),
    )

    state_file = Path(_env("STATE_FILE", str(PROJECT_ROOT / "state" / "state.json")))
    if not state_file.is_absolute():
        state_file = PROJECT_ROOT / state_file

    log_file_raw = _env("LOG_FILE", str(PROJECT_ROOT / "logs" / "monitor.log"))
    log_file: Path | None = None
    if log_file_raw and log_file_raw.lower() not in {"none", "off", "-"}:
        log_file = Path(log_file_raw)
        if not log_file.is_absolute():
            log_file = PROJECT_ROOT / log_file

    health_port_raw = _env("PORT") or _env("HEALTH_PORT")
    health_port = int(health_port_raw) if health_port_raw else None

    config = Config(
        restaurant=_env("RESTAURANT", "La Guerite Cannes"),
        venue_slug=_env("SEVENROOMS_VENUE_SLUG", "lagueritecannes"),
        target_date=_parse_date(_env("DATE", "2026-09-03")),
        party_size=_env_int("PARTY_SIZE", 6),
        earliest_time=_parse_time("EARLIEST_TIME", _env("EARLIEST_TIME", "14:00")),
        latest_time=_parse_time("LATEST_TIME", _env("LATEST_TIME", "16:00")),
        venue_timezone=_env("VENUE_TIMEZONE", "Europe/Paris"),
        min_check_interval_seconds=_env_int("MIN_CHECK_INTERVAL_SECONDS", 60),
        max_check_interval_seconds=_env_int("MAX_CHECK_INTERVAL_SECONDS", 300),
        active_hours_start=(
            _parse_time("ACTIVE_HOURS_START", _env("ACTIVE_HOURS_START"))
            if _env("ACTIVE_HOURS_START")
            else None
        ),
        active_hours_end=(
            _parse_time("ACTIVE_HOURS_END", _env("ACTIVE_HOURS_END"))
            if _env("ACTIVE_HOURS_END")
            else None
        ),
        offpeak_min_check_interval_seconds=_env_int("OFFPEAK_MIN_CHECK_INTERVAL_SECONDS", 300),
        offpeak_max_check_interval_seconds=_env_int("OFFPEAK_MAX_CHECK_INTERVAL_SECONDS", 900),
        request_timeout_seconds=_env_int("REQUEST_TIMEOUT_SECONDS", 30),
        max_attempts_per_check=_env_int("MAX_ATTEMPTS_PER_CHECK", 3),
        stop_after_first_notification=_env_bool("STOP_AFTER_FIRST_NOTIFICATION", False),
        bookable_slot_types=tuple(
            t.lower() for t in _split_list(_env("BOOKABLE_SLOT_TYPES", "book"))
        )
        or ("book",),
        use_playwright_fallback=_env_bool("USE_PLAYWRIGHT_FALLBACK", False),
        playwright_timeout_ms=_env_int("PLAYWRIGHT_TIMEOUT_MS", 45000),
        notify_channels=channels,
        notify_email_to=notify_email_to,
        notify_email_from=_env("NOTIFY_EMAIL_FROM", "onboarding@resend.dev"),
        resend_api_key=resend_api_key,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        pushover_token=pushover_token,
        pushover_user_key=pushover_user_key,
        error_notification_after_failures=_env_int("ERROR_NOTIFICATION_AFTER_FAILURES", 5),
        error_notification_cooldown_seconds=_env_int(
            "ERROR_NOTIFICATION_COOLDOWN_SECONDS", 3600
        ),
        heartbeat_hours=_env_int("HEARTBEAT_HOURS", 24),
        state_file=state_file,
        log_file=log_file,
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        user_agent=_env(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        ),
        health_port=health_port,
    )
    return config
