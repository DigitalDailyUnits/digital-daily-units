#!/usr/bin/env python3
"""Digital Daily Units updater.

Downloads the iTunes sales chart for every country Apple publishes one for,
makes sure every song has cover art, tracks chart history, estimates each
song's sales for the last 24 hours, and writes the site's data files.

Usage (the GitHub workflow runs this every 30 minutes):
    python fetch_charts.py --store store --out site/data [--legacy history.json]

  --store   folder that keeps history between runs (saved to the "data" branch)
  --out     folder the site reads: index.json plus one file per country
  --legacy  the old single history.json, imported once so existing history carries over

Per country, in order of preference:
  1. iTunes Store top songs with explicit=true (the full sales chart)
  2. iTunes Store top songs, default (hides explicit songs)
If both fail, the last good copy of that country's chart is kept.

Cover art, in order of preference: the chart feed, the artwork remembered from
an earlier run, then Apple's lookup service by the song's iTunes id. If all
three fail, the page draws a placeholder cover.

Uses only the Python standard library.
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HEADERS = {"User-Agent": "Mozilla/5.0 (Digital Daily Units updater)"}
UPDATE_MINUTES = [7, 37]      # must match the minutes in .github/workflows/update.yml
KEEP_DAYS_OF_POSITIONS = 10   # daily positions remembered per song
FORGET_AFTER_DAYS = 90        # songs unseen this long are dropped from history
PAUSE_BETWEEN_COUNTRIES = 0.25  # seconds, to go easy on Apple's servers

# ---- sales estimate settings ----
# A song's estimated sales for the last 24 hours =
#   demand factor × country scale × (NOW_WEIGHT × curve(current position) + (1 - NOW_WEIGHT) × average curve over the last 24 hours)
# where curve(position) = CURVE_A × position^-CURVE_B. A song on the chart never shows less than 1.
CURVE_A = 4636
CURVE_B = 0.68
NOW_WEIGHT = 0.65             # how much the current position counts versus the last 24 hours
OFF_CHART_POSITION = 110      # position assumed for times a song was outside the chart
MIN_SCALE = 0.001             # smallest market size used, so tiny stores still show a spread of numbers
# Relative demand by local weekday (Monday first). New music comes out on Fridays.
WEEKDAY_DEMAND = [0.97, 0.95, 0.95, 0.98, 1.08, 1.06, 1.00]
DAILY_SWING = 0.025           # ± change over the day, highest in the late evening

# ---- countries ----
# key: used in the site's links (#us, #fr ...); code: Apple's storefront code; scale: market size
# compared with the US (1.0, a rough estimate); utc: rough local time offset for the daily pattern.
# main=True puts the country in the "Main countries" tabs.
COUNTRIES = [
    # key, code, name, scale, utc, main
    ("us", "us", "United States", 1.0, -5, True),
    ("ca", "ca", "Canada", 0.11, -5, True),
    ("uk", "gb", "United Kingdom", 0.14, 0, True),
    ("au", "au", "Australia", 0.06, 10, True),
    ("br", "br", "Brazil", 0.05, -3, True),
    ("ae", "ae", "United Arab Emirates", 0.006, 4, False),
    ("ag", "ag", "Antigua and Barbuda", 0.0005, -4, False),
    ("ai", "ai", "Anguilla", 0.0003, -4, False),
    ("am", "am", "Armenia", 0.001, 4, False),
    ("ar", "ar", "Argentina", 0.008, -3, False),
    ("at", "at", "Austria", 0.01, 1, False),
    ("az", "az", "Azerbaijan", 0.001, 4, False),
    ("bb", "bb", "Barbados", 0.0006, -4, False),
    ("be", "be", "Belgium", 0.012, 1, False),
    ("bf", "bf", "Burkina Faso", 0.0004, 0, False),
    ("bg", "bg", "Bulgaria", 0.002, 2, False),
    ("bh", "bh", "Bahrain", 0.001, 3, False),
    ("bm", "bm", "Bermuda", 0.0005, -4, False),
    ("bn", "bn", "Brunei", 0.0006, 8, False),
    ("bo", "bo", "Bolivia", 0.001, -4, False),
    ("bs", "bs", "Bahamas", 0.0006, -5, False),
    ("bw", "bw", "Botswana", 0.0005, 2, False),
    ("by", "by", "Belarus", 0.0015, 3, False),
    ("bz", "bz", "Belize", 0.0004, -6, False),
    ("ch", "ch", "Switzerland", 0.015, 1, False),
    ("cl", "cl", "Chile", 0.006, -4, False),
    ("co", "co", "Colombia", 0.006, -5, False),
    ("cr", "cr", "Costa Rica", 0.0015, -6, False),
    ("cv", "cv", "Cape Verde", 0.0003, -1, False),
    ("cy", "cy", "Cyprus", 0.001, 2, False),
    ("cz", "cz", "Czech Republic", 0.004, 1, False),
    ("de", "de", "Germany", 0.10, 1, False),
    ("dk", "dk", "Denmark", 0.009, 1, False),
    ("dm", "dm", "Dominica", 0.0003, -4, False),
    ("do", "do", "Dominican Republic", 0.0015, -4, False),
    ("ec", "ec", "Ecuador", 0.0015, -5, False),
    ("ee", "ee", "Estonia", 0.001, 2, False),
    ("eg", "eg", "Egypt", 0.003, 2, False),
    ("es", "es", "Spain", 0.03, 1, False),
    ("fi", "fi", "Finland", 0.006, 2, False),
    ("fj", "fj", "Fiji", 0.0004, 12, False),
    ("fm", "fm", "Micronesia", 0.0002, 10, False),
    ("fr", "fr", "France", 0.07, 1, False),
    ("gd", "gd", "Grenada", 0.0003, -4, False),
    ("gh", "gh", "Ghana", 0.001, 0, False),
    ("gm", "gm", "Gambia", 0.0003, 0, False),
    ("gr", "gr", "Greece", 0.004, 2, False),
    ("gt", "gt", "Guatemala", 0.0015, -6, False),
    ("gw", "gw", "Guinea-Bissau", 0.0002, 0, False),
    ("hk", "hk", "Hong Kong", 0.006, 8, False),
    ("hn", "hn", "Honduras", 0.001, -6, False),
    ("hu", "hu", "Hungary", 0.004, 1, False),
    ("id", "id", "Indonesia", 0.01, 7, False),
    ("ie", "ie", "Ireland", 0.008, 0, False),
    ("il", "il", "Israel", 0.005, 2, False),
    ("in", "in", "India", 0.02, 5.5, False),
    ("it", "it", "Italy", 0.03, 1, False),
    ("jo", "jo", "Jordan", 0.001, 3, False),
    ("jp", "jp", "Japan", 0.08, 9, False),
    ("ke", "ke", "Kenya", 0.001, 3, False),
    ("kg", "kg", "Kyrgyzstan", 0.0005, 6, False),
    ("kh", "kh", "Cambodia", 0.0008, 7, False),
    ("kn", "kn", "Saint Kitts and Nevis", 0.0003, -4, False),
    ("ky", "ky", "Cayman Islands", 0.0005, -5, False),
    ("kz", "kz", "Kazakhstan", 0.002, 5, False),
    ("la", "la", "Laos", 0.0004, 7, False),
    ("lb", "lb", "Lebanon", 0.001, 2, False),
    ("lk", "lk", "Sri Lanka", 0.001, 5.5, False),
    ("lt", "lt", "Lithuania", 0.0015, 2, False),
    ("lu", "lu", "Luxembourg", 0.0015, 1, False),
    ("lv", "lv", "Latvia", 0.001, 2, False),
    ("md", "md", "Moldova", 0.0005, 2, False),
    ("mn", "mn", "Mongolia", 0.0006, 8, False),
    ("mo", "mo", "Macau", 0.001, 8, False),
    ("mt", "mt", "Malta", 0.0008, 1, False),
    ("mu", "mu", "Mauritius", 0.0005, 4, False),
    ("mx", "mx", "Mexico", 0.04, -6, False),
    ("my", "my", "Malaysia", 0.007, 8, False),
    ("mz", "mz", "Mozambique", 0.0004, 2, False),
    ("na", "na", "Namibia", 0.0004, 2, False),
    ("ne", "ne", "Niger", 0.0002, 1, False),
    ("ng", "ng", "Nigeria", 0.003, 1, False),
    ("ni", "ni", "Nicaragua", 0.0006, -6, False),
    ("nl", "nl", "Netherlands", 0.025, 1, False),
    ("no", "no", "Norway", 0.01, 1, False),
    ("np", "np", "Nepal", 0.0005, 5.75, False),
    ("nz", "nz", "New Zealand", 0.012, 12, False),
    ("om", "om", "Oman", 0.001, 4, False),
    ("pa", "pa", "Panama", 0.001, -5, False),
    ("pe", "pe", "Peru", 0.004, -5, False),
    ("pg", "pg", "Papua New Guinea", 0.0003, 10, False),
    ("ph", "ph", "Philippines", 0.012, 8, False),
    ("pl", "pl", "Poland", 0.012, 1, False),
    ("pt", "pt", "Portugal", 0.006, 0, False),
    ("py", "py", "Paraguay", 0.001, -4, False),
    ("qa", "qa", "Qatar", 0.002, 3, False),
    ("ro", "ro", "Romania", 0.004, 2, False),
    ("ru", "ru", "Russia", 0.02, 3, False),
    ("sa", "sa", "Saudi Arabia", 0.006, 3, False),
    ("se", "se", "Sweden", 0.015, 1, False),
    ("sg", "sg", "Singapore", 0.006, 8, False),
    ("si", "si", "Slovenia", 0.001, 1, False),
    ("sk", "sk", "Slovakia", 0.002, 1, False),
    ("sv", "sv", "El Salvador", 0.001, -6, False),
    ("sz", "sz", "Eswatini", 0.0003, 2, False),
    ("th", "th", "Thailand", 0.008, 7, False),
    ("tj", "tj", "Tajikistan", 0.0004, 5, False),
    ("tm", "tm", "Turkmenistan", 0.0003, 5, False),
    ("tr", "tr", "Turkey", 0.006, 3, False),
    ("tt", "tt", "Trinidad and Tobago", 0.0008, -4, False),
    ("tw", "tw", "Taiwan", 0.006, 8, False),
    ("ua", "ua", "Ukraine", 0.003, 2, False),
    ("ug", "ug", "Uganda", 0.0005, 3, False),
    ("uz", "uz", "Uzbekistan", 0.0008, 5, False),
    ("ve", "ve", "Venezuela", 0.001, -4, False),
    ("vg", "vg", "British Virgin Islands", 0.0003, -4, False),
    ("vn", "vn", "Vietnam", 0.004, 7, False),
    ("za", "za", "South Africa", 0.008, 2, False),
    ("zw", "zw", "Zimbabwe", 0.0004, 2, False),
]


# ---------------------------------------------------------------- downloading

def get_json(url, attempts=3):
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # network hiccup or rate limit: wait and try again
            last = exc
            time.sleep(2 * (i + 1))
    raise last


def parse_sales_chart(j):
    entries = j["feed"].get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    songs = []
    for e in entries:
        images = e.get("im:image") or []
        art = images[-1]["label"] if images else ""
        song_id = ((e.get("id") or {}).get("attributes") or {}).get("im:id", "")
        songs.append({"id": song_id, "artist": e["im:artist"]["label"], "title": e["im:name"]["label"], "art": art})
    return songs, j["feed"]["updated"]["label"]


def fetch_country(code):
    for url in (f"https://itunes.apple.com/{code}/rss/topsongs/limit=100/explicit=true/json",
                f"https://itunes.apple.com/{code}/rss/topsongs/limit=100/json"):
        try:
            songs, chart_time = parse_sales_chart(get_json(url))
            if songs:
                return {"source": "sales", "chart_time": chart_time, "songs": songs}
        except Exception as exc:  # network error, bad JSON, changed layout
            print(f"  sales chart failed for {code}: {exc}", file=sys.stderr)
    return None


# ---------------------------------------------------------------- cover art

def song_key(song):
    return (song["artist"] + "|" + song["title"]).lower()


def lookup_artwork(ids, code):
    """Ask Apple's lookup service for cover art by iTunes id. Returns {id: art_url}."""
    found = {}
    ids = [i for i in ids if i]
    for start in range(0, len(ids), 150):
        batch = ids[start:start + 150]
        try:
            j = get_json(f"https://itunes.apple.com/lookup?id={','.join(batch)}&country={code}")
            for r in j.get("results", []):
                art = r.get("artworkUrl100") or r.get("artworkUrl60") or ""
                if art and r.get("trackId"):
                    found[str(r["trackId"])] = art
        except Exception as exc:
            print(f"  artwork lookup failed for {code}: {exc}", file=sys.stderr)
    return found


