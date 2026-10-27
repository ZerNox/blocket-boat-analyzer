from boats import scrape

class FakeClient:
    def __init__(self, text: str):
        self.text, self.calls = text, 0

    def get(self, url, params=None):
        self.calls += 1
        return type("R", (), {"status_code": 200, "text": self.text})()

def test_without_dealer_filter_keeps_other_params():
    url = "https://www.blocket.se/mobility/search/boat?class=7962&dealer_segment=1&price_to=300000"
    assert scrape._without_dealer_filter(url) == "https://www.blocket.se/mobility/search/boat?class=7962&price_to=300000"

def test_dealer_auktion_from_company_name_needs_no_page():
    client = FakeClient("")
    d = {"id": "1", "organisation_name": "Vaxxa.se - Nätauktioner", "heading": "Ryds 550"}
    assert scrape.dealer_auktion(client, d, {})
    assert client.calls == 0

def test_dealer_auktion_reads_page_once_per_version(monkeypatch):
    monkeypatch.setattr(scrape, "parse_item", lambda html: {"description": html, "specs": {}})
    monkeypatch.setattr(scrape.store, "save_page", lambda *a: None)
    checked: dict = {}
    d = {"id": "2", "organisation_name": "Båtcenter AB", "heading": "Ryds 550", "timestamp": 5}
    assert not scrape.dealer_auktion(FakeClient("Fin båt, ring oss"), d, checked)
    client = FakeClient("Säljs på auktion")
    assert not scrape.dealer_auktion(client, d, checked)  # same version: cached verdict
    assert client.calls == 0
    assert scrape.dealer_auktion(client, {**d, "timestamp": 6}, checked)  # ad edited: re-read


def test_scrub_keeps_dates_drops_phones():
    from boats import store

    assert store.scrub("visning 2026-10-29 13:00-14:00") == "visning 2026-10-29 13:00-14:00"
    assert store.scrub("ring 070-123 45 67") == "ring [tel]"
