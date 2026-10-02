"""Diagnostic « d'où vient le problème ? » d'un arrêt et d'une ligne.

Fonctions pures, sans Streamlit ni base de données : elles reçoivent les tables
déjà agrégées par les loaders du dashboard et renvoient des verdicts et des
phrases en français. Les seuils de palier et de zone viennent de
reports/palette.py ; les phrases présentent des indices et des pistes, jamais
une causalité établie.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pandas as pd

_SRC_PALETTE = str(Path(__file__).resolve().parents[1] / "reports")
if _SRC_PALETTE not in sys.path:
    sys.path.insert(0, _SRC_PALETTE)
from palette import NEGATIVE, POSITIVE, RISK_ZONE_LABELS, tier  # noqa: E402

PERIOD_ORDER = ["Matin", "Journée", "Pointe du soir", "Soirée & nuit", "Week-end"]
WEEKDAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

NOTABLE_DELAY_S = 60.0
LOCAL_SHARE = 0.5
UPSTREAM_SHARE = 0.25
MIN_DAY_PASSAGES = 5
MIN_DAYS_FOR_PATTERN = 5
CONCENTRATION_RATIO = 1.5
CONCENTRATION_MIN_PCT = 5.0
DEPART_SHARE = 0.5
HOTSPOT_SHARE = 0.5
HOTSPOT_OF_CARRIED = 0.25
SKIP_MIN_RATE = 0.5
EDGE_SHARE = 0.15
BLOCK_SHARE = 0.6
IMBALANCE_SHARE = 0.65
NO_PROBLEM_LOST_POINTS = 5.0


def format_seconds(value: float | int | None, signed: bool = False) -> str:
    if value is None or pd.isna(value):
        return "—"
    value = int(round(float(value)))
    sign = "+" if signed and value > 0 else "−" if value < 0 else ""
    absolute = abs(value)
    minutes, seconds = divmod(absolute, 60)
    return f"{sign}{minutes} min {seconds:02d} s" if minutes else f"{sign}{seconds} s"


def format_pct(value: float, decimals: int = 0) -> str:
    return f"{value:.{decimals}f}".replace(".", ",") + " %"


def median_from_hists(hists) -> float | None:
    """Médiane exacte (à la seconde) depuis des histogrammes JSON {secondes: effectif}.

    Comporte comme pandas.median() : pour un effectif pair, moyenne des deux
    valeurs centrales.
    """
    total = 0
    counts: Counter = Counter()
    for h in hists:
        if not h:
            continue
        for k, v in h.items():
            counts[int(k)] += v
            total += v
    if total == 0:
        return None
    if total % 2 == 1:
        target = (total + 1) // 2
        cum = 0
        for sec in sorted(counts):
            cum += counts[sec]
            if cum >= target:
                return sec
    lower, upper = total // 2, total // 2 + 1
    cum, found = 0, []
    for sec in sorted(counts):
        cum += counts[sec]
        if len(found) == 0 and cum >= lower:
            found.append(sec)
        if cum >= upper:
            found.append(sec)
            break
    return (found[0] + found[1]) / 2.0


def period_labels(date_service: pd.Series, heure: pd.Series) -> list[str]:
    """Créneau (jour de semaine × tranche horaire) de chaque agrégat horaire.

    Lundi–vendredi : Matin (06–10), Journée (10–16), Pointe du soir (16–20),
    Soirée & nuit (20–06). Samedi et dimanche : un seul créneau Week-end.
    """
    dow = pd.to_datetime(date_service).dt.dayofweek
    labels = []
    for d, h in zip(dow, heure.astype(int)):
        if d >= 5:
            labels.append("Week-end")
        elif h < 6:
            labels.append("Soirée & nuit")
        elif h < 10:
            labels.append("Matin")
        elif h < 16:
            labels.append("Journée")
        elif h < 20:
            labels.append("Pointe du soir")
        else:
            labels.append("Soirée & nuit")
    return labels


def reliability_score(pct_on_time: float, pct_skipped: float) -> float:
    """Score de fiabilité = ponctualité ≤ 5 min − 2 × taux d'arrêts non desservis, borné 0–100."""
    return float(min(100.0, max(0.0, pct_on_time - 2 * pct_skipped)))


def score_breakdown(pct_on_time: float, pct_skipped: float) -> dict:
    """Répartit les points perdus (100 − score) entre retards et service non rendu.

    `lost_delay` = 100 − ponctualité ; `lost_skip` = 2 × taux d'arrêts non desservis.
    `dominant` : « retards », « service » ou « aucun » (moins de
    NO_PROBLEM_LOST_POINTS points perdus).
    """
    lost_delay = max(0.0, 100.0 - float(pct_on_time))
    lost_skip = max(0.0, 2.0 * float(pct_skipped))
    score = reliability_score(pct_on_time, pct_skipped)
    lost = 100.0 - score
    if lost < NO_PROBLEM_LOST_POINTS:
        dominant = "aucun"
    elif lost_delay >= lost_skip:
        dominant = "retards"
    else:
        dominant = "service"
    return {"score": score, "lost": lost, "lost_delay": lost_delay,
            "lost_skip": lost_skip, "dominant": dominant}


def responsible_line(lines: pd.DataFrame) -> dict | None:
    """Ligne qui cumule le plus de passages problématiques à un arrêt.

    Passage problématique = passage à plus de 5 min de retard ou arrêt non desservi,
    compté en nombre absolu (pas en pourcentage). Colonnes attendues :
    route_id, ligne, cnt_gt300, skipped.
    """
    if lines is None or lines.empty:
        return None
    impact = lines["cnt_gt300"].fillna(0) + lines["skipped"].fillna(0)
    total = float(impact.sum())
    if total <= 0:
        return None
    idx = impact.idxmax()
    row = lines.loc[idx]
    return {"route_id": row["route_id"], "ligne": str(row["ligne"]),
            "impact": int(impact.loc[idx]), "share": float(impact.loc[idx]) / total}


