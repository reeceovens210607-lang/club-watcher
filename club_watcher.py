"""Club Watcher - alerts you when Exeter club tickets drop.

Watches Timepiece (Fixr), Fever (Fatsoma + Fixr) and Cavern (Skiddle).
Every few minutes it checks each page and pops up a notification when:
  - a new event appears
  - a new ticket tier goes on sale (or a sold-out tier comes back)
  - Fatsoma shows a tier scheduled to go on sale in the future (heads-up)
Every drop is logged to drops.csv so you can see each club's pattern.

Usage:
  python club_watcher.py          run forever (checks every CHECK_EVERY_MINS)
  python club_watcher.py once     do a single check and exit (or a boost run,
                                  if BOOST_HOURS is set or a boost window is on)
  python club_watcher.py report   show when each club tends to drop tickets
  python club_watcher.py test     send a test notification
"""

import csv
import datetime as dt
import html
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------- settings --

CHECK_EVERY_MINS = 3

# Boost: check every minute during these windows (UK time), when drops are
# likely. Each is (day or "Daily", start "HH:MM", hours, clubs to boost); other
# clubs keep their normal pace. A boost of every club can also be started any
# time from GitHub's "Run workflow" button.
BOOST_EVERY_SECS = 60
BOOST_WINDOWS = [
    ("Daily", "11:00", 5, ["Timepiece"]),
]

# The house's regular nights out. Alerts for these get a star and ntfy's
# "urgent" priority; other nights come through as normal notifications.
MAIN_NIGHTS = {
    "Fever": ["Mon"],
    "Cavern": ["Tue"],
    "Timepiece": ["Wed", "Fri"],
}

HERE = Path(__file__).resolve().parent

# Phone alerts: everyone installs the free "ntfy" app and subscribes to this
# topic. Anyone who knows the name can see the alerts, so it's kept out of the
# code: the laptop reads ntfy_topic.txt (never uploaded) and GitHub reads the
# NTFY_TOPIC secret.
_topic_file = HERE / "ntfy_topic.txt"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or (
    _topic_file.read_text().strip() if _topic_file.exists() else "")

# If the last check was longer ago than this (laptop was off, GitHub paused),
# quietly catch up instead of alerting about stale drops. Must stay under 12h,
# which is how long ntfy remembers alerts for de-duplication.
CATCH_UP_AFTER_HOURS = 6

SOURCES = [
    {"club": "Timepiece", "kind": "fixr_organiser", "slug": "timepiece"},
    {"club": "Timepiece", "kind": "fixr_venue", "id": 2783},
    {"club": "Fever", "kind": "fatsoma_page", "slug": "exeter-fever-3700589"},
    {"club": "Fever", "kind": "fixr_venue", "id": 2379},
    {"club": "Cavern", "kind": "skiddle_venue", "path": "Exeter/The-Cavern"},
]

STATE_FILE = HERE / "state.json"
DROPS_FILE = HERE / "drops.csv"
LOG_FILE = HERE / "watcher.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


# ------------------------------------------------------------------ helpers --

def log(msg, level=None):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    if level and os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::{msg}", flush=True)  # shows on the run's summary page
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url, accept="text/html"):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": accept, "Accept-Language": "en-GB"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def next_data(page):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        raise ValueError("page layout changed (no __NEXT_DATA__)")
    return json.loads(m.group(1))["props"]["pageProps"]


