"""Client for the public SevenRooms availability endpoint.

This is the very same request the booking widget makes from the browser when
you pick a date and party size on
https://www.sevenrooms.com/reservations/<venue>. It needs no login, no token
and no CAPTCHA solving, and `/api-yoa/` is not disallowed by
https://www.sevenrooms.com/robots.txt. We simply ask the public page the same
question a visitor would, at a slow and randomised pace.
"""

from __future__ import annotations

import logging
import random
import time as _time_module
from typing import Any

import requests

from .availability import CheckResult, CheckStatus, Slot, parse_slot_time
from .config import Config

logger = logging.getLogger(__name__)

ENDPOINT = "https://www.sevenrooms.com/api-yoa/availability/widget/range"

# Retry on the transient stuff only; 4xx (other than 429) means we asked wrong.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504, 522, 524}


class SevenRoomsClient:
    def __init__(self, config: Config, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
                "Referer": config.booking_url,
            }
        )

    # ------------------------------------------------------------------ fetch

    def _request_once(self) -> tuple[int, dict[str, Any] | None, str | None]:
        cfg = self.config
        params = {
            "venue": cfg.venue_slug,
            # Anchor the halo in the middle of the wanted window so the returned
            # range comfortably covers EARLIEST_TIME..LATEST_TIME.
            "time_slot": cfg.earliest_time.strftime("%H:%M"),
            "party_size": cfg.party_size,
            "halo_size_interval": 100,
            "start_date": cfg.target_date.strftime("%m-%d-%Y"),
            "num_days": 1,
            "channel": "SEVENROOMS_WIDGET",
        }
        response = self.session.get(
            ENDPOINT, params=params, timeout=cfg.request_timeout_seconds
        )
        if response.status_code != 200:
            return response.status_code, None, f"HTTP {response.status_code}"
        try:
            return response.status_code, response.json(), None
        except ValueError as exc:
            return response.status_code, None, f"response was not JSON ({exc})"

    def fetch(self) -> tuple[dict[str, Any] | None, str | None, int | None, bool]:
        """Fetch the availability payload.

        Returns ``(payload, error, http_status, retryable)``.
        """
        last_error: str | None = None
        last_status: int | None = None
        retryable = True

        for attempt in range(1, self.config.max_attempts_per_check + 1):
            try:
                status, payload, error = self._request_once()
            except requests.Timeout:
                status, payload, error = None, None, "request timed out"
            except requests.ConnectionError as exc:
                status, payload, error = None, None, f"connection error ({exc.__class__.__name__})"
            except requests.RequestException as exc:
                status, payload, error = None, None, f"request failed ({exc})"

            if payload is not None:
                return payload, None, status, False

            last_error, last_status = error, status
            retryable = status is None or status in _RETRYABLE_STATUS
            if not retryable or attempt == self.config.max_attempts_per_check:
                break

            # Exponential backoff with jitter; give 429s a much wider berth.
            backoff = min(60.0, 2.0 ** attempt) + random.uniform(0, 2)
            if status == 429:
                backoff = max(backoff, 60.0)
            logger.debug(
                "attempt %s/%s failed (%s); retrying in %.1fs",
                attempt,
                self.config.max_attempts_per_check,
                last_error,
                backoff,
            )
            _time_module.sleep(backoff)

        return None, last_error, last_status, retryable

    # ------------------------------------------------------------------ parse

    def check(self) -> CheckResult:
        cfg = self.config
        payload, error, http_status, retryable = self.fetch()

        if payload is None:
            # A non-retryable HTTP error (e.g. a permanent 404 because the venue
            # slug changed) means we genuinely cannot read availability anymore.
            status = CheckStatus.TRANSIENT_ERROR if retryable else CheckStatus.STRUCTURE_ERROR
            return CheckResult(status=status, error=error, http_status=http_status)

        return self.parse(payload, http_status=http_status)

    def parse(self, payload: dict[str, Any], http_status: int | None = None) -> CheckResult:
        """Turn a raw payload into a CheckResult.

        Kept separate from the network call so it can be unit-tested against
        recorded fixtures.
        """
        cfg = self.config

        def structure_error(message: str) -> CheckResult:
            return CheckResult(
                status=CheckStatus.STRUCTURE_ERROR, error=message, http_status=http_status
            )

        if not isinstance(payload, dict):
            return structure_error("payload was not a JSON object")

        data = payload.get("data")
        if not isinstance(data, dict):
            return structure_error("payload has no 'data' object (API shape changed?)")

        availability = data.get("availability")
        if not isinstance(availability, dict):
            return structure_error(
                "payload has no 'data.availability' mapping (API shape changed?)"
            )

        date_key = cfg.target_date.isoformat()
        shifts = availability.get(date_key)
        if shifts is None:
            # The venue simply publishes nothing for that day (closed, or the
            # date is outside its booking window). Not an error.
            logger.debug("no entry for %s in availability payload", date_key)
            return CheckResult(status=CheckStatus.OK, http_status=http_status)

        if not isinstance(shifts, list):
            return structure_error(f"availability['{date_key}'] was not a list")

        all_slots: list[Slot] = []
        unparsable = 0
        seen_types: set[str] = set()

        for shift in shifts:
            if not isinstance(shift, dict):
                continue
            if shift.get("is_closed"):
                continue
            shift_name = str(shift.get("name") or "")
            times = shift.get("times")
            if not isinstance(times, list):
                continue
            for entry in times:
                if not isinstance(entry, dict):
                    continue
                slot_time = parse_slot_time(entry.get("time_iso")) or parse_slot_time(
                    entry.get("time")
                )
                if slot_time is None:
                    unparsable += 1
                    continue
                slot_type = str(entry.get("type") or "").lower()
                seen_types.add(slot_type)
                all_slots.append(
                    Slot(
                        time=slot_time,
                        slot_type=slot_type,
                        shift_name=shift_name,
                        description=str(entry.get("public_time_slot_description") or ""),
                    )
                )

        # If the venue offered slots but we could not read a single time, the
        # format changed under us — better to shout than to silently report
        # "no availability" forever.
        if unparsable and not all_slots:
            return structure_error(
                f"found {unparsable} time entries for {date_key} but could not parse any of them"
            )

        if all_slots and not (seen_types & set(cfg.bookable_slot_types)) and not seen_types <= {
            "request",
            "closed",
            "wait",
            "waitlist",
            "",
        }:
            # Unrecognised vocabulary in the `type` field: we no longer know
            # which of these are truly bookable.
            return structure_error(
                "unrecognised slot types "
                f"{sorted(seen_types)}; expected one of {sorted(cfg.bookable_slot_types)}"
            )

        qualifying = sorted(
            slot
            for slot in all_slots
            if slot.slot_type in cfg.bookable_slot_types
            and cfg.earliest_time <= slot.time <= cfg.latest_time
        )

        return CheckResult(
            status=CheckStatus.OK,
            source="sevenrooms-api",
            qualifying=qualifying,
            all_slots=sorted(all_slots),
            http_status=http_status,
        )