def locate_cause(carried_s: float | None, gained_s: float | None, delay_s: float | None) -> dict:
    """Le retard à un arrêt vient-il de l'amont ou du tronçon qui y mène ?

    `carried_s` : retard moyen déjà présent à l'arrêt précédent du même voyage ;
    `gained_s` : retard moyen pris sur le tronçon ; `delay_s` : retard moyen à
    l'arrêt. Verdict « aucun » sous NOTABLE_DELAY_S, sinon « local »
    (≥ LOCAL_SHARE du retard pris sur le tronçon), « amont » (< UPSTREAM_SHARE)
    ou « mixte ».
    """
    if delay_s is None or pd.isna(delay_s) or float(delay_s) < NOTABLE_DELAY_S:
        return {"verdict": "aucun", "share_local": None}
    carried = max(0.0, float(carried_s or 0.0))
    gained = max(0.0, float(gained_s or 0.0))
    if carried + gained <= 0:
        return {"verdict": "aucun", "share_local": None}
    share = gained / (carried + gained)
    if share >= LOCAL_SHARE:
        verdict = "local"
    elif share < UPSTREAM_SHARE:
        verdict = "amont"
    else:
        verdict = "mixte"
    return {"verdict": verdict, "share_local": share}


def upstream_hotspot(profile: pd.DataFrame, stop_id: str) -> dict | None:
    """Tronçon amont où le plus de retard est pris, avant l'arrêt donné.

    `profile` : une ligne et une direction, colonnes stop_id, stop_name,
    prev_stop_name, commune, order, gain_s.
    """
    if profile is None or profile.empty or stop_id not in set(profile["stop_id"]):
        return None
    order = float(profile.loc[profile["stop_id"] == stop_id, "order"].iloc[0])
    upstream = profile[(profile["order"] < order) & (profile["gain_s"] > 0)
                       & profile["prev_stop_name"].notna()]
    if upstream.empty:
        return None
    row = upstream.loc[upstream["gain_s"].idxmax()]
    return {"from": row["prev_stop_name"], "to": row["stop_name"], "stop_id": row["stop_id"],
            "commune": row.get("commune"), "gain_s": float(row["gain_s"])}


