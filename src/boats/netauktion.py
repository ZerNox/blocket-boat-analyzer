"""Netauktion (netauktion.se): boats in the "Fordon > Båt och fartyg" category.

Netauktion (VAKab, Göteborg) runs online auctions for bankruptcy estates and companies, mostly in
western Sweden, so the boat category is small and mixed with outboards and trailers; kept when the
model is on the cabin-boat list, or its variant code says cabin — the same rule as Budi. The site is
server-rendered PHP (no JSON-LD, no embedded state). Live bid updates arrive over socket.io
(pusher-dev.netauktion.se) and POSTs to /ajaxfunctions.php (update_auction_status,
get_bid_calcuations), which robots.txt disallows; the HTML has everything we need:
- /kategori/bat-och-fartyg lists 20 cards a page ("?pagenumber=N"): product id, title, place
  ("Lagringsort"), end time and current bid; each card links to its auction, /auktion/<slug>?product=<id>;
- the auction page carries a modal per product whose "share by mail" link names the product page,
  /auktion/produkt/<slug> (one fetch per auction, not per product);
- the product page has the description, special terms, viewing/pickup, gallery, end time, current
  bid, minimum next bid, reserve state and whether bids are ex VAT. It stays up after the auction
  ends but looks the same — "AVSLUTAD" is toggled by JS — so ended means the end time has passed.

What the buyer pays (https://www.netauktion.se/conditions/, "INROPSAVGIFT" and "MOMS"):
- "budgivningen sker exkl moms om inget annat anges": a product says "All budgivning på detta objekt
  sker exklusive moms" (company seller: 25 % VAT is added to the bid) or "... med ej avd. moms" (VAT
  already inside and not deductible — private seller or margin scheme: nothing added);
- "Slag/inropsavgift tillkommer på vinnande bud med 12% upp till max 50 000kr exkl. moms. Slagavgift
  startar på 100kr": fee = min(max(100, 12 % of the bid), 50 000) ex VAT, always plus 25 % VAT.
  "Fast och/eller ingen slagavgift kan förekomma" — a per-object exception only shows in the bid box
  for logged-in members, so we can't see it.
So price = bid * (1 + VAT) + fee * 1.25. Checked against the site's own get_bid_calcuations and
top_bid_with_fee_and_tax (2026-10-27): 1 700 ex VAT -> 2 380; 10 000 ex VAT -> 14 000;
900 / 18 500 "ej avd. moms" -> 1 035 / 21 275.
A hidden reserve may apply; when it isn't reached the object goes to 24 h negotiation with the top
bidder ("förhandling"). Objects are sold "utan garanti/reklamationsrätt"; payment within three days.

ad_id = 7_000_000_000 + the Netauktion product id (?product=255373 -> 255373).
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import re
import sys
import time
from zoneinfo import ZoneInfo

from . import klaravik, market, models_list, rules, store
from .parse import text_sha

BASE = "https://www.netauktion.se"
LISTING = BASE + "/kategori/bat-och-fartyg"
ID_OFFSET = 7_000_000_000
VAT = 25.0  # added to the bid when bidding is "exklusive moms"
FEE_VAT = 1.25  # the fee is always ex VAT
FEE_RATE, FEE_MIN, FEE_MAX = 0.12, 100, 50_000
TZ = ZoneInfo("Europe/Stockholm")
TEST_ITEM = "provauktion"  # the site's always-open "try bidding here" product, on every auction page
# boat-type words, not makes ("Glasfiberbåt med styrpulpet, Silver Viking")
GENERIC = r"(?:Motorbåt|Båt|Hyttbåt|Kabinbåt|Daycruiser|Glasfiberbåt|Styrpulpetbåt|Motorbat|Bat)"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _text(s: str) -> str:
    """HTML block -> text keeping its line breaks."""
    s = re.sub(r"(?:<br\s*/?>|</p>)[ \t]*\r?\n?", "\n", s)  # "<br />\n" is one line break
    s = htmllib.unescape(re.sub(r"<[^>]+>", "", s)).replace("\r", "")
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", "\n".join(l.strip() for l in s.split("\n"))).strip()


def _kr(s: str | None) -> int:
    return int(re.sub(r"\D", "", s or "") or 0)


def _close_iso(s: str | None) -> str | None:
    """"2026-11-02 21:14(:00)" Swedish time -> ISO with offset."""
    m = re.search(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d(?::\d\d)?)", s or "")
    if not m:
        return None
    t = m.group(2) if m.group(2).count(":") == 2 else m.group(2) + ":00"
    return dt.datetime.fromisoformat(f"{m.group(1)}T{t}").replace(tzinfo=TZ).isoformat()


def parse_listing(page: str) -> list[dict]:
    """The product cards on a category page: id, auction url, title, place, close, card bid."""
    out = []
    for m in re.finditer(r"<article id='(\d+)'(.*?)</article>", page, re.S):
        body = m.group(2)
        link = re.search(r"<a href='(/auktion/[^']+)'", body)
        title = re.search(r'object-card-title-text">(.*?)</div>', body, re.S)
        place = re.search(r"Lagringsort:\s*([^<]+)<", body)
        close = re.search(r"Avslutas:\s*([^<]+)<", body)
        bid = re.search(r"object-card-current-bid[^>]*>([^<]+)<", body)
        reserve = re.search(r"res-price-text-\d+'>([^<]*)<", body)
        out.append({
            "id": int(m.group(1)),
            "auction_url": BASE + htmllib.unescape(link.group(1)) if link else None,
            "title": _clean(title.group(1)) if title else "",
            "place": htmllib.unescape(place.group(1)).strip() if place else None,
            "close": _close_iso(close.group(1)) if close else None,
            "bid": _kr(bid.group(1)) if bid else None,
            "reserve_reached": "ej" not in reserve.group(1) if reserve else None,
        })
    return out


def has_page(page: str, n: int) -> bool:
    """Whether the category pager links to page n."""
    return f"GoToPage({n})" in page


def parse_auction_links(page: str) -> dict[int, str]:
    """Product id -> product page url, from the auction page's per-product modals."""
    out = {}
    for m in re.finditer(r'id="object_modal_(\d+)"', page):
        nxt = page.find('id="object_modal_', m.end())
        link = re.search(r"https://www\.netauktion\.se/auktion/produkt/([a-z0-9-]+)", page[m.end(): nxt if nxt > 0 else None])
        if link and not link.group(1).startswith(TEST_ITEM):
            out[int(m.group(1))] = f"{BASE}/auktion/produkt/{link.group(1)}"
    return out


