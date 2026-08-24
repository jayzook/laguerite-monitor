"""Tiny JSON-file persistence so restarts don't re-notify about known slots."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class MonitorState:
    version: int = STATE_VERSION

    notified_times: list[str] = field(default_factory=list)
    """Qualifying slots (as 'HH:MM') that were available at the last good check
    and have already been notified about. A slot leaves this list when it
    disappears, which is what allows a re-notification if it comes back."""

    # Identity of the search these results belong to. If the user changes the
    # date/party/window, previous notifications no longer apply.
    criteria_fingerprint: str = ""

    consecutive_failures: int = 0
    last_error_notified_at: float | None = None
    last_heartbeat_at: float | None = None
    last_check_at: str | None = None
    last_success_at: str | None = None
    total_checks: int = 0
    total_notifications: int = 0
    booked: bool = False
    """Set when STOP_AFTER_FIRST_NOTIFICATION has fired, so a restart stays stopped."""

    @property
    def notified(self) -> set[str]:
        return set(self.notified_times)

    def set_notified(self, keys: set[str]) -> None:
        self.notified_times = sorted(keys)


def _fingerprint(config) -> str:
    return "|".join(
        [
            config.venue_slug,
            config.target_date.isoformat(),
            str(config.party_size),
            config.earliest_time.strftime("%H:%M"),
            config.latest_time.strftime("%H:%M"),
        ]
    )


def load_state(path: Path, config) -> MonitorState:
    fingerprint = _fingerprint(config)
    if not path.exists():
        return MonitorState(criteria_fingerprint=fingerprint)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read state file %s (%s); starting fresh", path, exc)
        return MonitorState(criteria_fingerprint=fingerprint)

    if not isinstance(raw, dict):
        logger.warning("State file %s is not an object; starting fresh", path)
        return MonitorState(criteria_fingerprint=fingerprint)

    known = {f for f in MonitorState.__dataclass_fields__}
    state = MonitorState(**{k: v for k, v in raw.items() if k in known})

    if state.criteria_fingerprint and state.criteria_fingerprint != fingerprint:
        logger.info(
            "Search criteria changed since last run; clearing remembered notifications"
        )
        state = MonitorState(criteria_fingerprint=fingerprint)
    state.criteria_fingerprint = fingerprint
    state.version = STATE_VERSION
    return state


def save_state(path: Path, state: MonitorState) -> None:
    """Write atomically so a crash mid-write cannot corrupt the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(state), handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
