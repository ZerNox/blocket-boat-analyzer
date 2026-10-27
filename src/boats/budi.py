"""Budi auctions (budi.se): boats in the "Fordon > Båtar" subcategory.

Budi is a general online auction house (surplus, returns, bankruptcy estates), so the boat
subcategory is small and mixed with outboards and gear; kept when the model is on the cabin-boat
list, or its variant code says cabin — the same rule as Kronofogden. The site is server-rendered
(ASP.NET, no JSON state): the category page lists cards whose data attributes carry id, end time,
ended flag and city; "?p=N" returns the first 60*N cards, so we page until nothing new shows up.
The object page has everything else: a schema.org Product (name, description, images), the bid
box's data attributes (current bid ex VAT, its VAT rate, bid step, service fee and its type) and
an analytics blob with the reserve price state. The JSON bid info the page JS polls,
/api/wwwapi/auctionobject/<id>/bidinfo, has the same numbers plus Budi's own totals; we don't need
the extra request.

What the buyer pays (https://www.budi.se/support/faq, "Vad innebär serviceavgift?",
"Tillkommer moms på serviceavgiften?", "Vad innebär det när det på utropet står ex. moms";
https://www.budi.se/info/villkor-konsument §7-8):
- bids are shown ex VAT; when the object is sold with VAT (company seller, vattype 0) 25 % is added
  to the bid; "inkl. ej avdragsgill moms" objects (vattype 2: margin scheme / private seller) add none;
- a service fee ("serviceavgift") is added on every winning bid, always plus 25 % VAT. It is either
  fixed per object (type 1, e.g. 4 900 kr ex VAT on a 50 000 kr Nimbus) or dynamic (type 2: a
  percentage of the winning bid with a minimum, e.g. 16 %, min 195 kr);
- a hidden reserve price may apply; when it isn't reached the winner is contacted to negotiate.
So price = bid * (1 + VAT) + fee * 1.25, which matches the API's bidCurrentTotalToPayAmount.

ad_id = 4_000_000_000 + the Budi object id (/objekt/141837/... -> 141837).
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import re
import sys
import time

from . import klaravik, market, models_list, rules, store
from .parse import text_sha

BASE = "https://www.budi.se"
LISTING = BASE + "/kategori/fordon/batar"
ID_OFFSET = 4_000_000_000
FEE_VAT = 1.25  # "25 % moms läggs alltid på serviceavgiften"
PAGE_SIZE = 60
GENERIC = r"(?:Motorbåt|Båt|Hyttbåt|Kabinbåt|Daycruiser|Motorbat|Bat)"  # boat-type words, not makes


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'\b{re.escape(name)}="([^"]*)"', tag)
    return htmllib.unescape(m.group(1)) if m else None


def _num(s: str | None) -> float:
    try:
        return float(s) if s not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_listing(page: str) -> list[dict]:
    """The object cards on a category page: id, url, title, city, close, ended, card bid."""
    out = []
    for m in re.finditer(r'<a href="(/objekt/(\d+)/[^"]+)"(.*?)</a>', page, re.S):
        tag = m.group(0)[: m.group(0).find(">") + 1]
        body = m.group(3)
        title = re.search(r'<p class="mb-0 pt-1" title="([^"]*)"', body)
        bid = re.search(r'bid-current-amount[^>]*>([^<]+)<', body)
        out.append({
            "id": int(m.group(2)),
            "url": BASE + htmllib.unescape(m.group(1)),
            "title": htmllib.unescape(title.group(1)) if title else "",
            "city": _attr(tag, "data-budi-auctionobject-citykey"),
            "close": _attr(tag, "data-budi-auctionobject-endingdatetimeiso"),
            "ended": _attr(tag, "data-budi-auctionobject-isended") == "true",
            "bid": int(re.sub(r"\D", "", htmllib.unescape(bid.group(1))) or 0) if bid else None,
        })
    return out


def listing(client) -> list[dict]:
    """Every card in the boat subcategory (each "?p=N" returns the first 60*N)."""
    cards: dict[int, dict] = {}
    for page in range(1, 20):
        r = client.get(LISTING, [("p", str(page))] if page > 1 else None)
        found = parse_listing(r.text)
        new = [c for c in found if c["id"] not in cards]
        cards.update((c["id"], c) for c in new)
        if not new or len(found) < PAGE_SIZE * page:
            break
    return list(cards.values())


def parse_object(page: str) -> dict:
    """Name, description, specs, auction state and place from a Budi object page."""
    product = {}
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            d = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if d.get("@type") == "Product":
            product = d
    box = re.search(r'<div[^>]*class="budi-bidplace[^"]*"[^>]*>', page, re.S)
    box = box.group(0) if box else ""
    info_tag = re.search(r'<div[^>]*data-isended="[^"]*"[^>]*>', page, re.S)
    info_tag = info_tag.group(0) if info_tag else ""
    analytics = {}
    m = re.search(r"addDefaultEventData\((\{.*?\})\);", page, re.S)
    if m:
        try:
            analytics = json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # "Auktionsavslut", "Visning", "Utlämning", "Adress" rows of the auction information block
    info = {}
    for r in re.finditer(r'budi-information__label[^>]*>(.*?)</div>\s*<span[^>]*>(.*?)</span>\s*</div>', page, re.S):
        info.setdefault(_clean(r.group(1)).rstrip(":"), _clean(r.group(2)))
    specs = {}
    for r in re.finditer(r'budi-property-item__text">(.*?)</span>\s*</span>\s*<span class="budi-property-item__value[^>]*>(.*?)</span>'
                         r'(?:\s*<span class="budi-property-item__unit[^>]*>(.*?)</span>)?', page, re.S):
        specs[_clean(r.group(1))] = f"{_clean(r.group(2))} {_clean(r.group(3) or '')}".strip()
    desc = re.sub(r"[ \t\xa0]+", " ", htmllib.unescape(product.get("description") or "")).replace("\r", "").strip()
    # Sellers often add a "Fakta" block: "Fabrikat: Nimbus", "Modell: 800 Turbo", "Längd: 8,0 meter"
    for k, v in re.findall(r"^([A-ZÅÄÖ][\wåäö ]{1,25}):\s*(.+)$", desc, re.M):
        specs.setdefault(k.strip(), v.strip())
    has_reserve = analytics.get("budi_auctionobject_hasreservationprice") == "true"
    met = analytics.get("budi_auctionobject_isreservationpricemet") == "true"
    return {
        "id": int(_attr(box, "data-budi-auctionobjectid") or _attr(info_tag, "data-auctionobjectid") or 0),
        "title": htmllib.unescape(product.get("name") or _attr(box, "data-budi-auctionobject-displayname") or ""),
        "description": desc,
        "specs": specs,
        "images": product.get("image") or [],
        "url": product.get("url"),
        "close": _attr(info_tag, "data-endingdatetimeiso"),
        "ended": _attr(info_tag, "data-isended") == "true",
        "bid": int(_num(_attr(box, "data-budi-bidcurrent-exvat"))),
        "bid_step": int(_num(_attr(box, "data-budi-bidincrement-exvat"))),
        "vat": _num(_attr(box, "data-budi-bidcurrent-vatpercentage")),
        "vat_type": _attr(box, "data-budi-vattype"),
        "fee": {
            "type": _attr(box, "data-budi-servicefee-type"),  # "1" fixed, "2" percentage with a minimum
            "ex_vat": _num(_attr(box, "data-budi-servicefee-exvat")),
            "vat": _num(_attr(box, "data-budi-servicefee-vatpercentage") or "25"),
            "bps": _num(_attr(box, "data-budi-servicefee-dynpercentage-bps")),
            "min_ex_vat": _num(_attr(box, "data-budi-servicefee-dynminexvat")),
        },
        "reserve_reached": met or not has_reserve,
        "place": info.get("Adress") or (analytics.get("budi_auctionobject_city") or "").capitalize() or None,
        "viewing": info.get("Visning"),
        "pickup": info.get("Utlämning"),
    }


def service_fee(p: dict, bid: int) -> float:
    """Service fee ex VAT at this bid: fixed, or a percentage of the bid but at least the minimum."""
    f = p["fee"]
    if f["type"] == "2" and f["bps"]:
        return max(f["min_ex_vat"], bid * f["bps"] / 10_000)
    return f["ex_vat"]


def buyer_total(p: dict) -> tuple[int, int, int]:
    """(bid, fee ex VAT, what you'd pay at that bid: bid + its VAT + fee incl. 25 % VAT)."""
    bid = p["bid"]
    fee = service_fee(p, bid)
    total = bid * (1 + p["vat"] / 100) + fee * (1 + p["fee"]["vat"] / 100)
    return bid, int(round(fee)), int(total + 0.5)


def wanted(title: str, description: str) -> str | None:
    """The model family when it's on the cabin list, or when its variant code says cabin."""
    # Titles lead with the boat type ("Motorbåt Nimbus 800 ..."): drop it, or "motorbåt 568" is a family.
    cands = [re.sub(rf"^{GENERIC}\s+", "", title or "", flags=re.I)]
    # The seller's "Fakta" block names the boat when the title leads with the engine or trailer.
    fakta = re.search(r"^Fabrikat:\s*(.+)$", description, re.M)
    modell = re.search(r"^Modell:\s*(.+)$", description, re.M)
    if fakta and modell:
        cands.append(f"{fakta.group(1).strip()} {modell.group(1).strip()}")
    for cand in cands:
        make = cand.split()[0] if cand else None
        if not make or re.fullmatch(GENERIC, make, re.I):
            continue
        family, _ = market.model_keys(make, None, cand)
        if family and (family in models_list.families() or rules.HULL_CODE_CABIN.search(cand)):
            return family
    return None


def _make_model(p: dict) -> tuple[str | None, str | None]:
    # Product.brand is unreliable (the Nimbus says "Volvo", its engine); prefer the seller's "Fabrikat".
    make = p["specs"].get("Fabrikat") or p["specs"].get("Märke")
    if make:
        return make, p["specs"].get("Modell")
    words = re.sub(rf"^{GENERIC}\s+", "", p["title"], flags=re.I).split()
    return (words[0] if words else None), (" ".join(words[1:]) or None)


def _length_ft(specs: dict) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)", specs.get("Längd") or "")
    if not m:
        return None
    x = float(m.group(1).replace(",", "."))
    x = x / 100 if x > 50 else x  # "575" cm
    return round(x * 3.281, 1) if 3 <= x <= 20 else None


