"""Netauktion parser tests against trimmed saved pages (tests/fixtures/netauktion_*.html); no network."""

import datetime as dt
from pathlib import Path

import pytest

from boats import market, netauktion, store

FIX = Path(__file__).parent / "fixtures"
NOW = dt.datetime(2026, 10, 27, 6, 0, tzinfo=netauktion.TZ)  # when the fixtures were saved


def page(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture
def viking():  # running, bids ex VAT (25 % added), reserve not met
    return netauktion.parse_product(page("netauktion_product_255373.html"), NOW)


@pytest.fixture
def trailer():  # running, bids ex VAT, reserve met
    return netauktion.parse_product(page("netauktion_product_255374.html"), NOW)


@pytest.fixture
def car():  # running, "ej avd. moms": nothing added to the bid
    return netauktion.parse_product(page("netauktion_product_254886.html"), NOW)


@pytest.fixture
def ended():  # closed 2026-10-26, reserve not met
    return netauktion.parse_product(page("netauktion_product_247241.html"), NOW)


def test_listing_cards():
    cards = netauktion.parse_listing(page("netauktion_category_bat.html"))
    assert [c["id"] for c in cards] == [255372, 255373, 255374]
    assert cards[1] == {
        "id": 255373,
        "auction_url": "https://www.netauktion.se/auktion/bat-karra?product=255373",
        "title": "Glasfiberbåt med styrpuplet, Silver Viking",
        "place": "Torslanda/Björlanda",
        "close": "2026-11-02T21:14:00+01:00",
        "bid": 1700,
        "reserve_reached": False,
    }
    assert cards[2]["reserve_reached"] is True
    assert not netauktion.has_page(page("netauktion_category_bat.html"), 2)


def test_auction_links_skip_test_item():
    assert netauktion.parse_auction_links(page("netauktion_auction_bat-karra.html")) == {
        255372: "https://www.netauktion.se/auktion/produkt/aldre-batmotorer",
        255373: "https://www.netauktion.se/auktion/produkt/glasfiberbat-med-styrpuplet-silver-viking",
        255374: "https://www.netauktion.se/auktion/produkt/batkarra-oreggad-580cm",
    }  # not 1004, the always-open "PROVAUKTION"


def test_product_fields(viking):
    assert viking["id"] == 255373
    assert viking["title"] == "Glasfiberbåt med styrpuplet, Silver Viking"
    assert viking["url"] == "https://www.netauktion.se/auktion/produkt/glasfiberbat-med-styrpuplet-silver-viking"
    assert viking["close"] == "2026-11-02T21:14:00+01:00"
    assert viking["ended"] is False
    assert viking["bid"] == 1700 and viking["next_bid"] == 1800
    assert viking["vat"] == 25 and viking["margin_scheme"] is False
    assert viking["reserve_reached"] is False
    assert viking["place"] == "Torslanda/Björlanda"
    assert viking["viewing"].startswith("Ingen visning planerad")
    assert viking["pickup"] == "2026-11-06 kl 14.00 - 15.00 i Torslanda (enda tillfället)"
    assert len(viking["images"]) == 3 and viking["images"][0].endswith("-l.jpg")
    assert viking["specs"]["Marknadsvärde"].startswith("6 000 kr")
    assert "Längd ca 308cm." in viking["description"]
    assert "utan garanti/reklamationsrätt" in viking["description"]  # the special terms are appended


def test_ended_by_close_time(ended):
    assert ended["close"] == "2026-10-26T19:13:00+01:00"
    assert ended["ended"] is True
    assert netauktion.parse_product(page("netauktion_product_247241.html"), dt.datetime(2026, 10, 26, 12, 0, tzinfo=netauktion.TZ))["ended"] is False


def test_not_a_product_page():
    assert netauktion.parse_product("<html>404</html>") == {"id": 0}


# Expected totals are Netauktion's own top_bid_with_fee_and_tax / get_bid_calcuations (2026-10-27).
def test_buyer_total_vat_on_bid(viking, trailer):
    assert netauktion.buyer_total(viking) == (1700, 204, 2380)  # (1 700 + 12 %) * 1.25
    assert netauktion.buyer_total(trailer) == (5400, 648, 7560)
    assert netauktion.buyer_total(dict(viking, bid=10_000)) == (10_000, 1200, 14_000)


def test_buyer_total_margin_scheme(car):
    assert car["vat"] == 0 and car["margin_scheme"] is True
    assert netauktion.buyer_total(car) == (18500, 2220, 21275)  # 18 500 + 2 220 * 1.25
    assert netauktion.buyer_total(dict(car, bid=900)) == (900, 108, 1035)


def test_fee_bounds():
    assert netauktion.buyer_fee(0) == 0
    assert netauktion.buyer_fee(500) == 100  # minimum
    assert netauktion.buyer_fee(1_000_000) == 50_000  # cap


def test_wanted():
    assert netauktion.wanted("Motorbåt Ryds 568 DC", "") == "ryds 568"  # cabin variant code
    assert netauktion.wanted("Flipper 630 HT -2004", "") == "flipper 630"  # on the cabin list
    assert netauktion.wanted("Hyttbåt, Flipper 630", "") == "flipper 630"  # make after the comma
    assert netauktion.wanted("Motorbåt med trailer", "Märke: Flipper\nModell: 630 HT\n") == "flipper 630"
    assert netauktion.wanted("Glasfiberbåt med styrpuplet, Silver Viking", "") is None
    assert netauktion.wanted("Båtkärra, oreggad 580cm", "") is None
    assert netauktion.wanted("Äldre båtmotorer", "") is None


def test_to_ad(viking):
    ad = netauktion.to_ad(viking, viking["url"], None, "2026-10-27", "silver viking")
    assert ad["ad_id"] == netauktion.ID_OFFSET + 255373
    assert ad["source"] == "netauktion" and ad["dealer_segment"] == "Auktion"
    assert ad["price"] == 2380
    assert ad["make"] == "Silver" and ad["year"] is None
    assert ad["length_ft"] == 10.1  # "Längd ca 308cm"
    assert ad["status"] == "active"
    assert ad["auction"] == {
        "bid": 1700, "next_bid": 1800, "fee": 204, "vat": 25.0, "margin_scheme": False, "reserve_reached": False,
        "close": "2026-11-02T21:14:00+01:00", "ended": False,
        "viewing": "Ingen visning planerad. För mer info, sänd mail till Handläggare", "as_is": True,
    }
    assert ad["price_history"] == [{"date": "2026-10-27", "price": 2380, "bid": 1700}]


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text, self.status_code = text, status_code


class FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None):
        self.calls.append(url)
        return FakeResponse(self.pages[url]) if url in self.pages else FakeResponse("", 404)


