"""Toveks auktioner (tovek.se): boats in running online auctions.

Tovek sells bankruptcy estates, liquidations and company surplus. There is no item category:
an auction ("part") is one seller's lot list, so we walk every running auction and keep the
items whose title says boat ("Motorbåt", "Hyttbåt", "Segelbåt" ...) or names a model on the
cabin-boat list; of those, kept when the model is on the list or its variant code says cabin
(the same rule as Kronofogden). Few boats at a time, often none.

The site is Next.js (app router) and the lists are not in the HTML: the browser fetches them
through Next server actions, i.e. POST <any page> with a "Next-Action: <id>" header and the
JSON-encoded argument list as body, answered as React Server Components rows ("1:{json}").
We use three: getAuctions (running auctions), getAuctionItems (every item of one auction,
with title, description, start price, VAT rate, fee, end time and viewing address) and
getRecentItemBidsByItemIds (live bids and the end time, which late bids extend). The action ids
are content hashes that change when Tovek deploys; the known ones are tried first and, if
Next answers "not found", re-read from the page's JS chunks. The item page itself
(/auktion/rop/<slug>/<itemId>, any slug redirects) is server-rendered with the same item object
plus "cachedBids" in the RSC payload, so ended items are settled with a plain GET.

What the buyer pays (https://tovek.se/om-oss/faq):
- "Hur mycket är slagavgiften? Slagavgiften står specificerad på varje objekt. Det är en fast
  avgift som baseras på startpriset. På slagavgiften tillkommer det alltid 25% moms." The item
  shows it as "Slagavgift: 600 kr exkl. moms" (itemFeeValue);
- "Tillkommer det moms på objekten? Det framgår inne på varje objekt": the item's VAT rate
  (itemVatValue, 25 or 0) is added to the bid; 0 means no VAT (private/estate goods, margin
  scheme);
- bids can't be below the start price ("Utgångspris", itemMinBid) and there's no hidden reserve:
  the highest bid at the end wins;
- per invoice (not per item) an "exp. avgift på 30 kr exkl. moms" is added; not included here.
So price = bid * (1 + VAT) + fee * 1.25, at the current high bid (the start price when there is
no bid yet). Sold as is, no right of withdrawal for estate sales ("Vid konkursutförsäljning
gäller inte konsumentköplagen").

ad_id = 3_000_000_000 + Tovek's item id (/auktion/rop/.../774111 -> 774111).
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import re
import sys
import time
from zoneinfo import ZoneInfo

from . import klaravik, market, models_list, rules, store
from .parse import text_sha

BASE = "https://tovek.se"
ACTION_PAGE = BASE + "/auktioner/pagaende-auktioner"
ID_OFFSET = 3_000_000_000
FEE_VAT = 1.25  # "På slagavgiften tillkommer det alltid 25% moms"
TZ = ZoneInfo("Europe/Stockholm")  # item times are local, without offset
# Server action ids as of 2026-10; refreshed from the JS chunks when Tovek redeploys.
ACTIONS = {
    "getAuctions": "7fed40d39c5dc37e5a2804c72ef3b51c94cef75da6",
    "getAuctionItems": "7fb889a52d5d76fc29f37286bf9d2814d9e629d991",
    "getRecentItemBidsByItemIds": "7f16e5d8939a9ed767b293d5c6fefad2ad8d26d0fe",
}
GENERIC = r"(?:Motorbåt|Båt|Plastbåt|Hyttbåt|Kabinbåt|Daycruiser|Segelbåt|Motorseglare|Snipa)"  # not makes
# A word ending in "båt"/"båtar" (not "båtvagn", "båtmotor", "båtstöttor") or a boat type.
PHONE = re.compile(r"(?:\+46|\b0)\d{1,3}[- ]?\d{2,3}[ ]?\d{2}(?:[ ]?\d{2,3})?\b")
BOAT = re.compile(r"\w*båt(?:ar|en)?\b|\bdaycruiser\b|\bmotorseglare\b|\bsnipa\b|\bkajutbåt\b|\bcabin\s?cruiser\b", re.I)


def ad_id(item_id: int) -> int:
    return ID_OFFSET + int(item_id)


def _slug(s: str) -> str:
    s = s.lower().translate(str.maketrans("åäöé", "aaoe"))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "rop"


def item_url(item: dict) -> str:
    return f"{BASE}/auktion/rop/{_slug(item.get('itemTitle') or '')}/{item['itemId']}"


def _clean(s: str | None) -> str:
    s = re.sub(r"<br\s*/?>|</p>", "\n", s or "")
    s = htmllib.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\r", "")
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\s*\n\s*", "\n", s).strip()


def _time(s: str | None) -> dt.datetime | None:
    """'2026-10-30 13:14:00' (Stockholm time) -> aware datetime; '0000-00-00 ...' -> None."""
    if not s or s.startswith("0000"):
        return None
    try:
        return dt.datetime.fromisoformat(s).replace(tzinfo=TZ)
    except ValueError:
        return None


# --- server actions ---


def _rsc_rows(data: bytes) -> dict[str, object]:
    """React Server Components rows: '<id>:<json>\\n', or '<id>:T<hex byte length>,<text>' for long
    strings (an item description), which may contain newlines and are referenced as "$<id>"."""
    rows: dict[str, object] = {}
    i = 0
    while i < len(data):
        m = re.compile(rb"([0-9a-f]+):(T([0-9a-f]+),)?").match(data, i)
        if not m:
            j = data.find(b"\n", i)
            i = len(data) if j < 0 else j + 1
            continue
        if m.group(2):
            n = int(m.group(3), 16)
            rows[m.group(1).decode()] = data[m.end(): m.end() + n].decode()
            i = m.end() + n
        else:
            j = data.find(b"\n", m.end())
            j = len(data) if j < 0 else j
            rows[m.group(1).decode()] = json.loads(data[m.end(): j])
            i = j + 1
    return rows


def _resolve(v, rows: dict):
    if isinstance(v, dict):
        return {k: _resolve(x, rows) for k, x in v.items()}
    if isinstance(v, list):
        return [_resolve(x, rows) for x in v]
    if isinstance(v, str) and v.startswith("$"):
        if v.startswith("$$"):
            return v[1:]
        ref = re.fullmatch(r"\$@?([0-9a-f]+)", v)
        if ref and ref.group(1) in rows:
            return _resolve(rows[ref.group(1)], rows)
    return v


def rsc_value(data: bytes | str):
    """The value of a server action response: row 0 is {"a":"$@1",...}, row 1 the JSON result."""
    rows = _rsc_rows(data.encode() if isinstance(data, str) else data)
    head = rows.get("0")
    ref = str(head.get("a", "$@1")) if isinstance(head, dict) else "$@1"
    if ref[2:] not in rows:
        raise ValueError("no action result in response")
    return _resolve(rows[ref[2:]], rows)


def find_actions(js: str) -> dict[str, str]:
    """Action name -> id from a JS chunk: createServerReference)("7fed...",...,"getAuctions")."""
    return {name: aid for aid, name in re.findall(r'createServerReference\)\("([0-9a-f]{40,})",[^)]*?"(\w+)"\)', js)}


def _refresh_actions(client) -> None:
    page = client.get(ACTION_PAGE).text
    chunks = list(dict.fromkeys(re.findall(r'/_next/static/chunks/[^"\'\s?]+\.js', page)))
    chunks.sort(key=lambda c: "app/" not in c and not re.search(r"/\d+-", c))  # page + shared chunks first
    found: dict[str, str] = {}
    for c in chunks:
        found.update({k: v for k, v in find_actions(client.get(BASE + c).text).items() if k in ACTIONS})
        if len(found) == len(ACTIONS):
            break
    ACTIONS.update(found)
    client._tovek_refreshed = True  # once per run, so a real outage doesn't re-scan for every call


def _post(client, action_id: str, args: list):
    # Client.get only does GET; a server action is a POST, so pace it with the client's own delay.
    wait = getattr(client, "_last", 0.0) + getattr(client, "delay_s", 1.2) - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    client._last = time.monotonic()
    return client.http.post(ACTION_PAGE, content=json.dumps(args).encode(), headers={
        "Next-Action": action_id, "Accept": "text/x-component", "Content-Type": "text/plain;charset=UTF-8"})


def call(client, name: str, *args):
    """Call a Tovek server action by name; re-read the action ids once if the deploy changed them."""
    for attempt in range(2):
        r = _post(client, ACTIONS[name], list(args))
        if r.status_code == 200 and "text/x-component" in r.headers.get("content-type", ""):
            try:
                return rsc_value(r.content)
            except (ValueError, json.JSONDecodeError):
                pass
        if attempt == 0 and not getattr(client, "_tovek_refreshed", False):
            print(f"  tovek: action {name} failed (HTTP {r.status_code}), re-reading action ids", file=sys.stderr)
            _refresh_actions(client)
            continue
        break
    raise RuntimeError(f"tovek action {name} failed: HTTP {r.status_code}")


# --- listing and parsing ---


def is_boat(title: str, families: set[str] | None = None) -> bool:
    if BOAT.search(title or ""):
        return True
    return bool(families) and _family(title) in families


def listing(client) -> list[dict]:
    """Every boat-like item in a running auction, as Tovek's item JSON plus "part" and live "bids"."""
    parts, offset = [], 0
    while True:
        res = call(client, "getAuctions", {"offset": offset, "limit": 100, "partStatus": ["running"],
                                           "sort": {"partAuctionStart": "asc"}})
        parts += res.get("auctions") or []
        offset += 100
        if offset >= int(res.get("totalHits") or 0) or not res.get("auctions"):
            break
    families = models_list.families()
    items = []
    for part in parts:
        res = call(client, "getAuctionItems", part["partId"], {"limit": 0})
        for it in res.get("auctionItems") or []:
            if is_boat(it.get("itemTitle") or "", families):
                it["part"] = {k: part.get(k) for k in ("partId", "partTitle", "partStatus", "auctionTitle", "partLocation")}
                items.append(it)
    if items:
        live = call(client, "getRecentItemBidsByItemIds", [it["itemId"] for it in items])
        by_id = {b["itemId"]: b for b in live.get("allItemBids") or []}
        for it in items:
            it["bids"] = by_id.get(it["itemId"])
    return items


