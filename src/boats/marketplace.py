"""Import Facebook Marketplace listings saved with the bookmarklet (site/marketplace-grab.js).

Facebook disallows automated access, so nothing here fetches from Facebook: the bookmarklet
saves what the user is looking at in their own browser, and this imports those JSON files.
A search page gives title, price and town per card; a listing page adds the full text, which
the rules, the text LLM and the ranking then use like any other ad. The full text stays in
the local cache (gitignored); the git DB gets the structured fields.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

from . import klaravik, market, models_list, rules, store
from .config import CACHE_DIR
from .parse import text_sha

INBOX = Path.home() / "Hämtningar"
INBOX_FALLBACK = Path.home() / "Downloads"


def _price(text: str | None) -> int | None:
    if not text:
        return None
    if re.search(r"gratis|free", text, re.I):
        return 0
    digits = re.sub(r"[^\d]", "", text.split("kr")[0])
    return int(digits) if digits else None


def _text_path(ad_id: int) -> Path:
    return CACHE_DIR / "marketplace" / f"{ad_id}.json"


def _description(full: str, title: str) -> str:
    """The seller's text from a listing page's main column (drops navigation and chrome)."""
    for marker in ("Säljarens beskrivning", "Beskrivning", "Seller's description", "Description", "Detaljer", "Details"):
        i = full.find(marker)
        if i >= 0:
            full = full[i + len(marker):]
            break
    full = re.split(r"\n(?:Information om säljaren|Seller information|Säljarinformation|Dagens val|Today's picks)\b", full)[0]
    return full.strip() or title


def import_files(paths: list[Path], cfg: dict) -> dict:
    today = dt.date.today().isoformat()
    stats = {"files": 0, "items": 0, "new": 0, "with_text": 0, "not_on_list": 0}
    market.learn_from_store()
    wanted = models_list.families()
    makes = {f.split()[0] for f in wanted}
    for path in paths:
        data = json.loads(path.read_text())
        if data.get("source") != "facebook-marketplace":
            continue
        stats["files"] += 1
        for it in data.get("items", []):
            stats["items"] += 1
            aid = int(it["id"])
            # Same rule as the auctions: the model is on the cabin list, or its variant code says cabin.
            title_raw = it.get("title") or ""
            family, _ = market.model_keys(title_raw.split()[0] if title_raw else None, None, title_raw)
            listed_make = (title_raw.split()[0].lower() if title_raw else "") in makes
            cabin_title = bool(rules.HULL_CODE_CABIN.search(title_raw) or rules.CABIN_CUE.search(title_raw))
            on_list = family in wanted or rules.HULL_CODE_CABIN.search(title_raw)
            # From a "Märke hytt" search the title may lack the model number: a listed make plus a
            # cabin word in the title is enough ("Örnvik hyttbåt med Suzuki 90").
            if store.load_ad(aid) is None and not (on_list or (listed_make and cabin_title)):
                stats["not_on_list"] += 1
                continue
            ad = store.load_ad(aid)
            if ad is None:
                stats["new"] += 1
                ad = {"first_seen": it.get("saved") or today, "price_history": []}
            price = _price(it.get("price"))
            town = (it.get("location") or ad.get("location") or "").split(",")[0].strip() or None
            if not town and it.get("text"):
                # A listing page: the town is the line under the price.
                ls = [x.strip() for x in it["text"].splitlines() if x.strip()]
                i = next((k for k, x in enumerate(ls) if it.get("price") and x == it["price"].strip()), None)
                cand = ls[i + 1].split(",")[0] if i is not None and i + 1 < len(ls) else None
                town = cand if cand and klaravik.place_coords(cand)[0] is not None else None
            lat, lon = klaravik.place_coords(town)
            title = store.scrub(it.get("title") or ad.get("heading") or "", 200)
            ad.update({
                "ad_id": aid,
                "source": "marketplace",
                "url": it["url"],
                "heading": title,
                "make": title.split()[0] if title else None,
                "location": town,
                "lat": lat if lat is not None else ad.get("lat"),
                "lon": lon if lon is not None else ad.get("lon"),
                "dealer_segment": "Privat",
                "status": "active",
                "found_by": ["marketplace", it.get("query") or "sparad"],
                "last_seen": today,
                "image": it.get("image") or ad.get("image"),
            })
            if price is not None:
                ad["price"] = price
                hist = ad.setdefault("price_history", [])
                if not hist or hist[-1]["price"] != price:
                    hist.append({"date": today, "price": price})
            if it.get("text"):
                desc = _description(it["text"], title)
                p = _text_path(aid)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"heading": title, "description": desc}, ensure_ascii=False))
                year = re.search(r"(?:årsmodell|år|modellår)\D{0,5}(19[5-9]\d|20[0-4]\d)", desc, re.I)
                ad["year"] = ad.get("year") or (int(year.group(1)) if year else None)
                ad["motor_size"] = ad.get("motor_size") or rules.extract_hp(title, desc, {})[0]
                ad["page"] = {"fetched_ts": int(time.time() * 1000), "text_sha": text_sha(title, desc, {}),
                              "description_chars": len(desc)}
                stats["with_text"] += 1
            else:
                ad.setdefault("motor_size", rules.extract_hp(title, "", {})[0])
            ad.setdefault("specs", {})
            store.save_ad(ad)
        done = path.with_suffix(".imported.json")
        path.rename(done)
    print(f"  marketplace: {stats}", file=sys.stderr)
    return stats


def inbox_files() -> list[Path]:
    return sorted(p for d in (INBOX, INBOX_FALLBACK) if d.exists() for p in d.glob("marketplace-*.json")
                  if not p.name.endswith(".imported.json"))


def cached_text(ad: dict) -> dict | None:
    p = _text_path(ad["ad_id"])
    if not p.exists():
        # A card from a search page only: the title is all we have.
        return {"heading": ad.get("heading") or "", "description": "", "specs": {}}
    d = json.loads(p.read_text())
    return {"heading": d["heading"], "description": d["description"], "specs": {}}
