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

USER_AGENT = os.environ.get("USER_AGENT", "snowbasin-watch/1.0 (personal use)")

LIFT_MARKERS = [("Open", " Lift Open"), ("Closed", " Lift Closed"), ("On Hold", " Lift On Hold"),
                ("Scheduled", " Lift Scheduled"), ("Delayed", " Lift Delayed")]
TRAIL_MARKERS = [("Open", " Trail Open"), ("Closed", " Trail Closed"),
                 ("Expected", " Trail Expected"), ("Delayed", " Trail Delayed")]

def fetch_lines() -> List[str]:
    r = requests.get(URL, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln]

def slice_section(lines: List[str], start_key: str, end_key: str) -> List[str]:
    try:
        start = next(i for i, ln in enumerate(lines) if ln == start_key)
    except StopIteration:
        return []
    try:
        end = next(i for i, ln in enumerate(lines[start+1:], start+1) if ln == end_key)
    except StopIteration:
        end = len(lines)
    return lines[start:end]

def parse_rows(section_lines: List[str], kind: str) -> Dict[str, str]:
    markers = LIFT_MARKERS if kind == "lifts" else TRAIL_MARKERS
    out: Dict[str, str] = {}
    group = kind.capitalize()

    for ln in section_lines:
        if "Toggle accordion" in ln:
            group = ln.replace("Toggle accordion", "").strip()
            continue

        found: Tuple[str, int] | None = None
        for status, token in markers:
            idx = ln.rfind(token)
            if idx != -1 and (found is None or idx > found[1]):
                found = (status, idx)

        if not found:
            continue

        status, idx = found
        name = ln[:idx].strip()
        key = f"{group} :: {name}"
        out[key] = status

    return out

def load_state() -> Dict[str, Dict[str, str]]:
    if not os.path.exists(STATE_FILE):
        return {"lifts": {}, "trails": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

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

def main():
    lines = fetch_lines()

    lifts_section = slice_section(lines, "## Lifts", "## Trails")
    trails_section = slice_section(lines, "## Trails", "## Parking")

    current = {
        "lifts": parse_rows(lifts_section, "lifts"),
        "trails": parse_rows(trails_section, "trails"),
    }

    prev = load_state()
    first_run = (not prev["lifts"] and not prev["trails"])

    newly_open_lifts = sorted(k for k, v in current["lifts"].items() if v == "Open" and prev["lifts"].get(k) != "Open")
    newly_open_trails = sorted(k for k, v in current["trails"].items() if v == "Open" and prev["trails"].get(k) != "Open")

    save_state(current)

    if first_run:
        print("Initialized state; no notification on first run.")
        return

    if not newly_open_lifts and not newly_open_trails:
        print("No new opens.")
        return

    parts = []
    if newly_open_lifts:
        parts.append("New lifts open:\n- " + "\n- ".join(newly_open_lifts[:30]))
    if newly_open_trails:
        parts.append("New trails open:\n- " + "\n- ".join(newly_open_trails[:30]))

    pushover_notify("Snowbasin update ✅ something opened", "\n\n".join(parts))
    print("Notification sent.")

if __name__ == "__main__":
    main()
