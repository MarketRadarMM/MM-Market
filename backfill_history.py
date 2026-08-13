#!/usr/bin/env python3
"""
Backfill historical series into docs/data/series.json.

Three sources, all retrospective:
  - YSX  : the market-summary page carries a full daily MYANPIX table back
           to 2019 in the same HTML we already fetch for today's figure
  - Stooq: daily XAU/USD history as CSV, no key
  - Telegram: t.me/s/<channel>?before=<id> pages backwards through the
           archive; parse_post() from fetch_prices.py reads each post

Safe to re-run: existing rows are merged by date, not duplicated. Run it
once to seed, then the daily fetchers keep it current.

Respects PUBLIC_MODE -- no channel identifiers are written to the output,
only dates and values.

Usage:
    TG_CHANNEL=<handle> DATA_DIR=docs/data PUBLIC_MODE=1 \
        TG_PAGES=40 python backfill_history.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Reuse the parser we already trust.
from fetch_prices import parse_post, CHANNEL, UA

OUT_DIR = Path(os.environ.get("DATA_DIR", "data"))
MMT = timezone(timedelta(hours=6, minutes=30))
HEADERS = {"User-Agent": UA, "Accept": "*/*",
           "Accept-Language": "en-US,en;q=0.9"}

# How many pages of ~20 Telegram posts to walk back. 40 pages is roughly
# 800 posts, which at this channel's cadence is well over a year.
TG_PAGES = int(os.environ.get("TG_PAGES", "40"))
TG_DELAY = float(os.environ.get("TG_DELAY", "1.5"))   # be polite

SERIES_PATH = OUT_DIR / "series.json"


# --------------------------------------------------------------------------
# YSX -- daily MYANPIX table
# --------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# "3 Aug 2026 831.77 844.40 831.77 840.40 16,310 115,039,650 8 1,594,140"
YSX_ROW_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})\s+"
    r"([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+"
    r"([\d,]+)\s+([\d,]+)\s+(\d+)\s+([\d,]+)"
)


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def backfill_ysx():
    rows = {}
    try:
        resp = requests.get(
            "https://ysx-mm.com/main-board/mktdata/market-summary/",
            headers=HEADERS, timeout=40)
        if resp.status_code != 200:
            print(f"[ysx] HTTP {resp.status_code}", file=sys.stderr)
            return rows
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
    except Exception as e:
        print(f"[ysx] {type(e).__name__}", file=sys.stderr)
        return rows

    for m in YSX_ROW_RE.finditer(text):
        day, mon, year, o, h, l, c, vol, val, listed, cap = m.groups()
        mi = MONTHS.get(mon[:3].lower())
        if not mi:
            continue
        date = f"{int(year):04d}-{mi:02d}-{int(day):02d}"
        rows[date] = {
            "date": date,
            "close": _num(c), "open": _num(o),
            "high": _num(h), "low": _num(l),
            "volume": _num(vol), "value_mmk": _num(val),
            "market_cap_mil_mmk": _num(cap),
        }

    print(f"[ysx] {len(rows)} daily rows", file=sys.stderr)
    return rows


# --------------------------------------------------------------------------
# Stooq -- daily XAU/USD
# --------------------------------------------------------------------------

def backfill_xau():
    rows = {}
    try:
        resp = requests.get("https://stooq.com/q/d/l/?s=xauusd&i=d",
                            headers=HEADERS, timeout=40)
        if resp.status_code != 200:
            print(f"[xau] HTTP {resp.status_code}", file=sys.stderr)
            return rows
        lines = resp.text.strip().splitlines()
        if len(lines) < 2:
            print("[xau] empty response", file=sys.stderr)
            return rows
        header = [h.strip().lower() for h in lines[0].split(",")]
        for line in lines[1:]:
            rec = dict(zip(header, [c.strip() for c in line.split(",")]))
            date, close = rec.get("date"), _num(rec.get("close"))
            if date and close:
                rows[date] = {"date": date, "close": close}
    except Exception as e:
        print(f"[xau] {type(e).__name__}", file=sys.stderr)
        return rows

    print(f"[xau] {len(rows)} daily rows", file=sys.stderr)
    return rows


# --------------------------------------------------------------------------
# Telegram -- paginate backwards through the archive
# --------------------------------------------------------------------------

def _page(channel, before=None):
    url = f"https://t.me/s/{channel}"
    if before:
        url += f"?before={before}"
    resp = requests.get(url, headers=HEADERS, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for w in soup.select("div.tgme_widget_message"):
        pid = w.get("data-post", "")
        body = w.select_one("div.tgme_widget_message_text")
        try:
            num = int(pid.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            num = None
        out.append({"num": num,
                    "text": body.get_text("\n", strip=True) if body else ""})
    return out


def backfill_street(channel):
    rows = {}
    if not channel:
        print("[street] TG_CHANNEL not set -- skipped", file=sys.stderr)
        return rows

    before = None
    for page in range(TG_PAGES):
        try:
            posts = _page(channel, before)
        except Exception as e:
            print(f"[street] stopped at page {page}: {type(e).__name__}",
                  file=sys.stderr)
            break
        if not posts:
            break

        nums = [p["num"] for p in posts if p["num"] is not None]
        for p in posts:
            parsed = parse_post(p["text"])
            date = parsed.get("post_date")
            gold = parsed.get("gold") or {}
            fx = parsed.get("fx") or {}
            fuel = parsed.get("fuel") or {}
            if not date or not (gold or fx):
                continue
            row = {"date": date}
            if gold.get("gold_16pe"):
                row["gold_16pe"] = gold["gold_16pe"]
            if gold.get("gold_15pe"):
                row["gold_15pe"] = gold["gold_15pe"]
            for k, v in fx.items():
                row[k] = v
            for k, v in fuel.items():
                row[k] = v
            # Later posts on the same date win; we walk backwards, so only
            # fill a date we have not already seen.
            rows.setdefault(date, row)

        if not nums:
            break
        before = min(nums)
        if before <= 1:
            break
        time.sleep(TG_DELAY)

    print(f"[street] {len(rows)} dated readings", file=sys.stderr)
    return rows


# --------------------------------------------------------------------------

def merge(existing, new):
    """Merge by date. Existing rows win, so re-running never clobbers."""
    out = {r["date"]: r for r in existing if r.get("date")}
    for date, row in new.items():
        if date not in out:
            out[date] = row
    return [out[d] for d in sorted(out)]


def main():
    prev = {}
    if SERIES_PATH.exists():
        try:
            prev = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}

    series = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Daily series. street = community-reported; "
                "myanpix = Yangon Stock Exchange; xau = world gold spot.",
        "street":  merge(prev.get("street", []),  backfill_street(CHANNEL)),
        "myanpix": merge(prev.get("myanpix", []), backfill_ysx()),
        "xau":     merge(prev.get("xau", []),     backfill_xau()),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SERIES_PATH.write_text(
        json.dumps(series, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    for k in ("street", "myanpix", "xau"):
        rows = series[k]
        span = f"{rows[0]['date']} .. {rows[-1]['date']}" if rows else "empty"
        print(f"[series] {k:8s} {len(rows):5d} rows  {span}", file=sys.stderr)

    print(f"wrote {SERIES_PATH}")


if __name__ == "__main__":
    main()
    sys.exit(0)
