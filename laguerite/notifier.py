"""Notifications: Resend email first, then Telegram, then Pushover."""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from .availability import Slot
from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class NotifyOutcome:
    sent: list[str]
    failed: dict[str, str]

    @property
    def any_sent(self) -> bool:
        return bool(self.sent)

    def describe(self) -> str:
        parts = []
        if self.sent:
            parts.append("sent via " + ", ".join(self.sent))
        for channel, reason in self.failed.items():
            parts.append(f"{channel} FAILED ({reason})")
        return "; ".join(parts) if parts else "no channels configured"


class Notifier:
    def __init__(self, config: Config, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()

    # ------------------------------------------------------------- messages

    def _availability_message(self, slots: list[Slot]) -> tuple[str, str, str]:
        cfg = self.config
        pretty_date = cfg.target_date.strftime("%B %-d, %Y") if _supports_dash() else cfg.target_date.strftime("%B %d, %Y").replace(" 0", " ")
        times = ", ".join(s.label for s in slots)
        subject = f"La Guérite availability found! — {times}"

        link_all = cfg.deep_link()
        lines = [
            "La Guérite availability found!",
            "",
            f"Restaurant: {cfg.restaurant}",
            f"Date: {pretty_date}",
            f"Party: {cfg.party_size}",
            f"Time: {times}",
            "",
            "BOOK NOW — each link opens with the date, party size and time already",
            'set. Just press "Search", then pick the time.',
            "",
        ]
        for slot in slots:
            lines.append(f"  {slot.label}")
            lines.append(f"  {cfg.deep_link(slot.time)}")
            lines.append("")
        text = "\n".join(lines)

        rows = "".join(
            f'<li style="margin:6px 0"><strong>{html.escape(s.label)}</strong> — '
            f'<a href="{html.escape(cfg.deep_link(s.time))}">book this time</a></li>'
            for s in slots
        )
        body_html = f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:520px;line-height:1.5">
  <h2 style="margin:0 0 4px">La Guérite availability found!</h2>
  <p style="margin:0 0 16px;color:#555">A bookable table matched your criteria.</p>
  <table cellpadding="0" cellspacing="0" style="margin-bottom:16px">
    <tr><td style="padding:2px 16px 2px 0;color:#666">Restaurant</td><td><strong>{html.escape(cfg.restaurant)}</strong></td></tr>
    <tr><td style="padding:2px 16px 2px 0;color:#666">Date</td><td><strong>{html.escape(pretty_date)}</strong></td></tr>
    <tr><td style="padding:2px 16px 2px 0;color:#666">Party</td><td><strong>{cfg.party_size}</strong></td></tr>
    <tr><td style="padding:2px 16px 2px 0;color:#666">Time</td><td><strong>{html.escape(times)}</strong></td></tr>
  </table>
  <p style="margin:0 0 8px;color:#555">
    Each link opens with the date, party size and time already set —
    just press <strong>Search</strong>, then pick the time.
  </p>
  <ul style="padding-left:20px;margin:0 0 20px">{rows}</ul>
  <p style="margin:0 0 24px">
    <a href="{html.escape(link_all)}"
       style="background:#111;color:#fff;padding:12px 20px;border-radius:6px;text-decoration:none;display:inline-block">
      Open the booking page
    </a>
  </p>
  <p style="color:#888;font-size:12px;margin:0">
    Tables at this restaurant go fast — book immediately. You will not get another
    alert for these same times unless they disappear and come back.
  </p>
</div>"""
        return subject, text, body_html

    def _error_message(self, reason: str, failures: int) -> tuple[str, str, str]:
        cfg = self.config
        subject = "La Guérite monitor — cannot read availability"
        text = (
            "The La Guérite monitor could not determine availability.\n\n"
            f"Reason: {reason}\n"
            f"Consecutive failed checks: {failures}\n\n"
            "The monitor is still running and will keep retrying, but the booking site\n"
            "may have changed. Please check it manually:\n"
            f"{cfg.deep_link()}\n"
        )
        body_html = f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:520px;line-height:1.5">
  <h2 style="margin:0 0 8px">La Guérite monitor needs attention</h2>
  <p>The monitor could not determine availability.</p>
  <p style="background:#fff4f4;border-left:3px solid #d33;padding:10px 14px;margin:16px 0">
    <strong>Reason:</strong> {html.escape(reason)}<br>
    <strong>Consecutive failed checks:</strong> {failures}
  </p>
  <p>It is still running and will keep retrying, but the booking site may have changed.</p>
  <p><a href="{html.escape(cfg.deep_link())}">Check the booking page manually</a></p>
</div>"""
        return subject, text, body_html

    # ------------------------------------------------------------- channels

    def _send_email(self, subject: str, text: str, body_html: str) -> None:
        cfg = self.config
        if not cfg.resend_api_key:
            raise RuntimeError("RESEND_API_KEY is not set")
        if not cfg.notify_email_to:
            raise RuntimeError("NOTIFY_EMAIL_TO is not set")

        recipients = [addr.strip() for addr in cfg.notify_email_to.split(",") if addr.strip()]
        response = self.session.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {cfg.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": cfg.notify_email_from,
                "to": recipients,
                "subject": subject,
                "text": text,
                "html": body_html,
            },
            timeout=cfg.request_timeout_seconds,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"Resend returned HTTP {response.status_code}: {response.text[:300]}")

    def _send_telegram(self, subject: str, text: str) -> None:
        cfg = self.config
        if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
            raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")
        # Deliberately NO parse_mode. The booking URL contains underscores
        # (default_date, default_party_size, default_time) which Telegram's
        # Markdown parser reads as italic markers — that either mangles the
        # link or fails outright with "can't parse entities". Plain text is
        # safe, and Telegram auto-links bare URLs regardless.
        response = self.session.post(
            f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
            json={
                "chat_id": cfg.telegram_chat_id,
                "text": f"{subject}\n\n{text}",
                "disable_web_page_preview": False,
            },
            timeout=cfg.request_timeout_seconds,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"Telegram returned HTTP {response.status_code}: {response.text[:300]}")

    def _send_pushover(self, subject: str, text: str, url: str) -> None:
        cfg = self.config
        if not (cfg.pushover_token and cfg.pushover_user_key):
            raise RuntimeError("PUSHOVER_TOKEN / PUSHOVER_USER_KEY are not set")
        response = self.session.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": cfg.pushover_token,
                "user": cfg.pushover_user_key,
                "title": subject,
                "message": text,
                "url": url,
                "url_title": "Open booking page",
                "priority": 1,
            },
            timeout=cfg.request_timeout_seconds,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"Pushover returned HTTP {response.status_code}: {response.text[:300]}")

    def _send_console(self, subject: str, text: str) -> None:
        """Dry-run channel: prints the notification instead of sending it.

        Lets you exercise the whole detect -> notify path with no credentials.
        """
        border = "=" * 66
        logger.info(
            "NOTIFICATION (console channel — nothing was actually sent)\n%s\nSubject: %s\n%s\n%s\n%s",
            border,
            subject,
            "-" * 66,
            text,
            border,
        )

    def _dispatch(self, subject: str, text: str, body_html: str) -> NotifyOutcome:
        sent: list[str] = []
        failed: dict[str, str] = {}
        for channel in self.config.notify_channels:
            try:
                if channel == "console":
                    self._send_console(subject, text)
                elif channel == "email":
                    self._send_email(subject, text, body_html)
                elif channel == "telegram":
                    self._send_telegram(subject, text)
                elif channel == "pushover":
                    self._send_pushover(subject, text, self.config.deep_link())
                else:
                    raise RuntimeError(f"unknown channel {channel!r}")
                sent.append(channel)
            except Exception as exc:
                failed[channel] = str(exc)
                logger.error("Notification via %s failed: %s", channel, exc)
        return NotifyOutcome(sent=sent, failed=failed)

    # ---------------------------------------------------------------- public

    def notify_availability(self, slots: list[Slot]) -> NotifyOutcome:
        subject, text, body_html = self._availability_message(slots)
        return self._dispatch(subject, text, body_html)

    def notify_error(self, reason: str, failures: int) -> NotifyOutcome:
        subject, text, body_html = self._error_message(reason, failures)
        return self._dispatch(subject, text, body_html)

    def notify_heartbeat(self, state, last_summary: str) -> NotifyOutcome:
        """Periodic proof of life, so silence never means 'probably dead'."""
        cfg = self.config
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        subject = "La Guérite monitor — still watching"
        text = (
            f"Still watching. No qualifying table yet.\n\n"
            f"Date: {cfg.target_date.isoformat()}  ({cfg.party_size} guests, "
            f"{cfg.earliest_time:%H:%M}-{cfg.latest_time:%H:%M})\n"
            f"Checks run: {state.total_checks}\n"
            f"Last check: {state.last_check_at or 'never'}\n"
            f"Last result: {last_summary}\n"
            f"Alerts sent so far: {state.total_notifications}\n\n"
            f"This is a routine check-in at {stamp}. You will get a separate\n"
            f"alert the moment a bookable table appears.\n\n"
            f"{cfg.deep_link()}\n"
        )
        body_html = f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:520px;line-height:1.5">
  <h2 style="margin:0 0 8px">Still watching 👀</h2>
  <p style="margin:0 0 16px;color:#555">No qualifying table yet — this is a routine check-in.</p>
  <table cellpadding="0" cellspacing="0">
    <tr><td style="padding:2px 16px 2px 0;color:#666">Looking for</td><td><strong>{cfg.target_date.isoformat()}, {cfg.party_size} guests, {cfg.earliest_time:%H:%M}–{cfg.latest_time:%H:%M}</strong></td></tr>
    <tr><td style="padding:2px 16px 2px 0;color:#666">Checks run</td><td><strong>{state.total_checks}</strong></td></tr>
    <tr><td style="padding:2px 16px 2px 0;color:#666">Last check</td><td><strong>{html.escape(str(state.last_check_at or 'never'))}</strong></td></tr>
    <tr><td style="padding:2px 16px 2px 0;color:#666">Last result</td><td>{html.escape(last_summary)}</td></tr>
  </table>
