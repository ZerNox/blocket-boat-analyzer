import pytest

from boats import rules


def ey(desc, heading="Båt", specs=None, boat_year=2015):
    return rules.extract_engine_year(heading, desc, specs or {}, boat_year)


@pytest.mark.parametrize(
    "desc,year",
    [
        ("Ny motor 2026, Mercury 60 hk fyrtakt med 5 års garanti. Nyrenoverat skrov årsmodell 2015.", 2026),
        ("Båten är från 2008 med Yamaha F60 från 2015.", 2015),
        ("Motor: Suzuki DF50 årsmodell 2017. Servad 2024.", 2017),
        ("Mercury 40 hk, 2019. Båten byggd 2012.", 2019),
        ("2018 års Mercury 60 ELPT sitter på.", 2018),
        ("Motorn köptes ny 2019 och har gått 120 timmar.", 2019),
        ("Motorn servad 2023. Yamaha F40 -16.", 2016),
        ("✅ Årsmodell: 2009  ✅ Motor: Yamaha 50hk, 08/2011 – driftsäker 4-takt ✅ Längd: 4,7 m", 2011),
        ("fisherman 20 med 50hk tohatsu 2009 insprutning i toppskick", 2009),
        ("Micore 51 CC R-design & Yamaha 60 hk -2024 inkl båtvagn.", 2024),
        ("Den har bytt motor till en fyrtakts Evinrude 70 hk ca 20 år gammal.", rules.THIS_YEAR - 20),
    ],
)
def test_engine_year_found(desc, year):
    r = ey(desc, boat_year=2009)
    assert r["engine_year"] == year, r
    assert not r["needs_llm"]


@pytest.mark.parametrize(
    "desc",
    [
        "Fin båt årsmodell 2015. Servad 2023.",  # no engine year at all
        "Motorn servad 2023 och bytt impeller 2024.",  # only event years
        "Fin båt, bottenmålad 2022.",
    ],
)
def test_engine_year_left_for_llm(desc):
    r = ey(desc)
    assert r["engine_year"] is None and r["needs_llm"], r


def test_ambiguous_years_go_to_llm():
    r = ey("Motor Yamaha 2008. Mercury 60 hk 2014 på reservdelar.")
    assert r["needs_llm"] and r["engine_year"] is None


def test_spec_field_year_wins():
    r = ey("Motor från 2010.", specs={"Motortillverkare": "Yamaha F60 2018"})
    assert r["engine_year"] == 2018 and r["source"] == "rule:spec"


def test_original_engine_uses_boat_year():
    r = ey("Originalmotor, går fint.", boat_year=2011)
    assert r["engine_year"] == 2011 and r["source"] == "rule:original"


def test_old_engine_on_newer_hull_is_verified():
    r = ey("Mercury 40 hk från 1998.", boat_year=2015)
    assert r["engine_year"] == 1998 and r["needs_llm"]


def test_motorbat_is_not_engine_cue():
    assert ey("Motorbåt årsmodell 2015, fint skick.")["engine_year"] is None


def test_price_and_hours_are_not_years():
    assert ey("Motor 60 hk, pris 2000 kr.")["engine_year"] is None
    assert ey("Motor Yamaha, använd 2010-2015 i Stockholm.")["engine_year"] is None
    assert ey("Motor Yamaha 60 hk, 20 år gammal båt.")["engine_year"] is None


def test_motor_and_boat_same_year():
    assert ey("Både motor och båt är från -98, ny drevolja.", boat_year=1998)["engine_year"] == 1998


@pytest.mark.parametrize(
    "desc,item,included",
    [
        ("Inkl 30-kärra", "trailer", True),
        ("Båttrailer ingår.", "trailer", True),
        ("Säljes utan trailer.", "trailer", False),
        ("Trailer ingår ej men kan köpas separat.", "trailer", False),
        ("Trailer finns att köpa för 10 000 kr.", "trailer", False),
        ("Garmin Echomap inklusive sjökort.", "plotter", True),
        ("Garmin Echomap inklusive sjökort.", "ekolod", True),
        ("Nytt förkapell och sprayhood.", "kapell", True),
        ("Vinterkapell medföljer.", "vinterkapell", True),
        ("Minn Kota elmotor med GPS-ankare.", "elmotor", True),
        ("Ingen motor men trailer ingår.", "trailer", True),
    ],
)
def test_equipment(desc, item, included):
    eq = rules.extract_equipment("Båt", desc)
    assert eq[item]["included"] is included, eq


