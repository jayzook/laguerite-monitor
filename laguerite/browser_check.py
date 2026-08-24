"""Playwright fallback: drive the real booking page in headless Chromium.

The SevenRooms widget does not accept a full deep link — it renders a search
form (guests / date / time) and only queries availability once you press
Search. So this module reproduces exactly what a human does:

    set party size -> pick the date -> press Search -> read the offered times

Two independent readings are taken and cross-checked:

1. the availability XHR the widget fires, parsed by the *same* parser the
   direct API client uses, and
2. the rendered ``sr-timeslot-button`` elements, filtered by their ``data-date``
   attribute so that the page's "Other dates with availability" carousel can
   never be mistaken for the date we actually want.

Only genuinely bookable times are counted. The separate "Submit a table
request" control is deliberately ignored — a request is not a reservation.

Nothing here bypasses a CAPTCHA, a login, or any other access control, and no
screen coordinates or image recognition are used.
"""

from __future__ import annotations

import logging
import re
from datetime import date as _date
from typing import Any

from .availability import CheckResult, CheckStatus, Slot, parse_slot_time
from .config import Config
from .sevenrooms_api import SevenRoomsClient

logger = logging.getLogger(__name__)

_AVAILABILITY_URL_MARKER = "/availability/widget/range"
_MAX_MONTH_CLICKS = 30

# Selectors are SevenRooms' own `data-test` hooks, which are far more stable
# than CSS class names (their classes are hashed by styled-components).
SEL_SEARCH = "[data-test='sr-search-button']"
SEL_GUEST_BUTTON = "[data-test='sr-guest-count-button']"
SEL_GUEST_PICKER = "[data-test='sr-guest-count']"
SEL_DATE_BUTTON = "[data-test='sr-calendar-date-button']"
SEL_DATE_PICKER = "[data-test='sr-calendar-date-picker']"
SEL_MONTH_HEADER = "[data-test='sr-calendar-date-picker-button']"
SEL_MONTH_NEXT = "[data-test='sr-increment-arrow']"
SEL_MONTH_PREV = "[data-test='sr-decrement-arrow']"
SEL_TIMESLOT = "[data-test='sr-timeslot-button']"
SEL_RESULT_BLOCK = "[data-test='sr-main-result-block']"
SEL_REQUEST_BUTTON = "[data-test='sr-request-button']"
SEL_BANNER = "[data-test='banner-info']"

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    # The widget has a language switcher; cover French too.
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}


class PlaywrightUnavailable(RuntimeError):
    """Playwright, or its browser binary, is not installed."""


class WidgetInteractionError(RuntimeError):
    """The booking form did not behave the way we know how to drive."""


def _parse_month_header(text: str) -> tuple[int, int] | None:
    """'September 2026' -> (2026, 9)."""
    if not text:
        return None
    lowered = text.strip().lower()
    year_match = re.search(r"(20\d{2})", lowered)
    if not year_match:
        return None
    year = int(year_match.group(1))
    for name, number in _MONTHS.items():
        if name in lowered:
            return year, number
    return None


def _set_party_size(page: Any, party_size: int) -> None:
    page.click(SEL_GUEST_BUTTON, timeout=15000)
    page.wait_for_timeout(600)
    # The open picker lists the sizes as plain text nodes; match exactly so
    # "6" never selects "16".
    page.click(f"{SEL_GUEST_PICKER} >> text='{party_size}'", timeout=10000)
    page.wait_for_timeout(600)

    shown = (page.inner_text(SEL_GUEST_BUTTON) or "").strip()
    if not re.search(rf"\b{party_size}\b", shown):
        raise WidgetInteractionError(
            f"could not set party size to {party_size} (picker shows {shown!r})"
        )


def _open_date_picker(page: Any) -> None:
    for selector in (SEL_DATE_BUTTON, SEL_MONTH_HEADER):
        element = page.query_selector(selector)
        if element:
            try:
                element.click()
                page.wait_for_timeout(800)
            except Exception:
                continue
            if page.query_selector(SEL_DATE_PICKER):
                return
    if not page.query_selector(SEL_DATE_PICKER):
        raise WidgetInteractionError("could not open the date picker")


