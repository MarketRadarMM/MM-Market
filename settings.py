# ---- MM Market Radar (cloud) settings ----
#
# Feed status as of 2026-08-13, measured from a GitHub Actions runner:
#   working  : BBC Burmese, Myanmar Now (EN), Myanmar Now (MM)
#   HTTP 403 : Irrawaddy, Frontier Myanmar, GNLM  -- Cloudflare blocks the
#              runner's datacenter IP; read via Google News instead
#   SSLError : Khit Thit, Myawady -- incomplete certificate chain
#   0 entries: RFA -- old feed path predates their 2025/26 site rebuild

FEEDS = {
    # --- direct, confirmed working ---
    "BBC Burmese":      "https://feeds.bbci.co.uk/burmese/rss.xml",
    "Myanmar Now (EN)": "https://myanmar-now.org/en/feed/",
    "Myanmar Now (MM)": "https://myanmar-now.org/mm/feed/",

    # --- direct, TLS verification disabled (see INSECURE_TLS below) ---
    "Khit Thit":        "https://yktnews.com/feed/",
    "Myawady":          "https://www.myawady.net.mm/feed/",

    # --- via Google News, because the site 403s our runner ---
    # Titles and links only; summaries are thin, so classification is weaker.
    "Irrawaddy":        "https://news.google.com/rss/search?q=site:irrawaddy.com+when:7d&hl=en-US&gl=US&ceid=US:en",
    "Frontier Myanmar": "https://news.google.com/rss/search?q=site:frontiermyanmar.net+when:7d&hl=en-US&gl=US&ceid=US:en",
    "GNLM":             "https://news.google.com/rss/search?q=site:gnlm.com.mm+when:7d&hl=en-US&gl=US&ceid=US:en",

    # --- RFA: news production was suspended Oct 2025 and has only partly
    #     resumed. This is their current site-wide English feed, not a
    #     Burma-specific one, so most items will classify as irrelevant.
    "RFA (EN)":         "https://www.rfa.org/arc/outboundfeeds/english/rss/",

    # DVB removed 2026-07: their new site no longer offers RSS.
}

# Feeds whose TLS certificate chain is broken. Requests to these skip
# certificate verification.
#
# TRADE-OFF: without verification, a network attacker between the runner and
# the site could serve forged headlines. The practical risk is low for public
# RSS, but it is not zero. Remove a name from this set to fail closed instead.
INSECURE_TLS = {"Khit Thit", "Myawady"}

# Publishing switches. To go back to fully discreet output, set both to False.
# SHOW_ALL_NEWS: website lists every article (non-market ones marked neutral).
# INCLUDE_LINKS: article titles link to the source; Telegram alerts carry the link.
SHOW_ALL_NEWS = True
INCLUDE_LINKS = True
# Keep this many signals on the public site (newest first).
MAX_SIGNALS = 300
# Remember this many seen-article fingerprints (hashed — raw URLs are never
# stored in the repo).
MAX_SEEN = 6000
