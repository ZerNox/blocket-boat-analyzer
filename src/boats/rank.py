"""Value ranking: compare each asking price to what the market asks for that specific model.

Stage A — the whole Blocket boat market (db/market.jsonl, ~11k ads, listing fields only):
  ridge regression of log(price) on boat age, hp, length, boat type, engine type, fuel and
  dealer/private, then shrunken effects for make -> model family -> variant fitted on the
  residuals ("Sandström 560" vs. an average 19 ft hyttbåt). A level with n ads gets weight
  n / (n + k), so one ad says little and fifteen say a lot. The ad itself is left out of its
  own group (leave-one-out), so it is never compared with itself.
Stage B — the ranked ads, whose text and photos have been read: corrects stage A for what a
  listing doesn't show — engine year relative to the hull, two/four-stroke, unknown engine year.
Adjusted price = asking price - value of included extras (trailer class/age, plotter, ...).
Value = fair / adjusted - 1. Red flags and implausible "deals" (> 2 sd) lower the score.
"""

from __future__ import annotations

import datetime as dt
import math
import re

import numpy as np

from . import market
from .config import in_range, local_ranges

THIS_YEAR = dt.date.today().year
MIN_CLASS = 20  # a boat type needs this many market ads to get its own coefficient
ITEM = "https://www.blocket.se/mobility/item/{id}"


def eligible(ad: dict, cfg: dict) -> bool:
    ext = ad.get("extraction") or {}
    if ad.get("status") != "active" or not ad.get("price") or ad["price"] < cfg["ranking"]["min_price"]:
        return False
    if ext.get("engine_type") == "none":
        return False
    if ext.get("hull") == "open" and ext.get("hull_certain"):
        return False  # certainly an open console boat; uncertain ones stay in, marked
    if cfg["ranking"]["outboard_only"] and ext.get("engine_type") != "outboard":
        return False
    model_listed = any(f != "kategori" for f in ad.get("found_by") or [])
    for field, rng in local_ranges(cfg["search"]["url"]).items():
        if field == "length" and model_listed:
            continue  # a listed model is similar by definition, whatever length the seller typed
        value = {"motor_size": hp_of, "length": length_of}[field](ad)
        if not in_range(value, rng):
            return False  # e.g. Motorstorlek empty but the text says 115 hk
    return True


def length_of(ad: dict) -> float | None:
    """Length in feet; the model number wins when the listed length is implausible ("495 ft", "8 ft").

    Swedish model numbers are usually the length in cm: Sandström 560 -> 5.6 m -> 18.4 ft.
    """
    listed = ad.get("length_ft")
    family, _ = market.model_keys(ad.get("make"), (ad.get("specs") or {}).get("Modell"), ad.get("heading"))
    m = re.search(r"(\d{3})\b", family or "")
    from_model = int(m.group(1)) / 30.48 if m and 350 <= int(m.group(1)) <= 800 else None
    if listed and 8 <= listed <= 60 and (not from_model or abs(listed - from_model) / from_model < 0.25):
        return float(listed)
    return round(from_model, 1) if from_model else (float(listed) if listed and 8 <= listed <= 60 else None)


def hp_of(ad: dict) -> float | None:
    return ad.get("motor_size") or (ad.get("extraction") or {}).get("engine_hp")


def trailer_value(t: dict, prices: dict) -> int:
    kmh = t.get("kmh")
    base = prices.get("trailer_80" if kmh and kmh >= 80 else "trailer_30" if kmh and kmh <= 40 else "trailer", 0)
    if t.get("year"):
        age = max(THIS_YEAR - t["year"], 0)
        base *= max(1 - prices.get("trailer_depreciation_per_year", 0) * age, 0.4)
    return int(base)


def extras_value(ext: dict, prices: dict) -> tuple[int, list[str]]:
    eq = ext.get("equipment") or {}
    items = [k for k, v in eq.items() if v.get("included")]
    total = sum(trailer_value(eq[k], prices) if k == "trailer" else prices.get(k, 0) for k in items)
    return total, items


def _market_row(ad: dict) -> dict:
    """A ranked ad in the same shape as a market row, using what extraction learned."""
    ext = ad.get("extraction") or {}
    family, variant = market.model_keys(ad.get("make"), (ad.get("specs") or {}).get("Modell"), ad.get("heading"))
    return {
        "id": ad["ad_id"], "price": ad.get("price"), "year": ext.get("boat_year"), "motor_size": hp_of(ad),
        "length": length_of(ad), "boat_class": ad.get("boat_class"),
        "motor_type": {"outboard": "Utombordare", "inboard": "Inombordare"}.get(ext.get("engine_type"), ad.get("motor_type")),
        "motor_fuel": ad.get("motor_fuel"), "dealer_segment": ad.get("dealer_segment"), "make": ad.get("make"),
        "family": family, "variant": variant, "heading": ad.get("heading"),
    }


