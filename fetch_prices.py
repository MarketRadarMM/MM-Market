#!/usr/bin/env python3
"""
Fetch street prices from up to two public Telegram channels.

  TG_CHANNEL2 -- primary: gold (both weight systems, sell + buyback),
                 world gold (WG), YGEA reference, FX buy/sell table
  TG_CHANNEL  -- legacy: fuel prices, and full fallback if primary fails

Handles are supplied via environment / repository secrets and never appear
in this file. PUBLIC_MODE=1 strips all source-identifying fields from every
output, including the stdout dump that lands in CI logs.

Series continuity: the legacy channel quoted old-system (16.606g) sell
prices, so gold_16pe / gold_15pe keep meaning exactly that and the
multi-year series continues unbroken. New-system and buyback prices are
additional keys.

Usage:
    TG_CHANNEL2=<handle> TG_CHANNEL=<handle> DATA_DIR=docs/data \
        PUBLIC_MODE=1 python fetch_prices.py
"""

import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CHANNEL = os.environ.get("TG_CHANNEL", "").strip().lstrip("@")     # legacy/fuel
CHANNEL2 = os.environ.get("TG_CHANNEL2", "").strip().lstrip("@")   # primary
OUT_DIR = Path(os.environ.get("DATA_DIR", "data"))
DEBUG = os.environ.get("DEBUG_LINES", "").strip() not in ("", "0", "false")
PUBLIC_MODE = os.environ.get("PUBLIC_MODE", "").strip() not in ("", "0", "false")
UA = "Mozilla/5.0 (compatible; MarketRadarMM/1.0)"

MMT = timezone(timedelta(hours=6, minutes=30))
# Primary channel posts daily (occasionally skipping the weekly close day).
STALE_AFTER_HOURS = float(os.environ.get("STALE_AFTER_HOURS", "48"))

BURMESE_DIGITS = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\ufeff"), None)


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.translate(ZERO_WIDTH)
    text = text.translate(BURMESE_DIGITS)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]", " ", text)
    return re.sub(r"[ \t]+", " ", text)


NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def to_number(fragment: str):
    fragment = re.sub(r"\s+", "", fragment or "")
    m = NUM_RE.search(fragment)
    if not m:
        return None
    raw = m.group(0).replace(",", "").rstrip(".")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


# --------------------------------------------------------------------------
# Shared patterns
# --------------------------------------------------------------------------

GOLD_MIN, GOLD_MAX = 1_000_000, 100_000_000
WG_MIN, WG_MAX = 1_000, 20_000          # world gold, USD/oz

