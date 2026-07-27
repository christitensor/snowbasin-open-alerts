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


def telegram_notify(title: str, message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID; skipping notification.")
        print(title)
        print(message)
        return

    text = f"{title}\n\n{message}\n\n{URL}"
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        timeout=20,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": "true",
        },
    )
    resp.raise_for_status()


def fmt(items: List[str], limit: int = 30) -> str:
    shown = items[:limit]
    more = len(items) - len(shown)
    msg = "\n- " + "\n- ".join(shown) if shown else ""
    if more > 0:
        msg += f"\n(+{more} more)"
    return msg


def main():
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

    parts = []
    if newly_open_lifts:
        parts.append("New lifts open:" + fmt(newly_open_lifts))
    if newly_open_trails:
        parts.append("New trails/runs open:" + fmt(newly_open_trails))
    if newly_open_gates:
        parts.append("New access gates open:" + fmt(newly_open_gates))

    telegram_notify("Snowbasin update: something opened", "\n\n".join(parts))
    print("Notification sent.")


if __name__ == "__main__":
    main()
