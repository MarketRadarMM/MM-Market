#!/usr/bin/env python3
"""
Backfill and maintain docs/data/series.json.

Walks BOTH Telegram channels (primary TG_CHANNEL2, legacy TG_CHANNEL):
the primary supplies gold / FX / world gold / YGEA, the legacy channel
supplies fuel and the deep 2023+ history. Rows merge per-key by date, so
the two sources fill each other's gaps without overwriting.

Also refreshes MYANPIX from YSX and per-company closes from
market_latest.json, and runs the outlier filter over everything.

Run once with a large TG_PAGES to seed, then daily with TG_PAGES=2.
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

from fetch_prices import parse_post, CHANNEL, CHANNEL2, UA

OUT_DIR = Path(os.environ.get("DATA_DIR", "data"))
MMT = timezone(timedelta(hours=6, minutes=30))
HEADERS = {"User-Agent": UA, "Accept": "*/*",
           "Accept-Language": "en-US,en;q=0.9"}

TG_PAGES = int(os.environ.get("TG_PAGES", "40"))
TG_DELAY = float(os.environ.get("TG_DELAY", "1.5"))

SERIES_PATH = OUT_DIR / "series.json"

OUTLIER_TOLERANCE = 0.40
MEDIAN_WINDOW = 5


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Outlier removal (snapshot first, delete after -- see earlier KeyError fix)
# --------------------------------------------------------------------------

def _median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def sanitize(rows, keys):
    removed = 0
    for key in keys:
        idx = [i for i, r in enumerate(rows)
               if isinstance(r.get(key), (int, float))]
        if len(idx) < 5:
            continue
        vals = [rows[i][key] for i in idx]
        drop = []
        for pos in range(len(idx)):
            lo = max(0, pos - MEDIAN_WINDOW)
            hi = min(len(idx), pos + MEDIAN_WINDOW + 1)
            neighbours = [vals[j] for j in range(lo, hi) if j != pos]
            med = _median(neighbours)
            if not med:
                continue
            if abs(vals[pos] - med) / med > OUTLIER_TOLERANCE:
                drop.append(idx[pos])
        for i in drop:
            rows[i].pop(key, None)
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


# --------------------------------------------------------------------------
# Telegram archives (both channels)
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


def flatten(parsed):
    """One parsed post -> flat {key: value} for the series row."""
    row = {}
    gold = parsed.get("gold") or {}
    for k in ("gold_16pe", "gold_15pe", "gold_16pe_buy",
              "gold_16pe_new", "gold_15pe_new", "gold_16pe_new_buy"):
        if gold.get(k):
            row[k] = gold[k]
    for k, v in (parsed.get("fx") or {}).items():
        row[k] = v
    for k, v in (parsed.get("fuel") or {}).items():
        row[k] = v
    if parsed.get("wg_usd") is not None:
        row["wg_usd"] = parsed["wg_usd"]
    if parsed.get("ygea") is not None:
        row["ygea"] = parsed["ygea"]
    return row


def walk_channel(channel, label):
    """
    {date: {key: value}} for one channel's archive.

    Walking is newest-first, and per-key setdefault means the LAST post of
    each day wins -- i.e. the market-close reading rather than the 10AM one,
    which is the right value for a daily series.
    """
    rows = {}
    if not channel:
        print(f"[{label}] channel not set -- skipped", file=sys.stderr)
        return rows

    before = None
    for page in range(TG_PAGES):
        try:
            posts = _page(channel, before)
        except Exception as e:
            print(f"[{label}] stopped at page {page}: {type(e).__name__}",
                  file=sys.stderr)
            break
        if not posts:
            break

        nums = [p["num"] for p in posts if p["num"] is not None]
        for p in posts:
            parsed = parse_post(p["text"])
            date = parsed.get("post_date")
            if not date:
                continue
            flat = flatten(parsed)
            if not flat:
                continue
            day = rows.setdefault(date, {"date": date})
            for k, v in flat.items():
                day.setdefault(k, v)

        if not nums:
            break
        before = min(nums)
        if before <= 1:
            break
        time.sleep(TG_DELAY)

    print(f"[{label}] {len(rows)} dated readings", file=sys.stderr)
    return rows


def todays_stocks():
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

def merge(existing, *new_maps, overwrite_dates=()):
    """
    Merge by date, then per key.

    Historical dates: existing values win, so re-running a backfill can
    never rewrite the past. Dates in `overwrite_dates` (today, in normal
    daily use): NEW values win -- this is what lets the six intraday cron
    runs walk today's row forward from the 10AM price to the market close.
    Without it the first run of the day froze the row and the close was
    silently discarded.
    """
    out = {r["date"]: dict(r) for r in existing if r.get("date")}
    for new in new_maps:
        for date, row in new.items():
            if date in out:
                if date in overwrite_dates:
                    out[date].update(row)
                else:
                    for k, v in row.items():
                        out[date].setdefault(k, v)
            else:
                out[date] = dict(row)
    return [out[d] for d in sorted(out)]


STREET_KEYS = ["gold_16pe", "gold_15pe", "gold_16pe_buy",
               "gold_16pe_new", "gold_15pe_new", "gold_16pe_new_buy",
               "wg_usd", "ygea",
               "USD", "THB", "CNY", "SGD", "JPY", "AUD", "EUR", "GBP", "MYR",
               "YGN_92", "YGN_95", "MDY_92", "MDY_95"]
TICKERS = ["FMI", "MTSH", "MCB", "FPB", "TMH", "EFR", "AMATA", "MAEX"]


def main():
    prev = {}
    if SERIES_PATH.exists():
        try:
            prev = json.loads(SERIES_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}

    primary = walk_channel(CHANNEL2, "street2")
    legacy = walk_channel(CHANNEL, "street")

    # Primary first: where both channels report the same date, the
    # primary's reading wins for shared keys (existing series rows still
    # outrank both).
    today = datetime.now(MMT).strftime("%Y-%m-%d")

    street = merge(prev.get("street", []), primary, legacy,
                   overwrite_dates={today})
    myanpix = merge(prev.get("myanpix", []), backfill_ysx(),
                    overwrite_dates={today})
    xau = prev.get("xau", [])                      # superseded by wg_usd
    stocks = merge(prev.get("stocks", []), todays_stocks(),
                   overwrite_dates={today})

    dropped = sanitize(street, STREET_KEYS)
    dropped += sanitize(myanpix, ["close"])
    dropped += sanitize(stocks, TICKERS)
    if dropped:
        print(f"[clean] removed {dropped} implausible values", file=sys.stderr)

    series = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Daily series. street = community-reported (wg_usd = world "
                "gold USD/oz, ygea = YGEA reference); myanpix/stocks = "
                "Yangon Stock Exchange.",
        "street": street, "myanpix": myanpix, "xau": xau, "stocks": stocks,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SERIES_PATH.write_text(
        json.dumps(series, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")

    for k in ("street", "myanpix", "stocks"):
        rows = series[k]
        span = f"{rows[0]['date']} .. {rows[-1]['date']}" if rows else "empty"
        print(f"[series] {k:8s} {len(rows):5d} rows  {span}", file=sys.stderr)

    print(f"wrote {SERIES_PATH}")


if __name__ == "__main__":
    main()
    sys.exit(0)
