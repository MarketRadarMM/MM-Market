"""
Rule-based (zero-cost) classifier for Myanmar market news.

Bilingual (Burmese + English) keyword rules. Two kinds of rules:
  - directional: the direction is obvious from the trigger itself
    (e.g. new sanctions -> kyat bearish, gold bullish)
  - watch: the article clearly touches a market, but direction needs a
    human eye -> flagged as "neutral" with watch=True so you still get pinged.

Returns the same JSON shape as the LLM classifier, so main.py doesn't care
which brain is plugged in. Edit RULES freely — this file IS the model.
"""

RULES = [
    # ---- kyat / gold macro triggers -------------------------------------
    {
        "name": "sanctions",
        "patterns": ["sanction", "ဒဏ်ခတ်", "ပိတ်ဆို့အရေးယူ",
                     "စီးပွားရေးပိတ်ဆို့", "အရေးယူပိတ်ဆို့"],
        "signals": [
            ("fx_kyat", "bearish", 0.65, "New sanctions pressure the kyat"),
            ("gold", "bullish", 0.65, "Kyat pressure drives savings into gold"),
        ],
    },
    {
        "name": "cbm_forex",
        "patterns": ["central bank", "ဗဟိုဘဏ်", "forex", "foreign exchange",
                     "surrender", "ငွေလဲနှုန်း", "နိုင်ငံခြားငွေ"],
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
        "requires_any": ["pigeon pea", "black gram", "tur", "urad", "matpe",
                         "pulses", "မတ်ပဲ", "ပဲစင်းငုံ", "ပဲတီစိမ်း", "ကုလားပဲ"],
        "signals": [
            ("pulses", "bullish", 0.6, "India easing pulse imports lifts Myanmar bean demand"),
        ],
    },
    {
        "name": "pulses_general",
        "patterns": ["pigeon pea", "black gram", "matpe", "urad", "mung bean",
                     "pulses export", "မတ်ပဲ", "ပဲစင်းငုံ", "ပဲတီစိမ်း"],
        "watch": True,
        "signals": [("pulses", "neutral", 0.5, "Bean-market story — read for direction")],
    },
    # ---- rice ------------------------------------------------------------
    {
        "name": "rice",
        "patterns": ["rice export", "rice price", "paddy", "ဆန်ဈေး", "ဆန်စျေး",
                     "ဆန်တင်ပို့", "စပါး"],
        "watch": True,
        "signals": [("rice", "neutral", 0.5, "Rice-market story — read for direction")],
    },
    # ---- border trade / logistics ---------------------------------------
    {
        "name": "border_disruption",
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
        "requires_any": ["fighting", "clash", "battle", "airstrike", "တိုက်ပွဲ",
                         "လေကြောင်း", "ပစ်ခတ်"],
        "signals": [
            ("pulses", "bullish", 0.55, "Conflict in bean/sesame heartland threatens supply"),
        ],
    },
    # ---- fuel / edible oil ----------------------------------------------
    {
        "name": "fuel",
        "patterns": ["fuel price", "petrol", "diesel", "စက်သုံးဆီ", "ဓာတ်ဆီ", "ဒီဇယ်"],
        "watch": True,
        "signals": [("fuel", "neutral", 0.5, "Fuel-market story — read for direction")],
    },
    {
        "name": "edible_oil",
        "patterns": ["palm oil", "edible oil", "cooking oil", "စားအုန်းဆီ", "စားသုံးဆီ"],
        "watch": True,
        "signals": [("edible_oil", "neutral", 0.5, "Edible-oil story — read for direction")],
    },
]


def _hit(text, patterns):
    return any(p.lower() in text for p in patterns)


def classify_rules(title, summary, source):
    text = f"{title}\n{summary}".lower()
    markets, watch, fired = {}, False, []

    for rule in RULES:
        if not _hit(text, rule["patterns"]):
            continue
        if "requires_any" in rule and not _hit(text, rule["requires_any"]):
            continue
        fired.append(rule["name"])
        watch = watch or rule.get("watch", False)
        for market, direction, conf, why in rule["signals"]:
            prev = markets.get(market)
            # keep the strongest / most directional signal per market
            if prev is None or (prev["direction"] == "neutral" and direction != "neutral") \
               or conf > prev["confidence"]:
                markets[market] = {"market": market, "direction": direction,
                                   "confidence": conf, "horizon": "weeks",
                                   "rationale": why}

    return {
        "relevant": bool(markets),
        "headline_summary": (f"rules matched: {', '.join(fired)}" if fired else ""),
        "markets": list(markets.values()),
        "watch": watch,
    }