def test_update_settles_ended(monkeypatch):
    db = {}
    monkeypatch.setattr(market, "learn_from_store", lambda: None)
    monkeypatch.setattr(store, "load_ad", lambda aid: db.get(aid))
    monkeypatch.setattr(store, "save_ad", lambda ad: db.__setitem__(ad["ad_id"], dict(ad)))
    monkeypatch.setattr(store, "save_page", lambda aid, html: None)
    monkeypatch.setattr(store, "all_ads", lambda: list(db.values()))
    # An ad seen running earlier, now off the listing; its page's end time has passed, reserve not met.
    url = "https://www.netauktion.se/auktion/produkt/solhybrid-395w-15st"
    aid = netauktion.ID_OFFSET + 247241
    db[aid] = {"ad_id": aid, "source": "netauktion", "status": "active", "url": url, "price": 5000}
    prod = "https://www.netauktion.se/auktion/produkt/"
    client = FakeClient({
        netauktion.LISTING: page("netauktion_category_bat.html"),
        "https://www.netauktion.se/auktion/bat-karra": page("netauktion_auction_bat-karra.html"),
        prod + "aldre-batmotorer": "",  # not a product page: listed, not kept
        prod + "glasfiberbat-med-styrpuplet-silver-viking": page("netauktion_product_255373.html"),
        prod + "batkarra-oreggad-580cm": page("netauktion_product_255374.html"),
        url: page("netauktion_product_247241.html"),
    })
    stats = netauktion.update(client, {})
    assert stats == {"netauktion_listed": 3, "netauktion_kept": 0, "netauktion_ended": 1}  # no cabin boats
    assert client.calls.count("https://www.netauktion.se/auktion/bat-karra") == 1  # one auction page for all cards
    assert db[aid]["status"] == "unsold"
    assert db[aid]["sold"] == {"date": db[aid]["sold"]["date"], "last_price": 7840, "final_bid": 5600, "reserve_reached": False}
