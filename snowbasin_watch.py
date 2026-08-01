import json
import os
import re
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

URL = "https://www.snowbasin.com/the-mountain/mountain-report/"
STATE_FILE = os.environ.get("STATE_FILE", "snowbasin_state.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Make it look more like a browser (sometimes helps with anti-bot / alternate markup)
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
DEBUG = os.environ.get("DEBUG", "0") == "1"

# The mountain report renders each lift/trail/gate as its own little card whose text,
# once flattened, comes out as a run of separate lines: Name, then zero or more "extra"
# lines (hours, lift type, a note like "Downhill biking only"), then a status line that
# is one of the tokens below (nothing else on the page matches these exactly), then
# sometimes a trailing "Suitable for ..." descriptor. The column layout has changed
# between the summer and winter versions of the report (and may change again), but this
# Name -> ... -> Status pattern is what both versions share, so we key off of it instead
# of assuming a fixed number/order of columns.
STATUS_TOKENS = {
    "Lift Open": "lifts",
    "Lift Closed": "lifts",
    "Lift On Hold": "lifts",
    "Lift Scheduled": "lifts",
    "Lift Delayed": "lifts",
    "Trail Open": "trails",
    "Trail Closed": "trails",
    "Trail Expected": "trails",
    "Trail Delayed": "trails",
    # Winter access gates have historically been rendered with the same markup as
    # trails ("Trail Open"/"Trail Closed"); these "Gate ..." variants are kept as a
    # fallback in case that changes.
    "Gate Open": "gates",
    "Gate Closed": "gates",
    "Gate On Hold": "gates",
    "Gate Scheduled": "gates",
    "Gate Delayed": "gates",
}

# Column-header labels that show up as their own line in the flattened text; never
# treat these as a lift/trail/gate name.
HEADER_LABELS = {
    "Lift Name", "Trail Name", "Gate Name", "Status", "Hours", "Lift Type", "Gate Type",
}

GATE_NAME_REGEX = re.compile(r".*\bGate$", re.IGNORECASE)
GATE_SPECIAL_NAMES = {"The Wallow"}  # historically shows up in the Access Gates list

# The lift table has a trailing "Hours" column (e.g. "9:00 AM – 4:00 PM") that renders
# *after* the Status cell, so it lands in the buffer for the *next* item's name unless
# skipped. It has no fixed prefix like "Suitable for", so match it by shape instead.
HOUR_RANGE_REGEX = re.compile(
    r"^\d{1,2}(:\d{2})?\s*[AaPp]\.?[Mm]\.?\s*[-–]\s*\d{1,2}(:\d{2})?\s*[AaPp]\.?[Mm]\.?$"
)


def normalize_text(s: str) -> str:
    # Normalize non-breaking spaces and a couple other common "special spaces"
    s = s.replace("\xa0", " ").replace("\u2009", " ").replace("\u202f", " ")
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fetch_lines() -> List[str]:
    r = requests.get(
        URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=25,
    )
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n")
    text = text.replace("\xa0", " ")  # important: normalize before splitting/regex
    lines = [normalize_text(ln) for ln in text.splitlines()]
    return [ln for ln in lines if ln]


def is_gate_row(group: str, name: str) -> bool:
    if GATE_NAME_REGEX.match(name):
        return True
    if name in GATE_SPECIAL_NAMES:
        return True
    if "gate" in group.lower():
        return True
    return False


def parse_report(lines: List[str]) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """
    Walks the flattened page text and pulls out (lifts, trails, gates) dicts of
    "{group} :: {name}" -> status.

    Each accordion section on the page is preceded by a "Toggle accordion" line whose
    header (the group name, e.g. "Lifts", "Trails", "Bike Park", "Access Gates") is the
    line immediately before it. Within a section, entries appear as a run of lines
    ending in one of STATUS_TOKENS; the first line of that run is the item's name.
    """
    lifts: Dict[str, str] = {}
    trails: Dict[str, str] = {}
    gates: Dict[str, str] = {}

    group: Optional[str] = None
    prev_line = ""
    buffer: List[str] = []

    for ln in lines:
        if ln == "Toggle accordion":
            group = prev_line
            buffer = []
            prev_line = ln
            continue

        prev_line = ln

        if group is None:
            continue

        if ln in HEADER_LABELS:
            continue

        if ln.startswith("Suitable for"):
            continue

        if HOUR_RANGE_REGEX.match(ln):
            continue

        token_kind = STATUS_TOKENS.get(ln)
        if token_kind is None:
            buffer.append(ln)
            continue

        if not buffer:
            # A status line with no preceding name; nothing sensible to record.
            continue

        name = buffer[0]
        buffer = []
        key = f"{group} :: {name}"

        if token_kind == "gates" or is_gate_row(group, name):
            gates[key] = ln
        elif token_kind == "lifts":
            lifts[key] = ln
        else:
            trails[key] = ln

    return lifts, trails, gates


def load_state() -> Dict[str, Dict[str, str]]:
    if not os.path.exists(STATE_FILE):
        return {"lifts": {}, "trails": {}, "gates": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("lifts", {})
    state.setdefault("trails", {})
    state.setdefault("gates", {})
    return state


def save_state(state: Dict[str, Dict[str, str]]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def telegram_notify(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID; skipping notification.")
        print(text)
        return

    # TELEGRAM_CHAT_ID may be a single chat/group ID or a comma-separated list of them,
    # so this one bot can notify a group chat and/or several individual chats.
    chat_ids = [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]

    errors = []
    for chat_id in chat_ids:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            timeout=20,
            data={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            },
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            errors.append(f"{chat_id}: {e}")

    if errors:
        raise RuntimeError("Telegram send failed for: " + "; ".join(errors))


def display_name(key: str) -> str:
    return key.split(" :: ", 1)[-1]


# Above this many newly-opened items, naming each one gets too long to glance at or
# have read aloud, so switch to a count-per-category summary instead.
MAX_NAMED_ITEMS = 6


def build_notification_text(newly_open_lifts: List[str], newly_open_trails: List[str], newly_open_gates: List[str]) -> str:
    total = len(newly_open_lifts) + len(newly_open_trails) + len(newly_open_gates)
    if total <= MAX_NAMED_ITEMS:
        names = [display_name(k) for k in newly_open_lifts + newly_open_trails + newly_open_gates]
        return "Open: " + ", ".join(names)

    def count_label(items: List[str], noun: str) -> Optional[str]:
        if not items:
            return None
        return f"{len(items)} {noun}{'s' if len(items) != 1 else ''}"

    counts = [
        c for c in (
            count_label(newly_open_lifts, "lift"),
            count_label(newly_open_trails, "trail"),
            count_label(newly_open_gates, "gate"),
        ) if c
    ]
    return "Open: " + ", ".join(counts)


def main():
    if os.environ.get("FORCE_TEST_NOTIFY", "").lower() in ("1", "true"):
        telegram_notify("Snowbasin test ✅")
        print("Test notification sent.")
        return

    lines = fetch_lines()

    lifts, trails, gates = parse_report(lines)

    if DEBUG:
        print(f"Total lines: {len(lines)}")
        print(f"Parsed lifts: {len(lifts)}")
        print(f"Parsed trails (excluding gates): {len(trails)}")
        print(f"Parsed gates: {len(gates)}")
        print("Sample parsed lifts:", list(lifts.items())[:5])
        print("Sample parsed trails:", list(trails.items())[:5])
        print("Sample parsed gates:", list(gates.items())[:10])

    current = {"lifts": lifts, "trails": trails, "gates": gates}

    prev = load_state()
    first_run = (not prev["lifts"] and not prev["trails"] and not prev["gates"])

    newly_open_lifts = sorted(k for k, v in lifts.items() if v == "Lift Open" and prev["lifts"].get(k) != v)
    newly_open_trails = sorted(k for k, v in trails.items() if v == "Trail Open" and prev["trails"].get(k) != v)
    newly_open_gates = sorted(
        k for k, v in gates.items()
        if v in ("Gate Open", "Trail Open", "Lift Open") and prev["gates"].get(k) != v
    )

    save_state(current)

    # If everything is empty, that usually means the runner got a different "shell" page / blocked page,
    # or the site's markup changed again and the parser needs another look.
    if len(lifts) == 0 and len(trails) == 0 and len(gates) == 0:
        raise RuntimeError("Parsing returned 0 lifts, 0 trails, and 0 gates — runner may be receiving different HTML.")

    if first_run:
        print("Initialized state; no notification on first run.")
        return

    if not newly_open_lifts and not newly_open_trails and not newly_open_gates:
        print("No new opens.")
        return

    telegram_notify(build_notification_text(newly_open_lifts, newly_open_trails, newly_open_gates))
    print("Notification sent.")


if __name__ == "__main__":
    main()