def to_ad(p: dict, url: str, prev: dict | None, today: str, family: str) -> dict:
    bid, fee, total = buyer_total(p)
    make, model = _make_model(p)
    year = re.search(r"(19[5-9]\d|20[0-4]\d)", p["specs"].get("Årsmodell") or p["specs"].get("År") or "")
    lat, lon = klaravik.place_coords(p["place"])
    ad = dict(prev or {"first_seen": today, "price_history": []})
    ad.update({
        "ad_id": ID_OFFSET + p["id"],
        "source": "budi",
        "url": url,
        "heading": store.scrub(p["title"], 200),
        "make": make,
        "boat_class": None,
        "location": p["place"],
        "lat": lat,
        "lon": lon,
        "dealer_segment": "Auktion",
        "price": total,
        "year": int(year.group(1)) if year else None,
        "motor_type": None,
        "motor_size": rules.extract_hp(p["title"], p["description"], {})[0],
        "length_ft": _length_ft(p["specs"]),
        "image": p["images"][0] if p["images"] else None,
        "image_urls": p["images"],
        "specs": {"Märke": make, "Modell": model, **{k: v for k, v in p["specs"].items() if len(v) < 80}},
        "auction": {
            "bid": bid,
            "next_bid": bid + p["bid_step"] if p["bid_step"] else None,
            "fee": fee,
            "vat": p["vat"],
            "margin_scheme": p["vat_type"] == "2",  # "inkl. ej avdragsgill moms"
            "reserve_reached": p["reserve_reached"],
            "close": p["close"],
            "ended": p["ended"],
            "viewing": store.scrub(p["viewing"], 160),
            "as_is": True,  # "Säljs i befintligt skick"; no right of withdrawal on auction purchases
        },
        "status": "active" if not p["ended"] else "ended",
        "found_by": ["budi", family],
        "last_seen": today,
        "page": {"fetched_ts": int(time.time() * 1000), "text_sha": text_sha(p["title"], p["description"], p["specs"]),
                 "description_chars": len(p["description"])},
    })
    hist = ad["price_history"]
    if not hist or hist[-1]["price"] != total:
        hist.append({"date": today, "price": total, "bid": bid})
    return ad