</div>"""
        return self._dispatch(subject, text, body_html)

    def notify_test(self) -> NotifyOutcome:
        cfg = self.config
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = "La Guérite monitor — test notification"
        text = (
            "This is a TEST notification from your La Guérite monitor.\n\n"
            "If you are reading this, notifications are wired up correctly.\n\n"
            f"Restaurant: {cfg.restaurant}\n"
            f"Date: {cfg.target_date.isoformat()}\n"
            f"Party: {cfg.party_size}\n"
            f"Window: {cfg.earliest_time:%H:%M}–{cfg.latest_time:%H:%M} ({cfg.venue_timezone})\n"
            f"Channels: {', '.join(cfg.notify_channels)}\n"
            f"Sent at: {stamp}\n\n"
            f"Booking page: {cfg.deep_link()}\n"
        )
        body_html = f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:520px;line-height:1.5">
  <h2 style="margin:0 0 8px">Test notification ✅</h2>
  <p>Your La Guérite monitor can reach you. No real availability has been found — this is just a wiring test.</p>
  <table cellpadding="0" cellspacing="0" style="margin:16px 0">
    <tr><td style="padding:2px 16px 2px 0;color:#666">Restaurant</td><td><strong>{html.escape(cfg.restaurant)}</strong></td></tr>
    <tr><td style="padding:2px 16px 2px 0;color:#666">Date</td><td><strong>{cfg.target_date.isoformat()}</strong></td></tr>
    <tr><td style="padding:2px 16px 2px 0;color:#666">Party</td><td><strong>{cfg.party_size}</strong></td></tr>
    <tr><td style="padding:2px 16px 2px 0;color:#666">Window</td><td><strong>{cfg.earliest_time:%H:%M}–{cfg.latest_time:%H:%M}</strong> ({html.escape(cfg.venue_timezone)})</td></tr>
    <tr><td style="padding:2px 16px 2px 0;color:#666">Channels</td><td><strong>{html.escape(', '.join(cfg.notify_channels))}</strong></td></tr>
  </table>
  <p><a href="{html.escape(cfg.deep_link())}">Booking page</a></p>
</div>"""
        return self._dispatch(subject, text, body_html)


