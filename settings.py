# ---- MM Market Radar (cloud) settings ----

FEEDS = {
    "BBC Burmese":      "https://feeds.bbci.co.uk/burmese/rss.xml",
    "Myanmar Now (EN)": "https://myanmar-now.org/en/feed/",
    "Myanmar Now (MM)": "https://myanmar-now.org/mm/feed/",
    "Irrawaddy":        "https://www.irrawaddy.com/feed",
    "Frontier Myanmar": "https://www.frontiermyanmar.net/en/feed/",
    "RFA Burma (EN)":   "https://www.rfa.org/english/news/burma_news/rss2.xml",
    # DVB removed 2026-07: their new site no longer offers RSS.
}

# Discreet mode: public output (website + Telegram) carries NO outlet names
# and NO article links — only the market signal, headline text and rationale.
DISCREET = True

# Keep this many signals on the public site (newest first).
MAX_SIGNALS = 300

# Remember this many seen-article fingerprints (hashed — raw URLs are never
# stored in the repo).
MAX_SEEN = 6000