class MarketModel:
    """Stage A: base ridge on listing fields + shrunken make/family/variant effects."""

    LEVELS = ("make", "family", "variant")

    def __init__(self, rows: list[dict], lam: float, prior: float, classes: list[str] | None = None,
                 length_ft: tuple[float, float] | None = None):
        self.prior = prior
        ok = [r for r in rows if r.get("price") and 5000 <= r["price"] <= 5_000_000
              and r.get("year") and 1950 <= r["year"] <= THIS_YEAR + 1 and not r.get("gone")
              and (not classes or r.get("boat_class") in classes)
              and (not length_ft or not r.get("length") or length_ft[0] <= r["length"] <= length_ft[1])]
        self.classes = sorted({c for c in (r.get("boat_class") for r in ok) if c
                               and sum(1 for x in ok if x.get("boat_class") == c) >= MIN_CLASS})
        hps = [r["motor_size"] for r in ok if r.get("motor_size")]
        lens = [r["length"] for r in ok if r.get("length")]
        self.med_hp = float(np.median(hps)) if hps else 30.0
        self.med_len = float(np.median(lens)) if lens else 16.0
        X = self._X(ok)
        y = np.log([r["price"] for r in ok])
        keep = np.ones(len(ok), dtype=bool)
        for _ in range(2):
            self.w, self.mu, self.sd, self.b = _ridge(X[keep], y[keep], lam)
            resid = y - self._base(X)
            keep = np.abs(resid) < 3 * resid[keep].std()
        rows_k = [r for r, k in zip(ok, keep) if k]
        resid = (y - self._base(X))[keep]
        # Sequential shrunken group effects on the residual: make, then family within it, then variant.
        self.stats: dict[str, dict[str, tuple[float, int]]] = {}
        self.fit_resid: dict[int, list[float]] = {r["id"]: [] for r in rows_k}
        for level in self.LEVELS:
            sums: dict[str, list] = {}
            for r, e in zip(rows_k, resid):
                if r.get(level):
                    acc = sums.setdefault(r[level], [0.0, 0])
                    acc[0] += e
                    acc[1] += 1
            self.stats[level] = {k: (v[0], v[1]) for k, v in sums.items()}
            for i, r in enumerate(rows_k):
                self.fit_resid[r["id"]].append(float(resid[i]))
            resid = np.array([e - (self._effect(level, r.get(level)) if r.get(level) else 0.0)
                              for r, e in zip(rows_k, resid)])
        self.n_fit = len(rows_k)
        self.resid_sd = float(resid.std())

    def _X(self, rows: list[dict]) -> np.ndarray:
        out = []
        for r in rows:
            age = min(max(THIS_YEAR - r["year"], 0), 70) if r.get("year") else 25.0
            hp, ln = r.get("motor_size"), r.get("length")
            mt = (r.get("motor_type") or "").lower()
            row = [age, age * age / 70, math.log(max(hp or self.med_hp, 1)), 0.0 if hp else 1.0,
                   float(ln or self.med_len), 0.0 if ln else 1.0,
                   1.0 if "utombord" in mt else 0.0, 1.0 if "inombord" in mt else 0.0,
                   1.0 if (r.get("motor_fuel") or "").lower() == "diesel" else 0.0,
                   0.0 if (r.get("dealer_segment") or "Privat") == "Privat" else 1.0]
            row += [1.0 if r.get("boat_class") == c else 0.0 for c in self.classes]
            out.append(row)
        return np.array(out, dtype=float)

    NAMES = ["båtens ålder", "båtens ålder", "hästkrafter", "hästkrafter", "längd", "längd",
             "motortyp", "motortyp", "diesel", "handlare"]

    def _base(self, X: np.ndarray) -> np.ndarray:
        return ((X - self.mu) / self.sd) @ self.w + self.b

    def _effect(self, level: str, key: str, own: float | None = None) -> float:
        s, n = self.stats[level].get(key, (0.0, 0))
        if own is not None and n:
            s, n = s - own, n - 1  # leave the ad itself out of its own group
        return s / (n + self.prior) if n > 0 else 0.0

    def predict(self, r: dict) -> tuple[float, list[list], dict]:
        """log fair price, breakdown rows [label, pct], group info {level: (key, n, pct)}."""
        X = self._X([r])
        z = ((X - self.mu) / self.sd)[0]
        logp = float(z @ self.w + self.b)
        contrib: dict[str, float] = {}
        names = self.NAMES + ["båttyp"] * len(self.classes)
        for name, c in zip(names, self.w * z):
            contrib[name] = contrib.get(name, 0.0) + float(c)
        own = self.fit_resid.get(r["id"])
        groups = {}
        for i, level in enumerate(self.LEVELS):
            key = r.get(level)
            if not key:
                continue
            eff = self._effect(level, key, own[i] if own else None)
            n = self.stats[level].get(key, (0, 0))[1] - (1 if own else 0)
            logp += eff
            groups[level] = (key, n, round(math.exp(eff) - 1, 3))
        return logp, [[k, round(math.exp(v) - 1, 3)] for k, v in contrib.items()], groups


