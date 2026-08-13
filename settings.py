# ---- MM Market Radar (cloud) settings ----
#
# Feed status measured from a GitHub Actions runner, 2026-08-13:
#   direct OK   : BBC Burmese, Myanmar Now (EN/MM), RFA
#   403         : Irrawaddy, Frontier, GNLM -- Cloudflare blocks datacenter IPs
#   404         : Myawady -- /feed/ does not exist on that site
#   SSLError    : Khit Thit -- TLS handshake fails; verify=False does not help,
#                 since that only skips certificate validation
#
# Everything unreachable directly is read through Google News RSS, which is
# not blocked. Trade-off: titles and links only, thin summaries, and links
# route via a Google redirect. Weaker input for the classifier, but real
# coverage instead of silence.

FEEDS = {
    # --- direct ---
    "BBC Burmese":      "https://feeds.bbci.co.uk/burmese/rss.xml",
    "Myanmar Now (EN)": "https://myanmar-now.org/en/feed/",
    "Myanmar Now (MM)": "https://myanmar-now.org/mm/feed/",
    "RFA (EN)":         "https://www.rfa.org/arc/outboundfeeds/english/rss/",

    # --- via Google News (English) ---
    # when:7d dropped here: it returned 0 and 1 entries respectively, while
    # GNLM with the same operator returned 100. The operator is unreliable.
    "Irrawaddy":        "https://news.google.com/rss/search?q=site:irrawaddy.com&hl=en-US&gl=US&ceid=US:en",
    "Frontier Myanmar": "https://news.google.com/rss/search?q=site:frontiermyanmar.net&hl=en-US&gl=US&ceid=US:en",
    "GNLM":             "https://news.google.com/rss/search?q=site:gnlm.com.mm+when:7d&hl=en-US&gl=US&ceid=US:en",

    # --- via Google News (Burmese) ---
    "Khit Thit":        "https://news.google.com/rss/search?q=site:yktnews.com&hl=my&gl=MM&ceid=MM:my",
    "Myawady":          "https://news.google.com/rss/search?q=site:myawady.net.mm&hl=my&gl=MM&ceid=MM:my",

    # DVB removed 2026-07: their new site no longer offers RSS.
}

# Feeds whose TLS certificate chain is broken. Requests to these skip
# certificate verification.
#
# TRADE-OFF: without verification, a network attacker between the runner and
# the site could serve forged headlines. Low practical risk for public RSS,
# but not zero. Empty now that nothing is fetched directly over a bad chain.
INSECURE_TLS = set()

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