def fmt_time(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%a %d %b %H:%M")


def fmt_iso(s):
    return dt.datetime.fromisoformat(s).astimezone().strftime("%a %d %b %H:%M")


# ---------------------------------------------------------------- scrapers --
# Each returns {event_key: {"name", "when", "day", "url", "tickets": {id: {...}}}}
# where a ticket is {"name", "status", "on_sale_at"(optional)}.
# status is one of: available, sold_out, scheduled, off_sale

_fixr_seen = {}  # event id -> tickets, so TP's organiser and venue pages share one lookup


def fixr_event_tickets(event_id):
    if event_id in _fixr_seen:
        return _fixr_seen[event_id]
    d = json.loads(fetch(f"https://api.fixr.co/api/v2/app/event/{event_id}",
                         accept="application/json"))
    tickets = {}
    for t in d.get("tickets", []):
        if t.get("expired"):
            status = "off_sale"
        elif t.get("sold_out"):
            status = "sold_out"
        else:
            status = "available"
        tickets[str(t["id"])] = {"name": t["name"], "status": status}
    _fixr_seen[event_id] = tickets
    return tickets


def fixr_events(raw_events):
    out = {}
    for e in raw_events:
        out[f"fixr:{e['id']}"] = {
            "name": e["name"],
            "when": fmt_time(e["openTime"]),
            "day": dt.datetime.fromtimestamp(e["openTime"]).strftime("%a"),
            "url": f"https://fixr.co/event/{e['routingPart']}",
            "tickets": fixr_event_tickets(e["id"]),
        }
        time.sleep(1)
    return out


def scrape_fixr_organiser(src):
    pp = next_data(fetch(f"https://fixr.co/organiser/{src['slug']}"))
    return fixr_events(pp["data"]["data"])


def scrape_fixr_venue(src):
    pp = next_data(fetch(f"https://fixr.co/venue/{src['id']}"))
    return fixr_events(pp["venue"]["events"])


def scrape_fatsoma_page(src):
    page = fetch(f"https://www.fatsoma.com/p/{src['slug']}")
    blobs = re.findall(r'<script type="fastboot/shoebox" id="([^"]+)">(.*?)</script>',
                       page, re.S)
    raw = next((b for i, b in blobs if "events-filter" in i), None)
    if raw is None:
        raise ValueError("page layout changed (no events data)")
    d = json.loads(raw)
    while isinstance(d, str):
        d = json.loads(d)
    tickets_by_id = {t["id"]: t["attributes"] for t in d.get("included", [])
                     if t["type"] == "ticket-options"}
    now = dt.datetime.now(dt.timezone.utc)
    out = {}
    for e in d["data"]:
        a = e["attributes"]
        tickets = {}
        for ref in e["relationships"]["ticket-options"]["data"]:
            t = tickets_by_id.get(ref["id"])
            if not t or not t.get("visible", True):
                continue
            on_sale_at = t.get("on-sale-at")
            s = t.get("on-sale-status", "")
            if s == "sold_out":
                status = "sold_out"
            elif s == "available":
                status = "available"
            elif on_sale_at and dt.datetime.fromisoformat(on_sale_at) > now:
                status = "scheduled"
            else:
                status = "off_sale"
            tickets[ref["id"]] = {"name": t["name"], "status": status,
                                  "on_sale_at": on_sale_at}
        out[f"fatsoma:{e['id']}"] = {
            "name": a["name"],
            "when": fmt_iso(a["starts-at"]),
            "day": dt.datetime.fromisoformat(a["starts-at"]).astimezone().strftime("%a"),
            "url": f"https://www.fatsoma.com/e/{a['vanity-name']}/{a.get('seo-name') or ''}",
            "tickets": tickets,
        }
    return out


def scrape_skiddle_venue(src):
    pp = next_data(fetch(f"https://www.skiddle.com/whats-on/{src['path']}/"))
    out = {}
    for e in pp["eventsData"]:
        if str(e.get("cancelled")) == "1":
            continue
        text = e.get("ticketStatusText", "")
        if str(e.get("ticketStatus")) == "2":
            status = "available"
        elif "sold" in text.lower() or "waiting" in text.lower():
            status = "sold_out"
        else:
            status = "off_sale"
        link = e.get("link") or ""
        out[f"skiddle:{e['id']}"] = {
            "name": e["eventname"],
            "when": e.get("date", ""),
            "day": dt.date.fromisoformat(e["date"]).strftime("%a") if e.get("date") else "",
            "url": "https://www.skiddle.com" + link if link.startswith("/") else link,
            "tickets": {"main": {"name": text or "Tickets", "status": status}},
        }
    return out


SCRAPERS = {
    "fixr_organiser": scrape_fixr_organiser,
    "fixr_venue": scrape_fixr_venue,
    "fatsoma_page": scrape_fatsoma_page,
    "skiddle_venue": scrape_skiddle_venue,
}


# ----------------------------------------------------------- notifications --

def already_sent(title, message):
    """The laptop and GitHub both watch, so check whether the other one has
    already sent this exact alert in the last 12 hours."""
    try:
        history = fetch(f"https://ntfy.sh/{NTFY_TOPIC}/json?poll=1&since=12h",
                        accept="application/json")
    except Exception:
        return False
    for line in history.splitlines():
        m = json.loads(line)
        if m.get("title") == title and m.get("message") == message:
            return True
    return False


def notify(title, message, url=None, main_night=False):
    title = title.encode("ascii", "ignore").decode()  # ntfy titles must be plain text
    log(f"ALERT  {'[MAIN NIGHT] ' if main_night else ''}{title} - {message}")
    if sys.platform == "win32":
        _toast(("⭐ " if main_night else "") + title, message, url)
    if NTFY_TOPIC:
        if already_sent(title, message):
            log("  (phone alert already sent by the other watcher)")
            return
        try:
            headers = {"Title": title,
                       "Priority": "urgent" if main_night else "default",
                       "Tags": "star,tickets" if main_night else "tickets"}
            if url:
                headers["Click"] = url
                headers["Actions"] = f"view, Get tickets, {url}"
            req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}",
                                         data=message.encode("utf-8"), headers=headers)
            urllib.request.urlopen(req, timeout=15)
        except Exception as ex:
            log(f"phone alert failed: {ex}")