def _select_date(page: Any, target: _date) -> None:
    _open_date_picker(page)

    for _ in range(_MAX_MONTH_CLICKS):
        header = ""
        element = page.query_selector(SEL_MONTH_HEADER)
        if element:
            header = (element.inner_text() or "").split("\n")[0]
        current = _parse_month_header(header)
        if current is None:
            raise WidgetInteractionError(
                f"could not read the calendar month header (saw {header!r})"
            )
        if current == (target.year, target.month):
            break
        selector = SEL_MONTH_NEXT if current < (target.year, target.month) else SEL_MONTH_PREV
        arrow = page.query_selector(selector)
        if not arrow:
            raise WidgetInteractionError(
                f"calendar is showing {header!r} and has no arrow to reach "
                f"{target:%B %Y}"
            )
        arrow.click()
        page.wait_for_timeout(700)
    else:
        raise WidgetInteractionError(f"could not navigate the calendar to {target:%B %Y}")

    # Click the day cell whose text is exactly the day number and which is not
    # disabled (greyed-out cells belong to closed days or adjacent months).
    clicked = page.evaluate(
        """(args) => {
            const root = document.querySelector(args.picker);
            if (!root) return false;
            const wanted = String(args.day);
            const nodes = Array.from(root.querySelectorAll('*'));
            for (const el of nodes) {
                if (el.children.length) continue;
                if ((el.textContent || '').trim() !== wanted) continue;
                let node = el;
                for (let i = 0; i < 4 && node; i++) {
                    const disabled = node.getAttribute && (
                        node.getAttribute('aria-disabled') === 'true' ||
                        node.hasAttribute('disabled')
                    );
                    if (disabled) return false;
                    node = node.parentElement;
                }
                el.click();
                return true;
            }
            return false;
        }""",
        {"picker": SEL_DATE_PICKER, "day": target.day},
    )
    if not clicked:
        raise WidgetInteractionError(
            f"could not click day {target.day} in the calendar "
            f"(it may be unavailable or outside the booking window)"
        )
    page.wait_for_timeout(900)


def _dom_slots_for_date(page: Any, target: _date) -> list[Slot]:
    """Read the rendered bookable times for exactly `target`.

    Each button carries data-date="MM-DD-YYYY", which lets us ignore the
    "Other dates with availability" section entirely.
    """
    wanted = target.strftime("%m-%d-%Y")
    slots: list[Slot] = []
    seen: set[str] = set()

    for handle in page.query_selector_all(SEL_TIMESLOT):
        try:
            if (handle.get_attribute("data-date") or "").strip() != wanted:
                continue
            text = (handle.inner_text() or "").strip()
        except Exception:
            continue
        first_line = text.split("\n")[0].strip()
        slot_time = parse_slot_time(first_line)
        if slot_time is None:
            continue
        key = slot_time.strftime("%H:%M")
        if key in seen:
            continue
        seen.add(key)
        description = " ".join(text.split("\n")[1:]).strip()
        slots.append(
            Slot(time=slot_time, slot_type="book", shift_name="dom", description=description)
        )
    return sorted(slots)