def recurrence(daily: pd.DataFrame) -> dict:
    """Le problème est-il ponctuel ou récurrent ?

    Jour dégradé = part des passages > 5 min dans le palier négatif « pourcent »
    (jours d'au moins MIN_DAY_PASSAGES passages). Verdict : « aucun »,
    « ponctuel » (< 25 % des jours), « fréquent » (< 50 %) ou « chronique » ;
    « période courte » sous MIN_DAYS_FOR_PATTERN jours observés.
    Colonnes attendues : date_service, obs, cnt_gt300.
    """
    empty = {"days": 0, "bad_days": 0, "bad_dates": [], "verdict": "aucun"}
    if daily is None or daily.empty:
        return empty
    d = (daily.groupby("date_service", sort=True)
         .agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum")).reset_index())
    d = d[d["obs"] >= MIN_DAY_PASSAGES]
    if d.empty:
        return empty
    pct = d["cnt_gt300"] / d["obs"] * 100
    bad = d[[tier(v, "pourcent") == NEGATIVE for v in pct]]
    days, bad_days = len(d), len(bad)
    ratio = bad_days / days
    if days < MIN_DAYS_FOR_PATTERN:
        verdict = "période courte"
    elif bad_days == 0:
        verdict = "aucun"
    elif ratio < 0.25:
        verdict = "ponctuel"
    elif ratio < 0.5:
        verdict = "fréquent"
    else:
        verdict = "chronique"
    return {"days": days, "bad_days": bad_days,
            "bad_dates": [pd.Timestamp(x) for x in bad["date_service"]], "verdict": verdict}


def period_table(hourly: pd.DataFrame) -> pd.DataFrame:
    """Retards > 5 min par créneau. Colonnes attendues : date_service, heure, obs, cnt_gt300."""
    cols = ["période", "obs", "cnt_gt300", "pct_gt300"]
    if hourly is None or hourly.empty:
        return pd.DataFrame(columns=cols)
    h = hourly.assign(période=period_labels(hourly["date_service"], hourly["heure"]))
    g = h.groupby("période").agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum")).reset_index()
    g = g[g["obs"] > 0]
    g["pct_gt300"] = g["cnt_gt300"] / g["obs"] * 100
    g["_o"] = g["période"].map({p: i for i, p in enumerate(PERIOD_ORDER)})
    return g.sort_values("_o").drop(columns="_o").reset_index(drop=True)[cols]


def weekday_table(daily: pd.DataFrame) -> pd.DataFrame:
    """Retards > 5 min par jour de la semaine. Colonnes attendues : date_service, obs, cnt_gt300."""
    cols = ["jour", "obs", "cnt_gt300", "pct_gt300"]
    if daily is None or daily.empty:
        return pd.DataFrame(columns=cols)
    d = daily.assign(_dow=pd.to_datetime(daily["date_service"]).dt.dayofweek)
    g = d.groupby("_dow").agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum")).reset_index()
    g = g[g["obs"] > 0].sort_values("_dow")
    g["jour"] = [WEEKDAYS[i] for i in g["_dow"]]
    g["pct_gt300"] = g["cnt_gt300"] / g["obs"] * 100
    return g.reset_index(drop=True)[cols]


def concentration(table: pd.DataFrame, label_col: str) -> dict | None:
    """Créneau (ou jour) où le problème se concentre, s'il ressort nettement.

    Retenu si sa part de passages > 5 min atteint CONCENTRATION_RATIO fois celle
    du reste et au moins CONCENTRATION_MIN_PCT.
    """
    if table is None or len(table) < 2:
        return None
    worst = table.loc[table["pct_gt300"].idxmax()]
    rest = table[table[label_col] != worst[label_col]]
    rest_obs = float(rest["obs"].sum())
    if rest_obs <= 0:
        return None
    rest_pct = float(rest["cnt_gt300"].sum()) / rest_obs * 100
    worst_pct = float(worst["pct_gt300"])
    if worst_pct < CONCENTRATION_MIN_PCT or worst_pct < CONCENTRATION_RATIO * rest_pct:
        return None
    return {"label": worst[label_col], "pct": worst_pct, "rest_pct": rest_pct}


def period_score(daily: pd.DataFrame | None) -> float | None:
    """Score de fiabilité d'un ensemble d'agrégats (colonnes obs, cnt_le300, skipped, eligible)."""
    if daily is None or daily.empty:
        return None
    obs = float(daily["obs"].sum())
    if obs <= 0:
        return None
    eligible = float(daily["eligible"].sum())
    skip = float(daily["skipped"].sum()) / eligible * 100 if eligible else 0.0
    return reliability_score(float(daily["cnt_le300"].sum()) / obs * 100, skip)


def score_change(current: pd.DataFrame | None, previous: pd.DataFrame | None) -> dict | None:
    """Score de la période comparé à celui de sa période de comparaison (mois précédent pour un mois)."""
    now, before = period_score(current), period_score(previous)
    if now is None or before is None:
        return None
    return {"current": now, "previous": before, "delta": now - before}


def percentile_rank(value: float, values: pd.Series) -> float | None:
    """Part (en %) des éléments dont le score est inférieur ou égal à `value`."""
    values = pd.Series(values).dropna()
    if values.empty:
        return None
    return float((values <= value).mean() * 100)


def classify_delay_origin(profile: pd.DataFrame) -> dict:
    """D'où vient le retard d'une ligne, sur une direction ?

    `profile` : arrêts ordonnés (colonne order), colonnes stop_name,
    prev_stop_name, commune, delay_s (retard moyen à l'arrêt), gain_s (retard
    moyen pris sur le tronçon qui y mène). Verdict :
    - « aucun » : le retard maximal atteint reste sous NOTABLE_DELAY_S ;
    - « départ » : le retard au premier arrêt atteint DEPART_SHARE du maximum ;
    - « localisé » : les 3 tronçons qui prennent le plus de retard en
      concentrent au moins HOTSPOT_SHARE ;
    - « diffus » : sinon.
    """
    base = {"verdict": "aucun", "first_delay": None, "peak_delay": None,
            "hotspots": [], "hotspot_share": None}
    if profile is None or profile.empty:
        return base
    p = profile.sort_values("order").reset_index(drop=True)
    peak = float(p["delay_s"].max())
    first = float(p["delay_s"].iloc[0])
    positive = p[(p["gain_s"] > 0) & p["prev_stop_name"].notna()]
    top = positive.nlargest(3, "gain_s")
    hotspots = [{"from": r["prev_stop_name"], "to": r["stop_name"], "stop_id": r.get("stop_id"),
                 "commune": r.get("commune"), "gain_s": float(r["gain_s"])} for _, r in top.iterrows()]
    total_gain = float(positive["gain_s"].sum())
    share = float(top["gain_s"].sum()) / total_gain if total_gain > 0 else None
    out = dict(base, first_delay=first, peak_delay=peak, hotspots=hotspots, hotspot_share=share)
    if peak < NOTABLE_DELAY_S:
        return out
    if first >= DEPART_SHARE * peak:
        out["verdict"] = "départ"
    elif share is not None and share >= HOTSPOT_SHARE:
        out["verdict"] = "localisé"
    else:
        out["verdict"] = "diffus"
    return out


def classify_skips(profile: pd.DataFrame) -> dict:
    """Où se situent les arrêts non desservis le long d'une direction ?

    `profile` : arrêts ordonnés (colonne order), colonnes stop_name, eligible,
    skipped. Verdict « aucun » (taux < SKIP_MIN_RATE %), « extrémités »
    (≥ BLOCK_SHARE des sauts sur les EDGE_SHARE premiers ou derniers arrêts),
    « bloc » (≥ BLOCK_SHARE des sauts sur une suite d'arrêts consécutifs à
    taux élevé, hors extrémités) ou « dispersé ».
    """
    p = profile.sort_values("order").reset_index(drop=True) if profile is not None else None
    if p is None or p.empty:
        return {"verdict": "aucun", "rate": 0.0, "block": None}
    total_skipped = float(p["skipped"].sum())
    total_eligible = float(p["eligible"].sum())
    rate = total_skipped / total_eligible * 100 if total_eligible else 0.0
    if total_skipped <= 0 or rate < SKIP_MIN_RATE:
        return {"verdict": "aucun", "rate": rate, "block": None}
    n = len(p)
    edge = max(1, round(n * EDGE_SHARE))
    edge_skips = float(p["skipped"].iloc[:edge].sum() + p["skipped"].iloc[max(edge, n - edge):].sum())
    if edge_skips >= BLOCK_SHARE * total_skipped:
        return {"verdict": "extrémités", "rate": rate, "block": None}
    stop_rate = p["skipped"] / p["eligible"].where(p["eligible"] > 0) * 100
    flagged = (stop_rate >= 2 * rate).fillna(False).tolist()
    best, start = (0.0, None, None), None
    for i, f in enumerate(flagged + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            run = float(p["skipped"].iloc[start:i].sum())
            if i - start >= 2 and run > best[0]:
                best = (run, start, i - 1)
            start = None
    if best[1] is not None and best[0] >= BLOCK_SHARE * total_skipped:
        return {"verdict": "bloc", "rate": rate,
                "block": (p["stop_name"].iloc[best[1]], p["stop_name"].iloc[best[2]])}
    return {"verdict": "dispersé", "rate": rate, "block": None}


def direction_imbalance(directions: pd.DataFrame) -> dict | None:
    """Une direction concentre-t-elle l'essentiel des passages > 5 min ?

    Colonnes attendues : terminus, obs, cnt_gt300 (une ligne par direction).
    Retenu si une direction porte au moins IMBALANCE_SHARE des retards > 5 min.
    """
    if directions is None or len(directions) < 2:
        return None
    total = float(directions["cnt_gt300"].sum())
    if total <= 0:
        return None
    top = directions.loc[directions["cnt_gt300"].idxmax()]
    share = float(top["cnt_gt300"]) / total
    if share < IMBALANCE_SHARE:
        return None
    return {"terminus": top["terminus"], "share": share}


def is_dominant_hotspot(hotspot: dict | None, carried_s: float | None) -> bool:
    """Le tronçon amont le plus pénalisant pèse-t-il au moins HOTSPOT_OF_CARRIED du retard importé ?"""
    if not hotspot or carried_s is None or pd.isna(carried_s) or float(carried_s) <= 0:
        return False
    return hotspot["gain_s"] / float(carried_s) >= HOTSPOT_OF_CARRIED


def _segment_label(hotspot: dict) -> str:
    commune = hotspot.get("commune")
    where = f" ({commune})" if isinstance(commune, str) and commune else ""
    return f"entre {hotspot['from']} et {hotspot['to']}{where}"


def _days(n: int, adjective: str = "") -> str:
    plural = "s" if n > 1 else ""
    return f"{n} jour{plural}" + (f" {adjective}{plural}" if adjective else "")


def _recurrence_sentence(rec: dict) -> str | None:
    if rec["days"] == 0:
        return None
    if rec["verdict"] == "période courte":
        return (f"Période trop courte pour juger de la récurrence ({_days(rec['days'], 'observé')}) : "
                "élargir la période pour savoir si le problème est ponctuel ou installé.")
    if rec["verdict"] == "aucun":
        return f"Aucun jour dégradé sur les {_days(rec['days'], 'observé')}."
    if rec["verdict"] == "ponctuel":
        dates = ", ".join(d.strftime("%d/%m") for d in rec["bad_dates"][:3])
        return f"Problème ponctuel : {_days(rec['bad_days'], 'dégradé')} sur {rec['days']} ({dates})."
    word = "chronique" if rec["verdict"] == "chronique" else "fréquent"
    return f"Problème {word} : {_days(rec['bad_days'], 'dégradé')} sur {rec['days']}."


def _concentration_sentence(conc: dict | None) -> str | None:
    if conc is None:
        return None
    label = conc["label"]
    prefix = "le " if label in WEEKDAYS else "en "
    return (f"Il se concentre {prefix}{label.lower() if label in WEEKDAYS else label} "
            f"({format_pct(conc['pct'])} de passages à plus de 5 min, contre "
            f"{format_pct(conc['rest_pct'])} le reste du temps).")


def _timing_sentences(rec: dict, conc: dict | None, peak: dict | None, peak_line: str | None) -> list[str]:
    out = [x for x in (_recurrence_sentence(rec),) if x]
    if peak and peak.get("verdict") == "récurrent":
        out.append(peak_sentence(peak, peak_line))
    elif conc:
        out.append(_concentration_sentence(conc))
    return out


def _peak_hint(peak: dict | None, where: str) -> list[str]:
    if not peak or peak.get("verdict") != "récurrent":
        return []
    day = WEEKDAYS[peak["weekday"]].lower()
    return [f"Retards qui reviennent chaque {day} {hour_range(peak['hour'])} : chercher une cause régulière "
            f"{where} à ce moment-là (marché, sortie d'école, livraisons, stationnement, horaire trop serré)."]


def stop_summary(responsible: dict | None, direction: str | None, cause: dict,
                 carried_s: float | None, gained_s: float | None, prev_stop: str | None,
                 hotspot: dict | None, rec: dict, conc: dict | None, zone: str | None,
                 peak: dict | None = None, peak_line: str | None = None) -> list[str]:
    """Phrases « En bref » de la fiche arrêt, dans l'ordre de lecture."""
    out = []
    if responsible is not None:
        where = f" ({direction})" if direction else ""
        out.append(f"La ligne {responsible['ligne']}{where} concentre "
                   f"{format_pct(responsible['share'] * 100)} des passages problématiques de l'arrêt "
                   "(retards de plus de 5 min et arrêts non desservis).")
    verdict = cause["verdict"]
    if verdict == "amont":
        text = (f"Le retard est surtout déjà présent en arrivant "
                f"({format_seconds(carried_s, signed=True)} en moyenne)")
        if is_dominant_hotspot(hotspot, carried_s):
            text += f" : il se forme en amont, principalement {_segment_label(hotspot)}."
        else:
            text += " : il s'accumule progressivement le long du parcours en amont, sans tronçon dominant."
        out.append(text)
    elif verdict == "local":
        origin = f" depuis {prev_stop}" if prev_stop else ""
        out.append(f"Le retard naît surtout ici : {format_seconds(gained_s, signed=True)} en moyenne "
                   f"sur le tronçon{origin}.")
    elif verdict == "mixte":
        origin = f" depuis {prev_stop}" if prev_stop else ""
        out.append(f"Le retard vient en partie de l'amont ({format_seconds(carried_s, signed=True)}) "
                   f"et en partie du tronçon{origin} ({format_seconds(gained_s, signed=True)}).")
    out.extend(_timing_sentences(rec, conc, peak, peak_line))
    if zone:
        out.append(f"Type de retard : {RISK_ZONE_LABELS[zone].lower()}.")
    return out


def stop_hints(cause: dict, hotspot: dict | None, prev_stop: str | None, stop_name: str,
               responsible: dict | None, rec: dict, pct_skipped: float, has_alerts: bool,
               carried_s: float | None = None, peak: dict | None = None) -> list[str]:
    """Pistes d'action de la fiche arrêt (indices, pas de conclusion)."""
    hints = []
    if cause["verdict"] in ("local", "mixte") and prev_stop:
        hints.append(f"Tronçon {prev_stop} → {stop_name} : piste d'aménagement de voirie "
                     "(priorité aux feux, voie réservée), de la compétence de la commune ou de "
                     "Bordeaux Métropole.")
    line = f" Voir la fiche de la ligne {responsible['ligne']}." if responsible else ""
    if cause["verdict"] in ("amont", "mixte") and is_dominant_hotspot(hotspot, carried_s):
        hints.append(f"Retard importé : il se traite là où il se forme, {_segment_label(hotspot)}.{line}")
    elif cause["verdict"] == "amont":
        hints.append("Retard importé et accumulé sur tout le parcours amont : il relève du temps de "
                     f"parcours de la ligne plus que d'un aménagement local.{line}")
    if rec["verdict"] == "ponctuel" and has_alerts:
        hints.append("Pics ponctuels : les rapprocher des perturbations signalées ces jours-là "
                     "(travaux, événements).")
    if tier(pct_skipped, "pourcent") != POSITIVE:
        hints.append("Arrêts non desservis fréquents : à signaler à l'exploitant.")
    where = "autour de l'arrêt" if cause["verdict"] in ("local", "mixte") else "sur le parcours en amont"
    hints.extend(_peak_hint(peak, where))
    return hints


def line_summary(ligne: str, breakdown: dict, cancelled: int, origin: dict,
                 imbalance: dict | None, skips: dict, rec: dict, conc: dict | None,
                 peak: dict | None = None, commune_info: dict | None = None) -> list[str]:
    """Phrases « En bref » de la fiche ligne, dans l'ordre de lecture."""
    out = []
    if breakdown["dominant"] == "aucun":
        out.append(f"La ligne {ligne} ne perd que {breakdown['lost']:.0f} point(s) de fiabilité.")
    else:
        out.append(f"Sur {breakdown['lost']:.0f} points perdus, {breakdown['lost_delay']:.0f} "
                   f"viennent des retards et {breakdown['lost_skip']:.0f} des arrêts non desservis.")
    if cancelled:
        out.append(f"{cancelled} course(s) supprimée(s) sur la période ; la plupart n'apparaissent "
                   "plus dans le flux et n'entrent donc pas dans le score.")
    verdict = origin["verdict"]
    if verdict == "départ":
        out.append(f"Le retard est déjà là au départ ({format_seconds(origin['first_delay'], signed=True)} "
                   f"au premier arrêt, pour un maximum de {format_seconds(origin['peak_delay'], signed=True)}).")
    elif verdict == "localisé":
        spots = " ; ".join(_segment_label(h) for h in origin["hotspots"])
        out.append(f"Les retards se forment surtout sur quelques tronçons : {spots}.")
    elif verdict == "diffus":
        out.append("Le retard se forme un peu partout sur le parcours, sans point noir dominant.")
    sentence = commune_sentence(commune_info)
    if sentence:
        out.append(sentence)
    if imbalance:
        out.append(f"Le sens {imbalance['terminus']} concentre {format_pct(imbalance['share'] * 100)} "
                   "des passages à plus de 5 min.")
    if skips["verdict"] == "extrémités":
        out.append("Les arrêts non desservis se situent surtout aux extrémités de la ligne "
                   "(prises ou fins de service en cours de ligne).")
    elif skips["verdict"] == "bloc":
        out.append(f"Les arrêts non desservis forment un bloc entre {skips['block'][0]} et {skips['block'][1]} "
                   "(déviation probable).")
    elif skips["verdict"] == "dispersé":
        out.append("Les arrêts non desservis sont dispersés le long de la ligne.")
    out.extend(_timing_sentences(rec, conc, peak, None))
    return out


def line_hints(origin: dict, skips: dict, cancelled: int, peak: dict | None = None) -> list[str]:
    """Pistes d'action de la fiche ligne, avec l'interlocuteur naturel."""
    hints = []
    verdict = origin["verdict"]
    if verdict == "départ":
        hints.append("Retard dès le terminus : régulation, temps de retournement ou sortie de dépôt, "
                     "qui relèvent de l'exploitant.")
    elif verdict == "localisé":
        for h in origin["hotspots"][:2]:
            commune = h.get("commune") if isinstance(h.get("commune"), str) else None
            who = f"commune de {commune} ou Bordeaux Métropole" if commune else "Bordeaux Métropole"
            hints.append(f"Tronçon {h['from']} → {h['to']} : point noir de circulation ({who}, voirie).")
    elif verdict == "diffus":
        hints.append("Retard réparti sur tout le parcours : temps de parcours prévu à réexaminer "
                     "(grille horaire, Bordeaux Métropole et l'exploitant).")
    if cancelled:
        hints.append("Courses supprimées : moyens d'exploitation (conducteurs, matériel), "
                     "à interroger auprès de l'exploitant.")
    if skips["verdict"] == "bloc":
        hints.append(f"Arrêts non desservis entre {skips['block'][0]} et {skips['block'][1]} : "
                     "à recouper avec les alertes travaux et déviations.")
    hints.extend(_peak_hint(peak, "sur le parcours"))
    return hints


WATCH_DECLINE_POINTS = 5.0
WATCH_SCORE_BELOW = 80.0


def watchlist(progression: pd.DataFrame | None, lines: pd.DataFrame | None,
              stops: pd.DataFrame | None, min_obs: int, limit: int = 3,
              prev_label: str | None = None) -> list[dict]:
    """Points à surveiller en priorité, sans notification : au plus `limit` éléments.

    1. parmi les lignes dont le score baisse d'au moins WATCH_DECLINE_POINTS points
       par rapport à la période de comparaison (`prev_label`), celle dont la baisse
       pèse le plus (baisse × passages de la période) ;
    2. l'arrêt qui cumule le plus de passages > 5 min parmi ceux sous
       WATCH_SCORE_BELOW (avec `n_sens`, un arrêt regroupé sur plusieurs sens est
       cité une fois, avec son sens le moins fiable) ;
    3. la ligne qui cumule le plus de passages > 5 min parmi celles sous
       WATCH_SCORE_BELOW, si elle n'est pas déjà citée.
    """
    items: list[dict] = []
    if progression is not None and not progression.empty:
        declining = progression[progression["delta_score"] <= -WATCH_DECLINE_POINTS]
        if not declining.empty:
            weight = -declining["delta_score"] * declining["observations"]
            worst = declining.loc[weight.idxmax()]
            items.append({
                "kind": "ligne", "id": worst["route_id"], "title": f"Ligne {worst['ligne']}",
                "reason": (f"score en baisse de {abs(float(worst['delta_score'])):.1f} points"
                           f"{' par rapport à ' + prev_label if prev_label else ''} "
                           f"({float(worst['score_fiabilite_prev']):.1f} → {float(worst['score_fiabilite']):.1f})"),
            })
    if stops is not None and not stops.empty:
        s = stops[(stops["observations"] >= min_obs) & (stops["score_fiabilite"] < WATCH_SCORE_BELOW)]
        if not s.empty:
            s = s.assign(late=s["observations"] * s["pct_retard_5min"] / 100).sort_values("late", ascending=False)
            top = s.iloc[0]
            n_dirs = int(top["n_sens"]) if "n_sens" in top and pd.notna(top["n_sens"]) else 1
            direction = top.get("direction") or ""
            score = float(top["score_fiabilite"])
            if n_dirs > 1:
                title = f"{top['stop_name']} ({n_dirs} {'sens' if n_dirs == 2 else 'quais'})"
                tail = f" ; le moins fiable : {direction}, {score:.1f}/100" if direction else f" ; score {score:.1f}/100"
            else:
                title = f"{top['stop_name']} — {direction}" if direction else str(top["stop_name"])
                tail = f" ; score {score:.1f}/100"
            items.append({
                "kind": "arrêt", "id": top["stop_id"], "title": title,
                "reason": f"le plus de passages en retard du territoire : {float(top['late']):.0f} à plus de 5 min{tail}",
            })
    if lines is not None and not lines.empty:
        cited = {i["id"] for i in items if i["kind"] == "ligne"}
        l = lines[(lines["observations"] >= min_obs) & (lines["score_fiabilite"] < WATCH_SCORE_BELOW)
                  & ~lines["route_id"].isin(cited)]
        if not l.empty:
            l = l.assign(late=l["observations"] * l["pct_retard_5min"] / 100).sort_values("late", ascending=False)
            top = l.iloc[0]
            items.append({
                "kind": "ligne", "id": top["route_id"], "title": f"Ligne {top['ligne']}",
                "reason": (f"le plus de passages en retard des lignes : {float(top['late']):.0f} à plus de 5 min ; "
                           f"score {float(top['score_fiabilite']):.1f}/100"),
            })
    return items[:limit]


MIN_CELL_PASSAGES = 3
MIN_SLOT_PASSAGES = 10
MIN_SLOT_DAYS = 3
SLOT_RECURRENT_SHARE = 0.5
MIN_PROFILE_PASSAGES = 2
NOTABLE_EXCESS_S = 60.0
PROPAGATION_SHARE = 0.5
SLOT_HOTSPOT_MIN_S = 30.0


def hour_range(hour: int) -> str:
    end = "minuit" if hour == 23 else f"{hour + 1} h"
    return f"entre {hour} h et {end}"


def slot_label(weekday: int, hour: int) -> str:
    end = "0 h" if hour == 23 else f"{hour + 1} h"
    return f"{WEEKDAYS[weekday]} {hour} h–{end}"


def week_hour_table(hourly: pd.DataFrame) -> pd.DataFrame:
    """Passages et retards > 5 min par jour de la semaine × heure.

    `days` : nombre de dates observées pour ce jour et cette heure ; `bad_days` :
    dates où cette heure a été dégradée (au moins MIN_CELL_PASSAGES passages et
    part > 5 min dans le palier négatif « pourcent »). Colonnes attendues :
    date_service, heure, obs, cnt_gt300.
    """
    cols = ["weekday", "heure", "obs", "cnt_gt300", "pct_gt300", "days", "bad_days"]
    if hourly is None or hourly.empty:
        return pd.DataFrame(columns=cols)
    h = hourly.assign(_date=pd.to_datetime(hourly["date_service"]).dt.normalize())
    per_day = (h.groupby(["_date", "heure"]).agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum"))
               .reset_index())
    per_day = per_day[per_day["obs"] > 0]
    if per_day.empty:
        return pd.DataFrame(columns=cols)
    per_day["weekday"] = per_day["_date"].dt.dayofweek
    per_day["_bad"] = [o >= MIN_CELL_PASSAGES and tier(c / o * 100, "pourcent") == NEGATIVE
                       for o, c in zip(per_day["obs"], per_day["cnt_gt300"])]
    t = (per_day.groupby(["weekday", "heure"])
         .agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum"), days=("_date", "nunique"),
              bad_days=("_bad", "sum"))
         .reset_index())
    t["pct_gt300"] = t["cnt_gt300"] / t["obs"] * 100
    t["heure"] = t["heure"].astype(int)
    t["bad_days"] = t["bad_days"].astype(int)
    return t[cols]


def find_peak(hourly: pd.DataFrame) -> dict:
    """Moment de la semaine (jour × heure) où les retards > 5 min se concentrent.

    Candidat : au moins MIN_SLOT_PASSAGES passages et MIN_CELL_PASSAGES par
    occurrence en moyenne (les heures creuses à un ou deux passages sont
    écartées), part > 5 min dans le palier négatif et au moins
    CONCENTRATION_RATIO fois celle du reste de la semaine. Parmi les candidats,
    celui qui cumule le plus de passages > 5 min l'emporte (le moment qui touche
    le plus d'usagers). Verdict : « récurrent » (dégradé au moins
    SLOT_RECURRENT_SHARE des fois, sur au moins MIN_SLOT_DAYS occurrences),
    « ponctuel », « à confirmer » (moins de MIN_SLOT_DAYS occurrences), « période
    courte » (aucun moment observé MIN_SLOT_DAYS fois) ou « aucun ».
    """
    t = week_hour_table(hourly)
    if t.empty:
        return {"verdict": "aucun"}
    total_obs, total_cnt = float(t["obs"].sum()), float(t["cnt_gt300"].sum())
    rest_obs = total_obs - t["obs"]
    t = t.assign(rest_pct=((total_cnt - t["cnt_gt300"]) / rest_obs.where(rest_obs > 0) * 100).fillna(0.0))
    negative = [tier(v, "pourcent") == NEGATIVE for v in t["pct_gt300"]]
    cand = t[(t["obs"] >= MIN_SLOT_PASSAGES) & (t["obs"] >= MIN_CELL_PASSAGES * t["days"])
             & pd.Series(negative, index=t.index)
             & (t["pct_gt300"] >= CONCENTRATION_RATIO * t["rest_pct"])]
    if cand.empty:
        return {"verdict": "période courte" if int(t["days"].max()) < MIN_SLOT_DAYS else "aucun"}
    recurrent = cand[(cand["days"] >= MIN_SLOT_DAYS) & (cand["bad_days"] / cand["days"] >= SLOT_RECURRENT_SHARE)]
    if not recurrent.empty:
        row = recurrent.sort_values(["cnt_gt300", "bad_days"], ascending=False).iloc[0]
        verdict = "récurrent"
    else:
        row = cand.sort_values(["cnt_gt300", "pct_gt300"], ascending=False).iloc[0]
        verdict = "à confirmer" if int(row["days"]) < MIN_SLOT_DAYS else "ponctuel"
    return {"verdict": verdict, "weekday": int(row["weekday"]), "hour": int(row["heure"]),
            "pct": float(row["pct_gt300"]), "rest_pct": float(row["rest_pct"]), "obs": int(row["obs"]),
            "days": int(row["days"]), "bad_days": int(row["bad_days"])}


def slot_lines(hourly: pd.DataFrame, weekday: int, hour: int) -> pd.DataFrame:
    """Lignes présentes à un moment (jour × heure), de la plus touchée à la moins touchée.

    Colonnes attendues : date_service, route_id, heure, obs, cnt_gt300.
    """
    cols = ["route_id", "obs", "cnt_gt300", "pct_gt300"]
    if hourly is None or hourly.empty:
        return pd.DataFrame(columns=cols)
    dow = pd.to_datetime(hourly["date_service"]).dt.dayofweek
    cell = hourly[(dow == weekday) & (hourly["heure"].astype(int) == hour)]
    g = cell.groupby("route_id").agg(obs=("obs", "sum"), cnt_gt300=("cnt_gt300", "sum")).reset_index()
    g = g[g["obs"] > 0]
    g["pct_gt300"] = g["cnt_gt300"] / g["obs"] * 100
    return g.sort_values(["cnt_gt300", "pct_gt300"], ascending=False).reset_index(drop=True)[cols]


def slot_profile(route_hourly: pd.DataFrame, profile: pd.DataFrame, weekday: int, hour: int) -> pd.DataFrame:
    """Retard moyen de chaque arrêt d'une direction sur un créneau, et d'habitude.

    `route_hourly` : agrégats horaires de la ligne par arrêt (date_service,
    stop_id, heure, obs, sum_delay) ; `profile` : arrêts ordonnés de la direction
    (stop_id, stop_name, order, commune). « D'habitude » = tous les autres jours
    et heures de la période. `excess` = retard sur le créneau − retard habituel.
    """
    base = profile[["stop_id", "stop_name", "order", "commune"]].copy()
    if route_hourly is None or route_hourly.empty:
        return base.assign(slot_delay=float("nan"), usual_delay=float("nan"), excess=float("nan"), slot_obs=0)
    dow = pd.to_datetime(route_hourly["date_service"]).dt.dayofweek
    in_slot = (dow == weekday) & (route_hourly["heure"].astype(int) == hour)

    def _agg(part: pd.DataFrame, prefix: str) -> pd.DataFrame:
        return (part.groupby("stop_id").agg(**{f"{prefix}_obs": ("obs", "sum"),
                                               f"{prefix}_sum": ("sum_delay", "sum")}).reset_index())

    p = base.merge(_agg(route_hourly[in_slot], "slot"), on="stop_id", how="left")
    p = p.merge(_agg(route_hourly[~in_slot], "usual"), on="stop_id", how="left")
    p[["slot_obs", "usual_obs"]] = p[["slot_obs", "usual_obs"]].fillna(0)
    p["slot_delay"] = p["slot_sum"] / p["slot_obs"].where(p["slot_obs"] >= MIN_PROFILE_PASSAGES)
    p["usual_delay"] = p["usual_sum"] / p["usual_obs"].where(p["usual_obs"] >= MIN_PROFILE_PASSAGES)
    p["excess"] = p["slot_delay"] - p["usual_delay"]
    return p.sort_values("order").reset_index(drop=True)[
        ["stop_id", "stop_name", "order", "commune", "slot_obs", "slot_delay", "usual_delay", "excess"]]


def propagation(sp: pd.DataFrame, stop_id: str) -> dict:
    """Où le surcroît de retard d'un créneau apparaît-il, et jusqu'où se prolonge-t-il ?

    Autour de l'arrêt donné, on suit les arrêts voisins (ordre de la ligne) tant
    que leur surcroît reste au moins égal à PROPAGATION_SHARE de celui de
    l'arrêt (et à NOTABLE_EXCESS_S). Verdict « aucun » si le surcroît à l'arrêt
    est inférieur à NOTABLE_EXCESS_S, sinon « prolongé » ou « résorbé ».
    """
    p = sp.dropna(subset=["excess"]).sort_values("order").reset_index(drop=True)
    if p.empty or stop_id not in set(p["stop_id"]):
        return {"verdict": "inconnu"}
    i = int(p.index[p["stop_id"] == stop_id][0])
    e0 = float(p.loc[i, "excess"])
    out = {"slot_delay": float(p.loc[i, "slot_delay"]), "usual_delay": float(p.loc[i, "usual_delay"]),
           "excess": e0}
    if e0 < NOTABLE_EXCESS_S:
        return dict(out, verdict="aucun")
    threshold = max(NOTABLE_EXCESS_S, PROPAGATION_SHARE * e0)
    j = i
    while j > 0 and p.loc[j - 1, "excess"] >= threshold:
        j -= 1
    k = i
    while k < len(p) - 1 and p.loc[k + 1, "excess"] >= threshold:
        k += 1
    return dict(out, verdict="prolongé" if k > i else "résorbé", origin=p.loc[j, "stop_name"], n_up=i - j,
                until=p.loc[k, "stop_name"], n_down=k - i, is_last=i == len(p) - 1)


def slot_hotspot(sp: pd.DataFrame) -> dict | None:
    """Tronçon où le surcroît de retard du créneau augmente le plus (au moins SLOT_HOTSPOT_MIN_S)."""
    p = sp.dropna(subset=["excess"]).sort_values("order").reset_index(drop=True)
    if len(p) < 2:
        return None
    rise = p["excess"].diff()
    idx = rise.idxmax()
    if pd.isna(rise.loc[idx]) or float(rise.loc[idx]) < SLOT_HOTSPOT_MIN_S:
        return None
    return {"from": p.loc[idx - 1, "stop_name"], "to": p.loc[idx, "stop_name"], "stop_id": p.loc[idx, "stop_id"],
            "commune": p.loc[idx, "commune"], "gain_s": float(rise.loc[idx])}


def commune_share(profile: pd.DataFrame, commune: str | None) -> dict | None:
    """Part du retard pris par une ligne sur les arrêts d'une commune (toutes directions).

    Retard pris = somme des retards positifs pris sur les tronçons qui mènent
    aux arrêts (colonne sum_gain de agg_daily_segment).
    """
    if not commune or profile is None or profile.empty:
        return None
    gains = profile["sum_gain"].clip(lower=0)
    total = float(gains.sum())
    inside = profile[profile["commune"] == commune]
    if inside.empty:
        return {"commune": commune, "n_stops": 0, "share": 0.0, "hotspot": None}
    share = float(gains[inside.index].sum()) / total if total > 0 else 0.0
    spots = inside[(inside["gain_s"] > 0) & inside["prev_stop_name"].notna()]
    hotspot = None
    if not spots.empty:
        r = spots.loc[spots["gain_s"].idxmax()]
        hotspot = {"from": r["prev_stop_name"], "to": r["stop_name"], "stop_id": r["stop_id"],
                   "commune": commune, "gain_s": float(r["gain_s"])}
    return {"commune": commune, "n_stops": int(inside["stop_id"].nunique()), "share": share, "hotspot": hotspot}


def peak_sentence(peak: dict, line: str | None = None) -> str | None:
    """Phrase sur le moment de la semaine où les retards se concentrent."""
    verdict = peak.get("verdict")
    if verdict == "aucun":
        return "Aucun moment de la semaine ne se détache nettement."
    if verdict == "période courte":
        return ("Pour repérer un moment qui revient chaque semaine (par exemple tous les vendredis vers 13 h), "
                "élargir la période à au moins trois semaines.")
    if verdict not in ("récurrent", "ponctuel", "à confirmer"):
        return None
    day = WEEKDAYS[peak["weekday"]].lower()
    moment = f"le {day} {hour_range(peak['hour'])}"
    figures = (f"{format_pct(peak['pct'])} de passages à plus de 5 min, contre "
               f"{format_pct(peak['rest_pct'])} le reste du temps")
    if verdict == "récurrent":
        text = (f"Pic récurrent {moment} : {figures} ; {peak['bad_days']} {day}s dégradés sur "
                f"{peak['days']}.")
    elif verdict == "ponctuel":
        text = (f"{moment[0].upper() + moment[1:]} ressort ({figures}), mais seulement "
                f"{_days(peak['bad_days'], 'dégradé').replace('jour', day)} sur {peak['days']} : plutôt un "
                "incident ponctuel.")
    else:
        text = (f"{moment[0].upper() + moment[1:]} ressort ({figures}), mais n'a été observé que "
                f"{peak['days']} fois : à confirmer sur une période plus longue.")
    if line:
        text += f" Ligne la plus touchée à ce moment-là : {line}."
    return text


def propagation_sentence(prop: dict, ligne: str, stop_name: str) -> str | None:
    """Phrase sur la répercussion du créneau le long de la ligne, autour d'un arrêt."""
    verdict = prop.get("verdict")
    if verdict == "inconnu":
        return None
    here = (f"À ce moment-là, la ligne {ligne} passe à {stop_name} avec "
            f"{format_seconds(prop['slot_delay'], signed=True)} de retard, contre "
            f"{format_seconds(prop['usual_delay'], signed=True)} d'habitude")
    if verdict == "aucun":
        return here + " : pas de surcroît notable."
    origin = (" Ce surcroît naît sur le tronçon qui mène à l'arrêt" if prop["n_up"] == 0 else
              f" Ce surcroît est déjà là dès {prop['origin']} ({prop['n_up']} arrêt(s) avant)")
    if prop["is_last"]:
        end = " ; l'arrêt est le dernier observé de la ligne."
    elif verdict == "prolongé":
        end = f" et se prolonge sur les {prop['n_down']} arrêt(s) suivant(s), jusqu'à {prop['until']}."
    else:
        end = " et se résorbe dès l'arrêt suivant."
    return here + "." + origin + end


def commune_sentence(info: dict | None) -> str | None:
    if not info:
        return None
    if info["n_stops"] == 0:
        return f"La ligne ne dessert pas {info['commune']} sur la période."
    text = (f"Sur {info['commune']} ({info['n_stops']} arrêt(s) de la ligne), la ligne prend "
            f"{format_pct(info['share'] * 100)} de son retard")
    if info["hotspot"] and info["share"] >= 0.05:
        h = info["hotspot"]
        text += (f" ; tronçon le plus pénalisant de la commune : {h['from']} → {h['to']} "
                 f"({format_seconds(h['gain_s'], signed=True)} en moyenne).")
    else:
        text += " : l'essentiel se forme ailleurs sur le parcours."
    return text


def slot_hotspot_sentence(hotspot: dict | None) -> str:
    """Phrase sur le tronçon où le retard s'aggrave le plus à un moment donné (`slot_hotspot`)."""
    if not hotspot:
        return "À ce moment-là, aucun tronçon n'aggrave nettement le retard par rapport au reste du temps."
    return (f"À ce moment-là, le retard s'aggrave surtout {_segment_label(hotspot)} : "
            f"{format_seconds(hotspot['gain_s'], signed=True)} de plus que d'habitude sur ce tronçon.")
