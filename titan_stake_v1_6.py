#!/usr/bin/env python3
"""TITAN STAKE v1.4 - couche de decision, strictement en aval de TITAN PLAT.

POURQUOI UN FICHIER SEPARE
Le referentiel TITAN PLAT interdit toute sortie de pari dans le moteur sportif,
et cette separation est correcte : si la couche de mise pouvait toucher la
couche sportive, l'analyse serait contaminee par le prix. Ce module consomme un
REPORT.json SCELLE et ne peut rien y modifier.

CE QUE CE MODULE FAIT
1. Convertit la distribution jointe de rangs simulee en probabilites d'evenements
   de pari exacts (gagnant, place, jumele, trio), sans approximation de Harville.
2. Recalibre ces probabilites contre le marche par un melange logit a la Benter.
3. Calcule l'esperance APRES prelevement, pari par pari.
4. Modelise la derive parimutuelle entre snapshots et rapport final observe.
5. Dimensionne par Kelly fractionne multi-issues sous plafonds durs.
6. N'emet un ticket que si TOUTES les conditions arithmetiques sont reunies.

CE QUE CE MODULE NE FAIT PAS
Il ne cree pas d'avantage. Si le systeme n'est pas calibre, il repond NO_BET et
affiche exactement ce qui manque. Ce n'est pas de la prudence decorative : sans
probabilites calibrees, l'esperance d'un pari parimutuel est mecaniquement
negative, du taux de prelevement exact.

TROIS PRINCIPES QUI GOUVERNENT LA CONCEPTION
- Prelevement et casse sont saisis par juridiction et type de pari, depuis une
  attestation explicite. Aucun taux generique n'est presume.
- Le marche post-SPORT-LOCK sert de comparateur et de composante de calibration ;
  il ne peut jamais reecrire l'analyse sportive.
- La derive est apprise depuis des journaux de prix chaines, pas depuis une
  affirmation generale sur la repartition temporelle des enjeux.

Commandes :
  python3 titan_stake_v1_4.py self-test
  python3 titan_stake_v1_4.py template market -o MARKET.json
  python3 titan_stake_v1_4.py template policy -o POLICY.json
  python3 titan_stake_v1_4.py calibrate AUDIT*.json --min-races 200 -o CALIBRATION.json
  python3 titan_stake_v1_4.py evaluate REPORT.json --samples RANKS.npz \\
      --market MARKET.json --policy POLICY.json \\
      [--calibration CALIBRATION.json] --commit COMMIT.json \\
      --ticket-journal TICKETS.jsonl -o TICKET.json
  python3 titan_stake_v1_4.py explain-gate TICKET.json

LES JEUX D'ARGENT PEUVENT ETRE DANGEREUX. Ce module est un calculateur
d'esperance, pas une incitation. Sa reponse normale est NO_BET.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np


STAKE_VERSION = "1.6.1"
CALIBRATION_SCHEMA = "titan-stake-calibration-1.4"
PRICE_LOG_SCHEMA = "titan-stake-pricelog-1.4"
DRIFT_SCHEMA = "titan-stake-drift-1.1"
MARKET_SCHEMA = "titan-stake-market-1.1"
POLICY_SCHEMA = "titan-stake-policy-1.4"
TICKET_SCHEMA = "titan-stake-ticket-1.4"
ACCEPTED_REPORT_SCHEMAS = {"titan-plat-report-11.5", "titan-plat-report-11.6"}
ACCEPTED_AUDIT_SCHEMAS = {"titan-plat-audit-11.5", "titan-plat-audit-11.6"}
ACCEPTED_COMMIT_SCHEMAS = {"titan-plat-commit-11.5", "titan-plat-commit-11.6"}
# Schema courant, utilise pour EMETTRE. Les ensembles ci-dessus servent a LIRE.
# Ne jamais emettre via next(iter(...)) sur un ensemble: l'ordre n'est pas
# garanti et une fixture se mettrait a dependre du hasard du hachage.
AUDIT_SCHEMA_CURRENT = "titan-plat-audit-11.6"
EXPOSURE_SCHEMA = "titan-stake-exposure-1.4"
CLV_SCHEMA = "titan-stake-clv-1.6"
# Repetee dans chaque sortie CLV, parce qu'une mesure qui circule sans sa
# clause de gouvernance finit toujours par etre lue comme une autorisation.
CLV_GOVERNANCE = (
    "la valeur de cloture est un INSTRUMENT DE MESURE et n'ouvre aucune porte: "
    "elle n'entre ni dans le niveau d'echelle, ni dans le plafond de mise, ni "
    "dans la calibration. Le palier 0 reste a 0 EUR quel que soit beta."
)
DRIFT_BUCKETS = [0.0, 5.0, 15.0, 30.0]

# ECHELLE GRADUEE DE MISE.
#
# La v1.1 avait une porte binaire: 200 courses ou rien. C'etait mecaniquement
# correct et operationnellement absurde, parce qu'elle ne donnait rien a faire
# entre la course 1 et la course 200, donc le compteur ne demarrait jamais.
#
# La v1.2 fait varier LE RISQUE avec LA PREUVE, pas la verite avec l'envie. A
# chaque niveau, le systeme produit une decision complete et engagee ; ce qui
# change est le montant. Le niveau 0 mise zero euro mais journalise un pari
# theorique, ce qui fait courir le compteur des la premiere course.
#
# Les seuils sont declares et provisoires. Ils ne sont pas calibres : ils
# encodent une regle de prudence, pas une estimation.
STAKING_LADDER = [
    {"level": 0, "mode": "PAPIER", "min_races": 0, "requires_positive_edge": False,
     "max_stake_pct_per_bet": 0.0, "min_edge_simple": 0.05, "min_edge_exotic": 0.25,
     "pools": ["SIMPLE_GAGNANT", "SIMPLE_PLACE"],
     "unlock": "journaliser 50 courses auditees avec engagement publie avant le depart"},
    {"level": 1, "mode": "PAPIER_SUIVI", "min_races": 50, "requires_positive_edge": False,
     "max_stake_pct_per_bet": 0.0, "min_edge_simple": 0.05, "min_edge_exotic": 0.25,
     "pools": ["SIMPLE_GAGNANT", "SIMPLE_PLACE"],
     "unlock": "atteindre 200 courses avec train >=100, holdout >=50 et IC 95 % positif"},
    {"level": 2, "mode": "MICRO", "min_races": 200, "requires_positive_edge": True,
     "max_stake_pct_per_bet": 0.0025, "min_edge_simple": 0.12, "min_edge_exotic": 0.40,
     "pools": ["SIMPLE_GAGNANT", "SIMPLE_PLACE"],
     "unlock": "atteindre 500 courses en conservant l'avantage hors echantillon"},
    {"level": 3, "mode": "REDUIT", "min_races": 500, "requires_positive_edge": True,
     "max_stake_pct_per_bet": 0.0075, "min_edge_simple": 0.08, "min_edge_exotic": 0.30,
     "pools": ["SIMPLE_GAGNANT", "SIMPLE_PLACE"],
     "unlock": "atteindre 1000 courses et deux periodes holdout positives"},
    {"level": 4, "mode": "NOMINAL", "min_races": 1000, "requires_positive_edge": True,
     "max_stake_pct_per_bet": 0.02, "min_edge_simple": 0.05, "min_edge_exotic": 0.25,
     "pools": ["SIMPLE_GAGNANT", "SIMPLE_PLACE", "JUMELE_GAGNANT", "JUMELE_PLACE", "TRIO"],
     "unlock": "regime nominal atteint; revoir la calibration tous les 100 tirages"},
]


def ladder_level(n_races: int, positive_edge: bool) -> dict[str, Any]:
    """Niveau atteint. Le risque suit la preuve, jamais l'inverse."""
    chosen = STAKING_LADDER[0]
    for step in STAKING_LADDER:
        if n_races >= step["min_races"] and (positive_edge or not step["requires_positive_edge"]):
            chosen = step
    return chosen

# Prelevements DECLARES, a re-verifier dans les conditions de l'operateur.
# Sources publiques 2024-2026 : TRJ Simple ~84,4 %, Jumele/Couple ~78 %,
# Trio ~63,5 %, Quinte ~64,75 %, Multi ~65 %. Le module refuse de calculer si
# l'utilisateur n'a pas atteste ces valeurs pour son operateur.
DEFAULT_TAKEOUT = {
    "SIMPLE_GAGNANT": 0.1560,
    "SIMPLE_PLACE": 0.1560,
    "JUMELE_GAGNANT": 0.2200,
    "JUMELE_PLACE": 0.2200,
    "TRIO": 0.3650,
    "TRIO_ORDRE": 0.3650,
    "ZE4": 0.3500,
    "MULTI": 0.3500,
    "QUINTE": 0.3525,
}
SIMPLE_POOLS = {"SIMPLE_GAGNANT", "SIMPLE_PLACE"}
EXOTIC_POOLS = {"JUMELE_GAGNANT", "JUMELE_PLACE", "TRIO", "TRIO_ORDRE", "ZE4", "MULTI", "QUINTE"}


