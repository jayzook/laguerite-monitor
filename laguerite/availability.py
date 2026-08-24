"""Value types describing the outcome of one availability check."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time as _time
from enum import Enum


class CheckStatus(str, Enum):
    OK = "ok"
    """The page/endpoint answered and we understood it."""

    TRANSIENT_ERROR = "transient_error"
    """Network blip, timeout, 5xx, rate limit. Retry later, nothing is wrong."""

    STRUCTURE_ERROR = "structure_error"
    """We reached the site but can no longer tell what is available."""


_TIME_PATTERNS = (
    "%H:%M:%S",
    "%H:%M",
    "%I:%M %p",
    "%I:%M%p",
    "%I %p",
)


def parse_slot_time(raw: str | None) -> _time | None:
    """Parse a SevenRooms time string.

    Handles both 24-hour venues ("14:30") and 12-hour venues ("2:30 PM"), and
    full timestamps ("2026-09-03 14:30:00"), so a locale change on the venue
    does not break detection.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None

    # Full datetime strings: keep only the clock part.
    match = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*([AaPp][Mm])?\s*$", text)
    if match:
        text = match.group(1) + (f" {match.group(2).upper()}" if match.group(2) else "")

    text = text.replace(".", "").upper()
    text = re.sub(r"\s+", " ", text)
    for pattern in _TIME_PATTERNS:
        try:
            return datetime.strptime(text, pattern).time()
        except ValueError:
            continue
    return None


def format_12h(value: _time) -> str:
    """`14:30` -> `2:30 PM` (no leading zero, matching how people say it)."""
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {suffix}"


@dataclass(frozen=True, order=True)
class Slot:
    """A single reservation time offered by the booking page."""

    time: _time
    slot_type: str = "book"
    shift_name: str = ""
    description: str = ""

    @property
    def key(self) -> str:
        """Stable identity used for de-duplicating notifications."""
        return self.time.strftime("%H:%M")

    @property
    def label(self) -> str:
        return format_12h(self.time)

    def __str__(self) -> str:  # pragma: no cover - display helper
        return self.label


@dataclass
class CheckResult:
    """Everything one check learned."""

    status: CheckStatus
    source: str = "sevenrooms-api"
    qualifying: list[Slot] = field(default_factory=list)
    """Bookable slots inside the requested time window, for the requested party."""

    all_slots: list[Slot] = field(default_factory=list)
    """Every slot the venue returned, whatever its type — useful for logs."""

    error: str | None = None
    http_status: int | None = None

    @property
    def page_loaded(self) -> bool:
        return self.status is not CheckStatus.TRANSIENT_ERROR

    @property
    def qualifying_keys(self) -> set[str]:
        return {slot.key for slot in self.qualifying}

    def summary(self) -> str:
        if self.status is CheckStatus.TRANSIENT_ERROR:
            return f"Check failed — {self.error}"
        if self.status is CheckStatus.STRUCTURE_ERROR:
            return f"Could not determine availability — {self.error}"
        if self.qualifying:
            return "AVAILABLE: " + ", ".join(s.label for s in self.qualifying)
        return "No availability"
