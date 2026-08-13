#!/usr/bin/env python3
"""
MM Market Radar — cloud pipeline (runs on GitHub Actions, zero cost).

Each run: fetch feeds -> classify NEW articles with the free rules engine ->
append signals to docs/data/signals.json (which the website reads) ->
optionally post each signal to a Telegram channel.

Only hashed fingerprints of seen articles are stored in the repo. Outlet
names and article URLs appear in public output only when SHOW_ALL_NEWS /
INCLUDE_LINKS are enabled in settings.py.
"""

import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests
import urllib3

from classifier_rules import classify_rules
from settings import (FEEDS, SHOW_ALL_NEWS, INCLUDE_LINKS, MAX_SIGNALS,
                      MAX_SEEN, INSECURE_TLS)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = os.path.dirname(os.path.abspath(__file__))
SIGNALS_PATH = os.path.join(BASE, "docs", "data", "signals.json")
BROWSER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SEEN_PATH = os.path.join(BASE, "docs", "data", "seen.json")

# feedparser sends only a User-Agent. Bot filters usually also inspect
# Accept and Accept-Language, so fetch through requests with a fuller set
# of headers and hand the bytes to feedparser afterwards.
FEED_HEADERS = {
    "User-Agent": BROWSER_AGENT,
    "Accept": ("application/rss+xml, application/xml, text/xml, "
               "application/atom+xml;q=0.9, */*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9,my;q=0.8",
    "Cache-Control": "no-cache",
}

TAG_RE = re.compile(r"<[^>]+>")

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
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def fingerprint(article_id):
    return hashlib.sha256(article_id.encode("utf-8")).hexdigest()[:20]


def strip_html(s):
    """Feed summaries are HTML. Keyword rules should not see markup."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", html_mod.unescape(TAG_RE.sub(" ", s))).strip()


def fetch_feed(source, feed_url):
    """
    Return (entries, note). Never raises.

    feedparser.parse() does NOT raise on HTTP errors -- a 403, a Cloudflare
    challenge page or a 404 all return normally with entries == [] and bozo
    set. A blocked feed was therefore indistinguishable from a feed with no
    new articles, and failed silently. The note explains which it is.
    """
    verify = source not in INSECURE_TLS

    try:
        resp = requests.get(
            feed_url, headers=FEED_HEADERS, timeout=25,
            allow_redirects=True, verify=verify,
        )
    except Exception as e:
        return [], f"request failed: {type(e).__name__}"

    if resp.status_code != 200:
        return [], f"HTTP {resp.status_code}"

    body = resp.content
    ctype = (resp.headers.get("content-type") or "").lower()

    parsed = feedparser.parse(body)
    entries = parsed.entries or []

    if entries:
        tls = "" if verify else " (TLS unverified)"
        return entries, f"{len(entries)} entries{tls}"

    head = body[:200].lstrip().lower()
    if "html" in ctype or head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return [], "blocked -- served HTML instead of a feed"
    if getattr(parsed, "bozo", 0):
        exc = getattr(parsed, "bozo_exception", None)
        return [], f"parse error: {type(exc).__name__ if exc else 'unknown'}"
    return [], "0 entries"


def fetch_new(seen):
    for source, feed_url in FEEDS.items():
        # Google News descriptions are not article excerpts. They contain
        # OTHER headlines from the same publisher plus "View Full Coverage"
        # links, so feeding them to the classifier matched keywords from
        # unrelated articles. Use the title alone for these feeds.
        via_google = "news.google.com" in feed_url

        entries, note = fetch_feed(source, feed_url)
        print(f"[feed] {source:20s} {note}", file=sys.stderr)

        for entry in entries:
            url = entry.get("link", "")
            article_id = entry.get("id") or url
            if not article_id:
                continue
            fp = fingerprint(article_id)
            if fp in seen:
                continue

            title = (entry.get("title") or "").strip()
            if via_google:
                # Google News appends " - Publisher"; the source is already
                # shown separately in the UI.
                if " - " in title:
                    title = title.rsplit(" - ", 1)[0].strip()
                summary = ""
            else:
                summary = strip_html(
                    entry.get("summary") or entry.get("description") or ""
                )

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
