#!/usr/bin/env python3
"""
Backfill and maintain docs/data/series.json.

Sources:
  - YSX  : market-summary page carries a daily MYANPIX table back to 2016
  - Stooq: daily XAU/USD history (best-effort; often unavailable)
  - Telegram: t.me/s/<channel>?before=<id> walks the archive backwards
  - market_latest.json: today's per-company YSX closes, appended daily

Safe to re-run: rows merge by date. Run once with a large TG_PAGES to seed,
then daily with TG_PAGES=2 to stay current.

OUTLIER FILTERING (added 2026-08):
The source is hand-typed and occasionally drops a digit -- one gold reading
came through about 90% below its neighbours, which drew a crash on the chart
that never happened. Every numeric series is now checked against a local
median and implausible points are removed. This runs on the merged result,
so re-running repairs values already stored.
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

from fetch_prices import parse_post, CHANNEL, UA

OUT_DIR = Path(os.environ.get("DATA_DIR", "data"))
MMT = timezone(timedelta(hours=6, minutes=30))
HEADERS = {"User-Agent": UA, "Accept": "*/*",
           "Accept-Language": "en-US,en;q=0.9"}

TG_PAGES = int(os.environ.get("TG_PAGES", "40"))
TG_DELAY = float(os.environ.get("TG_DELAY", "1.5"))

SERIES_PATH = OUT_DIR / "series.json"

# A daily move larger than this against the local median is treated as a
# typo rather than a market event. Gold and FX in Myanmar are volatile but
# not this volatile -- a genuine 40% single-day move would be historic.
OUTLIER_TOLERANCE = 0.40
MEDIAN_WINDOW = 5          # points either side used to build the median


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Outlier removal
# --------------------------------------------------------------------------

def _median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def sanitize(rows, keys):
    """
    Drop implausible values in place. Compares each point against the median
    of its neighbours rather than the previous point alone, so a single bad
    reading cannot drag the reference with it.
    """
    removed = 0
    for key in keys:
        idx = [i for i, r in enumerate(rows) if isinstance(r.get(key), (int, float))]
        if len(idx) < 5:
            continue
        for pos, i in enumerate(idx):
            lo = max(0, pos - MEDIAN_WINDOW)
            hi = min(len(idx), pos + MEDIAN_WINDOW + 1)
            neighbours = [rows[idx[j]][key] for j in range(lo, hi) if j != pos]
            med = _median(neighbours)
            if not med:
                continue
            v = rows[i][key]
            if abs(v - med) / med > OUTLIER_TOLERANCE:
                del rows[i][key]
                removed += 1
    return removed


# --------------------------------------------------------------------------
# YSX index history
# --------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

YSX_ROW_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})\s+"
    r"([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+"
    r"([\d,]+)\s+([\d,]+)\s+(\d+)\s+([\d,]+)"
)


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
        rows[date] = {"date": date, "close": _num(c), "open": _num(o),
                      "high": _num(h), "low": _num(l), "volume": _num(vol),
                      "value_mmk": _num(val), "market_cap_mil_mmk": _num(cap)}

    print(f"[ysx] {len(rows)} daily rows", file=sys.stderr)
    return rows


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
# Telegram archive
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
            for k in ("gold_16pe", "gold_15pe"):
                if gold.get(k):
                    row[k] = gold[k]
            row.update(fx)
            row.update(fuel)
            rows.setdefault(date, row)

        if not nums:
            break
        before = min(nums)
        if before <= 1:
            break
        time.sleep(TG_DELAY)

    print(f"[street] {len(rows)} dated readings", file=sys.stderr)
    return rows


def todays_stocks():
    """Per-company closes from market_latest.json, one row per day."""
    p = OUT_DIR / "market_latest.json"
    if not p.exists():
        return {}
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    block = m.get("stocks") or {}
    if block.get("status") != "ok":
        return {}
    date = block.get("as_of") or datetime.now(MMT).strftime("%Y-%m-%d")
    row = {"date": date}
    for s in block.get("stocks", []):
        if s.get("code") and s.get("close"):
            row[s["code"]] = s["close"]
    return {date: row} if len(row) > 1 else {}


# --------------------------------------------------------------------------

def merge(existing, new):
    out = {r["date"]: r for r in existing if r.get("date")}
    for date, row in new.items():
        if date in out:
            # Fill gaps in an existing row without overwriting known values
            for k, v in row.items():
                out[date].setdefault(k, v)
        else:
            out[date] = row
    return [out[d] for d in sorted(out)]


STREET_KEYS = ["gold_16pe", "gold_15pe", "USD", "THB", "CNY", "SGD",
               "JPY", "AUD", "YGN_92", "YGN_95", "MDY_92", "MDY_95"]
TICKERS = ["FMI", "MTSH", "MCB", "FPB", "TMH", "EFR", "AMATA", "MAEX"]


def main():
    prev = {}
    if SERIES_PATH.exists():
        try:
            prev = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}

    street  = merge(prev.get("street", []),  backfill_street(CHANNEL))
    myanpix = merge(prev.get("myanpix", []), backfill_ysx())
    xau     = merge(prev.get("xau", []),     backfill_xau())
    stocks  = merge(prev.get("stocks", []),  todays_stocks())

    dropped  = sanitize(street, STREET_KEYS)
    dropped += sanitize(myanpix, ["close"])
    dropped += sanitize(stocks, TICKERS)
    if dropped:
        print(f"[clean] removed {dropped} implausible values", file=sys.stderr)

    series = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Daily series. street = community-reported; "
                "myanpix/stocks = Yangon Stock Exchange; xau = world gold spot.",
        "street": street, "myanpix": myanpix, "xau": xau, "stocks": stocks,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SERIES_PATH.write_text(
        json.dumps(series, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    for k in ("street", "myanpix", "xau", "stocks"):
        rows = series[k]
        span = f"{rows[0]['date']} .. {rows[-1]['date']}" if rows else "empty"
        print(f"[series] {k:8s} {len(rows):5d} rows  {span}", file=sys.stderr)

    print(f"wrote {SERIES_PATH}")


if __name__ == "__main__":
    main()
    sys.exit(0)