def test_trailerable_is_not_trailer():
    assert "trailer" not in rules.extract_equipment("Båt", "Lätt och trailerbar båt.")


def test_vinterkapell_is_not_kapell():
    eq = rules.extract_equipment("Båt", "Vinterkapell ingår.")
    assert "kapell" not in eq


def test_engine_type_conflict_goes_to_llm():
    r = rules.extract_engine_type("Båt", "Volvo Penta AQ 130 med drev.", {"Motortyp": "Utombordare"}, None)
    assert r["needs_llm"]


def test_engine_type_declared():
    r = rules.extract_engine_type("Båt", "Fin båt.", {"Motortyp": "Utombordare"}, None)
    assert r == {"engine_type": "outboard", "source": "rule:spec", "needs_llm": False}


def test_no_engine():
    r = rules.extract_engine_type("Båt", "Säljes utan motor.", {"Motortyp": "Utombordare"}, None)
    assert r["engine_type"] == "none"


@pytest.mark.parametrize(
    "desc,hours",
    [("gångtid 8h", 8), ("10-20h per år.", None), ("Motorn har gått ca 150 timmar.", 150), ("Gått 3 säsonger.", None), ("60 hk", None)],
)
def test_hours(desc, hours):
    assert rules.extract_hours(desc) == hours


def test_stroke():
    assert rules.extract_stroke("Mercury 60 hk fyrtakt", "") == 4
    assert rules.extract_stroke("Evinrude E-TEC 50", "") == 2
    assert rules.extract_stroke("Motor", "Yamaha F60") == 4


def test_red_flags():
    assert "project" in rules.extract_red_flags("Renoveringsobjekt, motorn startar inte.")
    assert "defect" in rules.extract_red_flags("Renoveringsobjekt, motorn startar inte.")
    assert "defect" in rules.extract_red_flags("Nu har tyvärr ett motorfel uppstått.")
    assert "leak" in rules.extract_red_flags("Det finns en spricka i skrovet.")
    for word in ("Repartionsobjekt", "Reparationsobjekt", "renoveringsbehov"):
        assert "project" in rules.extract_red_flags(word)
    assert rules.extract_red_flags("Fin båt i toppskick.") == []


def test_spec_no_engine_but_text_describes_one():
    r = rules.extract_engine_type("Båt", "Motor\nSuzuki DF25 V-Twin, 25 hk, 4-takt.", {"Motor inkluderad": "Nej"}, None)
    assert r["engine_type"] is None and r["needs_llm"]


def test_spec_no_engine():
    r = rules.extract_engine_type("Båt", "Fin eka.", {"Motor inkluderad": "Nej"}, None)
    assert r["engine_type"] == "none"


def _text(desc, heading="Båt"):
    return {"heading": heading, "description": desc, "specs": {}}


def test_llm_year_verification():
    from boats.llm import verify_year

    def v(desc, year, ev):
        return verify_year({"engine_year": year, "engine_year_evidence": ev}, _text(desc))["engine_year"]

    assert v("Motorn servades i maj 2026: olja bytt.", 2026, "2026") is None
    assert v("Motorn köpt begagnat 2024.", 2024, "köpt begagnat 2024") is None
    assert v("Motor: Yamaha 50hk, 08/2011 – driftsäker 4-takt", 2011, "08/2011") == 2011
    assert v("Mercury 40 hk från 2018, fint skick.", 2018, "Mercury 40 hk från 2018") == 2018
    assert v("Mercury 40 hk från 2018.", 2019, "Mercury 40 hk från 2019") is None  # quote not in ad
    assert v("Båten är från 2010.", 2010, "Båten är från 2010") is None


@pytest.mark.parametrize(
    "heading,desc,spec,expected",
    [
        ("Båt 25HK + Vagn BYTE?", "Säljer båt med motor och vagn.", 2026, None),  # placeholder year
        ("Motorbåt", "Planande motorbåt från 90-talet.", 2026, 1995),
        ("Ryds 435", "Båten är från 1994.", 1900, 1994),
        ("HR 442 SC Suzuki DF 50 2026", "Ny båt.", 2026, 2026),
        ("Buster", "Fin båt.", 2012, 2012),
    ],
)
def test_resolve_boat_year(heading, desc, spec, expected):
    assert rules.resolve_boat_year(heading, desc, spec)[0] == expected


@pytest.mark.parametrize(
    "text,offered",
    [("Båt 25HK + Vagn BYTE?", True), ("Kan tänka mig byte mot vattenskoter.", True), ("Säljes/bytes", True),
     ("Hösten 2024 byte av ny startmotor.", False), ("Byte impellerhus 2026.", False), ("Kom gärna med bud!", False)],
)
def test_swap(text, offered):
    assert rules.swap_offered(text) is offered