def fill_artwork(songs, market_hist, code):
    """Make sure every song has an art URL where Apple has one. Modifies songs in place."""
    store = market_hist.get("songs", {})
    for song in songs:
        if not song["art"]:
            remembered = store.get(song_key(song), {}).get("art")
            if remembered:
                song["art"] = remembered
    missing = [s["id"] for s in songs if not s["art"] and s["id"]]
    if missing:
        found = lookup_artwork(missing, code)
        for song in songs:
            if not song["art"] and song["id"] in found:
                song["art"] = found[song["id"]]


# ---------------------------------------------------------------- history and sales

def day_diff(a, b):
    return (datetime.strptime(a, "%Y-%m-%d") - datetime.strptime(b, "%Y-%m-%d")).days


def curve(position):
    return CURVE_A * position ** -CURVE_B


def demand_factor(now, utc_offset):
    """Smooth day-of-week and time-of-day pattern, in the country's local time."""
    local = now + timedelta(hours=utc_offset)
    pos = local.weekday() + (local.hour + local.minute / 60) / 24 - 0.5
    i = math.floor(pos) % 7
    frac = pos - math.floor(pos)
    weekly = WEEKDAY_DEMAND[i] * (1 - frac) + WEEKDAY_DEMAND[(i + 1) % 7] * frac
    hour = local.hour + local.minute / 60
    daily = 1 + DAILY_SWING * math.cos(2 * math.pi * (hour - 22) / 24)
    return weekly * daily