def listing(client) -> list[dict]:
    """Every card in the boat category, each with its product page url (one auction page per auction)."""
    cards: dict[int, dict] = {}
    for n in range(1, 30):
        r = client.get(LISTING, [("pagenumber", str(n))] if n > 1 else None)
        new = [c for c in parse_listing(r.text) if c["id"] not in cards]
        cards.update((c["id"], c) for c in new)
        if not new or not has_page(r.text, n + 1):
            break
    links: dict[str, dict[int, str]] = {}
    for c in cards.values():
        auction = (c["auction_url"] or "").split("?")[0]
        if auction and auction not in links:
            r = client.get(auction)
            links[auction] = parse_auction_links(r.text) if r.status_code == 200 else {}
        c["url"] = links.get(auction, {}).get(c["id"])
    return list(cards.values())


def _tab(page: str, name: str, pid: int) -> str:
    m = re.search(rf'<div id="{name}_{pid}"[^>]*>(.*?)</div>\s*(?:<div id="obj-|</div>)', page, re.S)
    return _text(m.group(1)) if m else ""


def parse_product(page: str, now: dt.datetime | None = None) -> dict:
    """Title, description, terms, specs, auction state and place from a Netauktion product page."""
    pid = re.search(r"object_single-(\d+)", page)
    if not pid:
        return {"id": 0}
    pid = int(pid.group(1))
    title = re.search(r"object_single-\d+.*?<h2>(.*?)</h2>", page, re.S)
    url = re.search(r"<meta property='og:url' content='([^']+)'", page)
    desc = _tab(page, "obj-info", pid)
    terms = _tab(page, "obj-terms", pid)
    pickup = _tab(page, "obj-pickup", pid)
    facts = dict(re.findall(r'fact-text">([^:<]+):\s*([^<]*)<', page))
    close = _close_iso(facts.get("Avslutas"))
    bid = re.search(rf'top-bid-single-{pid}">([^<]*)<', page)
    step = re.search(rf'new-min-bid-single-{pid}[^>]*value="(\d+)"', page)
    reserve = re.search(rf"res-price-text-single-{pid}\">(.*?)</span>", page, re.S)
    tax = re.search(r"class='bidding-tax'>([^<]*)<", page)
    tax = tax.group(1) if tax else ""
    images = list(dict.fromkeys(BASE + u for u in re.findall(r'<a href="(/uploads/[^"]+)" itemprop="contentUrl"', page)))
    specs = {k.strip(): v.strip() for k, v in facts.items() if k.strip() in ("Marknadsvärde", "Lagringsort")}
    # Descriptions often carry "Märke: Flipper", "Modell: 630 HT", "Längd: 6,3 m" lines.
    for k, v in re.findall(r"^([A-ZÅÄÖ][\wåäö ]{1,25}):\s*(.+)$", desc, re.M):
        specs.setdefault(k.strip(), v.strip().rstrip("."))
    viewing = re.search(r"Visning:\s*(.+)", pickup)
    handover = re.search(r"Utlämning:\s*(?:Utlämning:\s*)?(.+)", pickup)
    now = now or dt.datetime.now(TZ)
    return {
        "id": pid,
        "title": _clean(title.group(1)) if title else "",
        "description": desc + (f"\n\nSärskilda villkor:\n{terms}" if terms else ""),
        "specs": specs,
        "images": images,
        "url": url.group(1) if url else None,
        "close": close,
        "ended": bool(close) and dt.datetime.fromisoformat(close) <= now,
        "bid": _kr(bid.group(1)) if bid else 0,
        "next_bid": int(step.group(1)) if step else None,
        # "exklusive moms": company seller, VAT on top; "ej avd. moms": inside the bid, not deductible
        "vat": VAT if "exklusive moms" in tax else 0.0,
        "margin_scheme": "ej avd" in tax,
        "reserve_reached": not (reserve and re.search(r"ej\s+uppn", _clean(reserve.group(1)))),
        "place": facts.get("Lagringsort", "").strip() or None,
        "viewing": viewing.group(1).strip() if viewing else None,
        "pickup": handover.group(1).strip() if handover else None,
    }