DATE_NUM_RE = re.compile(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(20\d{2})\b")
DATE_ENG_RE = re.compile(
    r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(20\d{2})\b",
    re.IGNORECASE)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

FX_CODES = ("USD", "GBP", "EUR", "SGD", "AUD", "AED", "CAD", "MYR",
            "THB", "TWD", "JPY", "HKD", "CNY", "INR", "KRW")
BUYSELL_RE = re.compile(r"Buy\s*([\d.,]+)\s*[-–—]\s*Sell\s*([\d.,]+)", re.IGNORECASE)
FX_HEAD_RE = re.compile(r"\b(" + "|".join(FX_CODES) + r")\s*1\b", re.IGNORECASE)

BOUNDS = {
    "USD": (1_000, 20_000), "GBP": (1_000, 25_000), "EUR": (1_000, 20_000),
    "SGD": (1_000, 10_000), "AUD": (1_000, 10_000), "AED": (500, 4_000),
    "CAD": (1_000, 10_000), "MYR": (300, 5_000),   "THB": (30, 600),
    "TWD": (50, 500),       "JPY": (5, 200),       "HKD": (200, 2_000),
    "CNY": (200, 2_000),    "INR": (20, 200),      "KRW": (1, 50),
    "fuel": (500, 20_000),
}


def in_bounds(key, value) -> bool:
    lo, hi = BOUNDS.get(key, (None, None))
    return True if lo is None else lo <= value <= hi


def find_date(line, current):
    if current:
        return current
    m = DATE_NUM_RE.search(line)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        if 1 <= d <= 31 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = DATE_ENG_RE.search(line)
    if m:
        d, mon, y = m.groups()
        mi = MONTHS[mon[:3].lower()]
        return f"{int(y):04d}-{mi:02d}-{int(d):02d}"
    return None


def gold_numbers_in(line):
    out = []
    for m in NUM_RE.finditer(line):
        v = to_number(m.group(0))
        if v is not None and GOLD_MIN <= v <= GOLD_MAX:
            out.append(int(v))
    return out


# --------------------------------------------------------------------------
# Primary-channel parser (gdb format)
#
# Structured posts look like:
#   13 Aug 2026 ( 10 :00 AM )          or   12 . 8 . 2026 ( 3:20 pm )
#   WG - 4406 $   /  ကမ္ဘာ့ရွှေဈေး - 4407$
#   USD - 4400 Ks
#   YGEA ဈေး - 7050000 Ks              (sometimes in Burmese digits)
#   စနစ်ဟောင်း( 16.606g )               old system
#     အရောင်း / ရောင်းဈေး - <sell>
#     ပြန်ဝယ် - <buyback>
#     ၁၅'ရောင်း -<15pe sell>            or a ၁၅ ပဲရည်ဈေး header + ရောင်းဈေး line
#   စနစ်သစ်( 16.329g )                  new system, same shape
#
# Separate 9AM FX posts carry "USD1$" headers with "Buy X - Sell Y" lines.
#
# Commentary posts quote shorthand ("အရောင်း ၉၉၅") -- those numbers fall
# outside GOLD_MIN..GOLD_MAX and are ignored automatically.
# --------------------------------------------------------------------------

def parse_gdb(lines):
    gold, fx, fx_detail = {}, {}, {}
    wg = ygea = None
    post_date = None

    system = None        # "old" | "new"
    grade = None         # "acheik" | "15pe"
    pending_fx = None    # code awaiting its Buy/Sell line

    for line in lines:
        post_date = find_date(line, post_date)

        # -- world gold ------------------------------------------------
        if wg is None and ("WG" in line or "ကမ္ဘာ့ရွှေ" in line):
            for m in NUM_RE.finditer(line):
                v = to_number(m.group(0))
                if v is not None and WG_MIN <= v <= WG_MAX:
                    wg = v
                    break

        # -- YGEA reference ----------------------------------------------
        if ygea is None and "YGEA" in line:
            vals = gold_numbers_in(line)
            if vals:
                ygea = vals[0]

        # -- single USD line in gold posts: "USD - 4400 Ks" ---------------
        if "USD" in line and "Buy" not in line and not FX_HEAD_RE.search(line):
            m = re.search(r"USD\s*[-:]?\s*([\d,]+)", line)
            if m:
                v = to_number(m.group(1))
                if v is not None and in_bounds("USD", v):
                    fx.setdefault("USD", v)

        # -- weight-system sections ----------------------------------------
        if "ဟောင်း" in line and "စနစ်" in line:
            system, grade = "old", "acheik"
        elif "သစ်" in line and "စနစ်" in line:
            system, grade = "new", "acheik"

        if "အခေါက်" in line:
            grade = "acheik"

        vals = gold_numbers_in(line)

        # "15'ရောင်း -9510000" -- marker and value on one line
        if system and re.search(r"\b15\b|15'", line) and "ရောင်း" in line:
            if vals:
                key = "gold_15pe" if system == "old" else "gold_15pe_new"
                gold.setdefault(key, vals[0])
                continue
            grade = "15pe"          # header form: value on a later line
            continue
        if system and re.search(r"\b15\b", line) and "ပဲရည်" in line:
            grade = "15pe"
            continue

        if system and vals:
            sell = ("အရောင်း" in line or "ရောင်းဈေး" in line
                    or "ရောင်းစျေး" in line)
            buy = "ပြန်ဝယ်" in line
            if buy:
                key = "gold_16pe_buy" if system == "old" else "gold_16pe_new_buy"
                gold.setdefault(key, vals[0])
            elif sell:
                if grade == "15pe":
                    key = "gold_15pe" if system == "old" else "gold_15pe_new"
                else:
                    key = "gold_16pe" if system == "old" else "gold_16pe_new"
                gold.setdefault(key, vals[0])

        # -- FX table: "USD1$" header, then "Buy 4350 - Sell 4400" ---------
        h = FX_HEAD_RE.search(line)
        if h:
            pending_fx = h.group(1).upper()
        bs = BUYSELL_RE.search(line)
        if bs and pending_fx:
            b, s = to_number(bs.group(1)), to_number(bs.group(2))
            if (b is not None and s is not None
                    and in_bounds(pending_fx, b) and in_bounds(pending_fx, s)):
                fx_detail.setdefault(pending_fx, {"buy": b, "sell": s})
                fx[pending_fx] = s          # flat key = sell, for continuity
            pending_fx = None

    out = {}
    if gold:
        out["gold"] = gold
    if fx:
        out["fx"] = fx
    if fx_detail:
        out["fx_detail"] = fx_detail
    if wg is not None:
        out["wg_usd"] = wg
    if ygea is not None:
        out["ygea"] = ygea
    if post_date:
        out["post_date"] = post_date
    return out


# --------------------------------------------------------------------------
# Legacy-channel parser (kept for fuel and for the historical archive)
# --------------------------------------------------------------------------

PURE_MARKERS = ("အခေါက်", "16")
PRICE_LABEL = "ပေါက်စျေး"
LEGACY_FX_RE = re.compile(
    r"\b(USD|THB|SGD|JPY|AUD|CNY|EUR|MYR|KRW|GBP)\b\.?\s*[-:]?\s*([\d.,]+)",
    re.IGNORECASE)
FUEL_RE = re.compile(r"\b(MDY|YGN)\s*(92|95)\s*[-–—]\s*([\d,]+)")


def parse_legacy(lines):
    gold, fx, fuel = {}, {}, {}
    post_date = None
    section = None
    ordered = []

    for line in lines:
        post_date = find_date(line, post_date)
        fx_hit = bool(LEGACY_FX_RE.search(line)) or bool(FUEL_RE.search(line))

        if not fx_hit and not DATE_NUM_RE.search(line):
            if re.search(r"\b15\b", line):
                section = "gold_15pe"
            elif any(k in line for k in PURE_MARKERS):
                section = "gold_16pe"

        vals = gold_numbers_in(line)
        ordered.extend(vals)

        if PRICE_LABEL in line and section and section not in gold and vals:
            gold[section] = vals[0]
            section = None
            continue

        for code, rawv in LEGACY_FX_RE.findall(line):
            code = code.upper()
            if code not in fx:
                v = to_number(rawv)
                if v is not None and in_bounds(code, v):
                    fx[code] = v

        for city, octane, rawv in FUEL_RE.findall(line):
            v = to_number(rawv)
            if v is not None and in_bounds("fuel", v):
                fuel[f"{city}_{octane}"] = int(v)

    if len(gold) < 2 and ordered:
        gold.setdefault("gold_16pe", ordered[0])
        if len(ordered) > 1:
            gold.setdefault("gold_15pe", ordered[1])

    if "gold_16pe" in gold and "gold_15pe" in gold:
        if gold["gold_15pe"] > gold["gold_16pe"]:
            gold["gold_16pe"], gold["gold_15pe"] = gold["gold_15pe"], gold["gold_16pe"]

    out = {}
    if gold:
        out["gold"] = gold
    if fx:
        out["fx"] = fx
    if fuel:
        out["fuel"] = fuel
    if post_date:
        out["post_date"] = post_date
    return out


def parse_post(text: str) -> dict:
    """
    Dispatcher used by both this script and backfill_history.py.
    gdb-format markers win; anything else falls through to the legacy parser.
    """
    raw = normalise(text)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    if DEBUG:
        for i, l in enumerate(lines):
            print(f"{i:3d}: {l!r}", file=sys.stderr)

    is_gdb = any(("စနစ်" in l and ("ဟောင်း" in l or "သစ်" in l)) or "YGEA" in l
                 or BUYSELL_RE.search(l) for l in lines)
    return parse_gdb(lines) if is_gdb else parse_legacy(lines)


# --------------------------------------------------------------------------
# Fetch + merge
# --------------------------------------------------------------------------

def fetch_posts(channel):
    resp = requests.get(f"https://t.me/s/{channel}",
                        headers={"User-Agent": UA}, timeout=25)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    soup = BeautifulSoup(resp.text, "html.parser")
    wrappers = soup.select("div.tgme_widget_message")
    if not wrappers:
        raise RuntimeError("no messages -- preview disabled or channel gone")
    posts = []
    for w in wrappers:
        body = w.select_one("div.tgme_widget_message_text")
        stamp = w.select_one("a.tgme_widget_message_date time")
        pid = w.get("data-post", "")
        posts.append({
            "text": body.get_text("\n", strip=True) if body else "",
            "posted_at": stamp.get("datetime") if stamp else None,
            "url": f"https://t.me/{pid}" if pid else None,
        })
    posts.reverse()
    return posts


def merge_part(dst, src, keys):
    for k in keys:
        if k in src:
            if isinstance(src[k], dict):
                d = dst.setdefault(k, {})
                for kk, vv in src[k].items():
                    d.setdefault(kk, vv)
            else:
                dst.setdefault(k, src[k])


def collect(channel, wanted, limit=30):
    """
    Walk newest-first, merging parsed parts until every wanted key is
    present or `limit` posts have been examined.

    The primary channel splits gold and FX into separate posts (10AM and
    9AM), so the latest complete picture is always an assembly of at least
    two posts.
    """
    values, chosen = {}, None
    try:
        posts = fetch_posts(channel)
    except Exception as exc:
        return {}, None, f"fetch failed: {exc}"

    for p in posts[:limit]:
        parsed = parse_post(p["text"])
        if not any(k in parsed for k in
                   ("gold", "fx", "fuel", "wg_usd", "ygea")):
            continue
        merge_part(values, parsed,
                   ("gold", "fx", "fx_detail", "fuel", "wg_usd", "ygea",
                    "post_date"))
        if chosen is None:
            chosen = p
        if all(k in values for k in wanted):
            break

    return values, chosen, None


def main():
    now = datetime.now(timezone.utc)
    out = {
        "source": "telegram",
        "channel": CHANNEL2 or CHANNEL,
        "fetched_at": now.isoformat(),
        "status": "error",
        "confidence": "amber",
        "note": None,
        "post_url": None,
        "posted_at": None,
        "post_date": None,
        "age_hours": None,
        "labels": {
            "gold_16pe": {"my": "အခေါက်ရွှေ (စနစ်ဟောင်း)", "en": "Pure gold, old system (16.606g)"},
            "gold_15pe": {"my": "၁၅ ပဲရည် (စနစ်ဟောင်း)", "en": "15 pè yay, old system"},
            "gold_16pe_new": {"my": "အခေါက်ရွှေ (စနစ်သစ်)", "en": "Pure gold, new system (16.329g)"},
            "gold_15pe_new": {"my": "၁၅ ပဲရည် (စနစ်သစ်)", "en": "15 pè yay, new system"},
            "gold_16pe_buy": {"my": "ပြန်ဝယ်ဈေး (စနစ်ဟောင်း)", "en": "Buyback, old system"},
            "ygea": {"my": "YGEA ရည်ညွှန်းဈေး", "en": "YGEA reference price"},
            "wg_usd": {"my": "ကမ္ဘာ့ရွှေဈေး", "en": "World gold (USD/oz)"},
        },
        "unit": "MMK per tical (ကျပ်သား); ရောင်းဈေး unless marked",
        "values": {},
        "source_disclaimer_my": "ခန့်မှန်းဈေးသာဖြစ်ပြီး တစ်နေရာနှင့်တစ်နေရာ ကွာဟနိုင်ပါသည်",
        "source_disclaimer_en": "Estimates only; prices vary by location",
    }

    if not (CHANNEL2 or CHANNEL):
        out["note"] = "no channel secrets set"
        write(out)
        return

    # Primary channel: gold + FX (+ WG, YGEA arrive with the gold post)
    chosen = None
    if CHANNEL2:
        values, chosen, err = collect(CHANNEL2, wanted=("gold", "fx"))
        if err:
            out["note"] = err
        elif values:
            out["post_date"] = values.pop("post_date", None)
            out["values"] = values

    # Legacy channel: fuel (and full fallback if primary yielded nothing)
    if CHANNEL:
        wanted = ("fuel",) if out["values"] else ("gold", "fx", "fuel")
        lv, lchosen, lerr = collect(CHANNEL, wanted=wanted)
        if lv:
            lv.pop("post_date", None) if out["values"] else None
            merge_part(out["values"], lv, ("gold", "fx", "fuel", "post_date"))
            if out.get("post_date") is None:
                out["post_date"] = out["values"].pop("post_date", None)
            else:
                out["values"].pop("post_date", None)
            if chosen is None:
                chosen = lchosen

    if not out["values"]:
        out["note"] = out["note"] or "no price post found on any channel"
        write(out)
        return

    if chosen:
        out["post_url"] = chosen["url"]
        out["posted_at"] = chosen["posted_at"]
        if chosen["posted_at"]:
            try:
                posted = datetime.fromisoformat(
                    chosen["posted_at"].replace("Z", "+00:00"))
                age = (now - posted).total_seconds() / 3600
                out["age_hours"] = round(age, 1)
                out["status"] = "ok" if age <= STALE_AFTER_HOURS else "stale"
                out["posted_at_mmt"] = posted.astimezone(MMT).isoformat()
            except ValueError:
                out["status"] = "ok"
        else:
            out["status"] = "ok"
    else:
        out["status"] = "ok"

    out["note"] = "fetched successfully"
    write(out)


def write(out: dict):
    # Scrub here, not in main(): every exit path passes through write(),
    # which also cleans the stdout dump that lands in the public CI log.
    if PUBLIC_MODE:
        for k in ("channel", "channel_url", "post_url", "note"):
            out.pop(k, None)
        out["source"] = "community"
        out["source_label_my"] = "လူမှုကွန်ရက်မှ စုစည်းသည်"
        out["source_label_en"] = "community-reported"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "prices_latest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    if out.get("values"):
        hist = OUT_DIR / "history"
        hist.mkdir(parents=True, exist_ok=True)
        day = datetime.now(MMT).strftime("%Y-%m-%d")
        path = hist / f"{day}.json"
        rows = []
        if path.exists():
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                rows = []

        def key(r):
            g = (r.get("values") or {}).get("gold") or {}
            return (r.get("post_date"), g.get("gold_16pe"),
                    (r.get("values") or {}).get("wg_usd"))

        if not any(key(r) == key(out) for r in rows):
            rows.append(out)
            path.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2),
                encoding="utf-8")

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
    sys.exit(0)
