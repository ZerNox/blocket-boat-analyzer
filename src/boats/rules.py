"""Deterministic extraction from ad text: engine type, engine year, hours, equipment, red flags.

Engine year is the hard one. Ads mix hull year, engine year, service years and
purchase years in free Swedish text ("Båt -15, Mercury 60 hk 2018, servad 2024").
Each year mention is attributed to the nearest preceding cue in its sentence
(engine / boat / service event). Years after a service-event cue are events,
not the engine's age. Exactly one distinct engine-attributed year is accepted.
Anything else (none, several, implausible) is left for the local LLM.
"""

from __future__ import annotations

import datetime as dt
import re

RULES_VERSION = 5
THIS_YEAR = dt.date.today().year

BRANDS = (
    "mercury|merc|mariner|yamaha|suzuki|honda|evinrude|johnson|tohatsu|selva|parsun|"
    "nissan|hidea|mercruiser|volvo penta|penta|e-?tec|optimax|verado"
)
ENGINE_CUE = re.compile(
    rf"\b(?:utombordsmotor\w*|utombordare\w*|aktersnurra\w*|motor(?!båt)\w*|engine|{BRANDS})\b"
    r"|\b\d{1,3}\s?(?:hk|hp|hästar|hästkrafter)\b"
    r"|\b(?:f|df|bf|ft|fl|me|ef)\s?\d{2,3}[a-z]{0,4}\b"
    r"|\b(?:fyrtakt\w*|tvåtakt\w*|[24]-?takt\w*)\b",
    re.I,
)
BOAT_CUE = re.compile(
    r"\b(?:båt(?:en|ens)?|\w+båt(?:en)?|skrov(?:et|ets)?|hull|byggd|byggår|tillverkningsår)\b", re.I
)
# A year right after one of these is when something happened to the engine, not its age.
EVENT_CUE = re.compile(
    r"\b(?:servad\w*|servade\w*|service\w*|servicad\w*|renover\w*|bytt\w*|byte|impeller\w*|"
    r"garanti\w*|besikt\w*|bottenmål\w*|målad\w*|lackad\w*|översyn\w*|genomgång\w*|"
    r"kontroller\w*|reparer\w*|lagad\w*|uppgrader\w*|tvättad\w*|försäkr\w*|"
    r"vinterförvar\w*|sjösatt\w*|senast|sedan|köpt\w*|köpte\w*|inköpt\w*|ägt|förvärv\w*)\b",
    re.I,
)
# "köpt ny 2019", "ny motor 2026" — the year the engine was new.
NEW_CUE = re.compile(r"\b(?:köpt\w*\s+ny|ny(?:köpt)?|nyinköpt|levererad\s+ny)\b", re.I)
MOTOR_AND_BOAT = re.compile(r"\b(?:både\s+)?(?:motor\w*\s+och\s+båt\w*|båt\w*\s+och\s+motor\w*)\b", re.I)
ENGINE_AGE = re.compile(r"(?:ca\.?\s+|cirka\s+|runt\s+)?(\d{1,2})\s*år\s+gam(?:mal|la|malt)(?!\s+(?:båt|skrov|\w+båt))", re.I)
ORIGINAL_ENGINE = re.compile(
    r"\b(?:orginal|original)\s*motor|motor\w*\s+(?:är\s+)?(?:orginal|original)|"
    r"motor\w*\s+(?:är\s+)?(?:från\s+)?samma\s+år",
    re.I,
)
YEAR = re.compile(r"(?<![\d.,/])(?<!\d-)(19[5-9]\d|20[0-4]\d)(?![\d.,]?\d)(?!\s?[-–]\s?(?:19|20)\d\d)(?!\s?(?:kr|:-|sek|mil|tim|h\b|rpm|varv|kg|mm|cm|st\b|km))", re.I)
MONTH_YEAR = re.compile(r"(?<![\d/])(?:0?[1-9]|1[0-2])/((?:19|20)\d\d)\b")
SHORT_YEAR = re.compile(r"(?:(?<=\s)-|årsmodell\s|årsm\.?\s?|mod\.?\s|år\s|\s')(\d{2})\b(?!\s?(?:hk|hp|kr|tim|h\b|%))", re.I)

HOURS = re.compile(
    r"(?:gångtid|gångtimmar|drifttimmar|timräknare)\D{0,12}?(\d{1,4})"
    r"|(\d{1,4})\s?(?:h|tim|timmar|gångtimmar|drifttimmar)\b",
    re.I,
)

