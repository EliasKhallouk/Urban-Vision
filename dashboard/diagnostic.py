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
    """Score de fiabilité = ponctualité ≤ 5 min − 2 × taux d'arrêts sautés, borné 0–100."""
    return float(min(100.0, max(0.0, pct_on_time - 2 * pct_skipped)))


def score_breakdown(pct_on_time: float, pct_skipped: float) -> dict:
    """Répartit les points perdus (100 − score) entre retards et service non rendu.

    `lost_delay` = 100 − ponctualité ; `lost_skip` = 2 × taux d'arrêts sautés.
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

    Passage problématique = passage à plus de 5 min de retard ou arrêt sauté,
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


def half_trend(daily: pd.DataFrame) -> dict | None:
    """Score de la moitié récente de la période comparé à la moitié précédente.

    Même découpage que la page « Évolution » (moitiés égales en jours de
    service). Colonnes attendues : date_service, obs, cnt_le300, skipped, eligible.
    """
    if daily is None or daily.empty:
        return None
    dates = sorted(daily["date_service"].unique())
    if len(dates) < 2:
        return None
    mid = dates[len(dates) // 2]

    def _score(part: pd.DataFrame) -> float | None:
        obs = float(part["obs"].sum())
        if obs <= 0:
            return None
        eligible = float(part["eligible"].sum())
        skip = float(part["skipped"].sum()) / eligible * 100 if eligible else 0.0
        return reliability_score(float(part["cnt_le300"].sum()) / obs * 100, skip)

    recent = _score(daily[daily["date_service"] >= mid])
    previous = _score(daily[daily["date_service"] < mid])
    if recent is None or previous is None:
        return None
    return {"recent": recent, "previous": previous, "delta": recent - previous}


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
    """Où se situent les arrêts sautés le long d'une direction ?

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


def stop_summary(responsible: dict | None, direction: str | None, cause: dict,
                 carried_s: float | None, gained_s: float | None, prev_stop: str | None,
                 hotspot: dict | None, rec: dict, conc: dict | None, zone: str | None) -> list[str]:
    """Phrases « En bref » de la fiche arrêt, dans l'ordre de lecture."""
    out = []
    if responsible is not None:
        where = f" ({direction})" if direction else ""
        out.append(f"La ligne {responsible['ligne']}{where} concentre "
                   f"{format_pct(responsible['share'] * 100)} des passages problématiques de l'arrêt "
                   "(retards de plus de 5 min et arrêts sautés).")
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
    for sentence in (_recurrence_sentence(rec), _concentration_sentence(conc)):
        if sentence:
            out.append(sentence)
    if zone:
        out.append(f"Type de retard : {RISK_ZONE_LABELS[zone].lower()}.")
    return out


def stop_hints(cause: dict, hotspot: dict | None, prev_stop: str | None, stop_name: str,
               responsible: dict | None, rec: dict, pct_skipped: float, has_alerts: bool,
               carried_s: float | None = None) -> list[str]:
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
        hints.append("Arrêts sautés fréquents : à signaler à l'exploitant.")
    return hints


def line_summary(ligne: str, breakdown: dict, cancelled: int, origin: dict,
                 imbalance: dict | None, skips: dict, rec: dict, conc: dict | None) -> list[str]:
    """Phrases « En bref » de la fiche ligne, dans l'ordre de lecture."""
    out = []
    if breakdown["dominant"] == "aucun":
        out.append(f"La ligne {ligne} ne perd que {breakdown['lost']:.0f} point(s) de fiabilité.")
    else:
        out.append(f"Sur {breakdown['lost']:.0f} points perdus, {breakdown['lost_delay']:.0f} "
                   f"viennent des retards et {breakdown['lost_skip']:.0f} des arrêts sautés.")
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
    if imbalance:
        out.append(f"Le sens {imbalance['terminus']} concentre {format_pct(imbalance['share'] * 100)} "
                   "des passages à plus de 5 min.")
    if skips["verdict"] == "extrémités":
        out.append("Les arrêts sautés se situent surtout aux extrémités de la ligne "
                   "(prises ou fins de service en cours de ligne).")
    elif skips["verdict"] == "bloc":
        out.append(f"Les arrêts sautés forment un bloc entre {skips['block'][0]} et {skips['block'][1]} "
                   "(déviation probable).")
    elif skips["verdict"] == "dispersé":
        out.append("Les arrêts sautés sont dispersés le long de la ligne.")
    for sentence in (_recurrence_sentence(rec), _concentration_sentence(conc)):
        if sentence:
            out.append(sentence)
    return out


def line_hints(origin: dict, skips: dict, cancelled: int) -> list[str]:
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
        hints.append(f"Arrêts sautés entre {skips['block'][0]} et {skips['block'][1]} : "
                     "à recouper avec les alertes travaux et déviations.")
    return hints
