#!/usr/bin/env python3
"""
MM Market Radar — cloud pipeline (runs on GitHub Actions, zero cost).

Each run: fetch feeds -> classify NEW articles with the free rules engine ->
append signals to docs/data/signals.json (which the website reads) ->
optionally post each signal to a Telegram channel.

Discreet by design: no outlet names or article URLs in any public output,
and only hashed fingerprints of seen articles are stored in the repo.
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests

from classifier_rules import classify_rules
from settings import FEEDS, SHOW_ALL_NEWS, INCLUDE_LINKS, MAX_SIGNALS, MAX_SEEN

BASE = os.path.dirname(os.path.abspath(__file__))
SIGNALS_PATH = os.path.join(BASE, "docs", "data", "signals.json")
BROWSER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SEEN_PATH = os.path.join(BASE, "docs", "data", "seen.json")

MARKET_LABELS = {
    "gold":       ("💰", "ရွှေ / Gold"),
    "fx_kyat":    ("💵", "ကျပ်ငွေ / Kyat FX"),
    "rice":       ("🌾", "ဆန် / Rice"),
    "pulses":     ("🌱", "ပဲမျိုးစုံ / Pulses"),
    "edible_oil": ("🛢", "စားသုံးဆီ / Edible oil"),
    "fuel":       ("⛽", "စက်သုံးဆီ / Fuel"),
}
DIR_LABELS = {"bullish": ("📈", "Bullish"), "bearish": ("📉", "Bearish"),
              "neutral": ("👀", "Watch")}


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def fingerprint(article_id):
    return hashlib.sha256(article_id.encode("utf-8")).hexdigest()[:20]


def fetch_new(seen):
    for source, feed_url in FEEDS.items():
        try:
            parsed = feedparser.parse(feed_url, agent=BROWSER_AGENT)
        except Exception as e:
            print(f"[warn] {source}: {e}", file=sys.stderr)
            continue
        for entry in parsed.entries:
            url = entry.get("link", "")
            article_id = entry.get("id") or url
            if not article_id:
                continue
            fp = fingerprint(article_id)
            if fp in seen:
                continue
            title = (entry.get("title") or "").strip()
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            yield fp, source, title, summary, url


def telegram_send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "disable_web_page_preview": True},
            timeout=30,
        )
    except Exception as e:
        print(f"[warn] telegram: {e}", file=sys.stderr)


def format_telegram(sig):
    lines = []
    for m in sig["markets"]:
        me, mlabel = MARKET_LABELS.get(m["market"], ("•", m["market"]))
        de, dlabel = DIR_LABELS.get(m["direction"], ("", m["direction"]))
        lines.append(f"{me} {mlabel} — {de} {dlabel} ({int(m['confidence']*100)}%)")
        lines.append(f"   ↳ {m['rationale']}")
    lines.append("")
    lines.append(f"📰 {sig['title']}")
    if INCLUDE_LINKS and sig.get("url"):
        lines.append(sig["url"])
    return "\n".join(lines)


def main():
    seen_list = load_json(SEEN_PATH, [])
    seen = set(seen_list)
    store = load_json(SIGNALS_PATH, {"generated_at": None, "signals": []})
    signals = store.get("signals", [])

    new_articles = 0
    new_signals = 0
    for fp, source, title, summary, url in fetch_new(seen):
        new_articles += 1
        seen.add(fp)
        seen_list.append(fp)

        result = classify_rules(title, summary, source)
        has_signal = bool(result.get("relevant") and result.get("markets"))
        if not has_signal and not SHOW_ALL_NEWS:
            continue

        sig = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "title": title,
            "source": source,
            "markets": [
                {"market": m["market"], "direction": m["direction"],
                 "confidence": round(m.get("confidence", 0), 2),
                 "rationale": m.get("rationale", "")}
                for m in result.get("markets", [])
            ] if has_signal else [],
        }
        if INCLUDE_LINKS and url:
            sig["url"] = url
        signals.insert(0, sig)
        if has_signal:
            new_signals += 1
            telegram_send(format_telegram(sig))   # Telegram: signals only, never the firehose
            time.sleep(1)  # keep Telegram happy

    store["signals"] = signals[:MAX_SIGNALS]
    store["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(SIGNALS_PATH, store)
    save_json(SEEN_PATH, seen_list[-MAX_SEEN:])
    print(f"done: {new_articles} new articles, {new_signals} signals")


if __name__ == "__main__":
    main()
