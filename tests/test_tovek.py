"""Tovek parser tests against trimmed saved responses (tests/fixtures/tovek_*); no network.

*.rsc are server action responses (React Server Components rows), *.html item pages reduced to
the RSC row that carries the item and its cachedBids. Phone numbers and bidder names are scrubbed.
"""

import json
from pathlib import Path

import pytest

from boats import tovek

FIX = Path(__file__).parent / "fixtures"


def raw(name: str) -> bytes:
    return (FIX / name).read_bytes()


def items(name: str) -> dict[int, dict]:
    return {it["itemId"]: it for it in tovek.rsc_value(raw(name))["auctionItems"]}


@pytest.fixture
def boat():  # running, no VAT, fee 600 kr ex VAT, 6 bids (live bids response)
    bids = tovek.rsc_value(raw("tovek_bids.rsc"))["allItemBids"][0]
    return tovek.parse_item(items("tovek_items_7820.rsc")[774111], bids)


@pytest.fixture
def bella():  # ended 2021, sold at 470 550 kr, no VAT, fee 10 000 kr ex VAT
    return tovek.parse_page((FIX / "tovek_item_508515.html").read_text())


def test_auctions_response():
    res = tovek.rsc_value(raw("tovek_auctions.rsc"))
    assert [a["partId"] for a in res["auctions"]] == [7820, 7809]
    assert res["auctions"][0]["partStatus"] == "running"


def test_text_rows_are_resolved():
    # Long descriptions come as a separate "2:T<hex len>," row referenced as "$2", newlines included.
    it = next(iter(items("tovek_items_trow.rsc").values()))
    assert it["itemDescription"].startswith("<p><span")
    assert it["itemDescription"].endswith("\n<p>Rad två</p>")
    assert "Rad två" in tovek.parse_item(it)["description"]


def test_find_actions():
    js = ('let r=(0,a.createServerReference)("7fed40d39c5dc37e5a2804c72ef3b51c94cef75da6",a.callServer,void 0,'
          'a.findSourceMapURL,"getAuctions")},x=(0,i.createServerReference)("008185a86fd172c6283fa06d792fd76c84eefedac0",'
          'i.callServer,void 0,i.findSourceMapURL,"getThemeSettings")')
    assert tovek.find_actions(js) == {"getAuctions": "7fed40d39c5dc37e5a2804c72ef3b51c94cef75da6",
                                      "getThemeSettings": "008185a86fd172c6283fa06d792fd76c84eefedac0"}


def test_item_fields(boat):
    assert boat["id"] == 774111
    assert boat["title"] == "Plastbåt Sparrholms med Parsun 9,8 hk fyrtaktsmotor och båtvagn"
    assert boat["url"] == "https://tovek.se/auktion/rop/plastbat-sparrholms-med-parsun-9-8-hk-fyrtaktsmotor-och-batvagn/774111"
    assert boat["description"].startswith("Parsun utombordsmotor, 9,8 hk")
    assert boat["close"] == "2026-10-30T13:14:00+01:00"
    assert boat["place"] == "Villands Vånga"
    assert boat["viewing"] == "2026-10-29 13:00–14:00"
    assert boat["lot"] == 15 and boat["part_id"] == 7820
    assert boat["start_price"] == 5000
    assert len(boat["images"]) == 2 and boat["images"][0].startswith("https://tovek2-objectstorage.b-cdn.net/")


def test_buyer_total_at_current_bid(boat):
    # 6 700 kr high bid, no VAT on the item, 600 kr slagavgift + 25 % VAT = 750
    assert (boat["bid"], boat["bids"], boat["vat"], boat["fee"]) == (6700, 6, 0, 600)
    assert boat["total"] == 6700 + 750
    assert boat["next_bid"] is None  # Tovek's bid step isn't public; only known before the first bid


def test_buyer_total_with_vat_and_no_bids():
    # Trädgårdstraktor: start price 40 000, 25 % VAT, fee 3 600 ex VAT; no bids in this response
    tractor = items("tovek_items_7820.rsc")[774105]
    assert tovek.buyer_total(tractor) == (40000, 40000 * 125 // 100 + 3600 * 125 // 100)
    p = tovek.parse_item(tractor)
    assert p["bids"] == 0 and p["next_bid"] == 40000 and p["vat"] == 25


def test_ended_item_page(bella):
    assert bella["id"] == 508515
    assert bella["title"].startswith("Motorbåt Bella 640 DC, årsmodell 2019")
    assert bella["ended"] is True
    assert bella["close"] == "2021-06-16T09:45:00+02:00"
    assert bella["winning_bid"] == 470550
    assert bella["total"] == 470550 + 12500
    assert "Garmin Echomap" in bella["description"] and "<" not in bella["description"]
    assert bella["place"] == "Ätran"
    assert bella["viewing"] == "Enligt överenskommelse"


def test_running_item_page_uses_cached_bids():
    p = tovek.parse_page((FIX / "tovek_item_774111.html").read_text())
    assert p["id"] == 774111 and p["bids"] == 3 and p["bid"] == 6700  # fixture keeps the top 3 bids
    assert tovek.parse_page("<html></html>") == {"id": 0}


def test_viewing_falls_back_to_contact_scrubbed():
    item = {"itemId": 1, "itemTitle": "Båt", "addressShowingStart": "0000-00-00 00:00:00",
            "addressShowingSpecial": "0346-48771, Martin"}
    assert tovek.parse_item(item)["viewing"] == "[tel], Martin"


def test_is_boat():
    assert tovek.is_boat("Plastbåt Sparrholms med Parsun 9,8 hk")
    assert tovek.is_boat("Motorbåt, Nimbus 3000, -83")
    assert tovek.is_boat("Segelbåt Motorseglare Albin 82 MS")
    assert not tovek.is_boat("Båttransportvagn för vagga, hydraulisk")
    assert not tovek.is_boat("Terassvärmare Trysil Flamma 68cm")
    assert tovek.is_boat("Ryds 550 HT med Yamaha 60", families={"ryds 550"})


def test_wanted():
    fams = {"buster xl"}
    assert tovek.wanted("Motorbåt Bella 640 DC, årsmodell 2019", "", fams) == "bella 640"  # cabin variant code
    assert tovek.wanted("Plastbåt Sparrholms med Parsun 9,8 hk", "", fams) is None


def test_to_ad(bella):
    ad = tovek.to_ad(bella, None, "2026-10-27", "bella 640")
    assert ad["ad_id"] == tovek.ID_OFFSET + 508515
    assert ad["source"] == "tovek" and ad["dealer_segment"] == "Auktion"
    assert ad["make"] == "Bella" and ad["year"] == 2019 and ad["motor_size"] == 115
    assert ad["price"] == 470550 + 12500
    a = ad["auction"]
    assert (a["bid"], a["fee"], a["vat"], a["margin_scheme"], a["ended"]) == (470550, 10000, 0, True, True)
    assert ad["status"] == "ended"
    assert ad["price_history"] == [{"date": "2026-10-27", "price": 483050, "bid": 470550}]


def test_cached_text_reads_item_json(monkeypatch):
    item = items("tovek_items_7820.rsc")[774111]
    monkeypatch.setattr(tovek.store, "load_page", lambda _id: json.dumps(item))
    t = tovek.cached_text({"ad_id": 1, "heading": "h", "specs": {}})
    assert t["title"].startswith("Plastbåt Sparrholms") and t["description"].startswith("Parsun")
