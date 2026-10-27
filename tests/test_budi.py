"""Budi parser tests against trimmed saved pages (tests/fixtures/budi_*.html); no network."""

from pathlib import Path

import pytest

from boats import budi, market, store

FIX = Path(__file__).parent / "fixtures"


def page(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture
def nimbus():  # ended, margin scheme (no VAT on the bid), fixed fee, reserve not met
    return budi.parse_object(page("budi_object_141837.html"))


@pytest.fixture
def xshore():  # ended, company seller: 25 % VAT on the bid, fixed fee
    return budi.parse_object(page("budi_object_145582.html"))


@pytest.fixture
def outboard():  # running, dynamic fee (16 %, min 195 kr), reserve met
    return budi.parse_object(page("budi_object_150917.html"))


def test_listing_cards():
    cards = budi.parse_listing(page("budi_category_batar.html"))
    assert cards == [{
        "id": 150917,
        "url": "https://www.budi.se/objekt/150917/fordon/elutombordare-minn-kota-endura-c2-34",
        "title": "Elutombordare Minn Kota Endura C2 34",
        "city": "Örebro",
        "close": "2026-11-04T10:33:00.000+01:00",
        "ended": False,
        "bid": 50,
    }]


def test_object_fields(nimbus):
    assert nimbus["id"] == 141837
    assert nimbus["title"].startswith("Motorbåt Nimbus 800 Turbo")
    assert nimbus["close"] == "2026-07-22T11:09:43.117+02:00"
    assert nimbus["ended"] is True
    assert nimbus["reserve_reached"] is False
    assert nimbus["place"] == "Siljansnäs"
    assert nimbus["viewing"].startswith("Enligt ö.k.")
    assert len(nimbus["images"]) == 4
    # property list + the seller's "Fakta" block
    assert nimbus["specs"]["Längd"] == "8 m"
    assert nimbus["specs"]["Fabrikat"] == "Nimbus"
    assert nimbus["specs"]["Årsmodell"] == "1981"
    assert "Säljs i befintligt skick" in nimbus["description"]


# Expected totals are Budi's own bidCurrentTotalToPayAmount.incVat from /api/wwwapi/auctionobject/<id>/bidinfo.
def test_buyer_total_margin_scheme_fixed_fee(nimbus):
    assert nimbus["vat_type"] == "2" and nimbus["vat"] == 0
    assert budi.buyer_total(nimbus) == (50000, 4900, 56125)  # 50 000 + 4 900 * 1.25


def test_buyer_total_vat_on_bid(xshore):
    assert xshore["vat"] == 25
    assert budi.buyer_total(xshore) == (400050, 29900, 537438)  # 400 050 * 1.25 + 29 900 * 1.25


def test_buyer_total_dynamic_fee(outboard):
    assert outboard["fee"]["type"] == "2"
    assert budi.buyer_total(outboard) == (50, 195, 294)  # minimum fee
    big = dict(outboard, bid=10_000)
    assert budi.buyer_total(big) == (10_000, 1600, 12_000)  # 16 % of the bid beats the minimum


def test_wanted():
    assert budi.wanted("Motorbåt Ryds 568 DC", "") == "ryds 568"  # cabin variant code
    assert budi.wanted("Flipper 630 HT -2004", "") == "flipper 630"  # on the cabin list
    assert budi.wanted("Båtpaket med trailer", "Fakta\nFabrikat: Flipper\nModell: 630 HT\n") == "flipper 630"
    assert budi.wanted("Elutombordare Minn Kota Endura C2 34", "") is None
    assert budi.wanted("Båt X-Shore 1 Open -2026 | Elektrisk", "") is None


def test_to_ad(nimbus):
    ad = budi.to_ad(nimbus, nimbus["url"], None, "2026-07-20", "nimbus 800")
    assert ad["ad_id"] == budi.ID_OFFSET + 141837
    assert ad["source"] == "budi" and ad["dealer_segment"] == "Auktion"
    assert ad["price"] == 56125
    assert ad["make"] == "Nimbus" and ad["year"] == 1981
    assert ad["length_ft"] == 26.2
    assert ad["auction"] == {
        "bid": 50000, "next_bid": 50500, "fee": 4900, "vat": 0.0, "margin_scheme": True, "reserve_reached": False,
        "close": "2026-07-22T11:09:43.117+02:00", "ended": True, "viewing": "Enligt ö.k. med [e-post]", "as_is": True,
    }
    assert ad["price_history"] == [{"date": "2026-07-20", "price": 56125, "bid": 50000}]


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text, self.status_code = text, status_code


class FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None):
        self.calls.append(url)
        return FakeResponse(self.pages[url]) if url in self.pages else FakeResponse("", 404)


def test_update_settles_ended(monkeypatch, nimbus):
    db = {}
    monkeypatch.setattr(market, "learn_from_store", lambda: None)
    monkeypatch.setattr(store, "load_ad", lambda aid: db.get(aid))
    monkeypatch.setattr(store, "save_ad", lambda ad: db.__setitem__(ad["ad_id"], dict(ad)))
    monkeypatch.setattr(store, "save_page", lambda aid, html: None)
    monkeypatch.setattr(store, "all_ads", lambda: list(db.values()))
    # The Nimbus was seen running earlier; now it's off the listing and its page says ended, reserve not met.
    aid = budi.ID_OFFSET + 141837
    db[aid] = {"ad_id": aid, "source": "budi", "status": "active", "url": nimbus["url"], "price": 50000}
    client = FakeClient({budi.LISTING: page("budi_category_batar.html"),
                         "https://www.budi.se/objekt/150917/fordon/elutombordare-minn-kota-endura-c2-34": page("budi_object_150917.html"),
                         nimbus["url"]: page("budi_object_141837.html")})
    stats = budi.update(client, {})
    assert stats == {"budi_listed": 1, "budi_kept": 0, "budi_ended": 1}  # the outboard isn't a cabin boat
    assert db[aid]["status"] == "unsold"
    assert db[aid]["sold"] == {"date": db[aid]["sold"]["date"], "last_price": 56125, "final_bid": 50000, "reserve_reached": False}