class StakeError(ValueError):
    """Erreur bloquante de donnee, de scellement ou de politique."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_obj(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise StakeError(f"Horodatage ISO requis: {where}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StakeError(f"Horodatage ISO invalide: {where}") from exc
    if parsed.tzinfo is None:
        raise StakeError(f"Fuseau obligatoire: {where}")
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise StakeError(f"Champ obligatoire absent: {where}.{key}")
    return mapping[key]


def as_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StakeError(f"Nombre attendu: {where}")
    result = float(value)
    if not math.isfinite(result):
        raise StakeError(f"Nombre non fini interdit: {where}")
    return result


def verify_sealed_hash(payload: dict[str, Any], field: str, label: str) -> None:
    stored = payload.get(field)
    if not isinstance(stored, str) or len(stored) != 64:
        raise StakeError(f"{label} sans empreinte SHA-256 valide")
    candidate = dict(payload)
    candidate.pop(field, None)
    if sha256_obj(candidate) != stored:
        raise StakeError(f"{label} modifie apres scellement")


# ---------------------------------------------------------------------------
# 1. DE-VIG DE SHIN (identique au moteur sportif, reimplante pour independance)
# ---------------------------------------------------------------------------

def shin_probabilities(odds: dict[int, float]) -> tuple[dict[int, float], float]:
    keys = list(odds)
    pi = np.asarray([1.0 / float(odds[k]) for k in keys], dtype=float)
    booksum = float(pi.sum())
    if booksum <= 1.0 + 1e-12:
        return {k: float(v / booksum) for k, v in zip(keys, pi)}, 0.0

    def total(z: float) -> float:
        inner = z * z + 4.0 * (1.0 - z) * pi * pi / booksum
        return float(np.sum((np.sqrt(inner) - z) / (2.0 * (1.0 - z))))

    lo, hi = 0.0, 0.9999
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if total(mid) > 1.0:
            lo = mid
        else:
            hi = mid
    z = 0.5 * (lo + hi)
    inner = z * z + 4.0 * (1.0 - z) * pi * pi / booksum
    values = (np.sqrt(inner) - z) / (2.0 * (1.0 - z))
    values = values / values.sum()
    return {k: float(v) for k, v in zip(keys, values)}, float(z)


# ---------------------------------------------------------------------------
# 2. MELANGE LOGIT MODELE x MARCHE (Benter 1994)
# ---------------------------------------------------------------------------

def logit_blend(
    p_model: np.ndarray, p_market: np.ndarray, a: float, b: float,
    drift: np.ndarray | None = None, c: float = 0.0,
) -> np.ndarray:
    """p ∝ p_modele^a * p_marche^b * exp(-c * derive), renormalise dans la course.

    Deux resultats distincts sont combines ici.

    1. Benter : la rentabilite vient du modele COMBINE au marche, pas du modele
       fondamental seul. a = 0 signifie que le travail sportif n'apporte rien
       au-dela du prix public.

    2. Dynamique de fin de marche : les rendements esperes dependent non
       seulement des cotes finales mais du CHEMIN par lequel elles sont
       atteintes. Un cheval dont la cote raccourcit dans les dernieres minutes
       rapporte davantage qu'un cheval de cote finale comparable.

    derive_i = log(cote_tardive_i) - log(cote_precoce_i). Elle est NEGATIVE
    quand le cheval est soutenu, d'ou le signe moins : un c positif remonte la
    probabilite des chevaux que la monnaie tardive a soutenus.

    a, b et c sont ajustes par maximum de vraisemblance, jamais choisis. c reste
    a zero tant qu'aucun journal de prix n'a permis de l'ajuster.
    """
    floor = 1e-9
    log_score = a * np.log(np.maximum(p_model, floor)) + b * np.log(np.maximum(p_market, floor))
    if drift is not None and c != 0.0:
        log_score = log_score - c * drift
    log_score -= log_score.max()
    weights = np.exp(log_score)
    return weights / weights.sum()


def race_log_loss(races: list[dict[str, Any]], a: float, b: float, c: float) -> float:
    total = 0.0
    for race in races:
        blended = logit_blend(race["p_model"], race["p_market"], a, b, race.get("drift"), c)
        total -= math.log(max(float(blended[race["winner_index"]]), 1e-12))
    return total / len(races)


def fit_blend(races: list[dict[str, Any]], grid: int = 41, fit_drift: bool = False) -> dict[str, Any]:
    """Ajuste (a, b, c) par maximum de vraisemblance sur le gagnant reel.

    Grille grossiere puis raffinement local : robuste, sans dependance externe,
    et suffisant pour trois parametres sur quelques centaines de courses.
    c n'est ajuste que si un journal de prix fournit une derive par cheval.
    """
    if not races:
        raise StakeError("Aucune course exploitable pour la calibration")
    c_values = np.linspace(0.0, 2.0, 21) if fit_drift else np.asarray([0.0])
    best = (0.0, 1.0, 0.0, race_log_loss(races, 0.0, 1.0, 0.0))
    for a in np.linspace(0.0, 2.0, grid):
        for b in np.linspace(0.0, 2.0, grid):
            for c in c_values:
                value = race_log_loss(races, float(a), float(b), float(c))
                if value < best[3]:
                    best = (float(a), float(b), float(c), value)
    step = 2.0 / (grid - 1)
    for _ in range(3):
        step /= 4.0
        centre_a, centre_b, centre_c, centre_v = best
        for a in (centre_a - step, centre_a, centre_a + step):
            for b in (centre_b - step, centre_b, centre_b + step):
                for c in ((centre_c - step, centre_c, centre_c + step) if fit_drift else (0.0,)):
                    if a < 0 or b < 0 or c < 0:
                        continue
                    value = race_log_loss(races, float(a), float(b), float(c))
                    if value < centre_v - 1e-12:
                        best = (float(a), float(b), float(c), value)
                        centre_v = value
    a, b, c, loss = best
    return {
        "a_model": round(a, 5),
        "b_market": round(b, 5),
        "c_drift": round(c, 5),
        "drift_fitted": bool(fit_drift),
        "log_loss_blend": round(loss, 6),
        "log_loss_model_only": round(race_log_loss(races, 1.0, 0.0, 0.0), 6),
        "log_loss_market_only": round(race_log_loss(races, 0.0, 1.0, 0.0), 6),
    }


def fit_market_only(races: list[dict[str, Any]], grid: int = 81) -> dict[str, Any]:
    """Meilleur modele n'utilisant QUE le marche, avec temperature libre.

    C'est la vraie reference a battre. Comparer le melange au marche brut est
    trompeur: reajuster une simple temperature sur le prix public corrige deja
    une partie du biais favori-outsider et ameliore la log-loss sans qu'aucune
    information sportive n'ait ete apportee.
    """
    def loss(b: float) -> float:
        return race_log_loss(races, 0.0, b, 0.0)

    best_b, best_loss = 1.0, loss(1.0)
    for b in np.linspace(0.05, 2.0, grid):
        value = loss(float(b))
        if value < best_loss:
            best_b, best_loss = float(b), value
    return {"b_market_only": round(best_b, 5), "log_loss_market_only_tuned": round(best_loss, 6)}


def bootstrap_improvement(
    races: list[dict[str, Any]], a: float, b: float, b_market_only: float,
    draws: int, seed: int, c: float = 0.0,
) -> dict[str, Any]:
    """Le modele ajoute-t-il de l'information a un marche DEJA recalibre ?

    On compare le melange (a, b) au meilleur modele marche-seul (0, b*), course
    par course, puis on reechantillonne LES COURSES - jamais les chevaux, dont
    les lignes ne sont pas independantes au sein d'une course.

    C'est la mesure d'amelioration au sens de Benter : ce qui compte n'est pas
    que le modele fondamental soit bon, mais qu'il apporte quelque chose que le
    prix public ne contient pas deja.
    """
    per_race = []
    cluster_keys = []
    for index, race in enumerate(races):
        blend = logit_blend(race["p_model"], race["p_market"], a, b, race.get("drift"), c)
        reference = logit_blend(race["p_model"], race["p_market"], 0.0, b_market_only)
        idx = race["winner_index"]
        per_race.append(
            math.log(max(float(blend[idx]), 1e-12)) - math.log(max(float(reference[idx]), 1e-12))
        )
        cluster_keys.append(str(race.get("day", race.get("race_key", index))))
    values = np.asarray(per_race, dtype=float)
    clusters: dict[str, list[int]] = {}
    for index, key in enumerate(cluster_keys):
        clusters.setdefault(key, []).append(index)
    keys = sorted(clusters)
    rng = np.random.default_rng(seed)
    samples = np.empty(draws, dtype=float)
    for draw in range(draws):
        chosen = rng.choice(keys, size=len(keys), replace=True)
        indices = [idx for key in chosen for idx in clusters[str(key)]]
        samples[draw] = float(np.mean(values[indices]))
    return {
        "baseline": "marche seul avec temperature ajustee",
        "evaluation": "HOLDOUT_TEMPOREL_PARAMETRES_GELES",
        "n_holdout_races": len(values),
        "n_day_clusters": len(keys),
        "bootstrap_unit": "journee_de_course",
        "mean_improvement_nats": round(float(values.mean()), 6),
        "ci95_low": round(float(np.quantile(samples, 0.025)), 6),
        "ci95_high": round(float(np.quantile(samples, 0.975)), 6),
        "adds_information_at_95": bool(np.quantile(samples, 0.025) > 0.0),
        "reading": (
            "positif = le travail sportif apporte de l'information absente du prix; "
            "l'intervalle doit exclure zero par le bas"
        ),
    }


def drift_by_race(
    price_log: list[dict[str, Any]],
    starts: dict[str, datetime] | None = None,
) -> dict[str, dict[str, float]]:
    """Derive log(cote tardive / cote precoce) par cheval, depuis le journal de prix."""
    snapshots: dict[str, list[dict[str, Any]]] = {}
    verify_price_journal(price_log)
    for index, record in enumerate(price_log):
        key = str(record["race_key"])
        if starts is not None and key in starts:
            observed = parse_iso(record["observed_at"], f"price_log[{index}].observed_at")
            start = starts[key]
            if record["kind"] == "snapshot":
                if observed >= start:
                    raise StakeError(f"Snapshot prix post-depart: {key}")
                calculated = (start - observed).total_seconds() / 60.0
                if abs(calculated - float(record["minutes_to_post"])) > 1.0:
                    raise StakeError(f"minutes_to_post incoherent avec observed_at: {key}")
            elif observed < start:
                raise StakeError(f"Rapport final date avant le depart: {key}")
        if record.get("kind") != "snapshot":
            continue
        snapshots.setdefault(key, []).append(record)
    result: dict[str, dict[str, float]] = {}
    for race_key, items in snapshots.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda r: -float(r["minutes_to_post"]))
        early, late = items[0], items[-1]
        shared = set(early["rapports"]) & set(late["rapports"])
        if not shared:
            continue
        result[race_key] = {
            key: math.log(float(late["rapports"][key]) / float(early["rapports"][key]))
            for key in shared
            if float(late["rapports"][key]) > 0 and float(early["rapports"][key]) > 0
        }
    return result


def build_calibration(
    audits: list[dict[str, Any]], *, min_races: int = 200, bootstrap_draws: int = 4000,
    price_log: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if isinstance(min_races, bool) or not isinstance(min_races, int) or min_races < 200:
        raise StakeError("min_races doit etre un entier >= 200")
    if (
        isinstance(bootstrap_draws, bool)
        or not isinstance(bootstrap_draws, int)
        or bootstrap_draws < 1000
    ):
        raise StakeError("bootstrap_draws doit etre un entier >= 1000")
    races: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    seen: set[str] = set()

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for audit in audits:
        if audit.get("schema_version") not in ACCEPTED_AUDIT_SCHEMAS:
            skip("schema_audit_incompatible")
            continue
        verify_sealed_hash(audit, "audit_sha256", "AUDIT")
        race_key = str(audit.get("race_key"))
        if race_key in seen:
            raise StakeError(f"Course dupliquee dans la calibration: {race_key}")
        seen.add(race_key)
        if audit.get("fill_mode", "FULL") != "FULL":
            skip("dossier_lite_exclu_de_la_calibration_predictive")
            continue
        market = audit.get("market_comparison")
        internal = audit.get("internal_model_probabilities")
        if market is None or internal is None:
            skip("marche_ou_probabilites_modele_absents")
            continue
        commitment = audit.get("prior_commitment")
        if commitment is None:
            skip("aucun_engagement_publie_avant_le_depart")
            continue
        # v1.5 : un engagement existe-t-il n'est plus la bonne question. La
        # question est: un TIERS peut-il constater qu'il existait avant le
        # depart, et le dossier sportif etait-il etanche au marche ? Un audit
        # anterieur a 11.6 ne porte pas cette information: il est donc traite
        # comme exploratoire, jamais comme preuve. Gonfler le denominateur des
        # 200 avec des courses invalidables reviendrait a s'auto-certifier.
        status = commitment.get("evidential_status")
        if status != "PROBANTE":
            skip(
                "course_exploratoire_non_opposable"
                if status is not None else
                "audit_sans_statut_probant_schema_anterieur_a_11_6"
            )
            continue
        numbers = sorted(int(k) for k in internal)
        p_model = np.asarray([float(internal[str(n)]) for n in numbers], dtype=float)
        consensus = market["internal_consensus"]["values"]
        if set(consensus) != {str(n) for n in numbers}:
            skip("couverture_marche_incomplete")
            continue
        p_market = np.asarray([float(consensus[str(n)]) for n in numbers], dtype=float)
        if (
            not np.all(np.isfinite(p_model))
            or not np.all(np.isfinite(p_market))
            or np.any(p_model <= 0)
            or np.any(p_market <= 0)
            or p_model.sum() <= 0
            or p_market.sum() <= 0
        ):
            skip("probabilites_degenerees")
            continue
        winner = int(audit["winner"])
        if winner not in numbers:
            skip("gagnant_hors_partants")
            continue
        scheduled = parse_iso(
            require(audit, "scheduled_start_utc", "audit"), "audit.scheduled_start_utc"
        )
        entry = {
            "race_key": race_key,
            "event_time": scheduled,
            "day": scheduled.date().isoformat(),
            "p_model": p_model / p_model.sum(),
            "p_market": p_market / p_market.sum(),
            "winner_index": numbers.index(winner),
        }
        entry["numbers"] = numbers
        races.append(entry)
    races.sort(key=lambda item: (item["event_time"], item["race_key"]))
    if price_log:
        starts = {race["race_key"]: race["event_time"] for race in races}
        drift_table = drift_by_race(price_log, starts=starts)
        for entry in races:
            drift_row = drift_table.get(entry["race_key"])
            if drift_row is not None and all(
                str(number) in drift_row for number in entry["numbers"]
            ):
                entry["drift"] = np.asarray(
                    [drift_row[str(number)] for number in entry["numbers"]],
                    dtype=float,
                )
    n = len(races)
    if n == 0:
        raise StakeError(f"Aucune course exploitable. Motifs: {skipped}")
    split_index = max(1, min(n - 1, int(math.floor(0.70 * n)))) if n > 1 else 1
    train = races[:split_index]
    holdout = races[split_index:]
    n_with_drift = sum(1 for race in train if "drift" in race)
    # La derive n'est ajustee que si TOUTES les courses en disposent: melanger des
    # courses avec et sans derive biaiserait l'estimation de c.
    can_fit_drift = n_with_drift == len(train) and n_with_drift > 0
    fit = fit_blend(train, fit_drift=can_fit_drift)
    reference = fit_market_only(train)
    fit.update(reference)
    if holdout:
        holdout_blend_loss = race_log_loss(
            holdout, fit["a_model"], fit["b_market"], fit["c_drift"]
        )
        holdout_market_loss = race_log_loss(
            holdout, 0.0, reference["b_market_only"], 0.0
        )
        boot = bootstrap_improvement(
            holdout, fit["a_model"], fit["b_market"], reference["b_market_only"],
            bootstrap_draws, seed=20260830, c=fit["c_drift"],
        )
    else:
        holdout_blend_loss = float("nan")
        holdout_market_loss = float("nan")
        boot = {
            "baseline": "marche seul avec temperature ajustee",
            "evaluation": "HOLDOUT_ABSENT",
            "n_holdout_races": 0,
            "n_day_clusters": 0,
            "mean_improvement_nats": 0.0,
            "ci95_low": 0.0,
            "ci95_high": 0.0,
            "adds_information_at_95": False,
            "reading": "aucun lot test disponible",
        }
    holdout_days = len({race["day"] for race in holdout})
    usable = (
        n >= min_races
        and len(train) >= 100
        and len(holdout) >= 50
        and holdout_days >= 20
        and boot["adds_information_at_95"]
        and fit["a_model"] > 0.05
    )
    calibration = {
        "schema_version": CALIBRATION_SCHEMA,
        "stake_version": STAKE_VERSION,
        "n_races": n,
        "n_train_races": len(train),
        "n_holdout_races": len(holdout),
        "n_holdout_day_clusters": holdout_days,
        "n_train_races_with_drift": n_with_drift,
        "min_races_required": min_races,
        "split": {
            "method": "chronologique_70_30",
            "train_end_utc": iso_utc(train[-1]["event_time"]) if train else None,
            "holdout_start_utc": iso_utc(holdout[0]["event_time"]) if holdout else None,
            "train_race_keys_sha256": sha256_obj([race["race_key"] for race in train]),
            "holdout_race_keys_sha256": sha256_obj([race["race_key"] for race in holdout]),
        },
        "skipped": skipped,
        "blend": fit,
        "holdout": {
            "log_loss_blend": (
                None if math.isnan(holdout_blend_loss) else round(holdout_blend_loss, 6)
            ),
            "log_loss_market_only_tuned_on_train": (
                None if math.isnan(holdout_market_loss) else round(holdout_market_loss, 6)
            ),
        },
        "bootstrap": boot,
        "usable_for_staking": bool(usable),
        "blocking_reasons": [
            reason for reason, failed in (
                (f"echantillon insuffisant ({n} < {min_races})", n < min_races),
                (f"train insuffisant ({len(train)} < 100)", len(train) < 100),
                (f"holdout insuffisant ({len(holdout)} < 50)", len(holdout) < 50),
                (f"holdout trop concentre ({holdout_days} < 20 journees)", holdout_days < 20),
                ("le modele n'ajoute rien a un marche recalibre (IC 95 % incluant zero)",
             not boot["adds_information_at_95"]),
                ("poids du modele nul: il n'ajoute rien au marche", fit["a_model"] <= 0.05),
            ) if failed
        ],
        "warning": (
            "les coefficients sont ajustes uniquement sur le train et leur avantage "
            "est teste uniquement sur le holdout chronologique; a_model proche de zero "
            "signifie que le sport n'ajoute rien au prix public"
        ),
        "drift_note": (
            "c_drift reste a zero tant que toutes les courses du lot ne portent pas "
            "deux snapshots de prix horodates; journalisez systematiquement"
        ),
    }
    calibration["calibration_sha256"] = sha256_obj(calibration)
    return calibration


# ---------------------------------------------------------------------------
# 3. PROBABILITES D'EVENEMENTS EXACTES A PARTIR DE LA DISTRIBUTION JOINTE
# ---------------------------------------------------------------------------

def load_samples(path: str | os.PathLike[str], report: dict[str, Any]) -> tuple[np.ndarray, list[int]]:
    data = np.load(path, allow_pickle=False)
    if str(data["schema_version"]) != "titan-plat-rank-samples-11.1":
        raise StakeError("Fichier de tirages incompatible avec ce module")
    if str(data["rank_samples_sha256"]) != report["rank_samples_sha256"]:
        raise StakeError("RANKS.npz ne correspond pas au REPORT scelle")
    ranks = np.asarray(data["ranks"], dtype=np.int16)
    numbers = [int(x) for x in np.asarray(data["numbers"])]
    if sorted(numbers) != sorted(report["render"]["verdict"]["strict_order"]):
        raise StakeError("Partants du fichier de tirages incoherents avec le rapport")
    return ranks, numbers


def importance_weights(
    ranks: np.ndarray, numbers: list[int], p_target: np.ndarray
) -> np.ndarray:
    """Repondere les tirages pour que la marginale de VICTOIRE egale p_target.

    Chaque tirage recoit le poids p_cible[gagnant] / p_brute[gagnant]. Toute
    probabilite jointe calculee ensuite (jumele, trio) herite automatiquement de
    la marginale calibree, tout en conservant la STRUCTURE CONDITIONNELLE de la
    simulation - qui finit deuxieme sachant le vainqueur.

    Limite assumee et signalee : seule la marginale de victoire est calibree.
    La structure conditionnelle reste non validee, ce qui justifie un seuil
    d'exigence plus severe sur les paris combines.
    """
    winners = np.argmax(ranks == 1, axis=1)
    counts = np.bincount(winners, minlength=len(numbers)).astype(float)
    p_raw = counts / counts.sum()
    safe = np.where(p_raw > 0, p_raw, 1.0)
    ratio = np.where(p_raw > 0, p_target / safe, 0.0)
    weights = ratio[winners]
    total = weights.sum()
    if total <= 0:
        raise StakeError("Reponderation degeneree: aucune masse restante")
    return weights / total


def event_probabilities(
    ranks: np.ndarray, numbers: list[int], weights: np.ndarray, place_slots: int
) -> dict[str, Any]:
    """Probabilites exactes, sans approximation de Harville.

    La plupart des systemes n'ont que les probabilites marginales de victoire et
    doivent reconstruire les combines par le modele de Harville, connu pour etre
    biaise. Ici la distribution jointe est disponible : on compte directement.
    """
    n = len(numbers)
    index = {number: idx for idx, number in enumerate(numbers)}
    win = np.zeros(n)
    place = np.zeros(n)
    for idx in range(n):
        win[idx] = float(weights[ranks[:, idx] == 1].sum())
        place[idx] = float(weights[ranks[:, idx] <= place_slots].sum())
    top2 = ranks <= 2
    top3 = ranks <= 3
    jumele_gagnant: dict[str, float] = {}
    jumele_place: dict[str, float] = {}
    trio: dict[str, float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            key = f"{numbers[i]}-{numbers[j]}"
            jumele_gagnant[key] = float(weights[top2[:, i] & top2[:, j]].sum())
            jumele_place[key] = float(weights[top3[:, i] & top3[:, j]].sum())
            for k in range(j + 1, n):
                trio[f"{numbers[i]}-{numbers[j]}-{numbers[k]}"] = float(
                    weights[top3[:, i] & top3[:, j] & top3[:, k]].sum()
                )
    return {
        "SIMPLE_GAGNANT": {str(numbers[i]): float(win[i]) for i in range(n)},
        "SIMPLE_PLACE": {str(numbers[i]): float(place[i]) for i in range(n)},
        "JUMELE_GAGNANT": jumele_gagnant,
        "JUMELE_PLACE": jumele_place,
        "TRIO": trio,
        "place_slots": place_slots,
        "index": index,
    }


# ---------------------------------------------------------------------------
# 4. DERIVE PARIMUTUELLE : ON MISE A UN RAPPORT QUE L'ON NE VOIT PAS
# ---------------------------------------------------------------------------

def drift_quantile(policy: dict[str, Any], minutes_to_post: float, pool: str) -> float:
    """Facteur multiplicatif PESSIMISTE applique au rapport observe.

    En parimutuel, seule une minorite des enjeux est placee loin du depart : de
    l'ordre du quart a T-20, pres de la moitie dans les cinq dernieres minutes.
    Le rapport lu a T-30 n'est donc PAS celui auquel on est paye, et la monnaie
    tardive est la plus informee : elle fait typiquement RACCOURCIR les chevaux
    reellement soutenus, c'est-a-dire exactement ceux qu'un bon modele veut jouer.

    Ignorer cette derive est la faute la plus courante des systemes amateurs :
    ils calculent une esperance sur un prix qui n'existera jamais.

    Le modele par defaut est volontairement penalisant tant que l'utilisateur
    n'a pas ajuste sa propre distribution de derive sur ses relevés horodates.
    """
    model = policy["drift_model"]
    if model.get("fitted") is True:
        table = model.get("fitted_log_drift_q10", {})
        keys = sorted(float(k) for k in table)
        chosen = None
        for key in keys:
            if minutes_to_post >= key:
                chosen = key
        if chosen is not None:
            return float(math.exp(float(table[str(chosen)])))
        # Horizon non couvert par les releves: on retombe sur la penalite
        # forfaitaire plutot que d'extrapoler une derive jamais observee.
    horizon = max(0.0, float(minutes_to_post))
    base = float(model["unfitted_penalty_per_10min"])
    cap = float(model["unfitted_max_penalty"])
    penalty = min(cap, base * horizon / 10.0)
    if pool in EXOTIC_POOLS:
        penalty = min(0.90, penalty * float(model["exotic_penalty_multiplier"]))
    return 1.0 - penalty


def apply_breakage(rapport: float, granularity: float) -> float:
    """Arrondi du rapport a la baisse, tel que pratique en parimutuel francais.

    Les rapports sont arrondis au dixieme d'euro inferieur. Sur un cheval a 2,39
    on est paye 2,30 : environ 4 % d'esperance qui disparaissent silencieusement.
    Quand le seuil de declenchement est a 5 %, ignorer la rupture suffit a
    transformer un pari perdant en pari apparemment gagnant.
    """
    if granularity <= 0:
        return rapport
    # Calcul en centimes entiers: 2.30 / 0.10 vaut 22,999... en flottant, et un
    # floor naif rendrait 2,20. Une erreur de dix centimes sur chaque rapport
    # fausserait toutes les esperances dans le sens optimiste.
    cents = int(round(rapport * 100.0))
    step = max(1, int(round(granularity * 100.0)))
    return (cents // step) * step / 100.0


# ---------------------------------------------------------------------------
# 5. KELLY FRACTIONNE MULTI-ISSUES
# ---------------------------------------------------------------------------

def multi_outcome_kelly(probabilities: np.ndarray, net_odds: np.ndarray) -> np.ndarray:
    """Kelly exact sur issues mutuellement exclusives (forme fermee).

    Maximise  sum_i p_i * log(1 - S + f_i * R_i)  +  (1 - sum_i p_i) * log(1 - S)
    avec S = sum f_i et R_i = cote decimale. Le dernier terme est la masse ou
    AUCUN cheval mise ne gagne : l'oublier fait exploser les mises.

    Solution de Smoczynski et Tomkins : on trie par p_i * R_i decroissant, on
    cherche le plus grand ensemble de tete verifiant p_t * R_t > b_t avec
        b_t = (1 - somme des p) / (1 - somme des 1/R)
    puis f_i = p_i - b / R_i sur cet ensemble, zero ailleurs.

    Dimensionner chaque pari isolement, comme le font la plupart des
    calculateurs, surmise des que l'on joue plusieurs chevaux d'une meme course :
    les issues sont exclusives, une seule peut payer.
    """
    n = len(probabilities)
    fractions = np.zeros(n, dtype=float)
    if n == 0:
        return fractions
    decimal = net_odds + 1.0
    valid = decimal > 1.0 + 1e-12
    if not np.any(valid):
        return fractions
    order = np.argsort(-(probabilities * decimal))
    cumulative_p = 0.0
    cumulative_inverse = 0.0
    chosen: list[int] = []
    b_star = None
    for idx in order:
        if not valid[idx]:
            continue
        trial_p = cumulative_p + float(probabilities[idx])
        trial_inverse = cumulative_inverse + 1.0 / float(decimal[idx])
        denominator = 1.0 - trial_inverse
        if denominator <= 1e-12:
            break
        b_trial = (1.0 - trial_p) / denominator
        if float(probabilities[idx]) * float(decimal[idx]) <= b_trial:
            break
        chosen.append(int(idx))
        cumulative_p, cumulative_inverse, b_star = trial_p, trial_inverse, b_trial
    if not chosen or b_star is None:
        return fractions
    for idx in chosen:
        fractions[idx] = max(0.0, float(probabilities[idx]) - b_star / float(decimal[idx]))
    total = fractions.sum()
    if total > 0.999:
        fractions *= 0.999 / total
    return fractions


# ---------------------------------------------------------------------------
# 6. EVALUATION D'UNE COURSE
# ---------------------------------------------------------------------------

def validate_policy(policy: dict[str, Any]) -> dict[str, Any]:
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise StakeError(f"schema_version de politique attendu: {POLICY_SCHEMA}")
    if policy.get("takeout_attested") is not True:
        raise StakeError(
            "policy.takeout_attested doit valoir true: verifiez les taux de prelevement "
            "reels de votre operateur avant tout calcul d'esperance"
        )
    bankroll = as_float(require(policy, "bankroll_eur", "policy"), "policy.bankroll_eur")
    if bankroll <= 0:
        raise StakeError("bankroll_eur doit etre strictement positif")
    for key in ("kelly_fraction", "max_stake_pct_per_bet", "max_stake_pct_per_race"):
        value = as_float(require(policy, key, "policy"), f"policy.{key}")
        if not 0.0 < value <= 1.0:
            raise StakeError(f"policy.{key} doit etre dans ]0,1]")
    if policy["kelly_fraction"] > 0.50:
        raise StakeError(
            "kelly_fraction > 0.50 est refuse: avec des probabilites estimees, le Kelly "
            "plein surmise systematiquement et le risque de ruine devient reel"
        )
    day_cap = as_float(
        require(policy, "max_stake_pct_per_day", "policy"),
        "policy.max_stake_pct_per_day",
    )
    if not 0.0 < day_cap <= 0.20:
        raise StakeError("policy.max_stake_pct_per_day doit etre dans ]0,0.20]")
    max_bets = require(policy, "max_bets_per_day", "policy")
    if isinstance(max_bets, bool) or not isinstance(max_bets, int) or not 1 <= max_bets <= 100:
        raise StakeError("policy.max_bets_per_day doit etre un entier entre 1 et 100")
    if float(policy["max_stake_pct_per_race"]) > day_cap:
        raise StakeError("max_stake_pct_per_race ne peut pas depasser le plafond journalier")
    unit = as_float(require(policy, "stake_rounding_eur", "policy"), "policy.stake_rounding_eur")
    breakage = as_float(require(policy, "breakage_eur", "policy"), "policy.breakage_eur")
    if unit <= 0.0 or breakage <= 0.0:
        raise StakeError("stake_rounding_eur et breakage_eur doivent etre positifs")
    window = as_float(
        require(policy, "max_minutes_to_post", "policy"), "policy.max_minutes_to_post"
    )
    if not 0.0 < window <= 30.0:
        raise StakeError("max_minutes_to_post doit etre dans ]0,30]")
    for key in ("min_edge_simple", "min_edge_exotic"):
        edge = as_float(require(policy, key, "policy"), f"policy.{key}")
        if not 0.0 <= edge <= 5.0:
            raise StakeError(f"policy.{key} doit etre dans [0,5]")
    timezone_name = require(policy, "day_timezone", "policy")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise StakeError("policy.day_timezone doit etre un fuseau IANA")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise StakeError(f"Fuseau IANA inconnu: {timezone_name}") from exc
    require(policy, "drift_model", "policy")
    takeout = require(policy, "takeout", "policy")
    if not isinstance(takeout, dict) or not takeout:
        raise StakeError("policy.takeout doit etre un objet non vide")
    for pool, raw_value in takeout.items():
        value = as_float(raw_value, f"policy.takeout.{pool}")
        if not 0.0 <= value < 1.0:
            raise StakeError(f"Prelevement hors [0,1[: {pool}")
    return policy


def evaluate_race(
    report: dict[str, Any], ranks: np.ndarray, numbers: list[int], market: dict[str, Any],
    policy: dict[str, Any], calibration: dict[str, Any] | None,
    commit: dict[str, Any] | None, *, current: datetime | None = None,
) -> dict[str, Any]:
    current = (current or now_utc()).replace(microsecond=0)
    verify_sealed_hash(report, "report_sha256", "REPORT")
    if report.get("schema_version") not in ACCEPTED_REPORT_SCHEMAS:
        raise StakeError(f"REPORT au schema {sorted(ACCEPTED_REPORT_SCHEMAS)} requis")
    validate_policy(policy)
    if market.get("schema_version") != MARKET_SCHEMA:
        raise StakeError(f"schema_version de marche attendu: {MARKET_SCHEMA}")
    if market.get("race_key") != report["race_key"]:
        raise StakeError("race_key du marche incompatible avec le rapport")

    start = parse_iso(report["clock"]["scheduled_start_utc"], "report.clock.scheduled_start_utc")
    observed = parse_iso(require(market, "observed_at", "market"), "market.observed_at")
    if observed >= start:
        raise StakeError("Snapshot marche posterieur au depart")
    if current >= start:
        raise StakeError("Evaluation refusee: le depart est passe")
    if observed > current + timedelta(seconds=5):
        raise StakeError("Snapshot marche date dans le futur")
    minutes_to_post = (start - observed).total_seconds() / 60.0

    scratched = market.get("non_runners", [])
    if not isinstance(scratched, list):
        raise StakeError("market.non_runners doit etre une liste")
    active = set(report["render"]["verdict"]["strict_order"])
    conflicting = sorted(set(int(x) for x in scratched) & active)

    win_rapports_raw = require(require(market, "pools", "market"), "SIMPLE_GAGNANT", "market.pools")
    win_rapports = {int(k): as_float(v, f"market.SIMPLE_GAGNANT.{k}") for k, v in
                    require(win_rapports_raw, "rapports", "market.pools.SIMPLE_GAGNANT").items()}
    if set(win_rapports) != set(numbers):
        raise StakeError("Les rapports gagnant doivent couvrir exactement tous les partants actifs")
    if any(value <= 1.0 for value in win_rapports.values()):
        raise StakeError("Rapport decimal <= 1 detecte")

    p_market_map, shin_z = shin_probabilities(win_rapports)
    p_market = np.asarray([p_market_map[n] for n in numbers], dtype=float)

    # Derive de prix entre deux snapshots. Negative = cheval soutenu.
    previous = market.get("previous_snapshot")
    drift_vector = None
    drift_minutes = None
    if isinstance(previous, dict):
        earlier = {int(k): as_float(v, "market.previous_snapshot") for k, v in
                   previous.get("rapports", {}).items()}
        previous_at = parse_iso(require(previous, "observed_at", "market.previous_snapshot"),
                                "market.previous_snapshot.observed_at")
        if previous_at >= observed:
            raise StakeError("previous_snapshot doit etre anterieur au snapshot principal")
        if set(earlier) == set(numbers) and all(v > 1.0 for v in earlier.values()):
            drift_vector = np.asarray(
                [math.log(win_rapports[n] / earlier[n]) for n in numbers], dtype=float
            )
            drift_minutes = round((start - previous_at).total_seconds() / 60.0, 2)

    matrix = report["internal"]["rank_matrix"]
    p_model = np.asarray([float(matrix[str(n)][0]) for n in numbers], dtype=float)
    p_model = p_model / p_model.sum()

    # ---- portes bloquantes -------------------------------------------------
    gates: list[dict[str, Any]] = []

    def gate(name: str, passed: bool, detail: str, kind: str = "BLOQUANTE") -> None:
        """kind BLOQUANTE : integrite du dossier, aucune mise ni papier possible.
        kind GRADUANTE : maturite de la preuve, fait descendre le niveau d'echelle
        sans empecher la production d'une decision engagee sur papier."""
        gates.append({"gate": name, "passed": bool(passed), "detail": detail, "kind": kind})

    def gate_graduating(name: str, passed: bool, detail: str) -> None:
        gate(name, passed, detail, kind="GRADUANTE")

    calibration_usable = (
        calibration is not None and calibration.get("usable_for_staking") is True
    )
    calibration_linked = (
        calibration is not None
        and report.get("calibration_sha256") == calibration.get("calibration_sha256")
        and report.get("simulation", {}).get("calibration_sha256")
            == calibration.get("calibration_sha256")
    )
    calibrated = calibration_usable and calibration_linked
    if calibration is None:
        gate_graduating("CALIBRATION_PRESENTE", False,
             "aucun fichier de calibration: les notes TITAN sont ordinales et non "
             "convertibles en probabilites de pari")
    else:
        verify_sealed_hash(calibration, "calibration_sha256", "CALIBRATION")
        if calibration.get("schema_version") != CALIBRATION_SCHEMA:
            raise StakeError(f"Calibration au schema {CALIBRATION_SCHEMA} requise")
        gate_graduating("CALIBRATION_EXPLOITABLE", calibrated,
             "; ".join(calibration["blocking_reasons"]) or
             f"calibree sur {calibration['n_races']} courses, "
             f"a_modele={calibration['blend']['a_model']}")
        gate_graduating(
            "CALIBRATION_CHAINEE_AU_REPORT",
            calibration_linked,
            "empreinte calibration identique dans REPORT et CALIBRATION"
            if calibration_linked else
            "REPORT non reconstruit avec simulation.calibration_sha256",
        )
    gate_graduating("STATUT_CALIBRATION_MOTEUR", report.get("calibration_status") == "VALIDATED_OOS",
         f"report.calibration_status = {report.get('calibration_status')}")
    gate_graduating(
        "DOSSIER_COMPLET",
        report.get("fill_mode", "FULL") == "FULL",
        f"report.fill_mode = {report.get('fill_mode', 'FULL')}",
    )
    gate("ENGAGEMENT_PUBLIE_AVANT_DEPART", commit is not None,
         "un rapport non engage publiquement avant le depart ne prouve aucune anteriorite"
         if commit is None else f"engage a T-{commit.get('minutes_before_start')} min")
    if commit is not None:
        verify_sealed_hash(commit, "entry_sha256", "COMMIT")
        if commit.get("schema_version") not in ACCEPTED_COMMIT_SCHEMAS:
            raise StakeError(f"COMMIT au schema {sorted(ACCEPTED_COMMIT_SCHEMAS)} requis")
        if commit.get("race_key") != report["race_key"]:
            raise StakeError("L'engagement publie porte sur une autre course")
        if commit.get("report_sha256") != report["report_sha256"]:
            raise StakeError("L'engagement publie ne correspond pas a ce rapport")
        committed_at = parse_iso(commit.get("committed_at_utc"), "commit.committed_at_utc")
        if committed_at >= start:
            raise StakeError("Engagement post-depart interdit")
    risk_grade = report["render"]["verdict"]["race_risk"]["grade"]
    # GRADUANTE et non bloquante: une course incertaine ne doit pas empecher
    # d'ENREGISTRER ce que le modele aurait joue. Elle doit empecher l'ARGENT.
    # Le chemin leger produit structurellement du U2/U3 - missingness elevee et
    # grade C - et le classer bloquant reviendrait a ne jamais faire avancer le
    # compteur, ce qui est exactement le piege que l'echelle graduee corrige.
    gate_graduating("RACE_RISK_ACCEPTABLE", risk_grade in {"U0", "U1"},
                    f"RACE-RISK {risk_grade}")
    gate("CHAMP_DEFINITIF", bool(report["clock"]["field_declared_final"]),
         "un non-partant tardif invalide les combines et modifie les rapports")
    window = float(policy.get("max_minutes_to_post", 15.0))
    gate("FENETRE_DE_PRIX", minutes_to_post <= window,
         f"snapshot a T-{minutes_to_post:.1f} min, fenetre autorisee T-{window:.0f} min; "
         "loin du depart le rapport affiche ne reflete qu'une minorite des enjeux")
    gate("AUCUN_NON_PARTANT_NON_TRAITE", not conflicting,
         "aucun retrait signale" if not conflicting else
         f"partants {conflicting} declares non-partants mais toujours dans le rapport scelle: "
         "relancer `scratch` puis `analyze`, et emettre un nouvel engagement")

    if calibrated:
        a = float(calibration["blend"]["a_model"])
        b = float(calibration["blend"]["b_market"])
        c = float(calibration["blend"].get("c_drift", 0.0))
        if drift_vector is None:
            c = 0.0
        p_final = logit_blend(p_model, p_market, a, b, drift_vector, c)
        probability_source = f"melange logit calibre (a={a}, b={b}, c={c})"
    else:
        # Sans calibration, on NE PEUT PAS convertir le modele en probabilites de
        # pari. Mais recopier le marche ne journalise rien: le module ne parierait
        # jamais contre lui, donc aucun pari theorique ne serait jamais enregistre
        # et le compteur ne demarrerait jamais. On expose donc le modele BRUT,
        # exclusivement pour le mode papier, ou aucun euro n'est engage.
        #
        # Toute esperance affichee dans cet etat est THEORIQUE. C'est precisement
        # ce qu'il faut mesurer sur 200 courses: le modele brut se trompe-t-il
        # moins que le prix public ? Personne ne le sait avant de l'avoir compte.
        a, b, c = 1.0, 0.0, 0.0
        p_final = p_model.copy()
        probability_source = (
            "MODELE BRUT NON CALIBRE - papier uniquement, esperances theoriques"
        )

    place_slots = 3 if len(numbers) >= 8 else 2
    weights = importance_weights(ranks, numbers, p_final)
    events = event_probabilities(ranks, numbers, weights, place_slots)
    return {
        "context": {
            "race_key": report["race_key"],
            "course_name": report["course_name"],
            # v1.6.1. Reporte pour que la valeur de cloture puisse comparer le
            # protocole COMPLET au remplissage LEGER: si les deux portent la
            # meme information, l'appareil ne gagne pas son cout.
            "fill_mode": report.get("fill_mode", "FULL"),
            "data_grade": report.get("data_grade"),
            "report_sha256": report["report_sha256"],
            "rank_samples_sha256": report["rank_samples_sha256"],
            "entry_sha256": commit.get("entry_sha256") if commit else None,
            "market_snapshot_sha256": sha256_obj(market),
            "policy_sha256": sha256_obj(policy),
            "calibration_sha256": (
                calibration.get("calibration_sha256") if calibration else None
            ),
            "evaluated_at_utc": iso_utc(current),
            "market_observed_at_utc": iso_utc(observed),
            "minutes_to_post_at_snapshot": round(minutes_to_post, 2),
            "n_runners": len(numbers),
            "place_slots": place_slots,
            "shin_z": round(shin_z, 5),
            "probability_source": probability_source,
            "blend_a_model": a,
            "blend_b_market": b,
            "blend_c_drift": c,
            "drift_available": drift_vector is not None,
            "previous_snapshot_minutes_to_post": drift_minutes,
            "drift_log_ratio": (
                {str(n): round(float(v), 5) for n, v in zip(numbers, drift_vector)}
                if drift_vector is not None else None
            ),
        },
        "gates": gates,
        "all_gates_passed": all(item["passed"] for item in gates),
        "blocking_gates_passed": all(
            item["passed"] for item in gates if item["kind"] == "BLOQUANTE"
        ),
        "graduating_gates_passed": all(
            item["passed"] for item in gates if item["kind"] == "GRADUANTE"
        ),
        "calibration_races": int(calibration["n_races"]) if calibration else 0,
        "calibration_positive_edge": bool(calibrated),
        "race_risk_grade": risk_grade,
        "numbers": numbers,
        "p_model": p_model,
        "p_market": p_market,
        "p_final": p_final,
        "events": events,
        "win_rapports": win_rapports,
        "minutes_to_post": minutes_to_post,
    }