def _toast(title, message, url):
    x = lambda s: html.escape(s or "", quote=True)
    launch = f' launch="{x(url)}" activationType="protocol"' if url else ""
    open_btn = (f'<action content="Open tickets" arguments="{x(url)}" activationType="protocol"/>'
                if url else "")
    xml = (f'<toast{launch} scenario="reminder"><visual><binding template="ToastGeneric">'
           f'<text>{x(title)}</text><text>{x(message)}</text></binding></visual>'
           f'<actions>{open_btn}<action content="Dismiss" arguments="dismiss" '
           f'activationType="system"/></actions></toast>')
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:TOAST_XML);"
        "$t = [Windows.UI.Notifications.ToastNotification]::new($x);"
        "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show($t)"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       env={**os.environ, "TOAST_XML": xml},
                       creationflags=0x08000000, timeout=30)  # CREATE_NO_WINDOW
    except Exception as ex:
        log(f"desktop alert failed: {ex}")


# ------------------------------------------------------------- change check --

def record_drop(club, event, ticket_name, change, on_sale_at=""):
    new = not DROPS_FILE.exists()
    with open(DROPS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["detected_at", "club", "event", "event_date", "ticket",
                        "change", "on_sale_at"])
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"), club,
                    event["name"], event["when"], ticket_name, change, on_sale_at or ""])


def compare(club, old, new, first_run):
    for key, ev in new.items():
        prev = old.get(key)
        main = ev.get("day") in MAIN_NIGHTS.get(club, [])
        if prev is None:
            if first_run:
                continue
            on_sale = [t for t in ev["tickets"].values() if t["status"] == "available"]
            record_drop(club, ev, "", "new_event")
            notify(f"{club}: new event!",
                   f"{ev['name']} ({ev['when']})" +
                   (f" - {len(on_sale)} ticket type(s) on sale NOW" if on_sale else ""),
                   ev["url"], main)
            for t in on_sale:
                record_drop(club, ev, t["name"], "on_sale", t.get("on_sale_at"))
            for t in ev["tickets"].values():
                if t["status"] == "scheduled":
                    record_drop(club, ev, t["name"], "scheduled", t.get("on_sale_at"))
            continue

        for tid, t in ev["tickets"].items():
            before = prev["tickets"].get(tid, {}).get("status")
            now = t["status"]
            if now == before:
                continue
            if now == "available" and not first_run:
                change = "restock" if before == "sold_out" else "on_sale"
                record_drop(club, ev, t["name"], change, t.get("on_sale_at"))
                notify(f"{club}: tickets {'back' if change == 'restock' else 'live'}!",
                       f"{ev['name']} ({ev['when']}) - {t['name']}", ev["url"], main)
            elif now == "scheduled":
                record_drop(club, ev, t["name"], "scheduled", t.get("on_sale_at"))
                notify(f"{club}: drop scheduled",
                       f"{ev['name']} - {t['name']} goes on sale {fmt_iso(t['on_sale_at'])}",
                       ev["url"], main)
            elif now == "sold_out" and before == "available":
                record_drop(club, ev, t["name"], "sold_out")
                log(f"sold out: {club} {ev['name']} - {t['name']}")


def check(state, clubs=None):
    """Check every source, or only those for the given clubs."""
    _fixr_seen.clear()
    last = state.get("_last_check")
    stale = last is not None and time.time() - last > CATCH_UP_AFTER_HOURS * 3600
    if stale:
        log(f"last check was over {CATCH_UP_AFTER_HOURS}h ago - catching up quietly")
    for src in SOURCES:
        if clubs and src["club"] not in clubs:
            continue
        src_id = f"{src['kind']}:{src.get('slug') or src.get('id') or src.get('path')}"
        try:
            events = SCRAPERS[src["kind"]](src)
        except Exception as ex:
            log(f"couldn't check {src['club']} ({src_id}): {ex}", "warning")
            continue
        first_run = src_id not in state
        old = state.get(src_id, {})
        compare(src["club"], old, events, first_run or stale)
        # Keep events that vanished briefly so a relist isn't treated as new
        state[src_id] = {**{k: v for k, v in old.items() if k not in events}, **events}
        if first_run:
            log(f"started watching {src['club']} ({src_id}): {len(events)} upcoming event(s)",
                "notice")
        time.sleep(2)
    state["_last_check"] = time.time()
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def active_boost(now=None):
    """If we're inside one of the BOOST_WINDOWS, return (end time, clubs)."""
    now = now or dt.datetime.now()
    for day, start, hours, clubs in BOOST_WINDOWS:
        h, m = map(int, start.split(":"))
        # Look back far enough to catch a window that started before midnight
        if day == "Daily":
            candidates = (0, 1)
        else:
            d = (now.weekday() - time.strptime(day[:3], "%a").tm_wday) % 7
            candidates = (d, d + 7)
        for days_back in candidates:
            begin = (now - dt.timedelta(days=days_back)).replace(hour=h, minute=m, second=0,
                                                                  microsecond=0)
            end = begin + dt.timedelta(hours=hours)
            if begin <= now < end:
                return end, clubs
    return None


