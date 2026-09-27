"""Value ranking: compare each asking price to a fair price learned from the market itself.

1. Adjusted price = asking price - estimated value of included extras (trailer, plotter, ...).
2. Fit a ridge regression of log(adjusted price) on boat age, engine age, log(hp), length,
   stroke, boat class and make, over all comparable ads. Outliers (>3 sd) are dropped and the
   model refit, so "1 kr" placeholders and dream prices don't set the market.
3. Value = fair price / adjusted price - 1: +25% means the boat is priced 25% under comparable ads.
4. Red flags (renoveringsobjekt, defekt, läcker, ...) shrink the score; unconfirmed engine years are marked.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np

THIS_YEAR = dt.date.today().year
MIN_GROUP = 12  # a make/class needs this many ads to get its own coefficient


def eligible(ad: dict, cfg: dict) -> bool:
    ext = ad.get("extraction") or {}
    if ad.get("status") != "active" or not ad.get("price") or ad["price"] < cfg["ranking"]["min_price"]:
        return False
    if ext.get("engine_type") == "none":
        return False
    if cfg["ranking"]["outboard_only"] and ext.get("engine_type") != "outboard":
        return False
    return bool(ad.get("motor_size"))


def extras_value(ext: dict, prices: dict) -> tuple[int, list[str]]:
    items = [k for k, v in (ext.get("equipment") or {}).items() if v.get("included")]
    return sum(prices.get(k, 0) for k in items), items


def _features(ads: list[dict]) -> tuple[np.ndarray, list[str]]:
    classes = _frequent([a.get("boat_class") for a in ads])
    makes = _frequent([a.get("make") for a in ads])
    lengths = [a["length_ft"] for a in ads if a.get("length_ft")]
    med_len = float(np.median(lengths)) if lengths else 16.0
    known_ages = [THIS_YEAR - (a["extraction"].get("boat_year")) for a in ads if a["extraction"].get("boat_year")]
    med_age = float(np.median(known_ages)) if known_ages else 25.0
    rows = []
    for a in ads:
        ext = a.get("extraction") or {}
        by = ext.get("boat_year")
        boat_age = min(max(THIS_YEAR - by, 0), 60) if by else med_age
        ey = ext.get("engine_year")
        engine_age = min(max(THIS_YEAR - ey, 0), 60) if ey else boat_age
        stroke = ext.get("engine_stroke")
        row = [
            boat_age,
            engine_age,
            0.0 if ey else 1.0,  # engine year unknown
            0.0 if by else 1.0,  # boat year unknown / placeholder
            math.log(max(a["motor_size"], 1)),
            float(a.get("length_ft") or med_len),
            {4: 1.0, 2: 0.0}.get(stroke, 0.5),
        ]
        row += [1.0 if a.get("boat_class") == c else 0.0 for c in classes]
        row += [1.0 if a.get("make") == m else 0.0 for m in makes]
        rows.append(row)
    names = ["boat_age", "engine_age", "engine_year_unknown", "boat_year_unknown", "log_hp", "length_ft", "four_stroke"]
    names += [f"class={c}" for c in classes] + [f"make={m}" for m in makes]
    return np.array(rows, dtype=float), names


def _frequent(values: list, n: int = MIN_GROUP) -> list:
    counts: dict = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    return sorted(k for k, c in counts.items() if c >= n)


GROUPS = {"boat_age": "båtens ålder", "engine_age": "motorns ålder", "engine_year_unknown": "motorår okänt",
          "boat_year_unknown": "båtår okänt", "log_hp": "hästkrafter", "length_ft": "längd", "four_stroke": "fyrtakt/tvåtakt"}


def _breakdown(names: list[str], contrib: np.ndarray) -> list[list]:
    """Fair-price effect of each feature vs. the average boat in the pool, as [label, pct]."""
    agg: dict[str, float] = {}
    for n, c in zip(names, contrib):
        key = "märke" if n.startswith("make=") else "båttyp" if n.startswith("class=") else GROUPS[n]
        agg[key] = agg.get(key, 0.0) + float(c)
    rows = [[k, round(math.exp(v) - 1, 3)] for k, v in agg.items()]
    return sorted(rows, key=lambda r: -abs(r[1]))


def _ridge(X: np.ndarray, y: np.ndarray, lam: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1
    Z = (X - mu) / sd
    A = Z.T @ Z + lam * np.eye(Z.shape[1])
    w = np.linalg.solve(A, Z.T @ (y - y.mean()))
    return w, mu, sd, float(y.mean())


def rank(ads: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    prices = cfg["extras"]
    pool = [a for a in ads if eligible(a, cfg)]
    if len(pool) < 20:
        raise RuntimeError(f"only {len(pool)} eligible ads — fetch and extract first")

    adj = []
    for a in pool:
        ev, items = extras_value(a["extraction"], prices)
        # Never let extras eat more than 40% of the price (cheap boat + expensive trailer).
        adj.append((max(a["price"] - ev, a["price"] * 0.6), ev, items))
    X, names = _features(pool)
    y = np.log([p for p, _, _ in adj])

    keep = np.ones(len(pool), dtype=bool)
    for _ in range(2):
        w, mu, sd, b = _ridge(X[keep], y[keep], cfg["ranking"]["ridge_lambda"])
        resid = y - (((X - mu) / sd) @ w + b)
        s = resid[keep].std()
        keep = np.abs(resid) < 3 * s

    Z = (X - mu) / sd
    pred = Z @ w + b
    resid_sd = float((y - pred)[keep].std())
    out = []
    for a, (adj_price, ev, items), p, k, z in zip(pool, adj, pred, keep, Z):
        ext = a["extraction"]
        fair = float(math.exp(p))
        value = fair / adj_price - 1
        flags = list(ext.get("red_flags") or [])
        # Past 2.5 sd under the market it's more likely a data error than a deal: cap, don't reward.
        score = min(value, math.exp(2.5 * resid_sd) - 1) - cfg["ranking"]["red_flag_penalty"] * len(flags)
        if not ext.get("engine_year"):
            score -= 0.05  # unknown engine age is a risk the price must compensate
        out.append({
            "ad_id": a["ad_id"],
            "url": a["url"],
            "heading": a["heading"],
            "image": a.get("image"),
            "location": a.get("location"),
            "make": a.get("make"),
            "boat_class": a.get("boat_class"),
            "price": a["price"],
            "price_history": a.get("price_history", []),
            "first_seen": (a.get("first_seen") or "")[:10],
            "boat_year": ext.get("boat_year"),
            "boat_year_listed": a.get("year"),
            "length_ft": a.get("length_ft"),
            "hp": a["motor_size"],
            "engine_type": ext.get("engine_type"),
            "engine_year": ext.get("engine_year"),
            "engine_year_source": ext.get("engine_year_source"),
            "engine_year_evidence": ext.get("engine_year_evidence"),
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
            "outlier": not bool(k),
            "too_good": bool((math.log(fair) - math.log(adj_price)) > 2.5 * resid_sd),
            "breakdown": _breakdown(names, w * z),
        })
    out.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(out, 1):
        r["rank"] = i
    coefs = {n: round(float(c / s), 4) for n, c, s in zip(names, w, sd)}
    model = {
        "n": len(pool),
        "n_fit": int(keep.sum()),
        "resid_sd_log": round(resid_sd, 3),
        "coef_per_unit_log_price": coefs,
        "extras_sek": prices,
    }
    return out, model