def update(client, cfg: dict) -> dict:
    """Fetch running boat auctions, keep those on the cabin-boat list, settle ended ones."""
    today = dt.date.today().isoformat()
    market.learn_from_store()
    stats = {"budi_listed": 0, "budi_kept": 0, "budi_ended": 0}
    seen = set()
    for card in listing(client):
        stats["budi_listed"] += 1
        r = client.get(card["url"])
        p = parse_object(r.text) if r.status_code == 200 else {"id": 0}
        if not p["id"]:
            continue
        family = wanted(p["title"], p["description"])
        if not family:
            continue
        ad = to_ad(p, card["url"], store.load_ad(ID_OFFSET + p["id"]), today, family)
        store.save_ad(ad)
        store.save_page(ad["ad_id"], r.text)
        seen.add(ad["ad_id"])
        stats["budi_kept"] += 1
    # Auctions that left the listing have ended: record the final price and whether it sold.
    for ad in store.all_ads():
        if ad.get("source") == "budi" and ad.get("status") == "active" and ad["ad_id"] not in seen:
            r = client.get(ad["url"])
            p = parse_object(r.text) if r.status_code == 200 else None
            if p and p["id"] and p["ended"]:
                bid, _fee, total = buyer_total(p)
                ad["status"] = "sold" if p["reserve_reached"] and bid else "unsold"
                ad["sold"] = {"date": today, "last_price": total, "final_bid": bid, "reserve_reached": p["reserve_reached"]}
            elif p and p["id"]:
                continue  # still running, just dropped off the listing (moved category?); check next run
            else:
                ad["status"] = "inactive"
            store.save_ad(ad)
            stats["budi_ended"] += 1
    print(f"  budi: {stats}", file=sys.stderr)
    return stats


def cached_text(ad: dict) -> dict | None:
    page = store.load_page(ad["ad_id"])
    if page is None:
        return None
    p = parse_object(page)
    # "Anmärkningar ... Säljs i befintligt skick" in the description feeds the red-flag rules.
    return {"heading": ad.get("heading") or p["title"], "title": p["title"], "description": p["description"],
            "specs": ad.get("specs") or p["specs"]}
