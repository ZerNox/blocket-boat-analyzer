"""The cabin-boat model list: which models count as "like a Sandström 560 MC".

Derived from the data (whole-market listings + our own hull analysis), then hand-edited:
cabin_models.csv is the one list the Blocket model search, Klaravik and the Marketplace
links all use. `boats models` regenerates it; rows with keep=no are remembered across runs.
"""

from __future__ import annotations

import collections
import csv
import re
import statistics

from . import market, store
from .config import ROOT

MODELS_CSV = ROOT / "cabin_models.csv"
CABIN_CLASSES = {"Hyttbåt", "Kabinbåt", "Daycruiser"}
OPEN_CLASSES = {"Styrpulpetbåt", "Bowrider", "Fiskebåt/Arbetsbåt"}
FIELDS = ["family", "keep", "search", "ads", "cabin_share", "length_ft", "years", "variants"]


def _num(family: str) -> int | None:
    m = re.search(r"\b[a-zåäö]{0,2}(\d{2,4})\b", family)
    if not m:
        return None
    n = int(m.group(1))
    return n // 10 if n >= 1000 else n


def _pretty(family: str, make_names: dict[str, str]) -> str:
    """'örnvik 575' -> 'Örnvik 575' using the make's own spelling from the ads."""
    mk, _, rest = family.partition(" ")
    # "d55" -> "D55", "ht" -> "HT"; plain numbers and names stay as they are
    rest = rest.upper() if re.fullmatch(r"[a-zåäö]{1,2}\d{0,4}[a-z]{0,2}", rest) else rest
    return f"{make_names.get(mk, mk.capitalize())} {rest}".strip()


def derive(min_len: float = 16.5, max_len: float = 21.5, min_cabin_share: float = 0.5, min_evidence: int = 2) -> list[dict]:
    market.learn_from_store()
    rows = market.load()
    make_names: dict[str, str] = {}
    fam: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        r["family"], r["variant"] = market.model_keys(r.get("make"), None, r.get("heading"))
        if r.get("make"):
            make_names.setdefault(r["make"].lower(), r["make"])
        if r["family"]:
            fam[r["family"]].append(r)
    hull: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for a in store.all_ads():
        e = a.get("extraction") or {}
        f, _ = market.model_keys(a.get("make"), (a.get("specs") or {}).get("Modell"), a.get("heading"))
        if f and e.get("hull") and e.get("hull_certain"):
            hull[f][e["hull"]] += 1

    out = []
    for f, ads in fam.items():
        n = _num(f)
        lens = [x["length"] for x in ads if x.get("length") and 12 <= x["length"] <= 30]
        length = n / 30.48 if n and 350 <= n <= 800 else (statistics.median(lens) if lens else None)
        if not length or not min_len <= length <= max_len:
            continue
        outboard = sum(1 for x in ads if (x.get("motor_type") or "").startswith("Utomb"))
        inboard = sum(1 for x in ads if (x.get("motor_type") or "").startswith("Inomb"))
        if inboard > outboard:
            continue
        h = hull.get(f, collections.Counter())
        cabin = sum(1 for x in ads if x.get("boat_class") in CABIN_CLASSES) + h["cabin"]
        open_ = sum(1 for x in ads if x.get("boat_class") in OPEN_CLASSES) + h["open"]
        share = cabin / max(cabin + open_, 1)
        if share < min_cabin_share or cabin < min_evidence:
            continue
        years = [x["year"] for x in ads if x.get("year") and x["year"] > 1950]
        variants = collections.Counter(x["variant"].split()[-1] for x in ads if x.get("variant"))
        out.append({
            "family": f,
            "keep": "yes",
            "search": _pretty(f, make_names),
            "ads": len(ads),
            "cabin_share": round(share, 2),
            "length_ft": round(length, 1),
            "years": f"{min(years)}-{max(years)}" if years else "",
            "variants": " ".join(v for v, _ in variants.most_common(4)),
        })
    out.sort(key=lambda r: (-r["ads"] * r["cabin_share"], r["family"]))
    return out


def load(include_dropped: bool = False) -> list[dict]:
    if not MODELS_CSV.exists():
        return []
    with open(MODELS_CSV, newline="") as fh:
        rows = list(csv.DictReader(fh))
    return rows if include_dropped else [r for r in rows if r.get("keep", "yes").strip().lower() != "no"]


def write(rows: list[dict]) -> None:
    """Regenerate, keeping hand edits: keep=no stays no, custom search terms stay, manual rows stay."""
    old = {r["family"]: r for r in load(include_dropped=True)}
    merged = []
    for r in rows:
        prev = old.pop(r["family"], None)
        if prev:
            r["keep"], r["search"] = prev.get("keep", "yes"), prev.get("search") or r["search"]
        merged.append(r)
    merged += list(old.values())  # rows added by hand, or no longer derived: keep them
    with open(MODELS_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(merged)


def families() -> set[str]:
    return {r["family"] for r in load()}


def search_terms() -> list[str]:
    return [r["search"] for r in load()]
