"""
Rule-based (zero-cost) classifier for Myanmar market news.

Bilingual (Burmese + English) keyword rules. Two kinds of rules:
  - directional: the direction is obvious from the trigger itself
    (e.g. new sanctions -> kyat bearish, gold bullish)
  - watch: the article clearly touches a market, but direction needs a
    human eye -> flagged as "neutral" with watch=True so you still get pinged.

Returns the same JSON shape as the LLM classifier, so the pipeline doesn't
care which brain is plugged in. Edit RULES freely — this file IS the model.

MATCHING (changed 2026-08):
Latin-script patterns are matched on word boundaries, not as substrings.
The old `pattern in text` test fired "muse" (the Shan border town) on the
word "Museum", which put rice/pulses/fuel signals on a story about a
library visit. It also fired "tur" (the pulse) on return, future, Turkey,
structure and Saturday.

Burmese patterns are still matched as substrings: Burmese does not use
spaces between words, so word boundaries are meaningless, and Burmese
terms here are long enough to be specific on their own.
"""

import re

_BURMESE = re.compile(r"[\u1000-\u109F]")


def _matcher(pattern):
    """Return a callable(text) -> bool for one pattern."""
    p = pattern.lower()
    if _BURMESE.search(p):
        return lambda text: p in text
    # Latin: require non-alphanumeric on both sides. Works for phrases too.
    rx = re.compile(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])")
    return lambda text: bool(rx.search(text))


RULES = [
    # ---- kyat / gold macro triggers -------------------------------------
    {
        "name": "sanctions",
        "patterns": ["sanction", "sanctions", "sanctioned", "ဒဏ်ခတ်",
                     "ပိတ်ဆို့အရေးယူ", "စီးပွားရေးပိတ်ဆို့", "အရေးယူပိတ်ဆို့"],
        "signals": [
            ("fx_kyat", "bearish", 0.65, "New sanctions pressure the kyat"),
            ("gold", "bullish", 0.65, "Kyat pressure drives savings into gold"),
        ],
    },
    {
        "name": "cbm_forex",
        # "surrender" alone removed: it fired on troops surrendering, which
        # is constant in Myanmar coverage. The FX sense is always qualified.
        "patterns": ["central bank", "ဗဟိုဘဏ်", "forex", "foreign exchange",
                     "surrender requirement", "export earnings surrender",
                     "ငွေလဲနှုန်း", "နိုင်ငံခြားငွေ"],
        "watch": True,
        "signals": [
            ("fx_kyat", "neutral", 0.5, "CBM/forex news — read for direction"),
            ("gold", "neutral", 0.5, "Kyat-sensitive: gold usually moves opposite to kyat"),
        ],
    },
    {
        "name": "dollar_rate",
        "patterns": ["dollar rate", "exchange rate", "ဒေါ်လာဈေး", "ဒေါ်လာစျေး"],
        "watch": True,
        "signals": [
            ("fx_kyat", "neutral", 0.5, "FX-rate story — check which way"),
            ("gold", "neutral", 0.5, "Domestic gold tracks the dollar rate"),
        ],
    },
    {
        "name": "world_gold",
        "patterns": ["gold price", "world gold", "ရွှေဈေး", "ရွှေစျေး", "ကမ္ဘာ့ရွှေ"],
        "watch": True,
        "signals": [("gold", "neutral", 0.5, "Direct gold-market story")],
    },
    # ---- pulses / beans --------------------------------------------------
    {
        "name": "india_pulses_easing",
        "patterns": ["duty-free", "duty free", "import duty", "အခွန်ကင်းလွတ်"],
        "requires_any": ["pigeon pea", "pigeon peas", "black gram", "tur",
                         "urad", "matpe", "pulses", "မတ်ပဲ", "ပဲစင်းငုံ",
                         "ပဲတီစိမ်း", "ကုလားပဲ"],
        "signals": [
            ("pulses", "bullish", 0.6, "India easing pulse imports lifts Myanmar bean demand"),
        ],
    },
    {
        "name": "pulses_general",
        "patterns": ["pigeon pea", "pigeon peas", "black gram", "matpe",
                     "urad", "mung bean", "mung beans", "pulses export",
                     "မတ်ပဲ", "ပဲစင်းငုံ", "ပဲတီစိမ်း"],
        "watch": True,
        "signals": [("pulses", "neutral", 0.5, "Bean-market story — read for direction")],
    },
    # ---- rice ------------------------------------------------------------
    {
        "name": "rice",
        "patterns": ["rice export", "rice exports", "rice price", "rice prices",
                     "paddy", "ဆန်ဈေး", "ဆန်စျေး", "ဆန်တင်ပို့", "စပါး"],
        "watch": True,
        "signals": [("rice", "neutral", 0.5, "Rice-market story — read for direction")],
    },
    # ---- border trade / logistics ---------------------------------------
    {
        "name": "border_disruption",
        # "muse" is the Shan State border town. With word-boundary matching
        # it no longer fires on "Museum".
        "patterns": ["muse", "chinshwehaw", "မူဆယ်", "ချင်းရွှေဟော်",
                     "border trade", "နယ်စပ်ကုန်သွယ်"],
        "watch": True,
        "signals": [
            ("pulses", "neutral", 0.5, "China border trade affects bean flows"),
            ("rice", "neutral", 0.5, "Border status affects rice exports"),
            ("fuel", "neutral", 0.45, "Border routes also carry imported goods"),
        ],
    },
    {
        "name": "agri_heartland_conflict",
        "patterns": ["sagaing", "magway", "စစ်ကိုင်း", "မကွေး"],
        "requires_any": ["fighting", "clash", "clashes", "battle", "airstrike",
                         "airstrikes", "တိုက်ပွဲ", "လေကြောင်း", "ပစ်ခတ်"],
        "signals": [
            ("pulses", "bullish", 0.55, "Conflict in bean/sesame heartland threatens supply"),
        ],
    },
    # ---- fuel / edible oil ----------------------------------------------
    {
        "name": "fuel",
        "patterns": ["fuel price", "fuel prices", "petrol", "diesel",
                     "စက်သုံးဆီ", "ဓာတ်ဆီ", "ဒီဇယ်"],
        "watch": True,
        "signals": [("fuel", "neutral", 0.5, "Fuel-market story — read for direction")],
    },
    {
        "name": "edible_oil",
        "patterns": ["palm oil", "edible oil", "cooking oil",
                     "စားအုန်းဆီ", "စားသုံးဆီ"],
        "watch": True,
        "signals": [("edible_oil", "neutral", 0.5, "Edible-oil story — read for direction")],
    },
]