def _ridge(X: np.ndarray, y: np.ndarray, lam: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1
    Z = (X - mu) / sd
    A = Z.T @ Z + lam * np.eye(Z.shape[1])
    w = np.linalg.solve(A, Z.T @ (y - y.mean()))
    return w, mu, sd, float(y.mean())


def _detail_features(a: dict) -> list[float]:
    """Stage B: what the listing doesn't show but the ad text/photos did."""
    ext = a.get("extraction") or {}
    by = ext.get("boat_year")
    ey = ext.get("engine_year") or ext.get("engine_year_est")
    newer = min(max((ey - by), -15), 30) if (ey and by) else 0.0  # years the engine is newer than the hull
    return [newer, 0.0 if ext.get("engine_year") else 1.0, {4: 1.0, 2: 0.0}.get(ext.get("engine_stroke"), 0.5)]


DETAIL_NAMES = ["motorns ålder", "motorår okänt", "fyrtakt/tvåtakt"]
LEVEL_LABELS = {"make": "märke", "family": "modell", "variant": "variant"}


def comparables(r: dict, rows: list[dict], n: int = 6) -> list[dict]:
    """Nearest-year ads of the same variant (else family), excluding the ad itself."""
    for level in ("variant", "family"):
        key = r.get(level)
        if not key:
            continue
        same = [x for x in rows if x.get(level) == key and x["id"] != r["id"] and not x.get("gone") and x.get("price")]
        if same:
            same.sort(key=lambda x: abs((x.get("year") or 0) - (r.get("year") or 0)))
            return [{"url": ITEM.format(id=x["id"]), "heading": x.get("heading"), "price": x["price"],
                     "year": x.get("year"), "hp": x.get("motor_size")} for x in same[:n]]
    return []


def rank(ads: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    prices = cfg["extras"]
    pool = [a for a in ads if eligible(a, cfg)]
    if len(pool) < 20:
        raise RuntimeError(f"only {len(pool)} eligible ads — fetch and extract first")
    market.learn_from_store()
    rows = market.load()
    for r in rows:  # re-key with the current vocabulary
        r["family"], r["variant"] = market.model_keys(r.get("make"), None, r.get("heading"))
    ours = {a["ad_id"]: _market_row(a) for a in pool}
    by_id = {r["id"]: r for r in rows}
    for i, r in ours.items():  # the ranked ads' own rows use what extraction learned
        if i in by_id:
            by_id[i].update({k: v for k, v in r.items() if v is not None})
        else:
            rows.append(r)
    rc = cfg["ranking"]
    mm = MarketModel(rows, rc["ridge_lambda"], rc.get("model_prior", 4.0), rc.get("market_classes"),
                     tuple(rc["market_length_ft"]) if rc.get("market_length_ft") else None)

    adj, preds = [], []
    for a in pool:
        ev, items = extras_value(a["extraction"], prices)
        # Never let extras eat more than 40% of the price (cheap boat + expensive trailer).
        adj.append((max(a["price"] - ev, a["price"] * 0.6), ev, items))
        preds.append(mm.predict(ours[a["ad_id"]]))

    # Stage B on the ranked ads: residual vs. the market model, explained by text/photo details.
    Xd = np.array([_detail_features(a) for a in pool])
    yd = np.log([p for p, _, _ in adj]) - np.array([p[0] for p in preds])
    keep = np.ones(len(pool), dtype=bool)
    for _ in range(2):
        wd, mud, sdd, bd = _ridge(Xd[keep], yd[keep], cfg["ranking"]["ridge_lambda"])
        resid = yd - (((Xd - mud) / sdd) @ wd + bd)
        keep = np.abs(resid) < 3 * resid[keep].std()
    Zd = (Xd - mud) / sdd
    detail = Zd @ wd + bd
    resid_sd = float((yd - detail)[keep].std())

    out = []
    for a, (adj_price, ev, items), (logp, base_rows, groups), dz, dp, fit in zip(pool, adj, preds, Zd, detail, keep):
        ext = a["extraction"]
        fair = float(math.exp(logp + dp))
        value = fair / adj_price - 1
        flags = list(ext.get("red_flags") or [])
        # Past 2 sd under the market a "deal" is more likely a wreck, a raft or a placeholder price:
        # credibility folds back, so +600% ranks below a believable +80%.
        r_log, lim = math.log(fair / adj_price), 2 * resid_sd
        credible = r_log if r_log <= lim else lim - (r_log - lim)
        score = math.exp(credible) - 1 - cfg["ranking"]["red_flag_penalty"] * len(flags)
        if not ext.get("engine_year"):
            score -= 0.05  # unknown engine age is a risk the price must compensate
        breakdown = [[f"{LEVEL_LABELS[lv]}: {key} ({n} annonser)", pct] for lv, (key, n, pct) in groups.items()]
        breakdown += base_rows + [[nm, round(math.exp(c) - 1, 3)] for nm, c in zip(DETAIL_NAMES, wd * dz)]
        agg: dict[str, float] = {}
        for label, pct in breakdown:
            agg[label] = agg.get(label, 0.0) + math.log1p(pct)
        breakdown = sorted(([k, round(math.exp(v) - 1, 3)] for k, v in agg.items()), key=lambda x: -abs(x[1]))
        mr = ours[a["ad_id"]]
        out.append({
            "ad_id": a["ad_id"],
            "url": a["url"],
            "heading": a["heading"],
            "image": a.get("image"),
            "location": a.get("location"),
            "make": a.get("make"),
            "model_family": mr.get("family"),
            "found_by": a.get("found_by") or ["kategori"],
            "hull": ext.get("hull"),
            "hull_certain": bool(ext.get("hull_certain")),
            "hull_source": ext.get("hull_source"),
            "hull_evidence": ext.get("hull_evidence"),
            "model_variant": mr.get("variant"),
            "boat_class": a.get("boat_class"),
            "price": a["price"],
            "price_history": a.get("price_history", []),
            "first_seen": (a.get("first_seen") or "")[:10],
            "boat_year": ext.get("boat_year"),
            "boat_year_listed": a.get("year"),
            "length_ft": length_of(a),
            "hp": round(hp_of(a), 1) if hp_of(a) else None,
            "hp_source": "annonsfält" if a.get("motor_size") else {"rule:text": "annonstext", "llm": "lokal AI", "bild": "bild"}.get(ext.get("engine_hp_source")),
            "hp_evidence": ext.get("engine_hp_evidence"),
            "engine_type": ext.get("engine_type"),
            "engine_year": ext.get("engine_year"),
            "engine_year_source": ext.get("engine_year_source"),
            "engine_year_evidence": ext.get("engine_year_evidence"),
            "engine_year_est": ext.get("engine_year_est"),
            "engine_year_est_reason": ext.get("engine_year_est_reason"),
            "trailer": {k: v for k, v in ((ext.get("equipment") or {}).get("trailer") or {}).items() if k in ("kmh", "year")},
            "engine_maker": ext.get("engine_maker"),
            "engine_hours": ext.get("engine_hours"),
            "engine_stroke": ext.get("engine_stroke"),
            "extras": items,
            "extras_excluded": [k for k, v in (ext.get("equipment") or {}).items() if not v.get("included")],
            "extras_value": ev,
            "adjusted_price": round(adj_price),
            "fair_price": round(fair),
            "value": round(value, 3),
            "score": round(score, 3),
            "red_flags": flags,
            "swap_offered": bool(ext.get("swap_offered")),
            "outlier": not bool(fit),
            "too_good": bool(r_log > 2.5 * resid_sd),
            "breakdown": breakdown,
            "comparables": comparables(mr, rows),
        })
    out.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(out, 1):
        r["rank"] = i
    model = {
        "n": len(pool),
        "n_fit": int(keep.sum()),
        "market_ads": mm.n_fit,
        "market_models": len(mm.stats["family"]),
        "market_resid_sd_log": round(mm.resid_sd, 3),
        "resid_sd_log": round(resid_sd, 3),
        "detail_coef": {n: round(float(c / s), 4) for n, c, s in zip(DETAIL_NAMES, wd, sdd)},
        "extras_sek": prices,
    }
    return out, model