def check_with_browser(config: Config) -> CheckResult:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on install
        raise PlaywrightUnavailable(
            "playwright is not installed; run `pip install playwright` "
            "and `python -m playwright install chromium`"
        ) from exc

    parser = SevenRoomsClient(config)
    captured: list[dict[str, Any]] = []
    # `default_time` is the one query param the widget honours; it saves a step.
    url = f"{config.booking_url}?default_time={config.earliest_time:%H:%M}"

    dom_slots: list[Slot] = []
    banner = ""
    saw_request_button = False
    interaction_error: str | None = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                context = browser.new_context(
                    user_agent=config.user_agent,
                    locale="en-US",
                    viewport={"width": 1400, "height": 1100},
                )
                page = context.new_page()
                page.set_default_timeout(config.playwright_timeout_ms)

                def on_response(response: Any) -> None:
                    if _AVAILABILITY_URL_MARKER in response.url:
                        try:
                            captured.append(response.json())
                        except Exception:
                            logger.debug("could not decode an availability response")

                page.on("response", on_response)

                page.goto(url, wait_until="domcontentloaded",
                          timeout=config.playwright_timeout_ms)
                page.wait_for_selector(SEL_SEARCH, timeout=config.playwright_timeout_ms)
                page.wait_for_timeout(1500)

                try:
                    _set_party_size(page, config.party_size)
                    _select_date(page, config.target_date)
                    page.click(SEL_SEARCH, timeout=15000)
                except WidgetInteractionError as exc:
                    interaction_error = str(exc)
                except PlaywrightTimeout as exc:
                    interaction_error = f"timed out driving the booking form ({exc.__class__.__name__})"

                if interaction_error is None:
                    # Wait for either results or the request-only state.
                    try:
                        page.wait_for_selector(
                            f"{SEL_RESULT_BLOCK}, {SEL_TIMESLOT}, {SEL_REQUEST_BUTTON}",
                            timeout=config.playwright_timeout_ms,
                        )
                    except PlaywrightTimeout:
                        logger.debug("no result block appeared before the timeout")
                    page.wait_for_timeout(3000)

                    dom_slots = _dom_slots_for_date(page, config.target_date)
                    saw_request_button = bool(page.query_selector(SEL_REQUEST_BUTTON))
                    element = page.query_selector(SEL_BANNER)
                    if element:
                        banner = (element.inner_text() or "").strip().replace("\n", " ")
            finally:
                browser.close()
    except PlaywrightUnavailable:
        raise
    except Exception as exc:
        message = (str(exc).strip().splitlines() or [exc.__class__.__name__])[0]
        lowered = message.lower()
        if "executable doesn't exist" in lowered or "browsertype.launch" in lowered:
            raise PlaywrightUnavailable(
                "Chromium is not installed for Playwright; "
                "run `python -m playwright install chromium`"
            ) from exc
        return CheckResult(
            status=CheckStatus.TRANSIENT_ERROR,
            source="playwright",
            error=f"browser check failed: {message}",
        )

    if banner:
        logger.debug("widget banner reads: %s", banner)
        # Guard against silently reading the wrong search.
        if not re.search(rf"\b{config.party_size}\b", banner):
            return CheckResult(
                status=CheckStatus.STRUCTURE_ERROR,
                source="playwright",
                error=(
                    f"the booking form ended up showing {banner!r}, which does not "
                    f"match party size {config.party_size}"
                ),
            )

    # --- Reading 1: the widget's own availability payload -------------------
    xhr_result: CheckResult | None = None
    for payload in captured:
        candidate = parser.parse(payload)
        if candidate.status is CheckStatus.OK:
            xhr_result = candidate
            break
    if xhr_result is None and captured:
        xhr_result = parser.parse(captured[-1])

    # --- Reading 2: the rendered buttons ------------------------------------
    dom_qualifying = sorted(
        s
        for s in dom_slots
        if config.earliest_time <= s.time <= config.latest_time
    )

    if xhr_result is not None and xhr_result.status is CheckStatus.OK:
        xhr_keys = {s.key for s in xhr_result.qualifying}
        dom_keys = {s.key for s in dom_qualifying}
        if dom_slots and xhr_keys != dom_keys:
            logger.warning(
                "Browser cross-check mismatch — XHR says %s, page shows %s. "
                "Trusting the page.",
                sorted(xhr_keys) or "nothing",
                sorted(dom_keys) or "nothing",
            )
            return CheckResult(
                status=CheckStatus.OK,
                source="playwright-dom",
                qualifying=dom_qualifying,
                all_slots=dom_slots,
            )
        xhr_result.source = "playwright-xhr"
        return xhr_result

    if dom_slots:
        return CheckResult(
            status=CheckStatus.OK,
            source="playwright-dom",
            qualifying=dom_qualifying,
            all_slots=dom_slots,
        )

    # No bookable buttons at all, but the page clearly rendered a result state
    # offering only a table *request* — that is a legitimate "no availability".
    if saw_request_button:
        return CheckResult(status=CheckStatus.OK, source="playwright-dom")

    if interaction_error:
        return CheckResult(
            status=CheckStatus.STRUCTURE_ERROR,
            source="playwright",
            error=f"booking form changed: {interaction_error}",
        )

    return CheckResult(
        status=CheckStatus.STRUCTURE_ERROR,
        source="playwright",
        error="booking page loaded but no availability data or time buttons were found",
    )
