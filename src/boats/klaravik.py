"""Klaravik auctions: boats whose model is on the cabin-boat list (cabin_models.csv).

Klaravik has no useful boat-type filter, so the model list is the filter. An auction has no
fixed price: the ranked "price" is what you'd pay at the current bid — bid + VAT (when the
seller is a company and the item isn't sold under the margin scheme) + the auction fee incl.
VAT — and the card shows the bid, the end time and whether the reserve price is reached.

Ads are stored in the same git DB with source "klaravik" and ad_id = 1_000_000_000 + Klaravik id.
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import re
import sys

from . import market, models_list, rules, store

BASE = "https://www.klaravik.se"
LISTING = BASE + "/auktion/fordon/batar-marint/batar-marint/"
ID_OFFSET = 1_000_000_000
VAT_ON_FEE = 1.25

# "Märke: Finnmaster Modell: 5700 År: 2007 Båttyp: Motorbåt ... Längd (m): 5,7 m"
SPEC = re.compile(r"(?<![\w(])([A-ZÅÄÖ][\wåäö ]{1,30}?(?:\s\([^)]{1,10}\))?)\s*:\s*(.+?)(?=\s+[A-ZÅÄÖ][\wåäö ]{1,30}?(?:\s\([^)]{1,10}\))?\s*:|$)")


def ad_id(klaravik_id: int) -> int:
    return ID_OFFSET + int(klaravik_id)


def is_klaravik(ad: dict) -> bool:
    return ad.get("source") == "klaravik"


def listing_urls(client) -> list[str]:
    urls: list[str] = []
    for page in range(1, 10):
        r = client.get(LISTING, [("page", str(page))] if page > 1 else None)
        found = [BASE + u for u in re.findall(r'/auktion/produkt/\d+-[a-z0-9-]+/', r.text)]
        new = [u for u in dict.fromkeys(found) if u not in urls]
        if not new:
            break
        urls += new
    return urls


def parse_product(page: str) -> dict:
    """Name, description, specs, auction data and place from a Klaravik product page."""
    product = {}
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if d.get("@type") == "Product":
            product = d
    auction = {}
    m = re.search(r":product-data='(\{.*?\})'", page, re.S)
    if m:
        try:
            auction = json.loads(htmllib.unescape(m.group(1)))
        except json.JSONDecodeError:
            pass
    times = re.search(r'"auction_start":"([^"]+)","auction_close":"([^"]+)"', page)
    ended = re.search(r'"ended":(true|false)', page)
    title = re.search(r"<title>([^<]+)</title>", page)
    place = None
    if title:  # "Motorbåt Finnmaster 5700, Simrishamn, Klaravik auktioner"
        parts = [p.strip() for p in htmllib.unescape(title.group(1)).split(",")]
        place = parts[-2] if len(parts) >= 3 else None
    desc = re.sub(r"\s+", " ", htmllib.unescape(product.get("description") or "")).strip()
    specs = {k.strip(): v.strip() for k, v in SPEC.findall(desc)}
    return {
        "id": int(product.get("sku") or auction.get("id") or 0),
        "name": htmllib.unescape(product.get("name") or ""),
        "brand": (product.get("brand") or {}).get("name"),
        "model": product.get("model"),
        "description": desc,
        "specs": specs,
        "images": product.get("image") or [],
        "auction": auction,
        "start": times.group(1) if times else None,
        "close": times.group(2) if times else None,
        "ended": bool(ended and ended.group(1) == "true"),
        "place": place,
    }


def buyer_total(auction: dict) -> tuple[int, int]:
    """(bid, what you'd pay at that bid incl. VAT and fee). VAT applies unless margin-scheme (vmb)."""
    bid = int((auction.get("bidBox") or {}).get("bid") or 0) or int(auction.get("nextBid") or 0)
    vat = float(auction.get("vat") or 0) if not auction.get("vmb") else 0.0
    fee = float(auction.get("auctionFee") or 0)
    return bid, int(round(bid * (1 + vat / 100) + fee * VAT_ON_FEE))


_PLACES: dict[str, tuple[float, float]] | None = None


def place_coords(place: str | None) -> tuple[float | None, float | None]:
    """Town -> mean coordinates of its postcodes (site/postnummer.json, GeoNames)."""
    global _PLACES
    if not place:
        return None, None
    if _PLACES is None:
        from .config import ROOT

        acc: dict[str, list] = {}
        for name, lat, lon in json.loads((ROOT / "site" / "postnummer.json").read_text()).values():
            acc.setdefault(name.lower(), []).append((lat, lon))
        _PLACES = {k: (round(sum(a for a, _ in v) / len(v), 2), round(sum(b for _, b in v) / len(v), 2)) for k, v in acc.items()}
    return _PLACES.get(place.lower(), (None, None))


def _length_ft(specs: dict) -> float | None:
    # Keys can carry a section header: "Mått och vikt Längd (m)"
    v = next((val for key, val in specs.items() if key.endswith("Längd (m)") or key.endswith("Längd")), None)
    m = re.search(r"(\d+(?:[.,]\d+)?)", v or "")
    if not m:
        return None
    x = float(m.group(1).replace(",", "."))
    x = x / 100 if x > 50 else x  # "575" cm
    return round(x * 3.281, 1) if 3 <= x <= 20 else None


def to_ad(p: dict, url: str, prev: dict | None, today: str) -> dict:
    bid, total = buyer_total(p["auction"])
    year = re.search(r"(19[5-9]\d|20[0-4]\d)", p["specs"].get("År", "") or p["specs"].get("Årsmodell", "") or "")
    lat, lon = place_coords(p["place"])
    hp = rules.extract_hp(p["name"], p["description"], {})[0]
    ad = dict(prev or {"first_seen": today, "price_history": []})
    ad.update({
        "ad_id": ad_id(p["id"]),
        "source": "klaravik",
        "url": url,
        "heading": store.scrub(p["name"], 200),
        "make": p["brand"],
        "boat_class": None,
        "location": p["place"],
        "lat": lat,
        "lon": lon,
        "dealer_segment": "Auktion",
        "price": total,
        "year": int(year.group(1)) if year else None,
        "motor_type": None,
        "motor_size": hp,
        "length_ft": _length_ft(p["specs"]),
        "image": p["images"][0] if p["images"] else None,
        "image_urls": p["images"],
        "specs": {"Märke": p["brand"], "Modell": p["model"], **{k: v for k, v in p["specs"].items() if len(v) < 80}},
        "auction": {
            "bid": bid,
            "next_bid": p["auction"].get("nextBid"),
            "fee": p["auction"].get("auctionFee"),
            "vat": p["auction"].get("vat"),
            "margin_scheme": bool(p["auction"].get("vmb")),
            "reserve_reached": bool(p["auction"].get("reservePriceReached") or p["auction"].get("zeroReserve")),
            "start": p["start"],
            "close": p["close"],
            "ended": p["ended"],
        },
        "last_seen": today,
    })
    hist = ad["price_history"]
    if not hist or hist[-1]["price"] != total:
        hist.append({"date": today, "price": total, "bid": bid})
    return ad


def update(client, cfg: dict) -> dict:
    """Fetch active boat auctions, keep those on the cabin-boat list, settle ended ones."""
    today = dt.date.today().isoformat()
    wanted = models_list.families()
    market.learn_from_store()
    stats = {"klaravik_listed": 0, "klaravik_kept": 0, "klaravik_ended": 0}
    seen = set()
    for url in listing_urls(client):
        stats["klaravik_listed"] += 1
        r = client.get(url)
        if r.status_code != 200:
            continue
        p = parse_product(r.text)
        if not p["id"]:
            continue
        family, _ = market.model_keys(p["brand"], p["model"], p["name"])
        if family not in wanted:
            continue
        prev = store.load_ad(ad_id(p["id"]))
        ad = to_ad(p, url, prev, today)
        ad["status"] = "active" if not p["ended"] else "ended"
        ad["found_by"] = ["klaravik", family]
        from .parse import text_sha
        import time

        ad["page"] = {"fetched_ts": int(time.time() * 1000), "text_sha": text_sha(ad["heading"] or "", p["description"], ad["specs"]),
                      "description_chars": len(p["description"])}
        store.save_ad(ad)
        store.save_page(ad["ad_id"], r.text)
        seen.add(ad["ad_id"])
        stats["klaravik_kept"] += 1
    # Auctions that left the listing have ended: record the final price and whether it sold.
    for ad in store.all_ads():
        if is_klaravik(ad) and ad.get("status") == "active" and ad["ad_id"] not in seen:
            r = client.get(ad["url"])
            p = parse_product(r.text) if r.status_code == 200 else None
            if p and p["id"]:
                bid, total = buyer_total(p["auction"])
                reached = bool(p["auction"].get("reservePriceReached") or p["auction"].get("zeroReserve"))
                ad["status"] = "sold" if reached and bid else "unsold"
                ad["sold"] = {"date": today, "last_price": total, "final_bid": bid, "reserve_reached": reached}
            else:
                ad["status"] = "inactive"
            store.save_ad(ad)
            stats["klaravik_ended"] += 1
    print(f"  klaravik: {stats}", file=sys.stderr)
    return stats


def cached_text(ad: dict) -> dict | None:
    page = store.load_page(ad["ad_id"])
    if page is None:
        return None
    p = parse_product(page)
    return {"heading": ad.get("heading") or p["name"], "title": p["name"], "description": p["description"],
            "specs": ad.get("specs") or p["specs"]}