def apply_history(market_hist, songs, today, now, scale=1.0, utc_offset=0):
    """Update one country's history with the latest chart and return the songs with movement and sales.

    market_hist: {"dates": [...], "snaps": [...], "songs": {key: record}}  (modified in place)
    """
    dates = market_hist.setdefault("dates", [])
    store = market_hist.setdefault("songs", {})
    now_min = int(now.timestamp() // 60)
    cutoff = now_min - 24 * 60
    snaps = [s for s in market_hist.get("snaps", []) if s > cutoff and s != now_min] + [now_min]
    market_hist["snaps"] = snaps
    factor = demand_factor(now, utc_offset) * max(scale, MIN_SCALE)

    earlier = [d for d in dates if d < today]
    prev_chart_day = earlier[-1] if earlier else None
    if today not in dates:
        dates.append(today)
        dates.sort()
    del dates[:-KEEP_DAYS_OF_POSITIONS]

    result = []
    for index, song in enumerate(songs):
        position = index + 1
        key = song_key(song)
        rec = store.get(key)
        is_new = rec is None
        if is_new:
            rec = {"first": today, "peak": position, "peak_date": today, "days": 0, "last_date": None, "pos": {}}
            store[key] = rec
        if rec["last_date"] != today:
            rec["days"] += 1
        previous = rec["pos"].get(prev_chart_day) if prev_chart_day else None
        rec["pos"][today] = position
        rec["last_date"] = today
        if song.get("art"):
            rec["art"] = song["art"]
        if position < rec["peak"]:
            rec["peak"], rec["peak_date"] = position, today
        for old in sorted(rec["pos"])[:-KEEP_DAYS_OF_POSITIONS]:
            del rec["pos"][old]
        recent = {m: p for m, p in rec.get("recent", []) if m > cutoff and m != now_min}
        recent[now_min] = position
        rec["recent"] = sorted(recent.items())
        average = sum(curve(recent.get(s, OFF_CHART_POSITION)) for s in snaps) / len(snaps)
        sales = factor * (NOW_WEIGHT * curve(position) + (1 - NOW_WEIGHT) * average)
        result.append({
            "artist": song["artist"],
            "title": song["title"],
            "sales": max(1, int(round(sales))),
            "art": song.get("art") or rec.get("art", ""),
            "prev": previous,
            "peak": rec["peak"],
            "days": rec["days"],
            "new": bool(is_new or (prev_chart_day is None and rec["days"] == 1)),
            "re": bool(previous is None and not is_new and rec["days"] > 1),
        })

    for key in [k for k, r in store.items() if r["last_date"] and day_diff(today, r["last_date"]) > FORGET_AFTER_DAYS]:
        del store[key]
    for r in store.values():  # keep the files small: drop positions older than 24 hours
        if r.get("recent"):
            r["recent"] = [x for x in r["recent"] if x[0] > cutoff]
            if not r["recent"]:
                del r["recent"]
    return result


# ---------------------------------------------------------------- files

def load_json(path, default):
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--store", default="store", help="folder that keeps history between runs")
    ap.add_argument("--out", default="site/data", help="folder the site reads")
    ap.add_argument("--legacy", default="", help="old single history.json to import once")
    ap.add_argument("--only", default="", help="comma-separated country keys, for testing")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    only = {k.strip() for k in args.only.split(",") if k.strip()}
    legacy = load_json(args.legacy, {}) if args.legacy else {}

    index_countries = []
    got = 0
    for key, code, name, scale, utc, main_country in COUNTRIES:
        if only and key not in only:
            continue
        hist_path = os.path.join(args.store, "history", key + ".json")
        last_path = os.path.join(args.store, "last", key + ".json")
        market_hist = load_json(hist_path, None)
        if market_hist is None:
            market_hist = legacy.get(key, {})  # carry over history from the old setup, if any
            if market_hist:
                print(f"{key}: imported existing history")

        print(f"{key}: fetching")
        result = fetch_country(code)
        if result:
            got += 1
            fill_artwork(result["songs"], market_hist, code)
            result["songs"] = apply_history(market_hist, result["songs"], today, now, scale, utc)
            result["fetched_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            save_json(hist_path, market_hist, compact=True)
            save_json(last_path, result, compact=True)
            print(f"  ok ({len(result['songs'])} songs)")
        else:
            result = load_json(last_path, None)
            print("  keeping previous copy" if result else "  no data yet", file=sys.stderr)

        if result:
            result.update({"key": key, "name": name})
            save_json(os.path.join(args.out, key + ".json"), result, compact=True)
            index_countries.append({
                "key": key, "code": code, "name": name, "main": main_country,
                "songs": len(result["songs"]), "chart_time": result.get("chart_time"),
                "fetched_at": result.get("fetched_at"),
            })
        time.sleep(PAUSE_BETWEEN_COUNTRIES)

    if not got:
        print("no charts could be downloaded this run", file=sys.stderr)
        if not index_countries:
            return 1

    times = [c["fetched_at"] for c in index_countries if c.get("fetched_at")]
    index = {
        "fetched_at": max(times) if times else now.strftime("%Y-%m-%dT%H:%M:%SZ"),  # newest real download
        "update_minutes": UPDATE_MINUTES,
        "countries": index_countries,
    }
    save_json(os.path.join(args.out, "index.json"), index)
    print(f"done: {got} of {len(index_countries)} countries updated this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