def _chats_from_updates(payload: dict) -> dict[str, str]:
    chats: dict[str, str] = {}
    for update in payload.get("result") or []:
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if not chat:
                continue
            chat_id = str(chat.get("id"))
            name = (
                chat.get("username")
                or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                or chat.get("title")
                or chat.get("type")
                or "chat"
            )
            chats[chat_id] = name
    return chats


def telegram_discover(
    token: str,
    timeout: int = 30,
    wait_seconds: int = 0,
    on_wait=None,
) -> tuple[str | None, list[tuple[str, str]], str | None]:
    """Look up a bot's identity and any chat IDs that have messaged it.

    Telegram only keeps a message queued until it is consumed, so a chat that
    messaged the bot earlier may leave nothing to find. With `wait_seconds` we
    long-poll, letting the user send a message while the command waits.

    Returns ``(bot_username, [(chat_id, description), ...], error)``.
    """
    base = f"https://api.telegram.org/bot{token}"
    try:
        me = requests.get(f"{base}/getMe", timeout=timeout)
    except requests.RequestException as exc:
        return None, [], f"could not reach Telegram ({exc.__class__.__name__})"

    if me.status_code == 401:
        return None, [], "Telegram rejected that bot token (401 Unauthorized)"
    if me.status_code >= 300:
        return None, [], f"getMe returned HTTP {me.status_code}: {me.text[:200]}"

    username = (me.json().get("result") or {}).get("username")

    deadline = time.monotonic() + max(0, wait_seconds)
    announced = False
    while True:
        # Telegram's own long-poll: the call blocks until a message arrives.
        poll = min(25, max(0, int(deadline - time.monotonic())))
        try:
            updates = requests.get(
                f"{base}/getUpdates",
                params={"timeout": poll} if poll else None,
                timeout=timeout + poll,
            )
        except requests.RequestException as exc:
            return username, [], f"could not fetch updates ({exc.__class__.__name__})"
        if updates.status_code == 409:
            return username, [], (
                "another process is polling this bot (409 Conflict) — "
                "stop it, or remove the webhook, then try again"
            )
        if updates.status_code >= 300:
            return username, [], f"getUpdates returned HTTP {updates.status_code}"

        chats = _chats_from_updates(updates.json())
        if chats:
            return username, sorted(chats.items()), None
        if time.monotonic() >= deadline:
            return username, [], None
        if on_wait and not announced:
            on_wait(username)
            announced = True


def _supports_dash() -> bool:
    """`%-d` works on glibc/macOS but not on Windows."""
    try:
        datetime(2026, 9, 3).strftime("%-d")
        return True
    except ValueError:
        return False