def buyer_fee(bid: int) -> float:
    """Inropsavgift ex VAT: 12 % of the bid, at least 100 kr, at most 50 000 kr; none without a bid."""
    return min(max(FEE_MIN, bid * FEE_RATE), FEE_MAX) if bid else 0.0


def buyer_total(p: dict) -> tuple[int, int, int]:
    """(bid, fee ex VAT, what you'd pay at that bid: bid + its VAT + fee incl. 25 % VAT)."""
    bid = p["bid"]
    fee = buyer_fee(bid)
    total = bid * (1 + p["vat"] / 100) + fee * FEE_VAT
    return bid, int(round(fee)), int(total + 0.5)


def wanted(title: str, description: str) -> str | None:
    """The model family when it's on the cabin list, or when its variant code says cabin."""
    # Titles lead with the boat type, or put the make after a comma ("Glasfiberbåt med styrpulpet,
    # Silver Viking"): try the title without the type word, and each comma-separated part.
    cands = [re.sub(rf"^{GENERIC}\s+", "", p.strip(), flags=re.I) for p in [title or ""] + (title or "").split(",")[1:]]
    make = re.search(r"^(?:Fabrikat|Märke):\s*(.+)$", description, re.M)
    modell = re.search(r"^Modell:\s*(.+)$", description, re.M)
    if make and modell:
        cands.append(f"{make.group(1).strip()} {modell.group(1).strip()}")
    for cand in cands:
        mk = cand.split()[0] if cand else None
        if not mk or re.fullmatch(GENERIC, mk, re.I):
            continue
        family, _ = market.model_keys(mk, None, cand)
        if family and (family in models_list.families() or rules.HULL_CODE_CABIN.search(cand)):
            return family
    return None


def _make_model(p: dict) -> tuple[str | None, str | None]:
    make = p["specs"].get("Fabrikat") or p["specs"].get("Märke")
    if make:
        return make, p["specs"].get("Modell")
    title = p["title"].split(",")[-1] if "," in p["title"] else p["title"]
    words = re.sub(rf"^{GENERIC}\s+", "", title.strip(), flags=re.I).split()
    return (words[0] if words else None), (" ".join(words[1:]) or None)