@pytest.mark.parametrize(
    "text",
    ["Brandsläckare och fendrar ingår.", "Fint kapell med några fläckar.", "Snyggt rött/vitt skrov.",
     "Inga lagningar eller sprickor i skrovet.", "Spolmuff. Inga läckage.", "Inget läckage.", "Läcker inte.",
     "Innergolvet har en spricka.", "Säljes i befintligt skick.", "Trä i relingen något defekt."],
)
def test_flag_false_positives(text):
    assert rules.extract_red_flags(text) == []


def test_leak():
    assert "leak" in rules.extract_red_flags("Motorn har ett kylvattenläckage, båten läcker lite.")


# --- patterns mined from the first full crawl (2026-09-27) ---

@pytest.mark.parametrize(
    "desc,year",
    [
        ("MARINER 75HK + BRENDERUP TRAILER 2019. Motor: Mariner 75 hk 2-takt, 2004", 2004),
        ("Ryds 440GT + Johnson 40hk -91 + Tiki 600 släp (2024, nyskick).", 1991),
        ("Ryds 510 GT 70hk Johnson -01 + 80km/h trailer -10.", 2001),
        ("Mercury 25 hk (2012)med ekolod, en fin Fogelstatrailer (2022) med boatbuckles.", 2012),
        ("Trailer från 2003 motor från 2005.", 2005),
        ("2011-Yamaha 70 F70-Båtsläp ingår.", 2011),
        ("HR 460 Fishing 2021 med Honda 40hk från 2022.", 2022),
        ("Uttern 495 HT 1980 Mercury 40hk 2006.", 2006),
        ("Båten är från -91 och motorn Yamaha 60 hk från-07.", 2007),
        ("Ryds 478 GT årsmodell 2003 med Mercury 60 Elpto-03 (2-takt), en ägare.", 2003),
        ("Mercury 60 hk, tillverkningsår 2020, gångtid 24 timmar.", 2020),
        ("Både motor ( Mercury 60 Efi 4-takt ) och båt är från 2003.", 2003),
        ("Båt, motor och trailer från 2018 men uttagna 2019.", 2018),
        ("En fin Ryds 435 ink en Evinrude 30 Hk samt TK 30 trailer allt från -98 med en ägare.", 1998),
        ("Båt, motor och 80km/trailer från 2006 välvårdad.", 2006),
        ("Båt, motor samt trailer i paketpris allt från 2020.", 2020),
    ],
)
def test_mined_engine_years(desc, year):  # noqa: D103
    r = ey(desc, boat_year=1980)
    assert r["engine_year"] == year, r


@pytest.mark.parametrize(
    "desc",
    ["Nyservad motor hösten -25.", "Motorn servad våren 2026.", "Kamremsbyte juni 2026 på motorn.",
     "Motorn är servad inför säsongen 2026.", "Motorn är servad förra året (2025)."],
)
def test_season_and_month_are_events(desc):
    assert ey(desc)["engine_year"] is None


@pytest.mark.parametrize(
    "desc,year,kmh",
    [
        ("Ryds 510 GT 70hk Johnson -01 + 80km/h trailer -10.", 2010, 80),
        ("Fin Fogelstatrailer (2022) med boatbuckles.", 2022, None),
        ("Inkl 30-kärra", None, 30),
        ("Brenderup 80km/h besiktigad till 2027.", None, 80),
        ("Paketpris inklusive besiktigad 80-trailer: 85.000kr", None, 80),
        ("Trailer från 2003 motor från 2005.", 2003, None),
    ],
)
def test_trailer_details(desc, year, kmh):
    t = rules.extract_trailer(desc)
    assert (t.get("year"), t.get("kmh")) == (year, kmh), t


@pytest.mark.parametrize(
    "text,boat_year,expected_max",
    [
        ("Johnson 40hk 2-takt", 2012, 2007),
        ("Evinrude 60hk", 2015, 2007),
        ("Evinrude E-TEC 50", 2022, 2020),
        ("60hk Volvo Penta utombordare", 1995, 1990),
        ("Mercury 40 hk tvåtakt", 2014, 2007),
        ("Yamaha F60 fyrtakt", 2015, 2015),
    ],
)
def test_engine_year_estimate(text, boat_year, expected_max):
    est = rules.estimate_engine_year(text, "", boat_year)
    assert est["year"] == expected_max, est