# ---------------------------------------------------------------------------
# 7. CONSTRUCTION DU TICKET
# ---------------------------------------------------------------------------

def candidate_bets(state: dict[str, Any], market: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
    """Toutes les mises calculables, avec leur esperance APRES prelevement.

    Un pari combine n'est evaluable que si l'operateur publie un rapport probable.
    En parimutuel, les rapports de Trio ou de Jumele ne sont pas connus avant la
    cloture : sans rapport probable publie, l'esperance n'est pas calculable, et
    une esperance non calculable ne peut jamais justifier une mise.
    """
    numbers = state["numbers"]
    events = state["events"]
    pools = market["pools"]
    minutes = state["minutes_to_post"]
    takeout = policy["takeout"]
    candidates: list[dict[str, Any]] = []

    for pool_name, pool in pools.items():
        if pool_name not in takeout:
            continue
        rapports = pool.get("rapports")
        if not isinstance(rapports, dict) or not rapports:
            continue
        probable = bool(pool.get("is_probable_payout", pool_name in EXOTIC_POOLS))
        table = events.get(pool_name)
        if table is None:
            continue
        drift = drift_quantile(policy, minutes, pool_name)
        for key, raw_rapport in rapports.items():
            probability = table.get(str(key))
            if probability is None:
                continue
            rapport = as_float(raw_rapport, f"market.pools.{pool_name}.rapports.{key}")
            if rapport <= 1.0:
                continue
            granularity = float(policy.get("breakage_eur", 0.10))
            paid_rapport = apply_breakage(rapport, granularity)
            conservative_rapport = apply_breakage(rapport * drift, granularity)
            ev_observed = probability * paid_rapport - 1.0
            ev_conservative = probability * conservative_rapport - 1.0
            candidates.append(
                {
                    "pool": pool_name,
                    "selection": str(key),
                    "family": "SIMPLE" if pool_name in SIMPLE_POOLS else "EXOTIC",
                    "probability": round(float(probability), 6),
                    "rapport_observed": round(rapport, 3),
                    "rapport_after_breakage": round(paid_rapport, 3),
                    "drift_factor_applied": round(drift, 4),
                    "rapport_conservative": round(conservative_rapport, 3),
                    "declared_takeout": takeout[pool_name],
                    "ev_observed_price": round(float(ev_observed), 5),
                    "ev_conservative_price": round(float(ev_conservative), 5),
                    "payout_is_probable_only": probable,
                    "market_implied_probability": round(1.0 / rapport, 6),
                }
            )
    candidates.sort(key=lambda item: -item["ev_conservative_price"])
    return candidates


def verify_ticket_journal(tickets: list[dict[str, Any]]) -> dict[str, Any]:
    """Verifie le journal append-only des decisions et refuse les doublons."""
    previous: str | None = None
    seen_races: set[str] = set()
    last_time: datetime | None = None
    for index, ticket in enumerate(tickets):
        where = f"ticket[{index}]"
        if ticket.get("schema_version") != TICKET_SCHEMA:
            raise StakeError(f"{where}: schema {TICKET_SCHEMA} requis")
        verify_sealed_hash(ticket, "ticket_sha256", where)
        if ticket.get("prev_ticket_sha256") != previous:
            raise StakeError(f"{where}: chainage du journal rompu")
        race_key = str(ticket.get("context", {}).get("race_key"))
        if not race_key or race_key == "None":
            raise StakeError(f"{where}: race_key absent")
        if race_key in seen_races:
            raise StakeError(f"{where}: course dupliquee dans le journal ({race_key})")
        seen_races.add(race_key)
        evaluated = parse_iso(
            ticket.get("context", {}).get("evaluated_at_utc"),
            f"{where}.context.evaluated_at_utc",
        )
        if last_time is not None and evaluated < last_time:
            raise StakeError(f"{where}: ordre chronologique du journal rompu")
        last_time = evaluated
        previous = ticket["ticket_sha256"]
    return {
        "n_tickets": len(tickets),
        "head_sha256": previous,
        "race_keys": seen_races,
    }


def day_exposure(
    tickets: list[dict[str, Any]], target_time: datetime, timezone_name: str,
) -> dict[str, Any]:
    """Exposition reelle deja engagee le jour civil du fuseau de politique."""
    state = verify_ticket_journal(tickets)
    zone = ZoneInfo(timezone_name)
    target_date = target_time.astimezone(zone).date()
    committed = 0.0
    count = 0
    same_day_tickets = 0
    for ticket in tickets:
        evaluated = parse_iso(
            ticket["context"]["evaluated_at_utc"], "ticket.context.evaluated_at_utc"
        )
        if evaluated.astimezone(zone).date() != target_date:
            continue
        same_day_tickets += 1
        if ticket.get("decision") != "TICKET":
            continue
        stake_pct = as_float(
            ticket.get("total_stake_pct_bankroll", 0.0),
            "ticket.total_stake_pct_bankroll",
        )
        if stake_pct < 0.0:
            raise StakeError("Exposition negative dans le journal")
        committed += stake_pct
        count += len(ticket.get("bets", []))
    return {
        "schema_version": EXPOSURE_SCHEMA,
        "date": target_date.isoformat(),
        "timezone": timezone_name,
        "pct_bankroll_committed": round(committed, 6),
        "bets_placed": count,
        "tickets_seen_same_day": same_day_tickets,
        "journal_tickets_total": state["n_tickets"],
        "journal_head_sha256": state["head_sha256"],
        "race_keys": sorted(state["race_keys"]),
    }


def build_ticket(
    state: dict[str, Any], market: dict[str, Any], policy: dict[str, Any],
    day_tickets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    step = ladder_level(state["calibration_races"], state["calibration_positive_edge"])
    # Une course U2 ou U3 reste analysable et journalisable, mais jamais payante:
    # elle est ramenee de force en mode papier quel que soit le niveau atteint.
    risky = state.get("race_risk_grade") not in {"U0", "U1"}
    maturity_blocked = not state.get("graduating_gates_passed", False)
    paper_mode = step["max_stake_pct_per_bet"] <= 0.0 or risky or maturity_blocked
    candidates = candidate_bets(state, market, policy)
    bankroll = float(policy["bankroll_eur"])
    target_time = parse_iso(
        state["context"]["evaluated_at_utc"], "state.context.evaluated_at_utc"
    )
    exposure = day_exposure(
        day_tickets or [], target_time, str(policy["day_timezone"])
    )
    if state["context"]["race_key"] in exposure["race_keys"]:
        raise StakeError(
            f"Course deja presente dans le journal de tickets: {state['context']['race_key']}"
        )
    day_cap = float(policy["max_stake_pct_per_day"])
    max_bets = int(policy["max_bets_per_day"])
    remaining_day = max(0.0, day_cap - exposure["pct_bankroll_committed"])
    remaining_bet_slots = max(0, max_bets - exposure["bets_placed"])
    # Les seuils de l'echelle priment sur ceux de la politique s'ils sont plus severes.
    min_simple = max(float(policy["min_edge_simple"]), float(step["min_edge_simple"]))
    min_exotic = max(float(policy["min_edge_exotic"]), float(step["min_edge_exotic"]))
    allowed_pools = set(step["pools"])
    blocked: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []

    for item in candidates:
        threshold = min_simple if item["family"] == "SIMPLE" else min_exotic
        reasons = []
        if not state["blocking_gates_passed"]:
            reasons.append("porte bloquante non franchie: integrite du dossier")
        if paper_mode and item["family"] != "SIMPLE":
            reasons.append("papier limite aux paris simples: les combines exigent une "
                           "structure conditionnelle calibree")
        if item["pool"] not in allowed_pools:
            reasons.append(f"pool non ouvert au niveau {step['level']} ({step['mode']})")
        if item["ev_conservative_price"] < threshold:
            reasons.append(
                f"esperance {item['ev_conservative_price']:+.3f} sous le seuil {threshold:+.3f}"
            )
        if item["family"] == "EXOTIC" and item["payout_is_probable_only"]:
            reasons.append("rapport seulement probable: paye au rapport final inconnu")
        if reasons:
            blocked.append({**item, "blocking_reasons": reasons})
        else:
            retained.append(item)

    bet_slot_binding = False
    if not paper_mode and len(retained) > remaining_bet_slots:
        bet_slot_binding = True
        overflow = retained[remaining_bet_slots:]
        retained = retained[:remaining_bet_slots]
        for item in overflow:
            blocked.append({
                **item,
                "blocking_reasons": [
                    f"plafond journalier de {max_bets} paris atteint"
                ],
            })

    stakes: list[dict[str, Any]] = []
    if retained:
        win_bets = [item for item in retained if item["pool"] == "SIMPLE_GAGNANT"]
        others = [item for item in retained if item["pool"] != "SIMPLE_GAGNANT"]
        if win_bets:
            probabilities = np.asarray([item["probability"] for item in win_bets])
            net_odds = np.asarray([item["rapport_conservative"] - 1.0 for item in win_bets])
            fractions = multi_outcome_kelly(probabilities, net_odds)
            for item, fraction in zip(win_bets, fractions):
                stakes.append({**item, "kelly_full": round(float(fraction), 6)})
        for item in others:
            probability = item["probability"]
            net = item["rapport_conservative"] - 1.0
            fraction = max(0.0, (probability * net - (1.0 - probability)) / net) if net > 0 else 0.0
            stakes.append({**item, "kelly_full": round(float(fraction), 6)})

    fraction_policy = float(policy["kelly_fraction"])
    per_bet_cap = min(float(policy["max_stake_pct_per_bet"]), float(step["max_stake_pct_per_bet"]))
    per_race_cap = float(policy["max_stake_pct_per_race"])
    unit = float(policy.get("stake_rounding_eur", 1.0))
    for item in stakes:
        pct = min(item["kelly_full"] * fraction_policy, per_bet_cap)
        item["stake_pct_bankroll"] = round(pct, 6)
        item["stake_eur_raw"] = round(pct * bankroll, 2)
    effective_race_cap = min(per_race_cap, remaining_day)
    if remaining_bet_slots <= 0:
        effective_race_cap = 0.0
    total_pct = sum(item["stake_pct_bankroll"] for item in stakes)
    stake_cap_binding = total_pct > effective_race_cap and total_pct > 0
    if total_pct > effective_race_cap and total_pct > 0:
        scale = effective_race_cap / total_pct
        for item in stakes:
            item["stake_pct_bankroll"] = round(item["stake_pct_bankroll"] * scale, 6)
            item["stake_eur_raw"] = round(item["stake_pct_bankroll"] * bankroll, 2)
    final_stakes = []
    for item in stakes:
        if paper_mode:
            # Le pari theorique est calcule au plafond du niveau NOMINAL: c'est ce
            # qu'on aurait joue. Il sert a mesurer, pas a miser.
            reference_pct = min(item["kelly_full"] * fraction_policy, 0.02)
            final_stakes.append({
                **item, "stake_eur": 0.0,
                "paper_stake_pct_bankroll": round(reference_pct, 6),
                "paper_stake_eur": round(reference_pct * bankroll, 2),
            })
            continue
        rounded = math.floor(item["stake_eur_raw"] / unit) * unit
        if rounded >= unit:
            final_stakes.append({**item, "stake_eur": round(rounded, 2)})
        else:
            blocked.append({**item, "blocking_reasons": ["mise inferieure a l'unite minimale"]})

    if not final_stakes:
        decision = "NO_BET"
    elif paper_mode:
        decision = "PAPIER"
    else:
        decision = "TICKET"
    total_stake = round(sum(item["stake_eur"] for item in final_stakes), 2)
    total_stake_pct = round(total_stake / bankroll, 6) if bankroll else 0.0
    requirement = requirement_table(state, candidates, policy)
    ticket = {
        "schema_version": TICKET_SCHEMA,
        "stake_version": STAKE_VERSION,
        "prev_ticket_sha256": exposure["journal_head_sha256"],
        "decision": decision,
        "ladder": {
            "level": step["level"],
            "mode": step["mode"],
            "races_logged": state["calibration_races"],
            "positive_edge_established": state["calibration_positive_edge"],
            "max_stake_pct_per_bet": step["max_stake_pct_per_bet"],
            "pools_open": sorted(allowed_pools),
            "min_edge_simple_effective": min_simple,
            "next_unlock": step["unlock"],
            "forced_paper_by_race_risk": bool(risky),
            "forced_paper_by_maturity_gate": bool(maturity_blocked),
            "reading": (
                "le montant suit la preuve accumulee; la decision, elle, est complete "
                "et engagee des le niveau 0"
            ),
        },
        "context": state["context"],
        "gates": state["gates"],
        "all_gates_passed": state["all_gates_passed"],
        "bets": final_stakes,
        "total_stake_eur": total_stake,
        "day_exposure": {
            "schema_version": exposure["schema_version"],
            "date": exposure["date"],
            "timezone": exposure["timezone"],
            "pct_bankroll_committed_before": exposure["pct_bankroll_committed"],
            "bets_placed_before": exposure["bets_placed"],
            "tickets_seen_same_day": exposure["tickets_seen_same_day"],
            "max_stake_pct_per_day": day_cap,
            "remaining_pct_bankroll_before": round(remaining_day, 6),
            "remaining_pct_bankroll_after": round(
                max(0.0, remaining_day - total_stake_pct), 6
            ),
            "max_bets_per_day": max_bets,
            "remaining_bet_slots_before": remaining_bet_slots,
            "bets_placed_after": exposure["bets_placed"] + len(final_stakes),
            "day_cap_binding": bool(
                not paper_mode
                and (stake_cap_binding or bet_slot_binding or remaining_day < per_race_cap)
            ),
        },
        "total_stake_pct_bankroll": total_stake_pct,
        "rejected": blocked[:40],
        "n_rejected": len(blocked),
        "what_would_have_to_be_true": requirement,
        "paper_warning": (
            "probabilites non calibrees: les esperances affichees sont THEORIQUES et "
            "servent a mesurer le modele sur la duree, jamais a justifier une mise"
        ) if paper_mode and not state["calibration_positive_edge"] else None,
        "probabilities_final": {
            str(number): round(float(value), 6)
            for number, value in zip(state["numbers"], state["p_final"])
        },
        "probabilities_market_shin": {
            str(number): round(float(value), 6)
            for number, value in zip(state["numbers"], state["p_market"])
        },
        "probabilities_model_raw": {
            str(number): round(float(value), 6)
            for number, value in zip(state["numbers"], state["p_model"])
        },
        "governance": (
            "toute mise emise doit etre journalisee et auditee; un ticket non journalise "
            "empeche toute mesure honnete du rendement reel"
        ),
        "responsible_play": (
            "les jeux d'argent peuvent etre dangereux: pertes, conflits, addiction. "
            "Aide et informations: joueurs-info-service.fr, 09 74 75 13 13 (appel non surtaxe)."
        ),
    }
    ticket["ticket_sha256"] = sha256_obj(ticket)
    return ticket


def requirement_table(
    state: dict[str, Any], candidates: list[dict[str, Any]], policy: dict[str, Any]
) -> list[dict[str, Any]]:
    """Ce qu'il faudrait pour que chaque type de pari devienne jouable.

    Sans cette table, un NO_BET ressemble a un refus. Avec elle, c'est un calcul
    que l'utilisateur peut verifier et contester ligne par ligne.
    """
    rows: list[dict[str, Any]] = []
    by_pool: dict[str, dict[str, Any]] = {}
    for item in candidates:
        best = by_pool.get(item["pool"])
        if best is None or item["ev_conservative_price"] > best["ev_conservative_price"]:
            by_pool[item["pool"]] = item
    for pool, item in sorted(by_pool.items()):
        threshold = float(policy["min_edge_simple"] if item["family"] == "SIMPLE"
                          else policy["min_edge_exotic"])
        needed_probability = (1.0 + threshold) / item["rapport_conservative"]
        needed_rapport = (1.0 + threshold) / max(item["probability"], 1e-9)
        rows.append(
            {
                "pool": pool,
                "best_selection": item["selection"],
                "current_probability": item["probability"],
                "required_probability": round(needed_probability, 6),
                "probability_gap": round(needed_probability - item["probability"], 6),
                "current_conservative_rapport": item["rapport_conservative"],
                "required_rapport": round(needed_rapport, 3),
                "declared_takeout": item["declared_takeout"],
                "reading": (
                    f"il faudrait une probabilite reelle de {needed_probability:.1%} "
                    f"(estimee {item['probability']:.1%}) ou un rapport de "
                    f"{needed_rapport:.2f} (observe {item['rapport_conservative']:.2f} "
                    f"apres derive)"
                ),
            }
        )
    return rows


def render_ticket(ticket: dict[str, Any]) -> str:
    verify_sealed_hash(ticket, "ticket_sha256", "TICKET")
    context = ticket["context"]
    lines = [
        f"TITAN STAKE v{ticket['stake_version']} - {context['course_name']}",
        f"race_key: {context['race_key']}",
        f"Snapshot marche: T-{context['minutes_to_post_at_snapshot']:.1f} min | "
        f"Shin z = {context['shin_z']:.4f}",
        f"Probabilites: {context['probability_source']}",
        "",
        f"DECISION: {ticket['decision']}   [echelle niveau {ticket['ladder']['level']} "
        f"- {ticket['ladder']['mode']} - {ticket['ladder']['races_logged']} courses journalisees]",
        "",
        "PORTES SYSTEME",
    ]
    for item in ticket["gates"]:
        mark = "OK  " if item["passed"] else "BLOC"
        lines.append(f"  [{mark}] {item['gate']}: {item['detail']}")
    if ticket["decision"] in {"TICKET", "PAPIER"}:
        if ticket["ladder"]["forced_paper_by_race_risk"]:
            lines.append("  (papier force: RACE-RISK au-dessus de U1, la course est "
                         "journalisee mais jamais payante)")
        if ticket["ladder"].get("forced_paper_by_maturity_gate"):
            lines.append(
                "  (papier force: au moins une porte de maturite/calibration n'est "
                "pas franchie)"
            )
        header = "MISES RETENUES" if ticket["decision"] == "TICKET" else (
            "PARIS THEORIQUES - AUCUN EURO ENGAGE, MAIS DECISION PRISE ET JOURNALISEE\n"
            "  (esperances theoriques sous probabilites non calibrees)")
        lines.extend(["", header])
        for bet in ticket["bets"]:
            amount = bet.get("paper_stake_eur", bet["stake_eur"])
            tag = "papier" if ticket["decision"] == "PAPIER" else "reel  "
            lines.append(
                f"  {bet['pool']:<16} {bet['selection']:<10} "
                f"{amount:>7.2f} EUR ({tag}) | p={bet['probability']:.3f} | "
                f"rapport prudent {bet['rapport_conservative']:.2f} | "
                f"EV {bet['ev_conservative_price']:+.3f}"
            )
        if ticket["decision"] == "TICKET":
            lines.append(f"  TOTAL: {ticket['total_stake_eur']:.2f} EUR "
                         f"({100 * ticket['total_stake_pct_bankroll']:.2f} % de la banque)")
            day = ticket["day_exposure"]
            lines.append(
                f"  Journee avant ce ticket: "
                f"{100 * day['pct_bankroll_committed_before']:.2f} % engages, "
                f"{100 * day['remaining_pct_bankroll_after']:.2f} % restants apres "
                f"ce ticket sur "
                f"{100 * day['max_stake_pct_per_day']:.0f} %"
                + ("  [PLAFOND JOURNALIER ACTIF]" if day["day_cap_binding"] else "")
            )
        lines.append(f"  Prochain palier: {ticket['ladder']['next_unlock']}")
    else:
        lines.extend([
            "",
            "AUCUNE MISE. Ce n'est pas une prudence de facade: l'esperance calculee",
            "apres prelevement est insuffisante ou non calculable.",
            "",
            "CE QU'IL FAUDRAIT POUR QUE CHAQUE PARI DEVIENNE JOUABLE",
        ])
        for row in ticket["what_would_have_to_be_true"]:
            lines.append(f"  {row['pool']:<16} {row['best_selection']:<10} {row['reading']}")
    lines.extend(["", f"ticket_sha256: {ticket['ticket_sha256']}", "",
                  ticket["responsible_play"]])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8. JOURNAL DE PRIX ET AJUSTEMENT DE LA DERIVE
# ---------------------------------------------------------------------------

def validate_price_record(record: dict[str, Any], where: str = "price_record") -> None:
    if record.get("schema_version") != PRICE_LOG_SCHEMA:
        raise StakeError(f"{where}: schema {PRICE_LOG_SCHEMA} requis")
    verify_sealed_hash(record, "record_sha256", where)
    previous = record.get("prev_record_sha256")
    if previous is not None and (
        not isinstance(previous, str) or len(previous) != 64
    ):
        raise StakeError(f"{where}.prev_record_sha256 invalide")
    if record.get("kind") not in {"snapshot", "final"}:
        raise StakeError(f"{where}.kind invalide")
    require(record, "race_key", where)
    parse_iso(require(record, "observed_at", where), f"{where}.observed_at")
    minutes = as_float(require(record, "minutes_to_post", where), f"{where}.minutes_to_post")
    if minutes < -30.0:
        raise StakeError(f"{where}.minutes_to_post inferieur a -30")
    rapports = require(record, "rapports", where)
    if not isinstance(rapports, dict) or not rapports:
        raise StakeError(f"{where}.rapports doit etre un objet non vide")
    for key, raw in rapports.items():
        if as_float(raw, f"{where}.rapports.{key}") <= 0.0:
            raise StakeError(f"{where}.rapports.{key} doit etre positif")


def verify_price_journal(records: list[dict[str, Any]]) -> dict[str, Any]:
    previous: str | None = None
    seen: set[str] = set()
    last_observed: datetime | None = None
    for index, record in enumerate(records):
        where = f"price_log[{index}]"
        validate_price_record(record, where)
        if record["record_sha256"] in seen:
            raise StakeError(f"{where}: enregistrement duplique")
        if record.get("prev_record_sha256") != previous:
            raise StakeError(f"{where}: chainage du journal de prix rompu")
        observed = parse_iso(record["observed_at"], f"{where}.observed_at")
        if last_observed is not None and observed < last_observed:
            raise StakeError(f"{where}: ordre chronologique du journal rompu")
        seen.add(record["record_sha256"])
        previous = record["record_sha256"]
        last_observed = observed
    return {"n_records": len(records), "head_sha256": previous}


def price_record(
    race_key: str, observed_at: str, minutes_to_post: float,
    rapports: dict[str, float], kind: str,
    previous_record_sha256: str | None = None,
) -> dict[str, Any]:
    if kind not in {"snapshot", "final"}:
        raise StakeError("kind doit valoir snapshot ou final")
    parse_iso(observed_at, "observed_at")
    if not isinstance(rapports, dict) or not rapports:
        raise StakeError("rapports doit etre un objet non vide")
    if not math.isfinite(float(minutes_to_post)) or float(minutes_to_post) < -30.0:
        raise StakeError("minutes_to_post invalide")
    cleaned = {}
    for key, value in rapports.items():
        number = as_float(value, f"rapports.{key}")
        if number <= 0.0:
            raise StakeError(f"Rapport non positif: {key}")
        cleaned[str(key)] = number
    record = {
        "schema_version": PRICE_LOG_SCHEMA,
        "race_key": race_key,
        "kind": kind,
        "observed_at": observed_at,
        "minutes_to_post": round(float(minutes_to_post), 2),
        "rapports": cleaned,
        "prev_record_sha256": previous_record_sha256,
    }
    record["record_sha256"] = sha256_obj(record)
    validate_price_record(record)
    return record


def read_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    out = []
    for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise StakeError(f"Journal JSONL illisible ligne {number}") from exc
    return out


def append_jsonl(path: str | os.PathLike[str], record: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")


def fit_drift_model(records: list[dict[str, Any]], min_observations: int = 200) -> dict[str, Any]:
    """Ajuste le quantile 10 % de log(rapport final / rapport observe) par tranche.

    C'est la donnee que personne ne collecte et sans laquelle aucune esperance
    parimutuelle n'est honnete. Chaque couple (snapshot, rapport final) d'une
    meme course alimente la tranche correspondant a son horizon.

    Le quantile 10 % est volontairement pessimiste : on veut la borne basse du
    rapport plausible, pas sa moyenne. Miser sur la moyenne d'une distribution
    de prix inconnue revient a se tromper une fois sur deux du mauvais cote.
    """
    finals: dict[str, dict[str, float]] = {}
    verify_price_journal(records)
    for index, record in enumerate(records):
        if record.get("kind") == "final":
            key = str(record["race_key"])
            if key in finals:
                raise StakeError(f"Deux rapports finaux pour la meme course: {key}")
            finals[key] = {k: float(v) for k, v in record["rapports"].items()}
    buckets: dict[float, list[float]] = {edge: [] for edge in DRIFT_BUCKETS}
    races_used: set[str] = set()
    for record in records:
        if record.get("kind") != "snapshot":
            continue
        key = str(record["race_key"])
        final = finals.get(key)
        if final is None:
            continue
        minutes = float(record["minutes_to_post"])
        edge = DRIFT_BUCKETS[0]
        for candidate in DRIFT_BUCKETS:
            if minutes >= candidate:
                edge = candidate
        for runner, observed in record["rapports"].items():
            target = final.get(str(runner))
            if target is None or float(observed) <= 0 or float(target) <= 0:
                continue
            buckets[edge].append(math.log(float(target) / float(observed)))
            races_used.add(key)
    counts = {str(edge): len(values) for edge, values in buckets.items()}
    quantiles = {
        str(edge): (round(float(np.quantile(values, 0.10)), 5)
                    if len(values) >= min_observations else None)
        for edge, values in buckets.items()
    }
    # Une tranche JAMAIS utilisee ne doit pas bloquer l'ajustement: si vous
    # relevez toujours vers T-10 et T-3, les tranches lointaines resteront vides
    # a jamais. Elles retombent simplement sur la penalite forfaitaire.
    unused = [edge for edge, count in counts.items() if count == 0]
    thin = [edge for edge, count in counts.items() if 0 < count < min_observations]
    fitted = any(value is not None for value in quantiles.values())
    model = {
        "schema_version": DRIFT_SCHEMA,
        "stake_version": STAKE_VERSION,
        "n_races": len(races_used),
        "observations_by_bucket": counts,
        "min_observations_required": min_observations,
        "underpopulated_buckets": thin,
        "unused_buckets": unused,
        "drift_model": {
            "fitted": fitted,
            "fitted_log_drift_q10": {k: v for k, v in quantiles.items() if v is not None},
            "unfitted_penalty_per_10min": 0.06,
            "unfitted_max_penalty": 0.30,
            "exotic_penalty_multiplier": 1.5,
        },
        "usage": "copier le bloc drift_model dans POLICY.json",
        "coverage_note": (
            "les tranches sans donnee retombent sur la penalite forfaitaire; "
            "ne relevez que les horizons auxquels vous misez reellement"
        ),
        "reading": (
            "une valeur negative signifie que le rapport final est typiquement "
            "INFERIEUR au rapport observe: le prix se resserre, souvent sur les "
            "chevaux que la monnaie tardive soutient"
        ),
    }
    model["drift_sha256"] = sha256_obj(model)
    return model


# ---------------------------------------------------------------------------
# 9. TEMPLATES
# ---------------------------------------------------------------------------

def proportional_probabilities(
    odds: dict[int, float], order: list[int] | None = None
) -> np.ndarray:
    """De-vigorisation PROPORTIONNELLE, la seule correcte en parimutuel.

    L'ARGUMENT EST ETROIT, ET IL FAUT LE GARDER ETROIT.

    On ne pretend PAS que Shin est un mauvais estimateur de probabilite. Shin
    attribue la majoration a des parieurs informes et corrige le biais
    favori-outsider - biais reel et documente dans les mises du public. Pour
    ESTIMER une probabilite de victoire, il reste defendable, et c'est pour
    cela qu'il est conserve partout ailleurs dans ce module.

    Ce qui est en cause ici, c'est de mesurer un DEPLACEMENT entre deux releves.
    La correction de Shin n'est pas un rescalage uniforme: elle depend du niveau
    de cote, et son z est reestime livre par livre. Elle ne s'annule donc PAS
    dans une difference entre deux instants, et ce qui reste est un residu
    correle au niveau de cote - c'est-a-dire correle a ce qu'on mesure.

    La normalisation proportionnelle, elle, est exacte pour cet usage. En
    parimutuel rapport_i = pool x (1 - prelevement) / mise_i, donc la
    majoration est un SCALAIRE UNIFORME et elle disparait EXACTEMENT en
    coordonnees log-ratio centrees, a n'importe quel taux: mesure a 1e-16 pour
    15 %, 25 % et 36 %. Les parts de mises sont conservees telles quelles, et
    c'est precisement ce qu'un deplacement de marche doit comparer.

    Consequence mesuree du melange des deux: sur des donnees ou le modele ne
    sait RIEN, la version Shin rendait beta = -0,04 declare significatif a 95 %
    - une conclusion fausse, en sens inverse du biais precedent. La version
    proportionnelle rend +0,002 sur les memes donnees.
    """
    keys = list(order) if order is not None else sorted(odds)
    values = np.asarray([1.0 / float(odds[key]) for key in keys], dtype=float)
    total = float(values.sum())
    if total <= 0.0:
        raise StakeError("Livre de cotes degenere: somme des inverses nulle")
    return values / total


# Valeurs critiques de Student a 97,5 %, par degres de liberte. Table figee
# plutot qu'une dependance a scipy: ce module ne doit rien importer d'autre que
# numpy pour rester executable partout.
_T_CRIT_975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
               7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 14: 2.145,
               16: 2.120, 19: 2.093, 24: 2.064, 29: 2.045, 39: 2.023,
               49: 2.010, 59: 2.001, 99: 1.984, 199: 1.972}


def student_critical(dof: int) -> float:
    if dof < 1:
        return 12.706
    for key in sorted(_T_CRIT_975):
        if dof <= key:
            return _T_CRIT_975[key]
    return 1.96


def cluster_interval(
    point: float, samples: np.ndarray, n_clusters: int
) -> tuple[float, float]:
    """Intervalle a 95 % corrige pour un NOMBRE DE GRAPPES faible.

    Le bootstrap par percentiles est anti-conservateur quand les grappes sont
    peu nombreuses - et vingt journees de course, c'est peu. Mesure sous
    hypothese nulle sur 60 jeux de 60 courses: le taux de rejet reel etait de
    11,7 % pour un nominal de 5 %. Un instrument qui annonce 95 % et se trompe
    deux fois plus souvent que promis est un instrument qui ment.

    On applique donc la correction standard d'inference groupee: valeur
    critique de Student a G-1 degres de liberte, et facteur de petit
    echantillon racine(G/(G-1)). Meme mesure apres correction: 3,3 %. On erre
    du cote CONSERVATEUR, ce qui est le bon cote pour ce projet.
    """
    spread = float(samples.std(ddof=1)) if samples.size > 1 else 0.0
    if n_clusters <= 1:
        return (float("-inf"), float("inf"))
    factor = student_critical(n_clusters - 1) * math.sqrt(
        n_clusters / (n_clusters - 1))
    half = factor * spread
    return point - half, point + half


def centred_log_ratio(probabilities: np.ndarray) -> np.ndarray:
    """Coordonnees CLR: log(p) recentre sur la course.

    Les probabilites d'une course somment a 1: ce sont des COMPOSITIONS, pas des
    grandeurs libres. Comparer deux compositions sans les recentrer melange le
    signal cherche avec la simple difference de normalisation entre les deux
    vecteurs. Le recentrage rend la mesure invariante a toute remise a l'echelle
    de la course - y compris a un taux de retour different d'un operateur ou
    d'un jour a l'autre.
    """
    logs = np.log(np.clip(probabilities, 1e-12, None))
    return logs - logs.mean()


def closing_line_value(
    tickets: list[dict[str, Any]], price_log: list[dict[str, Any]],
    *, draws: int = 2000, seed: int = 20260906, countable_only: bool = False,
) -> dict[str, Any]:
    """Le modele anticipe-t-il le mouvement du marche vers sa cloture ?

    POURQUOI CETTE MESURE, ET POURQUOI ELLE N'EST PAS UN RENDEMENT.

    En pari a cote fixe, battre la ligne de cloture EST l'avantage: on a pris
    5,0 sur un cheval que le marche a ferme a 4,0, et l'on est paye 5,0. En
    PARIMUTUEL - le cas francais - cela ne marche pas ainsi: on est paye le
    rapport FINAL, quel que soit le moment de la mise. Le prix qu'on "prend"
    n'existe pas.

    La mesure garde pourtant tout son sens, mais elle change de nature. La
    litterature etablit que la monnaie tardive est mieux informee que la
    monnaie precoce (annexe ecosysteme §8: pres de la moitie des enjeux sont
    engages dans les cinq dernieres minutes, et les parieurs informes retardent
    deliberement leurs mises). Le rapport final est donc un ETALON PUBLIC plus
    informe que le rapport observe au snapshot. Si nos desaccords avec le
    marche du snapshot predisent le sens du mouvement ulterieur, alors le
    modele contient une information que ce marche n'avait pas encore.

    Ce que l'on mesure exactement:

        d_i = clr(p_modele) - clr(p_marche)     desaccord du modele au snapshot
        m_i = clr(p_cloture) - clr(p_marche)    mouvement propre du marche
        beta = somme(d.m) / somme(d.d)          pente de m sur d

    beta = 0 : nos desaccords sont du bruit vis-a-vis du mouvement ulterieur.
    beta > 0 : le marche se deplace DANS notre sens; le modele savait quelque
               chose que le prix du snapshot ignorait.
    beta = 1 : le marche finit exactement sur notre opinion.

    CE QUE beta > 0 NE PROUVE PAS. Il ne prouve aucun profit. En parimutuel on
    encaisse la cloture: avoir raison AVANT elle ne paie rien en soi. Pour
    gagner de l'argent il faudrait que l'information depasse le prelevement -
    environ 15 % sur le simple - ce qui est une question distincte et bien plus
    dure. beta est un instrument de MESURE, jamais une autorisation de miser,
    et il n'entre dans aucune porte de l'echelle de mise.

    Le test est volontairement CONSERVATEUR: le modele est scelle a as_of, vers
    T-30, tandis que le marche de reference est celui du snapshot, souvent T-3.
    On demande donc au modele de battre un marche qui a eu une demi-heure
    d'information de plus que lui.
    """
    verify_ticket_journal(tickets)
    verify_price_journal(price_log)
    finals: dict[str, dict[str, float]] = {}
    snapshots: dict[str, list[dict[str, Any]]] = {}
    for record in price_log:
        key = str(record["race_key"])
        if record.get("kind") == "final":
            if key in finals:
                raise StakeError(f"Deux rapports finaux pour la meme course: {key}")
            finals[key] = {str(k): float(v) for k, v in record["rapports"].items()}
        elif record.get("kind") == "snapshot":
            snapshots.setdefault(key, []).append(record)

    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, ticket in enumerate(tickets):
        context = ticket.get("context") or {}
        key = str(context.get("race_key", ticket.get("race_key", f"?{index}")))
        final = finals.get(key)
        if final is None:
            skipped.append({"race_key": key, "reason": "aucun rapport final journalise"})
            continue
        model_raw = ticket.get("probabilities_model_raw") or {}
        market_raw = ticket.get("probabilities_market_shin") or {}
        if not model_raw or not market_raw:
            skipped.append({"race_key": key, "reason": "ticket sans vecteur de probabilites"})
            continue
        published = bool(context.get("entry_sha256")) and any(
            gate.get("gate") == "ENGAGEMENT_PUBLIE_AVANT_DEPART" and gate.get("passed")
            for gate in ticket.get("gates", [])
        )
        if countable_only and not published:
            skipped.append({"race_key": key, "reason": "engagement non publie avant le depart"})
            continue
        # NON-PARTANT TARDIF. Un cheval retire entre le snapshot et la cloture
        # disparait des rapports finaux, et en parimutuel le retrait provoque un
        # recalcul complet du pool. On restreint donc a l'INTERSECTION et l'on
        # renormalise des deux cotes: comparer un vecteur a sept chevaux avec un
        # vecteur a huit produirait un mouvement entierement fictif.
        common = sorted(
            set(model_raw) & set(market_raw) & set(final),
            key=lambda item: int(item) if str(item).lstrip("-").isdigit() else 0,
        )
        if len(common) < 3:
            skipped.append({"race_key": key, "reason": "moins de trois partants communs"})
            continue
        odds = {int(number): float(final[number]) for number in common}
        if any(value <= 1.0 for value in odds.values()):
            skipped.append({"race_key": key, "reason": "rapport final <= 1"})
            continue
        order = [int(n) for n in common]
        p_close = proportional_probabilities(odds, order)
        p_model = np.asarray([float(model_raw[n]) for n in common], dtype=float)
        if p_model.sum() <= 0:
            skipped.append({"race_key": key, "reason": "vecteur modele degenere"})
            continue
        p_model = p_model / p_model.sum()

        # BASE DE MARCHE. On la prend au JOURNAL DE PRIX quand il la porte, pour
        # que les trois vecteurs comparés soient dans le meme systeme de
        # coordonnees. Le vecteur du ticket est de-vigore par Shin, la cloture
        # ici par normalisation proportionnelle: les melanger reintroduirait
        # exactement la distorsion que ce bloc existe pour eviter.
        race_snaps = snapshots.get(key, [])
        usable = [
            snap for snap in race_snaps
            if set(common) <= set(map(str, snap["rapports"]))
            and all(float(snap["rapports"][str(n)]) > 1.0 for n in common)
        ]
        # Du plus loin du depart au plus proche.
        usable.sort(key=lambda snap: -float(snap["minutes_to_post"]))
        if usable:
            baseline_source = "journal_de_prix"
            p_market = proportional_probabilities(
                {int(n): float(usable[-1]["rapports"][str(n)]) for n in common}, order)
        else:
            baseline_source = "ticket_shin"
            p_market = np.asarray([float(market_raw[n]) for n in common], dtype=float)
            if p_market.sum() <= 0:
                skipped.append({"race_key": key, "reason": "vecteur marche degenere"})
                continue
            p_market = p_market / p_market.sum()
        disagreement = centred_log_ratio(p_model) - centred_log_ratio(p_market)
        movement = centred_log_ratio(p_close) - centred_log_ratio(p_market)

        # BASE PRECOCE INDEPENDANTE - correction du biais structurel de
        # l'estimateur simple: d et m partagent p_marche, donc tout bruit de
        # pool dans ce vecteur entre avec le meme signe dans les deux et
        # fabrique de la covariance meme quand le modele ne sait rien. Un
        # releve ANTERIEUR - le T-10 que le protocole impose deja - fournit une
        # base dont le bruit est independant de celui du snapshot principal.
        disagreement_early = None
        if len(usable) >= 2:
            p_early = proportional_probabilities(
                {int(n): float(usable[0]["rapports"][str(n)]) for n in common}, order)
            disagreement_early = (
                centred_log_ratio(p_model) - centred_log_ratio(p_early))
        observed_at = str(context.get("evaluated_at_utc") or "")
        rows.append({
            "race_key": key,
            "fill_mode": str(context.get("fill_mode") or "INCONNU"),
            "day": observed_at[:10] or key[:10],
            "n_runners": len(common),
            "n_scratched_after_snapshot": len(set(market_raw) - set(final)),
            "published_before_start": published,
            "minutes_to_post": context.get("minutes_to_post_at_snapshot"),
            "market_baseline_source": baseline_source,
            "d": disagreement,
            "m": movement,
            "d_early": disagreement_early,
        })

    if not rows:
        return {
            "schema_version": CLV_SCHEMA,
            "stake_version": STAKE_VERSION,
            "n_races": 0,
            "fitted": False,
            "n_skipped": len(skipped),
            "skipped": skipped[:20],
            "reading": (
                "aucune course exploitable: il faut, pour la MEME course, un ticket "
                "journalise et un rapport final au journal de prix"
            ),
            "governance": CLV_GOVERNANCE,
        }

    all_d = np.concatenate([row["d"] for row in rows])
    all_m = np.concatenate([row["m"] for row in rows])
    denominator = float(all_d @ all_d)
    if denominator <= 1e-12:
        return {
            "schema_version": CLV_SCHEMA,
            "stake_version": STAKE_VERSION,
            "n_races": len(rows),
            "fitted": False,
            "reason": "le modele n'a aucun desaccord avec le marche: beta indefini",
            "governance": CLV_GOVERNANCE,
        }
    beta = float(all_d @ all_m) / denominator

    # Bootstrap apparie par JOURNEE, comme partout ailleurs dans ce module: deux
    # courses du meme jour partagent le terrain, la meteo et la population de
    # parieurs, donc leurs residus ne sont pas independants.
    clusters: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        clusters.setdefault(row["day"], []).append(index)
    keys = sorted(clusters)
    rng = np.random.default_rng(seed)
    samples = np.empty(draws, dtype=float)
    for draw in range(draws):
        chosen = rng.choice(keys, size=len(keys), replace=True)
        picked = [i for key in chosen for i in clusters[str(key)]]
        d = np.concatenate([rows[i]["d"] for i in picked])
        m = np.concatenate([rows[i]["m"] for i in picked])
        bottom = float(d @ d)
        samples[draw] = float(d @ m) / bottom if bottom > 1e-12 else 0.0
    ci_low, ci_high = cluster_interval(beta, samples, len(keys))
    standard_error = float(samples.std(ddof=1))

    # PROJECTION DE PUISSANCE. A effet constant, combien de courses encore avant
    # que l'intervalle exclue zero ? C'est la seule facon de repondre a "ou en
    # suis-je" autrement que par un compteur aveugle.
    races_needed = None
    if standard_error > 1e-12 and abs(beta) > 1e-12:
        ratio = 1.96 * standard_error / abs(beta)
        if ratio > 1.0:
            races_needed = int(math.ceil(len(rows) * (ratio ** 2)))
        else:
            races_needed = len(rows)

    # ESTIMATEUR A BASES SEPAREES - le garde-fou contre notre propre biais.
    #
    # beta simple partage p_marche entre d et m. Si le marche du snapshot porte
    # du bruit de pool - et en parimutuel les pools precoces sont minces, donc
    # il en porte - alors ce bruit entre dans d avec le signe moins et dans m
    # avec le signe moins. Leur covariance est donc positive MEME SI le modele
    # ne sait strictement rien: beta est biaise VERS LE HAUT sous l'hypothese
    # nulle. Pour ce projet c'est le pire biais possible, celui qui annonce un
    # avantage inexistant.
    #
    # Correction standard, sans aucun parametre a choisir: prendre pour d une
    # base ANTERIEURE et independante - le releve T-10 que le protocole impose
    # deja - et garder m sur la base du snapshot principal.
    #
    #     beta_split = somme(d_precoce . m) / somme(d_precoce . d)
    #
    # Les bruits des deux releves etant independants, les termes croises
    # s'annulent en esperance et l'estimateur retrouve la meme grandeur que
    # beta - la part du desaccord que le marche adopte - sans le biais.
    split_rows = [row for row in rows if row["d_early"] is not None]
    split: dict[str, Any] = {
        "available": False,
        "reason": (
            "aucun snapshot de prix anterieur au snapshot d'evaluation. Relever "
            "systematiquement T-10 PUIS T-3, comme le protocole l'impose deja: "
            "sans base precoce independante, beta reste biaise vers le haut."
        ),
    }
    if split_rows:
        early_d = np.concatenate([row["d_early"] for row in split_rows])
        late_d = np.concatenate([row["d"] for row in split_rows])
        late_m = np.concatenate([row["m"] for row in split_rows])
        bottom = float(early_d @ late_d)
        if abs(bottom) > 1e-12:
            beta_split = float(early_d @ late_m) / bottom
            split_clusters: dict[str, list[int]] = {}
            for index, row in enumerate(split_rows):
                split_clusters.setdefault(row["day"], []).append(index)
            split_keys = sorted(split_clusters)
            rng_split = np.random.default_rng(seed + 1)
            split_samples = np.empty(draws, dtype=float)
            for draw in range(draws):
                chosen = rng_split.choice(split_keys, size=len(split_keys), replace=True)
                picked = [i for key in chosen for i in split_clusters[str(key)]]
                e = np.concatenate([split_rows[i]["d_early"] for i in picked])
                l = np.concatenate([split_rows[i]["d"] for i in picked])
                mm = np.concatenate([split_rows[i]["m"] for i in picked])
                den = float(e @ l)
                split_samples[draw] = float(e @ mm) / den if abs(den) > 1e-12 else 0.0
            split_low, split_high = cluster_interval(
                beta_split, split_samples, len(split_keys))
            split = {
                "available": True,
                "n_races": len(split_rows),
                "n_day_clusters": len(split_keys),
                "beta_split_baseline": round(beta_split, 5),
                "ci95_low": round(split_low, 5),
                "ci95_high": round(split_high, 5),
                "interval_method": (
                    "bootstrap par journee, corrige petit nombre de grappes "
                    "(Student G-1 et facteur racine(G/(G-1)))"
                ),
                "conclusive_at_95": bool(split_low > 0.0 or split_high < 0.0),
                "reading": (
                    "estimateur non biaise par le bruit de pool du snapshot. C'est "
                    "CELUI-CI qu'il faut croire quand les deux divergent: un "
                    "beta simple nettement superieur au beta a bases separees "
                    "signale que le premier lisait surtout du bruit de marche."
                ),
            }

    # LE PROTOCOLE COMPLET GAGNE-T-IL SON COUT ? (v1.6.1)
    #
    # Question centrale de ce projet, restee sans instrument: six versions ont
    # empile de la machinerie sans qu'on puisse dire si elle apporte quoi que ce
    # soit qu'un remplissage de trois minutes n'apporterait pas. On stratifie
    # donc beta par mode de remplissage. Si LITE et FULL portent le meme beta,
    # l'appareil ne gagne pas son cout - et c'est un resultat, pas un echec.
    #
    # CONFONDANT MAJEUR, et il interdit toute conclusion hative: les courses
    # traitees en FULL ne sont pas les memes que celles traitees en LITE. Tant
    # que les DEUX modes n'ont pas tourne sur les MEMES courses, l'ecart mesure
    # ici melange l'effet du protocole et le choix des courses. La commande
    # `challenge` du moteur existe pour cette comparaison appariee.
    by_fill: dict[str, Any] = {}
    for mode in sorted({row["fill_mode"] for row in rows}):
        subset = [row for row in rows if row["fill_mode"] == mode]
        sub_d = np.concatenate([row["d"] for row in subset])
        sub_m = np.concatenate([row["m"] for row in subset])
        bottom = float(sub_d @ sub_d)
        by_fill[mode] = {
            "n_races": len(subset),
            "beta": round(float(sub_d @ sub_m) / bottom, 5) if bottom > 1e-12 else None,
            "n_with_independent_baseline": sum(
                1 for row in subset if row["d_early"] is not None),
        }

    # Lecture legible: parmi les chevaux que le modele aime le PLUS par rapport
    # au marche, de combien le prix a-t-il bouge en leur faveur ?
    order = np.argsort(all_d)
    quintile = max(1, len(order) // 5)
    liked = order[-quintile:]
    disliked = order[:quintile]
    # LE VERDICT REFUSE, IL NE COMMENTE PAS (v1.6.1).
    #
    # beta simple est structurellement BIAISE VERS LE HAUT par le bruit de pool
    # partage entre d et m. Publier malgre tout un verdict assorti d'une mise en
    # garde, c'est publier un faux avantage que personne ne lira jusqu'au bout:
    # c'est precisement le mode d'echec que le reste du systeme evite en
    # REFUSANT le scellement plutot qu'en le commentant. On aligne donc la
    # mesure sur la meme regle.
    #
    # Sans base precoce independante, aucun verdict n'est rendu. beta simple
    # reste publie comme DIAGNOSTIC, jamais comme conclusion.
    #
    # Note negative, verifiee et consignee: un test de permutation ne repare PAS
    # ce biais. Permuter les vecteurs du modele entre courses injecte l'ecart
    # entre deux verites de course au denominateur et sous-estime le nul;
    # permuter les desaccords ramene le nul a zero. Dans les deux cas la
    # permutation casse l'appariement intra-course qui CREE le biais. Seule une
    # base reellement independante - le releve T-10 - le corrige.
    measurable = bool(split.get("available"))
    if measurable:
        verdict_low = float(split["ci95_low"])
        verdict_high = float(split["ci95_high"])
    else:
        verdict_low = verdict_high = 0.0
    conclusive = measurable and (verdict_low > 0.0 or verdict_high < 0.0)
    return {
        "schema_version": CLV_SCHEMA,
        "stake_version": STAKE_VERSION,
        "n_races": len(rows),
        "n_published_races": sum(1 for row in rows if row["published_before_start"]),
        "n_day_clusters": len(keys),
        "n_observations": int(all_d.size),
        "n_races_with_late_scratch": sum(
            1 for row in rows if row["n_scratched_after_snapshot"] > 0
        ),
        "n_skipped": len(skipped),
        "skipped": skipped[:20],
        "countable_only": bool(countable_only),
        "fitted": True,
        "beta_market_follows_model": round(beta, 5),
        "ci95_low": round(ci_low, 5),
        "ci95_high": round(ci_high, 5),
        "standard_error": round(standard_error, 5),
        "bootstrap_unit": "journee_de_course",
        "bootstrap_draws": draws,
        "conclusive_at_95": bool(conclusive),
        # Le verdict se lit sur l'estimateur NON BIAISE, jamais sur beta simple.
        "verdict": (
            "NON_MESURABLE_SANS_SNAPSHOT_PRECOCE" if not measurable else
            "LE_MODELE_ANTICIPE_LE_MARCHE" if verdict_low > 0.0 else
            "LE_MARCHE_CONTREDIT_LE_MODELE" if verdict_high < 0.0 else
            "INDECIS"
        ),
        "verdict_basis": (
            "estimateur a bases separees (non biaise)" if measurable else
            "aucun: sans releve de prix anterieur au snapshot d'evaluation, beta "
            "est biaise vers le haut et AUCUN verdict n'est rendu. Relever T-10 "
            "puis T-3, comme le protocole l'impose deja. beta simple ci-dessous "
            "est un diagnostic, pas une conclusion."
        ),
        "beta_is_biased_upward": not measurable,
        "races_needed_for_verdict_at_current_effect": races_needed,
        "split_baseline_estimator": split,
        "by_fill_mode": by_fill,
        "by_fill_mode_reading": (
            "beta par mode de remplissage. Un beta LITE comparable au beta FULL "
            "signifierait que le protocole complet n'apporte pas d'information "
            "que trois minutes de lecture n'apportent pas - ce serait un "
            "resultat exploitable, pas un echec. ATTENTION: tant que les deux "
            "modes n'ont pas tourne sur les MEMES courses, l'ecart melange "
            "l'effet du protocole et la selection des courses; utiliser "
            "`challenge` pour une comparaison appariee."
        ),
        "mean_move_on_most_liked": round(float(all_m[liked].mean()), 5),
        "mean_move_on_least_liked": round(float(all_m[disliked].mean()), 5),
        "legible_reading": (
            f"sur le cinquieme des partants que le modele prefere le plus au marche, "
            f"le prix a bouge de {all_m[liked].mean():+.3f} en log-probabilite d'ici "
            f"la cloture; sur le cinquieme qu'il rejette le plus, "
            f"{all_m[disliked].mean():+.3f}. Un ecart positif entre les deux signifie "
            f"que la monnaie tardive est allee dans notre sens."
        ),
        "reading": (
            "beta est la part de notre desaccord avec le marche du snapshot que le "
            "marche finit par ADOPTER avant la cloture. Positif et significatif: le "
            "modele contient une information que ce marche n'avait pas encore."
        ),
        "parimutuel_warning": (
            "EN PARIMUTUEL, UN BETA POSITIF N'EST PAS UN PROFIT. On est paye le "
            "rapport FINAL quel que soit le moment de la mise: avoir raison avant la "
            "cloture ne rapporte rien en soi. Pour gagner, l'information doit en plus "
            "depasser le prelevement (environ 15 % sur le simple, 36 % sur le trio). "
            "beta mesure si le modele SAIT quelque chose, pas s'il RAPPORTE."
        ),
        "confounders": [
            "bruit de pool partage: d et m sont tous deux mesures depuis le marche du "
            "snapshot, donc le bruit de ce marche gonfle beta meme sous l'hypothese "
            "nulle. C'est le biais le plus dangereux ici - il annonce un avantage "
            "inexistant. Lire beta a bases separees, qui en est immunise.",
            "diffusion lente d'une information publique: si le modele et la monnaie "
            "tardive reagissent au meme fait que le marche du snapshot n'avait pas "
            "encore integre, beta est positif sans aucune analyse superieure",
            "selection des courses: celles que l'operateur choisit d'analyser ne sont "
            "pas un echantillon aleatoire du programme",
            "un beta positif obtenu sur des courses EXPLORATOIRES n'est pas opposable: "
            "seule la sous-population publiee avant le depart l'est",
        ],
        "governance": CLV_GOVERNANCE,
    }


def market_template() -> dict[str, Any]:
    return {
        "schema_version": MARKET_SCHEMA,
        "race_key": "A_REMPLACER",
        "operator": "ZEturf",
        "observed_at": "YYYY-MM-DDTHH:MM:SS+02:00",
        "non_runners": [],
        "previous_snapshot": {
            "comment": "snapshot anterieur, ideal vers T-10; permet de mesurer la derive",
            "observed_at": "YYYY-MM-DDTHH:MM:SS+02:00",
            "rapports": {"1": 4.9, "2": 5.8, "3": 3.2},
        },
        "pools": {
            "SIMPLE_GAGNANT": {
                "is_probable_payout": False,
                "rapports": {"1": 4.5, "2": 6.2, "3": 3.1},
            },
            "SIMPLE_PLACE": {
                "is_probable_payout": False,
                "rapports": {"1": 1.8, "2": 2.3, "3": 1.5},
            },
            "TRIO": {
                "is_probable_payout": True,
                "comment": "rapport probable uniquement; le rapport final est inconnu avant cloture",
                "rapports": {"1-2-3": 28.0},
            },
        },
    }


def policy_template() -> dict[str, Any]:
    return {
        "schema_version": POLICY_SCHEMA,
        "takeout_attested": False,
        "attestation_note": (
            "passer takeout_attested a true seulement apres avoir verifie les taux de "
            "prelevement reels dans les conditions de l'operateur; les valeurs ci-dessous "
            "sont des ordres de grandeur publics 2024-2026, pas un engagement"
        ),
        "bankroll_eur": 1000.0,
        "kelly_fraction": 0.25,
        "max_stake_pct_per_bet": 0.02,
        "max_stake_pct_per_race": 0.05,
        "stake_rounding_eur": 1.0,
        "breakage_eur": 0.10,
        "max_minutes_to_post": 15.0,
        "max_stake_pct_per_day": 0.06,
        "max_bets_per_day": 6,
        "day_timezone": "Europe/Paris",
        "min_edge_simple": 0.05,
        "min_edge_exotic": 0.25,
        "takeout": dict(DEFAULT_TAKEOUT),
        "drift_model": {
            "fitted": False,
            "unfitted_penalty_per_10min": 0.06,
            "unfitted_max_penalty": 0.30,
            "exotic_penalty_multiplier": 1.5,
            "fitted_log_drift_q10": {"0": -0.01, "5": -0.03, "15": -0.08, "30": -0.15},
            "note": (
                "tant que fitted vaut false, une penalite forfaitaire penalisante est "
                "appliquee au rapport observe. Pour l'ajuster, journalisez pour chaque "
                "course le rapport vu a T-x et le rapport final, puis remplissez le "
                "quantile 10 % de log(final/observe) par tranche de minutes."
            ),
        },
    }


# ---------------------------------------------------------------------------
# 9. AUTOTESTS
# ---------------------------------------------------------------------------

def _synthetic_audits(
    n_races: int, edge: float, seed: int, *, informative: bool = True
) -> list[dict[str, Any]]:
    """Fabrique des audits au comportement connu.

    informative=True  : le modele observe la verite avec un bruit propre. Il
                        apporte donc une information INDEPENDANTE du marche, et
                        la calibration doit lui donner un poids non nul.
    informative=False : le modele est une COPIE DEGRADEE du marche. Il ne
                        contient aucune information au-dela du prix, et la
                        calibration doit ecraser son poids vers zero.

    Le second cas est le temoin negatif qui compte vraiment. Deux estimateurs
    bruites independants de la meme verite se completent toujours: tester
    l'absence d'avantage avec un tel montage donnerait un faux succes.
    """
    rng = np.random.default_rng(seed)
    audits = []
    for race in range(n_races):
        n = int(rng.integers(8, 15))
        numbers = list(range(1, n + 1))
        truth = rng.dirichlet(np.full(n, 0.9))
        noise_market = rng.normal(size=n) * 0.55
        p_market = truth * np.exp(noise_market)
        p_market /= p_market.sum()
        if informative:
            noise_model = rng.normal(size=n) * (0.55 * (1.0 - edge) + 1e-6)
            p_model = truth * np.exp(noise_model)
        else:
            p_model = p_market * np.exp(rng.normal(size=n) * 0.45)
        p_model /= p_model.sum()
        winner = int(numbers[rng.choice(n, p=truth)])
        scheduled = datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc) + timedelta(days=race)
        audit = {
            "schema_version": AUDIT_SCHEMA_CURRENT,
            "race_key": f"SYN-{race:05d}",
            "scheduled_start_utc": iso_utc(scheduled),
            "fill_mode": "FULL",
            "winner": winner,
            "prior_commitment": {
                "minutes_before_start": 12.0,
                "committed_at_utc": iso_utc(scheduled - timedelta(minutes=12)),
                "collection_integrity": "SEALED",
                "published": True,
                "publication_medium": "opentimestamps",
                "evidential_status": "PROBANTE",
            },
            "internal_model_probabilities": {str(numbers[i]): float(p_model[i]) for i in range(n)},
            "market_comparison": {
                "source": "SYNTHETIC_SELF_TEST",
                "internal_consensus": {
                    "values": {str(numbers[i]): float(p_market[i]) for i in range(n)}
                }
            },
        }
        audit["audit_sha256"] = sha256_obj(audit)
        audits.append(audit)
    return audits


def _synthetic_audits_with_drift(
    n_races: int, seed: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Courses ou la monnaie tardive porte une information reelle.

    La cote precoce est bruitee; la cote tardive se rapproche de la verite. La
    derive log(tardive/precoce) est donc informative, et la calibration doit
    retrouver un c strictement positif.
    """
    rng = np.random.default_rng(seed)
    audits, prices = [], []
    price_head: str | None = None
    for race in range(n_races):
        n = int(rng.integers(8, 14))
        numbers = list(range(1, n + 1))
        truth = rng.dirichlet(np.full(n, 0.9))
        early = truth * np.exp(rng.normal(size=n) * 0.70)
        early /= early.sum()
        late = truth * np.exp(rng.normal(size=n) * 0.30)
        late /= late.sum()
        model = truth * np.exp(rng.normal(size=n) * 0.60)
        model /= model.sum()
        winner = int(numbers[rng.choice(n, p=truth)])
        key = f"DRIFT-{race:05d}"
        scheduled = datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc) + timedelta(days=race)
        audit = {
            "schema_version": AUDIT_SCHEMA_CURRENT,
            "race_key": key, "winner": winner,
            "scheduled_start_utc": iso_utc(scheduled),
            "fill_mode": "FULL",
            "prior_commitment": {
                "minutes_before_start": 12.0,
                "committed_at_utc": iso_utc(scheduled - timedelta(minutes=12)),
                "collection_integrity": "SEALED",
                "published": True,
                "publication_medium": "opentimestamps",
                "evidential_status": "PROBANTE",
            },
            "internal_model_probabilities": {str(numbers[i]): float(model[i]) for i in range(n)},
            "market_comparison": {"source": "SYNTHETIC_SELF_TEST", "internal_consensus": {
                "values": {str(numbers[i]): float(early[i]) for i in range(n)}}},
        }
        audit["audit_sha256"] = sha256_obj(audit)
        audits.append(audit)
        # rapports bruts inverses des probabilites, marge 118 %
        early_odds = {str(numbers[i]): float(1.0 / (early[i] * 1.18)) for i in range(n)}
        late_odds = {str(numbers[i]): float(1.0 / (late[i] * 1.18)) for i in range(n)}
        final_odds = {str(numbers[i]): float(1.0 / (late[i] * 1.20)) for i in range(n)}
        record = price_record(
            key, iso_utc(scheduled - timedelta(minutes=12)), 12.0,
            early_odds, "snapshot", price_head
        )
        prices.append(record); price_head = record["record_sha256"]
        record = price_record(
            key, iso_utc(scheduled - timedelta(minutes=3)), 3.0,
            late_odds, "snapshot", price_head
        )
        prices.append(record); price_head = record["record_sha256"]
        record = price_record(
            key, iso_utc(scheduled + timedelta(minutes=1)), 0.0,
            final_odds, "final", price_head
        )
        prices.append(record); price_head = record["record_sha256"]
    return audits, prices


def staking_progress(
    tickets: list[dict[str, Any]], calibration: dict[str, Any] | None,
    price_log: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ou en est le systeme sur l'echelle, et ce qui debloque le palier suivant.

    Sans cette vue, l'utilisateur ne voit qu'une suite de refus. Avec elle, il
    voit un compteur qui avance. La difference est operationnelle, pas cosmetique:
    un compteur visible se remplit, un refus opaque se contourne.
    """
    verify_ticket_journal(tickets)
    if calibration is not None:
        verify_sealed_hash(calibration, "calibration_sha256", "CALIBRATION")
        if calibration.get("schema_version") != CALIBRATION_SCHEMA:
            raise StakeError(f"Calibration au schema {CALIBRATION_SCHEMA} requise")
    logged = len(tickets)
    positive = bool(calibration and calibration.get("usable_for_staking"))
    races_calibrated = int(calibration["n_races"]) if calibration else 0
    step = ladder_level(races_calibrated, positive)
    nxt = next((s for s in STAKING_LADDER if s["level"] == step["level"] + 1), None)
    decisions: dict[str, int] = {}
    for ticket in tickets:
        key = str(ticket.get("decision", "?"))
        decisions[key] = decisions.get(key, 0) + 1
    committed = sum(1 for t in tickets
                    if any(g["gate"] == "ENGAGEMENT_PUBLIE_AVANT_DEPART" and g["passed"]
                           for g in t.get("gates", [])))
    return {
        "stake_version": STAKE_VERSION,
        "tickets_logged": logged,
        "races_in_calibration": races_calibrated,
        "committed_before_start": committed,
        "commitment_coverage": round(committed / logged, 4) if logged else 0.0,
        "decisions": decisions,
        "current_level": step["level"],
        "current_mode": step["mode"],
        "max_stake_pct_per_bet": step["max_stake_pct_per_bet"],
        "positive_edge_established": positive,
        "next_unlock": step["unlock"],
        "races_to_next_level": (
            max(0, nxt["min_races"] - races_calibrated) if nxt else 0
        ),
        "edge_estimate": (calibration or {}).get("bootstrap"),
        # VALEUR DE CLOTURE (v1.6). Affichee ICI, sur le compteur que
        # l'operateur regarde deja, parce qu'elle repond des la premiere dizaine
        # de courses a la question a laquelle le compteur de calibration ne
        # repondra pas avant deux cents: le modele sait-il quelque chose ?
        # Elle n'ouvre aucune porte - voir CLV_GOVERNANCE.
        "closing_line_value": (
            closing_line_value(tickets, price_log) if price_log else {
                "fitted": False,
                "reading": (
                    "aucun journal de prix fourni: passer --prices PRICES.jsonl. "
                    "C'est la mesure la moins chere et la plus rapide du projet, et "
                    "elle ne demande qu'un rapport final journalise par course."
                ),
            }
        ),
        "reading": (
            "chaque course auditee et engagee avant le depart fait avancer le compteur; "
            "une course analysee mais non journalisee ne compte pas"
        ),
        "measurement_vs_permission": (
            "deux compteurs distincts et volontairement decouples: la CALIBRATION "
            "autorise (elle ouvre les paliers de mise, et exige 200 courses); la "
            "VALEUR DE CLOTURE mesure (elle tranche des la premiere dizaine, et "
            "n'autorise rien). Les confondre serait ouvrir une porte avec un "
            "instrument."
        ),
    }


def self_test() -> None:
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}

    # --- Shin -------------------------------------------------------------
    probabilities, z = shin_probabilities({1: 1.30, 2: 6.0, 3: 15.0, 4: 40.0})
    naive = {k: (1.0 / v) / sum(1.0 / x for x in {1: 1.30, 2: 6.0, 3: 15.0, 4: 40.0}.values())
             for k, v in {1: 1.30, 2: 6.0, 3: 15.0, 4: 40.0}.items()}
    assert abs(sum(probabilities.values()) - 1.0) < 1e-9
    assert probabilities[1] > naive[1] and probabilities[4] < naive[4] and z > 0
    checks["shin_devig"] = True

    # --- melange logit ----------------------------------------------------
    p_model = np.asarray([0.5, 0.3, 0.2])
    p_market = np.asarray([0.2, 0.3, 0.5])
    assert np.allclose(logit_blend(p_model, p_market, 0.0, 1.0), p_market)
    assert np.allclose(logit_blend(p_model, p_market, 1.0, 0.0), p_model)
    checks["logit_blend_endpoints"] = True

    # --- la calibration detecte un avantage reel --------------------------
    strong = build_calibration(
        _synthetic_audits(800, edge=0.70, seed=11),
        min_races=200, bootstrap_draws=1000,
    )
    assert strong["blend"]["a_model"] > 0.20
    assert strong["bootstrap"]["adds_information_at_95"] is True
    assert strong["usable_for_staking"] is True
    checks["calibration_detects_real_edge"] = True
    assert strong["n_train_races"] == 560 and strong["n_holdout_races"] == 240
    assert strong["bootstrap"]["evaluation"] == "HOLDOUT_TEMPOREL_PARAMETRES_GELES"
    checks["temporal_holdout_separates_fit_and_test"] = True
    details["strong_a_model"] = strong["blend"]["a_model"]

    # --- et refuse d'en inventer un absent --------------------------------
    # Temoin negatif: modele = copie degradee du marche, aucune information propre.
    noise = build_calibration(
        _synthetic_audits(600, edge=0.0, seed=12, informative=False),
        min_races=200, bootstrap_draws=1000,
    )
    assert noise["blend"]["a_model"] < 0.5 * strong["blend"]["a_model"], (
        f"poids modele trop eleve sans information propre: {noise['blend']['a_model']}"
    )
    assert noise["usable_for_staking"] is False, (
        "un modele sans information propre ne doit jamais etre declare exploitable"
    )
    checks["calibration_rejects_absent_edge"] = True
    details["a_model_informative_vs_parrot"] = [
        strong["blend"]["a_model"], noise["blend"]["a_model"]
    ]

    # --- echantillon insuffisant bloque -----------------------------------
    small = build_calibration(
        _synthetic_audits(40, edge=0.55, seed=13),
        min_races=200, bootstrap_draws=1000,
    )
    assert small["usable_for_staking"] is False
    assert any("insuffisant" in reason for reason in small["blocking_reasons"])
    checks["small_sample_blocked"] = True

    # --- v1.5 : le denominateur ne se remplit que de courses opposables -------
    # Condition degeneree exercee pour de vrai: un lot qui passait la
    # calibration en 1.4 doit desormais etre integralement rejete des lors que
    # les courses ne sont pas publiquement anterieures ET etanches au marche.
    probative = _synthetic_audits(240, edge=0.55, seed=21)

    def refuse_calibration(mutation, label: str) -> str:
        try:
            build_calibration(mutation, min_races=200, bootstrap_draws=1000)
        except StakeError as exc:
            return str(exc)
        raise AssertionError(f"Refus attendu non declenche: {label}")

    exploratory = json.loads(json.dumps(probative))
    for audit in exploratory:
        audit["prior_commitment"]["published"] = False
        audit["prior_commitment"]["evidential_status"] = "EXPLORATOIRE"
        audit.pop("audit_sha256", None)
        audit["audit_sha256"] = sha256_obj(audit)
    message = refuse_calibration(exploratory, "lot entierement exploratoire")
    assert "course_exploratoire_non_opposable" in message

    legacy = json.loads(json.dumps(probative))
    for audit in legacy:
        audit["prior_commitment"].pop("evidential_status", None)
        audit.pop("audit_sha256", None)
        audit["audit_sha256"] = sha256_obj(audit)
    message = refuse_calibration(legacy, "lot au schema anterieur a 11.6")
    assert "audit_sans_statut_probant_schema_anterieur_a_11_6" in message

    # Le lot probant, lui, passe: le durcissement ne casse pas le chemin normal.
    still_works = build_calibration(probative, min_races=200, bootstrap_draws=1000)
    assert still_works["n_train_races"] > 0
    checks["only_provable_races_count"] = True

    # --- derive parimutuelle ----------------------------------------------
    policy = policy_template()
    policy["takeout_attested"] = True
    near = drift_quantile(policy, 3.0, "SIMPLE_GAGNANT")
    far = drift_quantile(policy, 30.0, "SIMPLE_GAGNANT")
    exotic = drift_quantile(policy, 30.0, "TRIO")
    assert 0.0 < far < near <= 1.0 and exotic < far
    checks["drift_penalises_distant_snapshots"] = True
    details["drift_T3_vs_T30"] = [round(near, 4), round(far, 4)]

    # --- Kelly ------------------------------------------------------------
    fractions = multi_outcome_kelly(np.asarray([0.50]), np.asarray([3.0]))
    assert abs(float(fractions[0]) - 1.0 / 3.0) < 1e-9
    zero = multi_outcome_kelly(np.asarray([0.20]), np.asarray([3.0]))
    assert float(zero[0]) < 1e-12
    # Verification numerique des conditions du premier ordre sur un cas a deux
    # chevaux: la solution fermee doit etre un maximum local de la croissance
    # logarithmique attendue.
    probabilities = np.asarray([0.35, 0.30])
    decimal = np.asarray([4.0, 5.0])
    pair = multi_outcome_kelly(probabilities, decimal - 1.0)

    def growth(f: np.ndarray) -> float:
        total = float(f.sum())
        if total >= 1.0 or np.any(f < 0):
            return -1e18
        value = float((1.0 - probabilities.sum()) * math.log(1.0 - total))
        for i in range(len(f)):
            value += float(probabilities[i]) * math.log(1.0 - total + float(f[i]) * float(decimal[i]))
        return value

    base = growth(pair)
    for i in range(2):
        for delta in (-0.01, -0.002, 0.002, 0.01):
            perturbed = pair.copy()
            perturbed[i] = max(0.0, perturbed[i] + delta)
            assert growth(perturbed) <= base + 1e-12, "solution Kelly non optimale"
    checks["kelly_matches_closed_form"] = True
    checks["kelly_first_order_conditions"] = True
    details["kelly_p50_at_4.0"] = round(float(fractions[0]), 6)
    details["kelly_pair_fractions"] = [round(float(x), 5) for x in pair]

    # --- politique : Kelly plein refuse ------------------------------------
    reckless = policy_template()
    reckless["takeout_attested"] = True
    reckless["kelly_fraction"] = 1.0
    try:
        validate_policy(reckless)
    except StakeError:
        checks["full_kelly_refused"] = True
    else:
        raise AssertionError("Un Kelly plein aurait du etre refuse")

    unattested = policy_template()
    try:
        validate_policy(unattested)
    except StakeError:
        checks["takeout_attestation_required"] = True
    else:
        raise AssertionError("Un prelevement non atteste aurait du etre refuse")

    # --- coherence des prelevements ---------------------------------------
    assert DEFAULT_TAKEOUT["TRIO"] > DEFAULT_TAKEOUT["JUMELE_GAGNANT"] > DEFAULT_TAKEOUT["SIMPLE_GAGNANT"]
    checks["takeout_ordering_sane"] = True

    # --- NOUVEAU 1.1 : rupture (breakage) ---------------------------------
    assert abs(apply_breakage(2.39, 0.10) - 2.30) < 1e-9
    assert abs(apply_breakage(2.30, 0.10) - 2.30) < 1e-9
    assert abs(apply_breakage(11.97, 0.10) - 11.90) < 1e-9
    # Sur un favori, la rupture mange une part non negligeable de l'esperance.
    loss_pct = (2.39 - apply_breakage(2.39, 0.10)) / 2.39
    assert loss_pct > 0.03
    checks["breakage_applied"] = True
    details["breakage_loss_on_2.39"] = round(loss_pct, 4)

    # --- NOUVEAU 1.1 : signal de derive -----------------------------------
    p_m = np.asarray([0.34, 0.33, 0.33])
    # cheval 1 soutenu (cote raccourcit), cheval 3 lache
    drift = np.asarray([-0.20, 0.0, 0.20])
    with_drift = logit_blend(p_m, p_m, 0.5, 0.5, drift, 1.0)
    assert with_drift[0] > p_m[0] and with_drift[2] < p_m[2], "signe de derive inverse"
    assert np.allclose(logit_blend(p_m, p_m, 0.5, 0.5, drift, 0.0), p_m)
    checks["drift_signal_direction"] = True
    details["drift_effect_on_supported"] = round(float(with_drift[0] - p_m[0]), 5)

    # --- NOUVEAU 1.1 : la calibration retrouve un c connu -----------------
    audits, prices = _synthetic_audits_with_drift(320, seed=21)
    calibrated_drift = build_calibration(
        audits, min_races=200, price_log=prices, bootstrap_draws=1000
    )
    assert (
        calibrated_drift["n_train_races_with_drift"]
        == calibrated_drift["n_train_races"]
    )
    assert calibrated_drift["blend"]["drift_fitted"] is True
    assert calibrated_drift["blend"]["c_drift"] > 0.15, calibrated_drift["blend"]
    checks["calibration_recovers_drift_signal"] = True
    details["c_drift_recovered"] = calibrated_drift["blend"]["c_drift"]

    # sans journal de prix, c reste a zero
    no_prices = build_calibration(audits, min_races=200, bootstrap_draws=1000)
    assert no_prices["blend"]["c_drift"] == 0.0 and no_prices["blend"]["drift_fitted"] is False
    checks["drift_zero_without_price_log"] = True

    # --- NOUVEAU 1.1 : ajustement du modele de derive ---------------------
    drift_model = fit_drift_model(prices, min_observations=50)
    assert drift_model["drift_model"]["fitted"] is True, drift_model
    assert drift_model["unused_buckets"] == ["15.0", "30.0"], drift_model["unused_buckets"]
    values = list(drift_model["drift_model"]["fitted_log_drift_q10"].values())
    assert all(v < 0 for v in values), "le quantile 10 % doit etre negatif"
    thin = fit_drift_model(prices[:20], min_observations=500)
    assert thin["drift_model"]["fitted"] is False
    checks["drift_model_fitting"] = True
    tampered_prices = json.loads(json.dumps(prices))
    tampered_prices[0]["rapports"][next(iter(tampered_prices[0]["rapports"]))] *= 1.01
    try:
        fit_drift_model(tampered_prices, min_observations=50)
    except StakeError:
        checks["price_journal_tamper_detected"] = True
    else:
        raise AssertionError("Une alteration du journal de prix aurait du etre detectee")

    # Un horizon jamais releve retombe sur la penalite forfaitaire au lieu
    # d'extrapoler une derive inexistante.
    fitted_policy = policy_template()
    fitted_policy["takeout_attested"] = True
    fitted_policy["drift_model"] = drift_model["drift_model"]
    near_fitted = drift_quantile(fitted_policy, 3.0, "SIMPLE_GAGNANT")
    far_uncovered = drift_quantile(fitted_policy, 45.0, "SIMPLE_GAGNANT")
    assert 0.0 < near_fitted <= 1.0 and far_uncovered < near_fitted
    checks["uncovered_horizon_falls_back"] = True
    details["fitted_drift_q10"] = drift_model["drift_model"]["fitted_log_drift_q10"]
    details["drift_factor_T3_fitted_vs_T45_uncovered"] = [
        round(near_fitted, 4), round(far_uncovered, 4)
    ]

    # --- NOUVEAU 1.2 : echelle graduee ------------------------------------
    assert ladder_level(0, False)["mode"] == "PAPIER"
    assert ladder_level(60, False)["level"] == 1
    # Sans avantage etabli, le compteur seul ne debloque jamais l'argent reel.
    assert ladder_level(500, False)["max_stake_pct_per_bet"] == 0.0
    assert ladder_level(250, True)["mode"] == "MICRO"
    assert ladder_level(1000, True)["level"] == 4
    # Le risque est monotone croissant avec la preuve.
    caps = [ladder_level(n, True)["max_stake_pct_per_bet"]
            for n in (0, 60, 250, 600, 1000)]
    assert caps == sorted(caps) and caps[0] == 0.0 and caps[-1] == 0.02
    # Les combines ne s'ouvrent qu'au niveau nominal.
    assert "TRIO" not in ladder_level(600, True)["pools"]
    assert "TRIO" in ladder_level(1000, True)["pools"]
    checks["ladder_monotone_and_gated"] = True
    checks["ladder_requires_edge_not_just_volume"] = True
    details["ladder_caps_by_races"] = caps

    # --- NOUVEAU 1.3 : plafond journalier ---------------------------------
    def fake_ticket(index: int, previous: str | None, decision: str = "TICKET") -> dict[str, Any]:
        item = {
            "schema_version": TICKET_SCHEMA,
            "stake_version": STAKE_VERSION,
            "prev_ticket_sha256": previous,
            "decision": decision,
            "context": {
                "race_key": f"DAY-{index}",
                "evaluated_at_utc": f"2026-09-03T12:{index:02d}:00+00:00",
            },
            "total_stake_pct_bankroll": 0.005 if decision == "TICKET" else 0.0,
            "bets": ([{"pool": "SIMPLE_GAGNANT"}] if decision == "TICKET" else []),
            "gates": [],
        }
        item["ticket_sha256"] = sha256_obj(item)
        return item

    prior = []
    previous = None
    for index in range(5):
        item = fake_ticket(index, previous)
        prior.append(item)
        previous = item["ticket_sha256"]
    used = day_exposure(
        prior, datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc), "Europe/Paris"
    )
    assert used["pct_bankroll_committed"] == 0.025 and used["bets_placed"] == 5
    checks["daily_exposure_accumulates"] = True
    try:
        verify_ticket_journal(prior[1:])
    except StakeError:
        checks["ticket_journal_deletion_detected"] = True
    else:
        raise AssertionError("La suppression du debut du journal aurait du casser la chaine")

    # Une course risquee ne bloque pas la mesure, mais interdit l'argent.
    risky_state = {
        "calibration_races": 400, "calibration_positive_edge": True,
        "race_risk_grade": "U3", "blocking_gates_passed": True,
        "graduating_gates_passed": True,
        "numbers": [1, 2], "p_final": np.asarray([0.6, 0.4]),
        "p_market": np.asarray([0.5, 0.5]), "p_model": np.asarray([0.6, 0.4]),
        "events": {"SIMPLE_GAGNANT": {"1": 0.60, "2": 0.40}}, "minutes_to_post": 3.0,
        "gates": [], "context": {
            "race_key": "CURRENT",
            "evaluated_at_utc": "2026-09-03T13:00:00+00:00",
        }, "all_gates_passed": True,
    }
    risky_market = {"pools": {"SIMPLE_GAGNANT": {
        "is_probable_payout": False, "rapports": {"1": 2.60, "2": 3.60}}}}
    risky_policy = policy_template(); risky_policy["takeout_attested"] = True
    risky_ticket = build_ticket(risky_state, risky_market, risky_policy)
    assert risky_ticket["ladder"]["forced_paper_by_race_risk"] is True
    assert risky_ticket["decision"] in {"PAPIER", "NO_BET"}
    assert risky_ticket["total_stake_eur"] == 0.0
    safe_ticket = build_ticket({**risky_state, "race_risk_grade": "U1"}, risky_market, risky_policy)
    assert safe_ticket["decision"] == "TICKET" and safe_ticket["total_stake_eur"] > 0
    checks["risky_race_logged_but_never_paid"] = True

    capped = build_ticket(
        {**risky_state, "race_risk_grade": "U1"}, risky_market, risky_policy,
        day_tickets=prior,
    )
    assert len(capped["bets"]) == 1
    assert capped["day_exposure"]["bets_placed_after"] == 6
    checks["daily_bet_count_cap_enforced"] = True

    invalid_day = policy_template(); invalid_day["takeout_attested"] = True
    invalid_day["max_stake_pct_per_day"] = -0.01
    try:
        validate_policy(invalid_day)
    except StakeError:
        checks["invalid_daily_policy_rejected"] = True
    else:
        raise AssertionError("Un plafond journalier negatif aurait du etre refuse")

    progress = staking_progress([], None)
    assert progress["current_level"] == 0 and progress["races_to_next_level"] == 50
    checks["progress_reports_next_unlock"] = True

    # --- v1.6 VALEUR DE CLOTURE -----------------------------------------------
    # On fabrique un couple (journal de tickets, journal de prix) ou le marche
    # final se deplace VERS l'opinion du modele, puis un ou le desaccord est du
    # pur bruit. L'instrument doit trancher dans le premier cas et rester
    # indecis dans le second.
    def clv_fixture(
        n_races: int, follow: float, *, field: int = 8, scratch_on: set[int] | None = None,
        noise: float = 0.06, seed: int = 4242, pool_noise: float = 0.0,
        with_early_snapshot: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        generator = np.random.default_rng(seed)
        tickets_out: list[dict[str, Any]] = []
        prices_out: list[dict[str, Any]] = []
        previous_ticket: str | None = None
        previous_price: str | None = None
        scratch_on = scratch_on or set()
        for race in range(n_races):
            numbers = list(range(1, field + 1))
            base = generator.normal(0.0, 0.7, field)
            truth = np.exp(base) / np.exp(base).sum()
            # Le marche OBSERVE porte du bruit de pool autour du consensus vrai.
            # A pool_noise = 0 on retrouve le cas simple: marche == verite.
            late_log = np.log(truth) + generator.normal(0.0, pool_noise, field)
            market = np.exp(late_log) / np.exp(late_log).sum()
            early_log = np.log(truth) + generator.normal(0.0, pool_noise, field)
            early = np.exp(early_log) / np.exp(early_log).sum()
            # Desaccord du modele, centre pour ne pas biaiser la composition.
            # Il porte sur la VERITE, pas sur le marche observe.
            disagreement = generator.normal(0.0, 0.45, field)
            disagreement -= disagreement.mean()
            model_log = np.log(truth) + disagreement
            model = np.exp(model_log) / np.exp(model_log).sum()
            # Le marche final adopte une fraction `follow` du desaccord.
            close_log = np.log(truth) + follow * disagreement + generator.normal(
                0.0, noise, field
            )
            close = np.exp(close_log) / np.exp(close_log).sum()
            keep = [i for i in range(field) if not (race in scratch_on and i == field - 1)]
            # Les rapports finaux portent la marge de l'operateur (TRJ ~ 85 %).
            rapports = {
                str(numbers[i]): float(0.85 / max(close[i], 1e-6)) for i in keep
            }
            # Trois courses par journee, horodatage STRICTEMENT croissant: les
            # deux journaux sont chaines et refusent tout desordre chronologique.
            day = f"2026-09-{(race // 3) + 1:02d}"
            stamp = f"{day}T12:{(race % 3) * 10:02d}:00+00:00"
            ticket = {
                "schema_version": TICKET_SCHEMA,
                "stake_version": STAKE_VERSION,
                "prev_ticket_sha256": previous_ticket,
                "decision": "PAPIER",
                "context": {
                    "race_key": f"CLV-{race}",
                    "evaluated_at_utc": stamp,
                    "entry_sha256": "e" * 64,
                    "minutes_to_post_at_snapshot": 3.0,
                },
                "gates": [{"gate": "ENGAGEMENT_PUBLIE_AVANT_DEPART", "passed": True,
                           "detail": "synthetique", "kind": "GRADUANTE"}],
                "bets": [],
                "total_stake_pct_bankroll": 0.0,
                "probabilities_model_raw": {
                    str(numbers[i]): round(float(model[i]), 6) for i in range(field)
                },
                "probabilities_market_shin": {
                    str(numbers[i]): round(float(market[i]), 6) for i in range(field)
                },
            }
            ticket["ticket_sha256"] = sha256_obj(ticket)
            tickets_out.append(ticket)
            previous_ticket = ticket["ticket_sha256"]
            # Le protocole impose DEUX releves, T-10 puis T-3. Le plus tardif
            # sert de base au mouvement, le plus precoce de base independante.
            snapshot_plan = ([(10.0, early), (3.0, market)] if with_early_snapshot
                             else [(3.0, market)])
            for snap_minutes, snap_vector in snapshot_plan:
                snap_rapports = {
                    str(numbers[i]): float(0.85 / max(snap_vector[i], 1e-6))
                    for i in keep
                }
                snap = price_record(
                    f"CLV-{race}", stamp, snap_minutes, snap_rapports, "snapshot",
                    previous_price,
                )
                prices_out.append(snap)
                previous_price = snap["record_sha256"]
            record = price_record(
                f"CLV-{race}", stamp, 0.0, rapports, "final", previous_price
            )
            prices_out.append(record)
            previous_price = record["record_sha256"]
        return tickets_out, prices_out

    followed_tickets, followed_prices = clv_fixture(60, follow=0.55)
    followed = closing_line_value(followed_tickets, followed_prices, draws=400)
    assert followed["fitted"] is True
    assert followed["n_races"] == 60
    # Le marche adopte 55 % du desaccord: beta doit s'en approcher.
    assert 0.35 < followed["beta_market_follows_model"] < 0.75, followed
    assert followed["ci95_low"] > 0.0
    # SANS releve precoce, aucun verdict n'est rendu: beta simple est biaise
    # vers le haut et le module REFUSE de conclure au lieu de commenter.
    assert followed["verdict"] == "NON_MESURABLE_SANS_SNAPSHOT_PRECOCE"
    assert followed["conclusive_at_95"] is False
    assert followed["beta_is_biased_upward"] is True
    # AVEC releve precoce, le meme signal doit etre conclu.
    early_t, early_p = clv_fixture(60, follow=0.55, with_early_snapshot=True, seed=31)
    early = closing_line_value(early_t, early_p, draws=400)
    assert early["verdict"] == "LE_MODELE_ANTICIPE_LE_MARCHE", early["verdict"]
    assert early["conclusive_at_95"] is True
    assert early["beta_is_biased_upward"] is False
    # Lecture legible: les chevaux preferes doivent raccourcir plus que les autres.
    assert followed["mean_move_on_most_liked"] > followed["mean_move_on_least_liked"]
    checks["clv_detects_anticipated_move"] = True

    # Desaccord pur bruit: l'instrument doit rester INDECIS, sans quoi il
    # trouverait un avantage a tout le monde.
    noise_tickets, noise_prices = clv_fixture(60, follow=0.0, seed=99)
    noisy = closing_line_value(noise_tickets, noise_prices, draws=400)
    assert noisy["ci95_low"] <= 0.0 <= noisy["ci95_high"], noisy
    assert noisy["conclusive_at_95"] is False
    # La projection de puissance doit demander PLUS de courses qu'on n'en a.
    assert (noisy["races_needed_for_verdict_at_current_effect"] is None
            or noisy["races_needed_for_verdict_at_current_effect"] > noisy["n_races"])
    checks["clv_indecisive_on_pure_noise"] = True

    # INTERACTION: non-partant entre le snapshot et la cloture. Le cheval retire
    # disparait des rapports finaux et le pool est recalcule. Comparer un
    # vecteur a huit chevaux avec un vecteur a sept fabriquerait un mouvement
    # entierement fictif; on doit restreindre a l'intersection et renormaliser.
    scratched_tickets, scratched_prices = clv_fixture(
        60, follow=0.55, scratch_on=set(range(0, 60, 2))
    )
    scratched = closing_line_value(scratched_tickets, scratched_prices, draws=400)
    assert scratched["n_races"] == 60
    assert scratched["n_races_with_late_scratch"] == 30
    # Le beta doit rester du meme ordre: un retrait ne cree pas de signal.
    assert 0.30 < scratched["beta_market_follows_model"] < 0.80, scratched
    assert abs(scratched["beta_market_follows_model"]
               - followed["beta_market_follows_model"]) < 0.25
    checks["clv_survives_late_scratch"] = True

    # LA RESERVE. La valeur de cloture est un instrument de MESURE: elle ne doit
    # ouvrir aucune porte. Un beta ecrasant laisse le palier 0 a zero euro.
    before = staking_progress(followed_tickets, None)
    after = staking_progress(followed_tickets, None, followed_prices)
    assert after["closing_line_value"]["ci95_low"] > 0.0
    for field_name in ("current_level", "current_mode", "max_stake_pct_per_bet",
                       "positive_edge_established", "races_to_next_level"):
        assert before[field_name] == after[field_name], field_name
    assert after["current_level"] == 0
    assert after["max_stake_pct_per_bet"] == 0.0
    checks["clv_never_opens_a_gate"] = True

    # Journaux exiges intacts: une ligne de prix modifiee doit faire echouer la
    # mesure, pas produire un beta silencieusement faux.
    tampered = json.loads(json.dumps(followed_prices))
    tampered[10]["rapports"] = {k: v * 2.0 for k, v in tampered[10]["rapports"].items()}
    try:
        closing_line_value(followed_tickets, tampered, draws=50)
    except StakeError:
        pass
    else:
        raise AssertionError("Un journal de prix altere aurait du faire echouer la CLV")
    # Une course sans rapport final est ECARTEE et comptee, jamais devinee.
    # On coupe le journal de prix aux 20 premieres courses. La fixture emet un
    # snapshot T-3 puis un rapport final par course, donc deux enregistrements.
    kept_races = {f"CLV-{i}" for i in range(20)}
    partial = closing_line_value(
        followed_tickets,
        [rec for rec in followed_prices if rec["race_key"] in kept_races],
        draws=100,
    )
    assert partial["n_races"] == 20 and partial["n_skipped"] == 40, (
        partial["n_races"], partial["n_skipped"])
    # Aucun couple exploitable: sortie honnete, pas un beta invente.
    empty = closing_line_value(followed_tickets, [], draws=50)
    assert empty["fitted"] is False and empty["n_races"] == 0
    checks["clv_requires_intact_journals"] = True

    # LE BIAIS QUE L'INSTRUMENT SIMPLE PORTE, ET SA CORRECTION.
    #
    # d et m sont tous deux mesures depuis le marche du snapshot. Tout bruit de
    # pool dans ce vecteur entre donc avec le meme signe dans les deux et
    # fabrique de la covariance MEME QUAND LE MODELE NE SAIT RIEN. Ici le modele
    # ne sait strictement rien - follow = 0 - et le marche observe porte du
    # bruit: beta simple doit se declarer positif, ce qui serait un faux
    # avantage, et l'estimateur a bases separees doit le refuser.
    biased_tickets, biased_prices = clv_fixture(
        90, follow=0.0, pool_noise=0.30, with_early_snapshot=True, seed=515,
    )
    biased = closing_line_value(biased_tickets, biased_prices, draws=400)
    assert biased["fitted"] is True
    # Le biais est REEL et se voit: l'estimateur naif annonce un avantage.
    assert biased["beta_market_follows_model"] > 0.10, biased["beta_market_follows_model"]
    assert biased["ci95_low"] > 0.0, "le biais de base partagee devrait etre visible"
    # L'estimateur a bases separees doit ramener beta vers zero.
    corrected = biased["split_baseline_estimator"]
    assert corrected["available"] is True
    assert corrected["n_races"] == 90
    assert abs(corrected["beta_split_baseline"]) < abs(
        biased["beta_market_follows_model"]
    ), corrected
    assert corrected["ci95_low"] <= 0.0 <= corrected["ci95_high"], corrected
    assert corrected["conclusive_at_95"] is False
    # Et surtout: le VERDICT publie suit l'estimateur corrige, pas beta simple.
    # Un faux avantage de 0,32 ne doit jamais sortir en conclusion.
    assert biased["verdict"] == "INDECIS", biased["verdict"]
    assert biased["conclusive_at_95"] is False
    # Stratification par mode de remplissage: la question "l'appareil gagne-t-il
    # son cout" doit etre instrumentee, meme si elle reste sans reponse ici.
    assert set(biased["by_fill_mode"]) == {"INCONNU"}
    assert biased["by_fill_mode"]["INCONNU"]["n_races"] == 90
    # Et sur un VRAI signal, l'estimateur corrige doit continuer a conclure.
    real_tickets, real_prices = clv_fixture(
        90, follow=0.55, pool_noise=0.30, with_early_snapshot=True, seed=707,
    )
    real = closing_line_value(real_tickets, real_prices, draws=400)
    real_split = real["split_baseline_estimator"]
    assert real_split["available"] is True
    assert real_split["ci95_low"] > 0.0, real_split
    # Sans snapshot precoce, l'estimateur corrige n'est pas disponible et le dit
    # au lieu de laisser croire que beta simple suffit.
    assert followed["split_baseline_estimator"]["available"] is False
    checks["clv_split_baseline_removes_shared_noise_bias"] = True

    # DEFAUT TROUVE EN AUDIT v1.6.1 : UN DEPLACEMENT NE SE MESURE PAS AVEC SHIN.
    #
    # Shin reste un estimateur de probabilite defendable - il corrige un biais
    # favori-outsider reel - et il est conserve partout ailleurs. Mais sa
    # correction depend du NIVEAU de cote et son z est reestime livre par livre:
    # elle ne s'annule donc pas dans une difference entre deux instants. Le
    # residu est correle a ce qu'on mesure, et il fabriquait un beta NEGATIF
    # declare significatif sur des donnees ou le modele ne sait rien.
    # On construit le livre comme le parimutuel le construit REELLEMENT:
    # rapport_i = (1 - prelevement) / p_i, donc booksum = 1/(1 - prelevement).
    truth = {1: 0.40, 2: 0.25, 3: 0.18, 4: 0.11, 5: 0.06}
    books = {t: {k: (1.0 - t) / v for k, v in truth.items()}
             for t in (0.15, 0.25, 0.36)}
    reference = centred_log_ratio(
        np.asarray([truth[k] for k in sorted(truth)], dtype=float))
    for takeout, book in books.items():
        # La majoration uniforme DISPARAIT exactement en log-ratio centre,
        # quel que soit le taux: 15 % comme 36 %.
        assert float(np.abs(
            centred_log_ratio(proportional_probabilities(book)) - reference
        ).max()) < 1e-12, takeout
        # Shin, lui, ne conserve PAS ces coordonnees. C'est la distorsion qui,
        # appliquee a deux releves d'instants differents, ne s'annulait pas.
        shin_map, shin_z = shin_probabilities(book)
        shin_vector = np.asarray(
            [shin_map[k] for k in sorted(book)], dtype=float)
        assert shin_z > 0.0, takeout
        assert float(np.abs(
            centred_log_ratio(shin_vector) - reference).max()) > 0.02, takeout
    # Consequence de bout en bout: sur du BRUIT PUR, l'estimateur doit etre
    # CENTRE SUR ZERO. On teste l'absence de biais - propriete stable - et non
    # le verdict d'une graine particuliere: a 95 %, exiger l'indecision sur
    # trois graines fixes echouerait une fois sur sept par construction. Le
    # taux de rejet reel est mesure dans l'audit, qui peut se le permettre.
    null_betas = []
    for seed in range(40, 52):
        null_t, null_p = clv_fixture(
            60, follow=0.0, with_early_snapshot=True, seed=seed)
        null_betas.append(closing_line_value(
            null_t, null_p, draws=200)["split_baseline_estimator"]
            ["beta_split_baseline"])
    spread = float(np.std(null_betas, ddof=1)) / math.sqrt(len(null_betas))
    assert abs(float(np.mean(null_betas))) < 4.0 * spread, (
        "l'estimateur a bases separees doit etre centre sur zero sous "
        f"hypothese nulle: moyenne {np.mean(null_betas):+.5f}")
    checks["clv_parimutuel_devig_is_proportional"] = True

    # L'INTERVALLE DOIT ETRE HONNETE, PAS SEULEMENT LE POINT.
    # Mesure sous hypothese nulle: le bootstrap par percentiles rejetait 11,7 %
    # du temps pour un nominal de 5 %, avec une vingtaine de journees. La
    # correction d'inference groupee ramene la mesure a 3,3 %.
    draw_sample = np.random.default_rng(7).normal(0.0, 0.02, 2000)
    for clusters in (5, 10, 20, 40):
        low, high = cluster_interval(0.0, draw_sample, clusters)
        percentile_half = 1.96 * float(draw_sample.std(ddof=1))
        assert (high - low) / 2.0 > percentile_half, clusters
    # Moins il y a de grappes, plus l'intervalle doit etre large.
    widths = [cluster_interval(0.0, draw_sample, g)[1] for g in (5, 10, 20, 40)]
    assert widths[0] > widths[1] > widths[2] > widths[3]
    # Une seule grappe ne permet AUCUNE inference.
    assert cluster_interval(0.0, draw_sample, 1) == (float("-inf"), float("inf"))
    checks["clv_interval_corrected_for_few_clusters"] = True

    print(json.dumps(
        {
            "status": "PASS",
            "stake_version": STAKE_VERSION,
            "tests": checks,
            "details": details,
            "warning": "tests synthetiques uniquement; aucune validation predictive reelle",
        }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 10. LIGNE DE COMMANDE
# ---------------------------------------------------------------------------

def write_json(path: str | os.PathLike[str], value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                      encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TITAN STAKE v1.4 - couche de mise")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    template = sub.add_parser("template")
    template.add_argument("kind", choices=("market", "policy"))
    template.add_argument("-o", "--output", required=True)
    calibrate = sub.add_parser("calibrate")
    calibrate.add_argument("audits", nargs="+")
    calibrate.add_argument("-o", "--output", required=True)
    calibrate.add_argument("--min-races", type=int, default=200)
    calibrate.add_argument("--prices", default=None)
    logprice = sub.add_parser("logprice")
    logprice.add_argument("--journal", required=True)
    logprice.add_argument("--race-key", required=True, dest="race_key")
    logprice.add_argument("--observed-at", required=True, dest="observed_at")
    logprice.add_argument("--minutes-to-post", type=float, required=True, dest="minutes")
    logprice.add_argument("--kind", choices=("snapshot", "final"), default="snapshot")
    logprice.add_argument("--market", default=None)
    logprice.add_argument("--rapports", default=None)
    fitdrift = sub.add_parser("fit-drift")
    fitdrift.add_argument("journal")
    fitdrift.add_argument("-o", "--output", required=True)
    fitdrift.add_argument("--min-observations", type=int, default=200)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("report")
    evaluate.add_argument("--samples", required=True)
    evaluate.add_argument("--market", required=True)
    evaluate.add_argument("--policy", required=True)
    evaluate.add_argument("--calibration", default=None)
    evaluate.add_argument("--commit", default=None)
    evaluate.add_argument("--ticket-journal", required=True)
    evaluate.add_argument("-o", "--output", required=True)
    explain = sub.add_parser("explain-gate")
    explain.add_argument("ticket")
    progress = sub.add_parser("progress")
    progress.add_argument("tickets", nargs="*")
    progress.add_argument("--ticket-journal", default=None)
    progress.add_argument("--calibration", default=None)
    progress.add_argument("--prices", default=None)
    clv = sub.add_parser("clv")
    clv.add_argument("--ticket-journal", required=True)
    clv.add_argument("--prices", required=True)
    clv.add_argument("--countable-only", action="store_true")
    clv.add_argument("--draws", type=int, default=2000)
    clv.add_argument("-o", "--output", default=None)
    args = parser.parse_args(argv)

    def load(path: str) -> Any:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    try:
        if args.command == "self-test":
            self_test()
            return 0
        if args.command == "template":
            value = market_template() if args.kind == "market" else policy_template()
            write_json(args.output, value)
            print(json.dumps({"status": "OK", "output": args.output}))
            return 0
        if args.command == "logprice":
            if args.market:
                market = load(args.market)
                rapports = market["pools"]["SIMPLE_GAGNANT"]["rapports"]
            elif args.rapports:
                rapports = json.loads(args.rapports)
            else:
                raise StakeError("Fournir --market ou --rapports")
            existing_prices = read_jsonl(args.journal)
            price_state = verify_price_journal(existing_prices)
            record = price_record(
                args.race_key, args.observed_at, args.minutes, rapports, args.kind,
                price_state["head_sha256"],
            )
            verify_price_journal([*existing_prices, record])
            append_jsonl(args.journal, record)
            print(json.dumps({"status": "OK", "journal": args.journal, "kind": args.kind,
                              "n_runners": len(rapports)}, ensure_ascii=False))
            return 0
        if args.command == "fit-drift":
            value = fit_drift_model(read_jsonl(args.journal), min_observations=args.min_observations)
            write_json(args.output, value)
            print(json.dumps({"status": "OK", "output": args.output,
                              "n_races": value["n_races"],
                              "fitted": value["drift_model"]["fitted"],
                              "underpopulated_buckets": value["underpopulated_buckets"],
                              "fitted_log_drift_q10": value["drift_model"]["fitted_log_drift_q10"]},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "calibrate":
            value = build_calibration(
                [load(path) for path in args.audits], min_races=args.min_races,
                price_log=read_jsonl(args.prices) if args.prices else None,
            )
            write_json(args.output, value)
            print(json.dumps(
                {
                    "status": "OK", "output": args.output, "n_races": value["n_races"],
                    "usable_for_staking": value["usable_for_staking"],
                    "blocking_reasons": value["blocking_reasons"],
                    "blend": value["blend"], "bootstrap": value["bootstrap"],
                }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "evaluate":
            report = load(args.report)
            ranks, numbers = load_samples(args.samples, report)
            ticket_journal = read_jsonl(args.ticket_journal)
            verify_ticket_journal(ticket_journal)
            state = evaluate_race(
                report, ranks, numbers, load(args.market), load(args.policy),
                load(args.calibration) if args.calibration else None,
                load(args.commit) if args.commit else None,
            )
            ticket = build_ticket(
                state, load(args.market), load(args.policy),
                day_tickets=ticket_journal,
            )
            verify_ticket_journal([*ticket_journal, ticket])
            append_jsonl(args.ticket_journal, ticket)
            write_json(args.output, ticket)
            print(render_ticket(ticket))
            return 0
        if args.command == "progress":
            progress_tickets = (
                read_jsonl(args.ticket_journal)
                if args.ticket_journal else [load(path) for path in args.tickets]
            )
            value = staking_progress(
                progress_tickets,
                load(args.calibration) if args.calibration else None,
                read_jsonl(args.prices) if args.prices else None,
            )
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.command == "clv":
            value = closing_line_value(
                read_jsonl(args.ticket_journal), read_jsonl(args.prices),
                draws=args.draws, countable_only=args.countable_only,
            )
            if args.output:
                write_json(args.output, value)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.command == "explain-gate":
            ticket = load(args.ticket)
            verify_sealed_hash(ticket, "ticket_sha256", "TICKET")
            print(json.dumps(
                {
                    "decision": ticket["decision"],
                    "gates": ticket["gates"],
                    "what_would_have_to_be_true": ticket["what_would_have_to_be_true"],
                }, ensure_ascii=False, indent=2))
            return 0
    except (StakeError, AssertionError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
