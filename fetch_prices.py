#!/usr/bin/env python3
"""
Fetch gold / FX / fuel prices from a public Telegram channel.

The channel handle is supplied at runtime via the TG_CHANNEL environment
variable and never appears in this file.

Set PUBLIC_MODE=1 to strip all source-identifying fields (channel name,
channel URL, post URL, diagnostic notes and flags) from everything this
script writes, including the stdout dump that lands in CI logs.

Set DEBUG_LINES=1 to dump parsed lines for diagnosing format changes.
Do not enable DEBUG_LINES on a public repository: it prints raw post text.

Usage:
    TG_CHANNEL=<handle> DATA_DIR=docs/data PUBLIC_MODE=1 python fetch_prices.py
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

CHANNEL = os.environ.get("TG_CHANNEL", "").strip().lstrip("@")
OUT_DIR = Path(os.environ.get("DATA_DIR", "data"))
DEBUG = os.environ.get("DEBUG_LINES", "").strip() not in ("", "0", "false")
PUBLIC_MODE = os.environ.get("PUBLIC_MODE", "").strip() not in ("", "0", "false")
UA = "Mozilla/5.0 (compatible; MarketRadarMM/1.0)"

MMT = timezone(timedelta(hours=6, minutes=30))
STALE_AFTER_HOURS = float(os.environ.get("STALE_AFTER_HOURS", "72"))

BURMESE_DIGITS = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\ufeff"), None)


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.translate(ZERO_WIDTH)
    text = text.translate(BURMESE_DIGITS)      # ၁၆ -> 16, ၁၅ -> 15
    text = text.replace("\xa0", " ")
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]", " ", text)
    return re.sub(r"[ \t]+", " ", text)


# Note: no \s in the class. Whitespace must never be part of a number,
# or a line break can glue two separate values into one bad token.
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def to_number(fragment: str):
    """
    First number in the fragment. All whitespace stripped defensively,
    commas removed.

    The source sometimes drops a comma ('9540,000' for '9,540,000');
    removing all commas yields the intended value, so this is correct.
    """
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
# Patterns
# --------------------------------------------------------------------------

# After normalise(), ၁၆ / ၁၅ are ASCII, so these work on the digits alone
# and survive any Burmese encoding mismatch in the label text.
PURE_MARKERS = ("အခေါက်", "16")      # ၁၆ ပဲရည် / အခေါက်ရွှေ -- 99.9%
LOWER_MARKERS = ("15",)              # ၁၅ ပဲရည်
PRICE_LABEL = "ပေါက်စျေး"

FX_RE = re.compile(
    r"\b(USD|THB|SGD|JPY|AUD|CNY|EUR|MYR|KRW|GBP)\b\.?\s*[-:]?\s*([\d.,]+)",
    re.IGNORECASE,
)
FUEL_RE = re.compile(r"\b(MDY|YGN)\s*(92|95)\s*[-–—]\s*([\d,]+)")
DATE_RE = re.compile(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(20\d{2})\b")

GOLD_MIN, GOLD_MAX = 1_000_000, 100_000_000

BOUNDS = {
    "USD": (1_000, 20_000), "THB": (30, 600), "SGD": (1_000, 10_000),
    "JPY": (5, 200), "AUD": (1_000, 10_000), "CNY": (200, 2_000),
    "EUR": (1_000, 20_000), "MYR": (300, 5_000), "KRW": (1, 50),
    "GBP": (1_000, 25_000), "fuel": (500, 20_000),
}


def in_bounds(key, value) -> bool:
    lo, hi = BOUNDS.get(key, (None, None))
    return True if lo is None else lo <= value <= hi


def gold_numbers_in(line: str):
    """Every millions-scale number on this one line, in order."""
    found = []
    for m in NUM_RE.finditer(line):
        v = to_number(m.group(0))
        if v is not None and GOLD_MIN <= v <= GOLD_MAX:
            found.append(int(v))
    return found


def parse_post(text: str) -> dict:
    raw = normalise(text)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    if DEBUG:
        print("---- parsed lines ----", file=sys.stderr)
        for i, l in enumerate(lines):
            print(f"{i:3d}: {l!r}", file=sys.stderr)
        print("----------------------", file=sys.stderr)

    gold, fx, fuel = {}, {}, {}
    post_date = None
    section = None
    ordered_gold = []          # every gold-scale number, document order

    for line in lines:
        date_hit = DATE_RE.search(line)
        fx_hit = bool(FX_RE.search(line)) or bool(FUEL_RE.search(line))

        if date_hit and post_date is None:
            d, m, y = (int(g) for g in date_hit.groups())
            if 1 <= d <= 31 and 1 <= m <= 12:
                post_date = f"{y:04d}-{m:02d}-{d:02d}"

        # Grade markers. Skip date and currency lines: a bare "15" would
        # otherwise match inside a rate like "USD 4415" or a date "15.8.2026".
        # Check the lower grade first so "15ပဲရည်ရွှေ" isn't caught by the
        # broader pure-gold test.
        if not date_hit and not fx_hit:
            if any(k in line for k in LOWER_MARKERS):
                section = "gold_15pe"
            elif any(k in line for k in PURE_MARKERS):
                section = "gold_16pe"

        ordered_gold.extend(gold_numbers_in(line))

        if PRICE_LABEL in line and section and section not in gold:
            candidates = gold_numbers_in(line.split(PRICE_LABEL, 1)[1])
            if candidates:
                gold[section] = candidates[0]
                section = None
                continue

        for code, rawv in FX_RE.findall(line):
            code = code.upper()
            if code not in fx:
                v = to_number(rawv)
                if v is not None and in_bounds(code, v):
                    fx[code] = v

        for city, octane, rawv in FUEL_RE.findall(line):
            v = to_number(rawv)
            if v is not None and in_bounds("fuel", v):
                fuel[f"{city}_{octane}"] = int(v)

    # Fallback, no Burmese needed: these posts always list the pure grade
    # first, then the lower grade.
    used_fallback = False
    if len(gold) < 2 and ordered_gold:
        if "gold_16pe" not in gold:
            gold["gold_16pe"] = ordered_gold[0]
            used_fallback = True
        if "gold_15pe" not in gold and len(ordered_gold) > 1:
            gold["gold_15pe"] = ordered_gold[1]
            used_fallback = True

    # Order sanity: the pure grade is always the dearer of the two.
    swapped = False
    if "gold_16pe" in gold and "gold_15pe" in gold:
        if gold["gold_15pe"] > gold["gold_16pe"]:
            gold["gold_16pe"], gold["gold_15pe"] = gold["gold_15pe"], gold["gold_16pe"]
            swapped = True

    out = {"_used_fallback": used_fallback, "_swapped": swapped}
    if gold:
        out["gold"] = gold
    if fx:
        out["fx"] = fx
    if fuel:
        out["fuel"] = fuel
    if post_date:
        out["post_date"] = post_date
    return out


def sanity_flags(values: dict) -> list:
    flags = []
    if values.pop("_used_fallback", False):
        flags.append("gold read positionally, not from labels")
    if values.pop("_swapped", False):
        flags.append("grades were out of order and have been swapped")
    gold = values.get("gold", {})
    pure, lower = gold.get("gold_16pe"), gold.get("gold_15pe")
    if pure and lower:
        ratio = lower / pure
        if not 0.88 <= ratio <= 0.99:
            flags.append(f"15pe/16pe ratio {ratio:.3f} outside usual range")
    elif pure and not lower:
        flags.append("only one gold grade found")
    return flags


# --------------------------------------------------------------------------

def fetch_posts(channel: str):
    resp = requests.get(
        f"https://t.me/s/{channel}", headers={"User-Agent": UA}, timeout=20
    )
    if resp.status_code == 429:
        raise RuntimeError("rate limited (429)")
    if resp.status_code != 200:
        # Deliberately does not echo the URL, which contains the handle.
        raise RuntimeError(f"HTTP {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")
    wrappers = soup.select("div.tgme_widget_message")
    if not wrappers:
        raise RuntimeError("no messages found -- preview disabled or channel gone")

    posts = []
    for w in wrappers:
        body = w.select_one("div.tgme_widget_message_text")
        stamp = w.select_one("a.tgme_widget_message_date time")
        pid = w.get("data-post", "")
        posts.append({
            "id": pid,
            "text": body.get_text("\n", strip=True) if body else "",
            "posted_at": stamp.get("datetime") if stamp else None,
            "url": f"https://t.me/{pid}" if pid else None,
        })
    posts.reverse()
    return posts


def main():
    now = datetime.now(timezone.utc)
    out = {
        "source": "telegram",
        "channel": CHANNEL,
        "channel_url": f"https://t.me/{CHANNEL}" if CHANNEL else None,
        "fetched_at": now.isoformat(),
        "status": "error",
        "confidence": "amber",
        "note": None,
        "flags": [],
        "post_url": None,
        "posted_at": None,
        "post_date": None,
        "age_hours": None,
        "labels": {
            "gold_16pe": {"my": "၁၆ ပဲရည် (အခေါက်ရွှေ)", "en": "16 pè yay (99.9%)"},
            "gold_15pe": {"my": "၁၅ ပဲရည်", "en": "15 pè yay"},
        },
        "unit": "MMK per tical (ကျပ်သား)",
        "values": {},
        "source_disclaimer_my": "ခန့်မှန်းစျေးသာဖြစ်ပြီး တစ်နေရာနှင့်တစ်နေရာ ကွာဟနိုင်ပါသည်",
        "source_disclaimer_en": "Estimates only; prices vary by location",
    }

    if not CHANNEL:
        out["note"] = "TG_CHANNEL is not set -- check the repository secret"
        write(out)
        return

    try:
        posts = fetch_posts(CHANNEL)
    except Exception as exc:
        out["note"] = f"fetch failed: {exc}"
        write(out)
        return

    chosen, values = None, {}
    for p in posts:
        parsed = parse_post(p["text"])
        if parsed.get("gold") or parsed.get("fx"):
            chosen, values = p, parsed
            break

    if not chosen:
        out["note"] = "no price post found in recent messages"
        write(out)
        return

    out["post_url"] = chosen["url"]
    out["posted_at"] = chosen["posted_at"]
    out["post_date"] = values.pop("post_date", None)
    out["flags"] = sanity_flags(values)
    out["values"] = values

    if chosen["posted_at"]:
        try:
            posted = datetime.fromisoformat(chosen["posted_at"].replace("Z", "+00:00"))
            age = (now - posted).total_seconds() / 3600
            out["age_hours"] = round(age, 1)
            out["status"] = "ok" if age <= STALE_AFTER_HOURS else "stale"
            out["posted_at_mmt"] = posted.astimezone(MMT).isoformat()
        except ValueError:
            out["status"] = "ok"
    else:
        out["status"] = "ok"

    out["note"] = "fetched successfully"
    write(out)


def write(out: dict):
    # Scrub here, not in main(): every exit path goes through write(), and
    # this also cleans the stdout dump below, which lands in the CI log.
    if PUBLIC_MODE:
        for k in ("channel", "channel_url", "post_url", "note"):
            out.pop(k, None)
        out["source"] = "community"
        out["source_label_my"] = "လူမှုကွန်ရက်မှ စုစည်းသည်"
        out["source_label_en"] = "community-reported"
        out["flags"] = []

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "prices_latest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

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

        # post_url is stripped in PUBLIC_MODE, so dedupe on the reading
        # itself rather than on a field that would be None for every row.
        def key(r):
            g = (r.get("values") or {}).get("gold") or {}
            return (r.get("post_date"), g.get("gold_16pe"))

        if not any(key(r) == key(out) for r in rows):
            rows.append(out)
            path.write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
    sys.exit(0)
