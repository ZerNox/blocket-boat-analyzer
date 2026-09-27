"""Build the static GitHub Pages site: site/index.html + generated boats.json."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

from . import rank, store
from .config import ROOT


def build(cfg: dict, out: Path) -> dict:
    ads = store.all_ads()
    ranked, model = rank.rank(ads, cfg)
    active = [a for a in ads if a.get("status") == "active"]
    ext = [a.get("extraction") or {} for a in active]
    coverage = {
        "active_ads": len(active),
        "ranked": len(ranked),
        "engine_year_known": sum(1 for e in ext if e.get("engine_year")),
        "engine_year_by_source": _count(e.get("engine_year_source") for e in ext if e.get("engine_year")),
        "engine_type": _count(e.get("engine_type") for e in ext),
    }
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "site" / "index.html", out / "index.html")
    shutil.copy(ROOT / "site" / "postnummer.json", out / "postnummer.json")
    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "search_url": cfg["search"]["url"],
        "min_price": cfg["ranking"]["min_price"],
        "home": cfg["search"].get("home"),
        "max_km": cfg["search"].get("max_km"),
        "coverage": coverage,
        "model": model,
        "boats": ranked,
    }
    (out / "boats.json").write_text(json.dumps(payload, ensure_ascii=False))
    return coverage


def _count(values) -> dict:
    c: dict = {}
    for v in values:
        c[str(v)] = c.get(str(v), 0) + 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))