INBOARD_CUE = re.compile(
    r"\b(?:inombordare\w*|inombordsmotor\w*|inombords|aquamatic|z-?drev|drev\w*|mercruiser|saildrive|"
    r"volvo\s*penta\s*(?:md|aq|d\d)|dieselmotor\w*|jet-?drift)\b",
    re.I,
)
OUTBOARD_CUE = re.compile(r"\b(?:utombordare\w*|utombordsmotor\w*|aktersnurra\w*|utombords)\b", re.I)

# Equipment: item -> (positive pattern, excluded prefixes)
EQUIPMENT = {
    "trailer": r"\w*trailer(?:n|ns)?\b|\w*kärra(?:n)?\b|båtvagn\w*|släpvagn\w*|\bsläp(?:et)?\b|brenderup|respo\b|boat-?lift",
    "plotter": r"\w*plotter\w*|\bgps\w*|echomap|gpsmap|\bmfd\b|navionics|lowrance|simrad|raymarine|\bb&g\b",
    "ekolod": r"\w*ekolod\w*|fishfinder|\bsonar\b|livescope|echomap|humminbird|striker|garmin\s+(?:echo|striker)",
    "kapell": r"\b(?!vinter)\w*kapell\w*|sprayhood|suncover|bimini|\bcapell",
    "vinterkapell": r"vinterkapell\w*|vintertäck\w*|presenning\w*|täckpresenning",
    "elmotor": r"\belmotor\w*|\bel-motor\w*|trolling\s?-?motor\w*|minn\s?kota|motorguide|garmin\s+force|bogmotor\w*",
    "hjalpmotor": r"hjälpmotor\w*|reservmotor\w*|extramotor\w*",
    "vhf": r"\bvhf\b|kommunikationsradio",
    "stereo": r"stereo\w*|\bfusion\b|högtalare|bluetooth|\bradio\b",
    "hydraulstyrning": r"hydraul\w*\s?styr\w*|servostyr\w*|hydrauliskt?\s+styr\w*",
    "landstrom": r"landström\w*|batteriladdare|\bladdare\b",
}
EQUIPMENT_RE = {k: re.compile(v, re.I) for k, v in EQUIPMENT.items()}
NEG_BEFORE = re.compile(r"\b(?:utan|ej|inte|ingen|inga|exkl\w*|exklusive|förutom)\b(?:(?!\bmen\b)[^,.;\n]){0,25}$", re.I)
NEG_AFTER = re.compile(
    r"^[^.;\n]{0,40}?\b(?:ingår\s+(?:ej|inte)|medföljer\s+(?:ej|inte)|ej\s+med|inte\s+med|säljs\s+separat|"
    r"kan\s+(?:köpas|fås|erhållas)|finns\s+att\s+(?:köpa|få)|tillkommer|mot\s+tillägg|extra\s+kostnad|"
    r"(?:ingår\s+)?ej\s+i\s+priset|ingår\s+inte\s+i\s+priset|separat|saknas|finns\s+(?:ej|inte))\b",
    re.I,
)
# "trailerbar", "plats för trailer", "trailer finns inte" style false positives.
TRAILER_FALSE = re.compile(r"trailerbar\w*|trailervänlig\w*|lätt\s+att\s+traila|trailas", re.I)

RED_FLAGS = {
    "project": r"renoveringsobjekt|renoveringsbehov|projektbåt|\bprojekt\b|behöver\s+(?:renoveras|lagas|åtgärdas|ses\s+över)",
    "defect": r"defekt\w*|trasig\w*|haveri\w*|skär(?:t|er)|startar\s+(?:inte|ej)|går\s+(?:inte|ej)\s+(?:att\s+)?(?:starta|igång)|motorfel|kompression\w*\s+(?:låg|dålig)",
    "leak": r"läck\w*|vatten\s+i\s+(?:båten|skrovet|kölsvinet)|spricka|sprickor|röt\w*",
    "as_is": r"befintligt\s+skick|säljes\s+som\s+den\s+är|i\s+befintligt|okänd\s+status|ej\s+provkörd",
    "swap": r"\bbyte\b|\bbytes\b|byta\s+mot|\bbud\b|högstbjudande|lägg\s+bud",
    "no_engine": r"\butan\s+motor\b|motor\s+ingår\s+(?:ej|inte)|motorn\s+ingår\s+(?:ej|inte)|säljes\s+utan\s+motor",
}
RED_FLAG_RE = {k: re.compile(v, re.I) for k, v in RED_FLAGS.items()}

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÅÄÖ0-9])|\n+|;\s*|\s[-–•*]\s|[✅✔☑•▪►➤🔹🔸]\s*")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE_SPLIT.split(text) if s and s.strip()]