class Pacer:
    """During a boost, checks the boosted clubs every minute but still checks
    everything else only every CHECK_EVERY_MINS."""

    def __init__(self):
        self.last_full = 0

    def check(self, state, clubs):
        if clubs and time.time() - self.last_full < CHECK_EVERY_MINS * 60:
            check(state, clubs)
        else:
            check(state)
            self.last_full = time.time()


def boost(state, until, clubs=None):
    who = ", ".join(clubs) if clubs else "all clubs"
    log(f"BOOST ({who}) - checking every {BOOST_EVERY_SECS}s until {until:%a %H:%M}",
        "notice")
    pacer = Pacer()
    while True:
        pacer.check(state, clubs)
        if dt.datetime.now() + dt.timedelta(seconds=BOOST_EVERY_SECS) >= until:
            return
        time.sleep(BOOST_EVERY_SECS)


# ------------------------------------------------------------------- report --

def report():
    """Show when each club drops tickets, using drops.csv plus the release
    times Fatsoma publishes for tickets (so Fever has history from day one)."""
    times = defaultdict(list)  # club -> [datetime]
    seen = set()

    def add(club, event, when):
        key = (club, event, when.strftime("%Y%m%d%H"))
        if key not in seen:
            seen.add(key)
            times[club].append(when)

    if DROPS_FILE.exists():
        with open(DROPS_FILE, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["change"] not in ("new_event", "on_sale", "restock"):
                    continue
                when = (dt.datetime.fromisoformat(r["on_sale_at"]).astimezone()
                        if r["on_sale_at"] else dt.datetime.fromisoformat(r["detected_at"]))
                add(r["club"], r["event"], when.replace(tzinfo=None))
    for src_id, src_events in load_state().items():
        if not src_id.startswith("fatsoma_page:"):
            continue
        for ev in src_events.values():
            for t in ev["tickets"].values():
                if t.get("on_sale_at") and t["status"] != "scheduled":
                    when = dt.datetime.fromisoformat(t["on_sale_at"]).astimezone()
                    add("Fever", ev["name"], when.replace(tzinfo=None))

    if not times:
        print("No drops recorded yet - leave the watcher running for a week or two.")
        return
    for club, whens in sorted(times.items()):
        print(f"\n=== {club}: {len(whens)} release(s) recorded ===")
        days = Counter(w.strftime("%A") for w in whens)
        print("  By day:  " + ", ".join(f"{d} x{n}" for d, n in days.most_common()))
        hours = Counter(w.hour for w in whens)
        print("  By hour: " + ", ".join(f"{h:02d}:00 x{n}" for h, n in hours.most_common(5)))
        print("  Most recent:")
        for w in sorted(whens)[-8:]:
            print(f"    {w:%a %d %b %H:%M}")


# --------------------------------------------------------------------- main --

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "report":
        report()
    elif cmd == "test":
        notify("Club Watcher test", "If you can see this, alerts are working.",
               "https://fixr.co/organiser/timepiece")
    elif cmd == "once":
        # One check, or a run of checks if boosting (button or weekly window).
        # GitHub stops jobs after 6h, so a boost is capped at 5.5h per run.
        hours = float(os.environ.get("BOOST_HOURS") or 0)
        window = ((dt.datetime.now() + dt.timedelta(hours=hours), None) if hours
                  else active_boost())
        if window:
            until, clubs = window
            boost(load_state(), min(until, dt.datetime.now() + dt.timedelta(hours=5.5)), clubs)
        else:
            check(load_state())
    else:
        # Only one copy may run, or everyone gets every alert twice
        lock = socket.socket()
        try:
            lock.bind(("127.0.0.1", 47831))
        except OSError:
            print("Club Watcher is already running.")
            return
        log(f"Club Watcher running - checking every {CHECK_EVERY_MINS} mins. "
            "Close this window to stop.")
        state = load_state()
        pacer = Pacer()
        while True:
            window = active_boost()
            pacer.check(state, window[1] if window else None)
            if active_boost():
                time.sleep(BOOST_EVERY_SECS)
            else:
                time.sleep(CHECK_EVERY_MINS * 60 + random.randint(-20, 20))


if __name__ == "__main__":
    main()