def parse_page(page: str) -> dict:
    """The item object and its "cachedBids" from the RSC payload of a server-rendered item page."""
    flight = "".join(json.loads(f'"{s}"') for s in re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', page))
    dec = json.JSONDecoder()
    out = {}
    for key in ("item", "cachedBids"):
        i = flight.find(f'"{key}":{{')
        if i >= 0:
            out[key] = dec.raw_decode(flight, i + len(key) + 3)[0]
    if not out.get("item"):
        return {"id": 0}
    return parse_item(out["item"], out.get("cachedBids"))


def _bids(item: dict, bids: dict | None) -> list[int]:
    return sorted((int(b.get("historyBidValue") or 0) for b in (bids or {}).get("bids") or []), reverse=True)


def buyer_total(item: dict, bids: dict | None = None) -> tuple[int, int]:
    """(bid, what you'd pay at that bid incl. VAT and fee). The bid is the start price until someone bids."""
    values = _bids(item, bids)
    bid = (values[0] if values else 0) or int(item.get("itemWinningBidValue") or 0) or int(item.get("itemMinBid") or 0)
    vat = float(item.get("itemVatValue") or 0)
    fee = float(item.get("itemFeeValue") or 0)
    return bid, int(round(bid * (1 + vat / 100) + fee * FEE_VAT))


def parse_item(item: dict, bids: dict | None = None) -> dict:
    """Title, description, auction state and place from Tovek's item JSON (+ its bids object)."""
    values = _bids(item, bids)
    winning = int(item.get("itemWinningBidValue") or 0)
    bid, total = buyer_total(item, bids)
    close = _time((bids or {}).get("itemEndTime") or item.get("itemEndTime"))
    part = item.get("part") or {}
    ended = (item.get("itemStatus") not in (None, "active") or item.get("itemPartAuctionStatus") == "ended"
             or part.get("partStatus") == "ended" or bool(close and close < dt.datetime.now(TZ)))
    # "Bergmossevägen 15, 29198 Villands Vånga" -> "Villands Vånga"; addressTitle is the same town, mostly
    addr = item.get("addressAddress") or item.get("itemShowingAddress") or ""
    town = re.search(r"\d{3}\s?\d{2}\s+([^,]+)$", addr.strip())
    place = (town.group(1) if town else None) or (item.get("addressTitle") or "").strip() or None
    start, end = _time(item.get("addressShowingStart")), _time(item.get("addressShowingEnd"))
    info = item.get("itemShowingInfo")  # "29 oktober kl. 13.00", or {"text": "Enligt överenskommelse", "useLink": true}
    info = info.get("text") if isinstance(info, dict) else info
    viewing = (f"{start:%Y-%m-%d %H:%M}–{end:%H:%M}" if start and end else None) or (info or "").strip() \
        or PHONE.sub("[tel]", _clean(item.get("addressShowingSpecial"))) or None  # "0346-48771, Martin"
    images = [m["url"] for m in sorted(item.get("media") or [], key=lambda m: m.get("sort", 0)) if m.get("type") == "image"]
    return {
        "id": int(item.get("itemId") or 0),
        "url": item_url(item) if item.get("itemId") else None,
        "title": re.sub(r"\s+", " ", item.get("itemTitle") or "").strip(),
        "description": _clean(item.get("itemDescription")),
        "lot": item.get("itemSortNo"),
        "part_id": item.get("itemPartId"),
        "seller": part.get("auctionTitle"),
        "bid": bid,
        "total": total,
        "bids": len(values),
        "start_price": int(item.get("itemMinBid") or 0),
        "next_bid": int(item.get("itemMinBid") or 0) if not values and not winning else None,  # step table isn't public
        "winning_bid": winning or (values[0] if values and ended else 0),
        "vat": float(item.get("itemVatValue") or 0),
        "fee": float(item.get("itemFeeValue") or 0),
        "close": close.isoformat() if close else None,
        "ended": ended,
        "place": place,
        "viewing": viewing,
        "images": images or list(item.get("images") or []),
    }


def _family(title: str) -> str | None:
    """Model family from a title: "Motorbåt, Bella 640 DC, årsmodell 2019" -> "bella 640"."""
    cand = re.sub(rf"^{GENERIC}[\s,]+", "", title or "", flags=re.I).lstrip(", ")
    make = cand.split()[0].strip(",") if cand else None
    family, _ = market.model_keys(make, None, cand) if make else (None, None)
    return family


def wanted(title: str, description: str, families: set[str] | None = None) -> str | None:
    """The model family when it's on the cabin list, or when its variant code says cabin."""
    family = _family(title)
    families = models_list.families() if families is None else families
    if family and (family in families or rules.HULL_CODE_CABIN.search(title)):
        return family
    return None


def _make(title: str) -> str | None:
    cand = re.sub(rf"^{GENERIC}[\s,]+", "", title or "", flags=re.I).lstrip(", ")
    return cand.split()[0].strip(",") if cand else None


def to_ad(p: dict, prev: dict | None, today: str, family: str) -> dict:
    text = f"{p['title']}\n{p['description']}"
    year = re.search(r"(?:års?modell|årsm\.?|år|års)\s*:?\s*(19[5-9]\d|20[0-4]\d)", text, re.I) \
        or re.search(r"\b(19[6-9]\d|20[0-4]\d)\b", p["title"])
    length = re.search(r"[Ll]ängd\s*:?\s*(?:ca\.?\s*|cirka\s*)?(\d+(?:[.,]\d+)?)\s*(m|cm)\b", text)
    length_m = float(length.group(1).replace(",", ".")) / (100 if length and length.group(2) == "cm" else 1) if length else None
    lat, lon = klaravik.place_coords(p["place"])
    make = _make(p["title"])
    ad = dict(prev or {"first_seen": today, "price_history": []})
    ad.update({
        "ad_id": ad_id(p["id"]),
        "source": "tovek",
        "url": p["url"],
        "heading": store.scrub(p["title"], 200),
        "make": make,
        "boat_class": None,
        "location": p["place"],
        "lat": lat,
        "lon": lon,
        "dealer_segment": "Auktion",
        "price": p["total"],
        "year": int(year.group(1)) if year else None,
        "motor_type": None,
        "motor_size": rules.extract_hp(p["title"], p["description"], {})[0],
        "length_ft": round(length_m * 3.281, 1) if length_m and 3 <= length_m <= 20 else None,
        "image": p["images"][0] if p["images"] else None,
        "image_urls": p["images"],
        "specs": {"Märke": make, "Modell": " ".join(re.sub(rf"^{GENERIC}[\s,]+", "", p["title"], flags=re.I).split()[1:3]).strip(",") or None,
                  "Rop": p["lot"], "Säljare": store.scrub(p["seller"], 80)},
        "auction": {
            "bid": p["bid"],
            "next_bid": p["next_bid"],
            "bids": p["bids"],
            "fee": p["fee"],
            "vat": p["vat"],
            "margin_scheme": not p["vat"],
            "reserve_reached": p["bids"] > 0 or bool(p["winning_bid"]),  # no hidden reserve: any bid >= start price wins
            "close": p["close"],
            "ended": p["ended"],
            "viewing": p["viewing"],  # not store.scrub: its phone pattern eats "2026-10-29 13:00"
            "as_is": True,
        },
        "status": "active" if not p["ended"] else "ended",
        "found_by": ["tovek", family],
        "last_seen": today,
        "page": {"fetched_ts": int(time.time() * 1000), "text_sha": text_sha(p["title"], p["description"], {}),
                 "description_chars": len(p["description"])},
    })
    hist = ad["price_history"]
    if not hist or hist[-1]["price"] != p["total"]:
        hist.append({"date": today, "price": p["total"], "bid": p["bid"]})
    return ad


def update(client, cfg: dict) -> dict:
    """Fetch boats in running auctions, keep those on the cabin-boat list, settle ended ones."""
    today = dt.date.today().isoformat()
    market.learn_from_store()
    families = models_list.families()
    stats = {"tovek_listed": 0, "tovek_kept": 0, "tovek_ended": 0}
    seen = set()
    for raw in listing(client):
        stats["tovek_listed"] += 1
        p = parse_item(raw, raw.get("bids"))
        family = wanted(p["title"], p["description"], families) if p["id"] else None
        if not family:
            continue
        ad = to_ad(p, store.load_ad(ad_id(p["id"])), today, family)
        store.save_ad(ad)
        # The item JSON is the "page": the server-rendered item page carries the same object.
        store.save_page(ad["ad_id"], json.dumps(raw, ensure_ascii=False))
        seen.add(ad["ad_id"])
        stats["tovek_kept"] += 1
    # Items that left the running auctions have ended: the item page has the winning bid.
    for ad in store.all_ads():
        if ad.get("source") == "tovek" and ad.get("status") in ("active", "ended") and ad["ad_id"] not in seen:
            r = client.get(ad["url"])
            p = parse_page(r.text) if r.status_code == 200 else {"id": 0}
            if p["id"] and p["ended"]:
                ad["status"] = "sold" if p["winning_bid"] else "unsold"
                ad["sold"] = {"date": today, "last_price": p["total"] if p["winning_bid"] else ad.get("price"),
                              "final_bid": p["winning_bid"], "reserve_reached": bool(p["winning_bid"])}
                ad["auction"] = {**(ad.get("auction") or {}), "ended": True, "close": p["close"] or (ad.get("auction") or {}).get("close")}
            elif p["id"]:
                continue  # still running but not in the listing (e.g. its auction is paused); look again next run
            else:
                ad["status"] = "inactive"
            store.save_ad(ad)
            stats["tovek_ended"] += 1
    print(f"  tovek: {stats}", file=sys.stderr)
    return stats


def cached_text(ad: dict) -> dict | None:
    page = store.load_page(ad["ad_id"])
    if page is None:
        return None
    p = parse_item(json.loads(page)) if page.lstrip().startswith("{") else parse_page(page)
    # "Säljes i befintligt skick", "repobjekt", "legat under vatten" feed the red-flag rules.
    return {"heading": ad.get("heading") or p.get("title", ""), "title": p.get("title"),
            "description": p.get("description", ""), "specs": ad.get("specs") or {}}
