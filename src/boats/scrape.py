"""Fetch Blocket boat search results (JSON API) and ad pages.

The search page at /mobility/search/boat is backed by
/mobility/search/api/search/SEARCH_ID_BOAT_USED, which takes the same query
string and returns 50 docs per page, capped at 50 pages. Result sets over the
cap are split into price bands.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import random
import re
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx

from . import store
from .config import LOCAL_RANGES, in_range, local_ranges
from .parse import parse_item, text_sha

API = "https://www.blocket.se/mobility/search/api/search/{key}"
ITEM = "https://www.blocket.se/mobility/item/{id}"
SEARCH_KEYS = {"boat": "SEARCH_ID_BOAT_USED"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
PAGE_CAP = 50
PRICE_MAX = 1_000_000
# Auction sources with their own modules; each has update(client, cfg) and cached_text(ad).
AUCTION_SOURCES = ("klaravik", "kronofogden", "tovek", "budi", "netauktion")
# A company ad is kept only when the ad says it is an auction ("auktion", "nätauktion", "auktioneras").
AUKTION = re.compile(r"auktion", re.I)
DEALER_CHECKED = "dealer_checked.json"


class Client:
    def __init__(self, delay_s: float):
        self.delay_s = delay_s
        self._last = 0.0
        self.http = httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "sv-SE,sv;q=0.9"},
            timeout=30,
            follow_redirects=True,
        )

    def get(self, url: str, params: list[tuple[str, str]] | None = None) -> httpx.Response:
        for attempt in range(5):
            wait = self._last + self.delay_s + random.uniform(0, 0.4) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.http.get(url, params=params)
            except httpx.TransportError as e:
                print(f"  transport error {e!r}, retrying", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                print(f"  HTTP {r.status_code}, backing off", file=sys.stderr)
                time.sleep(15 * (attempt + 1))
                continue
            return r
        r.raise_for_status()
        return r


def _search_target(search_url: str) -> tuple[str, list[tuple[str, str]]]:
    u = urlparse(search_url)
    vertical = u.path.rstrip("/").rsplit("/", 1)[-1]
    key = SEARCH_KEYS.get(vertical, "SEARCH_ID_BOAT_USED")
    local = {name for pair in LOCAL_RANGES.values() for name in pair}
    params = [(k, v) for k, v in parse_qsl(u.query) if k != "page" and k not in local]
    return API.format(key=key), params


def _with_price(params, lo: int | None, hi: int | None):
    out = [(k, v) for k, v in params if k not in ("price_from", "price_to")]
    if lo is not None:
        out.append(("price_from", str(lo)))
    if hi is not None:
        out.append(("price_to", str(hi)))
    return out


def search(client: Client, search_url: str) -> list[dict]:
    """All docs matching the search, splitting into price bands past the page cap."""
    api, params = _search_target(search_url)
    q = dict(params)
    lo = int(q["price_from"]) if "price_from" in q else 0
    hi = int(q["price_to"]) if "price_to" in q else None

    docs: dict[str, dict] = {}
    bands = [(lo, hi)]
    while bands:
        b_lo, b_hi = bands.pop()
        band_params = _with_price(params, b_lo, b_hi)
        first = client.get(api, band_params + [("page", "1")]).json()
        meta = first["metadata"]
        total = meta["result_size"]["match_count"]
        per_page = max(len(first["docs"]), 1)
        if total > PAGE_CAP * per_page:
            top = b_hi if b_hi is not None else PRICE_MAX
            if b_hi is None:
                bands.append((PRICE_MAX + 1, None))
            if top - b_lo < 1000:
                print(f"  band {b_lo}-{top} still over cap ({total}); taking first {PAGE_CAP} pages", file=sys.stderr)
            else:
                mid = (b_lo + top) // 2
                bands += [(b_lo, mid), (mid + 1, top)]
                continue
        last = meta["paging"]["last"]
        print(f"  band {b_lo}-{b_hi if b_hi is not None else '∞'}: {total} ads, {last} pages", file=sys.stderr)
        for d in first["docs"]:
            docs[d["id"]] = d
        for page in range(2, last + 1):
            for d in client.get(api, band_params + [("page", str(page))]).json()["docs"]:
                docs[d["id"]] = d
    return list(docs.values())


def search_models(client: Client, cfg: dict):
    """Free-text search per listed model, in every boat type, kept only on an exact model-family match.

    Catches ads the seller filed under the wrong type or length ("Annat", "8 ft"). Yields (model, doc).
    """
    from .market import learn_from_store, model_keys  # market imports this module

    from . import models_list

    learn_from_store()  # number-less names ("Ryds Camping") only parse once the vocabulary is loaded

    extra = cfg["search"].get("model_filters", "")
    listed = cfg["search"].get("models", []) + models_list.search_terms()
    for model in dict.fromkeys(listed):  # config list + cabin_models.csv, no duplicates
        make = model.split()[0]
        target, target_variant = model_keys(make, None, model)
        if not target:
            print(f"  model list: can't parse {model!r}", file=sys.stderr)
            continue
        family_query = " ".join(model.split()[:2])  # search broadly, match the variant locally
        url = f"https://www.blocket.se/mobility/search/boat?{urlencode({'q': family_query})}&{extra}"
        hits = 0
        for d in search(client, url):
            fam, var = model_keys(d.get("make"), None, d.get("heading"))
            # "Sandström 560 MC" requires the MC variant; "Ryds 550" accepts any 550.
            match = fam == target and (not target_variant or var == target_variant)
            if match and all(in_range(d.get(f), rng) for f, rng in local_ranges(url).items()):
                hits += 1
                yield model, d
        print(f"  model {model}: {hits} ads", file=sys.stderr)


def _without_dealer_filter(url: str) -> str:
    """The search URL with dealer_segment dropped: company ads come back too, checked by dealer_auktion."""
    u = urlparse(url)
    return u._replace(query=urlencode([(k, v) for k, v in parse_qsl(u.query) if k != "dealer_segment"])).geturl()


def dealer_auktion(client: Client, d: dict, checked: dict) -> bool:
    """A company ad counts only when "auktion" is in the ad: company name, heading, description or specs.

    The page is fetched once per ad version; `checked` (ad id -> [changed ts, verdict]) is kept in cache/.
    """
    if AUKTION.search(f"{d.get('organisation_name') or ''} {d.get('heading') or ''}"):
        return True
    key = str(d["id"])
    if key in checked and checked[key][0] == d.get("timestamp"):
        return checked[key][1]
    r = client.get(d.get("canonical_url") or ITEM.format(id=d["id"]))
    if r.status_code != 200:
        return False
    parsed = parse_item(r.text)
    hit = bool(AUKTION.search(f"{parsed['description']} {' '.join(map(str, parsed['specs'].values()))}"))
    checked[key] = [d.get("timestamp"), hit]
    if hit:
        store.save_page(int(d["id"]), r.text)
    return hit


def page_status(client: Client, url: str) -> str:
    """What the ad page says now: 'sold' (SÅLD), 'inactive' (Inaktiv / removed) or 'not_in_search'."""
    r = client.get(url)
    if r.status_code in (404, 410):
        return "inactive"
    text = re.sub(r"<[^>]+>", " ", r.text)
    if re.search(r"\bSÅLD\b", text):
        return "sold"
    if re.search(r"\bInaktiv\b|Sidan hittades inte", text):
        return "inactive"
    return "not_in_search"


def _days(first_seen: str | None, today: str) -> int | None:
    if not first_seen:
        return None
    return (dt.date.fromisoformat(today) - dt.date.fromisoformat(first_seen[:10])).days


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _search_fields(d: dict) -> dict:
    coords = d.get("coordinates") or {}
    return {
        "ad_id": int(d["id"]),
        "url": d.get("canonical_url") or ITEM.format(id=d["id"]),
        "heading": store.scrub(d.get("heading"), 200),
        "make": d.get("make"),
        "boat_class": d.get("boat_class"),
        "location": d.get("location"),
        # ~1 km is plenty for a distance filter; no need for more precision in a public repo.
        "lat": round(coords["lat"], 2) if coords.get("lat") is not None else None,
        "lon": round(coords["lon"], 2) if coords.get("lon") is not None else None,
        "dealer_segment": d.get("dealer_segment"),
        "organisation_name": d.get("organisation_name"),
        "price": (d.get("price") or {}).get("amount"),
        "year": d.get("year"),
        "motor_type": d.get("motor_type"),
        "motor_fuel": d.get("motor_fuel"),
        "motor_size": d.get("motor_size"),
        "length_ft": d.get("length"),
        "width_cm": d.get("width"),
        "max_speed": d.get("max_speed"),
        "image": (d.get("image") or {}).get("url"),
        "image_urls": d.get("image_urls") or [],
        "changed_ts": d.get("timestamp"),
    }


def update(cfg: dict, fetch_pages: bool = True, limit: int | None = None) -> dict:
    scfg = cfg["search"]
    client = Client(scfg["delay_s"])
    run_start = _now()
    today = run_start[:10]

    print("searching…", file=sys.stderr)
    dealers = scfg.get("dealer_auktion", True)
    url = _without_dealer_filter(scfg["url"]) if dealers else scfg["url"]
    if dealers:
        cfg = {**cfg, "search": {**scfg, "model_filters": _without_dealer_filter("?" + scfg.get("model_filters", ""))[1:]}}
    checked_path = store.CACHE_DIR / DEALER_CHECKED
    checked = json.loads(checked_path.read_text()) if checked_path.exists() else {}

    def keep(d: dict) -> bool:
        return d.get("dealer_segment", "Privat") == "Privat" or (dealers and dealer_auktion(client, d, checked))

    found: dict[str, dict] = {}
    for d in search(client, url):
        if all(in_range(d.get(f), rng) for f, rng in local_ranges(url).items()) and keep(d):
            found[d["id"]] = {**d, "_found_by": ["kategori"]}
    for model, d in search_models(client, cfg):
        if d["id"] not in found and not keep(d):
            continue
        entry = found.setdefault(d["id"], {**d, "_found_by": []})
        entry["_found_by"].append(model)
    checked_path.parent.mkdir(parents=True, exist_ok=True)
    checked_path.write_text(json.dumps(checked))
    docs = list(found.values())
    stats = {"seen": len(docs), "new": 0, "price_changes": 0, "gone": 0, "pages": 0,
             "found_by_model_only": sum(1 for d in docs if "kategori" not in d["_found_by"])}

    seen_ids = set()
    for d in docs:
        fields = _search_fields(d)
        fields["found_by"] = d["_found_by"]
        seen_ids.add(fields["ad_id"])
        ad = store.load_ad(fields["ad_id"]) or {"first_seen": run_start, "price_history": []}
        if "ad_id" not in ad:
            stats["new"] += 1
        ad.update(fields)
        ad["last_seen"] = run_start
        ad["status"] = "active"
        hist = ad["price_history"]
        if fields["price"] is not None and (not hist or hist[-1]["price"] != fields["price"]):
            if hist:
                stats["price_changes"] += 1
            hist.append({"date": today, "price": fields["price"]})
        store.save_ad(ad)

    for ad in store.all_ads():
        if ad.get("status") == "active" and ad["ad_id"] not in seen_ids and ad.get("source", "blocket") == "blocket":
            # Sold ("SÅLD"), deactivated ("Inaktiv"), or still for sale but outside the search now.
            ad["status"] = page_status(client, ad["url"])
            ad["not_in_search_since"] = today
            if ad["status"] == "sold":
                ad["sold"] = {"date": today, "last_price": ad.get("price"), "days_seen": _days(ad.get("first_seen"), today)}
            store.save_ad(ad)
            stats["gone"] += 1
            stats[ad["status"]] = stats.get(ad["status"], 0) + 1

    if fetch_pages:
        stats["pages"] = fetch_ad_pages(client, cfg, seen_ids, limit)
    for source in AUCTION_SOURCES:
        if scfg.get(source, True):
            try:  # one auction site changing its markup must not stop the daily run
                stats.update(importlib.import_module(f".{source}", __package__).update(client, cfg))
            except Exception as e:
                print(f"  {source}: FAILED {e!r}", file=sys.stderr)
                stats[f"{source}_error"] = repr(e)[:200]
    return stats


def _needs_page(ad: dict, refetch_days: int) -> bool:
    page = ad.get("page")
    if not page or store.load_page(ad["ad_id"]) is None:
        return True
    if (ad.get("changed_ts") or 0) > page.get("fetched_ts", 0):
        return True
    age = time.time() - page.get("fetched_ts", 0) / 1000
    return age > refetch_days * 86400


def fetch_ad_pages(client: Client, cfg: dict, ids, limit: int | None = None) -> int:
    todo = [a for a in (store.load_ad(i) for i in sorted(ids)) if a and _needs_page(a, cfg["search"]["refetch_after_days"])]
    if limit:
        todo = todo[:limit]
    print(f"fetching {len(todo)} ad pages…", file=sys.stderr)
    done = 0
    for n, ad in enumerate(todo, 1):
        r = client.get(ad["url"])
        if r.status_code in (404, 410):
            store.patch_ad(ad["ad_id"], status="gone")
            continue
        if r.status_code != 200:
            print(f"  {ad['ad_id']}: HTTP {r.status_code}", file=sys.stderr)
            continue
        store.save_page(ad["ad_id"], r.text)
        parsed = parse_item(r.text)
        store.patch_ad(ad["ad_id"], specs=parsed["specs"], page={
            "fetched_ts": int(time.time() * 1000),
            "text_sha": text_sha(ad["heading"] or "", parsed["description"], parsed["specs"]),
            "description_chars": len(parsed["description"]),
        })
        done += 1
        if n % 25 == 0:
            print(f"  {n}/{len(todo)}", file=sys.stderr)
    return done


def cached_text(ad: dict) -> dict | None:
    """Heading, description and specs from the locally cached original page."""
    if ad.get("source") in AUCTION_SOURCES:
        return importlib.import_module(f".{ad['source']}", __package__).cached_text(ad)
    html = store.load_page(ad["ad_id"])
    if html is None:
        return None
    parsed = parse_item(html)
    return {"heading": ad.get("heading") or "", **parsed}