def _plausible(year: int) -> bool:
    return 1955 <= year <= THIS_YEAR + 1


def _nearest_cue_before(sentence: str, pos: int) -> str | None:
    """Kind of the cue closest before pos: 'new' | 'event' | 'engine' | 'boat' | None.

    On a tie in position the more specific kind wins ("köpt ny" is new, not a purchase event).
    """
    head = sentence[:pos]
    best: tuple[int, int, str] | None = None
    for prio, (kind, rx) in enumerate((("boat", BOAT_CUE), ("engine", ENGINE_CUE), ("event", EVENT_CUE), ("new", NEW_CUE))):
        for m in rx.finditer(head):
            cand = (m.start(), prio, kind)
            if best is None or cand[:2] > best[:2]:
                best = cand
    return best[2] if best else None


def year_mentions(sentence: str) -> list[tuple[int, int]]:
    out = [(int(m.group(1)), m.start(1)) for m in YEAR.finditer(sentence)]
    out += [(int(m.group(1)), m.start(1)) for m in MONTH_YEAR.finditer(sentence)]
    for m in SHORT_YEAR.finditer(sentence):
        yy = int(m.group(1))
        year = 2000 + yy if yy <= (THIS_YEAR + 1) % 100 else 1900 + yy
        if year >= 1960:
            out.append((year, m.start(1)))
    return [(y, p) for y, p in out if _plausible(y)]


def engine_year_candidates(text: str) -> list[dict]:
    """Every year mention attributed to the engine, with the sentence as evidence."""
    cands = []
    for sent in sentences(text):
        for m in ENGINE_AGE.finditer(sent):
            # "Evinrude 70 hk ca 20 år gammal"
            if _nearest_cue_before(sent, m.start()) == "engine":
                cands.append({"year": THIS_YEAR - int(m.group(1)), "evidence": sent, "how": "age"})
        for year, pos in year_mentions(sent):
            kind = _nearest_cue_before(sent, pos)
            both = MOTOR_AND_BOAT.search(sent[:pos])
            if both and pos - both.end() < 25:
                # "Både motor och båt är från -98"
                cands.append({"year": year, "evidence": sent, "how": "both"})
                continue
            if kind == "new":
                # "ny motor 2026" / "motorn köpt ny 2019": needs an engine cue somewhere in the sentence
                if ENGINE_CUE.search(sent):
                    cands.append({"year": year, "evidence": sent, "how": "new"})
                continue
            if kind == "event":
                # "servad 2023" is an event — unless an engine cue sits between the event word and the year
                # ("service 2024. Motor 2019" is split by sentence already).
                continue
            if kind == "engine":
                cands.append({"year": year, "evidence": sent, "how": "cue"})
                continue
            if kind is None:
                # Year leads the clause: "2018 års Mercury 60", "2019 Yamaha F40"
                tail = re.sub(r"^\s*(?:års?\s+)?", "", sent[pos + 4 : pos + 30])
                if ENGINE_CUE.match(tail):
                    cands.append({"year": year, "evidence": sent, "how": "lead"})
    return cands


def boat_year_mentions(text: str) -> set[int]:
    out = set()
    for sent in sentences(text):
        for year, pos in year_mentions(sent):
            kind = _nearest_cue_before(sent, pos)
            if kind == "boat":
                out.add(year)
    return out


DECADE = re.compile(r"\b(?:19)?([5-9]0)-?tal(?:et|s)?\b", re.I)
NEW_BOAT = re.compile(r"\b(?:ny\s+båt|nyskick|oanvänd|fabriksny|ny\s+för\s+året|ny\s+\d{4}|årsmodell\s+20\d\d|demo\w*)\b", re.I)


