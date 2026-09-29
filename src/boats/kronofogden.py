"""Kronofogden (Swedish Enforcement Authority) web auctions: seized boats.

Few boats at a time (the "Båtar" category, id 08). Kept when the model is on the cabin-boat
list, or its variant code says cabin (a "Ryds 568 DC" is a daycruiser though most 568s are
open). No fees or VAT are added ("Inga avgifter tillkommer"); sold as is, not test-run, no
right of withdrawal — the listed defects ("Iakttagna brister") go into the text the red-flag
rules read. ad_id = 2_000_000_000 + the item number (F109182 -> 109182).
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import re
import sys
import time

from . import klaravik, market, models_list, rules, store
from .parse import text_sha

BASE = "https://auktion.kronofogden.se/auk/"
LISTING = BASE + "w.objectlist?inC=KFM&inA=WEB&inCategoryId=08"
ID_OFFSET = 2_000_000_000
MONTHS = {m: i for i, m in enumerate(
    ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti", "september", "oktober", "november", "december"], 1)}


def _text(page: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    t = htmllib.unescape(re.sub(r"<[^>]+>", "\n", t))
    t = re.sub(r"[ \t ]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t)


def listing(client) -> list[tuple[str, str | None]]:
    """(object url, address) for every boat in the category."""
    r = client.get(LISTING)
    out = []
    for m in re.finditer(r'href="(w\.object\?[^"]+)"', r.text):
        url = BASE + htmllib.unescape(m.group(1))
        if url in (u for u, _ in out):
            continue
        after = _text(r.text[m.end(): m.end() + 1500]).strip().splitlines()
        address = after[1].strip() if len(after) > 1 else None  # title line, then "Street 8, Town"
        out.append((url, address))
    return out


def parse_object(page: str) -> dict:
    t = _text(page)
    m = re.search(r"\n(F(\d{5,7}))\.\s*\n\s*(.+?)\n(.*?)\n\s*Varunr F\d+", t, re.S)
    if not m:
        return {"id": 0}
    item, num, title, body = m.group(1), int(m.group(2)), m.group(3).strip(), m.group(4).strip()
    end = re.search(r"(\d{1,2}) ([A-Za-zåäö]+) (20\d\d) (\d{1,2}):(\d{2})", t)
    close = None
    if end and end.group(2).lower() in MONTHS:
        close = dt.datetime(int(end.group(3)), MONTHS[end.group(2).lower()], int(end.group(1)),
                            int(end.group(4)), int(end.group(5))).isoformat()
    amount = re.search(r"Utrop\s*\n\s*([\d\s]+)\s*SEK", t)
    fees = "Inga avgifter tillkommer" not in t
    viewing = re.search(r"Visning\s*\nSker\s*\n(.+?)\n(.+?)\n", t)
    return {
        "id": num,
        "item": item,
        "title": title,
        "description": body,
        "close": close,
        "amount": int(re.sub(r"\s", "", amount.group(1))) if amount else 0,
        "ended": not re.search(r"Budgivning pågår", t),
        "fees_extra": fees,
        "viewing": " ".join(viewing.groups()).strip() if viewing else None,
        "images": list(dict.fromkeys(re.findall(r'https://[^"\s]+/aukpic/[^"?\s]+\.jpe?g', page))),
    }


def wanted(title: str, description: str) -> str | None:
    """The model family when it's on the cabin list, or when its variant code says cabin."""
    make = title.split()[0] if title else None
    for cand in (title, re.sub(r"^(?:Motorbåt|Båt|Hyttbåt|Kabinbåt)\s+", "", title, flags=re.I)):
        make = cand.split()[0] if cand else make
        family, _ = market.model_keys(make, None, cand)
        if family and (family in models_list.families() or rules.HULL_CODE_CABIN.search(cand)):
            return family
    return None


def update(client, cfg: dict) -> dict:
    today = dt.date.today().isoformat()
    market.learn_from_store()
    stats = {"kfm_listed": 0, "kfm_kept": 0, "kfm_ended": 0}
    seen = set()
    for url, _address in listing(client):
        stats["kfm_listed"] += 1
        r = client.get(url)
        p = parse_object(r.text) if r.status_code == 200 else {"id": 0}
        if not p["id"]:
            continue
        family = wanted(p["title"], p["description"])
        if not family:
            continue
        aid = ID_OFFSET + p["id"]
        ad = store.load_ad(aid) or {"first_seen": today, "price_history": []}
        # "Sker tisdagen den 29 september mellan 10:00 och 11:00 på Söderbyvägen 36, Upplands Väsby."
        where = re.search(r"\bpå\s+[^,]+,\s*([^.,\n]+)", p["viewing"] or "")
        town = where.group(1).strip() if where else None
        lat, lon = klaravik.place_coords(town)
        year = re.search(r"[Åå]rsmodell\s+(19[5-9]\d|20[0-4]\d)", p["description"])
        length = re.search(r"Längd\s+cirka\s+(\d+(?:[.,]\d+)?)\s*m", p["description"])
        ad.update({
            "ad_id": aid,
            "source": "kronofogden",
            "url": url,
            "heading": store.scrub(p["title"], 200),
            "make": p["title"].split()[0] if p["title"] else None,
            "boat_class": None,
            "location": town,
            "lat": lat,
            "lon": lon,
            "dealer_segment": "Auktion",
            "price": p["amount"],
            "year": int(year.group(1)) if year else None,
            "motor_type": None,
            "motor_size": rules.extract_hp(p["title"], p["description"], {})[0],
            "length_ft": round(float(length.group(1).replace(",", ".")) * 3.281, 1) if length else None,
            "image": p["images"][0] if p["images"] else None,
            "image_urls": p["images"],
            "specs": {"Märke": p["title"].split()[0], "Modell": " ".join(p["title"].split()[1:])},
            "auction": {"bid": p["amount"], "fee": 0, "vat": 0, "margin_scheme": True, "reserve_reached": True,
                        "close": p["close"], "ended": p["ended"], "viewing": p["viewing"], "as_is": True},
            "status": "active" if not p["ended"] else "ended",
            "found_by": ["kronofogden", family],
            "last_seen": today,
            "page": {"fetched_ts": int(time.time() * 1000), "text_sha": text_sha(p["title"], p["description"], {}),
                     "description_chars": len(p["description"])},
        })
        hist = ad["price_history"]
        if not hist or hist[-1]["price"] != p["amount"]:
            hist.append({"date": today, "price": p["amount"], "bid": p["amount"]})
        store.save_ad(ad)
        store.save_page(aid, r.text)
        seen.add(aid)
        stats["kfm_kept"] += 1
    for ad in store.all_ads():
        if ad.get("source") == "kronofogden" and ad.get("status") == "active" and ad["ad_id"] not in seen:
            r = client.get(ad["url"])
            p = parse_object(r.text) if r.status_code == 200 else {"id": 0}
            ad["status"] = "sold" if p.get("id") and p.get("amount") else "inactive"
            ad["sold"] = {"date": today, "last_price": p.get("amount") or ad.get("price")}
            store.save_ad(ad)
            stats["kfm_ended"] += 1
    print(f"  kronofogden: {stats}", file=sys.stderr)
    return stats


def cached_text(ad: dict) -> dict | None:
    page = store.load_page(ad["ad_id"])
    if page is None:
        return None
    p = parse_object(page)
    # "Inte startad eller funktionstestad", "Iakttagna brister: Skador ..." feed the red-flag rules.
    return {"heading": ad.get("heading") or p.get("title", ""), "title": p.get("title"),
            "description": p.get("description", ""), "specs": ad.get("specs") or {}}
