#!/usr/bin/env python3
"""Digital Daily Units updater.

Downloads the iTunes sales charts for five storefronts, makes sure every song has
cover art, tracks chart history, estimates each song's sales for the last 24 hours,
and writes data.json for the site.

Per country, in order of preference:
  1. iTunes Store top songs with explicit=true (the full top 100 sales chart)
  2. iTunes Store top songs, default (hides explicit songs, so it can be shorter than 100)
  3. Apple Music most played (streaming chart), only if both sales feeds fail
If a country fails every source, its previous copy in data.json is kept.

Cover art, in order of preference:
  1. The artwork in the chart feed itself
  2. The artwork remembered from an earlier run (history.json)
  3. Apple's lookup service, asked by the song's iTunes id
If all three fail, the page draws a placeholder cover for that song.

history.json remembers, for every song seen: first day on chart, peak, days on
chart, recent daily positions and its artwork. Uses only the Python standard library.
"""
import json
import os
import sys
import math
import urllib.request
from datetime import datetime, timedelta, timezone

MARKETS = {"us": "us", "ca": "ca", "uk": "gb", "au": "au", "br": "br"}  # site key -> Apple storefront code

# ---- sales estimate settings ----
# A song's estimated sales for the last 24 hours =
#   demand factor × country scale × (NOW_WEIGHT × curve(current position) + (1 - NOW_WEIGHT) × average curve over the last 24 hours)
# where curve(position) = CURVE_A × position^-CURVE_B.
CURVE_A = 4636
CURVE_B = 0.68
NOW_WEIGHT = 0.65             # how much the current position counts versus the last 24 hours
OFF_CHART_POSITION = 110      # position assumed for times a song was outside the top 100
SCALE = {"us": 1.0, "ca": 0.11, "uk": 0.14, "au": 0.06, "br": 0.05}
UTC_OFFSET = {"us": -5, "ca": -5, "uk": 0, "au": 10, "br": -3}  # rough local time, used for the time-of-day pattern
# Relative demand by local weekday (Monday first). New music comes out on Fridays.
WEEKDAY_DEMAND = [0.97, 0.95, 0.95, 0.98, 1.08, 1.06, 1.00]
DAILY_SWING = 0.025           # ± change over the day, highest in the late evening
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data.json")
HISTORY = os.path.join(HERE, "history.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (Digital Daily Units updater)"}
UPDATE_MINUTES = [7, 37]      # must match the minutes in .github/workflows/update.yml
KEEP_DAYS_OF_POSITIONS = 10   # daily positions remembered per song
FORGET_AFTER_DAYS = 90        # songs unseen this long are dropped from history


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_sales_chart(j):
    entries = j["feed"]["entry"]
    if isinstance(entries, dict):
        entries = [entries]
    songs = []
    for e in entries:
        images = e.get("im:image") or []
        art = images[-1]["label"] if images else ""
        song_id = ((e.get("id") or {}).get("attributes") or {}).get("im:id", "")
        songs.append({"id": song_id, "artist": e["im:artist"]["label"], "title": e["im:name"]["label"], "art": art})
    return songs, j["feed"]["updated"]["label"]


def parse_streaming_chart(j):
    songs = [{"id": e.get("id", ""), "artist": e["artistName"], "title": e["name"], "art": e.get("artworkUrl100", "")}
             for e in j["feed"]["results"]]
    return songs, j["feed"]["updated"]


def fetch_market(code):
    attempts = [
        ("sales", f"https://itunes.apple.com/{code}/rss/topsongs/limit=100/explicit=true/json", parse_sales_chart),
        ("sales", f"https://itunes.apple.com/{code}/rss/topsongs/limit=100/json", parse_sales_chart),
        ("streaming", f"https://rss.marketingtools.apple.com/api/v2/{code}/music/most-played/100/songs.json", parse_streaming_chart),
    ]
    for source, url, parse in attempts:
        try:
            songs, chart_time = parse(get_json(url))
            if songs:
                return {"source": source, "chart_time": chart_time, "songs": songs}
        except Exception as exc:  # network error, bad JSON, changed layout
            print(f"  {source} chart failed for {code}: {exc}", file=sys.stderr)
    return None


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
    still_missing = sum(1 for s in songs if not s["art"])
    if still_missing:
        print(f"  {still_missing} songs without artwork (the page will draw a placeholder)", file=sys.stderr)


def day_diff(a, b):
    return (datetime.strptime(a, "%Y-%m-%d") - datetime.strptime(b, "%Y-%m-%d")).days


def curve(position):
    return CURVE_A * position ** -CURVE_B


def demand_factor(now, market):
    """Smooth day-of-week and time-of-day pattern, in the country's local time."""
    local = now + timedelta(hours=UTC_OFFSET.get(market, 0))
    # weekday as a continuous number, with each day's value centred at noon, so it changes smoothly hour by hour
    pos = local.weekday() + (local.hour + local.minute / 60) / 24 - 0.5
    i = math.floor(pos) % 7
    frac = pos - math.floor(pos)
    weekly = WEEKDAY_DEMAND[i] * (1 - frac) + WEEKDAY_DEMAND[(i + 1) % 7] * frac
    hour = local.hour + local.minute / 60
    daily = 1 + DAILY_SWING * math.cos(2 * math.pi * (hour - 22) / 24)
    return weekly * daily


def apply_history(market_hist, songs, today, now=None, market="us"):
    """Update one country's history with the latest chart and return the songs with movement and sales.

    market_hist: {"dates": [...], "snaps": [...], "songs": {key: record}}  (modified in place)
    """
    now = now or datetime.now(timezone.utc)
    dates = market_hist.setdefault("dates", [])
    store = market_hist.setdefault("songs", {})
    # snapshots (minutes since 1970) kept for the rolling 24-hour estimate
    now_min = int(now.timestamp() // 60)
    cutoff = now_min - 24 * 60
    snaps = [s for s in market_hist.get("snaps", []) if s > cutoff and s != now_min] + [now_min]
    market_hist["snaps"] = snaps
    factor = demand_factor(now, market) * SCALE.get(market, 1.0)
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
            "sales": int(round(sales)),
            "art": song.get("art") or rec.get("art", ""),
            "prev": previous,
            "peak": rec["peak"],
            "days": rec["days"],
            "new": bool(is_new or (prev_chart_day is None and rec["days"] == 1)),
            "re": bool(previous is None and not is_new and rec["days"] > 1),
        })

    for key in [k for k, r in store.items() if r["last_date"] and day_diff(today, r["last_date"]) > FORGET_AFTER_DAYS]:
        del store[key]
    for r in store.values():  # keep the file small: drop positions older than 24 hours
        if r.get("recent"):
            r["recent"] = [x for x in r["recent"] if x[0] > cutoff]
            if not r["recent"]:
                del r["recent"]
    return result


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def main():
    previous = load_json(OUT, {}).get("markets", {})
    history = load_json(HISTORY, {})
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    markets = {}
    for key, code in MARKETS.items():
        print(f"{key}: fetching")
        result = fetch_market(code)
        if result:
            print(f"  ok ({result['source']}, {len(result['songs'])} songs)")
            market_hist = history.setdefault(key, {})
            fill_artwork(result["songs"], market_hist, code)
            result["songs"] = apply_history(market_hist, result["songs"], today, now, key)
            markets[key] = result
        elif key in previous:
            print("  keeping previous copy")
            markets[key] = previous[key]

    if not markets:
        print("no data at all, leaving files untouched", file=sys.stderr)
        return 1

    payload = {"fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "update_minutes": UPDATE_MINUTES, "markets": markets}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