def resolve_boat_year(heading: str, description: str, spec_year: int | None) -> tuple[int | None, str | None]:
    """Blocket's year field, unless it's a placeholder (current year / 1900) the text doesn't back up."""
    text = f"{heading}.\n{description}"
    text_years = boat_year_mentions(text)
    decade = DECADE.search(text)
    fallback = (max(text_years), "text") if text_years else ((1900 + int(decade.group(1)) + 5, "text:decade") if decade else (None, None))
    if spec_year is None or not (1950 <= spec_year <= THIS_YEAR + 1):
        return fallback
    if spec_year >= THIS_YEAR - 1:
        if spec_year in text_years or str(spec_year) in heading or NEW_BOAT.search(text):
            return spec_year, "spec"
        if fallback[0] is None and not re.search(rf"\b{spec_year}\b", text):
            return None, None  # placeholder year, nothing better in the text
        return fallback if fallback[0] else (spec_year, "spec")
    return spec_year, "spec"


def extract_engine_year(heading: str, description: str, specs: dict, boat_year: int | None) -> dict:
    """{'engine_year', 'source', 'evidence', 'needs_llm', 'reason'}"""
    maker = specs.get("Motortillverkare", "") or ""
    spec_years = [y for y, _ in year_mentions(maker)]
    if len(set(spec_years)) == 1:
        return {"engine_year": spec_years[0], "source": "rule:spec", "evidence": f"Motortillverkare: {maker}", "needs_llm": False}

    text = f"{heading}.\n{description}"
    cands = engine_year_candidates(text)
    years = sorted({c["year"] for c in cands})

    if not years and boat_year and ORIGINAL_ENGINE.search(text):
        m = ORIGINAL_ENGINE.search(text)
        return {"engine_year": boat_year, "source": "rule:original", "evidence": _around(text, m.start()), "needs_llm": False}
    if not years:
        return {"engine_year": None, "source": None, "needs_llm": True, "reason": "no engine year found"}
    if len(years) > 1:
        # "ny motor 2026" beats older mentions of the replaced engine
        new = sorted({c["year"] for c in cands if c["how"] == "new"})
        if len(new) == 1 and new[0] == years[-1]:
            c = next(c for c in cands if c["year"] == new[0])
            return {"engine_year": new[0], "source": "rule:text", "evidence": c["evidence"], "needs_llm": False}
        return {"engine_year": None, "source": None, "needs_llm": True, "reason": f"ambiguous engine years {years}"}

    year = years[0]
    c = next(c for c in cands if c["year"] == year)
    if boat_year and year < boat_year - 3:
        # Older engine on a newer hull happens, but it is also the classic misread. Let the LLM confirm.
        return {"engine_year": year, "source": "rule:text", "evidence": c["evidence"], "needs_llm": True,
                "reason": f"engine {year} older than boat {boat_year}"}
    return {"engine_year": year, "source": "rule:text", "evidence": c["evidence"], "needs_llm": False}