# Compile once at import.
for _rule in RULES:
    _rule["_p"] = [_matcher(p) for p in _rule["patterns"]]
    if "requires_any" in _rule:
        _rule["_r"] = [_matcher(p) for p in _rule["requires_any"]]


def _hit(text, matchers):
    return any(m(text) for m in matchers)


def _replaces(new_dir, new_conf, prev):
    """
    Directional beats neutral, always. Only compare confidence when both
    are the same kind.

    The old test (`prev neutral and new directional) or conf > prev conf`)
    let a neutral 0.6 overwrite a directional 0.55, discarding the
    direction — which is the part worth having.
    """
    if prev is None:
        return True
    new_directional = new_dir != "neutral"
    prev_directional = prev["direction"] != "neutral"
    if new_directional != prev_directional:
        return new_directional
    return new_conf > prev["confidence"]


def classify_rules(title, summary, source):
    text = f"{title}\n{summary}".lower()
    markets, watch, fired = {}, False, []

    for rule in RULES:
        if not _hit(text, rule["_p"]):
            continue
        if "_r" in rule and not _hit(text, rule["_r"]):
            continue
        fired.append(rule["name"])
        watch = watch or rule.get("watch", False)
        for market, direction, conf, why in rule["signals"]:
            if _replaces(direction, conf, markets.get(market)):
                markets[market] = {"market": market, "direction": direction,
                                   "confidence": conf, "horizon": "weeks",
                                   "rationale": why}

    return {
        "relevant": bool(markets),
        "headline_summary": (f"rules matched: {', '.join(fired)}" if fired else ""),
        "markets": list(markets.values()),
        "watch": watch,
    }
