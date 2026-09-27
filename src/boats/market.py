"""Market-wide comparables: every boat ad on Blocket (search listing only, no ad pages).

The ranked search is too narrow to learn what a specific model is worth, so the whole
market is snapshotted into db/market.jsonl (one compact line per ad, sorted by id, so git
diffs are per-ad and history accumulates). Model keys are normalised from make + model name
("Sandström 560 MC" -> family "sandström 560", variant "sandström 560 mc").
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys

from . import store
from .config import ROOT
from .scrape import Client, search

MARKET = ROOT / "db" / "market.jsonl"
ALL_BOATS = "https://www.blocket.se/mobility/search/boat"
FIELDS = ("price", "year", "make", "heading", "boat_class", "motor_type", "motor_fuel", "motor_size",
          "length", "dealer_segment")

_STOP = re.compile(
    r"^(?:med|m|inkl\w*|samt|\+|&|och|utan|mercury|merc|mariner|yamaha|suzuki|honda|evinrude|johnson|tohatsu|"
    r"selva|parsun|hidea|volvo|penta|mercruiser|trailer\w*|kärra\w*|vagn\w*|\d{1,3}(?:hk|hp)|säljes|salu)$"
)
_SKIP_NEXT = ("hk", "hp", "km", "km/h", "kmh", "trailer", "kärra", "vagn", "liter", "l", "st", "m", "meter", "fot", "cm")
_MODEL_NUM = re.compile(r"^([a-zåäö]{0,3})-?(\d{1,4})([a-zåäö]{0,4})$")
_GENERIC = {"båt", "båten", "fiskebåt", "styrpulpet", "styrpulpetbåt", "snipa", "eka", "roddbåt", "motorbåt", "hyttbåt",
            "daycruiser", "säljes", "fin", "fint", "ny", "nyservad", "höstpris", "hösterbjudande", "prissänkt", "sport",
            "fishing", "classic", "de", "en", "ett", "min", "vår", "the"}
# Words naming a model line rather than the boat in general: they make a variant.
_QUALIFIERS = {"classic", "basic", "sport", "fishing", "cabin", "open", "cruiser", "fisher", "family"}
# Named models without a number ("Buster L", "Jofa Kuling"), learned from ads' Modell field: make -> names
NAMED_MODELS: dict[str, set[str]] = {}


def _tokens(text: str) -> list[str]:
    text = re.sub(r"(\d)[.,](\d)", r"\1\2", text.lower())  # "Askeladden 4.30" -> 430
    text = re.sub(r"(\d{3,4})x\d{2,4}", r"\1", text)      # "450x175" -> 450
    return [t for t in re.split(r"[\s,./()|:;!–]+", text) if t]


def learn_named_models(pairs) -> None:
    """(make, Modell field) pairs from fetched ads -> vocabulary of number-less model names."""
    for make, model in pairs:
        if not make or not model:
            continue
        toks = [t for t in _tokens(model) if t != make.lower()]
        if toks and not re.search(r"\d", toks[0]) and toks[0] not in _GENERIC and len(toks[0]) <= 12:
            NAMED_MODELS.setdefault(make.lower(), set()).add(toks[0])


def model_keys(make: str | None, model: str | None, heading: str | None) -> tuple[str | None, str | None]:
    """(family, variant) keys, e.g. ("sandström 560", "sandström 560 mc"); None when no model is found."""
    if not make or make.lower() in ("övriga", "annat", "other"):
        return None, None
    mk = make.lower().strip()
    text = (model or heading or "").lower()
    if not model:
        if mk not in text:
            return None, None
        text = text.split(mk, 1)[1]  # the model follows the make in the heading
    toks = [t for t in _tokens(text) if t not in (mk, mk + "s")]
    qualifier = None  # "Sandström Classic 560" is a different boat from "Sandström 560 MC"
    for i, tok in enumerate(toks[:5]):
        if tok in _QUALIFIERS:
            qualifier = qualifier or tok
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if tok in NAMED_MODELS.get(mk, ()):
            num = _MODEL_NUM.match(nxt)
            return f"{mk} {tok}", f"{mk} {tok} {num.group(2)}" if num and nxt not in _SKIP_NEXT else None
        if _STOP.match(tok):
            break  # reached the engine / trailer / sale description
        m = _MODEL_NUM.match(tok)
        if m:
            if nxt in _SKIP_NEXT or re.fullmatch(r"(?:19|20)\d\d", tok) and not model or tok.startswith("-"):
                continue
            prefix, num, suffix = m.groups()
            family = f"{mk} {prefix}{num}"
            if not suffix and re.fullmatch(r"[a-zåäö]{1,4}", nxt) and not _STOP.match(nxt) and nxt not in _GENERIC:
                suffix = nxt
            if not suffix and nxt in _QUALIFIERS:
                suffix = nxt  # "Sandström 565 Classic"
            suffix = suffix or qualifier
            return family, f"{family} {suffix}" if suffix else None
        if model and i == 0 and tok not in _GENERIC and not re.search(r"\d", tok):
            family = f"{mk} {tok}"
            num = _MODEL_NUM.match(nxt)
            return family, f"{family} {num.group(2)}" if num and nxt not in _SKIP_NEXT else None
    return None, None


def _row(d: dict) -> dict:
    row = {"id": int(d["id"]), "seen": dt.date.today().isoformat()}
    for f in FIELDS:
        v = d.get(f)
        if f == "price":
            v = (v or {}).get("amount")
        if v is not None:
            row[f] = v
    row["family"], row["variant"] = model_keys(d.get("make"), None, d.get("heading"))
    if row.get("heading"):
        row["heading"] = store.scrub(row["heading"], 200)  # sellers put phone numbers in headings too
    return row


def load() -> list[dict]:
    if not MARKET.exists():
        return []
    return [json.loads(line) for line in MARKET.read_text().splitlines() if line.strip()]


def learn_from_store() -> None:
    learn_named_models((a.get("make"), (a.get("specs") or {}).get("Modell")) for a in store.all_ads())


def update(cfg: dict) -> dict:
    """Snapshot the whole boat market. Ads that disappear keep their last row, marked gone."""
    learn_from_store()
    client = Client(cfg["search"]["delay_s"])
    print("searching the whole boat market…", file=sys.stderr)
    docs = search(client, ALL_BOATS)
    old = {r["id"]: r for r in load()}
    today = dt.date.today().isoformat()
    rows: dict[int, dict] = {}
    for d in docs:
        r = _row(d)
        prev = old.get(r["id"])
        r["first_seen"] = (prev or {}).get("first_seen", today)
        rows[r["id"]] = r
    gone = 0
    for i, r in old.items():
        if i not in rows:
            r = {**r, "gone": r.get("gone") or today}
            gone += r["gone"] == today
            rows[i] = r
    MARKET.write_text("".join(json.dumps(rows[i], ensure_ascii=False, sort_keys=True) + "\n" for i in sorted(rows)))
    active = [r for r in rows.values() if not r.get("gone")]
    return {"active": len(active), "with_family": sum(1 for r in active if r.get("family")), "gone_today": gone}
