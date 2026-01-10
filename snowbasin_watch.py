import json
import os
import re
from typing import Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

URL = "https://www.snowbasin.com/the-mountain/mountain-report/"
STATE_FILE = os.environ.get("STATE_FILE", "snowbasin_state.json")

PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")

# Make it look more like a browser (sometimes helps with anti-bot / alternate markup)
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
DEBUG = os.environ.get("DEBUG", "0") == "1"

LIFT_MARKERS = [
    ("Open", "Lift Open"),
    ("Closed", "Lift Closed"),
    ("On Hold", "Lift On Hold"),
    ("Scheduled", "Lift Scheduled"),
    ("Delayed", "Lift Delayed"),
]
TRAIL_MARKERS = [
    ("Open", "Trail Open"),
    ("Closed", "Trail Closed"),
    ("Expected", "Trail Expected"),
    ("Delayed", "Trail Delayed"),
]

GATE_NAME_REGEX = re.compile(r".*\bGate$", re.IGNORECASE)
GATE_SPECIAL_NAMES = {"The Wallow"}  # shows up in Access Gates list  [oai_citation:1‡Snowbasin Resort](https://www.snowbasin.com/the-mountain/mountain-report/)


def normalize_text(s: str) -> str:
    # Normalize non-breaking spaces and a couple other common “special spaces”
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


def parse_items_from_full_page(lines: List[str], kind: str) -> List[Tuple[str, str, str]]:
    """
    Returns list of (group, name, status) parsed from whole page.
    Uses 'Toggle accordion' headers for grouping.  [oai_citation:2‡Snowbasin Resort](https://www.snowbasin.com/the-mountain/mountain-report/)
    """
    markers = LIFT_MARKERS if kind == "lifts" else TRAIL_MARKERS
    items: List[Tuple[str, str, str]] = []

    group = "Unknown"

    for ln in lines:
        if "Toggle accordion" in ln:
            group = ln.replace("Toggle accordion", "").strip()
            continue

        found: Tuple[str, int, str] | None = None
        for status, token in markers:
            idx = ln.rfind(token)
            if idx != -1 and (found is None or idx > found[1]):
                found = (status, idx, token)

        if not found:
            continue

        status, idx, token = found
        name = ln[:idx].strip()
        items.append((group, name, status))

    return items


def is_gate_row(group: str, name: str) -> bool:
    if GATE_NAME_REGEX.match(name):
        return True
    if name in GATE_SPECIAL_NAMES:
        return True
    if "access gates" in group.lower():
        return True
    return False


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


def pushover_notify(title: str, message: str) -> None:
    if not PUSHOVER_USER or not PUSHOVER_TOKEN:
        print("Missing PUSHOVER_USER/PUSHOVER_TOKEN; skipping push.")
        print(title)
        print(message)
        return

    resp = requests.post(
        "https://api.pushover.net/1/messages.json",
        timeout=20,
        data={
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "title": title,
            "message": message,
            "url": URL,
            "url_title": "Snowbasin Mountain Report",
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

    # DEBUG: prove we’re seeing the “Lift …” and “Trail …” rows in GitHub Actions logs
    if DEBUG:
        lift_like = [ln for ln in lines if "Lift " in ln or " Lift" in ln][:12]
        trail_like = [ln for ln in lines if "Trail " in ln or " Trail" in ln][:12]
        print(f"Total lines: {len(lines)}")
        print("Sample lines containing 'Lift':")
        for ln in lift_like:
            print("  ", ln)
        print("Sample lines containing 'Trail':")
        for ln in trail_like:
            print("  ", ln)

    lift_items = parse_items_from_full_page(lines, "lifts")
    trail_like_items = parse_items_from_full_page(lines, "trails")

    lifts: Dict[str, str] = {}
    trails: Dict[str, str] = {}
    gates: Dict[str, str] = {}

    for group, name, status in lift_items:
        lifts[f"{group} :: {name}"] = status

    for group, name, status in trail_like_items:
        key = f"{group} :: {name}"
        if is_gate_row(group, name):
            gates[key] = status
        else:
            trails[key] = status

    if DEBUG:
        print(f"Parsed lifts: {len(lifts)}")
        print(f"Parsed trails (excluding gates): {len(trails)}")
        print(f"Parsed gates: {len(gates)}")
        print("Sample parsed lifts:", list(lifts.items())[:5])
        print("Sample parsed trails:", list(trails.items())[:5])
        print("Sample parsed gates:", list(gates.items())[:10])

    current = {"lifts": lifts, "trails": trails, "gates": gates}

    prev = load_state()
    first_run = (not prev["lifts"] and not prev["trails"] and not prev["gates"])

    newly_open_lifts = sorted(k for k, v in lifts.items() if v == "Open" and prev["lifts"].get(k) != "Open")
    newly_open_trails = sorted(k for k, v in trails.items() if v == "Open" and prev["trails"].get(k) != "Open")
    newly_open_gates = sorted(k for k, v in gates.items() if v == "Open" and prev["gates"].get(k) != "Open")

    save_state(current)

    # If everything is empty, that usually means the runner got a different “shell” page / blocked page.
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
        parts.append("New trails open:" + fmt(newly_open_trails))
    if newly_open_gates:
        parts.append("New access gates open:" + fmt(newly_open_gates))

    pushover_notify("Snowbasin update ✅ something opened", "\n\n".join(parts))
    print("Notification sent.")


if __name__ == "__main__":
    main()