def _around(text: str, pos: int, width: int = 90) -> str:
    return text[max(0, pos - width // 3) : pos + width].strip()


def extract_engine_type(heading: str, description: str, specs: dict, search_motor_type: str | None) -> dict:
    declared = specs.get("Motortyp") or search_motor_type
    included = (specs.get("Motor inkluderad") or "").lower()
    text = f"{heading}\n{description}"
    inb = INBOARD_CUE.search(text)
    outb = OUTBOARD_CUE.search(text)
    if RED_FLAG_RE["no_engine"].search(text):
        return {"engine_type": "none", "source": "rule:text", "needs_llm": False}
    if included == "nej":
        # Sellers mis-set this field: trust it only when the text doesn't describe an engine.
        engine_described = re.search(rf"\b(?:{BRANDS})\b|\b\d{{1,3}}\s?(?:hk|hp)\b", text, re.I)
        if not engine_described:
            return {"engine_type": "none", "source": "rule:spec", "needs_llm": False}
        return {"engine_type": None, "source": None, "needs_llm": True,
                "reason": "spec says no engine but text describes one", "evidence": _around(text, engine_described.start())}
    mapping = {"utombordare": "outboard", "inombordare": "inboard", "drev": "sterndrive", "el": "electric"}
    declared_norm = next((v for k, v in mapping.items() if declared and k in declared.lower()), None)
    if declared_norm == "outboard" and inb and not outb:
        return {"engine_type": "outboard", "source": "rule:spec", "needs_llm": True,
                "reason": f"declared outboard but text says '{inb.group(0)}'", "evidence": _around(text, inb.start())}
    if declared_norm == "inboard" and outb and not inb:
        return {"engine_type": "inboard", "source": "rule:spec", "needs_llm": True,
                "reason": f"declared inboard but text says '{outb.group(0)}'", "evidence": _around(text, outb.start())}
    if declared_norm:
        return {"engine_type": declared_norm, "source": "rule:spec", "needs_llm": False}
    if outb and not inb:
        return {"engine_type": "outboard", "source": "rule:text", "needs_llm": False, "evidence": _around(text, outb.start())}
    if inb and not outb:
        return {"engine_type": "inboard", "source": "rule:text", "needs_llm": False, "evidence": _around(text, inb.start())}
    return {"engine_type": None, "source": None, "needs_llm": True, "reason": "engine type unknown"}


def extract_hours(text: str) -> int | None:
    for sent in sentences(text):
        if not re.search(r"gångtid|gått|timmar|tim\b|\d\s?h\b|drifttimmar", sent, re.I):
            continue
        m = HOURS.search(sent)
        if m and not re.match(r"\s*(?:per|om|/)\s*(?:år|säsong|sommar)", sent[m.end():], re.I):
            val = int(m.group(1) or m.group(2))
            if 0 < val < 5000 and not (1955 <= val <= THIS_YEAR + 1):
                return val
    return None


def extract_stroke(text: str, maker: str) -> int | None:
    blob = f"{text} {maker}"
    if re.search(r"fyrtakt|4-?takt|four.?stroke|4-?stroke", blob, re.I):
        return 4
    if re.search(r"tvåtakt|2-?takt|two.?stroke|2-?stroke|e-?tec|optimax", blob, re.I):
        return 2
    # Modern model codes: Yamaha F/FT, Suzuki DF, Honda BF, Mercury "F60"/"ELPT EFI" are four-strokes.
    if re.search(r"\b(?:f|ft|df|bf)\s?\d{2,3}", blob, re.I):
        return 4
    return None


def extract_equipment(heading: str, description: str) -> dict:
    """item -> {'included': True|False, 'evidence': str}; items not mentioned are omitted."""
    out: dict[str, dict] = {}
    text = f"{heading}\n{description}"
    for sent in sentences(text):
        for item, rx in EQUIPMENT_RE.items():
            for m in rx.finditer(sent):
                if item == "trailer" and TRAILER_FALSE.search(sent[max(0, m.start() - 5) : m.end() + 25]):
                    continue
                if item == "elmotor" and re.match(r"elmotor\w*\s+(?:till|för)\s+(?:trim|ankar)", sent[m.start():], re.I):
                    continue
                negated = bool(NEG_BEFORE.search(sent[: m.start()]) or NEG_AFTER.search(sent[m.end():]))
                prev = out.get(item)
                # A positive mention wins over a negated one only if nothing was excluded explicitly.
                if prev is None or (prev["included"] and negated):
                    out[item] = {"included": not negated, "evidence": sent}
                break
    return out


def extract_red_flags(text: str) -> list[str]:
    return [k for k, rx in RED_FLAG_RE.items() if rx.search(text)]


def extract(heading: str, description: str, specs: dict, boat_year: int | None, search_motor_type: str | None) -> dict:
    text = f"{heading}\n{description}"
    etype = extract_engine_type(heading, description, specs, search_motor_type)
    eyear = extract_engine_year(heading, description, specs, boat_year)
    maker = specs.get("Motortillverkare") or ""
    reasons = [r for r in (etype.get("reason"), eyear.get("reason")) if r]
    if etype["engine_type"] == "none":
        eyear = {"engine_year": None, "source": None, "needs_llm": False}
        reasons = []
    boat_year, boat_year_source = resolve_boat_year(heading, description, boat_year)
    return {
        "rules_version": RULES_VERSION,
        "boat_year": boat_year,
        "boat_year_source": boat_year_source,
        "engine_type": etype["engine_type"],
        "engine_type_source": etype["source"],
        "engine_type_evidence": etype.get("evidence"),
        "engine_year": eyear["engine_year"],
        "engine_year_source": eyear["source"],
        "engine_year_evidence": eyear.get("evidence"),
        "engine_maker": maker or None,
        "engine_hours": extract_hours(text),
        "engine_stroke": extract_stroke(text, maker),
        "equipment": extract_equipment(heading, description),
        "red_flags": [f for f in extract_red_flags(text) if f != "no_engine"],
        "needs_llm": bool(etype["needs_llm"] or eyear["needs_llm"]),
        "llm_reasons": reasons,
    }
