"""Parse a Blocket mobility item page into description + spec table."""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup


def parse_item(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # "Beskrivning" is the free text; "Utrustning" is a separate section further down that sellers
    # use for the equipment list (heater, radar, plotter...). Both are read.
    sections = {"Beskrivning": "", "Utrustning": ""}
    for h2 in soup.find_all("h2"):
        title = h2.get_text(strip=True)
        if title in sections and not sections[title]:
            box = h2.find_next("div", attrs={"data-testid": "expandable-section"})
            if box:
                for br in box.find_all("br"):
                    br.replace_with("\n")
                parts = [p.get_text(" ", strip=False) for p in box.find_all(["p", "li"])]
                sections[title] = _clean("\n".join(parts) if parts else box.get_text("\n"))
    description = sections["Beskrivning"]
    if sections["Utrustning"]:
        description = f"{description}\nUtrustning:\n{sections['Utrustning']}".strip()

    specs: dict[str, str] = {}
    for dl in soup.select("section.key-info dl, div.specifications-area dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                specs.setdefault(dt.get_text(" ", strip=True), dd.get_text(" ", strip=True))

    title = soup.find("h1")
    return {
        "title": title.get_text(" ", strip=True) if title else None,
        "description": description,
        "specs": specs,
    }


def _clean(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def text_sha(heading: str, description: str, specs: dict) -> str:
    blob = heading + "\n" + description + "\n" + repr(sorted(specs.items()))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
