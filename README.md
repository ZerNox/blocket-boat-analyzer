# Blocket boat analyzer

Fetches used boats from a Blocket search, works out **engine type** (outboard vs. inboard) and
**engine year** from the unstructured ad text, finds what equipment is included (trailer,
plotter, ekolod, kapell, …), and ranks the boats by value. The result is published as a static
site on GitHub Pages.

## How it works

```
Blocket search API ──► db/ads/<id>.json  (git: structured fields, price history, extraction)
Blocket ad page    ──► cache/pages/      (local only: the original ad, re-read on every extract)
                          │
            rules.py  ◄───┘  regex/cue rules: engine year, type, hours, equipment, red flags
               │ unresolved / ambiguous
               ▼
            llm.py       Qwen2.5-7B on the local Arc GPU (llama.cpp, JSON-schema output)
                         year accepted only if the quoted sentence is in the ad and isn't a service date
               │
            rank.py      fair price model ──► value score
               │
            site/        static page + boats.json ──► GitHub Pages (Actions)
```

### Engine year

Ads mix hull year, engine year, service and purchase years: *"Båt -15, Mercury 60 hk 2018,
servad 2024"*. The rules split the text into sentences and assign each year to the nearest cue
before it:

| cue before the year | meaning | example |
|---|---|---|
| engine (motor, Yamaha, F60, 60 hk, fyrtakt…) | engine year | `Yamaha F60 2019` |
| new (ny, köpt ny) | engine year | `Ny motor 2026` |
| event (servad, bytt, köpt, garanti…) | ignored | `servad 2023` |
| boat (båt, skrov, byggd…) | boat year | `skrov årsmodell 2015` |
| trailer (trailer, kärra, släp, Tiki, Fogelsta…) | trailer year | `Tiki 600 släp (2024)` |
| date words (hösten, juni, säsongen, förra året) | ignored | `servad hösten -25` |

Lists share a year: `Både motor (Mercury 60 Efi) och båt är från 2003`, `Båt, motor samt trailer
… allt från 2020`. A year only counts as leading an engine (`2011-Yamaha 70`) at the start of its
clause, so `Uttern 495 HT 1980 Mercury 40hk 2006` gives 2006, not 1980.

When no engine year is stated, an **estimate** is used: the boat's year, capped by when that
engine family stopped being sold new. Johnson and non-E-TEC Evinrude end at 2007, E-TEC at 2020,
Volvo Penta outboards around 1990, and carburetted two-strokes (EU emission rules) at 2007. The
site shows it as `≈2007?` with the reason, and the model still treats the year as unknown.

The trailer's speed class (30 vs. 80 km/h) and year are parsed too, and set its value.

The `Motortillverkare` spec field is checked first. If exactly one engine year is found, the
rules accept it. If none is found, or there are conflicting years, or the engine looks much older
than the hull, the ad goes to the local LLM. The LLM must quote the sentence its year came from.
That quote must appear verbatim in the ad, and the same cue check is run on it, so a service date
the model misreads as the engine year is thrown out.

### Value ranking

1. **Adjusted price** = asking price − value of included extras (SEK values in `config.toml`;
   trailer 20 000 for 80 km/h, 7 000 for a 30 km/h kärra, 12 000 unknown, −4 %/year of age). Extras explicitly excluded ("trailer ingår ej", "kan köpas till")
   count for nothing.
2. **Fair price**: ridge regression of log(adjusted price) on boat age, engine age, log(hp),
   length, 2/4-stroke, boat class and make, fitted on all comparable ads (outliers refit out).
3. **Value** = fair / adjusted − 1. Red flags (renoveringsobjekt, defekt, läcker, befintligt
   skick) and an unknown engine year lower the score.
4. The site adds sliders to tilt the ranking toward a newer engine, more hp or more equipment,
   and a "Varför denna värdering?" breakdown per boat.

Only outboard boats with an engine included are ranked (`ranking.outboard_only`).

## Usage

```bash
uv sync
uv run boats fetch            # search + download new/changed ad pages (polite: 1 request at a time)
uv run boats extract          # rules, then local LLM for unresolved ads (holds the shared GPU lock)
uv run boats extract --no-llm # rules only
uv run boats rank --top 30    # ranking in the terminal
uv run boats site             # build _site/ locally
uv run boats publish          # commit db/ and push; GitHub Actions rebuilds the site
uv run boats update --publish # all of the above

uv run boats extract --llm-all && uv run boats audit   # measure rule vs LLM agreement
```

Change the search in `config.toml` (`search.url`, any Blocket boat search URL). Searches over
2 500 hits are split into price bands automatically.

### Local LLM

`llama-server` (llama.cpp Vulkan build) is started for each extract run and stopped afterwards.
The GPU is chosen by name (`device_match = "Arc"`) because Vulkan device indices change between
boots. The GPU lock file is shared with other GPU jobs on the host.

## Data and privacy

The repo is public, so `db/` holds only structured fields (price, year, hp, specs), the
extracted values and short evidence quotes with phone numbers and emails removed. Full ad pages
stay in `cache/`, which is gitignored. The site links to the original ads on Blocket.

## GitHub Pages setup (once)

Repo → Settings → Pages → Source: **GitHub Actions**.
