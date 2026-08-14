#!/usr/bin/env python3
"""
Fetch the attributable market data: CBM reference FX, world gold spot,
YSX index and the eight listed companies. Writes docs/data/market_latest.json.

Kept separate from fetch_prices.py (the community/Telegram source) so the
two never contaminate each other, and so one failing does not take out the
other.

Source tiers, carried through to the UI:
    official   -- Central Bank of Myanmar reference rates
    exchange   -- Yangon Stock Exchange
    global     -- international gold spot

The derived block is the point of this file. CBM's USD rate is a managed
reference; the street rate has been running at roughly double it.

Usage:
    DATA_DIR=docs/data python fetch_market.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUT_DIR = Path(os.environ.get("DATA_DIR", "data"))
MMT = timezone(timedelta(hours=6, minutes=30))
UA = "Mozilla/5.0 (compatible; MarketRadarMM/1.0)"
HEADERS = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}

# 1 tical (ကျပ်သား) = 16.606 g = 0.5339 troy oz.
TICAL_TROY_OZ = 0.5339

# CBM quotes these per 100 units of foreign currency, not per 1.
CBM_PER_100 = {"JPY", "KRW", "IDR", "VND", "LAK", "KHR"}

CURRENCIES_OF_INTEREST = ["USD", "EUR", "SGD", "THB", "CNY", "JPY",
                          "MYR", "INR", "KRW", "AUD", "GBP"]

TICKERS = ["FMI", "MTSH", "MCB", "FPB", "TMH", "EFR", "AMATA", "MAEX"]

COMPANY_NAMES = {
    "FMI":   "First Myanmar Investment",
    "MTSH":  "Myanmar Thilawa SEZ Holdings",
    "MCB":   "Myanmar Citizens Bank",
    "FPB":   "First Private Bank",
    "TMH":   "TMH Telecom",
    "EFR":   "Ever Flow River Group",
    "AMATA": "Amata Holding",
    "MAEX":  "Myanmar Agro Exchange",
}


def get(url, **kw):
    return requests.get(url, headers=HEADERS, timeout=30, **kw)


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("%", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------

def fetch_cbm():
    """Central Bank of Myanmar reference rates. Official, managed, daily."""
    out = {"tier": "official", "status": "error", "note": None,
           "rates": {}, "per_100": sorted(CBM_PER_100), "as_of": None}
    try:
        resp = get("https://forex.cbm.gov.mm/api/latest")
        if resp.status_code != 200:
            out["note"] = f"HTTP {resp.status_code}"
            return out
        data = resp.json()
    except Exception as e:
        out["note"] = f"{type(e).__name__}"
        return out

    raw = data.get("rates") or {}
    for code in CURRENCIES_OF_INTEREST:
        if code in raw:
            v = _num(raw[code])
            if v is not None:
                out["rates"][code] = v

    ts = data.get("timestamp")
    if ts:
        try:
            out["as_of"] = datetime.fromtimestamp(
                int(ts), tz=timezone.utc).astimezone(MMT).date().isoformat()
        except (TypeError, ValueError, OSError):
            pass

    out["status"] = "ok" if out["rates"] else "error"
    if not out["rates"]:
        out["note"] = "no recognised currencies in response"
    return out


def fetch_world_gold():
    """XAU/USD spot from Stooq. Free, no API key. Best-effort."""
    out = {"tier": "global", "status": "error", "note": None,
           "xau_usd": None, "as_of": None}
    try:
        resp = get("https://stooq.com/q/l/?s=xauusd&f=sd2t2ohlcv&h&e=csv")
        if resp.status_code != 200:
            out["note"] = f"HTTP {resp.status_code}"
            return out
        lines = resp.text.strip().splitlines()
        if len(lines) < 2:
            out["note"] = "empty response"
            return out
        header = [h.strip().lower() for h in lines[0].split(",")]
        rec = dict(zip(header, [c.strip() for c in lines[1].split(",")]))
        close = rec.get("close")
        if not close or close.upper() == "N/D":
            out["note"] = "no close price"
            return out
        out["xau_usd"] = float(close)
        out["as_of"] = rec.get("date")
        out["status"] = "ok"
    except Exception as e:
        out["note"] = f"{type(e).__name__}"
    return out


MYANPIX_RE = re.compile(
    r"MYANPIX\s+([\d,.]+)\s+([+-][\d,.]+)\s+([+-][\d,.]+)%\s+"
    r"([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,]+)\s+([\d,]+)\s+(\d+)\s+([\d,]+)"
)
ASOF_RE = re.compile(r"As of\s+(\d{1,2})\w*\s+([A-Za-z]+)\.?\s+(\d{4})")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# NAME CODE CLOSE CHANGE %CHG OPEN HIGH LOW VOL VALUE SHARES MCAP
STOCK_RE = re.compile(
    r"\b(" + "|".join(TICKERS) + r")\s+(\d{5})\s+"
    r"([\d,]+)\s+([+-][\d,]+)\s+([+-][\d.,]+)%\s+"
    r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+"
    r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)"
)


def _as_of(text):
    d = ASOF_RE.search(text)
    if not d:
        return None
    day, mon, year = d.groups()
    mi = MONTHS.get(mon[:3].lower())
    return f"{int(year):04d}-{mi:02d}-{int(day):02d}" if mi else None


def fetch_ysx_index():
    """Main-board summary: MYANPIX plus a long historical table."""
    out = {"tier": "exchange", "status": "error", "note": None,
           "index": "MYANPIX", "close": None, "change": None,
           "change_pct": None, "volume": None, "value_mmk": None,
           "listed": None, "market_cap_mil_mmk": None, "as_of": None}
    try:
        resp = get("https://ysx-mm.com/main-board/mktdata/market-summary/")
        if resp.status_code != 200:
            out["note"] = f"HTTP {resp.status_code}"
            return out
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
    except Exception as e:
        out["note"] = f"{type(e).__name__}"
        return out

    m = MYANPIX_RE.search(text)
    if not m:
        out["note"] = "MYANPIX row not found -- layout may have changed"
        return out

    close, change, pct, _o, _h, _l, vol, val, listed, cap = m.groups()
    out.update({
        "close": _num(close), "change": _num(change), "change_pct": _num(pct),
        "volume": _num(vol), "value_mmk": _num(val), "listed": int(listed),
        "market_cap_mil_mmk": _num(cap), "as_of": _as_of(text), "status": "ok",
    })
    return out


def fetch_ysx_stocks():
    """Per-company table for the eight listed companies."""
    out = {"tier": "exchange", "status": "error", "note": None,
           "as_of": None, "stocks": []}
    try:
        resp = get("https://ysx-mm.com/main-board/mktdata/stocktradingdata/")
        if resp.status_code != 200:
            out["note"] = f"HTTP {resp.status_code}"
            return out
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
    except Exception as e:
        out["note"] = f"{type(e).__name__}"
        return out

    seen = set()
    for m in STOCK_RE.finditer(text):
        (code, num, close, change, pct,
         o, h, l, vol, val, shares, cap) = m.groups()
        if code in seen:
            continue
        seen.add(code)
        out["stocks"].append({
            "code": code,
            "name": COMPANY_NAMES.get(code, code),
            "number": num,
            "close": _num(close),
            "change": _num(change),
            "change_pct": _num(pct) * (-1 if pct.strip().startswith("-") else 1),
            "open": _num(o), "high": _num(h), "low": _num(l),
            "volume": _num(vol), "value_mmk": _num(val),
            "listed_shares": _num(shares),
            "market_cap_mil_mmk": _num(cap),
        })

    out["as_of"] = _as_of(text)
    out["status"] = "ok" if out["stocks"] else "error"
    if not out["stocks"]:
        out["note"] = "no company rows matched -- layout may have changed"
    return out


def derive(cbm, gold, community):
    """Gaps between tiers -- the part worth publishing."""
    out = {}
    cbm_rates = cbm.get("rates") or {}
    vals = community.get("values") or {}
    street_fx = vals.get("fx") or {}
    street_gold = (vals.get("gold") or {}).get("gold_16pe")
    xau = gold.get("xau_usd")

    # Premium per currency, not just USD
    prem = {}
    for code in ("USD", "THB", "CNY", "SGD"):
        o, s = cbm_rates.get(code), street_fx.get(code)
        if o and s:
            prem[code] = {"official": o, "street": s,
                          "premium_pct": round((s / o - 1) * 100, 1)}
    if prem:
        out["fx_premium"] = prem
        if "USD" in prem:
            out["fx_spread"] = {
                "cbm_usd_mmk": prem["USD"]["official"],
                "street_usd_mmk": prem["USD"]["street"],
                "premium_pct": prem["USD"]["premium_pct"],
            }

    street_usd = street_fx.get("USD")
    if xau and street_usd:
        synth = xau * street_usd * TICAL_TROY_OZ
        out["synthetic_tical_mmk"] = int(round(synth))
        if street_gold:
            out["gold_premium_pct"] = round((street_gold / synth - 1) * 100, 1)
    if street_gold and street_usd:
        out["implied_xau_usd"] = round(street_gold / street_usd / TICAL_TROY_OZ, 1)

    return out


# --------------------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)

    community = {}
    p = OUT_DIR / "prices_latest.json"
    if p.exists():
        try:
            community = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            community = {}

    cbm = fetch_cbm()
    gold = fetch_world_gold()
    ysx = fetch_ysx_index()
    stocks = fetch_ysx_stocks()

    for name, block in (("cbm", cbm), ("gold", gold),
                        ("ysx", ysx), ("stocks", stocks)):
        extra = f" -- {block['note']}" if block.get("note") else ""
        n = f" ({len(block['stocks'])})" if name == "stocks" and block.get("stocks") else ""
        print(f"[market] {name:7s} {block['status']}{n}{extra}", file=sys.stderr)

    out = {
        "fetched_at": now.isoformat(),
        "tiers": {
            "official":  {"my": "တရားဝင် (ဗဟိုဘဏ်)", "en": "Official (CBM)"},
            "exchange":  {"my": "စတော့အိတ်ချိန်း",     "en": "Exchange (YSX)"},
            "global":    {"my": "ကမ္ဘာ့ဈေးကွက်",        "en": "Global market"},
            "community": {"my": "လူမှုကွန်ရက်",         "en": "Community-reported"},
        },
        "tical_troy_oz": TICAL_TROY_OZ,
        "cbm": cbm,
        "world_gold": gold,
        "ysx": ysx,
        "stocks": stocks,
        "derived": derive(cbm, gold, community),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "market_latest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    hist = OUT_DIR / "market_history"
    hist.mkdir(parents=True, exist_ok=True)
    day = datetime.now(MMT).strftime("%Y-%m-%d")
    (hist / f"{day}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(out.get("derived", {}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
    sys.exit(0)