def _length_ft(p: dict) -> float | None:
    # "Längd: 6,3 m", "Längd ca 308cm."
    m = re.search(r"(\d+(?:[.,]\d+)?)", p["specs"].get("Längd") or "") or \
        re.search(r"Längd\D{0,12}(\d+(?:[.,]\d+)?)", p["description"])
    if not m:
        return None
    x = float(m.group(1).replace(",", "."))
    x = x / 100 if x > 50 else x  # "308" cm
    return round(x * 3.281, 1) if 3 <= x <= 20 else None


def _coords(place: str | None) -> tuple[float | None, float | None]:
    # "Torslanda/Björlanda", "Gbg/Ringön"
    for part in re.split(r"[/,]", place or ""):
        lat, lon = klaravik.place_coords(part.strip())
        if lat is not None:
            return lat, lon
    return None, None


def to_ad(p: dict, url: str, prev: dict | None, today: str, family: str) -> dict:
    bid, fee, total = buyer_total(p)
    make, model = _make_model(p)
    year = re.search(r"(19[5-9]\d|20[0-4]\d)", p["specs"].get("Årsmodell") or p["specs"].get("År") or p["title"])
    lat, lon = _coords(p["place"])
    ad = dict(prev or {"first_seen": today, "price_history": []})
    ad.update({
        "ad_id": ID_OFFSET + p["id"],
        "source": "netauktion",
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
        "length_ft": _length_ft(p),
        "image": p["images"][0] if p["images"] else None,
        "image_urls": p["images"],
        "specs": {"Märke": make, "Modell": model, **{k: v for k, v in p["specs"].items() if len(v) < 80}},
        "auction": {
            "bid": bid,
            "next_bid": p["next_bid"],
            "fee": fee,
            "vat": p["vat"],
            "margin_scheme": p["margin_scheme"],  # "ej avd. moms"
            "reserve_reached": p["reserve_reached"],
            "close": p["close"],
            "ended": p["ended"],
            "viewing": store.scrub(p["viewing"], 160),
            "as_is": True,  # "utan garanti/reklamationsrätt"; buyer must inspect before bidding
        },
        "status": "active" if not p["ended"] else "ended",
        "found_by": ["netauktion", family],
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
    stats = {"netauktion_listed": 0, "netauktion_kept": 0, "netauktion_ended": 0}
    seen = set()
    for card in listing(client):
        stats["netauktion_listed"] += 1
        if not card["url"]:
            continue
        r = client.get(card["url"])
        p = parse_product(r.text) if r.status_code == 200 else {"id": 0}
        if not p["id"]:
            continue
        family = wanted(p["title"], p["description"])
        if not family:
            continue
        ad = to_ad(p, card["url"], store.load_ad(ID_OFFSET + p["id"]), today, family)
        store.save_ad(ad)
        store.save_page(ad["ad_id"], r.text)
        seen.add(ad["ad_id"])
        stats["netauktion_kept"] += 1
    # Auctions that left the listing have ended: record the final price and whether it sold.
    for ad in store.all_ads():
        if ad.get("source") == "netauktion" and ad.get("status") == "active" and ad["ad_id"] not in seen:
            r = client.get(ad["url"])
            p = parse_product(r.text) if r.status_code == 200 else None
            if p and p["id"] and p["ended"]:
                bid, _fee, total = buyer_total(p)
                ad["status"] = "sold" if p["reserve_reached"] and bid else "unsold"
                ad["sold"] = {"date": today, "last_price": total, "final_bid": bid, "reserve_reached": p["reserve_reached"]}
            elif p and p["id"]:
                continue  # still running, just dropped off the listing (moved category?); check next run
            else:
                ad["status"] = "inactive"
            store.save_ad(ad)
            stats["netauktion_ended"] += 1
    print(f"  netauktion: {stats}", file=sys.stderr)
    return stats


def cached_text(ad: dict) -> dict | None:
    page = store.load_page(ad["ad_id"])
    if page is None:
        return None
    p = parse_product(page)
    # The "Särskilda villkor" block (as is, no warranty) is in the description for the red-flag rules.
    return {"heading": ad.get("heading") or p["title"], "title": p["title"], "description": p["description"],
            "specs": ad.get("specs") or p["specs"]}
