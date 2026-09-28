import pytest

from boats import market


@pytest.fixture(autouse=True)
def named():
    market.NAMED_MODELS.clear()
    market.learn_named_models([("Buster", "L"), ("Buster", "XL"), ("Joda", "Kuling"), ("Crescent", "Primo"), ("Ryds", "Camping"), ("Buster", "M")])


@pytest.mark.parametrize(
    "make,model,heading,expected",
    [
        ("Sandström", "560 MC", None, ("sandström 560", "sandström 560 mc")),
        ("Ryds", None, "Ryds 478gt 60hk Mariner 2takt +80km trailer", ("ryds 478", "ryds 478 gt")),
        ("Ryds", "511GTS", None, ("ryds 511", "ryds 511 gts")),
        ("Ryds", None, "Ryds 511 GTS + 70hk Johnson", ("ryds 511", "ryds 511 gts")),
        ("Yamarin", "50sc", None, ("yamarin 50", "yamarin 50 sc")),
        ("Crescent", None, "Fiskebåt med 80 trailer", (None, None)),
        ("Buster", None, "Buster L med Yamaha F50 fyrtakt", ("buster l", None)),
        ("Askeladden", "4.30", None, ("askeladden 430", None)),
        ("Ryds", None, "Ryds 425 2018 med Tohatsu MFS 9.8", ("ryds 425", None)),
        ("Ryds", None, "Ryds med 50hk", (None, None)),
        ("Ryds", None, "Ryds camping 565 med 80hk Mercury", ("ryds camping", "ryds camping 565")),
        ("Uttern", "A46", None, ("uttern a46", None)),
        ("Övriga", "Siljan", None, (None, None)),
        ("Takacat", "T260S", None, ("takacat t260", "takacat t260 s")),
        ("Buster", None, "Buster M med Yamaha F40", ("buster m", None)),
    ],
)
def test_model_keys(make, model, heading, expected):
    assert market.model_keys(make, model, heading) == expected


def test_variant_keys_for_model_list():
    assert market.model_keys("Sandström", None, "Sandström 560 MC")[1] == "sandström 560 mc"
    assert market.model_keys("Sandström", None, "Sandström 560mc. 60 hk Honda. 2023")[1] == "sandström 560 mc"
    assert market.model_keys("Sandström", None, "Sandström 565 CC Mercury 50hk")[1] == "sandström 565 cc"


def test_qualifier_makes_variant():
    assert market.model_keys("Sandström", None, "SANDSTRÖM CLASSIC 560 HONDA 60 HK") == ("sandström 560", "sandström 560 classic")
    assert market.model_keys("Sandström", None, "Sandström 565 Classic 2017") == ("sandström 565", "sandström 565 classic")
    assert market.model_keys("Sandström", None, "Sandström 560 MC") == ("sandström 560", "sandström 560 mc")


def test_parse_reads_utrustning_section():
    from boats.parse import parse_item

    html = ('<section><h2 class="t3 mb-0">Beskrivning</h2><div data-testid="expandable-section"><div><p>Fin båt.</p></div></div></section>'
            '<section><h2 class="t3 mb-0">Utrustning</h2><div data-testid="expandable-section"><div><p>Ny dieselvärmare. Radar.</p></div></div></section>')
    d = parse_item(html)["description"]
    assert "Fin båt." in d and "Utrustning:" in d and "dieselvärmare" in d
