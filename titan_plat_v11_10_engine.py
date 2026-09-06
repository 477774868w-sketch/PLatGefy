#!/usr/bin/env python3
"""TITAN PLAT v11.5 SCENARIO-BMA - moteur sportif reproductible et auditable.

Revision majeure de v11.0. Cinq corrections structurelles :

1. SHRINKAGE VERS LA MOYENNE DU PELOTON (et non vers zero). En v11.0,
   `central = raw * confidence` penalisait mecaniquement tout cheval peu
   documente des que le peloton n'etait pas centre sur zero, ce qui violait
   la regle "une absence de preuve n'est jamais un malus sportif".
   v11.1 applique `central = s_bar + confidence * (s - s_bar)`, invariant par
   translation du peloton. Un self-test dedie verrouille cette propriete.

2. SEPARATION REELLE DES INCERTITUDES. En v11.0, l'erreur epistemique etait
   tiree a chaque tirage, donc mathematiquement indiscernable de l'erreur
   aleatoire. v11.1 utilise un echantillonnage imbrique : un monde epistemique
   (etat de connaissance) est tire, puis plusieurs courses sont simulees dans
   ce monde. On peut alors decomposer la variance de rang en une part
   REDUCTIBLE (par plus de donnees) et une part IRREDUCTIBLE (variance de la
   course elle-meme).

3. PALIERS FONDES SUR LA DOMINANCE REELLE. Deux chevaux ne sont separes que
   si la sensibilite aux poids de scenario ET la probabilite de dominance
   par paire ET l'ecart de rang attendu le justifient.

4. ABLATIONS OUTILLEES. Commande `ablate` : chaque axe stable et chaque canal
   conditionnel est neutralise a tour de role, l'impact sur la hierarchie est
   mesure. Le referentiel v11.0 promettait des ablations sans les fournir.

5. SCELLEMENT PUBLIC VERIFIABLE. Commande `commit` : un hash garde pour soi
   ne prouve rien. Le moteur produit un enregistrement d'engagement minimal,
   refuse de le produire apres le depart, et l'enchaine dans un journal
   append-only verifiable (`verify-journal`).

Ajouts secondaires : de-vig de Shin sur le canal marche, log-loss du marche
dans l'audit de lot, erreur standard Monte-Carlo publiee, profil de risque
rang-attendu vs forme-de-victoire, et prise en compte effective de la
volatilite de composition du peloton.

La v11.5 separe MODEL_VERSION de ENGINE_VERSION. Une modification de rendu ou
de gouvernance ne change donc plus silencieusement les tirages. MODEL_VERSION
ne doit etre augmente que si le coeur predictif ou la loi de simulation change.

La calibration de scenarios exige un label factuel independant de l'ordre
d'arrivee. La calibration de mise exige un ticket post-SPORT-LOCK chaine au
rapport et a l'engagement. Aucun proxy choisi apres l'arrivee n'est presente
comme une validation predictive.

Les notes /20 sont des indices de rang attendu normalises. Elles ne sont ni des
probabilites, ni des garanties, ni une preuve d'avantage predictif.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


ENGINE_VERSION = "11.10.0"
# Le coeur numerique est identique a celui de la v11.4. Garder cette valeur
# preserve exactement ses tirages; ne la changer que pour une modification du
# modele ou de la loi de simulation, jamais pour du reporting ou de la CLI.
#
# v11.6 ne touche AUCUNE loi de simulation. Elle ajoute deux pare-feux de
# gouvernance : le manifeste de collecte (§3bis) et l'attestation de
# publication (§8bis). MODEL_VERSION reste donc a 11.4.0. Attention: le
# manifeste entre dans sport_input_sha256, donc la graine change pour un
# dossier qui gagne un bloc collection. La LOI est identique; l'ENTREE ne
# l'est pas. C'est exactement la distinction que le couple de versions sert.
MODEL_VERSION = "11.4.0"
INPUT_SCHEMA = "titan-plat-input-11.10"
# 11.6 reste accepte: les champs v11.10 sont tous OPTIONNELS et un dossier
# 11.6 produit exactement les memes nombres qu'avant. Le numero change parce
# que le SENS d'ability_class change des qu'une marge est declaree, et qu'un
# dossier v11.10 doit etre refuse par un moteur v11.9 sur le schema plutot que
# de facon detournee par la clause anti-ecrasement.
LEGACY_INPUT_SCHEMAS = {"titan-plat-input-11.1", "titan-plat-input-11.6"}
REPORT_SCHEMA = "titan-plat-report-11.6"
SAMPLES_SCHEMA = "titan-plat-rank-samples-11.1"
RESULT_SCHEMA = "titan-plat-result-11.5"
LEGACY_RESULT_SCHEMAS = {"titan-plat-result-11.1"}
AUDIT_SCHEMA = "titan-plat-audit-11.6"
LEGACY_AUDIT_SCHEMAS = {"titan-plat-audit-11.1", "titan-plat-audit-11.5"}
BATCH_SCHEMA = "titan-plat-batch-11.6"
COMMIT_SCHEMA = "titan-plat-commit-11.6"
LEGACY_COMMIT_SCHEMAS = {"titan-plat-commit-11.1", "titan-plat-commit-11.5"}
PUBLICATION_SCHEMA = "titan-plat-publication-11.6"
ACCEPTED_TICKET_SCHEMAS = {"titan-stake-ticket-1.4"}
ABLATION_SCHEMA = "titan-plat-ablation-11.1"

LEVEL_CAPS = {
    "ability_class": 0.90,
    "weight_engagement": 0.60,
    "recent_form": 0.60,
    "stable_suitability": 0.60,
    "human_equipment": 0.35,
    "other": 0.20,
}
LEVEL_TOTAL_CAP = 2.75
ADJUSTMENT_CAPS = {
    "pace": 0.70,
    "going": 0.60,
    "draw_lane": 0.45,
    "traffic_trip": 0.55,
    "energy_distribution": 0.55,
}
SCENARIO_TOTAL_CAP = 1.40
EVIDENCE_TIERS = {"F-P1", "F-S1", "I", "H", "D"}
URL_TIERS = {"F-P1", "F-S1", "D"}

# ---------------------------------------------------------------------------
# VOIE DE TRANSCRIPTION ECRITE (v11.7)
#
# Quand ni sectionnel ni replay ne sont accessibles, la carte de rythme retombe
# sur un remplissage par defaut et le rapport le signale honnetement - mais un
# remplissage par defaut presente comme une lecture de peloton est une etiquette,
# pas une mesure. Les comptes-rendus ECRITS (bulletins officiels des societes
# de courses, commentaires de commissaires, presse specialisee) comblent
# exactement ce trou.
#
# Trois principes non negociables encadrent cette voie:
#
# 1. Un commentaire ecrit est une INTERPRETATION humaine, pas une mesure. Son
#    plafond d'influence est donc STRICTEMENT INFERIEUR a celui du rythme mesure.
#
# 2. Un incident de parcours DETRUIT de l'information, il n'en cree pas. Un
#    cheval enferme qui finit 8e n'est pas un cheval qui valait mieux que 8e:
#    c'est un cheval dont la 8e place ne dit rien. L'incident doit donc ELARGIR
#    l'incertitude epistemique, jamais remonter le niveau. C'est l'inverse de ce
#    que fait l'intuition, et c'est la source classique de surestimation.
#
# 3. Un role de rythme DEDUIT d'un commentaire doit etre distinguable d'un role
#    SUPPOSE. Sans cette distinction, un defaut de donnees se deguise en lecture.
READING_SOURCE_RELIABILITY = {
    "OFFICIEL": 1.00,    # bulletin officiel, commissaires, autorite hippique
    "PRESSE": 0.60,      # presse specialisee signee
    "SECONDAIRE": 0.30,  # forum, agregateur, source non recoupee
}
READING_ROLES = {"leader", "prominent", "midfield", "closer", "unknown"}
# Vocabulaire ferme. Une chaine libre laisserait l'operateur inventer un incident
# apres coup pour justifier une note; un vocabulaire ferme est auditable.
TROUBLE_VOCABULARY = {
    "enferme", "manque_de_place", "gene", "bouscule", "mal_place",
    "parcours_exterieur", "depart_manque", "monte_tardive",
    "perte_de_fers", "materiel_defaillant", "trafic",
}
READING_CAP = 0.40              # < ADJUSTMENT_CAPS["pace"] = 0.70, deliberement
TROUBLE_EPISTEMIC_INCREMENT = 0.12
TROUBLE_EPISTEMIC_MAX = 0.45
READING_FORM_CAP_WITH_TROUBLE = 0.15
DOC_BIAS_WARN = 0.60            # |rho| de Spearman au-dela duquel on alerte

# ---------------------------------------------------------------------------
# ECHELLE DE CLASSE PAR ALLOCATION (v11.8)
#
# Une musique "4-6-3-7-7" ne dit rien. Un 4e dans un Listed vaut mieux qu'un 1er
# dans une reclamer, et rien dans la suite de chiffres ne le signale. C'est le
# chantier n.3 du paragraphe 9 de l'annexe ecosysteme, jamais entame.
#
# Le piege, en le traitant, serait de demander au modele d'ESTIMER le niveau.
# Il produirait des niveaux plausibles et invérifiables, exactement le mode de
# defaillance que tout le reste du moteur combat.
#
# L'architecture retenue inverse donc la charge:
#
#     le collecteur ne declare que des FAITS OBSERVABLES
#     (allocation en euros, categorie, partants, place, date)
#
#     le moteur CALCULE l'indice de maniere deterministe
#
# ability_class cesse d'etre un jugement et devient une fonction. Deux
# collecteurs qui lisent la meme carte obtiennent le meme nombre, et un tiers
# peut le recalculer.
#
# L'allocation est le meilleur proxy public de la classe en France: elle est
# objective, publiee, et fortement stratifiee. Elle n'est pas parfaite - voir
# l'avertissement sur le confondant en fin de calcul.
FORM_CATEGORIES = {
    "GROUPE_1", "GROUPE_2", "GROUPE_3", "LISTED", "HANDICAP",
    "CONDITIONS", "RECLAMER", "A_RECLAMER", "MAIDEN", "AUTRE",
}
# La relation prix-classe est multiplicative, pas additive: on travaille en
# log10. Ancrage a 40 000 EUR, soit un bon handicap de classe 2.
CLASS_ANCHOR_LOG10 = 4.60
CLASS_SCALE_LOG10 = 0.55
CLASS_PERFORMANCE_BETA = 0.45   # cadran declare, NON calibre
CLASS_HALFLIFE_DAYS = 240.0
CLASS_SHRINK_PRIOR = 3.0        # nb de lignes pour atteindre 50% de credit
CLASS_SPREAD_MIN = 0.12         # en deca, le canal ne separe pas
CLASS_MOVE_SIGNIFICANT = 0.35   # montee/descente de classe notable
ALLOCATION_MIN_EUR = 3000.0
ALLOCATION_MAX_EUR = 6_000_000.0

# ---------------------------------------------------------------------------
# DISPERSION DES LIGNES ET COMPRESSION DU HANDICAP (v11.9)
#
# Deux ajouts qui n'exigent aucune donnee nouvelle, seulement une lecture plus
# honnete de ce qui est deja declare.
#
# 1. DISPERSION. Un cheval dont les sorties s'etalent de la reclamer au Groupe 3
#    n'est pas lisible comme un cheval regulier au meme niveau moyen. Les deux
#    peuvent avoir la meme note; ils n'ont pas la meme fiabilite. La dispersion
#    doit donc ELARGIR l'incertitude, exactement comme un incident de parcours.
#    Meme principe: ce qui brouille l'information ne cree pas de valeur.
#
# 2. COMPRESSION DU HANDICAP. Dans un handicap, les poids ne sont pas une
#    donnee parmi d'autres: ils sont l'opinion d'un handicapeur officiel, dont
#    le METIER est d'egaliser le peloton. Un ecart de poids resserre signifie
#    qu'un professionnel independant juge ces chevaux tres proches.
#
#    Contre-intuition importante: on n'en tire PAS un classement. Si le
#    handicapeur fait bien son travail, le poids annule l'avantage au lieu de le
#    signaler. On s'en sert donc comme SECOND AVIS sur une seule question - ce
#    peloton se separe-t-il ? - et l'on signale les desaccords avec notre propre
#    clarte structurelle. Un modele qui se declare net la ou le handicapeur voit
#    un mouchoir de poche doit etre regarde de pres.
DISPERSION_EPISTEMIC_K = 0.30
DISPERSION_MIN_LINES = 3
DISPERSION_EPISTEMIC_MAX = 0.40
HANDICAP_TIGHT_KG = 4.0     # amplitude en deca de laquelle le peloton est resserre
HANDICAP_WIDE_KG = 9.0      # au-dela, le handicapeur separe nettement
WEIGHT_MIN_KG = 40.0
WEIGHT_MAX_KG = 75.0
RACE_TYPES = {
    "HANDICAP", "CONDITIONS", "GROUPE", "LISTED", "RECLAMER", "MAIDEN", "AUTRE",
}

# ---------------------------------------------------------------------------
# LONGUEUR DE BATTUE (v11.10)
#
# Defaut le plus grossier de la v11.9: within_race_performance() ne lisait que
# la PLACE et la taille du peloton. Un 4e battu d'une encolure et un 4e battu de
# quinze longueurs recevaient donc exactement la meme note de sortie. C'est faux
# dans les deux sens: le premier a couru le niveau de la course, le second n'y
# avait pas sa place. La place est un rang; la marge est une mesure.
#
# Conversion. Une longueur vaut environ 2,4 m. Rapportee a la distance de la
# course, elle donne une PERTE RELATIVE de temps, donc une grandeur comparable
# entre un 1200 m et un 2400 m: cinq longueurs sur 1200 m sont un gouffre, sur
# 2800 m une nuance. C'est la raison pour laquelle le handicapping anglo-saxon
# fait varier son bareme longueurs-livres avec la distance, et c'est ce que
# cette normalisation reproduit sans avoir a tabuler quoi que ce soit.
#
# CADRANS DECLARES, NON CALIBRES - au sens du principe 2.6, exactement comme
# CLASS_PERFORMANCE_BETA. Aucun n'a ete ajuste sur donnees.
#
#   MARGIN_LENGTH_METRES     geometrie, la seule valeur non arbitraire du bloc
#   MARGIN_REFERENCE_SPREAD  perte relative correspondant a un cheval
#                            "franchement battu" (~15 L sur 1600 m = 2,25 %).
#                            Fixe a 2,5 %; c'est le zero de l'echelle basse.
#   MARGIN_WEIGHT            confiance accordee a la marge CONTRE le rang.
#                            Volontairement a 0,5 et pas davantage: un cheval
#                            qu'on laisse finir sans insister affiche une marge
#                            qui surestime sa defaite. C'est le biais connu des
#                            figures a la marge, et le rang y est immunise.
#   MARGIN_DEFAULT_DISTANCE  distance de repli quand distance_m est absente.
#   MARGIN_EPISTEMIC_K       elargissement d'incertitude quand la marge manque
#                            alors que le peloton la documente ailleurs.
MARGIN_LENGTH_METRES = 2.4
MARGIN_REFERENCE_SPREAD = 0.025
MARGIN_WEIGHT = 0.50
MARGIN_DEFAULT_DISTANCE_M = 1600.0
MARGIN_EPISTEMIC_K = 0.10
MARGIN_EPISTEMIC_MAX = 0.25
MARGIN_MAX_LENGTHS = 99.0
DISTANCE_MIN_M = 800.0
DISTANCE_MAX_M = 6000.0

# ---------------------------------------------------------------------------
# EFFETS HUMAINS EN A/E (v11.10)
#
# En v11.9, human_equipment etait un score libre plafonne a +/-0,35: le seul
# axe ou le modele pouvait encore ecrire un chiffre sans qu'aucun fait ne
# l'adosse. L'annexe ecosysteme est pourtant explicite (§3 et §7): un effet
# humain sans periode, denominateur et contexte vaut zero, et il se mesure en
# A/E, jamais en taux de reussite brut.
#
# La difference n'est pas cosmetique. Un taux de reussite mesure la QUALITE DES
# CHEVAUX CONFIES a une ecurie; l'A/E mesure ce qu'elle ajoute par rapport a ce
# que le marche attendait deja. Un entraineur a 25 % de reussite dont tous les
# partants etaient favoris a un A/E de 1,0: il n'apporte rien qui ne soit deja
# dans le prix. C'est l'erreur la plus repandue du domaine, et la v11.9 la
# laissait entrer par la porte d'un champ libre.
#
# Desormais: valeur non nulle => enregistrement A/E declare et denominateur
# obligatoires, et c'est le MOTEUR qui calcule le score. Meme chemin que
# ability_class depuis la v11.8.
#
# CADRANS DECLARES, NON CALIBRES.
#   HUMAN_AE_SHRINK_PRIOR  nb de partants pour crediter la moitie de l'ecart.
#                          200 parce qu'un A/E de 1,60 sur 25 partants est moins
#                          fiable qu'un A/E de 1,08 sur 1500 (annexe §3).
#   HUMAN_AE_BETA          conversion d'un log-A/E en points d'axe.
#   HUMAN_AE_MIN_RUNNERS   sous ce denominateur, l'enregistrement est refuse.
HUMAN_AE_SHRINK_PRIOR = 200.0
HUMAN_AE_BETA = 0.55
HUMAN_AE_MIN_RUNNERS = 30
HUMAN_AE_MAX = 4.0
HUMAN_AE_WINDOWS = {14, 30, 90, 180, 365}
HUMAN_AE_SCOPES = {
    "TRAINER", "JOCKEY", "TRAINER_COURSE", "TRAINER_CATEGORY",
    "JOCKEY_COURSE", "TRAINER_JOCKEY", "EQUIPMENT_CHANGE", "STABLE",
}

SCENARIO_STRENGTH = {"LOW": 8.0, "MEDIUM": 25.0, "HIGH": 80.0, "VERY_HIGH": 200.0}
FORBIDDEN_RENDER_KEYS = {
    "p_win", "p_top2", "p_top3", "p_top4", "p_top5", "implied_probability",
    "win_probability", "place_probability", "consensus_probability",
}

# ---------------------------------------------------------------------------
# PARE-FEU DE COLLECTE (v11.6)
#
# v11.5 interdisait deja que des cotes ENTRENT dans INPUT.json. Elle ne pouvait
# rien dire de ce que le collecteur avait REGARDE avant de sceller. Une racecard
# affichant un bloc de cotes suffit a contaminer un dossier sportif sans laisser
# la moindre trace machine: l'operateur ne peut alors que confesser la fuite.
#
# Une confession n'est pas un invariant. Le manifeste ci-dessous transforme la
# fuite en ECHEC DUR au scellement, et rend la contamination detectable par un
# tiers qui n'a que INPUT.json sous les yeux.
#
# Tokens de domaine ou de chemin dont la presence rend une page presumee
# porteuse de prix. Le decoupage est fait sur les separateurs non alphanumeriques,
# donc "parislongchamp" est UN token et ne declenche jamais "paris".
PRICE_BEARING_TOKENS = frozenset({
    "cote", "cotes", "odds", "rapport", "rapports", "pronostic", "pronostics",
    "prono", "pronos", "betting", "bet", "bets", "wager", "tipster", "tips",
    "pmu", "zeturf", "unibet", "winamax", "betclic", "pariez", "parions",
    "betfair", "bet365", "oddschecker", "sportytrader", "turfoo", "equidia",
    "geny", "canalturf", "paristurf", "zone-turf", "zoneturf", "letrot",
    "quinte", "tierce", "favori", "favoris", "enjeux", "masse",
})
COLLECTION_INTEGRITY_STATES = {"SEALED", "LITE_DECLARED", "ABSENT"}
PUBLICATION_MEDIA = {
    # Chaque support ci-dessous produit une trace qu'un TIERS peut dater sans
    # nous croire sur parole. Un message dans une conversation privee n'en fait
    # pas partie: il ne prouve rien a personne d'autre qu'a son auteur.
    "opentimestamps",      # recu .ots ancre sur Bitcoin
    "blockchain_txid",     # transaction portant le hash en OP_RETURN
    "git_public",          # commit signe pousse sur un depot public
    "public_post",         # publication horodatee et publiquement consultable
    "third_party_notary",  # horodatage qualifie eIDAS ou equivalent
}

# Cadrans declares, provisoires, non calibres.
TIER_DOMINANCE_THRESHOLD = 0.60
TIER_EXPECTED_RANK_GAP = 0.45
COMPOSITION_EPISTEMIC_COEFFICIENT = 0.25
RISK_PROFILE_GAP = 2

# Registres d'affirmation. Le defaut d'un rendu automatique n'est pas l'exces de
# confiance : c'est la bouillie prudente. Un systeme qui produit une hierarchie
# nette puis la noie sous des reserves ne protege personne, il gaspille son
# propre travail. Chaque niveau ci-dessous impose une formulation DECIDEE ; ce
# qui varie est la portee de l'affirmation, jamais sa fermete.
CLARITY_REGISTERS = {
    "FORTE": {
        "register": "AFFIRMATIF_SANS_RESERVE",
        "instruction": (
            "Annonce l'ordre de tete directement, sans conditionnel et sans reserve "
            "generique. Les fragilites se disent en une ligne, a la fin, pas en preambule."
        ),
        "banned": ["il pourrait", "on ne peut pas exclure", "difficile a dire",
                   "course a eviter", "je ne me mouille pas"],
    },
    "MOYENNE": {
        "register": "AFFIRMATIF_AVEC_FRONTIERE",
        "instruction": (
            "Annonce l'ordre directement, puis nomme LA frontiere la plus fragile "
            "avec sa probabilite de dominance. Une seule reserve, chiffree, pas trois."
        ),
        "banned": ["il pourrait", "difficile a dire", "course a eviter"],
    },
    "FAIBLE": {
        "register": "AFFIRMATIF_PAR_PALIERS",
        "instruction": (
            "Annonce le palier de tete comme un bloc, affirmativement, puis l'ordre "
            "strict comme departage calcule. Dis clairement CE QUI separe et CE QUI "
            "ne separe pas. Ne remplace pas l'analyse par des precautions."
        ),
        "banned": ["difficile a dire", "je ne sais pas", "course a eviter"],
    },
    "NULLE": {
        "register": "AFFIRMATIF_SUR_L_INDECIDABILITE",
        "instruction": (
            "Dis en UNE phrase pourquoi la course ne se lit pas, en citant l'indicateur "
            "responsable. C'est une conclusion, pas une derobade : elle doit etre aussi "
            "nette qu'un ordre. Puis donne quand meme la hierarchie et les paliers."
        ),
        "banned": ["je prefere ne pas", "a vous de voir", "sans avis"],
    },
}


class InputError(ValueError):
    """Erreur bloquante de donnee, de protocole ou de scellement."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256_obj(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Any, where: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"Horodatage ISO requis: {where}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"Horodatage ISO invalide: {where}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InputError(f"Fuseau obligatoire: {where}")
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise InputError(f"Champ obligatoire absent: {where}.{key}")
    return mapping[key]


def as_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"Nombre attendu: {where}")
    result = float(value)
    if not math.isfinite(result):
        raise InputError(f"Nombre non fini interdit: {where}")
    return result


def as_probability(value: Any, where: str) -> float:
    result = as_float(value, where)
    if not 0.0 <= result <= 1.0:
        raise InputError(f"Probabilite hors [0,1]: {where}")
    return result


def as_positive_int(value: Any, where: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"Entier attendu: {where}")
    if not minimum <= value <= maximum:
        raise InputError(f"{where} doit etre entre {minimum} et {maximum}")
    return value


def require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"Texte requis: {where}")
    return value.strip()


def require_url(value: Any, where: str) -> str:
    text = require_text(value, where)
    if not text.startswith(("https://", "http://")):
        raise InputError(f"URL directe requise: {where}")
    return text


def id_list(value: Any, where: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise InputError(f"Liste d'identifiants requise: {where}")
    if not allow_empty and not value:
        raise InputError(f"Liste vide interdite: {where}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise InputError(f"Identifiant vide ou non textuel: {where}")
    if len(set(value)) != len(value):
        raise InputError(f"Identifiant duplique: {where}")
    return list(value)


def deterministic_seed(race_key: str, as_of: str, sport_input_hash: str) -> int:
    payload = f"{MODEL_VERSION}|{race_key}|{as_of}|{sport_input_hash}"
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big") % (2**32)


def sport_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(raw, ensure_ascii=False))
    payload["market"] = None
    return payload


def verify_sealed_hash(payload: dict[str, Any], field: str, label: str) -> None:
    stored = payload.get(field)
    if not isinstance(stored, str) or len(stored) != 64:
        raise InputError(f"{label} sans empreinte SHA-256 valide")
    candidate = dict(payload)
    candidate.pop(field, None)
    if sha256_obj(candidate) != stored:
        raise InputError(f"{label} modifie apres scellement")


# ---------------------------------------------------------------------------
# 1. HORLOGE ET PARE-FEU TEMPOREL
# ---------------------------------------------------------------------------

def freshness_inflation(knowledge_minutes_to_start: float) -> float:
    """Inflation deterministe fondee sur l'horizon du gel as_of, jamais sur l'heure d'execution."""
    if knowledge_minutes_to_start <= 20.0:
        return 1.0
    excess = knowledge_minutes_to_start - 20.0
    scaled = math.log1p(excess / 60.0) / math.log1p(24.0)
    return min(1.35, 1.0 + 0.30 * scaled)


def composition_volatility(knowledge_minutes_to_start: float, field_final: bool) -> float:
    if field_final:
        return 0.0
    if knowledge_minutes_to_start <= 20.0:
        return 0.10
    return min(1.0, 0.10 + (knowledge_minutes_to_start - 20.0) / 120.0)


def epistemic_inflation(knowledge_minutes_to_start: float, field_final: bool) -> float:
    """v11.1 : la volatilite de composition alimente reellement l'incertitude.

    En v11.0 elle etait calculee, affichee, et n'influencait aucun nombre.
    Coefficient declare, provisoire, a valider hors echantillon.
    """
    freshness = freshness_inflation(knowledge_minutes_to_start)
    volatility = composition_volatility(knowledge_minutes_to_start, field_final)
    return min(1.60, freshness * (1.0 + COMPOSITION_EPISTEMIC_COEFFICIENT * volatility))


def execution_state(minutes_to_start: float) -> str:
    if minutes_to_start < 0:
        return "CLOSED"
    if minutes_to_start <= 20:
        return "FINAL"
    if minutes_to_start <= 75:
        return "WINDOW"
    if minutes_to_start <= 240:
        return "PRE"
    return "EARLY"


def build_clock(raw: dict[str, Any], current: datetime) -> dict[str, Any]:
    race = raw["race"]
    start = parse_iso(require(race, "scheduled_start", "race"), "race.scheduled_start")
    as_of = parse_iso(require(race, "as_of", "race"), "race.as_of")
    if as_of >= start:
        raise InputError("race.as_of doit etre strictement anterieur au depart")
    if as_of > current.replace(microsecond=0):
        raise InputError("race.as_of est dans le futur par rapport au moteur")
    knowledge_minutes = (start - as_of).total_seconds() / 60.0
    execution_minutes = (start - current).total_seconds() / 60.0
    field_final = bool(race.get("field_declared_final", False))
    return {
        "computed_at_utc": iso_utc(current),
        "scheduled_start_utc": iso_utc(start),
        "as_of_utc": iso_utc(as_of),
        "knowledge_minutes_to_start": round(knowledge_minutes, 2),
        "execution_minutes_to_start": round(execution_minutes, 2),
        "execution_state": execution_state(execution_minutes),
        "field_declared_final": field_final,
        "composition_volatility": round(composition_volatility(knowledge_minutes, field_final), 4),
        "freshness_inflation": round(freshness_inflation(knowledge_minutes), 4),
        "epistemic_inflation": round(epistemic_inflation(knowledge_minutes, field_final), 4),
        "definitive": bool(knowledge_minutes <= 20 and field_final),
        "reproducibility_rule": "le coeur sportif depend de as_of, pas de l'heure d'execution",
    }


# ---------------------------------------------------------------------------
# 2. REGISTRE DE PREUVES
# ---------------------------------------------------------------------------

def validate_evidence_registry(raw: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    registry = require(raw, "evidence_registry", "root")
    if not isinstance(registry, dict) or not registry:
        raise InputError("evidence_registry doit etre un objet non vide")
    for eid, item in registry.items():
        where = f"evidence_registry.{eid}"
        if not isinstance(eid, str) or not eid.strip() or not isinstance(item, dict):
            raise InputError(f"Preuve invalide: {where}")
        tier = require_text(require(item, "tier", where), f"{where}.tier")
        if tier not in EVIDENCE_TIERS:
            raise InputError(f"Tier invalide: {where}.tier")
        require_text(require(item, "family_id", where), f"{where}.family_id")
        require_text(require(item, "summary", where), f"{where}.summary")
        observed = parse_iso(require(item, "observed_at", where), f"{where}.observed_at")
        available = parse_iso(require(item, "available_at", where), f"{where}.available_at")
        if observed > as_of or available > as_of:
            raise InputError(f"Fuite temporelle: {where} posterieur a as_of")
        parents = id_list(item.get("parent_ids", []), f"{where}.parent_ids", allow_empty=True)
        if tier in {"I", "H"} and not parents:
            raise InputError(f"Inference/hypothese sans parent: {where}")
        if tier in URL_TIERS:
            require_url(require(item, "source_url", where), f"{where}.source_url")
        elif item.get("source_url") is not None:
            require_url(item["source_url"], f"{where}.source_url")
    for eid, item in registry.items():
        for parent in item.get("parent_ids", []):
            if parent not in registry:
                raise InputError(f"Parent inconnu: {eid} -> {parent}")
            parent_available = parse_iso(
                registry[parent]["available_at"],
                f"evidence_registry.{parent}.available_at",
            )
            child_observed = parse_iso(
                item["observed_at"], f"evidence_registry.{eid}.observed_at"
            )
            if parent_available > child_observed:
                raise InputError(
                    f"Chronologie impossible: parent {parent} disponible apres {eid}"
                )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(eid: str) -> None:
        if eid in visiting:
            raise InputError(f"Cycle dans le registre de preuves autour de {eid}")
        if eid in visited:
            return
        visiting.add(eid)
        for parent in registry[eid].get("parent_ids", []):
            visit(parent)
        visiting.remove(eid)
        visited.add(eid)

    for eid in registry:
        visit(eid)
    return registry


def refs(value: Any, where: str, registry: dict[str, Any], *, allow_empty: bool = False) -> list[str]:
    values = id_list(value, where, allow_empty=allow_empty)
    unknown = sorted(set(values) - set(registry))
    if unknown:
        raise InputError(f"EVIDENCE_ID inconnu dans {where}: {unknown}")
    return values


def families_of(registry: dict[str, Any], ids: Iterable[str]) -> set[str]:
    return {str(registry[eid]["family_id"]) for eid in ids}


# ---------------------------------------------------------------------------
# 3. PARTANTS ET SCENARIOS
# ---------------------------------------------------------------------------

def epistemic_floors(
    epistemic_by_number: dict[int, float],
    readings: dict[int, dict[str, Any]],
    form_lines: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Plancher d'incertitude RELATIF au peloton, pour chaque partant concerne.

    Trois motifs elargissent l'incertitude, et aucun ne touche au niveau:
      - un incident de parcours declare (v11.7);
      - des sorties heterogenes (v11.9);
      - des longueurs de battue moins documentees que celles du peloton (v11.10).

    Ils se combinent par le MAXIMUM et non par la somme: un cheval gene,
    irregulier ET sans marge n'est pas trois fois plus opaque, il l'est au moins
    autant que par le pire de ses motifs.

    Pourquoi RELATIF au peloton. Un plancher absolu se laisserait satisfaire par
    n'importe quel remplissage uniformement large. La revendication reelle est
    ordinale: ce cheval est MOINS lisible que ses rivaux. Elle n'a donc de sens
    que par rapport a eux.

    Fonction extraite en v11.10 pour etre partagee entre la validation et la
    reconstruction apres non-partant. Les deux DOIVENT lire le meme plancher:
    une copie du calcul finirait par diverger, et c'est precisement une
    divergence de ce genre qui a produit le pire bug de la serie.
    """
    values = list(epistemic_by_number.values())
    median = float(np.median(values)) if values else 0.0
    documented = [
        float(item.get("margin_coverage", 0.0))
        for item in form_lines.values() if item.get("declared")
    ]
    field_margin_coverage = float(np.mean(documented)) if documented else 0.0
    by_number: dict[int, dict[str, Any]] = {}
    for number in epistemic_by_number:
        reading = readings.get(number, {})
        form = form_lines.get(number, {})
        trouble_increment = float(reading.get("epistemic_floor_increment", 0.0))
        dispersion_increment = float(form.get("dispersion_increment", 0.0))
        margin_increment = 0.0
        if field_margin_coverage > 0.0 and form.get("declared"):
            shortfall = max(
                0.0, field_margin_coverage - float(form.get("margin_coverage", 0.0))
            )
            margin_increment = min(
                MARGIN_EPISTEMIC_MAX, MARGIN_EPISTEMIC_K * shortfall
            )
        causes: list[str] = []
        if reading.get("trouble_flags"):
            causes.append(
                f"{len(reading['trouble_flags'])} incident(s) de parcours "
                f"(+{trouble_increment:.3f})"
            )
        if dispersion_increment > 0.05:
            causes.append(f"sorties heterogenes (+{dispersion_increment:.3f})")
        if margin_increment > 0.05:
            causes.append(
                f"longueurs de battue moins documentees que le peloton "
                f"(+{margin_increment:.3f})"
            )
        if not causes:
            continue
        increment = max(trouble_increment, dispersion_increment, margin_increment)
        by_number[number] = {
            "floor": median + increment,
            "increment": increment,
            "causes": causes,
        }
    return {"median": median, "field_margin_coverage": field_margin_coverage,
            "by_number": by_number}


def validate_runners(
    raw: dict[str, Any], registry: dict[str, Any], as_of: datetime
) -> tuple[
    list[dict[str, Any]], list[int], dict[int, set[str]], dict[int, set[str]],
    set[str], dict[int, dict[str, Any]], dict[int, dict[str, Any]],
    dict[str, Any], dict[str, Any], dict[int, dict[str, Any]],
]:
    runners = require(raw, "runners", "root")
    if not isinstance(runners, list):
        raise InputError("runners doit etre une liste")
    active = [r for r in runners if isinstance(r, dict) and r.get("active") is True]
    if len(active) < 3:
        raise InputError("Au moins trois partants actifs sont requis")
    numbers: list[int] = []
    baseline_families: dict[int, set[str]] = {}
    factor_families: dict[int, set[str]] = {}
    sport_ids: set[str] = set()
    readings: dict[int, dict[str, Any]] = {}
    epistemic_by_number: dict[int, float] = {}
    trouble_context: dict[int, tuple[str, dict[str, Any], float]] = {}
    form_lines: dict[int, dict[str, Any]] = {}
    declared_ability: dict[int, tuple[str, float]] = {}
    human_records: dict[int, dict[str, Any]] = {}
    declared_human: dict[int, tuple[str, float]] = {}
    for idx, runner in enumerate(active):
        where = f"runners.active[{idx}]"
        number = require(runner, "no", where)
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise InputError(f"Numero entier positif attendu: {where}.no")
        numbers.append(number)
        require_text(require(runner, "name", where), f"{where}.name")
        identity = refs(require(runner, "identity_evidence_ids", where), f"{where}.identity_evidence_ids", registry)
        if not any(registry[eid]["tier"] == "F-P1" for eid in identity):
            raise InputError(f"Identite sans preuve officielle F-P1: {where}")
        sport_ids.update(identity)
        components = require(runner, "score_components", where)
        if not isinstance(components, dict) or set(components) != set(LEVEL_CAPS):
            raise InputError(f"{where}.score_components doit contenir exactement {sorted(LEVEL_CAPS)}")
        used: set[str] = set()
        total = 0.0
        for axis, cap in LEVEL_CAPS.items():
            item = components[axis]
            cw = f"{where}.score_components.{axis}"
            if not isinstance(item, dict):
                raise InputError(f"Composante invalide: {cw}")
            value = as_float(require(item, "value", cw), f"{cw}.value")
            if abs(value) > cap + 1e-12:
                raise InputError(f"Composante hors garde-fou +/-{cap}: {cw}")
            ids = refs(item.get("evidence_ids", []), f"{cw}.evidence_ids", registry, allow_empty=value == 0)
            fams = families_of(registry, ids)
            if value != 0:
                clash = sorted(used & fams)
                if clash:
                    raise InputError(f"Famille comptee sur deux axes pour le cheval {number}: {clash}")
                used.update(fams)
                sport_ids.update(ids)
            total += value
        if abs(total) > LEVEL_TOTAL_CAP + 1e-12:
            raise InputError(f"Score stable hors garde-fou +/-{LEVEL_TOTAL_CAP}: cheval {number}")
        confidence = as_probability(require(runner, "evidence_confidence", where), f"{where}.evidence_confidence")
        if confidence < 0.05:
            raise InputError(f"evidence_confidence trop faible: {where}")
        aleatory = as_float(require(runner, "aleatory_sd", where), f"{where}.aleatory_sd")
        epistemic = as_float(require(runner, "epistemic_sd", where), f"{where}.epistemic_sd")
        if not 0.45 <= aleatory <= 2.50:
            raise InputError(f"aleatory_sd hors [0.45,2.50]: {where}")
        if not 0.10 <= epistemic <= 1.80:
            raise InputError(f"epistemic_sd hors [0.10,1.80]: {where}")
        weight = runner.get("weight_kg")
        if weight is not None:
            weight = as_float(weight, f"{where}.weight_kg")
            if not WEIGHT_MIN_KG <= weight <= WEIGHT_MAX_KG:
                raise InputError(
                    f"{where}.weight_kg hors [{WEIGHT_MIN_KG},{WEIGHT_MAX_KG}]: {weight}"
                )
            runner["weight_kg"] = weight
        reading = validate_race_reading(runner, registry, as_of, where)
        readings[number] = reading
        form_lines[number] = validate_form_lines(runner, registry, as_of, where)
        human_records[number] = validate_human_records(runner, registry, as_of, where)
        declared_human[number] = (where, float(components["human_equipment"]["value"]))
        declared_ability[number] = (where, float(components["ability_class"]["value"]))
        epistemic_by_number[number] = epistemic
        trouble_context[number] = (where, reading, float(components["recent_form"]["value"]))
        loadings = runner.get("factor_loadings", {})
        factor_evidence = runner.get("factor_evidence", {})
        if not isinstance(loadings, dict) or not isinstance(factor_evidence, dict):
            raise InputError(f"factor_loadings/factor_evidence invalides: {where}")
        f_used: set[str] = set()
        for factor, raw_value in loadings.items():
            value = as_float(raw_value, f"{where}.factor_loadings.{factor}")
            if abs(value) > 1.0:
                raise InputError(f"Charge factorielle hors +/-1: {where}.{factor}")
            ids = refs(factor_evidence.get(factor, []), f"{where}.factor_evidence.{factor}", registry, allow_empty=value == 0)
            fams = families_of(registry, ids)
            if value != 0 and (fams & used):
                raise InputError(f"Famille partagee entre niveau et facteur: cheval {number}, facteur {factor}")
            if value != 0 and (fams & f_used):
                raise InputError(f"Famille partagee entre deux facteurs: cheval {number}")
            if value != 0:
                f_used.update(fams)
                sport_ids.update(ids)
        baseline_families[number] = used
        factor_families[number] = f_used
    if len(set(numbers)) != len(numbers):
        raise InputError("Numeros de partants actifs dupliques")
    # REGLE CENTRALE v11.7, verifiee une fois le peloton complet connu.
    #
    # Un incident de parcours est une PERTE d'information, jamais un credit. Le
    # plancher est donc RELATIF au peloton: un cheval gene doit etre declare plus
    # incertain que le partant median, sans quoi l'incident n'a rien change et sa
    # declaration n'etait qu'un ornement. Un plancher absolu se laisserait
    # satisfaire par n'importe quel remplissage un peu large.
    floors = epistemic_floors(epistemic_by_number, readings, form_lines)
    median_epistemic = floors["median"]
    for number, (where, reading, form_value) in trouble_context.items():
        detail = floors["by_number"].get(number)
        if detail is not None and epistemic_by_number[number] + 1e-12 < detail["floor"]:
            raise InputError(
                f"{where}: {' et '.join(detail['causes'])} exigent epistemic_sd >= "
                f"{detail['floor']:.3f} (mediane du peloton {median_epistemic:.3f}), "
                f"valeur fournie {epistemic_by_number[number]:.3f}. Une performance "
                "moins informative que celle des rivaux elargit l'incertitude; elle "
                "ne baisse jamais le niveau."
            )
        if reading["trouble_flags"] and form_value > READING_FORM_CAP_WITH_TROUBLE:
            raise InputError(
                f"{where}: recent_form={form_value:.2f} au-dessus de "
                f"{READING_FORM_CAP_WITH_TROUBLE} alors qu'un incident est declare. "
                "Un incident n'autorise pas a creer de la valeur, il interdit de "
                "conclure. Pour creer de la valeur il faut une preuve positive "
                "independante de l'incident."
            )
    # ECHELLE DE CLASSE v11.8. Le collecteur a declare des faits; le moteur en
    # deduit ability_class. Il ne reste plus au collecteur qu'a RECOPIER le
    # resultat, et toute divergence est refusee. Sans cette clause, l'echelle
    # ne serait qu'une suggestion qu'un jugement pourrait ecraser en silence -
    # c'est-a-dire exactement l'hallucination qu'elle est censee empecher.
    today_allocation = raw["race"].get("allocation_eur")
    if today_allocation is not None:
        today_allocation = as_float(today_allocation, "race.allocation_eur")
        if not ALLOCATION_MIN_EUR <= today_allocation <= ALLOCATION_MAX_EUR:
            raise InputError(
                f"race.allocation_eur hors [{ALLOCATION_MIN_EUR:.0f},"
                f"{ALLOCATION_MAX_EUR:.0f}]: {today_allocation}"
            )
    # race.race_type preexiste en minuscules dans les dossiers v11.x. On
    # normalise au lieu d'imposer une casse, pour ne casser aucun dossier
    # existant, et on tolere une valeur hors liste: seul HANDICAP declenche le
    # module de compression, le reste est simplement sans objet.
    race_type = raw["race"].get("race_type")
    if isinstance(race_type, str):
        race_type = race_type.strip().upper()
    elif race_type is not None:
        raise InputError("race.race_type doit etre une chaine")
    ladder = build_class_ladder(form_lines, today_allocation)
    computed = dict(ladder.get("ability_by_number", {}))
    # Quand l'echelle est inactive - moins de deux partants documentes - elle ne
    # produit aucune valeur. Sans la ligne ci-dessous, un cheval pourrait alors
    # declarer form_lines ET un ability_class invente, ce qui est exactement
    # l'hallucination que le module existe pour empecher. Historique declare
    # mais echelle muette: la seule valeur licite est zero.
    for number, item in form_lines.items():
        if item.get("declared") and number not in computed:
            computed[number] = 0.0
    for number, expected_value in computed.items():
        where, declared_value = declared_ability[number]
        if abs(declared_value - expected_value) > 1e-3:
            raise InputError(
                f"{where}.score_components.ability_class = {declared_value:.4f} alors que "
                f"l'echelle de classe calcule {expected_value:.4f} a partir des "
                f"{form_lines[number]['n_lines']} lignes de forme declarees. Quand "
                "form_lines est fourni, ability_class n'est plus un jugement: c'est une "
                "fonction des faits. Recopier la valeur calculee, ou corriger les faits."
            )
    # EFFETS HUMAINS v11.10. Meme chemin que l'echelle de classe: le collecteur
    # declare des faits - fenetre, denominateur, victoires, attendu - et le
    # moteur calcule. Un human_equipment non nul sans enregistrement A/E est
    # refuse, parce que c'etait le dernier axe ou un chiffre pouvait entrer dans
    # le hash sans qu'aucun fait ne l'adosse.
    for number, (where, declared_value) in declared_human.items():
        record = human_records.get(number, {"declared": False, "value": 0.0})
        if not record.get("declared"):
            if abs(declared_value) > 1e-9:
                raise InputError(
                    f"{where}.score_components.human_equipment = {declared_value:.4f} "
                    "sans human_records. Un effet humain sans fenetre, denominateur et "
                    "A/E vaut ZERO: un taux de reussite mesure la qualite des chevaux "
                    "confies, pas la valeur ajoutee de l'entourage. Declarer un "
                    "enregistrement A/E, ou remettre l'axe a zero."
                )
            continue
        expected_value = float(record["value"])
        if abs(declared_value - expected_value) > 1e-3:
            raise InputError(
                f"{where}.score_components.human_equipment = {declared_value:.4f} alors "
                f"que les {record['n_records']} enregistrement(s) A/E declares donnent "
                f"{expected_value:.4f}. Quand human_records est fourni, l'axe n'est plus "
                "un jugement: c'est une fonction des faits. Recopier la valeur calculee, "
                "ou corriger les faits."
            )
    compression = handicap_compression(active, race_type)
    return (active, numbers, baseline_families, factor_families, sport_ids,
            readings, form_lines, ladder, compression, human_records)


def validate_scenarios(
    raw: dict[str, Any],
    numbers: list[int],
    registry: dict[str, Any],
    baseline_families: dict[int, set[str]],
    factor_families: dict[int, set[str]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Valide des scenarios JOINTS, exclusifs et exhaustifs.

    Une famille peut etayer le meme canal dans plusieurs scenarios mutuellement
    exclusifs. Elle ne peut jamais alimenter deux canaux additifs, ni le niveau
    stable et un ajustement conditionnel.
    """
    scenarios = require(raw, "scenarios", "root")
    if not isinstance(scenarios, list) or not 3 <= len(scenarios) <= 8:
        raise InputError("Entre 3 et 8 scenarios conjoints sont requis")
    seen_ids: set[str] = set()
    probability_sum = 0.0
    residual_count = 0
    sport_ids: set[str] = set()
    cross_scenario_channel: dict[tuple[int, str], str] = {}
    expected_runner_keys = {str(number) for number in numbers}
    for s_idx, scenario in enumerate(scenarios):
        where = f"scenarios[{s_idx}]"
        if not isinstance(scenario, dict):
            raise InputError(f"Scenario invalide: {where}")
        sid = require_text(require(scenario, "id", where), f"{where}.id")
        if sid in seen_ids:
            raise InputError(f"Scenario duplique: {sid}")
        seen_ids.add(sid)
        probability = as_probability(require(scenario, "probability", where), f"{where}.probability")
        if probability < 0.02:
            raise InputError(f"Probabilite de scenario < 0.02: {sid}")
        probability_sum += probability
        require_text(require(scenario, "mechanism", where), f"{where}.mechanism")
        require_text(require(scenario, "probability_basis", where), f"{where}.probability_basis")
        require_text(require(scenario, "failure_mode", where), f"{where}.failure_mode")
        is_residual = bool(require(scenario, "is_residual", where))
        if is_residual:
            residual_count += 1
            if probability < 0.03:
                raise InputError("Le scenario residuel doit porter au moins 0.03 de masse")
        state_ids = refs(
            scenario.get("evidence_ids", []), f"{where}.evidence_ids", registry,
            allow_empty=is_residual,
        )
        sport_ids.update(state_ids)
        adjustments = require(scenario, "runner_adjustments", where)
        if not isinstance(adjustments, dict) or set(adjustments) != expected_runner_keys:
            raise InputError(
                f"{where}.runner_adjustments doit couvrir exactement tous les actifs {sorted(expected_runner_keys)}"
            )
        for horse_key, channels in adjustments.items():
            number = int(horse_key)
            cw = f"{where}.runner_adjustments.{horse_key}"
            if not isinstance(channels, dict):
                raise InputError(f"Ajustements invalides: {cw}")
            bad = sorted(set(channels) - set(ADJUSTMENT_CAPS))
            if bad:
                raise InputError(f"Canaux inconnus dans {cw}: {bad}")
            local_families: set[str] = set()
            total = 0.0
            for channel, item in channels.items():
                iw = f"{cw}.{channel}"
                if not isinstance(item, dict):
                    raise InputError(f"Ajustement invalide: {iw}")
                value = as_float(require(item, "value", iw), f"{iw}.value")
                if abs(value) > ADJUSTMENT_CAPS[channel] + 1e-12:
                    raise InputError(f"Ajustement hors garde-fou +/-{ADJUSTMENT_CAPS[channel]}: {iw}")
                ids = refs(item.get("evidence_ids", []), f"{iw}.evidence_ids", registry, allow_empty=value == 0)
                fams = families_of(registry, ids)
                if value != 0:
                    if fams & baseline_families[number]:
                        raise InputError(f"Famille partagee niveau/scenario: cheval {number}, scenario {sid}")
                    if fams & factor_families[number]:
                        raise InputError(f"Famille partagee facteur/scenario: cheval {number}, scenario {sid}")
                    if fams & local_families:
                        raise InputError(f"Famille comptee sur deux canaux dans {sid}, cheval {number}")
                    for family in fams:
                        key = (number, family)
                        prior_channel = cross_scenario_channel.get(key)
                        if prior_channel is not None and prior_channel != channel:
                            raise InputError(
                                f"Famille {family} change de canal entre scenarios pour le cheval {number}"
                            )
                        cross_scenario_channel[key] = channel
                    local_families.update(fams)
                    sport_ids.update(ids)
                total += value
            if abs(total) > SCENARIO_TOTAL_CAP + 1e-12:
                raise InputError(f"Ajustement total hors +/-{SCENARIO_TOTAL_CAP}: {sid}, cheval {number}")
        means = scenario.get("factor_means", {})
        if not isinstance(means, dict):
            raise InputError(f"{where}.factor_means doit etre un objet")
        for factor, value in means.items():
            if abs(as_float(value, f"{where}.factor_means.{factor}")) > 1.50:
                raise InputError(f"Moyenne factorielle hors +/-1.50: {where}.{factor}")
        noise = as_float(scenario.get("noise_multiplier", 1.0), f"{where}.noise_multiplier")
        if not 0.80 <= noise <= 1.80:
            raise InputError(f"noise_multiplier hors [0.80,1.80]: {where}")
    if abs(probability_sum - 1.0) > 1e-8:
        raise InputError(f"Les probabilites de scenarios doivent sommer a 1 (obtenu {probability_sum:.8f})")
    if residual_count != 1:
        raise InputError("Un et un seul scenario doit porter is_residual=true")
    return scenarios, sport_ids


def validate_unknowns(raw: dict[str, Any], registry: dict[str, Any]) -> None:
    unknowns = raw.get("unknowns", [])
    if not isinstance(unknowns, list):
        raise InputError("unknowns doit etre une liste")
    seen: set[str] = set()
    for idx, item in enumerate(unknowns):
        where = f"unknowns[{idx}]"
        if not isinstance(item, dict):
            raise InputError(f"Inconnue invalide: {where}")
        uid = require_text(require(item, "id", where), f"{where}.id")
        if uid in seen:
            raise InputError(f"Inconnue dupliquee: {uid}")
        seen.add(uid)
        severity = require_text(require(item, "severity", where), f"{where}.severity")
        if severity not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            raise InputError(f"Severite invalide: {where}.severity")
        require_text(require(item, "description", where), f"{where}.description")
        modules = item.get("affected_modules", [])
        if not isinstance(modules, list) or any(not isinstance(x, str) for x in modules):
            raise InputError(f"affected_modules invalide: {where}")
        refs(item.get("evidence_ids", []), f"{where}.evidence_ids", registry, allow_empty=True)


def validate_market(market: Any, numbers: list[int], start: datetime) -> None:
    if market is None:
        return
    if not isinstance(market, dict):
        raise InputError("market doit etre null ou un objet")
    sources = require(market, "sources", "market")
    if not isinstance(sources, list) or not sources:
        raise InputError("market.sources doit etre une liste non vide")
    expected = {str(n) for n in numbers}
    for idx, source in enumerate(sources):
        where = f"market.sources[{idx}]"
        if not isinstance(source, dict):
            raise InputError(f"Source marche invalide: {where}")
        require_text(require(source, "operator", where), f"{where}.operator")
        kind = require_text(require(source, "market_type", where), f"{where}.market_type")
        if kind not in {"parimutuel", "fixed_odds", "exchange"}:
            raise InputError(f"market_type invalide: {where}")
        require_url(require(source, "source_url", where), f"{where}.source_url")
        observed = parse_iso(require(source, "observed_at", where), f"{where}.observed_at")
        if observed >= start:
            raise InputError(f"Snapshot marche posterieur au depart: {where}")
        odds = require(source, "odds", where)
        if not isinstance(odds, dict) or set(odds) != expected:
            raise InputError(f"{where}.odds doit couvrir exactement tous les partants actifs")
        for key, value in odds.items():
            if as_float(value, f"{where}.odds.{key}") <= 1.0:
                raise InputError(f"Cote decimale <=1: {where}.odds.{key}")


# ---------------------------------------------------------------------------
# 3bis. MANIFESTE DE COLLECTE - LA FUITE DEVIENT UN ECHEC, PLUS UN AVEU
# ---------------------------------------------------------------------------

def url_tokens(url: str) -> set[str]:
    """Decoupe une URL en tokens alphanumeriques minuscules.

    Le decoupage evite les faux positifs de sous-chaine: "parislongchamp.com"
    donne {"parislongchamp", "com"} et ne declenche donc jamais un token "paris".
    """
    return {token for token in re.split(r"[^a-z0-9]+", url.lower()) if token}


def price_signals(url: str) -> list[str]:
    """Tokens de l'URL presumes porteurs de prix."""
    return sorted(url_tokens(url) & PRICE_BEARING_TOKENS)


def normalise_url(url: str) -> str:
    """Forme canonique pour comparer une URL declaree a une URL de preuve."""
    cleaned = url.strip().lower()
    cleaned = re.sub(r"^https?://", "", cleaned)
    cleaned = re.sub(r"^www\.", "", cleaned)
    return cleaned.rstrip("/")


def validate_collection(
    raw: dict[str, Any], registry: dict[str, Any], as_of: datetime, fill_mode: str
) -> dict[str, Any]:
    """Pare-feu de collecte v11.6.

    Le moteur ne peut pas observer ce que le collecteur a lu. Il peut en
    revanche exiger que la liste des pages ouvertes avant le scellement soit
    DECLAREE, SCELLEE dans sport_input_sha256, et COHERENTE avec le registre de
    preuves. Trois refus durs en decoulent:

    1. une page declaree porteuse de prix ouverte avant as_of;
    2. une page declaree sans prix dont l'URL porte un token de prix;
    3. une URL citee dans evidence_registry mais absente du manifeste.

    Le troisieme point est celui qui donne des dents au dispositif: sans lui, il
    suffirait d'omettre la page contaminee. Avec lui, toute source reellement
    utilisee doit apparaitre, et son innocuite est engagee explicitement.
    """
    collection = raw.get("collection")
    if collection is None:
        if fill_mode == "FULL":
            raise InputError(
                "Pare-feu de collecte: bloc `collection` obligatoire en mode FULL. "
                "Un dossier scelle sans manifeste de collecte ne peut pas demontrer "
                "son etancheite au marche."
            )
        return {
            "integrity": "ABSENT",
            "n_sources": 0,
            "declared_urls": [],
            "flagged_tokens": [],
            "note": "dossier LITE sans manifeste: etancheite non demontrable, jamais comptable",
        }
    if not isinstance(collection, dict):
        raise InputError("collection doit etre null ou un objet")
    collector = require_text(require(collection, "collector", "collection"), "collection.collector")
    attestation = require_text(
        require(collection, "attestation", "collection"), "collection.attestation"
    )
    sources = require(collection, "sources", "collection")
    if not isinstance(sources, list) or not sources:
        raise InputError("collection.sources doit etre une liste non vide")
    declared: dict[str, dict[str, Any]] = {}
    flagged: list[dict[str, Any]] = []
    for idx, source in enumerate(sources):
        where = f"collection.sources[{idx}]"
        if not isinstance(source, dict):
            raise InputError(f"Source de collecte invalide: {where}")
        url = require_url(require(source, "url", where), f"{where}.url")
        require_text(require(source, "purpose", where), f"{where}.purpose")
        fetched = parse_iso(require(source, "fetched_at", where), f"{where}.fetched_at")
        price_bearing = require(source, "price_bearing", where)
        if not isinstance(price_bearing, bool):
            raise InputError(
                f"{where}.price_bearing doit etre un booleen declare explicitement"
            )
        if fetched > as_of:
            raise InputError(
                f"Fuite temporelle de collecte: {where} ouvert apres as_of"
            )
        signals = price_signals(url)
        if price_bearing:
            raise InputError(
                f"CONTAMINATION DE COLLECTE: {where} declare porteur de prix et ouvert "
                f"a {iso_utc(fetched)}, avant le scellement. Le scellement est refuse. "
                "Recollecter le dossier sportif depuis des pages sans cotes, ou "
                "abandonner la course. Aucune analyse sportive scellee apres lecture "
                "des cotes n'a de valeur probante."
            )
        if signals:
            raise InputError(
                f"CONTAMINATION PRESUMEE: {where} declare sans prix mais son URL porte "
                f"le ou les jetons {signals}. Le moteur ne prend pas la declaration de "
                "l'operateur contre l'evidence de l'URL. Corriger la source ou declarer "
                "price_bearing=true, ce qui refusera le scellement."
            )
        key = normalise_url(url)
        if key in declared:
            raise InputError(f"URL declaree deux fois dans le manifeste: {where}")
        declared[key] = {"url": url, "fetched_at": iso_utc(fetched)}
    evidence_urls: dict[str, str] = {}
    for eid, item in registry.items():
        candidate = item.get("source_url")
        if isinstance(candidate, str) and candidate.strip():
            evidence_urls.setdefault(normalise_url(candidate), eid)
    missing = sorted(key for key in evidence_urls if key not in declared)
    if missing:
        raise InputError(
            "Manifeste incomplet: " + ", ".join(
                f"{evidence_urls[key]} cite {key}" for key in missing[:5]
            ) + ". Toute page ayant produit une preuve doit figurer dans "
            "collection.sources avec sa declaration de prix."
        )
    official = raw.get("race", {}).get("official_source_url")
    if isinstance(official, str) and normalise_url(official) not in declared:
        raise InputError(
            "Manifeste incomplet: race.official_source_url absent de collection.sources"
        )
    return {
        "integrity": "SEALED" if fill_mode == "FULL" else "LITE_DECLARED",
        "n_sources": len(declared),
        "collector": collector,
        "attestation": attestation,
        "declared_urls": sorted(declared),
        "flagged_tokens": flagged,
        "note": (
            "aucune page porteuse de prix declaree ni detectee avant as_of; "
            "manifeste scelle dans sport_input_sha256"
        ),
    }


# ---------------------------------------------------------------------------
# 3ter. VOIE DE TRANSCRIPTION ECRITE (v11.7)
# ---------------------------------------------------------------------------

def validate_race_reading(
    runner: dict[str, Any], registry: dict[str, Any], as_of: datetime, where: str
) -> dict[str, Any]:
    """Lit des comptes-rendus ECRITS quand aucun sectionnel n'est disponible.

    Retourne un resume scelle, et surtout un PLANCHER d'incertitude epistemique
    que le remplissage devra respecter. Le point delicat est traite ici de facon
    volontairement contre-intuitive: un incident de parcours ne remonte jamais la
    note, il elargit l'incertitude. Voir le commentaire des constantes.
    """
    reading = runner.get("race_reading")
    if reading is None:
        return {
            "declared": False, "n_readings": 0, "roles_observed": [],
            "role_basis": "SUPPOSE", "trouble_flags": [], "reliability": 0.0,
            "epistemic_floor_increment": 0.0,
            "note": "aucun compte-rendu ecrit exploite; role de rythme suppose",
        }
    if not isinstance(reading, dict):
        raise InputError(f"{where}.race_reading doit etre null ou un objet")
    readings = require(reading, "readings", f"{where}.race_reading")
    if not isinstance(readings, list) or not readings:
        raise InputError(f"{where}.race_reading.readings doit etre une liste non vide")
    roles: list[str] = []
    troubles: set[str] = set()
    reliabilities: list[float] = []
    for idx, item in enumerate(readings):
        rw = f"{where}.race_reading.readings[{idx}]"
        if not isinstance(item, dict):
            raise InputError(f"Lecture invalide: {rw}")
        eid = require_text(require(item, "evidence_id", rw), f"{rw}.evidence_id")
        if eid not in registry:
            raise InputError(f"{rw}.evidence_id inconnu du registre: {eid}")
        tier = registry[eid]["tier"]
        if tier not in {"F-S1", "D"}:
            raise InputError(
                f"{rw}: un compte-rendu ecrit doit s'appuyer sur une preuve "
                f"documentaire F-S1 ou D, pas sur {tier}"
            )
        source_class = require_text(require(item, "source_class", rw), f"{rw}.source_class")
        if source_class not in READING_SOURCE_RELIABILITY:
            raise InputError(
                f"{rw}.source_class doit etre parmi {sorted(READING_SOURCE_RELIABILITY)}"
            )
        observed = parse_iso(require(item, "observed_at", rw), f"{rw}.observed_at")
        if observed > as_of:
            raise InputError(f"Fuite temporelle: {rw} posterieur a as_of")
        role = require_text(require(item, "observed_role", rw), f"{rw}.observed_role")
        if role not in READING_ROLES:
            raise InputError(f"{rw}.observed_role doit etre parmi {sorted(READING_ROLES)}")
        flags = item.get("trouble", [])
        if not isinstance(flags, list):
            raise InputError(f"{rw}.trouble doit etre une liste")
        for flag in flags:
            if flag not in TROUBLE_VOCABULARY:
                raise InputError(
                    f"{rw}.trouble contient un terme hors vocabulaire: {flag}. "
                    f"Vocabulaire ferme: {sorted(TROUBLE_VOCABULARY)}"
                )
            troubles.add(flag)
        if role != "unknown":
            roles.append(role)
        reliabilities.append(READING_SOURCE_RELIABILITY[source_class])
    # Un incident n'est credite qu'a hauteur de la fiabilite de la source qui le
    # rapporte: un incident vu sur un forum ne vaut pas un incident acte par les
    # commissaires.
    best = max(reliabilities) if reliabilities else 0.0
    increment = min(
        TROUBLE_EPISTEMIC_MAX, TROUBLE_EPISTEMIC_INCREMENT * len(troubles) * best
    )
    dominant = ""
    if roles:
        dominant = max(sorted(set(roles)), key=roles.count)
    return {
        "declared": True,
        "n_readings": len(readings),
        "roles_observed": sorted(set(roles)),
        "dominant_role": dominant,
        "role_basis": "OBSERVE" if roles else "SUPPOSE",
        "trouble_flags": sorted(troubles),
        "reliability": round(best, 3),
        "epistemic_floor_increment": round(increment, 4),
        "note": (
            "un incident elargit l'incertitude et ne remonte jamais la note; "
            "la 8e place d'un cheval enferme n'est pas informative, ni dans un "
            "sens ni dans l'autre"
        ),
    }


def spearman_rho(x: list[float], y: list[float]) -> float:
    """Correlation de rang de Spearman, egalites traitees par rangs moyens."""
    if len(x) != len(y) or len(x) < 3:
        return 0.0

    def rank(values: list[float]) -> np.ndarray:
        order = np.argsort(np.asarray(values, dtype=float), kind="stable")
        ranks = np.empty(len(values), dtype=float)
        ranks[order] = np.arange(1, len(values) + 1, dtype=float)
        array = np.asarray(values, dtype=float)
        for value in np.unique(array):
            mask = array == value
            if mask.sum() > 1:
                ranks[mask] = ranks[mask].mean()
        return ranks

    rx, ry = rank(x), rank(y)
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    denominator = float(np.sqrt((rx * rx).sum() * (ry * ry).sum()))
    if denominator <= 1e-12:
        return 0.0
    return float((rx * ry).sum() / denominator)


def documentation_bias(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Le classement suit-il l'APTITUDE ou le VOLUME DE DOSSIER ?

    Question rarement posee et pourtant decisive. Dans un modele a retrecissement,
    un cheval sans preuve est tire vers la moyenne et ne peut pas s'en ecarter. Un
    cheval documente, lui, bouge. Le classement qui en resulte peut donc separer
    les chevaux SELON LEUR VOLUME D'INFORMATION plutot que selon leur valeur, et
    rien dans la sortie ne le signalerait.

    On mesure ici la correlation de rang entre evidence_confidence et rang
    attendu. Une correlation forte n'est pas une faute en soi - mieux documenter
    un bon cheval le fait legitimement monter - mais elle devient un confondant
    des lors qu'elle n'est pas confirmee par l'arrivee reelle. C'est audit_report
    qui tranche, course apres course.
    """
    if len(entries) < 3:
        return {"rho": 0.0, "grade": "INDETERMINE", "n": len(entries)}
    confidence = [float(entry["evidence_confidence"]) for entry in entries]
    expected = [float(entry["expected_rank"]) for entry in entries]
    # Rang attendu FAIBLE = bon. On inverse pour qu'un rho positif se lise
    # "mieux documente donc mieux classe".
    rho = spearman_rho(confidence, [-value for value in expected])
    magnitude = abs(rho)
    if magnitude >= 0.85:
        grade = "TRES_FORT"
    elif magnitude >= DOC_BIAS_WARN:
        grade = "FORT"
    elif magnitude >= 0.35:
        grade = "MODERE"
    else:
        grade = "FAIBLE"
    top4 = entries[:4]
    top4_are_top_documented = (
        sorted((entry["no"] for entry in top4)) ==
        sorted(
            entry["no"] for entry in
            sorted(entries, key=lambda e: -float(e["evidence_confidence"]))[:4]
        )
    ) if len(entries) >= 4 else False
    return {
        "rho": round(rho, 4),
        "grade": grade,
        "n": len(entries),
        "top4_identical_to_best_documented": top4_are_top_documented,
        "warning": (
            "le classement suit fortement le volume de dossier; verifier en audit "
            "s'il suit aussi l'arrivee reelle, faute de quoi il s'agit d'un "
            "confondant et non d'un signal"
            if magnitude >= DOC_BIAS_WARN else None
        ),
        "interpretation": (
            "rho positif: les chevaux les mieux documentes sont les mieux classes. "
            "Legitime si la documentation revele une vraie superiorite, confondant "
            "si elle ne fait qu'autoriser l'ecart a la moyenne."
        ),
    }


# ---------------------------------------------------------------------------
# 3quater. ECHELLE DE CLASSE PAR ALLOCATION (v11.8)
# ---------------------------------------------------------------------------

def class_index(allocation_eur: float) -> float:
    """Convertit une allocation en indice de classe, ancre a 40 000 EUR.

    Echelle logarithmique parce que la relation dotation-niveau est
    multiplicative: passer de 15k a 30k n'est pas le meme saut que de 300k a
    315k. Valeurs indicatives: reclamer 15k -> -0,77 ; handicap 40k -> 0,00 ;
    Listed 70k -> +0,44 ; Groupe 3 130k -> +0,93 ; Groupe 1 400k -> +1,82.
    """
    return (math.log10(allocation_eur) - CLASS_ANCHOR_LOG10) / CLASS_SCALE_LOG10


def within_race_performance(finish_position: int, field_size: int) -> float:
    """Place normalisee dans [-1, +1], conditionnee a la taille du peloton.

    Un 4e sur 6 n'est pas un 4e sur 18. C'est l'erreur la plus repandue dans la
    lecture des musiques et elle est ici impossible a commettre.
    """
    if field_size <= 1:
        return 0.0
    return 1.0 - 2.0 * (finish_position - 1) / (field_size - 1)


def margin_performance(
    beaten_lengths: float, distance_m: float, finish_position: int
) -> float:
    """Performance mesuree a la MARGE, dans [-1, +1] comme la version au rang.

    La marge est convertie en perte RELATIVE de temps - longueurs x 2,4 m sur la
    distance - puis rapportee a MARGIN_REFERENCE_SPREAD. Un cheval battu de la
    reference complete tombe a -1 et n'y descend pas plus bas: au-dela, la
    distinction entre "battu de vingt longueurs" et "battu de quarante" n'est
    plus une information sur l'aptitude, c'est du bruit de fin de course.

    Le vainqueur vaut +1, comme au rang. Un dead-heat pour la victoire (place 1,
    marge 0) vaut donc +1 pour les deux, ce qui est correct.
    """
    if distance_m <= 0.0:
        distance_m = MARGIN_DEFAULT_DISTANCE_M
    relative_loss = (beaten_lengths * MARGIN_LENGTH_METRES) / distance_m
    slack = min(1.0, relative_loss / MARGIN_REFERENCE_SPREAD)
    if finish_position == 1:
        return 1.0
    return 1.0 - 2.0 * slack


def blended_performance(
    finish_position: int, field_size: int,
    beaten_lengths: float | None, distance_m: float | None,
) -> tuple[float, bool]:
    """Note de performance d'une sortie, marge comprise quand elle est declaree.

    Retourne (performance, marge_utilisee). Sans marge declaree on retombe
    exactement sur la v11.9 - propriete verrouillee par self-test, parce qu'un
    dossier sans longueurs doit continuer a produire la meme note qu'avant.

    Le melange est volontairement partiel (MARGIN_WEIGHT = 0,5). La marge est
    une mesure plus fine que le rang, mais elle est aussi plus fragile: un
    cheval que son jockey laisse finir sans insister - parce que la course est
    jouee, ou pour le menager - affiche une marge qui exagere sa defaite. Le
    rang, lui, y est insensible. On garde donc les deux lectures.
    """
    rank_based = within_race_performance(finish_position, field_size)
    if beaten_lengths is None:
        return rank_based, False
    margin_based = margin_performance(
        beaten_lengths,
        MARGIN_DEFAULT_DISTANCE_M if distance_m is None else distance_m,
        finish_position,
    )
    blended = (1.0 - MARGIN_WEIGHT) * rank_based + MARGIN_WEIGHT * margin_based
    return blended, True


def competitiveness(performance: float) -> float:
    """Part de la classe d'une course qu'un cheval a REELLEMENT justifiee.

    Sans ce facteur, l'echelle credite la classe de la course a tous ceux qui y
    figuraient, et un cheval battu de vingt longueurs dans un Groupe 1 ressort
    devant un bon 6e de Groupe 3. C'est faux: etre distance dans un Groupe 1
    n'est pas une reference de Groupe 1, c'est la preuve qu'on n'y avait pas sa
    place.

    Le vainqueur touche la classe pleine, le cheval de milieu de peloton en
    touche les deux tiers, le dernier un quart. Le plancher a 0,25 evite de dire
    qu'un dernier de Groupe 1 vaut exactement un dernier de reclamer.
    """
    return 0.25 + 0.75 * (1.0 + performance) / 2.0


def outing_rating(
    allocation_eur: float, finish_position: int, field_size: int,
    beaten_lengths: float | None = None, distance_m: float | None = None,
) -> float:
    """Note d'une sortie: classe REELLEMENT justifiee, plus la performance.

    beaten_lengths et distance_m sont optionnels: absents, la note est celle de
    la v11.9 au chiffre pres.
    """
    performance, _ = blended_performance(
        finish_position, field_size, beaten_lengths, distance_m
    )
    return (
        class_index(allocation_eur) * competitiveness(performance)
        + CLASS_PERFORMANCE_BETA * performance
    )


def validate_form_lines(
    runner: dict[str, Any], registry: dict[str, Any], as_of: datetime, where: str
) -> dict[str, Any]:
    """Lit l'historique chiffre d'un partant. Aucun jugement, que des faits."""
    lines = runner.get("form_lines")
    if lines is None:
        return {"declared": False, "n_lines": 0, "rating": None,
                "note": "aucun historique chiffre; ability_class reste au jugement plafonne"}
    if not isinstance(lines, list) or not lines:
        raise InputError(f"{where}.form_lines doit etre null ou une liste non vide")
    parsed: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        lw = f"{where}.form_lines[{idx}]"
        if not isinstance(line, dict):
            raise InputError(f"Ligne de forme invalide: {lw}")
        eid = require_text(require(line, "evidence_id", lw), f"{lw}.evidence_id")
        if eid not in registry:
            raise InputError(f"{lw}.evidence_id inconnu du registre: {eid}")
        if registry[eid]["tier"] not in {"F-S1", "D"}:
            raise InputError(f"{lw}: historique adosse a une preuve non documentaire")
        allocation = as_float(require(line, "allocation_eur", lw), f"{lw}.allocation_eur")
        if not ALLOCATION_MIN_EUR <= allocation <= ALLOCATION_MAX_EUR:
            raise InputError(
                f"{lw}.allocation_eur hors [{ALLOCATION_MIN_EUR:.0f},"
                f"{ALLOCATION_MAX_EUR:.0f}]: {allocation}"
            )
        category = require_text(require(line, "category", lw), f"{lw}.category")
        if category not in FORM_CATEGORIES:
            raise InputError(f"{lw}.category doit etre parmi {sorted(FORM_CATEGORIES)}")
        field_size = require(line, "field_size", lw)
        finish = require(line, "finish_position", lw)
        for name, value in (("field_size", field_size), ("finish_position", finish)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InputError(f"{lw}.{name} doit etre un entier positif")
        if finish > field_size:
            raise InputError(f"{lw}: place {finish} superieure au nombre de partants")
        run_date = parse_iso(require(line, "race_date", lw), f"{lw}.race_date")
        if run_date > as_of:
            raise InputError(f"Fuite temporelle: {lw} posterieur a as_of")
        age_days = max(0.0, (as_of - run_date).total_seconds() / 86400.0)
        # LONGUEUR DE BATTUE (v11.10). Deux faits declares, lus sur la meme
        # ligne de resultat que la place: aucune recherche supplementaire, et
        # aucun jugement. Absents, on retombe exactement sur la v11.9.
        beaten = line.get("beaten_lengths")
        if beaten is not None:
            beaten = as_float(beaten, f"{lw}.beaten_lengths")
            if beaten < 0.0:
                raise InputError(
                    f"{lw}.beaten_lengths negatif: une marge se compte depuis le "
                    f"vainqueur, elle ne peut pas etre inferieure a zero"
                )
            if beaten > MARGIN_MAX_LENGTHS:
                raise InputError(
                    f"{lw}.beaten_lengths hors [0,{MARGIN_MAX_LENGTHS:.0f}]: {beaten}"
                )
            if finish == 1 and beaten > 1e-9:
                raise InputError(
                    f"{lw}: un vainqueur ne peut pas etre battu de {beaten} longueurs"
                )
            if finish > 1 and beaten <= 0.0:
                # Tolere: certaines sources arrondissent une courte tete a 0.
                # On la traite comme une marge nulle, jamais comme une victoire.
                beaten = 0.0
        distance = line.get("distance_m")
        if distance is not None:
            distance = as_float(distance, f"{lw}.distance_m")
            if not DISTANCE_MIN_M <= distance <= DISTANCE_MAX_M:
                raise InputError(
                    f"{lw}.distance_m hors [{DISTANCE_MIN_M:.0f},{DISTANCE_MAX_M:.0f}]:"
                    f" {distance}"
                )
        if distance is not None and beaten is None:
            raise InputError(
                f"{lw}.distance_m declaree sans beaten_lengths: la distance ne sert "
                f"qu'a normaliser une marge, seule elle n'informe rien"
            )
        performance, margin_used = blended_performance(
            finish, field_size, beaten, distance
        )
        parsed.append({
            "class_index": class_index(allocation),
            "outing_rating": outing_rating(
                allocation, finish, field_size, beaten, distance
            ),
            "performance": performance,
            "margin_used": margin_used,
            "margin_distance_declared": distance is not None,
            "age_days": age_days,
            "weight": 0.5 ** (age_days / CLASS_HALFLIFE_DAYS),
            "allocation_eur": allocation,
            "category": category,
        })
    total_weight = sum(item["weight"] for item in parsed)
    if total_weight <= 1e-12:
        raise InputError(f"{where}.form_lines: toutes les sorties sont trop anciennes")
    rating = sum(
        item["weight"] * item["outing_rating"] for item in parsed
    ) / total_weight
    best_class = max(item["class_index"] for item in parsed)
    mean_class = sum(item["weight"] * item["class_index"] for item in parsed) / total_weight
    # DISPERSION v11.9. Deux chevaux peuvent partager la meme note moyenne sans
    # partager la meme fiabilite: l'un a couru trois fois au meme niveau, l'autre
    # a oscille de la reclamer au Groupe 3. La dispersion ne change pas le
    # niveau, elle elargit l'incertitude - meme regle que pour un incident.
    variance = sum(
        item["weight"] * (item["outing_rating"] - rating) ** 2 for item in parsed
    ) / total_weight
    dispersion = math.sqrt(max(0.0, variance))
    dispersion_increment = (
        min(DISPERSION_EPISTEMIC_MAX, DISPERSION_EPISTEMIC_K * dispersion)
        if len(parsed) >= DISPERSION_MIN_LINES else 0.0
    )
    # Couverture de marge: part du POIDS de recence porte par des lignes dont la
    # longueur de battue est declaree. On pondere par le poids et non par le
    # nombre, parce qu'une marge sur une sortie d'il y a deux ans n'informe pas
    # autant qu'une marge sur la derniere.
    margin_weight = sum(item["weight"] for item in parsed if item["margin_used"])
    margin_coverage = margin_weight / total_weight
    return {
        "declared": True,
        "n_lines": len(parsed),
        "n_lines_with_margin": sum(1 for item in parsed if item["margin_used"]),
        "margin_coverage": round(margin_coverage, 4),
        "margin_distance_declared": all(
            item["margin_distance_declared"] for item in parsed if item["margin_used"]
        ) if any(item["margin_used"] for item in parsed) else False,
        "rating": round(rating, 4),
        "rating_dispersion": round(dispersion, 4),
        "dispersion_increment": round(dispersion_increment, 4),
        "dispersion_note": (
            "sorties heterogenes: le niveau moyen est moins informatif"
            if dispersion_increment > 0.05 else
            "sorties homogenes ou echantillon trop court pour conclure"
        ),
        "mean_class_index": round(mean_class, 4),
        "best_class_index": round(best_class, 4),
        "effective_sample": round(total_weight, 3),
        "shrink": round(len(parsed) / (len(parsed) + CLASS_SHRINK_PRIOR), 4),
        "categories": sorted({item["category"] for item in parsed}),
    }


def validate_human_records(
    runner: dict[str, Any], registry: dict[str, Any], as_of: datetime, where: str
) -> dict[str, Any]:
    """Effets humains: A/E declare et denominateur, ou zero. Aucun jugement.

    Pourquoi l'A/E et pas le taux de reussite. Un taux de reussite de 25 %
    mesure d'abord la QUALITE DES CHEVAUX CONFIES a une ecurie. Si ses partants
    etaient tous favoris, elle n'a rien ajoute a ce que le marche savait deja:
    son A/E vaut 1 et sa contribution honnete est nulle. L'A/E - victoires
    reelles sur victoires attendues d'apres les cotes - est la seule des deux
    mesures qui reponde a la question "cet entourage bat-il le prix ?".

    Le retrecissement par denominateur fait le reste du travail: un A/E de 1,60
    sur 25 partants pese moins qu'un A/E de 1,08 sur 1500 (annexe ecosysteme
    §3, "failure mode dominant: les petits echantillons").
    """
    records = runner.get("human_records")
    if records is None:
        return {"declared": False, "n_records": 0, "value": 0.0,
                "note": "aucun effet humain documente; human_equipment doit valoir zero"}
    if not isinstance(records, list) or not records:
        raise InputError(f"{where}.human_records doit etre null ou une liste non vide")
    cap = LEVEL_CAPS["human_equipment"]
    parsed: list[dict[str, Any]] = []
    seen_scopes: set[str] = set()
    total = 0.0
    for idx, record in enumerate(records):
        rw = f"{where}.human_records[{idx}]"
        if not isinstance(record, dict):
            raise InputError(f"Enregistrement humain invalide: {rw}")
        eid = require_text(require(record, "evidence_id", rw), f"{rw}.evidence_id")
        if eid not in registry:
            raise InputError(f"{rw}.evidence_id inconnu du registre: {eid}")
        if registry[eid]["tier"] not in {"F-S1", "D"}:
            raise InputError(
                f"{rw}: statistique humaine adossee a une preuve non documentaire. "
                "Une declaration d'entraineur ou un bruit de piste est un tier H: "
                "il peut ouvrir un scenario, jamais deplacer un niveau."
            )
        scope = require_text(require(record, "scope", rw), f"{rw}.scope")
        if scope not in HUMAN_AE_SCOPES:
            raise InputError(f"{rw}.scope doit etre parmi {sorted(HUMAN_AE_SCOPES)}")
        if scope in seen_scopes:
            raise InputError(
                f"{rw}.scope duplique pour ce cheval: {scope}. Deux fenetres du meme "
                "perimetre compteraient deux fois la meme information."
            )
        seen_scopes.add(scope)
        window = require(record, "window_days", rw)
        if isinstance(window, bool) or not isinstance(window, int):
            raise InputError(f"{rw}.window_days doit etre un entier")
        if window not in HUMAN_AE_WINDOWS:
            raise InputError(
                f"{rw}.window_days doit etre parmi {sorted(HUMAN_AE_WINDOWS)}: {window}"
            )
        runners_n = require(record, "runners", rw)
        if isinstance(runners_n, bool) or not isinstance(runners_n, int) or runners_n <= 0:
            raise InputError(f"{rw}.runners doit etre un entier positif")
        if runners_n < HUMAN_AE_MIN_RUNNERS:
            raise InputError(
                f"{rw}.runners = {runners_n} sous le minimum {HUMAN_AE_MIN_RUNNERS}. "
                "Un effet humain sur un denominateur aussi court n'est pas une "
                "statistique, c'est une anecdote."
            )
        wins = require(record, "wins", rw)
        if isinstance(wins, bool) or not isinstance(wins, int) or wins < 0:
            raise InputError(f"{rw}.wins doit etre un entier positif ou nul")
        if wins > runners_n:
            raise InputError(f"{rw}.wins ({wins}) superieur au denominateur ({runners_n})")
        expected = as_float(require(record, "expected_wins", rw), f"{rw}.expected_wins")
        if expected <= 0.0:
            raise InputError(
                f"{rw}.expected_wins doit etre strictement positif: c'est la somme des "
                "probabilites implicites des cotes de ces partants, sans quoi aucun "
                "A/E n'est calculable."
            )
        if expected > runners_n:
            raise InputError(
                f"{rw}.expected_wins ({expected}) superieur au denominateur ({runners_n})"
            )
        observed_at = parse_iso(require(record, "observed_at", rw), f"{rw}.observed_at")
        if observed_at > as_of:
            raise InputError(f"Fuite temporelle: {rw}.observed_at posterieur a as_of")
        require_text(require(record, "context", rw), f"{rw}.context")
        # A/E. Le +0,5 au numerateur est une correction de continuite: sans elle
        # une ecurie a zero victoire produit un log(0) et une contribution
        # infiniment negative sur un echantillon qui peut etre court.
        ae = (wins + 0.5) / (expected + 0.5)
        ae = min(HUMAN_AE_MAX, ae)
        shrink = runners_n / (runners_n + HUMAN_AE_SHRINK_PRIOR)
        contribution = HUMAN_AE_BETA * math.log(ae) * shrink
        total += contribution
        parsed.append({
            "scope": scope,
            "window_days": window,
            "runners": runners_n,
            "wins": wins,
            "expected_wins": round(expected, 3),
            "a_over_e": round(ae, 4),
            "shrink": round(shrink, 4),
            "contribution": round(contribution, 4),
        })
    value = round(max(-cap, min(cap, total)), 4)
    return {
        "declared": True,
        "n_records": len(parsed),
        "records": parsed,
        "value": value,
        "raw_total": round(total, 4),
        "capped": abs(total) > cap + 1e-12,
        "note": (
            "human_equipment est CALCULE depuis les A/E declares; une valeur "
            "divergente fait echouer le scellement"
        ),
    }


def build_class_ladder(
    form_by_number: dict[int, dict[str, Any]], today_allocation: float | None
) -> dict[str, Any]:
    """Transforme les notes brutes en ability_class RELATIF au peloton.

    Trois garde-fous que tout praticien exigerait:

    1. CENTRAGE SUR LE PELOTON. Une note absolue basse n'a aucun sens: ces
       chevaux courent les uns contre les autres. Seul l'ecart au champ compte.

    2. RETRECISSEMENT PAR TAILLE D'ECHANTILLON. Une seule ligne de forme ne
       vaut pas huit. Le credit accorde suit n/(n+3).

    3. TEST DE SEPARATION. Si l'ecart interquartile du peloton est inferieur au
       seuil, le canal de classe N'APPORTE RIEN et le dit, au lieu de fabriquer
       une separation artificielle a partir de bruit. Meme philosophie que la
       clarte structurelle.
    """
    declared = {n: item for n, item in form_by_number.items() if item.get("declared")}
    if len(declared) < 2:
        return {"active": False, "reason": "moins de deux partants avec historique chiffre",
                "ability_by_number": {}, "separates": False}
    ratings = {n: float(item["rating"]) for n, item in declared.items()}
    values = sorted(ratings.values())
    centre = float(np.median(values))
    spread = float(np.quantile(values, 0.75) - np.quantile(values, 0.25))
    separates = spread >= CLASS_SPREAD_MIN
    ability: dict[int, float] = {}
    cap = LEVEL_CAPS["ability_class"]
    scale = 1.0 if spread <= 1e-9 else min(1.0, cap / max(1e-9, max(
        abs(value - centre) for value in values)))
    for number, rating in ratings.items():
        raw = (rating - centre) * scale
        shrink = float(declared[number]["shrink"])
        ability[number] = round(
            max(-cap, min(cap, raw * shrink * (1.0 if separates else 0.0))), 4
        )
    moves: dict[int, dict[str, Any]] = {}
    if today_allocation is not None:
        today_index = class_index(today_allocation)
        for number, item in declared.items():
            delta = today_index - float(item["mean_class_index"])
            if delta >= CLASS_MOVE_SIGNIFICANT:
                label = "MONTEE_DE_CLASSE"
            elif delta <= -CLASS_MOVE_SIGNIFICANT:
                label = "DESCENTE_DE_CLASSE"
            else:
                label = "CLASSE_STABLE"
            moves[number] = {"delta": round(delta, 3), "label": label}
    margin_coverages = [
        float(item.get("margin_coverage", 0.0)) for item in declared.values()
    ]
    mean_margin_coverage = float(np.mean(margin_coverages)) if margin_coverages else 0.0
    return {
        "active": True,
        "n_runners_with_history": len(declared),
        "median_rating": round(centre, 4),
        "interquartile_spread": round(spread, 4),
        "separates": separates,
        # v11.10. Publie a quel point l'echelle repose sur des marges mesurees
        # plutot que sur des rangs. A 0, le canal traite encore un 4e d'une
        # encolure comme un 4e de quinze longueurs et il faut le dire.
        "margin_coverage_mean": round(mean_margin_coverage, 4),
        "n_runners_with_margin": sum(
            1 for item in declared.values()
            if float(item.get("margin_coverage", 0.0)) > 0.0
        ),
        "margin_note": (
            "aucune longueur de battue declaree: le canal de classe lit des RANGS, "
            "donc un 4e battu d'une encolure et un 4e battu de quinze longueurs y "
            "sont identiques. C'est une approximation connue, pas une mesure."
            if mean_margin_coverage <= 0.0 else
            f"marges declarees sur {mean_margin_coverage:.0%} du poids de recence; "
            "au-dela de cette part, les notes de sortie restent des rangs"
        ),
        "ability_by_number": ability,
        "class_movement": moves,
        "separation_note": (
            "le canal de classe separe le peloton"
            if separates else
            "ecart interquartile sous le seuil: le canal de classe N'APPORTE RIEN "
            "sur cette course et toutes les contributions sont mises a zero"
        ),
        "confounding_warning": (
            "l'allocation mesure la classe des courses courues, donc en partie la "
            "qualite des proprietaires et entraineurs et non la seule aptitude du "
            "cheval. Un bon entourage engage mieux. Ce canal reste un proxy, il "
            "n'est pas calibre, et son influence reelle doit etre mesuree par le "
            "module d'ablation avant d'etre creditee."
        ),
    }


def validate_simulation(raw: dict[str, Any], *, allow_test_samples: bool) -> dict[str, int | float | str]:
    """v11.1 : echantillonnage imbrique explicite (mondes epistemiques x courses)."""
    simulation = require(raw, "simulation", "root")
    if not isinstance(simulation, dict):
        raise InputError("simulation doit etre un objet")
    if "seed" in simulation:
        raise InputError("Graine manuelle interdite")
    if "n_samples" in simulation:
        raise InputError(
            "simulation.n_samples est supprime en 11.1. Utiliser n_worlds et races_per_world "
            "(n_samples = n_worlds * races_per_world)."
        )
    min_worlds = 50 if allow_test_samples else 400
    min_races = 10 if allow_test_samples else 40
    min_total = 20_000 if allow_test_samples else 200_000
    n_worlds = as_positive_int(
        require(simulation, "n_worlds", "simulation"), "simulation.n_worlds", min_worlds, 200_000
    )
    races_per_world = as_positive_int(
        require(simulation, "races_per_world", "simulation"),
        "simulation.races_per_world", min_races, 20_000,
    )
    total = n_worlds * races_per_world
    if not min_total <= total <= 5_000_000:
        raise InputError(
            f"n_worlds * races_per_world doit etre entre {min_total} et 5 000 000 (obtenu {total})"
        )
    df = as_float(require(simulation, "student_df", "simulation"), "simulation.student_df")
    if df <= 2.0:
        raise InputError("simulation.student_df doit etre >2")
    grade = require_text(require(simulation, "scenario_confidence", "simulation"), "simulation.scenario_confidence")
    if grade not in SCENARIO_STRENGTH:
        raise InputError(f"scenario_confidence invalide: {grade}")
    status = require_text(require(simulation, "calibration_status", "simulation"), "simulation.calibration_status")
    if status not in {"UNVALIDATED", "VALIDATED_OOS"}:
        raise InputError("calibration_status invalide")
    calibration_sha = simulation.get("calibration_sha256")
    if status == "VALIDATED_OOS":
        if (
            not isinstance(calibration_sha, str)
            or len(calibration_sha) != 64
            or any(char not in "0123456789abcdef" for char in calibration_sha.lower())
        ):
            raise InputError(
                "simulation.calibration_sha256 valide requis avec VALIDATED_OOS"
            )
    elif calibration_sha is not None:
        raise InputError(
            "simulation.calibration_sha256 doit etre null quand le statut est UNVALIDATED"
        )
    return {
        "n_worlds": n_worlds,
        "races_per_world": races_per_world,
        "n_samples": total,
        "student_df": df,
        "scenario_confidence": grade,
        "calibration_status": status,
        "calibration_sha256": calibration_sha,
    }


def validate_input(
    raw: dict[str, Any], *, current: datetime | None = None, allow_test_samples: bool = False
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InputError("La racine JSON doit etre un objet")
    if raw.get("schema_version") in LEGACY_INPUT_SCHEMAS:
        raise InputError(
            f"Dossier au schema {raw.get('schema_version')}: anterieur au pare-feu de "
            f"collecte. Migrer vers {INPUT_SCHEMA} en ajoutant un bloc `collection`. "
            "Aucune conversion automatique: le manifeste doit etre declare par le "
            "collecteur, pas devine par le moteur."
        )
    if raw.get("schema_version") != INPUT_SCHEMA:
        raise InputError(f"schema_version attendu: {INPUT_SCHEMA}")
    if raw.get("template_only") is True:
        raise InputError("Template vierge: renseigner la course et passer template_only a false")
    fill_mode = raw.get("fill_mode", "FULL")
    if fill_mode not in {"FULL", "LITE"}:
        raise InputError("fill_mode doit valoir FULL ou LITE")
    race = require(raw, "race", "root")
    if not isinstance(race, dict):
        raise InputError("race doit etre un objet")
    for key in ("race_key", "course_name", "race_type", "surface", "data_grade", "course"):
        require_text(require(race, key, "race"), f"race.{key}")
    if race["data_grade"] not in {"A", "B", "C", "D"}:
        raise InputError("race.data_grade doit valoir A, B, C ou D")
    distance = require(race, "distance_m", "race")
    if isinstance(distance, bool) or not isinstance(distance, int) or not 600 <= distance <= 5000:
        raise InputError("race.distance_m doit etre un entier entre 600 et 5000")
    require_url(require(race, "official_source_url", "race"), "race.official_source_url")
    current = (current or now_utc()).astimezone(timezone.utc).replace(microsecond=0)
    clock = build_clock(raw, current)
    as_of = parse_iso(race["as_of"], "race.as_of")
    start = parse_iso(race["scheduled_start"], "race.scheduled_start")
    registry = validate_evidence_registry(raw, as_of)
    collection = validate_collection(raw, registry, as_of, fill_mode)
    (active, numbers, base_families, factor_families, runner_ids,
     readings, form_lines, class_ladder, compression,
     human_records) = validate_runners(raw, registry, as_of)
    scenarios, scenario_ids = validate_scenarios(raw, numbers, registry, base_families, factor_families)
    validate_unknowns(raw, registry)
    simulation = validate_simulation(raw, allow_test_samples=allow_test_samples)
    uncertainty = require(raw, "uncertainty", "root")
    if not isinstance(uncertainty, dict):
        raise InputError("uncertainty doit etre un objet")
    as_probability(require(uncertainty, "race_missingness", "uncertainty"), "uncertainty.race_missingness")
    if not isinstance(require(uncertainty, "critical_unknown", "uncertainty"), bool):
        raise InputError("uncertainty.critical_unknown doit etre booleen")
    if raw.get("market") is not None:
        raise InputError(
            "Pare-feu SPORT-LOCK: market doit rester null dans INPUT.json. "
            "Le marche post-lock entre uniquement par TITAN STAKE."
        )
    if fill_mode == "LITE" and race["data_grade"] not in {"C", "D"}:
        raise InputError("Un dossier LITE ne peut pas declarer un data_grade A ou B")
    return {
        "clock": clock,
        "registry": registry,
        "collection": collection,
        "readings": readings,
        "form_lines": form_lines,
        "human_records": human_records,
        "class_ladder": class_ladder,
        "handicap_compression": compression,
        "active": active,
        "numbers": numbers,
        "scenarios": scenarios,
        "simulation": simulation,
        "fill_mode": fill_mode,
        "sport_evidence_ids": runner_ids | scenario_ids,
    }


# ---------------------------------------------------------------------------
# 4. CONSTRUCTION DES TABLEAUX - SHRINKAGE CORRIGE
# ---------------------------------------------------------------------------

def shrink_towards_field_mean(raw_scores: np.ndarray, confidence: np.ndarray) -> np.ndarray:
    """CORRECTIF v11.1.

    v11.0 calculait `raw * confidence`, soit un retrecissement vers ZERO. Des
    que le peloton n'etait pas centre sur zero, un cheval peu documente etait
    pousse vers le bas du classement uniquement parce qu'il etait mal
    documente, ce qui contredit la regle "une absence de preuve n'est jamais
    un malus sportif".

    v11.1 retrecit vers la MOYENNE DU PELOTON :
        central_i = s_bar + confidence_i * (s_i - s_bar)

    Propriete verrouillee par self-test : ajouter une constante identique a
    tous les scores bruts laisse le vecteur centre strictement inchange.
    """
    field_mean = float(np.mean(raw_scores))
    shrunk = field_mean + confidence * (raw_scores - field_mean)
    return shrunk - float(np.mean(shrunk))


def build_arrays(raw: dict[str, Any], validated: dict[str, Any]) -> dict[str, Any]:
    runners = validated["active"]
    scenarios = validated["scenarios"]
    numbers = np.asarray(validated["numbers"], dtype=np.int32)
    index_of = {int(number): idx for idx, number in enumerate(numbers)}
    raw_scores = np.asarray(
        [sum(float(item["value"]) for item in runner["score_components"].values()) for runner in runners],
        dtype=float,
    )
    confidence = np.asarray([float(r["evidence_confidence"]) for r in runners], dtype=float)
    field_mean = float(np.mean(raw_scores))
    central = shrink_towards_field_mean(raw_scores, confidence)
    factor_names = sorted(
        {str(name) for runner in runners for name in runner.get("factor_loadings", {})}
        | {str(name) for scenario in scenarios for name in scenario.get("factor_means", {})}
    )
    factor_index = {name: idx for idx, name in enumerate(factor_names)}
    loadings = np.zeros((len(runners), len(factor_names)), dtype=float)
    for r_idx, runner in enumerate(runners):
        for name, value in runner.get("factor_loadings", {}).items():
            loadings[r_idx, factor_index[str(name)]] = float(value)
    weights = np.asarray([float(s["probability"]) for s in scenarios], dtype=float)
    adjustments = np.zeros((len(scenarios), len(runners)), dtype=float)
    factor_means = np.zeros((len(scenarios), len(factor_names)), dtype=float)
    noise = np.asarray([float(s.get("noise_multiplier", 1.0)) for s in scenarios], dtype=float)
    for s_idx, scenario in enumerate(scenarios):
        for horse_key, channels in scenario["runner_adjustments"].items():
            h_idx = index_of[int(horse_key)]
            adjustments[s_idx, h_idx] = sum(float(item["value"]) for item in channels.values())
        for name, value in scenario.get("factor_means", {}).items():
            factor_means[s_idx, factor_index[str(name)]] = float(value)
    inflation = float(validated["clock"]["epistemic_inflation"])
    epistemic_input = np.asarray([float(r["epistemic_sd"]) for r in runners], dtype=float)
    epistemic_floor = 0.20 + 0.90 * (1.0 - confidence)
    epistemic = np.maximum(epistemic_input, epistemic_floor) * inflation
    aleatory = np.asarray([float(r["aleatory_sd"]) for r in runners], dtype=float)
    return {
        "runners": runners,
        "numbers": numbers,
        "names": [str(r["name"]) for r in runners],
        "index_of": index_of,
        "raw_scores": raw_scores,
        "field_mean_raw": field_mean,
        "central": central,
        "confidence": confidence,
        "epistemic_sd": epistemic,
        "aleatory_sd": aleatory,
        "factor_names": factor_names,
        "loadings": loadings,
        "weights": weights / weights.sum(),
        "adjustments": adjustments,
        "factor_means": factor_means,
        "noise_multiplier": noise,
        "scenarios": scenarios,
        "simulation": validated["simulation"],
    }


# ---------------------------------------------------------------------------
# 5. SIMULATION IMBRIQUEE - SEPARATION REELLE DES INCERTITUDES
# ---------------------------------------------------------------------------

def simulate(
    raw: dict[str, Any], arrays: dict[str, Any], sport_input_hash: str,
    *, seed_override: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """Echantillonnage imbrique en deux niveaux.

    Niveau externe : un MONDE EPISTEMIQUE. On tire une fois l'erreur sur le
    niveau reel de chaque cheval, puis on la GELE. C'est l'etat de connaissance
    du modele : plus de donnees le retreciraient.

    Niveau interne : les COURSES dans ce monde. On tire le scenario, les
    facteurs partages et le bruit aleatoire Student-t. C'est la variabilite
    physique de la course : aucune donnee supplementaire ne la reduit.

    En v11.0, l'erreur epistemique etait retiree a chaque tirage : elle
    s'additionnait en quadrature avec l'aleatoire et la separation annoncee au
    chapitre 8 du referentiel etait purement notationnelle.

    Retourne (ranks, scenario_index, world_index, seed) aplatis en
    (n_worlds * races_per_world, n_horses).
    """
    config = arrays["simulation"]
    n_worlds = int(config["n_worlds"])
    races = int(config["races_per_world"])
    n_samples = n_worlds * races
    seed = (
        deterministic_seed(raw["race"]["race_key"], raw["race"]["as_of"], sport_input_hash)
        if seed_override is None else int(seed_override)
    )
    rng = np.random.default_rng(seed)
    df = float(config["student_df"])
    scale = math.sqrt((df - 2.0) / df)
    n_horses = len(arrays["numbers"])
    n_factors = len(arrays["factor_names"])
    ranks = np.empty((n_samples, n_horses), dtype=np.int16)
    scenario_index = np.empty(n_samples, dtype=np.int16)
    world_index = np.empty(n_samples, dtype=np.int32)
    # Le decoupage est DERIVE de la taille du probleme, jamais choisi par
    # l'utilisateur : un parametre de chunk libre modifierait le flux du
    # generateur et donc les resultats a entree sportive identique.
    budget_elements = 4_000_000
    chunk_worlds = max(1, min(n_worlds, budget_elements // max(1, races * n_horses)))
    for start_world in range(0, n_worlds, chunk_worlds):
        stop_world = min(start_world + chunk_worlds, n_worlds)
        w = stop_world - start_world
        # ---- niveau externe : une erreur epistemique par monde, gelee ----
        epistemic = rng.normal(size=(w, 1, n_horses)) * arrays["epistemic_sd"][None, None, :]
        # ---- niveau interne : les courses de ce monde ----
        idx = rng.choice(len(arrays["weights"]), size=(w, races), p=arrays["weights"])
        base = arrays["central"][None, None, :] + arrays["adjustments"][idx]
        if n_factors:
            factor_draws = rng.normal(size=(w, races, n_factors)) + arrays["factor_means"][idx]
            shared = factor_draws @ arrays["loadings"].T
        else:
            shared = 0.0
        aleatory = (
            rng.standard_t(df, size=(w, races, n_horses))
            * scale
            * arrays["aleatory_sd"][None, None, :]
            * arrays["noise_multiplier"][idx][:, :, None]
        )
        performance = base + shared + epistemic + aleatory
        flat = performance.reshape(w * races, n_horses)
        order = np.argsort(-flat, axis=1, kind="stable")
        inverse = np.empty_like(order)
        inverse[np.arange(w * races)[:, None], order] = np.arange(1, n_horses + 1)
        lo = start_world * races
        hi = stop_world * races
        ranks[lo:hi] = inverse.astype(np.int16)
        scenario_index[lo:hi] = idx.reshape(-1).astype(np.int16)
        world_index[lo:hi] = np.repeat(np.arange(start_world, stop_world, dtype=np.int32), races)
    return ranks, scenario_index, world_index, seed


def rank_matrix(ranks: np.ndarray) -> np.ndarray:
    n_samples, n_horses = ranks.shape
    matrix = np.empty((n_horses, n_horses), dtype=float)
    for idx in range(n_horses):
        matrix[idx] = np.bincount(ranks[:, idx], minlength=n_horses + 1)[1:] / n_samples
    return matrix


def uncertainty_decomposition(
    ranks: np.ndarray, n_worlds: int, races_per_world: int
) -> dict[str, np.ndarray]:
    """Decomposition de la variance de rang : reductible vs irreductible.

    Var_totale = Var_entre_mondes( E[R | monde] )  +  E_mondes[ Var_intra_monde(R) ]
                 ^ part epistemique, reductible       ^ part aleatoire, irreductible

    L'erreur standard Monte-Carlo de E[R] decoule directement du plan a deux
    niveaux : les moyennes par monde sont independantes et identiquement
    distribuees, donc se = sqrt(Var_entre / n_worlds).
    """
    n_horses = ranks.shape[1]
    cube = ranks.reshape(n_worlds, races_per_world, n_horses).astype(np.float64)
    world_means = cube.mean(axis=1)
    between = world_means.var(axis=0, ddof=1) if n_worlds > 1 else np.zeros(n_horses)
    within = cube.var(axis=1, ddof=1).mean(axis=0) if races_per_world > 1 else np.zeros(n_horses)
    total = between + within
    safe = np.where(total > 1e-12, total, 1.0)
    return {
        "expected_rank": world_means.mean(axis=0),
        "between_world_variance": between,
        "within_world_variance": within,
        "total_variance": total,
        "epistemic_share": np.where(total > 1e-12, between / safe, 0.0),
        "mc_stderr": np.sqrt(between / max(1, n_worlds)),
    }


def pairwise_dominance(ranks: np.ndarray, i: int, j: int) -> float:
    """P(le cheval i finit devant le cheval j) sur le melange complet."""
    return float(np.mean(ranks[:, i] < ranks[:, j]))


def note_from_expected_rank(expected_rank: float, n_horses: int) -> float:
    if n_horses <= 1:
        return 10.0
    note = 20.0 * (n_horses - float(expected_rank)) / (n_horses - 1)
    return round(float(np.clip(note, 0.0, 20.0)), 2)


def weighted_quantile(values: np.ndarray, weights: np.ndarray, quantile: float) -> float:
    order = np.argsort(values)
    sorted_values = values[order]
    sorted_weights = weights[order]
    cumulative = np.cumsum(sorted_weights) / np.sum(sorted_weights)
    return float(sorted_values[np.searchsorted(cumulative, quantile, side="left")])


def scenario_weight_worlds(
    weights: np.ndarray, confidence_grade: str, seed: int, draws: int = 30_000
) -> tuple[np.ndarray, dict[str, Any]]:
    """Sensibilite aux poids de scenario.

    La concentration Dirichlet est un cadran de sensibilite declare, pas une
    taille d'echantillon posterieure calibree. Le BMA central utilise toujours
    les poids ponctuels declares.
    """
    strength = SCENARIO_STRENGTH[confidence_grade]
    alpha = np.maximum(weights * strength, 0.05)
    rng = np.random.default_rng(seed ^ 0xA5A5_11B1)
    worlds = rng.dirichlet(alpha, size=draws)
    modal = np.argmax(worlds, axis=1)
    modal_rates = np.bincount(modal, minlength=len(weights)) / draws
    primary = int(np.argmax(weights))
    summary = {
        "confidence_grade": confidence_grade,
        "sensitivity_strength": strength,
        "primary_index": primary,
        "primary_modal_rate": round(float(modal_rates[primary]), 4),
        "weight_q10": [round(float(x), 4) for x in np.quantile(worlds, 0.10, axis=0)],
        "weight_q90": [round(float(x), 4) for x in np.quantile(worlds, 0.90, axis=0)],
        "synthesis_mode": (
            "DOMINANT_SCENARIO"
            if float(weights[primary]) >= 0.45 and float(modal_rates[primary]) >= 0.80
            else "MIXTURE"
        ),
        "rule": "la hierarchie finale reste toujours BMA, meme en mode DOMINANT_SCENARIO",
    }
    return worlds, summary


def scenario_analysis(
    ranks: np.ndarray,
    scenario_index: np.ndarray,
    arrays: dict[str, Any],
    registry: dict[str, Any],
    global_notes: dict[int, float],
) -> tuple[list[dict[str, Any]], np.ndarray, np.ndarray]:
    numbers = [int(x) for x in arrays["numbers"]]
    n_horses = len(numbers)
    expected_by_scenario = np.empty((len(arrays["scenarios"]), n_horses), dtype=float)
    notes_by_scenario = np.empty_like(expected_by_scenario)
    output: list[dict[str, Any]] = []
    for s_idx, scenario in enumerate(arrays["scenarios"]):
        mask = scenario_index == s_idx
        count = int(mask.sum())
        if count < 500:
            raise InputError(f"Scenario {scenario['id']} sous-echantillonne ({count} tirages)")
        sub = ranks[mask]
        matrix = rank_matrix(sub)
        expected = matrix @ np.arange(1, n_horses + 1)
        expected_by_scenario[s_idx] = expected
        notes = np.asarray([note_from_expected_rank(value, n_horses) for value in expected])
        notes_by_scenario[s_idx] = notes
        order_idx = np.argsort(expected, kind="stable")
        deltas = sorted(
            [
                {"no": numbers[idx], "delta_note": round(float(notes[idx]) - global_notes[numbers[idx]], 2)}
                for idx in range(n_horses)
            ],
            key=lambda item: -item["delta_note"],
        )
        evidence_ids = scenario.get("evidence_ids", [])
        weak_points: list[dict[str, str]] = []
        if scenario.get("is_residual"):
            weak_points.append({"type": "RESIDUAL", "detail": "branche d'evenement non modelise"})
        if not evidence_ids:
            weak_points.append({"type": "NO_DIRECT_ANCHOR", "detail": "aucun EVIDENCE_ID d'etat"})
        else:
            tiers = {registry[eid]["tier"] for eid in evidence_ids}
            families = families_of(registry, evidence_ids)
            if tiers <= {"I", "H"}:
                weak_points.append({"type": "HYPOTHETICAL", "detail": "etat fonde seulement sur I/H"})
            if len(families) == 1:
                weak_points.append({"type": "SINGLE_FAMILY", "detail": "une seule famille de preuve"})
        output.append(
            {
                "id": scenario["id"],
                "probability_pct": round(100.0 * float(arrays["weights"][s_idx]), 2),
                "mechanism": scenario["mechanism"],
                "probability_basis": scenario["probability_basis"],
                "failure_mode": scenario["failure_mode"],
                "is_residual": bool(scenario["is_residual"]),
                "realized_samples": count,
                "order": [numbers[idx] for idx in order_idx],
                "notes": {str(numbers[idx]): round(float(notes[idx]), 2) for idx in range(n_horses)},
                "beneficiaries": [item for item in deltas[:3] if item["delta_note"] > 0.30],
                "victims": [item for item in reversed(deltas[-3:]) if item["delta_note"] < -0.30],
                "weak_points": weak_points,
            }
        )
    order = np.argsort(-arrays["weights"], kind="stable")
    output = [output[idx] for idx in order]
    return output, expected_by_scenario, notes_by_scenario


def dependency_grade(index: float) -> str:
    if index <= 0.08:
        return "FAIBLE"
    if index <= 0.18:
        return "MODEREE"
    return "FORTE"


def epistemic_share_grade(share: float) -> str:
    """Part de l'incertitude de rang qui serait reductible par plus de donnees."""
    if share <= 0.10:
        return "IRREDUCTIBLE"
    if share <= 0.25:
        return "PEU_REDUCTIBLE"
    if share <= 0.45:
        return "PARTIELLEMENT_REDUCTIBLE"
    return "LARGEMENT_REDUCTIBLE"


def build_tiers(entries: list[dict[str, Any]], ranks: np.ndarray, order_index: list[int]) -> list[dict[str, Any]]:
    """v11.1 : trois conditions cumulatives pour separer deux paliers.

    v11.0 ne testait que la sensibilite aux poids de scenario (mondes Dirichlet).
    Deux chevaux pouvaient donc se retrouver en paliers distincts alors que leurs
    distributions de rang reelles se recouvraient presque totalement.

    v11.1 exige en plus une DOMINANCE PAR PAIRE sur le melange complet. Les
    seuils sont des cadrans declares, provisoires, non calibres.
    """
    tiers: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    for position, entry in enumerate(entries):
        if not current:
            current = [entry]
            continue
        previous = current[-1]
        dominance = pairwise_dominance(ranks, order_index[position - 1], order_index[position])
        weight_separated = previous["synthesis_rank_q90"] < entry["synthesis_rank_q10"]
        gap_separated = entry["expected_rank"] - previous["expected_rank"] >= TIER_EXPECTED_RANK_GAP
        dominance_separated = dominance >= TIER_DOMINANCE_THRESHOLD
        separated = weight_separated and gap_separated and dominance_separated
        boundaries.append(
            {
                "above": previous["no"],
                "below": entry["no"],
                "dominance_probability": round(dominance, 4),
                "weight_separated": bool(weight_separated),
                "expected_rank_gap": round(entry["expected_rank"] - previous["expected_rank"], 3),
                "separated": bool(separated),
            }
        )
        if separated:
            tiers.append(current)
            current = [entry]
        else:
            current.append(entry)
    if current:
        tiers.append(current)
    labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    built = [
        {
            "tier": labels[idx] if idx < len(labels) else f"T{idx + 1}",
            "runners": [item["no"] for item in group],
            "note_high": group[0]["note"],
            "note_low": group[-1]["note"],
        }
        for idx, group in enumerate(tiers)
    ]
    return built, boundaries


def check_invariants(matrix: np.ndarray) -> dict[str, Any]:
    row_error = float(np.max(np.abs(matrix.sum(axis=1) - 1.0)))
    column_error = float(np.max(np.abs(matrix.sum(axis=0) - 1.0)))
    ok = row_error < 1e-9 and column_error < 1e-9
    return {"ok": bool(ok), "max_row_error": row_error, "max_column_error": column_error}


def render_key_leaks(value: Any) -> list[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_RENDER_KEYS:
                found.add(key)
            found.update(render_key_leaks(child))
    elif isinstance(value, list):
        for child in value:
            found.update(render_key_leaks(child))
    return sorted(found)


# ---------------------------------------------------------------------------
# 6. RACE-RISK ET CANAL MARCHE
# ---------------------------------------------------------------------------

def race_risk(
    raw: dict[str, Any], matrix: np.ndarray, scenarios: list[dict[str, Any]],
    hierarchy: list[int], numbers: list[int], clock: dict[str, Any],
    synthesis_world_ranks: np.ndarray, mean_epistemic_share: float,
) -> dict[str, Any]:
    n_horses = matrix.shape[0]
    winner_distribution = matrix[:, 0]
    entropy = -float(np.sum(np.where(winner_distribution > 0, winner_distribution * np.log(winner_distribution), 0.0)))
    normalized_entropy = entropy / math.log(n_horses)
    overall_top4 = set(hierarchy[: min(4, n_horses)])
    turnover = 0.0
    for scenario in scenarios:
        top4 = set(scenario["order"][: min(4, n_horses)])
        union = max(1, len(overall_top4 | top4))
        turnover += scenario["probability_pct"] / 100.0 * (1.0 - len(overall_top4 & top4) / union)
    central_top = hierarchy[0]
    top_stability = float(np.mean(synthesis_world_ranks[:, numbers.index(central_top)] == 1))
    missingness = float(raw["uncertainty"]["race_missingness"])
    alerts = {
        "outcome_entropy_high": normalized_entropy >= 0.90,
        "scenario_rotation_high": turnover >= 0.20,
        "scenario_weight_instability": top_stability < 0.70,
        "missingness_high": missingness >= 0.33,
        "critical_unknown": bool(raw["uncertainty"]["critical_unknown"]),
        "field_not_final": clock["composition_volatility"] > 0.30,
        "data_grade_low": raw["race"]["data_grade"] in {"C", "D"},
        "epistemic_share_high": mean_epistemic_share >= 0.40,
    }
    count = sum(bool(value) for value in alerts.values())
    if alerts["critical_unknown"] or count >= 5:
        grade = "U3"
    elif count >= 3:
        grade = "U2"
    elif count >= 1:
        grade = "U1"
    else:
        grade = "U0"
    return {
        "grade": grade,
        "status": "garde_fou_provisoire_a_valider_hors_echantillon",
        "alert_count": count,
        "alerts": alerts,
        "indicators": {
            "normalized_outcome_entropy": round(normalized_entropy, 4),
            "scenario_top4_turnover": round(turnover, 4),
            "synthesis_top_stability_index": round(top_stability, 4),
            "race_missingness": missingness,
            "composition_volatility": clock["composition_volatility"],
            "mean_epistemic_share": round(mean_epistemic_share, 4),
        },
    }


def shin_probabilities(odds: dict[int, float]) -> tuple[dict[int, float], float]:
    """De-vig de Shin (1993), en remplacement de la normalisation naive 1/cote.

    La normalisation proportionnelle suppose une marge uniforme et reproduit
    donc le biais favori-outsider dans les probabilites implicites. Le modele
    de Shin suppose une proportion z de flux informe et resout :

        p_i = [ sqrt(z^2 + 4(1-z) pi_i^2 / B) - z ] / (2(1-z)),  sum p_i = 1

    ou pi_i = 1/cote_i et B = sum(pi_i). z est resolu par bissection.
    Retourne (probabilites, z). z = 0 pour un livre sans surcote.
    """
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


def market_diagnostic(market: Any, numbers: list[int], sport_order: list[int]) -> dict[str, Any] | None:
    if market is None:
        return None
    per_source = []
    stacked: dict[int, list[float]] = {number: [] for number in numbers}
    for source in market["sources"]:
        odds = {int(key): float(value) for key, value in source["odds"].items()}
        booksum = sum(1.0 / value for value in odds.values())
        shin, z = shin_probabilities(odds)
        for number, value in shin.items():
            stacked[number].append(value)
        per_source.append(
            {
                "operator": source["operator"],
                "market_type": source["market_type"],
                "observed_at": source["observed_at"],
                "booksum": round(booksum, 4),
                "overround_pct": round(100.0 * (booksum - 1.0), 3),
                "shin_z": round(z, 5),
                "order": sorted(numbers, key=lambda n: -shin[n]),
            }
        )
    consensus = {number: float(np.mean(values)) for number, values in stacked.items()}
    normalizer = sum(consensus.values())
    consensus = {number: value / normalizer for number, value in consensus.items()}
    market_order = sorted(numbers, key=lambda n: -consensus[n])
    sport_rank = {number: idx + 1 for idx, number in enumerate(sport_order)}
    market_rank = {number: idx + 1 for idx, number in enumerate(market_order)}
    n = len(numbers)
    squared = sum((sport_rank[number] - market_rank[number]) ** 2 for number in numbers)
    spearman = 1.0 - 6.0 * squared / (n * (n * n - 1)) if n > 1 else 1.0
    divergences = []
    for number in numbers:
        gap = market_rank[number] - sport_rank[number]
        divergences.append(
            {
                "no": number,
                "sport_rank": sport_rank[number],
                "market_rank": market_rank[number],
                "rank_gap": gap,
                "verdict": "SPORT_PLUS_HAUT" if gap >= 3 else "MARCHE_PLUS_HAUT" if gap <= -3 else "ALIGNE",
            }
        )
    divergences.sort(key=lambda item: -abs(item["rank_gap"]))
    return {
        "status": "LECTURE_SEPAREE_NON_RETROACTIVE",
        "devig_method": "shin_1993",
        "sources": per_source,
        "consensus_order": market_order,
        "spearman_sport_vs_market": round(spearman, 4),
        "divergences": divergences,
        "internal_consensus": {
            "note": "donnee de marche, jamais rendue par cheval au public; sert au benchmark d'audit",
            "values": {str(number): round(value, 6) for number, value in consensus.items()},
        },
    }


def samples_digest(
    ranks: np.ndarray, scenario_index: np.ndarray, world_index: np.ndarray, numbers: np.ndarray,
    sport_input_hash: str, engine_hash: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(SAMPLES_SCHEMA.encode("utf-8"))
    digest.update(sport_input_hash.encode("ascii"))
    digest.update(engine_hash.encode("ascii"))
    digest.update(np.asarray(numbers, dtype="<i4").tobytes(order="C"))
    digest.update(np.asarray(scenario_index, dtype="<i2").tobytes(order="C"))
    digest.update(np.asarray(world_index, dtype="<i4").tobytes(order="C"))
    digest.update(np.asarray(ranks, dtype="<i2").tobytes(order="C"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 7. RAPPORT
# ---------------------------------------------------------------------------

def handicap_compression(
    runners: list[dict[str, Any]], race_type: str | None
) -> dict[str, Any]:
    """Second avis, signe par un professionnel independant : le handicapeur.

    Dans un handicap, les poids ne sont pas une variable parmi d'autres. Ils sont
    l'opinion officielle d'un handicapeur dont le metier est precisement
    d'egaliser le peloton. Une amplitude de poids resserree signifie qu'un expert
    independant juge ces chevaux tres proches.

    Contre-intuition centrale, et raison pour laquelle ce module ne classe rien:
    si le handicapeur travaille bien, le poids ANNULE l'avantage au lieu de le
    signaler. Convertir un poids eleve en bonus d'aptitude reviendrait a compter
    deux fois la meme information, dans le mauvais sens.

    On s'en sert donc pour une seule question - ce peloton se separe-t-il ? - et
    l'on confronte la reponse a notre propre clarte structurelle. Un modele qui
    se declare net la ou le handicapeur voit un mouchoir de poche merite d'etre
    regarde de pres : c'est un desaccord entre deux lectures independantes, et
    c'est exactement le genre de signal qu'aucune metrique interne ne peut
    produire seule.
    """
    if race_type != "HANDICAP":
        return {
            "applicable": False,
            "reason": "le poids ne mesure l'avis d'un handicapeur que dans un handicap; "
                      "ailleurs il decoule de l'age, du sexe et des surcharges",
        }
    weights = [
        float(runner["weight_kg"]) for runner in runners
        if runner.get("active") is True and runner.get("weight_kg") is not None
    ]
    if len(weights) < 4:
        return {"applicable": False, "reason": "moins de quatre poids declares"}
    spread = max(weights) - min(weights)
    if spread <= HANDICAP_TIGHT_KG:
        verdict = "PELOTON_RESSERRE"
    elif spread >= HANDICAP_WIDE_KG:
        verdict = "PELOTON_ETALE"
    else:
        verdict = "PELOTON_INTERMEDIAIRE"
    return {
        "applicable": True,
        "n_weights": len(weights),
        "spread_kg": round(spread, 2),
        "sd_kg": round(float(np.std(weights)), 3),
        "handicapper_verdict": verdict,
        "note": (
            "amplitude de poids: opinion d'un handicapeur officiel sur la "
            "separabilite du peloton. Aucun classement n'en est tire: dans un "
            "handicap bien construit le poids annule l'avantage plutot qu'il ne "
            "le revele."
        ),
    }


def clarity_second_opinion(
    clarity: dict[str, Any], compression: dict[str, Any]
) -> dict[str, Any]:
    """Confronte notre clarte structurelle a l'avis du handicapeur."""
    if not compression.get("applicable"):
        return {"status": "SANS_OBJET"}
    level = str(clarity.get("level", "")).upper()
    model_separates = level not in {"NULLE", "FAIBLE", ""}
    handicapper_separates = compression["handicapper_verdict"] == "PELOTON_ETALE"
    tight = compression["handicapper_verdict"] == "PELOTON_RESSERRE"
    if model_separates and tight:
        status = "DESACCORD_MODELE_PLUS_SUR"
        comment = (
            "le modele se declare net la ou le handicapeur a resserre les poids. "
            "Deux lectures independantes divergent: se mefier de la hierarchie."
        )
    elif not model_separates and handicapper_separates:
        status = "DESACCORD_HANDICAPEUR_PLUS_SUR"
        comment = (
            "le handicapeur etale nettement les poids alors que le modele ne "
            "separe pas: il est probable que des donnees nous manquent."
        )
    else:
        status = "ACCORD"
        comment = "les deux lectures vont dans le meme sens"
    return {
        "status": status,
        "model_clarity_level": clarity.get("level"),
        "handicapper_verdict": compression["handicapper_verdict"],
        "comment": comment,
    }


def build_structural_clarity(
    entries: list[dict[str, Any]], tiers: list[dict[str, Any]],
    boundaries: list[dict[str, Any]], risk: dict[str, Any], matrix: np.ndarray,
    numbers: list[int], mean_share: float,
) -> dict[str, Any]:
    """Clarte structurelle : nettete interne, jamais confiance predictive.

    Ce bloc existe parce qu'un moteur honnete peut produire un rendu lache. Sans
    ancrage mecanique, un modele de langage confronte a une liste d'avertissements
    produit de la prudence generique a chaque course, y compris quand la structure
    est nette. C'est un gaspillage : la hierarchie est calculee, autant l'assumer.

    L'indice ne mesure PAS la probabilite d'avoir raison. Il mesure uniquement la
    nettete de la separation dans le modele.
    """
    leader = entries[0]
    runner_up = entries[1] if len(entries) > 1 else None
    dominance = boundaries[0]["dominance_probability"] if boundaries else 0.5
    leader_idx = numbers.index(leader["no"])
    leader_win_share = float(matrix[leader_idx][0])
    uniform_share = 1.0 / len(numbers)
    tier_a = tiers[0]["runners"] if tiers else [leader["no"]]

    score = 0
    drivers: list[str] = []
    if dominance >= 0.62:
        score += 2
        drivers.append(f"dominance de tete {dominance:.2f} (>=0.62)")
    elif dominance >= 0.55:
        score += 1
        drivers.append(f"dominance de tete {dominance:.2f} (>=0.55)")
    else:
        drivers.append(f"dominance de tete faible {dominance:.2f}")
    if len(tier_a) <= 2:
        score += 1
        drivers.append(f"palier A resserre ({len(tier_a)} chevaux)")
    else:
        drivers.append(f"palier A large ({len(tier_a)} chevaux)")
    if leader["scenario_dependency_grade"] == "FAIBLE":
        score += 1
        drivers.append("leader peu dependant du scenario")
    elif leader["scenario_dependency_grade"] == "FORTE":
        score -= 1
        drivers.append("leader fortement dependant du scenario")
    if leader["epistemic_share"] <= 0.25:
        score += 1
        drivers.append(f"incertitude du leader surtout irreductible ({leader['epistemic_share']:.2f})")
    if leader_win_share >= 1.8 * uniform_share:
        score += 1
        drivers.append("masse de victoire du leader nettement au-dessus de l'uniforme")
    grade = risk["grade"]
    if grade == "U3":
        score -= 2
        drivers.append("RACE-RISK U3")
    elif grade == "U2":
        score -= 1
        drivers.append("RACE-RISK U2")
    elif grade == "U0":
        score += 1
        drivers.append("RACE-RISK U0")

    if score >= 5:
        level = "FORTE"
    elif score >= 3:
        level = "MOYENNE"
    elif score >= 1:
        level = "FAIBLE"
    else:
        level = "NULLE"
    profile = CLARITY_REGISTERS[level]

    assertions = [
        f"Le n°{leader['no']} mene la hierarchie BMA avec un rang attendu de "
        f"{leader['expected_rank']:.2f} sur {len(numbers)} partants."
    ]
    if runner_up is not None:
        assertions.append(
            f"Le n°{leader['no']} termine devant le n°{runner_up['no']} dans "
            f"{100.0 * dominance:.0f} % des courses simulees."
        )
    assertions.append(
        f"Le palier de tete est {'-'.join(str(x) for x in tier_a)} : "
        + ("ces chevaux ne sont pas separes par la simulation."
           if len(tier_a) > 1 else "il est seul a ce niveau.")
    )
    weakest = min(boundaries, key=lambda b: abs(b["dominance_probability"] - 0.5)) if boundaries else None
    if weakest is not None:
        assertions.append(
            f"La frontiere la plus fragile est {weakest['above']} contre {weakest['below']} "
            f"({weakest['dominance_probability']:.2f})."
        )
    return {
        "level": level,
        "score": score,
        "drivers": drivers,
        "leader": leader["no"],
        "leader_dominance_over_second": round(dominance, 4),
        "tier_a": tier_a,
        "mean_epistemic_share": round(mean_share, 4),
        "language_register": profile["register"],
        "rendering_instruction": profile["instruction"],
        "banned_phrases": profile["banned"],
        "ready_assertions": assertions,
        "scope": (
            "mesure la nettete de la separation dans le modele, jamais la probabilite "
            "d'avoir raison; une clarte FORTE n'autorise aucune mise"
        ),
    }


def build_report(
    raw: dict[str, Any], *, engine_path: str | None = None,
    current: datetime | None = None, allow_test_samples: bool = False,
    seed_override: int | None = None,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    current = (current or now_utc()).astimezone(timezone.utc).replace(microsecond=0)
    validated = validate_input(raw, current=current, allow_test_samples=allow_test_samples)
    arrays = build_arrays(raw, validated)
    config = arrays["simulation"]
    clean_sport_payload = sport_payload(raw)
    sport_input_hash = sha256_obj(clean_sport_payload)
    ranks, scenario_index, world_index, seed = simulate(
        raw, arrays, sport_input_hash, seed_override=seed_override
    )
    matrix = rank_matrix(ranks)
    n_horses = len(arrays["numbers"])
    numbers = [int(value) for value in arrays["numbers"]]
    decomposition = uncertainty_decomposition(
        ranks, int(config["n_worlds"]), int(config["races_per_world"])
    )
    expected = matrix @ np.arange(1, n_horses + 1)
    global_notes = {numbers[idx]: note_from_expected_rank(expected[idx], n_horses) for idx in range(n_horses)}
    scenarios, scenario_expected, scenario_notes = scenario_analysis(
        ranks, scenario_index, arrays, validated["registry"], global_notes
    )
    worlds, scenario_weight_summary = scenario_weight_worlds(
        arrays["weights"], config["scenario_confidence"], seed
    )
    combined_expected = worlds @ scenario_expected
    order_worlds = np.argsort(combined_expected, axis=1, kind="stable")
    inverse_worlds = np.empty_like(order_worlds)
    inverse_worlds[np.arange(len(worlds))[:, None], order_worlds] = np.arange(1, n_horses + 1)

    # Profil de risque : ordre par rang attendu vs ordre par masse de victoire.
    # Le rang attendu enterre structurellement les chevaux a forte variance.
    p_win = matrix[:, 0]
    win_shape_order = sorted(range(n_horses), key=lambda i: (-p_win[i], expected[i]))
    win_shape_rank = {idx: pos + 1 for pos, idx in enumerate(win_shape_order)}
    expected_order = sorted(range(n_horses), key=lambda i: (expected[i], i))
    expected_rank_position = {idx: pos + 1 for pos, idx in enumerate(expected_order)}

    entries = []
    for idx, number in enumerate(numbers):
        scenario_rank_values = scenario_expected[:, idx]
        dependency = float(np.sqrt(np.sum(arrays["weights"] * (scenario_rank_values - expected[idx]) ** 2)))
        dependency_index = dependency / max(1, n_horses - 1)
        scenario_note_values = scenario_notes[:, idx]
        profile_gap = expected_rank_position[idx] - win_shape_rank[idx]
        if profile_gap >= RISK_PROFILE_GAP:
            profile = "PROFIL_VARIANCE"
        elif profile_gap <= -RISK_PROFILE_GAP:
            profile = "PROFIL_REGULARITE"
        else:
            profile = "PROFIL_NEUTRE"
        share = float(decomposition["epistemic_share"][idx])
        entries.append(
            {
                "no": number,
                "name": arrays["names"][idx],
                "note": global_notes[number],
                "expected_rank": round(float(expected[idx]), 3),
                "expected_rank_mc_stderr": round(float(decomposition["mc_stderr"][idx]), 4),
                "rank_q10": int(np.quantile(ranks[:, idx], 0.10, method="higher")),
                "rank_q50": int(np.quantile(ranks[:, idx], 0.50, method="higher")),
                "rank_q90": int(np.quantile(ranks[:, idx], 0.90, method="higher")),
                "scenario_note_q10": round(weighted_quantile(scenario_note_values, arrays["weights"], 0.10), 2),
                "scenario_note_q90": round(weighted_quantile(scenario_note_values, arrays["weights"], 0.90), 2),
                "scenario_note_min": round(float(np.min(scenario_note_values)), 2),
                "scenario_note_max": round(float(np.max(scenario_note_values)), 2),
                "scenario_dependency_index": round(dependency_index, 4),
                "scenario_dependency_grade": dependency_grade(dependency_index),
                "epistemic_share": round(share, 4),
                "epistemic_share_grade": epistemic_share_grade(share),
                "reducible_rank_sd": round(float(np.sqrt(decomposition["between_world_variance"][idx])), 3),
                "irreducible_rank_sd": round(float(np.sqrt(decomposition["within_world_variance"][idx])), 3),
                "risk_profile": profile,
                "risk_profile_gap": int(profile_gap),
                "synthesis_rank_q10": int(np.quantile(inverse_worlds[:, idx], 0.10, method="higher")),
                "synthesis_rank_q50": int(np.quantile(inverse_worlds[:, idx], 0.50, method="higher")),
                "synthesis_rank_q90": int(np.quantile(inverse_worlds[:, idx], 0.90, method="higher")),
                "evidence_confidence": round(float(arrays["confidence"][idx]), 3),
                "stable_score_raw": round(float(arrays["raw_scores"][idx]), 3),
                "stable_score_shrunk_centered": round(float(arrays["central"][idx]), 3),
                "epistemic_sd_effective": round(float(arrays["epistemic_sd"][idx]), 3),
                "aleatory_sd": round(float(arrays["aleatory_sd"][idx]), 3),
            }
        )
    entries.sort(key=lambda item: (item["expected_rank"], item["synthesis_rank_q90"], item["no"]))
    for entry in entries:
        reading = validated["readings"].get(entry["no"], {})
        entry["role_basis"] = reading.get("role_basis", "SUPPOSE")
        entry["reading_trouble_flags"] = reading.get("trouble_flags", [])
        entry["reading_n"] = reading.get("n_readings", 0)
    hierarchy = [entry["no"] for entry in entries]
    order_index = [numbers.index(number) for number in hierarchy]
    tiers, tier_boundaries = build_tiers(entries, ranks, order_index)
    mean_share = float(np.mean(decomposition["epistemic_share"]))
    risk = race_risk(
        raw, matrix, scenarios, hierarchy, numbers, validated["clock"], inverse_worlds, mean_share
    )
    structural_clarity = build_structural_clarity(
        entries, tiers, tier_boundaries, risk, matrix, numbers, mean_share
    )
    invariants = check_invariants(matrix)
    primary_idx = int(np.argmax(arrays["weights"]))
    scenario_weight_summary["primary_scenario_id"] = arrays["scenarios"][primary_idx]["id"]
    scenario_weight_summary["primary_probability_pct"] = round(100.0 * float(arrays["weights"][primary_idx]), 2)
    sport_core = {
        "strict_order": hierarchy,
        "tiers": tiers,
        "notes": {str(entry["no"]): entry["note"] for entry in entries},
        "scenario_orders": {scenario["id"]: scenario["order"] for scenario in scenarios},
        "synthesis_mode": scenario_weight_summary["synthesis_mode"],
    }
    sport_core_sha = sha256_obj(sport_core)
    engine_hash = sha256_file(engine_path or __file__)
    sample_hash = samples_digest(
        ranks, scenario_index, world_index, arrays["numbers"], sport_input_hash, engine_hash
    )
    render = {
        "verdict": {
            "strict_order": hierarchy,
            "tiers": tiers,
            "tier_boundaries": tier_boundaries,
            "synthesis_mode": scenario_weight_summary["synthesis_mode"],
            "primary_scenario": scenario_weight_summary["primary_scenario_id"],
            "primary_scenario_probability_pct": scenario_weight_summary["primary_probability_pct"],
            "race_risk": risk,
            "structural_clarity": structural_clarity,
        },
        "scenarios": scenarios,
        "horses": entries,
        "uncertainty_split": {
            "design": "echantillonnage imbrique: mondes epistemiques x courses par monde",
            "n_worlds": int(config["n_worlds"]),
            "races_per_world": int(config["races_per_world"]),
            "mean_epistemic_share": round(mean_share, 4),
            "reading": (
                "part de la variance de rang attribuable a l'etat de connaissance; "
                "le complement est la variabilite de course, irreductible par plus de donnees"
            ),
        },
        "note_scale": {
            "formula": "20*(n-rang_attendu)/(n-1)",
            "anchor": "10/20 = rang attendu moyen du peloton",
            "warning": "indice ordinal non calibre; aucune conversion en probabilite",
        },
    }
    leaks = render_key_leaks(render)
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "model_version": MODEL_VERSION,
        "race_key": raw["race"]["race_key"],
        "course_name": raw["race"]["course_name"],
        "fill_mode": raw.get("fill_mode", "FULL"),
        "clock": validated["clock"],
        "data_grade": raw["race"]["data_grade"],
        "calibration_status": config["calibration_status"],
        "calibration_sha256": config.get("calibration_sha256"),
        "collection": validated["collection"],
        "race_reading": {
            "n_runners_with_written_reading": sum(
                1 for item in validated["readings"].values() if item.get("declared")
            ),
            "n_runners_total": len(validated["readings"]),
            "roles_observed_not_assumed": sum(
                1 for item in validated["readings"].values()
                if item.get("role_basis") == "OBSERVE"
            ),
            "n_runners_with_trouble": sum(
                1 for item in validated["readings"].values() if item.get("trouble_flags")
            ),
            "pace_map_basis": (
                "OBSERVE" if any(
                    item.get("role_basis") == "OBSERVE"
                    for item in validated["readings"].values()
                ) else "SUPPOSE"
            ),
            "note": (
                "pace_map_basis=SUPPOSE signifie que la repartition des roles est un "
                "remplissage par defaut et non une lecture de ce peloton; toute "
                "conclusion tactique en decoulant est sans fondement empirique"
            ),
        },
        "documentation_bias": documentation_bias(entries),
        "class_ladder": validated["class_ladder"],
        "handicap_compression": validated["handicap_compression"],
        "clarity_second_opinion": clarity_second_opinion(
            structural_clarity, validated["handicap_compression"]
        ),
        "claim": "scenario_hierarchies_and_bma_order_not_predictively_validated",
        "render": render,
        "scenario_weight_sensitivity": scenario_weight_summary,
        "market_diagnostic": market_diagnostic(raw.get("market"), numbers, hierarchy),
        "simulation": {
            "n_worlds": int(config["n_worlds"]),
            "races_per_world": int(config["races_per_world"]),
            "n_samples": int(config["n_samples"]),
            "student_df": float(config["student_df"]),
            "seed_policy": (
                "sha256(model_version|race_key|as_of|sport_input_hash)"
                if seed_override is None else "SEED_GELEE_DIAGNOSTIC_ABLATION"
            ),
            "model_version": MODEL_VERSION,
            "calibration_sha256": config.get("calibration_sha256"),
            "seed": seed,
            "factor_names": arrays["factor_names"],
            "shrinkage_rule": "central = moyenne_peloton + confiance * (score - moyenne_peloton)",
            "field_mean_raw_score": round(float(arrays["field_mean_raw"]), 6),
            "max_expected_rank_mc_stderr": round(float(np.max(decomposition["mc_stderr"])), 4),
        },
        "invariant_check": invariants,
        "render_purity_check": {"ok": not leaks, "forbidden_keys_found": leaks},
        "unknowns": raw.get("unknowns", []),
        "internal": {
            "rank_matrix": {str(numbers[idx]): [float(v) for v in matrix[idx]] for idx in range(n_horses)},
            "scenario_expected_ranks": {
                arrays["scenarios"][s_idx]["id"]: {
                    str(numbers[h_idx]): float(scenario_expected[s_idx, h_idx])
                    for h_idx in range(n_horses)
                }
                for s_idx in range(len(arrays["scenarios"]))
            },
            "variance_decomposition": {
                str(numbers[idx]): {
                    "between_world": float(decomposition["between_world_variance"][idx]),
                    "within_world": float(decomposition["within_world_variance"][idx]),
                }
                for idx in range(n_horses)
            },
            "warning": "audit interne; ne pas rendre les probabilites cheval au public",
        },
        "sport_core_sha256": sport_core_sha,
        "sport_input_sha256": sport_input_hash,
        "engine_sha256": engine_hash,
        "rank_samples_sha256": sample_hash,
        "evidence_registry_sha256": sha256_obj(validated["registry"]),
        "sealed_sport_input": clean_sport_payload,
    }
    report["report_sha256"] = sha256_obj(report)
    samples = {
        "ranks": ranks,
        "scenario_index": scenario_index,
        "world_index": world_index,
        "numbers": arrays["numbers"],
        "sport_input_sha256": np.asarray(sport_input_hash),
        "engine_sha256": np.asarray(engine_hash),
        "rank_samples_sha256": np.asarray(sample_hash),
        "schema_version": np.asarray(SAMPLES_SCHEMA),
    }
    return report, samples


def render_report(report: dict[str, Any]) -> str:
    verify_sealed_hash(report, "report_sha256", "REPORT")
    clock = report["clock"]
    render = report["render"]
    verdict = render["verdict"]
    split = render["uncertainty_split"]
    lines = [
        f"TITAN PLAT v{report['engine_version']} - {report['course_name']}",
        f"race_key: {report['race_key']}",
        (
            f"Horloge: {clock['execution_state']} | gel sportif T-"
            f"{clock['knowledge_minutes_to_start']:.1f} min | definitif="
            f"{'OUI' if clock['definitive'] else 'NON'}"
        ),
        f"Donnees: {report['data_grade']} | RACE-RISK {verdict['race_risk']['grade']} | "
        f"CLARTE STRUCTURELLE {verdict['structural_clarity']['level']} "
        f"(score {verdict['structural_clarity']['score']})",
        (
            f"Synthese: {verdict['synthesis_mode']} | scenario primaire "
            f"{verdict['primary_scenario']} ({verdict['primary_scenario_probability_pct']:.2f}%)"
        ),
        (
            f"Incertitude: {100.0 * split['mean_epistemic_share']:.1f}% reductible "
            f"({split['n_worlds']} mondes x {split['races_per_world']} courses)"
        ),
        "",
        "SCENARIOS - ORDRES COMPLETS",
    ]
    for scenario in render["scenarios"]:
        lines.append(
            f"{scenario['probability_pct']:6.2f}% | {scenario['id']} | "
            f"{'-'.join(str(number) for number in scenario['order'])}"
        )
        lines.append(f"  mecanisme: {scenario['mechanism']}")
        lines.append(f"  faiblesse: {scenario['failure_mode']}")
        for item in scenario["beneficiaries"]:
            lines.append(f"  profite: {item['no']} ({item['delta_note']:+.2f} note)")
        for item in scenario["victims"]:
            lines.append(f"  souffre: {item['no']} ({item['delta_note']:+.2f} note)")
    lines.extend(["", "HIERARCHIE FINALE BMA - ORDRE STRICT"])
    by_no = {item["no"]: item for item in render["horses"]}
    for rank, number in enumerate(verdict["strict_order"], start=1):
        horse = by_no[number]
        lines.append(
            f"{rank:>2}. {number} {horse['name']} | {horse['note']:.2f}/20 | "
            f"rang attendu {horse['expected_rank']:.2f} +/- {horse['expected_rank_mc_stderr']:.3f} | "
            f"dependance {horse['scenario_dependency_grade']} | "
            f"reductible {100.0 * horse['epistemic_share']:.0f}% | {horse['risk_profile']}"
        )
    lines.extend(["", "AFFIRMATIONS DIRECTEMENT UTILISABLES"])
    for sentence in verdict["structural_clarity"]["ready_assertions"]:
        lines.append(f"  {sentence}")
    lines.append(
        f"  Registre impose: {verdict['structural_clarity']['language_register']}"
    )
    lines.extend(["", "PALIERS DE ROBUSTESSE"])
    for tier in verdict["tiers"]:
        lines.append(f"Palier {tier['tier']}: {'-'.join(str(x) for x in tier['runners'])}")
    lines.append("")
    lines.append("FRONTIERES NON SEPARANTES (dominance par paire)")
    for boundary in verdict["tier_boundaries"]:
        if not boundary["separated"]:
            lines.append(
                f"  {boundary['above']} vs {boundary['below']}: "
                f"P(devant) = {boundary['dominance_probability']:.3f} - non separes"
            )
    lines.extend(
        [
            "",
            f"sport_core_sha256: {report['sport_core_sha256']}",
            f"rank_samples_sha256: {report['rank_samples_sha256']}",
            f"report_sha256:      {report['report_sha256']}",
            "Aucune probabilite d'arrivee par cheval n'est rendue.",
        ]
    )
    return "\n".join(lines)


def report_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    verify_sealed_hash(before, "report_sha256", "REPORT AVANT")
    verify_sealed_hash(after, "report_sha256", "REPORT APRES")
    if before["race_key"] != after["race_key"]:
        raise InputError("delta impossible entre deux courses differentes")
    b = {item["no"]: item for item in before["render"]["horses"]}
    a = {item["no"]: item for item in after["render"]["horses"]}
    common = sorted(set(b) & set(a))
    rank_b = {number: idx + 1 for idx, number in enumerate(before["render"]["verdict"]["strict_order"])}
    rank_a = {number: idx + 1 for idx, number in enumerate(after["render"]["verdict"]["strict_order"])}
    movements = [
        {
            "no": number,
            "rank_before": rank_b[number],
            "rank_after": rank_a[number],
            "rank_delta": rank_b[number] - rank_a[number],
            "note_before": b[number]["note"],
            "note_after": a[number]["note"],
            "note_delta": round(a[number]["note"] - b[number]["note"], 2),
        }
        for number in common
    ]
    movements.sort(key=lambda item: (-abs(item["rank_delta"]), -abs(item["note_delta"])))
    return {
        "race_key": before["race_key"],
        "as_of_before": before["clock"]["as_of_utc"],
        "as_of_after": after["clock"]["as_of_utc"],
        "order_before": before["render"]["verdict"]["strict_order"],
        "order_after": after["render"]["verdict"]["strict_order"],
        "synthesis_mode_before": before["render"]["verdict"]["synthesis_mode"],
        "synthesis_mode_after": after["render"]["verdict"]["synthesis_mode"],
        "movements": movements,
    }


# ---------------------------------------------------------------------------
# 8. SCELLEMENT PUBLIC - UN HASH GARDE POUR SOI NE PROUVE RIEN
# ---------------------------------------------------------------------------

REVISION_REASONS = {"NON_PARTANT", "TERRAIN", "MONTE", "DECLARATION", "CORRECTION_SAISIE"}


def build_commit(
    report: dict[str, Any], *, previous_entry_sha256: str | None = None,
    current: datetime | None = None, supersedes: dict[str, Any] | None = None,
    revision_reason: str | None = None,
) -> dict[str, Any]:
    """Enregistrement d'engagement minimal, publiable AVANT le depart.

    Les empreintes de v11.0 prouvaient la coherence interne du rapport, pas
    l'anteriorite de la prevision : n'importe qui peut hacher n'importe quoi
    apres l'arrivee. Publier cet enregistrement avant le depart (commit git
    signe, horodatage public, depot notarie) est ce qui transforme la chaine
    de hachage en preuve opposable a un tiers.

    Le moteur REFUSE d'emettre un engagement apres l'heure de depart.
    L'enregistrement ne contient aucune information sportive exploitable :
    seulement des empreintes, l'identite de la course et les horodatages.
    """
    verify_sealed_hash(report, "report_sha256", "REPORT")
    current = (current or now_utc()).astimezone(timezone.utc).replace(microsecond=0)
    start = parse_iso(report["clock"]["scheduled_start_utc"], "report.clock.scheduled_start_utc")
    if current >= start:
        raise InputError(
            "Engagement refuse: le depart est passe. Un scellement post-depart ne prouve "
            "aucune anteriorite et n'a aucune valeur d'audit."
        )
    if previous_entry_sha256 is not None:
        previous_entry_sha256 = require_text(previous_entry_sha256, "previous_entry_sha256")
        if len(previous_entry_sha256) != 64:
            raise InputError("previous_entry_sha256 doit etre une empreinte SHA-256")
    # Une course peut legitimement etre re-analysee avant son depart: non-partant,
    # changement de terrain ou de monte. Le journal doit l'accepter SANS ouvrir la
    # porte a la revision opportuniste apres lecture des cotes. La revision est
    # donc autorisee, tracee, motivee par une cause declaree, et comptee: un taux
    # de revision eleve est en soi un signal a auditer.
    revision = 0
    supersedes_sha = None
    if supersedes is not None:
        verify_sealed_hash(supersedes, "entry_sha256", "ENGAGEMENT SUPERSEDE")
        if supersedes.get("schema_version") not in ({COMMIT_SCHEMA} | LEGACY_COMMIT_SCHEMAS):
            raise InputError("Schema de l'engagement supersede incompatible")
        if supersedes.get("race_key") != report["race_key"]:
            raise InputError("L'engagement supersede porte sur une autre course")
        if supersedes.get("scheduled_start_utc") != report["clock"]["scheduled_start_utc"]:
            raise InputError("L'engagement supersede porte sur un autre horaire de course")
        if supersedes.get("report_sha256") == report["report_sha256"]:
            raise InputError("Revision inutile: le rapport est identique a l'engagement supersede")
        superseded_at = parse_iso(
            supersedes.get("committed_at_utc"), "supersedes.committed_at_utc"
        )
        if superseded_at >= current:
            raise InputError("L'engagement supersede doit etre anterieur a la revision")
        old_as_of = parse_iso(supersedes.get("as_of_utc"), "supersedes.as_of_utc")
        new_as_of = parse_iso(report["clock"]["as_of_utc"], "report.clock.as_of_utc")
        if new_as_of < old_as_of:
            raise InputError("Une revision ne peut pas reculer as_of")
        if revision_reason not in REVISION_REASONS:
            raise InputError(
                f"revision_reason obligatoire et parmi {sorted(REVISION_REASONS)}"
            )
        old_revision = supersedes.get("revision", 0)
        if isinstance(old_revision, bool) or not isinstance(old_revision, int) or old_revision < 0:
            raise InputError("Numero de revision supersede invalide")
        revision = old_revision + 1
        supersedes_sha = supersedes["entry_sha256"]
    elif revision_reason is not None:
        raise InputError("revision_reason exige un engagement supersede")
    entry = {
        "schema_version": COMMIT_SCHEMA,
        "engine_version": report["engine_version"],
        "model_version": report.get("model_version", MODEL_VERSION),
        "race_key": report["race_key"],
        "course_name": report["course_name"],
        "scheduled_start_utc": report["clock"]["scheduled_start_utc"],
        "as_of_utc": report["clock"]["as_of_utc"],
        "committed_at_utc": iso_utc(current),
        "minutes_before_start": round((start - current).total_seconds() / 60.0, 2),
        "n_active_runners": len(report["render"]["verdict"]["strict_order"]),
        "data_grade": report["data_grade"],
        "calibration_status": report["calibration_status"],
        "collection_integrity": report.get("collection", {}).get("integrity", "ABSENT"),
        "race_risk_grade": report["render"]["verdict"]["race_risk"]["grade"],
        "sport_core_sha256": report["sport_core_sha256"],
        "sport_input_sha256": report["sport_input_sha256"],
        "engine_sha256": report["engine_sha256"],
        "rank_samples_sha256": report["rank_samples_sha256"],
        "report_sha256": report["report_sha256"],
        "prev_entry_sha256": previous_entry_sha256,
        "revision": revision,
        "supersedes_entry_sha256": supersedes_sha,
        "revision_reason": revision_reason,
        "disclosure": "aucun ordre, aucune note, aucune cote dans cet enregistrement",
    }
    entry["entry_sha256"] = sha256_obj(entry)
    return entry


def read_journal(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise InputError(f"Journal illisible ligne {line_number}") from exc
    return entries


def append_journal(path: str | os.PathLike[str], entry: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")


# ---------------------------------------------------------------------------
# 8bis. ATTESTATION DE PUBLICATION - LA PHRASE DEVIENT UN NOMBRE
# ---------------------------------------------------------------------------

def build_publication(
    entry: dict[str, Any], *, medium: str, reference: str, published_at: str,
    current: datetime | None = None,
) -> dict[str, Any]:
    """Atteste qu'un engagement a ete rendu opposable AVANT le depart.

    v11.5 ecrivait deja, en toutes lettres, qu'un engagement n'a de valeur que
    s'il a ete publie avant le depart. Cette phrase etait une chaine de
    caracteres dans la sortie de verify_journal: aucune fonction ne la
    verifiait, aucun nombre n'en dependait. C'est exactement le defaut que le
    referentiel reproche a la separation d'incertitudes de la v11.0 - une
    etiquette qui ne change aucun nombre.

    L'attestation est un enregistrement SEPARE, pas un champ de l'engagement:
    le support de publication horodate le hash, il ne peut donc pas etre connu
    avant que ce hash existe. La chaine du journal reste intacte.
    """
    verify_sealed_hash(entry, "entry_sha256", "ENGAGEMENT")
    if entry.get("schema_version") not in ({COMMIT_SCHEMA} | LEGACY_COMMIT_SCHEMAS):
        raise InputError("Schema d'engagement incompatible")
    if medium not in PUBLICATION_MEDIA:
        raise InputError(
            f"medium doit etre parmi {sorted(PUBLICATION_MEDIA)}. Un message dans une "
            "conversation privee n'est pas une publication: aucun tiers ne peut le dater."
        )
    reference = require_text(reference, "reference")
    if len(reference) < 8:
        raise InputError("reference trop courte pour etre verifiable par un tiers")
    current = (current or now_utc()).astimezone(timezone.utc).replace(microsecond=0)
    published = parse_iso(published_at, "published_at")
    committed = parse_iso(entry.get("committed_at_utc"), "entry.committed_at_utc")
    start = parse_iso(entry.get("scheduled_start_utc"), "entry.scheduled_start_utc")
    if published < committed:
        raise InputError("Publication anterieure a l'engagement qu'elle atteste")
    if published >= start:
        raise InputError(
            "Publication posterieure au depart: elle ne prouve aucune anteriorite. "
            "Une course dont l'engagement n'a pas ete publie avant le depart ne "
            "compte pas dans le denominateur de validation."
        )
    if published > current:
        raise InputError("Publication datee dans le futur")
    record = {
        "schema_version": PUBLICATION_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "race_key": entry["race_key"],
        "entry_sha256": entry["entry_sha256"],
        "report_sha256": entry["report_sha256"],
        "scheduled_start_utc": entry["scheduled_start_utc"],
        "committed_at_utc": entry["committed_at_utc"],
        "published_at_utc": iso_utc(published),
        "minutes_before_start": round((start - published).total_seconds() / 60.0, 2),
        "medium": medium,
        "reference": reference,
        "attested": (
            "cette reference doit permettre a un tiers de constater, sans nous croire "
            "sur parole, que entry_sha256 existait avant scheduled_start_utc"
        ),
    }
    record["publication_sha256"] = sha256_obj(record)
    return record


def index_publications(publications: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Indexe les attestations valides par empreinte d'engagement."""
    index: dict[str, dict[str, Any]] = {}
    for idx, record in enumerate(publications):
        where = f"publication[{idx}]"
        if not isinstance(record, dict):
            raise InputError(f"{where} non objet")
        if record.get("schema_version") != PUBLICATION_SCHEMA:
            raise InputError(f"{where} au schema inattendu")
        verify_sealed_hash(record, "publication_sha256", where)
        if record.get("medium") not in PUBLICATION_MEDIA:
            raise InputError(f"{where}: support de publication non recevable")
        published = parse_iso(record.get("published_at_utc"), f"{where}.published_at_utc")
        start = parse_iso(record.get("scheduled_start_utc"), f"{where}.scheduled_start_utc")
        if published >= start:
            raise InputError(f"{where}: publication post-depart")
        key = str(record.get("entry_sha256"))
        previous = index.get(key)
        if previous is None or published < parse_iso(
            previous["published_at_utc"], f"{where}.published_at_utc"
        ):
            index[key] = record
    return index


def verify_journal(
    entries: list[dict[str, Any]], publications: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Verifie la chaine append-only : empreintes, chainage et anteriorite."""
    if not entries:
        raise InputError("Journal vide")
    problems: list[dict[str, Any]] = []
    previous: str | None = None
    last_committed: datetime | None = None
    seen: dict[str, dict[str, Any]] = {}
    for idx, entry in enumerate(entries):
        where = f"entry[{idx}]"
        if not isinstance(entry, dict):
            problems.append({"entry": where, "issue": "entree non objet"})
            continue
        if entry.get("schema_version") not in ({COMMIT_SCHEMA} | LEGACY_COMMIT_SCHEMAS):
            problems.append({"entry": where, "issue": "schema_version inattendu"})
            continue
        try:
            verify_sealed_hash(entry, "entry_sha256", where)
        except InputError as exc:
            problems.append({"entry": where, "issue": str(exc)})
            continue
        if entry.get("prev_entry_sha256") != previous:
            problems.append({"entry": where, "issue": "chainage rompu avec l'entree precedente"})
        key = str(entry.get("race_key"))
        raw_revision = entry.get("revision", 0)
        if isinstance(raw_revision, bool) or not isinstance(raw_revision, int) or raw_revision < 0:
            problems.append({"entry": where, "issue": "numero de revision invalide"})
            revision = -1
        else:
            revision = raw_revision
        if key in seen:
            expected = seen[key]["entry_sha256"]
            if entry.get("supersedes_entry_sha256") != expected:
                problems.append({
                    "entry": where,
                    "issue": f"course {key} republiee sans superseder l'engagement precedent",
                })
            elif revision != int(seen[key].get("revision", 0)) + 1:
                problems.append({"entry": where, "issue": f"numero de revision incoherent: {key}"})
            elif entry.get("revision_reason") not in REVISION_REASONS:
                problems.append({"entry": where, "issue": f"revision sans motif declare: {key}"})
        else:
            if revision != 0:
                problems.append({"entry": where, "issue": f"premiere entree de {key} en revision {revision}"})
            if entry.get("supersedes_entry_sha256") is not None:
                problems.append({"entry": where, "issue": f"premiere entree de {key} avec supersedes"})
            if entry.get("revision_reason") is not None:
                problems.append({"entry": where, "issue": f"premiere entree de {key} avec motif de revision"})
        seen[key] = entry
        try:
            committed = parse_iso(entry.get("committed_at_utc"), f"{where}.committed_at_utc")
            start = parse_iso(entry.get("scheduled_start_utc"), f"{where}.scheduled_start_utc")
            as_of = parse_iso(entry.get("as_of_utc"), f"{where}.as_of_utc")
            if committed >= start:
                problems.append({"entry": where, "issue": "engagement posterieur au depart"})
            if as_of > committed:
                problems.append({"entry": where, "issue": "as_of posterieur a l'engagement"})
            if last_committed is not None and committed < last_committed:
                problems.append({"entry": where, "issue": "ordre chronologique du journal rompu"})
            last_committed = committed
        except InputError as exc:
            problems.append({"entry": where, "issue": str(exc)})
        previous = entry["entry_sha256"]
    revised = [key for key, entry in seen.items() if int(entry.get("revision", 0)) > 0]
    # v11.6 : la regle de gouvernance cesse d'etre une phrase. Une course ne
    # compte dans le denominateur de validation que si son engagement de tete a
    # ete PUBLIE avant le depart ET si son dossier portait un manifeste de
    # collecte scelle. Tout le reste est du travail, pas de la preuve.
    published_index = index_publications(publications) if publications else {}
    unpublished: list[str] = []
    unsealed: list[str] = []
    countable: list[str] = []
    for key, entry in seen.items():
        has_publication = entry.get("entry_sha256") in published_index
        sealed = entry.get("collection_integrity") == "SEALED"
        if not has_publication:
            unpublished.append(key)
        if not sealed:
            unsealed.append(key)
        if has_publication and sealed:
            countable.append(key)
    if publications is None:
        countable = []
    return {
        "n_entries": len(entries),
        "n_races": len(seen),
        "n_revised_races": len(revised),
        "revision_rate": round(len(revised) / len(seen), 4) if seen else 0.0,
        "revision_warning": (
            "un taux de revision eleve doit etre audite: reviser apres avoir vu les "
            "cotes bouger detruirait la valeur probante du journal"
        ),
        "chain_ok": not problems,
        "head_sha256": entries[-1].get("entry_sha256") if entries else None,
        "problems": problems,
        "publications_supplied": publications is not None,
        "n_published_races": len(seen) - len(unpublished),
        "n_unpublished_races": len(unpublished),
        "unpublished_races": sorted(unpublished)[:20],
        "n_unsealed_collection_races": len(unsealed),
        "unsealed_collection_races": sorted(unsealed)[:20],
        "n_countable_races": len(countable),
        "countable_rate": round(len(countable) / len(seen), 4) if seen else 0.0,
        "governance": (
            "une course ne compte que si son engagement de tete a ete publie avant le "
            "depart sur un support opposable ET si son manifeste de collecte est scelle; "
            "n_countable_races est le SEUL compteur qui alimente le denominateur des 200"
        ),
    }


# ---------------------------------------------------------------------------
# 9. ABLATIONS - QUEL MODULE PORTE REELLEMENT DU SIGNAL
# ---------------------------------------------------------------------------

def reseat_declared_ability(variant: dict[str, Any]) -> None:
    """Recopie dans le dossier l'ability_class que l'echelle calcule REELLEMENT.

    Necessaire des qu'une variante d'ablation touche une entree de l'echelle de
    classe sans vouloir neutraliser l'echelle elle-meme. Sans cela, la clause
    anti-ecrasement de la v11.8 refuserait la variante, et le module serait le
    seul qu'on ne saurait pas mesurer - exactement le defaut que la v11.8 avait
    introduit pour ability_class et que la v11.9 a corrige.

    On passe par validate_form_lines et build_class_ladder, jamais par une
    reimplementation: une copie du calcul divergerait tot ou tard du calcul.
    """
    as_of = parse_iso(variant["race"]["as_of"], "race.as_of")
    registry = validate_evidence_registry(variant, as_of)
    form_by_number: dict[int, dict[str, Any]] = {}
    active = [r for r in variant["runners"] if r.get("active") is True]
    for idx, runner in enumerate(active):
        form_by_number[int(runner["no"])] = validate_form_lines(
            runner, registry, as_of, f"runners.active[{idx}]"
        )
    today_allocation = variant["race"].get("allocation_eur")
    if today_allocation is not None:
        today_allocation = float(today_allocation)
    ladder = build_class_ladder(form_by_number, today_allocation)
    computed = dict(ladder.get("ability_by_number", {}))
    for number, item in form_by_number.items():
        if item.get("declared") and number not in computed:
            computed[number] = 0.0
    for runner in active:
        number = int(runner["no"])
        if number in computed:
            runner["score_components"]["ability_class"]["value"] = round(
                float(computed[number]), 4
            )


def ablation_variants(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    variants: dict[str, dict[str, Any]] = {}
    for axis in LEVEL_CAPS:
        variant = json.loads(json.dumps(raw))
        for runner in variant["runners"]:
            if runner.get("active") is True:
                runner["score_components"][axis] = {"value": 0.0, "evidence_ids": []}
                # v11.8 : neutraliser un axe impose de retirer AUSSI ses entrees.
                # ability_class est desormais CALCULE depuis form_lines; laisser
                # form_lines en place ferait echouer la variante sur la clause
                # anti-ecrasement, et le module le plus recent serait justement
                # le seul qu'on ne saurait pas mesurer.
                if axis == "ability_class":
                    runner.pop("form_lines", None)
                # v11.10, meme piege, meme correctif. human_equipment est
                # desormais CALCULE depuis human_records: laisser les
                # enregistrements A/E en place ferait echouer la variante sur la
                # clause anti-ecrasement, et l'axe deviendrait le seul qu'on ne
                # saurait pas mesurer. C'est exactement le defaut que la v11.8
                # avait introduit pour ability_class. Un module qu'on ne sait pas
                # ablater est un module qu'on croit sur parole.
                if axis == "human_equipment":
                    runner.pop("human_records", None)
        variants[f"AXE_{axis}"] = variant
    for channel in ADJUSTMENT_CAPS:
        variant = json.loads(json.dumps(raw))
        for scenario in variant["scenarios"]:
            for channels in scenario["runner_adjustments"].values():
                channels.pop(channel, None)
        variants[f"CANAL_{channel}"] = variant
    # LONGUEUR DE BATTUE (v11.10). Variante dediee: on retire les marges SANS
    # retirer l'historique, pour mesurer ce que la marge ajoute par-dessus le
    # seul rang. Sans elle, la marge ne serait mesurable que confondue avec
    # l'echelle de classe entiere, et l'on croirait le module sur parole.
    margins = json.loads(json.dumps(raw))
    stripped = False
    for runner in margins["runners"]:
        for line in runner.get("form_lines") or []:
            if isinstance(line, dict) and line.pop("beaten_lengths", None) is not None:
                stripped = True
            if isinstance(line, dict):
                line.pop("distance_m", None)
    if stripped:
        # Retirer les marges change l'ability_class que l'echelle calcule: il
        # faut donc la recopier, sinon la variante echoue sur l'anti-ecrasement.
        reseat_declared_ability(margins)
        variants["MARGE_LONGUEURS"] = margins
    uniform = json.loads(json.dumps(raw))
    count = len(uniform["scenarios"])
    share = round(1.0 / count, 6)
    for scenario in uniform["scenarios"]:
        scenario["probability"] = share
    uniform["scenarios"][0]["probability"] = round(1.0 - share * (count - 1), 6)
    variants["POIDS_SCENARIOS_UNIFORMES"] = uniform
    flat_confidence = json.loads(json.dumps(raw))
    for runner in flat_confidence["runners"]:
        if runner.get("active") is True:
            runner["evidence_confidence"] = 1.0
    variants["CONFIANCE_UNIFORME_1"] = flat_confidence
    return variants


def run_ablation(
    raw: dict[str, Any], *, engine_path: str | None = None, current: datetime | None = None,
    worlds: int = 400, races: int = 100,
) -> dict[str, Any]:
    """Neutralise chaque module a tour de role et mesure l'impact sur la hierarchie.

    La graine est GELEE sur celle du dossier complet pour toutes les variantes.
    Sans cela, modifier un score changerait le sport_input_hash, donc la graine,
    donc l'ordre - et l'on mesurerait du bruit Monte-Carlo au lieu d'un effet
    de module. Le nombre de tirages est reduit : c'est un diagnostic comparatif,
    pas un rapport scellable.
    """
    current = (current or now_utc()).astimezone(timezone.utc).replace(microsecond=0)
    diagnostic = json.loads(json.dumps(raw))
    diagnostic["simulation"]["n_worlds"] = worlds
    diagnostic["simulation"]["races_per_world"] = races
    baseline, _ = build_report(
        diagnostic, engine_path=engine_path, current=current, allow_test_samples=True
    )
    frozen_seed = int(baseline["simulation"]["seed"])
    baseline_order = baseline["render"]["verdict"]["strict_order"]
    baseline_notes = {item["no"]: item["note"] for item in baseline["render"]["horses"]}
    results = []
    for label, variant in ablation_variants(diagnostic).items():
        try:
            report, _ = build_report(
                variant, engine_path=engine_path, current=current,
                allow_test_samples=True, seed_override=frozen_seed,
            )
        except InputError as exc:
            results.append({"module": label, "status": "INVALIDE", "error": str(exc)})
            continue
        order = report["render"]["verdict"]["strict_order"]
        notes = {item["no"]: item["note"] for item in report["render"]["horses"]}
        rho = spearman_from_orders(baseline_order, order)
        moved = sum(1 for a, b in zip(baseline_order, order) if a != b)
        note_deltas = {no: round(notes[no] - baseline_notes[no], 3) for no in baseline_notes}
        worst = max(note_deltas.items(), key=lambda kv: abs(kv[1]))
        results.append(
            {
                "module": label,
                "status": "OK",
                "order": order,
                "spearman_vs_baseline": None if rho is None else round(rho, 5),
                "positions_changed": moved,
                "top3_changed": baseline_order[:3] != order[:3],
                "winner_changed": baseline_order[0] != order[0],
                "max_abs_note_delta": abs(worst[1]),
                "most_affected_runner": worst[0],
            }
        )
    ranked = sorted(
        [
            item for item in results
            if item["status"] == "OK"
            and (item["positions_changed"] > 0 or item["max_abs_note_delta"] >= 0.05)
        ],
        key=lambda item: (
            item["spearman_vs_baseline"] if item["spearman_vs_baseline"] is not None else 2.0,
            -item["max_abs_note_delta"],
        ),
    )
    ablation = {
        "schema_version": ABLATION_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "race_key": diagnostic["race"]["race_key"],
        "mode": "DIAGNOSTIC_REDUCED_SAMPLES_FROZEN_SEED",
        "n_worlds": worlds,
        "races_per_world": races,
        "frozen_seed": frozen_seed,
        "baseline_order": baseline_order,
        "modules": results,
        "most_influential": [item["module"] for item in ranked[:3]],
        "no_effect": [
            item["module"] for item in results
            if item["status"] == "OK" and item["positions_changed"] == 0
            and item["max_abs_note_delta"] < 0.05
        ],
        "warning": (
            "une ablation mesure la sensibilite de la hierarchie a un module, pas la "
            "valeur predictive de ce module; seule une comparaison hors echantillon le peut"
        ),
    }
    ablation["ablation_sha256"] = sha256_obj(ablation)
    return ablation


# ---------------------------------------------------------------------------
# 10. AUDIT APRES-COURSE
# ---------------------------------------------------------------------------

def validate_result(result: dict[str, Any], report: dict[str, Any]) -> list[int]:
    accepted = {RESULT_SCHEMA} | LEGACY_RESULT_SCHEMAS
    if not isinstance(result, dict) or result.get("schema_version") not in accepted:
        raise InputError(f"Resultat au schema parmi {sorted(accepted)} requis")
    if result.get("template_only") is True:
        raise InputError("Template resultat non renseigne")
    if result.get("race_key") != report["race_key"]:
        raise InputError("race_key du resultat incompatible")
    if result.get("verified") is not True or result.get("status") != "official_final":
        raise InputError("Resultat officiel final et verifie requis")
    require_url(require(result, "source_url", "result"), "result.source_url")
    verified_at = parse_iso(require(result, "verified_at", "result"), "result.verified_at")
    start = parse_iso(report["clock"]["scheduled_start_utc"], "report.clock.scheduled_start_utc")
    if verified_at <= start:
        raise InputError("verified_at doit etre posterieur au depart")
    if result.get("dead_heat") is True:
        raise InputError("Dead-heat non pris en charge dans cette version")
    order = require(result, "order", "result")
    if not isinstance(order, list) or not order:
        raise InputError("result.order doit etre une liste non vide")
    if any(isinstance(x, bool) or not isinstance(x, int) for x in order):
        raise InputError("result.order doit contenir des numeros entiers")
    if len(set(order)) != len(order):
        raise InputError("Doublon dans result.order")
    active = set(report["render"]["verdict"]["strict_order"])
    if not set(order) <= active:
        raise InputError("result.order contient un non-partant")
    realized = result.get("realized_scenario_id")
    if realized is not None:
        realized = require_text(realized, "result.realized_scenario_id")
        declared = {str(item["id"]) for item in report["render"]["scenarios"]}
        if realized not in declared:
            raise InputError(
                f"realized_scenario_id absent des scenarios declares: {realized}"
            )
        require_url(
            require(result, "scenario_label_source_url", "result"),
            "result.scenario_label_source_url",
        )
        require_text(
            require(result, "scenario_label_basis", "result"),
            "result.scenario_label_basis",
        )
    return order


def spearman_from_orders(predicted: list[int], actual: list[int]) -> float | None:
    if set(predicted) != set(actual) or len(predicted) < 2:
        return None
    n = len(predicted)
    pred_rank = {number: idx + 1 for idx, number in enumerate(predicted)}
    actual_rank = {number: idx + 1 for idx, number in enumerate(actual)}
    squared = sum((pred_rank[number] - actual_rank[number]) ** 2 for number in predicted)
    return 1.0 - 6.0 * squared / (n * (n * n - 1))


def market_comparison_from_ticket(
    report: dict[str, Any], ticket: dict[str, Any], commit: dict[str, Any] | None,
) -> dict[str, Any]:
    """Extrait le benchmark marche d'un ticket cree APRES le SPORT-LOCK.

    Le rapport sportif reste market=null. Le ticket scelle fournit la chaine
    report -> engagement -> snapshot -> probabilites Shin sans reanalyser le
    sport et sans faire entrer les cotes dans le coeur sportif.
    """
    verify_sealed_hash(ticket, "ticket_sha256", "TICKET")
    if ticket.get("schema_version") not in ACCEPTED_TICKET_SCHEMAS:
        raise InputError(
            f"Ticket au schema parmi {sorted(ACCEPTED_TICKET_SCHEMAS)} requis"
        )
    context = require(ticket, "context", "ticket")
    if context.get("race_key") != report["race_key"]:
        raise InputError("Le ticket porte sur une autre course")
    if context.get("report_sha256") != report["report_sha256"]:
        raise InputError("Le ticket n'est pas chaine au rapport audite")
    if context.get("rank_samples_sha256") != report["rank_samples_sha256"]:
        raise InputError("Le ticket n'est pas chaine aux tirages du rapport")
    if commit is None:
        raise InputError("Un ticket utilise pour calibration exige le COMMIT correspondant")
    if context.get("entry_sha256") != commit.get("entry_sha256"):
        raise InputError("Le ticket n'est pas chaine a l'engagement fourni")
    observed = parse_iso(
        context.get("market_observed_at_utc"), "ticket.context.market_observed_at_utc"
    )
    start = parse_iso(
        report["clock"]["scheduled_start_utc"], "report.clock.scheduled_start_utc"
    )
    if observed >= start:
        raise InputError("Le snapshot du ticket est posterieur au depart")
    snapshot_sha = context.get("market_snapshot_sha256")
    if not isinstance(snapshot_sha, str) or len(snapshot_sha) != 64:
        raise InputError("Le ticket ne porte pas d'empreinte du snapshot marche")
    raw = require(ticket, "probabilities_market_shin", "ticket")
    active = report["render"]["verdict"]["strict_order"]
    if set(raw) != {str(number) for number in active}:
        raise InputError("Le ticket ne couvre pas exactement tous les partants actifs")
    probabilities = {
        str(number): as_float(raw[str(number)], f"ticket.probabilities_market_shin.{number}")
        for number in active
    }
    if any(value <= 0.0 or value >= 1.0 for value in probabilities.values()):
        raise InputError("Probabilite marche du ticket hors ]0,1[")
    if abs(sum(probabilities.values()) - 1.0) > 2e-5:
        raise InputError("Les probabilites marche du ticket ne somment pas a 1")
    consensus_order = sorted(active, key=lambda number: (-probabilities[str(number)], number))
    return {
        "source": "TITAN_STAKE_POST_SPORT_LOCK",
        "devig_method": "SHIN",
        "observed_at_utc": iso_utc(observed),
        "market_snapshot_sha256": snapshot_sha,
        "ticket_sha256": ticket["ticket_sha256"],
        "consensus_order": consensus_order,
        "internal_consensus": {
            "values": probabilities,
            "sum": round(sum(probabilities.values()), 6),
        },
    }


def documentation_confounding(
    report: dict[str, Any], order: list[int]
) -> dict[str, Any]:
    """Le test qui tranche : la documentation predit-elle le MODELE ou l'ARRIVEE ?

    documentation_bias, calcule avant la course, mesure a quel point le classement
    suit le volume de dossier. Seul, ce nombre est ambigu: mieux documenter un bon
    cheval le fait legitimement monter.

    Ici on dispose de l'arrivee. On calcule la MEME correlation contre le rang
    reel. Deux lectures s'opposent alors:

      rho_model eleve ET rho_result eleve  -> la documentation revele une vraie
                                              superiorite. Tout va bien.
      rho_model eleve ET rho_result nul    -> le classement separe les chevaux
                                              par leur volume d'information et
                                              non par leur valeur. CONFONDANT.

    Une course ne tranche rien; l'ecart moyen sur plusieurs dizaines de courses
    tranche. Le champ est donc emis a chaque audit pour etre agrege ensuite.
    """
    entries = report["render"].get("horses") or []
    if len(entries) < 4:
        return {"status": "INDETERMINE", "n": len(entries)}
    finish = {number: idx + 1 for idx, number in enumerate(order)}
    unplaced = len(order) + 1
    confidence: list[float] = []
    model_rank: list[float] = []
    actual_rank: list[float] = []
    for entry in entries:
        confidence.append(float(entry["evidence_confidence"]))
        model_rank.append(-float(entry["expected_rank"]))
        actual_rank.append(-float(finish.get(entry["no"], unplaced)))
    rho_model = spearman_rho(confidence, model_rank)
    rho_result = spearman_rho(confidence, actual_rank)
    gap = rho_model - rho_result
    if abs(rho_model) < 0.35:
        status = "NON_APPLICABLE"
    elif abs(rho_result) >= 0.35 and gap < 0.40:
        status = "DOCUMENTATION_INFORMATIVE"
    elif gap >= 0.40:
        status = "CONFONDANT_SUSPECTE"
    else:
        status = "AMBIGU"
    return {
        "status": status,
        "n": len(entries),
        "rho_confidence_vs_model_rank": round(rho_model, 4),
        "rho_confidence_vs_actual_rank": round(rho_result, 4),
        "gap": round(gap, 4),
        "single_race_warning": (
            "une course seule ne prouve rien; agreger ce champ sur au moins 30 "
            "courses avant d'en tirer une conclusion sur le remplissage"
        ),
    }


def audit_report(
    report: dict[str, Any], result: dict[str, Any], *,
    commit: dict[str, Any] | None = None,
    ticket: dict[str, Any] | None = None,
    publication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verify_sealed_hash(report, "report_sha256", "REPORT")
    if report.get("schema_version") != REPORT_SCHEMA:
        raise InputError(f"Rapport au schema {REPORT_SCHEMA} requis")
    order = validate_result(result, report)
    predicted = report["render"]["verdict"]["strict_order"]
    predicted_rank = {number: idx + 1 for idx, number in enumerate(predicted)}
    winner = order[0]
    rank_matrix_data = report["internal"]["rank_matrix"]
    p_win = float(rank_matrix_data[str(winner)][0])
    winner_log_loss = -math.log(max(p_win, 1e-12))
    top3_actual = set(order[:3]) if len(order) >= 3 else None
    top3_pred = set(predicted[:3])
    overlap = len(top3_actual & top3_pred) if top3_actual is not None else None
    scenario_scores = []
    if len(order) == len(predicted):
        for scenario in report["render"]["scenarios"]:
            rho = spearman_from_orders(scenario["order"], order)
            scenario_scores.append({"id": scenario["id"], "spearman": rho})
        scenario_scores.sort(key=lambda item: -(item["spearman"] if item["spearman"] is not None else -2.0))
    market = (
        market_comparison_from_ticket(report, ticket, commit)
        if ticket is not None else None
    )
    market_log_loss = None
    market_rank_of_winner = None
    market_comparison = None
    if market is not None:
        consensus = market["internal_consensus"]["values"]
        market_log_loss = round(-math.log(max(float(consensus[str(winner)]), 1e-12)), 6)
        market_rank_of_winner = market["consensus_order"].index(winner) + 1
        # Le consensus de-vigue est REPORTE dans l'audit, sans quoi la couche de
        # mise ne peut pas ajuster son melange: elle a besoin du couple
        # (probabilite modele, probabilite marche) course par course.
        market_comparison = {
            "devig_method": market["devig_method"],
            "consensus_order": market["consensus_order"],
            "source": market["source"],
            "observed_at_utc": market["observed_at_utc"],
            "market_snapshot_sha256": market["market_snapshot_sha256"],
            "ticket_sha256": market["ticket_sha256"],
            "spearman_sport_vs_market": spearman_from_orders(
                predicted, market["consensus_order"]
            ),
            "internal_consensus": market["internal_consensus"],
        }
    commitment = None
    if commit is not None:
        verify_sealed_hash(commit, "entry_sha256", "COMMIT")
        if commit.get("schema_version") not in ({COMMIT_SCHEMA} | LEGACY_COMMIT_SCHEMAS):
            raise InputError("Schema COMMIT incompatible")
        if commit.get("race_key") != report["race_key"]:
            raise InputError("L'engagement publie porte sur une autre course")
        if commit.get("report_sha256") != report["report_sha256"]:
            raise InputError("L'engagement publie ne correspond pas a ce rapport")
        committed_at = parse_iso(commit.get("committed_at_utc"), "commit.committed_at_utc")
        scheduled_start = parse_iso(
            report["clock"]["scheduled_start_utc"], "report.clock.scheduled_start_utc"
        )
        if committed_at >= scheduled_start:
            raise InputError("Engagement post-depart interdit dans l'audit")
        published_record = None
        if publication is not None:
            index = index_publications([publication])
            published_record = index.get(commit["entry_sha256"])
            if published_record is None:
                raise InputError(
                    "L'attestation de publication ne porte pas sur cet engagement"
                )
        collection_integrity = (
            commit.get("collection_integrity")
            or report.get("collection", {}).get("integrity")
            or "ABSENT"
        )
        commitment = {
            "committed_at_utc": commit["committed_at_utc"],
            "minutes_before_start": commit["minutes_before_start"],
            "entry_sha256": commit["entry_sha256"],
            "collection_integrity": collection_integrity,
            "published": published_record is not None,
            "publication_medium": (
                published_record["medium"] if published_record else None
            ),
            "publication_reference": (
                published_record["reference"] if published_record else None
            ),
            "published_minutes_before_start": (
                published_record["minutes_before_start"] if published_record else None
            ),
            # Le seul champ que la calibration doit lire. Une course EXPLORATOIRE
            # est du travail utile et une preuve nulle: elle ne doit jamais entrer
            # dans le denominateur des 200, sous peine de le gonfler avec des
            # courses qu'aucun tiers ne peut verifier.
            "evidential_status": (
                "PROBANTE"
                if published_record is not None and collection_integrity == "SEALED"
                else "EXPLORATOIRE"
            ),
            "evidential_reason": (
                None
                if published_record is not None and collection_integrity == "SEALED"
                else "; ".join(filter(None, [
                    None if published_record is not None
                    else "engagement non publie avant le depart sur un support opposable",
                    None if collection_integrity == "SEALED"
                    else f"manifeste de collecte non scelle ({collection_integrity})",
                ]))
            ),
        }
    realized_scenario = result.get("realized_scenario_id")
    audit = {
        "schema_version": AUDIT_SCHEMA,
        "engine_version": report["engine_version"],
        "model_version": report.get("model_version", MODEL_VERSION),
        "race_key": report["race_key"],
        "scheduled_start_utc": report["clock"]["scheduled_start_utc"],
        "fill_mode": report.get("fill_mode", "FULL"),
        "documentation_confounding": documentation_confounding(report, order),
        "winner": winner,
        "winner_hierarchy_rank": predicted_rank[winner],
        "winner_log_loss_internal": round(winner_log_loss, 6),
        "internal_model_probabilities": {
            str(number): float(rank_matrix_data[str(number)][0]) for number in predicted
        },
        "uniform_winner_log_loss": round(math.log(len(predicted)), 6),
        "market_winner_log_loss": market_log_loss,
        "market_rank_of_winner": market_rank_of_winner,
        "market_comparison": market_comparison,
        "top3_overlap": overlap,
        "spearman_full_order": spearman_from_orders(predicted, order),
        "best_matching_scenario": scenario_scores[0] if scenario_scores else None,
        "realized_scenario_id": realized_scenario,
        "scenario_label": (
            {
                "id": realized_scenario,
                "source_url": result.get("scenario_label_source_url"),
                "basis": result.get("scenario_label_basis"),
                "rule": result.get("scenario_label_rule"),
            }
            if realized_scenario is not None else None
        ),
        "scenario_declared_probabilities": {
            scenario["id"]: round(scenario["probability_pct"] / 100.0, 6)
            for scenario in report["render"]["scenarios"]
        },
        "prior_commitment": commitment,
        "report_sha256": report["report_sha256"],
        "result": result,
    }
    audit["audit_sha256"] = sha256_obj(audit)
    return audit


def batch_audit(audits: list[dict[str, Any]]) -> dict[str, Any]:
    if not audits:
        raise InputError("Au moins un audit requis")
    seen: set[str] = set()
    for audit in audits:
        if audit.get("schema_version") != AUDIT_SCHEMA:
            raise InputError(f"Audit au schema {AUDIT_SCHEMA} requis")
        verify_sealed_hash(audit, "audit_sha256", "AUDIT")
        key = audit["race_key"]
        if key in seen:
            raise InputError(f"Course dupliquee dans le lot: {key}")
        seen.add(key)
    n = len(audits)
    if n < 50:
        status = "INSUFFICIENT_FOR_RECALIBRATION"
    elif n < 200:
        status = "DIAGNOSTIC_ONLY"
    else:
        status = "ELIGIBLE_TEMPORAL_OOS_REVIEW"
    sport_losses = [float(item["winner_log_loss_internal"]) for item in audits]
    uniform_losses = [float(item["uniform_winner_log_loss"]) for item in audits]
    winners_top3 = [1.0 if item["winner_hierarchy_rank"] <= 3 else 0.0 for item in audits]
    paired = [
        (float(item["winner_log_loss_internal"]), float(item["market_winner_log_loss"]))
        for item in audits if item.get("market_winner_log_loss") is not None
    ]
    committed = sum(1 for item in audits if item.get("prior_commitment") is not None)
    market_block: dict[str, Any] = {
        "n_races_with_market": len(paired),
        "verdict": "AUCUN_BENCHMARK_MARCHE",
    }
    if paired:
        sport_paired = np.asarray([a for a, _ in paired])
        market_paired = np.asarray([b for _, b in paired])
        difference = sport_paired - market_paired
        mean_difference = float(np.mean(difference))
        paired_audits = [
            item for item in audits if item.get("market_winner_log_loss") is not None
        ]
        day_clusters: dict[str, list[int]] = {}
        for index_value, item in enumerate(paired_audits):
            timestamp = item.get("scheduled_start_utc")
            if isinstance(timestamp, str):
                day = parse_iso(timestamp, "audit.scheduled_start_utc").date().isoformat()
            else:
                day = f"UNKNOWN-{index_value}"
            day_clusters.setdefault(day, []).append(index_value)
        rng = np.random.default_rng(20260903)
        bootstrap = np.empty(4000, dtype=float)
        days = sorted(day_clusters)
        for draw in range(len(bootstrap)):
            chosen = rng.choice(days, size=len(days), replace=True)
            indices = [idx for day in chosen for idx in day_clusters[str(day)]]
            bootstrap[draw] = float(np.mean(difference[indices]))
        ci_low = float(np.quantile(bootstrap, 0.025))
        ci_high = float(np.quantile(bootstrap, 0.975))
        market_block = {
            "n_races_with_market": len(paired),
            "n_day_clusters": len(day_clusters),
            "mean_winner_log_loss_sport": round(float(np.mean(sport_paired)), 6),
            "mean_winner_log_loss_market": round(float(np.mean(market_paired)), 6),
            "mean_difference_sport_minus_market": round(mean_difference, 6),
            "cluster_bootstrap_ci95": [round(ci_low, 6), round(ci_high, 6)],
            "verdict": (
                "SPORT_MEILLEUR_QUE_MARCHE_A_95" if ci_high < 0 else
                "MARCHE_MEILLEUR_QUE_SPORT_A_95" if ci_low > 0 else
                "INDISCERNABLE"
            ),
            "caveat": (
                "bootstrap groupe par journee; la conclusion predictive exige encore un "
                "lot temporellement separe et un protocole de challenge pre-enregistre"
            ),
        }
    result = {
        "schema_version": BATCH_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "n_races": n,
        "status": status,
        "n_races_committed_before_start": committed,
        "commitment_coverage": round(committed / n, 4),
        "n_full_mode": sum(1 for item in audits if item.get("fill_mode", "FULL") == "FULL"),
        "n_lite_mode": sum(1 for item in audits if item.get("fill_mode") == "LITE"),
        "mean_winner_log_loss_sport": round(float(np.mean(sport_losses)), 6),
        "mean_winner_log_loss_uniform": round(float(np.mean(uniform_losses)), 6),
        "winner_in_top3_rate": round(float(np.mean(winners_top3)), 6),
        "market_benchmark": market_block,
        "governance": "split temporel, bootstrap par course, un seul changement de module a la fois",
        "warning": (
            "battre l'uniforme n'est pas une performance; seule la comparaison au consensus de "
            "marche sur des previsions engagees avant le depart a une valeur probante"
        ),
    }
    result["batch_sha256"] = sha256_obj(result)
    return result


# ---------------------------------------------------------------------------
# 14. CHEMIN LEGER - LA LARGEUR PRIME SUR LA PROFONDEUR
# ---------------------------------------------------------------------------
#
# Le systeme complet optimise la PROFONDEUR par course. En hippisme, l'avantage
# vient de la LARGEUR sur les courses : quarante minutes passees sur une course
# valent moins que les memes quarante minutes reparties sur la deux-centieme
# course d'un journal.
#
# Le chemin leger existe pour cela, et pour cela seulement. Il ne pretend pas
# egaler un dossier complet : il permet d'en remplir vingt par jour au lieu de
# deux, donc de faire courir le compteur d'engagements en semaines plutot qu'en
# mois. Les dossiers legers sont marques comme tels et plafonnes en grade C.
#
# Regle d'independance respectee sans triche : un remplissage leger repose sur
# DEUX lectures distinctes au plus - la carte officielle et l'historique de
# resultats. Il n'ouvre donc que deux axes de niveau, plus le canal de rythme.
# Pretendre en ouvrir six a partir d'une seule lecture violerait la regle de
# famille du chapitre 4.

LITE_SCHEMA = "titan-plat-lite-11.5"
RETEST_SCHEMA = "titan-plat-retest-11.5"
SCENARIO_AUDIT_SCHEMA = "titan-plat-scenario-audit-11.5"
ABLATION_BATCH_SCHEMA = "titan-plat-ablation-batch-11.5"
CHALLENGE_SCHEMA = "titan-plat-challenge-11.5"

# Coefficients de rythme declares, provisoires, non calibres. Ils encodent une
# regle de lecture, pas une estimation : un train soutenu use les chevaux de
# tete et sert les finisseurs, un train lent fait l'inverse.
LITE_PACE_COEFFICIENTS = {
    "leader": {"SOUTENU": -0.30, "LENT": 0.25},
    "prominent": {"SOUTENU": -0.15, "LENT": 0.12},
    "midfield": {"SOUTENU": 0.10, "LENT": -0.08},
    "closer": {"SOUTENU": 0.30, "LENT": -0.25},
}
LITE_ROLES = set(LITE_PACE_COEFFICIENTS)


def lite_template() -> dict[str, Any]:
    return {
        "schema_version": LITE_SCHEMA,
        "template_only": True,
        "race": {
            "race_key": "YYYY-MM-DD|HIPPODROME|R1C1",
            "course_name": "A_REMPLACER",
            "course": "HIPPODROME",
            "race_type": "handicap",
            "surface": "gazon",
            "distance_m": 1600,
            "scheduled_start": "YYYY-MM-DDTHH:MM:SS+02:00",
            "as_of": "YYYY-MM-DDTHH:MM:SS+02:00",
            "field_declared_final": True,
            "card_source_url": "https://www.france-galop.com/",
            "results_source_url": "https://www.france-galop.com/",
        },
        "runners": [
            {"no": 1, "name": "A_REMPLACER", "level": 0.0, "form": 0.0,
             "level_basis": "", "form_basis": "", "confidence": 0.50,
             "pace_role": "midfield"},
            {"no": 2, "name": "A_REMPLACER", "level": 0.0, "form": 0.0,
             "level_basis": "", "form_basis": "", "confidence": 0.50,
             "pace_role": "midfield"},
            {"no": 3, "name": "A_REMPLACER", "level": 0.0, "form": 0.0,
             "level_basis": "", "form_basis": "", "confidence": 0.50,
             "pace_role": "midfield"},
        ],
        "pace_split": {"SOUTENU": 0.30, "REGULIER": 0.45, "LENT": 0.19},
        "residual_mass": 0.06,
        "help": {
            "level": "niveau intrinseque, lu sur la carte officielle, entre -0.90 et 0.90",
            "form": "forme recente, lue sur l'historique de resultats, entre -0.60 et 0.60",
            "confidence": "part du dossier reellement documentee, entre 0.05 et 0.80 en LITE",
            "level_basis": "fait ou comparable precis justifiant toute valeur non nulle",
            "form_basis": "course(s) et observation precise justifiant toute valeur non nulle",
            "pace_role": "leader | prominent | midfield | closer",
            "regle": "zero est la bonne valeur quand la preuve ne separe pas",
        },
    }


def expand_lite(lite: dict[str, Any]) -> dict[str, Any]:
    """Transforme un dossier leger en INPUT.json complet et valide.

    L'expansion n'invente aucune information. Elle traduit mecaniquement deux
    lectures en un registre de preuves coherent, avec des familles distinctes et
    des incertitudes elargies pour refleter la superficialite du remplissage.
    """
    if lite.get("schema_version") != LITE_SCHEMA:
        raise InputError(f"schema_version attendu: {LITE_SCHEMA}")
    if lite.get("template_only") is True:
        raise InputError("Template leger vierge: renseigner la course")
    race = require(lite, "race", "root")
    for key in ("race_key", "course_name", "course", "race_type", "surface"):
        require_text(require(race, key, "race"), f"race.{key}")
    card_url = require_url(require(race, "card_source_url", "race"), "race.card_source_url")
    results_url = require_url(require(race, "results_source_url", "race"), "race.results_source_url")
    as_of = require_text(require(race, "as_of", "race"), "race.as_of")
    parse_iso(as_of, "race.as_of")
    runners = require(lite, "runners", "root")
    if not isinstance(runners, list) or len(runners) < 3:
        raise InputError("Au moins trois partants sont requis")

    registry: dict[str, Any] = {
        "SRC-CARD": {
            "tier": "F-P1", "family_id": "SOURCE-CARD",
            "summary": "Carte officielle de la course",
            "source_url": card_url, "observed_at": as_of, "available_at": as_of,
            "parent_ids": [],
        },
        "SRC-RESULTS": {
            "tier": "F-S1", "family_id": "SOURCE-RESULTS",
            "summary": "Historique de resultats consulte",
            "source_url": results_url, "observed_at": as_of, "available_at": as_of,
            "parent_ids": [],
        },
    }
    full_runners: list[dict[str, Any]] = []
    roles: dict[int, str] = {}
    for index, runner in enumerate(runners):
        where = f"runners[{index}]"
        number = require(runner, "no", where)
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise InputError(f"Numero entier positif attendu: {where}.no")
        name = require_text(require(runner, "name", where), f"{where}.name")
        level = as_float(runner.get("level", 0.0), f"{where}.level")
        form = as_float(runner.get("form", 0.0), f"{where}.form")
        confidence = as_probability(runner.get("confidence", 0.50), f"{where}.confidence")
        if not 0.05 <= confidence <= 0.80:
            raise InputError(f"confidence LITE doit etre dans [0.05,0.80]: {where}")
        level_basis = str(runner.get("level_basis", "")).strip()
        form_basis = str(runner.get("form_basis", "")).strip()
        if level and not level_basis:
            raise InputError(f"level_basis obligatoire quand level est non nul: {where}")
        if form and not form_basis:
            raise InputError(f"form_basis obligatoire quand form est non nul: {where}")
        role = require_text(runner.get("pace_role", "midfield"), f"{where}.pace_role")
        if role not in LITE_ROLES:
            raise InputError(f"pace_role invalide: {where} ({sorted(LITE_ROLES)})")
        if abs(level) > LEVEL_CAPS["ability_class"]:
            raise InputError(f"level hors +/-{LEVEL_CAPS['ability_class']}: {where}")
        if abs(form) > LEVEL_CAPS["recent_form"]:
            raise InputError(f"form hors +/-{LEVEL_CAPS['recent_form']}: {where}")
        roles[number] = role
        registry[f"ID-{number}"] = {
            "tier": "F-P1", "family_id": f"IDENTITY-{number}",
            "summary": f"Identite officielle du numero {number}",
            "source_url": card_url, "observed_at": as_of, "available_at": as_of,
            "parent_ids": [],
        }
        registry[f"LVL-{number}"] = {
            "tier": "I", "family_id": f"CARD-{number}",
            "summary": level_basis or f"Niveau non separant (zero), numero {number}",
            "observed_at": as_of, "available_at": as_of, "parent_ids": ["SRC-CARD"],
        }
        registry[f"FRM-{number}"] = {
            "tier": "I", "family_id": f"RESULTS-{number}",
            "summary": form_basis or f"Forme non separante (zero), numero {number}",
            "observed_at": as_of, "available_at": as_of, "parent_ids": ["SRC-RESULTS"],
        }
        registry[f"PCE-{number}"] = {
            "tier": "H", "family_id": f"PACEMAP-{number}",
            "summary": f"Role de rythme suppose ({role}), numero {number}",
            "observed_at": as_of, "available_at": as_of, "parent_ids": ["SRC-CARD"],
        }
        components = {axis: {"value": 0.0, "evidence_ids": []} for axis in LEVEL_CAPS}
        components["ability_class"] = {
            "value": level, "evidence_ids": [f"LVL-{number}"] if level else []
        }
        components["recent_form"] = {
            "value": form, "evidence_ids": [f"FRM-{number}"] if form else []
        }
        full_runners.append({
            "no": number, "name": name, "active": True,
            "identity_evidence_ids": [f"ID-{number}"],
            "score_components": components,
            "evidence_confidence": confidence,
            # Incertitudes elargies: un remplissage leger sait moins de choses,
            # et doit le dire dans la distribution plutot que dans le score.
            "aleatory_sd": 1.05,
            "epistemic_sd": 0.90,
            "factor_loadings": {}, "factor_evidence": {},
        })

    split = require(lite, "pace_split", "root")
    if not isinstance(split, dict) or set(split) != {"SOUTENU", "REGULIER", "LENT"}:
        raise InputError("pace_split doit contenir exactement SOUTENU, REGULIER et LENT")
    residual = as_probability(require(lite, "residual_mass", "root"), "residual_mass")
    if residual < 0.03:
        raise InputError("residual_mass doit valoir au moins 0.03")
    total = sum(as_probability(v, f"pace_split.{k}") for k, v in split.items()) + residual
    if abs(total - 1.0) > 1e-8:
        raise InputError(f"pace_split + residual_mass doit sommer a 1 (obtenu {total:.8f})")

    numbers = [int(r["no"]) for r in full_runners]

    def adjustments(scenario: str) -> dict[str, Any]:
        block: dict[str, Any] = {}
        for number in numbers:
            value = LITE_PACE_COEFFICIENTS[roles[number]].get(scenario, 0.0)
            block[str(number)] = (
                {"pace": {"value": value, "evidence_ids": [f"PCE-{number}"]}} if value else {}
            )
        return block

    scenarios = [
        {"id": "REGULIER", "probability": float(split["REGULIER"]),
         "mechanism": "train regulier sans duel durable en tete",
         "probability_basis": "repartition des roles de rythme declaree",
         "failure_mode": "un partant force le train des le depart",
         "is_residual": False, "evidence_ids": ["SRC-CARD"],
         "runner_adjustments": {str(n): {} for n in numbers},
         "factor_means": {}, "noise_multiplier": 1.0},
        {"id": "SOUTENU", "probability": float(split["SOUTENU"]),
         "mechanism": "pression precoce, course d'usure, finisseurs avantages",
         "probability_basis": "nombre d'animateurs credibles sur la carte",
         "failure_mode": "un animateur renonce et le train retombe",
         "is_residual": False, "evidence_ids": ["SRC-CARD"],
         "runner_adjustments": adjustments("SOUTENU"),
         "factor_means": {}, "noise_multiplier": 1.05},
        {"id": "LENT", "probability": float(split["LENT"]),
         "mechanism": "compression du peloton et acceleration tardive",
         "probability_basis": "absence d'animateur decide sur la carte",
         "failure_mode": "un jockey prend l'initiative de durcir",
         "is_residual": False, "evidence_ids": ["SRC-CARD"],
         "runner_adjustments": adjustments("LENT"),
         "factor_means": {}, "noise_multiplier": 1.0},
        {"id": "AUTRE_COURSE", "probability": residual,
         "mechanism": "incident, tactique ou evolution non modelisee",
         "probability_basis": "masse residuelle explicite",
         "failure_mode": "branche volontairement peu descriptive",
         "is_residual": True, "evidence_ids": [],
         "runner_adjustments": {str(n): {} for n in numbers},
         "factor_means": {}, "noise_multiplier": 1.30},
    ]
    return {
        "schema_version": INPUT_SCHEMA,
        "template_only": False,
        "fill_mode": "LITE",
        "fill_warning": (
            "dossier leger: deux lectures, deux axes de niveau, un canal de rythme. "
            "Concu pour la largeur du journal, pas pour la profondeur d'une course."
        ),
        "race": {
            "race_key": race["race_key"], "course_name": race["course_name"],
            "course": race["course"], "race_type": race["race_type"],
            "surface": race["surface"],
            "distance_m": require(race, "distance_m", "race"),
            "scheduled_start": require_text(require(race, "scheduled_start", "race"),
                                            "race.scheduled_start"),
            "as_of": as_of,
            "field_declared_final": bool(race.get("field_declared_final", False)),
            # Un remplissage de trois minutes n'est pas un dossier de grade A.
            "data_grade": "C",
            "official_source_url": card_url,
        },
        "simulation": {
            "n_worlds": 2000, "races_per_world": 100, "student_df": 5.0,
            "scenario_confidence": "LOW", "calibration_status": "UNVALIDATED",
            "calibration_sha256": None,
        },
        "uncertainty": {
            "race_missingness": 0.60,
            "critical_unknown": False,
        },
        "evidence_registry": registry,
        # Le manifeste LITE est deduit des deux seules URL que le dossier leger
        # utilise. Il n'invente rien: si le collecteur a ouvert autre chose, il
        # doit l'ajouter a la main avant de sceller. Un dossier LITE reste
        # EXPLORATOIRE et ne compte jamais dans le denominateur des 200.
        "collection": {
            "collector": require_text(
                lite.get("collector", "LITE_EXPANSION"), "lite.collector"
            ),
            "attestation": (
                "manifeste deduit de l'expansion LITE; toute page supplementaire "
                "ouverte avant as_of doit etre ajoutee manuellement"
            ),
            "sources": [
                {"url": card_url, "fetched_at": as_of, "price_bearing": False,
                 "purpose": "carte officielle des partants"},
            ] + ([] if normalise_url(results_url) == normalise_url(card_url) else [
                {"url": results_url, "fetched_at": as_of, "price_bearing": False,
                 "purpose": "historique de resultats"},
            ]),
        },
        "runners": full_runners,
        "scenarios": scenarios,
        "market": None,
        "unknowns": [{
            "id": "LITE_FILL", "severity": "MEDIUM",
            "description": "remplissage leger: sectionnels, replay et trip ledger absents",
            "affected_modules": ["energy_distribution", "traffic_trip", "draw_lane"],
            "evidence_ids": [],
        }],
    }


# ---------------------------------------------------------------------------
# 15. FIDELITE DE SAISIE - LA BORNE QUE PERSONNE NE MESURE
# ---------------------------------------------------------------------------

def input_retest(
    first: dict[str, Any], second: dict[str, Any], *, current: datetime | None = None,
    worlds: int = 400, races: int = 100,
) -> dict[str, Any]:
    """Deux remplissages independants de la MEME course donnent-ils le meme resultat ?

    C'est la mesure la plus negligee de tout le systeme, et la plus decisive.
    Le moteur publie une erreur Monte-Carlo de l'ordre du centieme de rang. Si
    deux saisies humaines de la meme course deplacent les chevaux d'un rang
    entier, cette precision est une illusion : le bruit dominant n'est pas dans
    la simulation, il est dans le clavier.

    Aucun raffinement du moteur ne peut compenser une saisie irreproductible.
    Tant que ce chiffre n'est pas connu, augmenter n_worlds revient a mesurer au
    micron une piece taillee a la hache.
    """
    current = (current or now_utc()).astimezone(timezone.utc).replace(microsecond=0)
    if first["race"]["race_key"] != second["race"]["race_key"]:
        raise InputError("Le test-retest exige deux saisies de la MEME course")
    if first["race"].get("scheduled_start") != second["race"].get("scheduled_start"):
        raise InputError("Le test-retest exige le meme horaire officiel")
    if first["race"].get("as_of") != second["race"].get("as_of"):
        raise InputError(
            "Le test-retest de saisie exige le meme as_of. Deux gels differents "
            "mesurent une derive d'information, pas la reproductibilite de saisie."
        )
    shared_seed_payload = (
        f"RETEST|{MODEL_VERSION}|{first['race']['race_key']}|{first['race']['as_of']}"
    )
    shared_seed = int.from_bytes(
        hashlib.sha256(shared_seed_payload.encode("utf-8")).digest()[:8], "big"
    ) % (2**32)
    reports = []
    for candidate in (first, second):
        reduced = json.loads(json.dumps(candidate))
        reduced["simulation"]["n_worlds"] = worlds
        reduced["simulation"]["races_per_world"] = races
        report, _ = build_report(
            reduced, current=current, allow_test_samples=True,
            seed_override=shared_seed,
        )
        reports.append(report)
    a, b = reports
    order_a = a["render"]["verdict"]["strict_order"]
    order_b = b["render"]["verdict"]["strict_order"]
    if set(order_a) != set(order_b):
        raise InputError("Les deux saisies ne declarent pas les memes partants actifs")
    rho = spearman_from_orders(order_a, order_b)
    horses_a = {h["no"]: h for h in a["render"]["horses"]}
    horses_b = {h["no"]: h for h in b["render"]["horses"]}
    rank_a = {n: i + 1 for i, n in enumerate(order_a)}
    rank_b = {n: i + 1 for i, n in enumerate(order_b)}
    movements = []
    for number in sorted(horses_a):
        movements.append({
            "no": number,
            "rank_delta": rank_a[number] - rank_b[number],
            "note_delta": round(horses_b[number]["note"] - horses_a[number]["note"], 2),
            "raw_score_delta": round(
                horses_b[number]["stable_score_raw"] - horses_a[number]["stable_score_raw"], 3
            ),
            "expected_rank_delta": round(
                horses_b[number]["expected_rank"] - horses_a[number]["expected_rank"], 3
            ),
        })
    movements.sort(key=lambda item: -abs(item["expected_rank_delta"]))
    input_noise = float(np.sqrt(np.mean([m["expected_rank_delta"] ** 2 for m in movements])))
    mc_noise = max(
        float(a["simulation"]["max_expected_rank_mc_stderr"]),
        float(b["simulation"]["max_expected_rank_mc_stderr"]),
    )
    ratio = input_noise / mc_noise if mc_noise > 0 else float("inf")
    if rho is None:
        verdict = "INDETERMINE"
    elif rho >= 0.95 and input_noise <= 0.30:
        verdict = "SAISIE_REPRODUCTIBLE"
    elif rho >= 0.85:
        verdict = "SAISIE_ACCEPTABLE"
    else:
        verdict = "SAISIE_INSTABLE"
    result = {
        "schema_version": RETEST_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "model_version": MODEL_VERSION,
        "race_key": first["race"]["race_key"],
        "order_first": order_a,
        "order_second": order_b,
        "order_spearman": None if rho is None else round(rho, 5),
        "top1_identical": order_a[0] == order_b[0],
        "top3_set_identical": set(order_a[:3]) == set(order_b[:3]),
        "top3_order_identical": order_a[:3] == order_b[:3],
        "rms_expected_rank_shift": round(input_noise, 4),
        "monte_carlo_stderr": round(mc_noise, 4),
        "input_noise_over_mc_noise": round(ratio, 1) if math.isfinite(ratio) else None,
        "verdict": verdict,
        "common_random_seed": shared_seed,
        "seed_policy": "common_random_numbers_same_model_version_race_and_as_of",
        "movements": movements,
        "reading": (
            "un ratio superieur a 1 signifie deja que la saisie bruite plus que la "
            "simulation. Et le ratio CROIT avec n_worlds, puisque augmenter les tirages "
            "reduit le denominateur sans toucher au numerateur: passe un certain point, "
            "simuler davantage ne fait que mesurer plus finement une saisie instable"
        ),
        "protocol": (
            "remplir deux fois le meme gel documentaire (meme as_of) a plusieurs "
            "jours d'intervalle, sans relire la premiere saisie; repeter sur dix "
            "courses avant de conclure"
        ),
    }
    result["retest_sha256"] = sha256_obj(result)
    return result


# ---------------------------------------------------------------------------
# 16. FIABILITE DES SCENARIOS
# ---------------------------------------------------------------------------

def scenario_reliability(audits: list[dict[str, Any]], bins: int = 5) -> dict[str, Any]:
    """Fiabilite des masses de scenarios contre des labels factuels independants.

    Le scenario realise doit etre code depuis le rythme, le terrain, la voie et
    les incidents observes, sans utiliser la ressemblance entre un ordre predit
    et l'arrivee. Le precedent proxy "scenario le mieux correle au resultat"
    etait circulaire et ne pouvait pas etablir une calibration.
    """
    if isinstance(bins, bool) or not isinstance(bins, int) or not 2 <= bins <= 20:
        raise InputError("bins doit etre un entier entre 2 et 20")
    observations: list[tuple[float, int]] = []
    race_scores: list[dict[str, float]] = []
    skipped = 0
    seen: set[str] = set()
    for audit in audits:
        verify_sealed_hash(audit, "audit_sha256", "AUDIT")
        if audit.get("schema_version") != AUDIT_SCHEMA:
            raise InputError(f"Audit au schema {AUDIT_SCHEMA} requis")
        race_key = str(audit.get("race_key"))
        if race_key in seen:
            raise InputError(f"Course dupliquee dans scenario-audit: {race_key}")
        seen.add(race_key)
        declared = audit.get("scenario_declared_probabilities")
        realized = audit.get("realized_scenario_id")
        if not declared or realized is None:
            skipped += 1
            continue
        winner_id = str(realized)
        probabilities = {str(key): float(value) for key, value in declared.items()}
        if winner_id not in probabilities:
            raise InputError(f"Label de scenario non declare: {race_key} / {winner_id}")
        if any(value < 0.0 or value > 1.0 for value in probabilities.values()):
            raise InputError(f"Probabilite de scenario invalide: {race_key}")
        if abs(sum(probabilities.values()) - 1.0) > 2e-5:
            raise InputError(f"Masses de scenarios non normalisees: {race_key}")
        brier = 0.0
        top = max(probabilities, key=probabilities.get)
        for scenario_id, probability in probabilities.items():
            outcome = 1 if scenario_id == winner_id else 0
            observations.append((probability, outcome))
            brier += (probability - outcome) ** 2
        race_scores.append({
            "brier": brier,
            "log_score": -math.log(max(probabilities[winner_id], 1e-12)),
            "primary_hit": 1.0 if top == winner_id else 0.0,
        })
    if not observations:
        raise InputError(f"Aucun audit exploitable ({skipped} ignores)")
    edges = np.linspace(0.0, 1.0, bins + 1)
    table = []
    total_gap = 0.0
    total_weight = 0
    for index in range(bins):
        low, high = float(edges[index]), float(edges[index + 1])
        selected = [item for item in observations if low <= item[0] < high or
                    (index == bins - 1 and item[0] == 1.0)]
        if not selected:
            continue
        declared_mean = float(np.mean([item[0] for item in selected]))
        realized = float(np.mean([item[1] for item in selected]))
        n_selected = len(selected)
        z = 1.959963984540054
        denom = 1.0 + z * z / n_selected
        centre = (realized + z * z / (2.0 * n_selected)) / denom
        half = z * math.sqrt(
            realized * (1.0 - realized) / n_selected
            + z * z / (4.0 * n_selected * n_selected)
        ) / denom
        table.append({
            "bin": f"[{low:.2f},{high:.2f})",
            "n": len(selected),
            "declared_mean": round(declared_mean, 4),
            "realized_rate": round(realized, 4),
            "gap": round(realized - declared_mean, 4),
            "realized_rate_ci95": [
                round(max(0.0, centre - half), 4),
                round(min(1.0, centre + half), 4),
            ],
        })
        total_gap += abs(realized - declared_mean) * len(selected)
        total_weight += len(selected)
    calibration_error = total_gap / total_weight if total_weight else float("nan")
    n_labeled = len(race_scores)
    if n_labeled < 50:
        verdict = "ECHANTILLON_INSUFFISANT"
    elif n_labeled < 200:
        verdict = "DIAGNOSTIC_SEULEMENT"
    elif calibration_error <= 0.07:
        verdict = "COMPATIBLE_AVEC_CALIBRATION_A_CONFIRMER_HORS_ECHANTILLON"
    else:
        verdict = "MASSES_DE_SCENARIOS_A_RECALIBRER"
    result = {
        "schema_version": SCENARIO_AUDIT_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "n_audits": len(audits),
        "n_labeled_races": n_labeled,
        "n_skipped": skipped,
        "n_observations": len(observations),
        "reliability_table": table,
        "mean_absolute_calibration_error": round(calibration_error, 5),
        "mean_multiclass_brier": round(
            float(np.mean([item["brier"] for item in race_scores])), 6
        ),
        "mean_scenario_log_score": round(
            float(np.mean([item["log_score"] for item in race_scores])), 6
        ),
        "primary_scenario_hit_rate": round(
            float(np.mean([item["primary_hit"] for item in race_scores])), 6
        ),
        "verdict": verdict,
        "label_contract": (
            "labels factuels independants de l'ordre d'arrivee; le proxy "
            "best_matching_scenario est explicitement exclu"
        ),
        "caveat": (
            "les observations d'une meme course sont correlees; les intervalles par "
            "case sont descriptifs et la promotion exige un lot temporel distinct"
        ),
    }
    result["scenario_audit_sha256"] = sha256_obj(result)
    return result


# ---------------------------------------------------------------------------
# 17. ABLATIONS AGREGEES ET COMPARAISON CHAMPION / CHALLENGER
# ---------------------------------------------------------------------------

def aggregate_ablations(ablations: list[dict[str, Any]]) -> dict[str, Any]:
    """Quels modules ne bougent JAMAIS rien, sur l'ensemble du lot ?

    Une ablation isolee ne prouve rien : un module peut etre inerte sur une
    course et decisif sur la suivante. Agregee sur des dizaines de courses, elle
    devient une decision de gouvernance. Un module systematiquement inerte doit
    etre supprime, ou son remplissage est trop timide pour justifier son plafond.

    Simplifier un systeme est presque toujours plus rentable que l'enrichir.
    """
    if not ablations:
        raise InputError("Au moins une ablation est requise")
    modules: dict[str, dict[str, list[float]]] = {}
    seen: set[str] = set()
    for ablation in ablations:
        if ablation.get("schema_version") != ABLATION_SCHEMA:
            raise InputError(f"Ablation au schema {ABLATION_SCHEMA} requise")
        verify_sealed_hash(ablation, "ablation_sha256", "ABLATION")
        race_key = str(ablation.get("race_key"))
        if race_key in seen:
            raise InputError(f"Course dupliquee dans ablate-batch: {race_key}")
        seen.add(race_key)
        for item in ablation.get("modules", []):
            if item.get("status") != "OK":
                continue
            bucket = modules.setdefault(item["module"], {"note": [], "moved": [], "rho": [], "top3": []})
            bucket["note"].append(float(item["max_abs_note_delta"]))
            bucket["moved"].append(float(item["positions_changed"]))
            bucket["top3"].append(1.0 if item["top3_changed"] else 0.0)
            if item.get("spearman_vs_baseline") is not None:
                bucket["rho"].append(float(item["spearman_vs_baseline"]))
    rows = []
    for name, bucket in modules.items():
        rows.append({
            "module": name,
            "n_races": len(bucket["note"]),
            "mean_max_note_delta": round(float(np.mean(bucket["note"])), 4),
            "p90_max_note_delta": round(float(np.quantile(bucket["note"], 0.90)), 4),
            "share_races_with_movement": round(float(np.mean([1.0 if v > 0 else 0.0
                                                              for v in bucket["moved"]])), 4),
            "share_races_top3_changed": round(float(np.mean(bucket["top3"])), 4),
            "mean_spearman_vs_baseline": (
                round(float(np.mean(bucket["rho"])), 5) if bucket["rho"] else None
            ),
        })
    rows.sort(key=lambda item: -item["mean_max_note_delta"])
    inert = [row["module"] for row in rows
             if row["share_races_with_movement"] == 0.0 and row["p90_max_note_delta"] < 0.05]
    marginal = [row["module"] for row in rows
                if row["module"] not in inert and row["share_races_with_movement"] < 0.10]
    result = {
        "schema_version": ABLATION_BATCH_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "n_ablations": len(ablations),
        "sample_status": (
            "SUFFISANT_POUR_TRIAGE" if len(ablations) >= 50 else
            "DIAGNOSTIC_SEULEMENT_MOINS_DE_50_COURSES"
        ),
        "modules": rows,
        "inert_modules": inert,
        "marginal_modules": marginal,
        "recommendation": (
            "supprimer les modules inertes ou reconnaitre que leur remplissage est trop "
            "timide pour justifier leur plafond; ne jamais retirer deux modules a la fois"
        ),
        "warning": (
            "l'ablation mesure la SENSIBILITE de la hierarchie, jamais la valeur "
            "predictive; un module inerte peut etre juste, un module sensible peut nuire"
        ),
    }
    result["ablation_batch_sha256"] = sha256_obj(result)
    return result


def challenge(
    champion: list[dict[str, Any]], challenger: list[dict[str, Any]],
    *, draws: int = 5000, seed: int = 20260903,
) -> dict[str, Any]:
    """Comparaison appariee champion / challenger sur EXACTEMENT les memes courses.

    Le seul chemin honnete pour qu'un systeme s'ameliore au lieu de deriver. Le
    bootstrap est apparie et fait PAR COURSE : les lignes d'une meme course ne
    sont pas independantes, et comparer deux lots differents ne prouverait rien.

    Un challenger ne doit modifier QU'UN SEUL module. Le moteur ne peut pas le
    verifier: c'est une discipline humaine, et c'est la seule qui rende les
    resultats interpretables.
    """
    def index(audits: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        table = {}
        for audit in audits:
            verify_sealed_hash(audit, "audit_sha256", "AUDIT")
            if audit.get("schema_version") != AUDIT_SCHEMA:
                raise InputError(f"Audit au schema {AUDIT_SCHEMA} requis")
            key = str(audit["race_key"])
            if key in table:
                raise InputError(f"Course dupliquee dans challenge: {key}")
            if audit.get("prior_commitment") is None:
                raise InputError(f"Course sans engagement pre-course: {key}")
            if audit.get("fill_mode", "FULL") != "FULL":
                raise InputError(f"Course LITE interdite dans challenge predictif: {key}")
            table[key] = audit
        return table

    left, right = index(champion), index(challenger)
    if set(left) != set(right):
        missing_left = sorted(set(right) - set(left))
        missing_right = sorted(set(left) - set(right))
        raise InputError(
            "Les lots champion/challenger doivent contenir exactement les memes "
            f"courses; absentes champion={missing_left[:5]}, challenger={missing_right[:5]}"
        )
    shared = sorted(
        left,
        key=lambda key: (left[key].get("scheduled_start_utc", ""), key),
    )
    if len(shared) < 100:
        raise InputError(
            f"Seulement {len(shared)} courses communes: 100 courses appariees requises"
        )
    differences = np.asarray([
        float(right[key]["winner_log_loss_internal"]) - float(left[key]["winner_log_loss_internal"])
        for key in shared
    ])
    clusters: dict[str, list[int]] = {}
    for index_value, key in enumerate(shared):
        timestamp = left[key].get("scheduled_start_utc")
        if not isinstance(timestamp, str):
            raise InputError(f"scheduled_start_utc absent du lot champion: {key}")
        day = parse_iso(timestamp, f"audit[{key}].scheduled_start_utc").date().isoformat()
        clusters.setdefault(day, []).append(index_value)
    days = sorted(clusters)
    if len(days) < 20:
        raise InputError(
            f"Seulement {len(days)} journees independantes: 20 requises pour le bootstrap"
        )
    rng = np.random.default_rng(seed)
    samples = np.empty(draws, dtype=float)
    for draw in range(draws):
        chosen_days = rng.choice(days, size=len(days), replace=True)
        indices = [index_value for day in chosen_days for index_value in clusters[str(day)]]
        samples[draw] = float(np.mean(differences[indices]))
    low, high = float(np.quantile(samples, 0.025)), float(np.quantile(samples, 0.975))
    mean = float(differences.mean())
    if high < 0.0:
        verdict = "CHALLENGER_MEILLEUR"
    elif low > 0.0:
        verdict = "CHAMPION_MEILLEUR"
    else:
        verdict = "INDISCERNABLE"
    result = {
        "schema_version": CHALLENGE_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "n_paired_races": len(shared),
        "n_day_clusters": len(days),
        "bootstrap_unit": "journee_de_course",
        "mean_log_loss_champion": round(float(np.mean(
            [float(left[key]["winner_log_loss_internal"]) for key in shared])), 6),
        "mean_log_loss_challenger": round(float(np.mean(
            [float(right[key]["winner_log_loss_internal"]) for key in shared])), 6),
        "mean_difference": round(mean, 6),
        "ci95_low": round(low, 6),
        "ci95_high": round(high, 6),
        "verdict": verdict,
        "promotion_allowed": verdict == "CHALLENGER_MEILLEUR",
        "governance": (
            "ne promouvoir que sur un lot temporellement posterieur au lot de mise au "
            "point, seulement si le challenger ne modifie qu'un module, et enregistrer "
            "chaque challenge tente pour eviter le p-hacking par essais repetes"
        ),
    }
    result["challenge_sha256"] = sha256_obj(result)
    return result


# ---------------------------------------------------------------------------
# 11. ENTREES / SORTIES ET TEMPLATES
# ---------------------------------------------------------------------------

def write_json(path: str | os.PathLike[str], value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def save_samples(path: str | os.PathLike[str], samples: dict[str, np.ndarray]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(target, **samples)


def empty_runner(no: int) -> dict[str, Any]:
    return {
        "no": no,
        "name": f"A_REMPLACER_{no}",
        "active": True,
        "identity_evidence_ids": [f"ID-{no}"],
        "score_components": {axis: {"value": 0.0, "evidence_ids": []} for axis in LEVEL_CAPS},
        "evidence_confidence": 0.50,
        "aleatory_sd": 1.00,
        "epistemic_sd": 0.80,
        "factor_loadings": {},
        "factor_evidence": {},
        # VOIE DE TRANSCRIPTION ECRITE (v11.7). A utiliser quand aucun sectionnel
        # ni replay n'est accessible: les comptes-rendus ECRITS des bulletins
        # officiels donnent le role de rythme reellement observe et les incidents
        # de parcours. Mettre a null si aucun compte-rendu n'a ete lu.
        #
        # ATTENTION: declarer un incident ELARGIT l'incertitude et PLAFONNE la
        # forme recente. Un cheval gene n'est pas un cheval sous-estime, c'est un
        # cheval sur lequel la derniere sortie ne dit rien.
        "race_reading": None,
        "_race_reading_exemple": {
            "readings": [{
                "evidence_id": f"BULL-{no}",
                "source_class": "OFFICIEL | PRESSE | SECONDAIRE",
                "observed_at": "YYYY-MM-DDTHH:MM:SS+02:00",
                "observed_role": "leader | prominent | midfield | closer | unknown",
                "trouble": ["enferme", "manque_de_place"],
            }],
        },
    }


def input_template() -> dict[str, Any]:
    numbers = [1, 2, 3]
    empty_adjustments = {str(number): {} for number in numbers}

    def scenario(sid: str, probability: float, residual: bool, noise: float) -> dict[str, Any]:
        return {
            "id": sid,
            "probability": probability,
            "mechanism": "A_REMPLACER" if not residual else "incident, tactique ou evolution non modelisee",
            "probability_basis": "A_REMPLACER" if not residual else "masse residuelle explicite",
            "failure_mode": "A_REMPLACER" if not residual else "branche volontairement peu descriptive",
            "is_residual": residual,
            "evidence_ids": [],
            "runner_adjustments": json.loads(json.dumps(empty_adjustments)),
            "factor_means": {},
            "noise_multiplier": noise,
        }

    return {
        "schema_version": INPUT_SCHEMA,
        "template_only": True,
        "fill_mode": "FULL",
        "race": {
            "race_key": "YYYY-MM-DD|HIPPODROME|R1C1",
            "course_name": "A_REMPLACER",
            "course": "HIPPODROME",
            "race_type": "handicap|conditions|maiden|reclamer|listed|groupe",
            "surface": "gazon|PSF",
            "distance_m": 1600,
            "scheduled_start": "YYYY-MM-DDTHH:MM:SS+02:00",
            "as_of": "YYYY-MM-DDTHH:MM:SS+02:00",
            "field_declared_final": False,
            "data_grade": "C",
            "official_source_url": "https://www.france-galop.com/",
        },
        "simulation": {
            "n_worlds": 2000,
            "races_per_world": 100,
            "student_df": 5.0,
            "scenario_confidence": "LOW|MEDIUM|HIGH|VERY_HIGH",
            "calibration_status": "UNVALIDATED",
            "calibration_sha256": None,
        },
        "uncertainty": {"race_missingness": 0.50, "critical_unknown": False},
        # PARE-FEU DE COLLECTE (obligatoire en FULL). Declarer ICI toute page
        # ouverte avant as_of, y compris celles qui n'ont produit aucune preuve.
        # Une page portant des cotes fait echouer le scellement: c'est voulu.
        "collection": {
            "collector": "A_REMPLACER",
            "attestation": (
                "aucune page portant des cotes, rapports ou pronostics n'a ete "
                "ouverte avant as_of pour cette course"
            ),
            "sources": [
                {
                    "url": "https://www.france-galop.com/",
                    "fetched_at": "YYYY-MM-DDTHH:MM:SS+02:00",
                    "price_bearing": False,
                    "purpose": "carte officielle des partants",
                }
            ],
        },
        "evidence_registry": {            "ID-1": {
                "tier": "F-P1",
                "family_id": "IDENTITY-1",
                "summary": "Identite officielle a remplacer",
                "source_url": "https://www.france-galop.com/",
                "observed_at": "YYYY-MM-DDTHH:MM:SS+02:00",
                "available_at": "YYYY-MM-DDTHH:MM:SS+02:00",
                "parent_ids": [],
            }
        },
        "runners": [empty_runner(number) for number in numbers],
        "scenarios": [
            scenario("S1_PRIMAIRE", 0.40, False, 1.0),
            scenario("S2_ALTERNATIF", 0.35, False, 1.0),
            scenario("S3_CONTRARIAN", 0.20, False, 1.0),
            scenario("AUTRE_COURSE", 0.05, True, 1.30),
        ],
        "market": None,
        "unknowns": [],
    }


def result_template() -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA,
        "template_only": True,
        "race_key": "A_REMPLACER",
        "status": "official_final",
        "verified": True,
        "source_url": "https://www.france-galop.com/",
        "verified_at": "YYYY-MM-DDTHH:MM:SS+02:00",
        "dead_heat": False,
        "order": [],
        "realized_scenario_id": None,
        "scenario_label_source_url": None,
        "scenario_label_basis": None,
        "scenario_label_rule": (
            "label factuel fonde sur rythme/terrain/voie/incidents, etabli sans utiliser "
            "la ressemblance entre l'ordre predit et l'arrivee"
        ),
    }


def apply_scratch(
    raw: dict[str, Any], removed: list[int], new_as_of: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconstruit un dossier apres non-partant tardif.

    Un retrait apres le scellement invalide l'analyse : le peloton sur lequel la
    hierarchie a ete calculee n'existe plus, les interactions de rythme changent,
    et en parimutuel les rapports sont recalcules. Bricoler mentalement le
    resultat precedent est une faute de protocole.

    La bonne procedure est mecanique : desactiver le partant, retirer sa ligne de
    TOUS les scenarios, avancer as_of au moment ou le retrait a ete connu,
    relancer l'analyse et emettre un NOUVEL engagement chaine au precedent.
    L'ancien engagement reste dans le journal : il documente ce qui etait prevu
    avant l'evenement, ce qui est exactement son role.
    """
    if not removed:
        raise InputError("Aucun numero a retirer")
    updated = json.loads(json.dumps(raw))
    raw_epistemic = {
        int(r["no"]): float(r["epistemic_sd"])
        for r in raw["runners"]
        if r.get("active") is True and r.get("epistemic_sd") is not None
    }
    active_before = {int(r["no"]) for r in updated["runners"] if r.get("active") is True}
    unknown = sorted(set(removed) - active_before)
    if unknown:
        raise InputError(f"Numeros absents des partants actifs: {unknown}")
    remaining = sorted(active_before - set(removed))
    if len(remaining) < 3:
        raise InputError("Moins de trois partants actifs apres retrait: analyse impossible")
    for runner in updated["runners"]:
        if int(runner["no"]) in set(removed):
            runner["active"] = False
            runner["scratched"] = True
    for scenario in updated["scenarios"]:
        for number in removed:
            scenario["runner_adjustments"].pop(str(number), None)
    market = updated.get("market")
    if isinstance(market, dict):
        # Les cotes du retire doivent disparaitre: en parimutuel le retrait
        # provoque un recalcul complet des rapports, l'ancien prix n'existe plus.
        for source in market.get("sources", []):
            for number in removed:
                source.get("odds", {}).pop(str(number), None)
    updated["race"]["as_of"] = new_as_of
    updated["race"]["field_declared_final"] = True
    # v11.8.2. L'echelle de classe est RELATIVE au peloton: retirer un partant
    # deplace la mediane, donc TOUS les ability_class calcules changent. Sans ce
    # recalcul, la reconstruction apres non-partant echouait systematiquement sur
    # la clause anti-ecrasement - c'est-a-dire que l'etape T-60 du protocole
    # devenait impossible des qu'un dossier portait un historique chiffre.
    survivors = {
        int(runner["no"]): runner for runner in updated["runners"]
        if runner.get("active") is True
    }
    if any(runner.get("form_lines") for runner in survivors.values()):
        as_of_dt = parse_iso(new_as_of, "new_as_of")
        registry = updated.get("evidence_registry", {})
        recomputed = build_class_ladder(
            {
                number: validate_form_lines(runner, registry, as_of_dt, f"scratch[{number}]")
                for number, runner in survivors.items()
            },
            updated["race"].get("allocation_eur"),
        )
        for number, runner in survivors.items():
            if runner.get("form_lines"):
                runner["score_components"]["ability_class"]["value"] = (
                    recomputed["ability_by_number"].get(number, 0.0)
                )
    # v11.10. DEUXIEME MOITIE DU BUG DE NON-PARTANT, restee ouverte depuis la
    # v11.8.2. Celle-ci avait corrige l'echelle de classe, qui est CALCULEE:
    # retirer un partant deplace la mediane du peloton, donc tous les
    # ability_class, et le moteur savait les recalculer.
    #
    # Le plancher d'incertitude a exactement la meme relativite, mais il porte
    # sur epistemic_sd, qui est DECLARE. Retirer un partant peu incertain fait
    # monter la mediane, donc le plancher, donc un dossier valide AVANT le
    # retrait devient invalide APRES - alors que rien concernant le cheval gene
    # n'a change. L'operateur se retrouvait alors, a T-60 et sous contrainte de
    # temps, force d'inventer une valeur d'incertitude pour franchir une porte:
    # c'est-a-dire exactement le jugement discretionnaire que toute
    # l'architecture existe pour empecher.
    #
    # On porte donc la contrainte, dans le SEUL sens conservateur: on ELARGIT
    # jusqu'au plancher, jamais on ne resserre. Un retrait detruit de
    # l'information sur le peloton; il ne peut pas en creer. Toute correction
    # est journalisee dans la note - une modification silencieuse d'une valeur
    # declaree serait pire que le refus qu'elle remplace.
    widened: list[dict[str, Any]] = []
    if survivors:
        as_of_dt = parse_iso(new_as_of, "new_as_of")
        registry = updated.get("evidence_registry", {})
        surviving_readings = {
            number: validate_race_reading(runner, registry, as_of_dt, f"scratch[{number}]")
            for number, runner in survivors.items()
        }
        surviving_forms = {
            number: validate_form_lines(runner, registry, as_of_dt, f"scratch[{number}]")
            for number, runner in survivors.items()
        }
        # Elargir un cheval deplace la mediane, donc les planchers des autres.
        # On itere jusqu'au point fixe. Si la boucle ne converge pas - cas ou la
        # MAJORITE du peloton devrait etre plus incertaine que sa propre mediane,
        # ce qui est incoherent - on s'arrete et validate refusera en clair.
        for _ in range(8):
            current_eps = {
                number: float(runner["epistemic_sd"])
                for number, runner in survivors.items()
            }
            floors = epistemic_floors(current_eps, surviving_readings, surviving_forms)
            changed = False
            for number, detail in floors["by_number"].items():
                target = min(1.80, float(detail["floor"]))
                if current_eps[number] + 1e-12 < target:
                    survivors[number]["epistemic_sd"] = round(target, 4)
                    changed = True
            if not changed:
                break
        for number, runner in survivors.items():
            before = float(raw_epistemic.get(number, runner["epistemic_sd"]))
            after = float(runner["epistemic_sd"])
            if after > before + 1e-9:
                widened.append({
                    "no": number,
                    "epistemic_sd_before": round(before, 4),
                    "epistemic_sd_after": round(after, 4),
                    "reason": floors["by_number"].get(number, {}).get("causes", []),
                })
    note = {
        "removed": sorted(removed),
        "remaining_active": remaining,
        "new_as_of": new_as_of,
        "epistemic_widened_after_scratch": widened,
        "epistemic_widening_note": (
            "le plancher d'incertitude est relatif au peloton: retirer un partant le "
            "deplace. Les valeurs ci-dessus ont ete ELARGIES pour rester au-dessus du "
            "nouveau plancher, jamais resserrees. Un retrait detruit de l'information, "
            "il n'en cree pas."
        ),
        "required_next_steps": [
            "relancer validate puis analyze sur le dossier reduit",
            "emettre un NOUVEL engagement commit chaine au precedent",
            "ne jamais reutiliser le rapport ni le ticket calcules avant le retrait",
        ],
        "warning": (
            "les preuves du registre restent inchangees; verifiez a la main que les "
            "ajustements de rythme des partants restants tiennent encore compte du "
            "peloton reel, notamment le compte de pression"
        ),
    }
    return updated, note


# ---------------------------------------------------------------------------
# 12. AUTOTESTS
# ---------------------------------------------------------------------------

def synthetic_input() -> tuple[dict[str, Any], datetime]:
    current = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)
    stamp = "2026-08-21T09:50:00+02:00"
    registry: dict[str, Any] = {}
    runners: list[dict[str, Any]] = []
    levels = np.linspace(0.80, -0.80, 7)
    for number, level in enumerate(levels, start=1):
        registry[f"ID-{number}"] = {
            "tier": "F-P1", "family_id": f"IDENTITY-{number}",
            "summary": f"Identite officielle synthetique {number}",
            "source_url": "https://www.france-galop.com/",
            "observed_at": stamp, "available_at": stamp, "parent_ids": [],
        }
        registry[f"PERF-{number}"] = {
            "tier": "F-S1", "family_id": f"PERFORMANCE-{number}",
            "summary": f"Performance synthetique {number}",
            "source_url": "https://example.org/performance",
            "observed_at": stamp, "available_at": stamp, "parent_ids": [],
        }
        registry[f"FORM-{number}"] = {
            "tier": "I", "family_id": f"FORM-{number}",
            "summary": f"Forme derivee {number}",
            "observed_at": stamp, "available_at": stamp, "parent_ids": [f"PERF-{number}"],
        }
        registry[f"PACE-{number}"] = {
            "tier": "I", "family_id": f"PACE-{number}",
            "summary": f"Profil de rythme derive {number}",
            "observed_at": stamp, "available_at": stamp, "parent_ids": [f"PERF-{number}"],
        }
        ability = float(np.clip(level, -0.70, 0.70))
        form = float(level - ability)
        components = {axis: {"value": 0.0, "evidence_ids": []} for axis in LEVEL_CAPS}
        components["ability_class"] = {"value": ability, "evidence_ids": [f"PERF-{number}"]}
        components["recent_form"] = {"value": form, "evidence_ids": [f"FORM-{number}"] if form else []}
        runners.append(
            {
                "no": number, "name": f"SYNTHETIQUE_{number}", "active": True,
                "identity_evidence_ids": [f"ID-{number}"],
                "score_components": components,
                "evidence_confidence": 0.75 if number != 4 else 0.35,
                "aleatory_sd": 0.90 + 0.04 * number,
                "epistemic_sd": 0.35,
                "factor_loadings": {}, "factor_evidence": {},
            }
        )
    registry["SC-SLOW"] = {
        "tier": "H", "family_id": "SCENARIO-SLOW", "summary": "Rythme lent plausible",
        "observed_at": stamp, "available_at": stamp, "parent_ids": ["PACE-1"],
    }
    registry["SC-FAST"] = {
        "tier": "H", "family_id": "SCENARIO-FAST", "summary": "Rythme soutenu plausible",
        "observed_at": stamp, "available_at": stamp, "parent_ids": ["PACE-2"],
    }

    def empty_adjustments() -> dict[str, Any]:
        return {str(number): {} for number in range(1, 8)}

    slow = empty_adjustments()
    slow["1"] = {"pace": {"value": -0.25, "evidence_ids": ["PACE-1"]}}
    slow["7"] = {"pace": {"value": 0.35, "evidence_ids": ["PACE-7"]}}
    fast = empty_adjustments()
    fast["1"] = {"pace": {"value": 0.35, "evidence_ids": ["PACE-1"]}}
    fast["7"] = {"pace": {"value": -0.25, "evidence_ids": ["PACE-7"]}}
    raw = {
        "schema_version": INPUT_SCHEMA,
        "template_only": False,
        "race": {
            "race_key": "2026-08-21|SYNTHETIQUE|R0C0",
            "course_name": "DEMO SYNTHETIQUE",
            "course": "SYNTHETIQUE",
            "race_type": "handicap",
            "surface": "gazon",
            "distance_m": 1600,
            "scheduled_start": "2026-08-21T15:00:00+02:00",
            "as_of": stamp,
            "field_declared_final": False,
            "data_grade": "B",
            "official_source_url": "https://www.france-galop.com/",
        },
        "simulation": {
            "n_worlds": 300,
            "races_per_world": 100,
            "student_df": 5.0,
            "scenario_confidence": "MEDIUM",
            "calibration_status": "UNVALIDATED",
        },
        "uncertainty": {"race_missingness": 0.20, "critical_unknown": False},
        "collection": {
            "collector": "SELF_TEST",
            "attestation": "aucune page porteuse de prix ouverte avant as_of",
            "sources": [
                {"url": "https://www.france-galop.com/", "fetched_at": stamp,
                 "price_bearing": False, "purpose": "carte officielle"},
                {"url": "https://example.org/performance", "fetched_at": stamp,
                 "price_bearing": False, "purpose": "historique de performances"},
                {"url": "https://example.org/suitability", "fetched_at": stamp,
                 "price_bearing": False, "purpose": "aptitude, variante d'ablation"},
            ],
        },
        "evidence_registry": registry,
        "runners": runners,
        "scenarios": [
            {
                "id": "LENT", "probability": 0.30,
                "mechanism": "compression et acceleration tardive",
                "probability_basis": "un animateur credible",
                "failure_mode": "un jockey force le train",
                "is_residual": False, "evidence_ids": ["SC-SLOW"],
                "runner_adjustments": slow, "factor_means": {}, "noise_multiplier": 1.0,
            },
            {
                "id": "REGULIER", "probability": 0.40,
                "mechanism": "train regulier sans duel durable",
                "probability_basis": "equilibre des profils",
                "failure_mode": "depart dispute",
                "is_residual": False, "evidence_ids": ["SC-SLOW", "SC-FAST"],
                "runner_adjustments": empty_adjustments(), "factor_means": {}, "noise_multiplier": 1.0,
            },
            {
                "id": "SOUTENU", "probability": 0.25,
                "mechanism": "pression precoce et course d'usure",
                "probability_basis": "plusieurs prises de position possibles",
                "failure_mode": "un animateur renonce",
                "is_residual": False, "evidence_ids": ["SC-FAST"],
                "runner_adjustments": fast, "factor_means": {}, "noise_multiplier": 1.0,
            },
            {
                "id": "AUTRE_COURSE", "probability": 0.05,
                "mechanism": "incident ou tactique non modelisee",
                "probability_basis": "masse residuelle",
                "failure_mode": "branche volontairement generique",
                "is_residual": True, "evidence_ids": [],
                "runner_adjustments": empty_adjustments(), "factor_means": {}, "noise_multiplier": 1.30,
            },
        ],
        "market": None,
        "unknowns": [],
    }
    return raw, current


def _add_uniform_offset(raw: dict[str, Any], offset: float) -> dict[str, Any]:
    """Ajoute une constante identique a TOUS les scores bruts, ordre brut inchange."""
    shifted = json.loads(json.dumps(raw))
    stamp = shifted["race"]["as_of"]
    for runner in shifted["runners"]:
        number = runner["no"]
        eid = f"SUIT-{number}"
        shifted["evidence_registry"][eid] = {
            "tier": "F-S1", "family_id": f"SUITABILITY-{number}",
            "summary": f"Aptitude synthetique {number}",
            "source_url": "https://example.org/suitability",
            "observed_at": stamp, "available_at": stamp, "parent_ids": [],
        }
        runner["score_components"]["stable_suitability"] = {"value": offset, "evidence_ids": [eid]}
    return shifted


def self_test() -> None:
    raw, current = synthetic_input()
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}

    report_a, samples_a = build_report(raw, current=current, allow_test_samples=True)
    report_b, samples_b = build_report(raw, current=current, allow_test_samples=True)
    assert report_a["invariant_check"]["ok"]
    assert report_a["render_purity_check"]["ok"]
    assert np.array_equal(samples_a["ranks"], samples_b["ranks"])
    assert report_a["sport_core_sha256"] == report_b["sport_core_sha256"]
    checks["determinism_and_invariants"] = True
    expected_seed = deterministic_seed(
        raw["race"]["race_key"], raw["race"]["as_of"],
        sha256_obj(sport_payload(raw)),
    )
    assert report_a["simulation"]["seed"] == expected_seed
    assert report_a["simulation"]["model_version"] == MODEL_VERSION
    assert MODEL_VERSION != ENGINE_VERSION
    checks["model_seed_decoupled_from_engine_release"] = True

    assert len(report_a["render"]["scenarios"]) == 4
    assert all(scenario["order"] for scenario in report_a["render"]["scenarios"])
    assert len(report_a["render"]["verdict"]["strict_order"]) == 7
    assert report_a["render"]["verdict"]["synthesis_mode"] in {"MIXTURE", "DOMINANT_SCENARIO"}
    checks["scenario_orders_and_bma"] = True

    assert abs(note_from_expected_rank(4.0, 7) - 10.0) < 1e-12
    checks["note_anchor"] = True

    # --- NOUVEAU 11.1 : invariance par translation du peloton -----------------
    # C'est le test qui manquait en 11.0 et qui laissait passer un shrinkage
    # vers zero penalisant les chevaux peu documentes.
    shifted = _add_uniform_offset(raw, 0.55)
    report_shift, _ = build_report(shifted, current=current, allow_test_samples=True)
    base_central = {h["no"]: h["stable_score_shrunk_centered"] for h in report_a["render"]["horses"]}
    shift_central = {h["no"]: h["stable_score_shrunk_centered"] for h in report_shift["render"]["horses"]}
    max_gap = max(abs(base_central[k] - shift_central[k]) for k in base_central)
    assert max_gap < 1e-9, f"shrinkage non invariant par translation (ecart {max_gap})"
    assert report_shift["render"]["verdict"]["strict_order"] == report_a["render"]["verdict"]["strict_order"]
    checks["translation_invariant_shrinkage"] = True
    details["max_central_gap_after_uniform_shift"] = max_gap

    # Un cheval peu documente ne doit pas etre pousse sous un cheval de score
    # brut inferieur par le seul effet de sa confiance.
    positive = _add_uniform_offset(raw, 0.55)
    raw_scores = {h["no"]: h["stable_score_raw"] for h in report_shift["render"]["horses"]}
    centered = {h["no"]: h["stable_score_shrunk_centered"] for h in report_shift["render"]["horses"]}
    for a in raw_scores:
        for b in raw_scores:
            if raw_scores[a] > raw_scores[b] + 1e-9:
                assert centered[a] > centered[b] - 1e-9, f"inversion niveau {a}/{b}"
    checks["no_penalty_for_missing_evidence"] = True

    # --- NOUVEAU 11.1 : separation reelle des incertitudes -------------------
    split = report_a["render"]["uncertainty_split"]
    assert 0.0 <= split["mean_epistemic_share"] <= 1.0
    high_epistemic = json.loads(json.dumps(raw))
    for runner in high_epistemic["runners"]:
        runner["epistemic_sd"] = 1.60
        runner["aleatory_sd"] = 0.50
    low_epistemic = json.loads(json.dumps(raw))
    for runner in low_epistemic["runners"]:
        runner["epistemic_sd"] = 0.10
        runner["aleatory_sd"] = 2.40
    high_report, _ = build_report(high_epistemic, current=current, allow_test_samples=True)
    low_report, _ = build_report(low_epistemic, current=current, allow_test_samples=True)
    high_share = high_report["render"]["uncertainty_split"]["mean_epistemic_share"]
    low_share = low_report["render"]["uncertainty_split"]["mean_epistemic_share"]
    assert high_share > low_share + 0.20, f"decomposition inoperante ({high_share} vs {low_share})"
    checks["epistemic_aleatory_separation"] = True
    details["epistemic_share_high_vs_low"] = [high_share, low_share]

    # --- NOUVEAU 11.1 : de-vig de Shin ---------------------------------------
    fair = {1: 2.0, 2: 4.0, 3: 4.0}
    probabilities, z = shin_probabilities(fair)
    assert abs(sum(probabilities.values()) - 1.0) < 1e-9 and abs(z) < 1e-6
    juiced = {1: 1.80, 2: 3.40, 3: 3.40}
    probabilities, z = shin_probabilities(juiced)
    assert abs(sum(probabilities.values()) - 1.0) < 1e-9 and z > 0.0
    spread = {1: 1.30, 2: 6.0, 3: 15.0, 4: 40.0}
    shin_spread, z_spread = shin_probabilities(spread)
    naive_spread = {k: (1.0 / v) / sum(1.0 / x for x in spread.values()) for k, v in spread.items()}
    # Shin corrige le biais favori-outsider : il RELEVE le favori et ABAISSE les
    # outsiders par rapport a la normalisation proportionnelle.
    assert shin_spread[1] > naive_spread[1], "Shin doit relever le favori"
    assert shin_spread[4] < naive_spread[4], "Shin doit abaisser l'outsider extreme"
    assert abs(sum(shin_spread.values()) - 1.0) < 1e-9
    checks["shin_devig"] = True
    details["shin_z_on_juiced_book"] = round(z, 5)

    # --- NOUVEAU 11.1 : paliers fondes sur la dominance ----------------------
    boundaries = report_a["render"]["verdict"]["tier_boundaries"]
    assert boundaries and all("dominance_probability" in b for b in boundaries)
    for boundary in boundaries:
        if boundary["separated"]:
            assert boundary["dominance_probability"] >= TIER_DOMINANCE_THRESHOLD
    checks["tiers_require_pairwise_dominance"] = True

    # --- NOUVEAU 11.1 : scellement public ------------------------------------
    before_start = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    entry_1 = build_commit(report_a, current=before_start)
    assert entry_1["prev_entry_sha256"] is None and entry_1["minutes_before_start"] > 0
    assert "strict_order" not in json.dumps(entry_1)
    after_start = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)
    try:
        build_commit(report_a, current=after_start)
    except InputError:
        checks["commit_refused_after_start"] = True
    else:
        raise AssertionError("Un engagement post-depart aurait du etre refuse")
    second = json.loads(json.dumps(report_a))
    second["race_key"] = "2026-08-21|SYNTHETIQUE|R0C1"
    second.pop("report_sha256")
    second["report_sha256"] = sha256_obj(second)
    entry_2 = build_commit(
        second, previous_entry_sha256=entry_1["entry_sha256"],
        current=datetime(2026, 8, 21, 12, 1, tzinfo=timezone.utc),
    )
    chain = verify_journal([entry_1, entry_2])
    assert chain["chain_ok"], chain["problems"]
    # Une meme course re-engagee apres non-partant doit passer, mais seulement en
    # superseding explicite et motive.
    revised_raw = json.loads(json.dumps(raw))
    revised_raw["race"]["as_of"] = "2026-08-21T10:00:00+02:00"
    revised_report, _ = build_report(
        revised_raw, current=current, allow_test_samples=True
    )
    revised = build_commit(
        revised_report, previous_entry_sha256=entry_2["entry_sha256"],
        supersedes=entry_1, revision_reason="NON_PARTANT",
        current=datetime(2026, 8, 21, 12, 2, tzinfo=timezone.utc),
    )
    assert revised["revision"] == 1 and revised["supersedes_entry_sha256"] == entry_1["entry_sha256"]
    revised_chain = verify_journal([entry_1, entry_2, revised])
    assert revised_chain["chain_ok"], revised_chain["problems"]
    assert revised_chain["n_races"] == 2 and revised_chain["n_revised_races"] == 1
    try:
        build_commit(
            revised_report, supersedes=entry_1,
            current=datetime(2026, 8, 21, 12, 2, tzinfo=timezone.utc),
        )
    except InputError:
        checks["revision_requires_declared_reason"] = True
    else:
        raise AssertionError("Une revision sans motif aurait du etre refusee")
    naked = build_commit(report_a, previous_entry_sha256=entry_2["entry_sha256"],
                         current=datetime(2026, 8, 21, 12, 3, tzinfo=timezone.utc))
    assert not verify_journal([entry_1, entry_2, naked])["chain_ok"]
    checks["silent_republication_blocked"] = True
    tampered = json.loads(json.dumps(entry_2))
    tampered["sport_core_sha256"] = "0" * 64
    assert not verify_journal([entry_1, tampered])["chain_ok"]
    checks["public_commitment_chain"] = True

    # --- NOUVEAU 11.1 : ablations --------------------------------------------
    ablation = run_ablation(raw, current=current, worlds=200, races=100)
    assert ablation["modules"] and all(m["status"] == "OK" for m in ablation["modules"])
    pace = next(m for m in ablation["modules"] if m["module"] == "CANAL_pace")
    other = next(m for m in ablation["modules"] if m["module"] == "AXE_other")
    assert other["positions_changed"] == 0 and other["max_abs_note_delta"] < 0.05
    assert pace["max_abs_note_delta"] > other["max_abs_note_delta"]
    checks["ablation_detects_active_modules"] = True
    details["ablation_most_influential"] = ablation["most_influential"]

    # --- reproductibilite as_of et separation marche -------------------------
    report_late, _ = build_report(raw, current=after_start, allow_test_samples=True)
    assert report_late["clock"]["execution_state"] == "CLOSED"
    assert report_late["sport_core_sha256"] == report_a["sport_core_sha256"]
    checks["as_of_reproducibility"] = True

    contaminated_market = json.loads(json.dumps(raw))
    contaminated_market["market"] = {
        "sources": [{
            "operator": "DEMO", "market_type": "fixed_odds",
            "source_url": "https://example.org/odds", "observed_at": raw["race"]["as_of"],
            "odds": {str(number): float(2.2 + number * 1.7) for number in range(1, 8)},
        }]
    }
    try:
        validate_input(contaminated_market, current=current, allow_test_samples=True)
    except InputError:
        pass
    else:
        raise AssertionError("Le marche aurait du etre refuse dans INPUT sportif")
    assert report_a["market_diagnostic"] is None
    checks["market_separation"] = True

    # --- garde-fous herites de 11.0 ------------------------------------------
    contaminated = json.loads(json.dumps(raw))
    contaminated["scenarios"][0]["runner_adjustments"]["1"]["pace"]["evidence_ids"] = ["PERF-1"]
    try:
        validate_input(contaminated, current=current, allow_test_samples=True)
    except InputError:
        checks["family_double_count_blocked"] = True
    else:
        raise AssertionError("Le double comptage par famille aurait du etre bloque")

    broken = json.loads(json.dumps(raw))
    broken["scenarios"][0]["probability"] = 0.40
    try:
        validate_input(broken, current=current, allow_test_samples=True)
    except InputError:
        checks["scenario_mass_blocked"] = True
    else:
        raise AssertionError("Une masse de scenarios !=1 aurait du etre bloquee")

    leaked = json.loads(json.dumps(raw))
    leaked["evidence_registry"]["PERF-1"]["available_at"] = "2026-08-21T10:30:00+02:00"
    try:
        validate_input(leaked, current=current, allow_test_samples=True)
    except InputError:
        checks["temporal_leak_blocked"] = True
    else:
        raise AssertionError("Une fuite temporelle aurait du etre bloquee")

    seeded = json.loads(json.dumps(raw))
    seeded["simulation"]["seed"] = 7
    try:
        validate_input(seeded, current=current, allow_test_samples=True)
    except InputError:
        checks["seed_shopping_blocked"] = True
    else:
        raise AssertionError("Une graine manuelle aurait du etre bloquee")

    legacy = json.loads(json.dumps(raw))
    legacy["simulation"].pop("n_worlds")
    legacy["simulation"]["n_samples"] = 300_000
    try:
        validate_input(legacy, current=current, allow_test_samples=True)
    except InputError:
        checks["legacy_n_samples_rejected"] = True
    else:
        raise AssertionError("Un dossier 11.0 aurait du etre rejete explicitement")

    # --- Clarte structurelle : nettete interne, pas confiance predictive -------
    clarity = report_a["render"]["verdict"]["structural_clarity"]
    assert clarity["level"] in CLARITY_REGISTERS
    assert clarity["ready_assertions"] and clarity["banned_phrases"]
    assert clarity["leader"] == report_a["render"]["verdict"]["strict_order"][0]
    # Une course nette doit noter plus haut qu'une course brouillee.
    blurred = json.loads(json.dumps(raw))
    for runner in blurred["runners"]:
        for axis in runner["score_components"]:
            runner["score_components"][axis]["value"] = 0.0
            runner["score_components"][axis]["evidence_ids"] = []
        runner["aleatory_sd"] = 2.40
    blurred["uncertainty"]["critical_unknown"] = True
    blurred_report, _ = build_report(blurred, current=current, allow_test_samples=True)
    blurred_clarity = blurred_report["render"]["verdict"]["structural_clarity"]
    assert blurred_clarity["score"] < clarity["score"], (
        f"clarte insensible a la nettete: {blurred_clarity['score']} vs {clarity['score']}"
    )
    assert blurred_clarity["level"] == "NULLE"
    assert "AFFIRMATIF" in blurred_clarity["language_register"]
    checks["structural_clarity_tracks_separation"] = True
    checks["structural_clarity_never_claims_accuracy"] = True

    # --- NOUVEAU 11.2 : non-partant tardif -----------------------------------
    reduced, note = apply_scratch(raw, [4], "2026-08-21T09:55:00+02:00")
    assert note["remaining_active"] == [1, 2, 3, 5, 6, 7]
    for scenario in reduced["scenarios"]:
        assert "4" not in scenario["runner_adjustments"]
    reduced_report, _ = build_report(reduced, current=current, allow_test_samples=True)
    assert 4 not in reduced_report["render"]["verdict"]["strict_order"]
    assert len(reduced_report["render"]["verdict"]["strict_order"]) == 6
    assert reduced_report["sport_core_sha256"] != report_a["sport_core_sha256"]
    try:
        apply_scratch(raw, [1, 2, 3, 4, 5], "2026-08-21T09:55:00+02:00")
    except InputError:
        checks["scratch_blocks_undersized_field"] = True
    else:
        raise AssertionError("Un peloton reduit sous trois partants aurait du etre bloque")
    checks["late_scratch_rebuild"] = True

    # --- NOUVEAU 11.4 : chemin leger ------------------------------------------
    lite = {
        "schema_version": LITE_SCHEMA, "template_only": False,
        "race": {
            "race_key": "2026-08-21|LITE|R1C1", "course_name": "DEMO LEGERE",
            "course": "LITE", "race_type": "handicap", "surface": "gazon",
            "distance_m": 1600, "scheduled_start": "2026-08-21T15:00:00+02:00",
            "as_of": "2026-08-21T09:50:00+02:00", "field_declared_final": True,
            "card_source_url": "https://www.france-galop.com/",
            "results_source_url": "https://example.org/results",
        },
        "runners": [
            {"no": 1, "name": "L1", "level": 0.70, "form": 0.25,
             "level_basis": "classe synthetique L1", "form_basis": "forme synthetique L1",
             "confidence": 0.65, "pace_role": "leader"},
            {"no": 2, "name": "L2", "level": 0.40, "form": 0.10,
             "level_basis": "classe synthetique L2", "form_basis": "forme synthetique L2",
             "confidence": 0.60, "pace_role": "prominent"},
            {"no": 3, "name": "L3", "level": 0.10, "form": 0.00,
             "level_basis": "classe synthetique L3", "form_basis": "",
             "confidence": 0.50, "pace_role": "midfield"},
            {"no": 4, "name": "L4", "level": -0.20, "form": 0.20,
             "level_basis": "classe synthetique L4", "form_basis": "forme synthetique L4",
             "confidence": 0.40, "pace_role": "closer"},
            {"no": 5, "name": "L5", "level": -0.55, "form": -0.15,
             "level_basis": "classe synthetique L5", "form_basis": "forme synthetique L5",
             "confidence": 0.55, "pace_role": "closer"},
        ],
        "pace_split": {"SOUTENU": 0.32, "REGULIER": 0.44, "LENT": 0.18},
        "residual_mass": 0.06,
    }
    expanded = expand_lite(lite)
    assert expanded["race"]["data_grade"] == "C" and expanded["fill_mode"] == "LITE"
    validate_input(expanded, current=current, allow_test_samples=True)
    expanded["simulation"]["n_worlds"] = 300
    lite_report, _ = build_report(expanded, current=current, allow_test_samples=True)
    assert lite_report["render"]["verdict"]["strict_order"][0] == 1
    assert len(lite_report["render"]["scenarios"]) == 4
    # Le canal de rythme doit reellement separer les roles entre scenarios.
    fast = next(s for s in lite_report["render"]["scenarios"] if s["id"] == "SOUTENU")
    slow = next(s for s in lite_report["render"]["scenarios"] if s["id"] == "LENT")
    assert fast["notes"]["5"] > slow["notes"]["5"], "le finisseur doit profiter du train soutenu"
    assert fast["notes"]["1"] < slow["notes"]["1"], "l'animateur doit souffrir du train soutenu"
    checks["lite_expansion_valid_and_active"] = True

    broken_lite = json.loads(json.dumps(lite))
    broken_lite["residual_mass"] = 0.20
    try:
        expand_lite(broken_lite)
    except InputError:
        checks["lite_mass_conservation_enforced"] = True
    else:
        raise AssertionError("Une masse totale differente de 1 aurait du etre bloquee")

    # --- NOUVEAU 11.4 : test-retest de saisie ---------------------------------
    variant = json.loads(json.dumps(lite))
    for runner in variant["runners"]:
        runner["level"] = round(runner["level"] + 0.08, 3)
    retest_same = input_retest(expanded, expand_lite(lite), current=current, worlds=200, races=100)
    assert retest_same["order_spearman"] == 1.0
    assert retest_same["verdict"] == "SAISIE_REPRODUCTIBLE"
    noisy = json.loads(json.dumps(lite))
    for index, runner in enumerate(noisy["runners"]):
        shift = 0.35 if index % 2 else -0.35
        runner["level"] = round(max(-0.90, min(0.90, runner["level"] + shift)), 3)
    retest_noisy = input_retest(expanded, expand_lite(noisy), current=current, worlds=200, races=100)
    assert retest_noisy["rms_expected_rank_shift"] > 5 * max(
        retest_same["rms_expected_rank_shift"], 1e-6
    )
    assert retest_noisy["order_spearman"] < 1.0
    assert retest_noisy["verdict"] != "SAISIE_REPRODUCTIBLE"
    assert retest_noisy["input_noise_over_mc_noise"] > 1.0
    checks["retest_detects_input_noise"] = True
    details["retest_ratio_stable_vs_noisy"] = [
        retest_same["input_noise_over_mc_noise"], retest_noisy["input_noise_over_mc_noise"]
    ]

    # --- audit et gouvernance -------------------------------------------------
    result = {
        "schema_version": RESULT_SCHEMA, "template_only": False,
        "race_key": report_a["race_key"], "status": "official_final", "verified": True,
        "source_url": "https://example.org/result", "verified_at": "2026-08-21T15:30:00+02:00",
        "dead_heat": False, "order": list(report_a["render"]["verdict"]["strict_order"]),
        "realized_scenario_id": report_a["render"]["scenarios"][0]["id"],
        "scenario_label_source_url": "https://example.org/replay",
        "scenario_label_basis": "rythme et positions observes sur le replay synthetique",
        "scenario_label_rule": "label etabli sans comparer l'ordre predit a l'arrivee",
    }
    active_numbers = report_a["render"]["verdict"]["strict_order"]
    market_probabilities = {
        str(number): 1.0 / len(active_numbers) for number in active_numbers
    }
    synthetic_ticket = {
        "schema_version": "titan-stake-ticket-1.4",
        "context": {
            "race_key": report_a["race_key"],
            "report_sha256": report_a["report_sha256"],
            "rank_samples_sha256": report_a["rank_samples_sha256"],
            "entry_sha256": entry_1["entry_sha256"],
            "market_observed_at_utc": "2026-08-21T12:57:00+00:00",
            "market_snapshot_sha256": "a" * 64,
        },
        "probabilities_market_shin": market_probabilities,
    }
    synthetic_ticket["ticket_sha256"] = sha256_obj(synthetic_ticket)
    audit = audit_report(
        report_a, result, commit=entry_1, ticket=synthetic_ticket
    )
    assert audit["winner_hierarchy_rank"] == 1
    assert audit["market_winner_log_loss"] is not None
    # Contrat avec la couche de mise: sans ces deux blocs, la calibration ne
    # peut rien ajuster. Une fixture ecrite a la main masquerait le probleme.
    assert audit["market_comparison"] is not None
    assert set(audit["market_comparison"]["internal_consensus"]["values"]) == {
        str(number) for number in report_a["render"]["verdict"]["strict_order"]
    }
    assert set(audit["internal_model_probabilities"]) == set(
        audit["market_comparison"]["internal_consensus"]["values"]
    )
    assert audit["prior_commitment"] is not None
    scenario_check = scenario_reliability([audit])
    assert scenario_check["n_labeled_races"] == 1
    assert scenario_check["verdict"] == "ECHANTILLON_INSUFFISANT"
    batch = batch_audit([audit])
    assert batch["status"] == "INSUFFICIENT_FOR_RECALIBRATION"
    assert batch["market_benchmark"]["n_races_with_market"] == 1
    assert batch["commitment_coverage"] == 1.0
    checks["audit_and_governance"] = True

    # --- v11.6 pare-feu de collecte -------------------------------------------
    # Un test qui n'exerce pas la condition degeneree ne prouve rien: chaque
    # refus est ici declenche pour de vrai, pas asserte en commentaire.
    def must_refuse(mutation: dict[str, Any], label: str) -> None:
        try:
            validate_input(mutation, current=current, allow_test_samples=True)
        except InputError:
            return
        raise AssertionError(f"Refus attendu non declenche: {label}")

    absent = json.loads(json.dumps(raw))
    absent.pop("collection")
    must_refuse(absent, "FULL sans manifeste de collecte")

    confessed = json.loads(json.dumps(raw))
    confessed["collection"]["sources"][0]["price_bearing"] = True
    must_refuse(confessed, "page declaree porteuse de prix")

    disguised = json.loads(json.dumps(raw))
    disguised["collection"]["sources"].append({
        "url": "https://example.org/cotes-partants", "fetched_at": raw["race"]["as_of"],
        "price_bearing": False, "purpose": "declaration mensongere",
    })
    must_refuse(disguised, "URL portant un jeton de prix declaree sans prix")

    undeclared = json.loads(json.dumps(raw))
    undeclared["collection"]["sources"] = [
        source for source in undeclared["collection"]["sources"]
        if "performance" not in source["url"]
    ]
    must_refuse(undeclared, "preuve citant une page absente du manifeste")

    late_fetch = json.loads(json.dumps(raw))
    late_fetch["collection"]["sources"][0]["fetched_at"] = "2026-08-21T14:59:00+02:00"
    must_refuse(late_fetch, "page ouverte apres as_of")

    # Le token doit se lire sur des jetons, jamais sur des sous-chaines: un
    # hippodrome nomme ParisLongchamp ne doit pas declencher le jeton "paris".
    assert price_signals("https://www.parislongchamp.com/partants") == []
    assert price_signals("https://www.pmu.fr/turf/") == ["pmu"]
    assert report_a["collection"]["integrity"] == "SEALED"
    assert entry_1["collection_integrity"] == "SEALED"
    checks["collection_firewall"] = True

    # --- v11.6 attestation de publication -------------------------------------
    start_utc = parse_iso(entry_1["scheduled_start_utc"], "entry.scheduled_start_utc")
    publication = build_publication(
        entry_1, medium="opentimestamps",
        reference="ots:" + "b" * 40,
        published_at=iso_utc(start_utc - timedelta(minutes=5)),
        current=start_utc,
    )
    assert publication["minutes_before_start"] == 5.0

    def must_refuse_publication(kwargs: dict[str, Any], label: str) -> None:
        try:
            build_publication(entry_1, current=start_utc, **kwargs)
        except InputError:
            return
        raise AssertionError(f"Refus attendu non declenche: {label}")

    must_refuse_publication(
        {"medium": "opentimestamps", "reference": "ots:" + "b" * 40,
         "published_at": iso_utc(start_utc + timedelta(minutes=1))},
        "publication posterieure au depart",
    )
    must_refuse_publication(
        {"medium": "private_chat", "reference": "ots:" + "b" * 40,
         "published_at": iso_utc(start_utc - timedelta(minutes=5))},
        "support de publication non opposable",
    )
    must_refuse_publication(
        {"medium": "git_public", "reference": "x",
         "published_at": iso_utc(start_utc - timedelta(minutes=5))},
        "reference non verifiable",
    )

    # Le compteur des 200 ne bouge que si la preuve est complete.
    without = verify_journal([entry_1])
    assert without["n_countable_races"] == 0
    assert without["n_unpublished_races"] == 1
    with_proof = verify_journal([entry_1], [publication])
    assert with_proof["n_countable_races"] == 1
    assert with_proof["n_unpublished_races"] == 0

    audit_unproven = audit_report(report_a, result, commit=entry_1)
    assert audit_unproven["prior_commitment"]["evidential_status"] == "EXPLORATOIRE"
    audit_proven = audit_report(
        report_a, result, commit=entry_1, publication=publication
    )
    assert audit_proven["prior_commitment"]["evidential_status"] == "PROBANTE"
    assert audit_proven["prior_commitment"]["publication_medium"] == "opentimestamps"
    checks["publication_attestation"] = True

    # --- v11.7 voie de transcription ecrite -----------------------------------
    stamp = raw["race"]["as_of"]
    with_reading = json.loads(json.dumps(raw))
    with_reading["collection"]["sources"].append({
        "url": "https://www.france-galop.com/sites/default/files/25plat06.pdf",
        "fetched_at": stamp, "price_bearing": False,
        "purpose": "bulletin officiel, comptes-rendus ecrits",
    })
    target = with_reading["runners"][0]
    number = target["no"]
    with_reading["evidence_registry"][f"BULL-{number}"] = {
        "tier": "F-S1", "family_id": f"BULLETIN-{number}",
        "summary": "compte-rendu ecrit officiel de la sortie precedente",
        "source_url": "https://www.france-galop.com/sites/default/files/25plat06.pdf",
        "observed_at": stamp, "available_at": stamp, "parent_ids": [],
    }
    target["race_reading"] = {"readings": [{
        "evidence_id": f"BULL-{number}", "source_class": "OFFICIEL",
        "observed_at": stamp, "observed_role": "closer", "trouble": ["enferme"],
    }]}
    # Un incident impose un plancher d'incertitude. Le remplissage courant ne le
    # respecte pas: le moteur doit refuser, pas arrondir en silence.
    must_refuse(with_reading, "incident declare sans elargissement epistemique")

    widened = json.loads(json.dumps(with_reading))
    widened["runners"][0]["epistemic_sd"] = 0.60
    widened["runners"][0]["score_components"]["recent_form"]["value"] = 0.0
    validated_reading = validate_input(widened, current=current, allow_test_samples=True)
    reading = validated_reading["readings"][number]
    assert reading["role_basis"] == "OBSERVE"
    assert reading["trouble_flags"] == ["enferme"]
    assert reading["epistemic_floor_increment"] > 0.0

    # L'incident ne doit JAMAIS servir a creer de la valeur.
    inflated = json.loads(json.dumps(widened))
    inflated["runners"][0]["score_components"]["recent_form"]["value"] = 0.45
    must_refuse(inflated, "incident utilise pour gonfler la forme recente")

    # Une source moins fiable credite moins l'incident.
    forum = json.loads(json.dumps(widened))
    forum["runners"][0]["race_reading"]["readings"][0]["source_class"] = "SECONDAIRE"
    forum_reading = validate_input(
        forum, current=current, allow_test_samples=True
    )["readings"][number]
    assert forum_reading["epistemic_floor_increment"] < reading["epistemic_floor_increment"]

    bad_vocab = json.loads(json.dumps(widened))
    bad_vocab["runners"][0]["race_reading"]["readings"][0]["trouble"] = ["pas_en_forme"]
    must_refuse(bad_vocab, "incident hors vocabulaire ferme")

    weak_tier = json.loads(json.dumps(widened))
    weak_tier["evidence_registry"][f"BULL-{number}"]["tier"] = "H"
    weak_tier["evidence_registry"][f"BULL-{number}"].pop("source_url", None)
    must_refuse(weak_tier, "compte-rendu adosse a une preuve non documentaire")

    report_reading = build_report(widened, current=current, allow_test_samples=True)[0]
    assert report_reading["race_reading"]["pace_map_basis"] == "OBSERVE"
    assert report_reading["race_reading"]["n_runners_with_trouble"] == 1
    assert report_a["race_reading"]["pace_map_basis"] == "SUPPOSE"
    checks["written_reading_channel"] = True

    # --- v11.7 detecteur de confondant documentaire ---------------------------
    assert abs(spearman_rho([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0]) - 1.0) < 1e-9
    assert abs(spearman_rho([1.0, 2.0, 3.0, 4.0], [4.0, 3.0, 2.0, 1.0]) + 1.0) < 1e-9
    assert abs(spearman_rho([1.0, 1.0, 1.0, 1.0], [1.0, 2.0, 3.0, 4.0])) < 1e-9

    horses = report_a["render"]["horses"]
    ranked = sorted(horses, key=lambda item: item["expected_rank"])
    # Cas 1: l'arrivee suit exactement le classement. La documentation, si elle
    # predit le modele, predit alors aussi le resultat: pas de confondant.
    aligned = documentation_confounding(report_a, [item["no"] for item in ranked])
    # Cas 2: l'arrivee est l'inverse exact. Si rho_model etait eleve, l'ecart
    # devient maximal et le confondant doit se declencher.
    reversed_order = documentation_confounding(
        report_a, [item["no"] for item in reversed(ranked)]
    )
    assert aligned["rho_confidence_vs_model_rank"] == reversed_order["rho_confidence_vs_model_rank"]
    assert reversed_order["gap"] >= aligned["gap"]
    assert set(aligned) >= {"status", "rho_confidence_vs_actual_rank", "gap"}
    assert "documentation_confounding" in audit_proven
    checks["documentation_confounding_detector"] = True

    # --- v11.8 echelle de classe par allocation -------------------------------
    # L'echelle doit reproduire le jugement d'un praticien: une place moyenne
    # dans un Groupe 3 vaut mieux qu'une victoire en reclamer.
    win_claimer = outing_rating(15000, 1, 8)
    sixth_g3 = outing_rating(130000, 6, 12)
    last_g1 = outing_rating(400000, 16, 16)
    fourth_listed = outing_rating(70000, 4, 10)
    assert sixth_g3 > fourth_listed > win_claimer
    # Le point qui separe une vraie echelle d'un classement par dotation:
    # etre distance dans un Groupe 1 ne doit PAS valoir mieux qu'un bon
    # resultat dans un Groupe 3.
    assert last_g1 < sixth_g3
    assert last_g1 < fourth_listed
    assert competitiveness(1.0) == 1.0 and abs(competitiveness(-1.0) - 0.25) < 1e-12
    # La place doit etre conditionnee au peloton, jamais lue brute.
    assert within_race_performance(4, 6) < within_race_performance(4, 18)
    assert abs(within_race_performance(1, 10) - 1.0) < 1e-12
    assert abs(within_race_performance(10, 10) + 1.0) < 1e-12
    assert abs(class_index(40000)) < 0.02

    base_form = json.loads(json.dumps(raw))
    hist = "https://www.france-galop.com/fr/courses/historique"
    base_form["collection"]["sources"].append({
        "url": hist, "fetched_at": stamp, "price_bearing": False,
        "purpose": "historique chiffre des sorties",
    })
    base_form["race"]["allocation_eur"] = 52000.0
    profiles = [
        (130000.0, "GROUPE_3", 12, 3), (80000.0, "LISTED", 10, 2),
        (40000.0, "HANDICAP", 14, 5), (18000.0, "RECLAMER", 9, 4),
    ]
    for position, runner_block in enumerate(base_form["runners"]):
        allocation, category, field, finish = profiles[position % len(profiles)]
        rid = runner_block["no"]
        base_form["evidence_registry"][f"HIST-{rid}"] = {
            "tier": "F-S1", "family_id": f"HISTORIQUE-{rid}",
            "summary": "historique chiffre des sorties", "source_url": hist,
            "observed_at": stamp, "available_at": stamp, "parent_ids": [],
        }
        runner_block["form_lines"] = [{
            "evidence_id": f"HIST-{rid}", "race_date": "2026-06-15T14:00:00+02:00",
            "allocation_eur": allocation, "category": category,
            "field_size": field, "finish_position": finish,
        }]
    # Le collecteur ne peut plus imposer son jugement: toute divergence est
    # refusee, y compris la valeur 0 qui etait licite avant.
    must_refuse(base_form, "ability_class en desaccord avec l'echelle calculee")

    ladder_probe = build_class_ladder(
        {
            runner_block["no"]: validate_form_lines(
                runner_block, base_form["evidence_registry"],
                parse_iso(base_form["race"]["as_of"], "as_of"), "probe"
            )
            for runner_block in base_form["runners"]
        },
        52000.0,
    )
    assert ladder_probe["active"] is True
    assert ladder_probe["separates"] is True
    aligned_form = json.loads(json.dumps(base_form))
    for runner_block in aligned_form["runners"]:
        runner_block["score_components"]["ability_class"]["value"] = (
            ladder_probe["ability_by_number"][runner_block["no"]]
        )
    validated_form = validate_input(aligned_form, current=current, allow_test_samples=True)
    ladder = validated_form["class_ladder"]
    assert ladder["separates"] is True
    assert all(abs(v) <= LEVEL_CAPS["ability_class"] + 1e-9
               for v in ladder["ability_by_number"].values())
    # Montee et descente de classe se lisent sur l'allocation du jour.
    labels = {item["label"] for item in ladder["class_movement"].values()}
    assert "DESCENTE_DE_CLASSE" in labels or "MONTEE_DE_CLASSE" in labels

    # Peloton homogene: le canal doit s'ANNULER au lieu de fabriquer un ecart.
    flat = json.loads(json.dumps(base_form))
    for runner_block in flat["runners"]:
        runner_block["form_lines"][0].update(
            {"allocation_eur": 40000.0, "category": "HANDICAP",
             "field_size": 12, "finish_position": 6}
        )
        runner_block["score_components"]["ability_class"]["value"] = 0.0
    flat_validated = validate_input(flat, current=current, allow_test_samples=True)
    assert flat_validated["class_ladder"]["separates"] is False
    assert all(value == 0.0
               for value in flat_validated["class_ladder"]["ability_by_number"].values())

    fake = json.loads(json.dumps(aligned_form))
    fake["runners"][0]["form_lines"][0]["finish_position"] = 99
    must_refuse(fake, "place superieure au nombre de partants")
    future = json.loads(json.dumps(aligned_form))
    future["runners"][0]["form_lines"][0]["race_date"] = "2027-01-01T12:00:00+01:00"
    must_refuse(future, "sortie posterieure a as_of")
    absurd = json.loads(json.dumps(aligned_form))
    absurd["runners"][0]["form_lines"][0]["allocation_eur"] = 12.0
    must_refuse(absurd, "allocation hors plage plausible")
    checks["class_ladder_from_allocation"] = True

    # --- v11.8.1 regressions trouvees en audit --------------------------------
    # 1 et 2. race.allocation_eur n'etait pas valide: une valeur negative ou non
    # numerique remontait un ValueError ou un TypeError brut au lieu d'un refus
    # propre. Un crash n'est pas un refus: il ne dit pas quoi corriger.
    bad_alloc = json.loads(json.dumps(aligned_form))
    bad_alloc["race"]["allocation_eur"] = -5000.0
    must_refuse(bad_alloc, "allocation du jour negative")
    text_alloc = json.loads(json.dumps(aligned_form))
    text_alloc["race"]["allocation_eur"] = "beaucoup"
    must_refuse(text_alloc, "allocation du jour non numerique")

    # 3. Le bug serieux. La clause anti-ecrasement empechait l'ablation de
    # neutraliser ability_class: la variante echouait, et le canal le plus
    # recent devenait le seul dont on ne pouvait pas mesurer l'influence. Un
    # module qu'on ne sait pas ablater est un module qu'on croit sur parole.
    ablation = run_ablation(aligned_form, current=current, worlds=400, races=100)
    invalid = [item for item in ablation["modules"] if item.get("status") == "INVALIDE"]
    assert not invalid, f"variantes d'ablation invalides: {[i['module'] for i in invalid]}"
    assert any(item["module"] == "AXE_ability_class" for item in ablation["modules"])
    checks["class_channel_is_ablatable"] = True

    # --- v11.8.2 deux trous trouves en audit d'interaction --------------------
    # A. NON-PARTANT TARDIF. L'echelle est relative au peloton: retirer un
    # partant deplace la mediane et change TOUS les ability_class. Sans
    # recalcul, l'etape T-60 du protocole devenait impossible des qu'un dossier
    # portait un historique chiffre. C'etait le bug le plus grave de la serie:
    # il ne cassait rien en test et cassait tout en course reelle.
    victim = aligned_form["runners"][0]["no"]
    before = {
        runner["no"]: runner["score_components"]["ability_class"]["value"]
        for runner in aligned_form["runners"]
    }
    reduced, scratch_note = apply_scratch(
        json.loads(json.dumps(aligned_form)), [victim], aligned_form["race"]["as_of"]
    )
    validated_reduced = validate_input(
        reduced, current=current, allow_test_samples=True
    )
    assert victim not in validated_reduced["class_ladder"]["ability_by_number"]
    after = {
        runner["no"]: runner["score_components"]["ability_class"]["value"]
        for runner in reduced["runners"] if runner.get("active") is True
    }
    assert after != {k: v for k, v in before.items() if k != victim}, (
        "le retrait doit deplacer la mediane et donc les valeurs recalculees"
    )
    assert scratch_note["removed"] == [victim]

    # B. ECHELLE INACTIVE. Avec un seul partant documente, l'echelle ne produit
    # aucune valeur. Un ability_class invente passait alors sans controle, soit
    # exactement l'hallucination que le module existe pour empecher.
    lonely = json.loads(json.dumps(aligned_form))
    for runner_block in lonely["runners"][1:]:
        runner_block.pop("form_lines", None)
        runner_block["score_components"]["ability_class"] = {
            "value": 0.0, "evidence_ids": []
        }
    lonely["runners"][0]["score_components"]["ability_class"]["value"] = 0.85
    must_refuse(lonely, "ability_class invente alors que l'echelle est inactive")
    lonely["runners"][0]["score_components"]["ability_class"]["value"] = 0.0
    zero_ok = validate_input(lonely, current=current, allow_test_samples=True)
    assert zero_ok["class_ladder"]["active"] is False
    checks["class_ladder_survives_scratch_and_silence"] = True

    # --- v11.9 dispersion des lignes de forme ---------------------------------
    # Deux chevaux de meme note moyenne mais d'historiques opposes: l'un regulier,
    # l'autre oscillant de la reclamer au Groupe 3. Le second doit etre declare
    # PLUS incertain, sans que son niveau bouge.
    steady = validate_form_lines(
        {"form_lines": [
            {"evidence_id": "E", "race_date": "2026-06-15T12:00:00+02:00",
             "allocation_eur": 40000.0, "category": "HANDICAP",
             "field_size": 12, "finish_position": 6},
            {"evidence_id": "E", "race_date": "2026-05-15T12:00:00+02:00",
             "allocation_eur": 42000.0, "category": "HANDICAP",
             "field_size": 12, "finish_position": 6},
            {"evidence_id": "E", "race_date": "2026-04-15T12:00:00+02:00",
             "allocation_eur": 38000.0, "category": "HANDICAP",
             "field_size": 12, "finish_position": 6},
        ]},
        {"E": {"tier": "F-S1"}}, parse_iso("2026-08-21T12:00:00+02:00", "x"), "steady",
    )
    scattered = validate_form_lines(
        {"form_lines": [
            {"evidence_id": "E", "race_date": "2026-06-15T12:00:00+02:00",
             "allocation_eur": 130000.0, "category": "GROUPE_3",
             "field_size": 12, "finish_position": 2},
            {"evidence_id": "E", "race_date": "2026-05-15T12:00:00+02:00",
             "allocation_eur": 15000.0, "category": "RECLAMER",
             "field_size": 12, "finish_position": 11},
            {"evidence_id": "E", "race_date": "2026-04-15T12:00:00+02:00",
             "allocation_eur": 90000.0, "category": "LISTED",
             "field_size": 12, "finish_position": 3},
        ]},
        {"E": {"tier": "F-S1"}}, parse_iso("2026-08-21T12:00:00+02:00", "x"), "scattered",
    )
    assert scattered["rating_dispersion"] > steady["rating_dispersion"]
    assert scattered["dispersion_increment"] > steady["dispersion_increment"]
    assert steady["dispersion_increment"] < 0.05
    # Un echantillon trop court n'autorise aucune conclusion sur la regularite.
    short = validate_form_lines(
        {"form_lines": [
            {"evidence_id": "E", "race_date": "2026-06-15T12:00:00+02:00",
             "allocation_eur": 130000.0, "category": "GROUPE_3",
             "field_size": 12, "finish_position": 2},
            {"evidence_id": "E", "race_date": "2026-05-15T12:00:00+02:00",
             "allocation_eur": 15000.0, "category": "RECLAMER",
             "field_size": 12, "finish_position": 11},
        ]},
        {"E": {"tier": "F-S1"}}, parse_iso("2026-08-21T12:00:00+02:00", "x"), "short",
    )
    assert short["dispersion_increment"] == 0.0
    checks["form_dispersion_widens_uncertainty"] = True

    # --- v11.10 longueur de battue --------------------------------------------
    # Le defaut le plus grossier de la v11.9: un 4e battu d'une encolure et un
    # 4e battu de quinze longueurs recevaient la MEME note de sortie.
    stamp_hist = "2026-06-15T12:00:00+02:00"
    as_of_probe = parse_iso("2026-08-21T12:00:00+02:00", "x")
    reg_probe = {"E": {"tier": "F-S1"}}

    def margin_probe(**extra: Any) -> dict[str, Any]:
        line = {"evidence_id": "E", "race_date": stamp_hist,
                "allocation_eur": 40000.0, "category": "HANDICAP",
                "field_size": 12, "finish_position": 4, "distance_m": 1600.0}
        line.update(extra)
        return validate_form_lines(
            {"form_lines": [line]}, reg_probe, as_of_probe, "margin"
        )

    close = margin_probe(beaten_lengths=0.3)
    thrashed = margin_probe(beaten_lengths=15.0)
    rank_only = validate_form_lines(
        {"form_lines": [{"evidence_id": "E", "race_date": stamp_hist,
                         "allocation_eur": 40000.0, "category": "HANDICAP",
                         "field_size": 12, "finish_position": 4}]},
        reg_probe, as_of_probe, "rank",
    )
    # Meme place, meme peloton, meme allocation: seule la marge change.
    assert close["rating"] > thrashed["rating"], (close["rating"], thrashed["rating"])
    assert close["margin_coverage"] == 1.0
    assert rank_only["margin_coverage"] == 0.0
    # Retro-compatibilite stricte: sans marge, la note est celle de la v11.9.
    assert abs(rank_only["rating"] - round(
        outing_rating(40000.0, 4, 12), 4)) < 1e-9
    # La marge encadre le rang, elle ne le remplace pas: un 4e d'une encolure ne
    # depasse pas un vainqueur, un 4e de quinze longueurs ne tombe pas sous un
    # dernier. C'est ce que borne MARGIN_WEIGHT.
    assert margin_probe(finish_position=1, field_size=12,
                        beaten_lengths=0.0)["rating"] > close["rating"]
    # La normalisation par la distance: cinq longueurs sur 1200 m coutent plus
    # que cinq longueurs sur 2800 m.
    assert (margin_probe(beaten_lengths=5.0, distance_m=1200.0)["rating"]
            < margin_probe(beaten_lengths=5.0, distance_m=2800.0)["rating"])
    # Plafond bas: au-dela de la reference, "battu de trente" et "battu de
    # quarante" ne se distinguent plus - c'est du bruit de fin de course.
    assert (margin_probe(beaten_lengths=30.0)["rating"]
            == margin_probe(beaten_lengths=40.0)["rating"])

    def margin_must_refuse(label: str, **extra: Any) -> None:
        try:
            margin_probe(**extra)
        except InputError:
            return
        raise AssertionError(f"Refus attendu non declenche: {label}")

    # Sondes adverses sur l'entree nouvelle.
    margin_must_refuse("marge negative", beaten_lengths=-1.0)
    margin_must_refuse("marge non numerique", beaten_lengths="courte tete")
    margin_must_refuse("marge hors plage", beaten_lengths=1000.0)
    margin_must_refuse("marge non finie", beaten_lengths=float("nan"))
    margin_must_refuse("vainqueur battu de longueurs",
                       finish_position=1, beaten_lengths=2.0)
    margin_must_refuse("distance sans marge", distance_m=1600.0)
    margin_must_refuse("distance hors plage", beaten_lengths=2.0, distance_m=50.0)
    margin_must_refuse("distance non numerique", beaten_lengths=2.0, distance_m="mile")
    checks["margin_separates_close_from_beaten"] = True

    # Absence de marge: elargit l'incertitude RELATIVEMENT au peloton, et ne
    # touche jamais au niveau. Meme regle que l'incident de parcours.
    margin_field = json.loads(json.dumps(aligned_form))
    for position, runner_block in enumerate(margin_field["runners"]):
        # Tous documentent leur marge SAUF le premier.
        if position:
            runner_block["form_lines"][0]["beaten_lengths"] = 2.0 + position
            runner_block["form_lines"][0]["distance_m"] = 1600.0
    margin_ladder = build_class_ladder(
        {
            runner_block["no"]: validate_form_lines(
                runner_block, margin_field["evidence_registry"],
                parse_iso(margin_field["race"]["as_of"], "as_of"), "probe"
            )
            for runner_block in margin_field["runners"]
        },
        52000.0,
    )
    for runner_block in margin_field["runners"]:
        runner_block["score_components"]["ability_class"]["value"] = (
            margin_ladder["ability_by_number"][runner_block["no"]]
        )
    # Le cheval sans marge garde l'epistemic_sd du peloton: refus attendu.
    must_refuse(margin_field, "marge moins documentee que le peloton sans elargissement")
    widened = json.loads(json.dumps(margin_field))
    widened["runners"][0]["epistemic_sd"] = 0.35 + MARGIN_EPISTEMIC_MAX + 0.01
    ok_widened = validate_input(widened, current=current, allow_test_samples=True)
    assert ok_widened["class_ladder"]["margin_coverage_mean"] > 0.0
    # Personne ne documente: aucun differentiel, aucun refus, comportement v11.9.
    assert validate_input(
        aligned_form, current=current, allow_test_samples=True
    )["class_ladder"]["margin_coverage_mean"] == 0.0
    checks["margin_absence_widens_uncertainty"] = True

    # Ablatable, sinon c'est un module qu'on croit sur parole.
    margin_variants = ablation_variants(widened)
    assert "MARGE_LONGUEURS" in margin_variants
    stripped_lines = [
        line for runner_block in margin_variants["MARGE_LONGUEURS"]["runners"]
        for line in (runner_block.get("form_lines") or [])
    ]
    assert all("beaten_lengths" not in line for line in stripped_lines)
    # La variante doit VALIDER: retirer la marge change l'ability_class calculee,
    # et sans recopie elle echouerait sur la clause anti-ecrasement - le defaut
    # exact que la v11.8 avait introduit pour ability_class.
    validate_input(
        margin_variants["MARGE_LONGUEURS"], current=current, allow_test_samples=True
    )
    assert "MARGE_LONGUEURS" not in ablation_variants(aligned_form)
    checks["margin_channel_is_ablatable"] = True

    # --- v11.10 effets humains en A/E -----------------------------------------
    # Un taux de reussite mesure la qualite des chevaux confies; l'A/E mesure ce
    # que l'entourage ajoute au prix. La v11.9 laissait entrer un chiffre libre.
    human_base = json.loads(json.dumps(aligned_form))
    human_base["runners"][0]["score_components"]["human_equipment"] = {
        "value": 0.20, "evidence_ids": [],
    }
    must_refuse(human_base, "human_equipment non nul sans enregistrement A/E")

    ae_url = "https://www.france-galop.com/fr/statistiques"
    human_ok = json.loads(json.dumps(aligned_form))
    human_ok["collection"]["sources"].append({
        "url": ae_url, "fetched_at": stamp, "price_bearing": False,
        "purpose": "statistiques A/E entraineur",
    })
    human_ok["evidence_registry"]["AE-1"] = {
        "tier": "F-S1", "family_id": "AE-ENTRAINEUR-1",
        "summary": "A/E entraineur sur 90 jours", "source_url": ae_url,
        "observed_at": stamp, "available_at": stamp, "parent_ids": [],
    }
    ae_record = {
        "evidence_id": "AE-1", "scope": "TRAINER", "window_days": 90,
        "runners": 420, "wins": 68, "expected_wins": 48.0,
        "observed_at": stamp, "context": "plat, handicaps de classe 2 et 3",
    }
    human_ok["runners"][0]["human_records"] = [ae_record]
    computed_human = validate_human_records(
        human_ok["runners"][0], human_ok["evidence_registry"],
        parse_iso(human_ok["race"]["as_of"], "as_of"), "probe",
    )
    assert computed_human["declared"] is True
    assert computed_human["value"] > 0.0        # A/E > 1 credite
    assert computed_human["records"][0]["a_over_e"] > 1.0
    # Le collecteur ne peut plus imposer son jugement.
    must_refuse(human_ok, "human_equipment en desaccord avec l'A/E calcule")
    human_ok["runners"][0]["score_components"]["human_equipment"] = {
        "value": computed_human["value"], "evidence_ids": ["AE-1"],
    }
    validated_human = validate_input(human_ok, current=current, allow_test_samples=True)
    assert validated_human["human_records"][1]["value"] == computed_human["value"]

    # DEFAUT TROUVE EN AUDIT v11.10, identique a celui de la v11.8 sur
    # ability_class: neutraliser l'axe sans retirer ses ENTREES fait echouer la
    # variante sur la clause anti-ecrasement, et l'axe le plus recent devient le
    # seul dont on ne peut pas mesurer l'influence.
    human_variants = ablation_variants(human_ok)
    neutralised = human_variants["AXE_human_equipment"]
    assert all(not runner.get("human_records")
               for runner in neutralised["runners"] if runner.get("active"))
    validate_input(neutralised, current=current, allow_test_samples=True)

    # Un A/E sous 1 doit COUTER: c'est tout l'interet de la mesure.
    cold = json.loads(json.dumps(human_ok))
    cold["runners"][0]["human_records"][0].update({"wins": 20, "expected_wins": 45.0})
    cold_value = validate_human_records(
        cold["runners"][0], cold["evidence_registry"],
        parse_iso(cold["race"]["as_of"], "as_of"), "probe",
    )["value"]
    assert cold_value < 0.0
    checks["human_effects_require_ae"] = True

    # Retrecissement par denominateur: un A/E de 1,60 sur 25 partants pese moins
    # qu'un A/E de 1,08 sur 1500. C'est le failure mode dominant du domaine.
    def ae_value(runners_n: int, wins: int, expected: float) -> float:
        probe = json.loads(json.dumps(human_ok))
        probe["runners"][0]["human_records"][0].update(
            {"runners": runners_n, "wins": wins, "expected_wins": expected}
        )
        return validate_human_records(
            probe["runners"][0], probe["evidence_registry"],
            parse_iso(probe["race"]["as_of"], "as_of"), "probe",
        )["value"]

    assert ae_value(1500, 240, 150.0) > ae_value(40, 10, 6.25)

    def human_must_refuse(label: str, **patch: Any) -> None:
        probe = json.loads(json.dumps(human_ok))
        record = dict(ae_record)
        record.update(patch)
        for key, value in list(record.items()):
            if value is None:
                record.pop(key)
        probe["runners"][0]["human_records"] = [record]
        try:
            validate_human_records(
                probe["runners"][0], probe["evidence_registry"],
                parse_iso(probe["race"]["as_of"], "as_of"), "probe",
            )
        except InputError:
            return
        raise AssertionError(f"Refus attendu non declenche: {label}")

    # Sondes adverses sur l'entree nouvelle.
    human_must_refuse("denominateur trop court", runners=10)
    human_must_refuse("denominateur negatif", runners=-50)
    human_must_refuse("denominateur non entier", runners=42.5)
    human_must_refuse("victoires superieures au denominateur", wins=500)
    human_must_refuse("victoires negatives", wins=-1)
    human_must_refuse("attendu nul", expected_wins=0.0)
    human_must_refuse("attendu negatif", expected_wins=-3.0)
    human_must_refuse("attendu non numerique", expected_wins="beaucoup")
    human_must_refuse("attendu superieur au denominateur", expected_wins=900.0)
    human_must_refuse("fenetre hors liste", window_days=7)
    human_must_refuse("fenetre absente", window_days=None)
    human_must_refuse("perimetre inconnu", scope="ASTROLOGIE")
    human_must_refuse("contexte absent", context=None)
    human_must_refuse("preuve inconnue du registre", evidence_id="AE-INEXISTANT")
    human_must_refuse("observation posterieure a as_of",
                      observed_at="2027-01-01T12:00:00+01:00")
    # Un bruit de piste est un tier H: il peut ouvrir un scenario, jamais
    # deplacer un niveau.
    rumour = json.loads(json.dumps(human_ok))
    rumour["evidence_registry"]["AE-RUMEUR"] = {
        "tier": "H", "family_id": "AE-RUMEUR", "summary": "propos d'entraineur",
        "observed_at": stamp, "available_at": stamp, "parent_ids": ["PERF-1"],
    }
    rumour["runners"][0]["human_records"] = [dict(ae_record, evidence_id="AE-RUMEUR")]
    try:
        validate_human_records(
            rumour["runners"][0], rumour["evidence_registry"],
            parse_iso(rumour["race"]["as_of"], "as_of"), "probe",
        )
        raise AssertionError("Refus attendu non declenche: A/E adosse a un tier H")
    except InputError:
        pass
    # Deux fenetres du meme perimetre compteraient deux fois la meme information.
    duplicated = json.loads(json.dumps(human_ok))
    duplicated["runners"][0]["human_records"] = [
        dict(ae_record), dict(ae_record, window_days=30),
    ]
    try:
        validate_human_records(
            duplicated["runners"][0], duplicated["evidence_registry"],
            parse_iso(duplicated["race"]["as_of"], "as_of"), "probe",
        )
        raise AssertionError("Refus attendu non declenche: perimetre A/E duplique")
    except InputError:
        pass
    checks["human_ae_shrinks_with_denominator"] = True

    # --- v11.10 : seconde moitie du bug de non-partant --------------------------
    # DEFAUT PREEXISTANT, trouve en auditant la v11.9 livree. La v11.8.2 avait
    # corrige l'echelle de classe, qui est CALCULEE. Le plancher d'incertitude a
    # la meme relativite au peloton mais porte sur epistemic_sd, qui est DECLARE:
    # retirer un partant peu incertain fait monter la mediane, donc le plancher,
    # et un dossier valide AVANT le retrait etait refuse APRES - sans que rien
    # concernant le cheval gene ait change. L'operateur devait alors inventer une
    # incertitude a T-60 pour franchir la porte.
    scratch_case = json.loads(json.dumps(raw))
    bulletin = "https://www.france-galop.com/sites/default/files/bulletin.pdf"
    scratch_case["collection"]["sources"].append({
        "url": bulletin, "fetched_at": stamp, "price_bearing": False,
        "purpose": "compte-rendu officiel",
    })
    scratch_case["evidence_registry"]["BULL-SC"] = {
        "tier": "F-S1", "family_id": "BULLETIN-SC", "summary": "compte-rendu officiel",
        "source_url": bulletin, "observed_at": stamp, "available_at": stamp,
        "parent_ids": [],
    }
    scratch_case["runners"][0]["race_reading"] = {"readings": [{
        "evidence_id": "BULL-SC", "source_class": "OFFICIEL", "observed_at": stamp,
        "observed_role": "closer", "trouble": ["enferme"],
    }]}
    # Des epistemic_sd etales, pour que la mediane BOUGE a un retrait.
    for position, runner_block in enumerate(scratch_case["runners"]):
        runner_block["epistemic_sd"] = [0.35, 0.20, 0.22, 0.24, 0.60, 0.62, 0.64][position]
    # On amene le cheval gene PILE a son plancher.
    for _ in range(20):
        try:
            validate_input(scratch_case, current=current, allow_test_samples=True)
            break
        except InputError as exc:
            found = re.search(r">= ([0-9.]+)", str(exc))
            if not found:
                raise
            scratch_case["runners"][0]["epistemic_sd"] = float(found.group(1))
    floor_before = float(scratch_case["runners"][0]["epistemic_sd"])
    # Retrait d'un partant PEU incertain: la mediane monte, le plancher aussi.
    reduced, scratch_note = apply_scratch(
        scratch_case, [2], "2026-08-21T09:55:00+02:00"
    )
    # La reconstruction doit passer: c'est la promesse du protocole a T-60.
    validate_input(reduced, current=current, allow_test_samples=True)
    widened_entry = scratch_note["epistemic_widened_after_scratch"]
    assert widened_entry, "le plancher a monte sans que l'incertitude soit portee"
    assert widened_entry[0]["no"] == 1
    # Sens conservateur UNIQUEMENT: on elargit, jamais on ne resserre.
    assert widened_entry[0]["epistemic_sd_after"] > widened_entry[0]["epistemic_sd_before"]
    survivors_after = {
        int(r["no"]): float(r["epistemic_sd"])
        for r in reduced["runners"] if r.get("active") is True
    }
    before_by_number = {
        int(r["no"]): float(r["epistemic_sd"])
        for r in scratch_case["runners"] if r.get("active") is True
    }
    assert all(survivors_after[n] >= before_by_number[n] - 1e-9 for n in survivors_after)
    assert survivors_after[1] >= floor_before
    # Retrait d'un partant TRES incertain: la mediane baisse, aucun elargissement.
    _, quiet_note = apply_scratch(scratch_case, [5], "2026-08-21T09:55:00+02:00")
    assert quiet_note["epistemic_widened_after_scratch"] == []
    checks["scratch_carries_epistemic_floor"] = True

    # --- v11.9 compression du handicap ----------------------------------------
    tight_field = [{"active": True, "weight_kg": w} for w in (57.0, 56.5, 56.0, 55.5, 55.0)]
    wide_field = [{"active": True, "weight_kg": w} for w in (60.0, 57.0, 54.0, 51.5, 50.0)]
    assert handicap_compression(tight_field, "HANDICAP")["handicapper_verdict"] == "PELOTON_RESSERRE"
    assert handicap_compression(wide_field, "HANDICAP")["handicapper_verdict"] == "PELOTON_ETALE"
    # Hors handicap, le poids ne dit rien de l'avis d'un handicapeur.
    assert handicap_compression(wide_field, "GROUPE")["applicable"] is False
    assert handicap_compression(tight_field[:2], "HANDICAP")["applicable"] is False
    # Le desaccord entre les deux lectures doit remonter, dans les deux sens.
    assert clarity_second_opinion(
        {"level": "FORTE"}, handicap_compression(tight_field, "HANDICAP")
    )["status"] == "DESACCORD_MODELE_PLUS_SUR"
    assert clarity_second_opinion(
        {"level": "NULLE"}, handicap_compression(wide_field, "HANDICAP")
    )["status"] == "DESACCORD_HANDICAPEUR_PLUS_SUR"
    assert clarity_second_opinion(
        {"level": "NULLE"}, handicap_compression(tight_field, "HANDICAP")
    )["status"] == "ACCORD"
    assert clarity_second_opinion({"level": "FORTE"}, {"applicable": False})["status"] == "SANS_OBJET"
    weighted = json.loads(json.dumps(aligned_form))
    weighted["race"]["race_type"] = "handicap"
    for index, runner_block in enumerate(weighted["runners"]):
        runner_block["weight_kg"] = 56.0 - 0.5 * index
    report_weighted = build_report(weighted, current=current, allow_test_samples=True)[0]
    assert report_weighted["handicap_compression"]["applicable"] is True
    assert report_weighted["clarity_second_opinion"]["status"] in {
        "ACCORD", "DESACCORD_MODELE_PLUS_SUR", "DESACCORD_HANDICAPEUR_PLUS_SUR"
    }
    absurd_weight = json.loads(json.dumps(weighted))
    absurd_weight["runners"][0]["weight_kg"] = 120.0
    must_refuse(absurd_weight, "poids hors plage physique")
    checks["handicapper_second_opinion"] = True

    print(
        json.dumps(
            {
                "status": "PASS",
                "engine_version": ENGINE_VERSION,
                "tests": checks,
                "details": details,
                "warning": "synthetic_tests_only_no_predictive_validation",
            },
            ensure_ascii=False, indent=2,
        )
    )


# ---------------------------------------------------------------------------
# 13. LIGNE DE COMMANDE
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TITAN PLAT v11.5 SCENARIO-BMA")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    template = sub.add_parser("template")
    template.add_argument("kind", choices=("input", "result", "lite"))
    template.add_argument("-o", "--output", required=True)
    expand = sub.add_parser("expand")
    expand.add_argument("lite")
    expand.add_argument("-o", "--output", required=True)
    retest = sub.add_parser("retest")
    retest.add_argument("first")
    retest.add_argument("second")
    scen = sub.add_parser("scenario-audit")
    scen.add_argument("audits", nargs="+")
    scen.add_argument("-o", "--output", required=True)
    ablbatch = sub.add_parser("ablate-batch")
    ablbatch.add_argument("ablations", nargs="+")
    ablbatch.add_argument("-o", "--output", required=True)
    chall = sub.add_parser("challenge")
    chall.add_argument("--champion", nargs="+", required=True)
    chall.add_argument("--challenger", nargs="+", required=True)
    chall.add_argument("-o", "--output", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("input")
    analyze = sub.add_parser("analyze")
    analyze.add_argument("input")
    analyze.add_argument("-o", "--output", required=True)
    analyze.add_argument("--samples", required=True)
    render = sub.add_parser("render")
    render.add_argument("report")
    scratch = sub.add_parser("scratch")
    scratch.add_argument("input")
    scratch.add_argument("--remove", type=int, nargs="+", required=True)
    scratch.add_argument("--as-of", required=True, dest="as_of")
    scratch.add_argument("-o", "--output", required=True)
    commit = sub.add_parser("commit")
    commit.add_argument("report")
    commit.add_argument("-o", "--output", required=True)
    commit.add_argument("--journal", default=None)
    commit.add_argument("--supersedes", default=None)
    commit.add_argument("--reason", default=None, choices=sorted(REVISION_REASONS))
    publish = sub.add_parser("publish")
    publish.add_argument("commit")
    publish.add_argument("-o", "--output", required=True)
    publish.add_argument("--medium", required=True, choices=sorted(PUBLICATION_MEDIA))
    publish.add_argument("--reference", required=True)
    publish.add_argument("--at", required=True, dest="published_at")
    publish.add_argument("--ledger", default=None)
    verify = sub.add_parser("verify-journal")
    verify.add_argument("journal")
    verify.add_argument("--publications", default=None)
    ablate = sub.add_parser("ablate")
    ablate.add_argument("input")
    ablate.add_argument("-o", "--output", required=True)
    ablate.add_argument("--worlds", type=int, default=400)
    ablate.add_argument("--races", type=int, default=100)
    delta = sub.add_parser("delta")
    delta.add_argument("before")
    delta.add_argument("after")
    audit = sub.add_parser("audit")
    audit.add_argument("report")
    audit.add_argument("result")
    audit.add_argument("-o", "--output", required=True)
    audit.add_argument("--commit", default=None)
    audit.add_argument("--ticket", default=None)
    audit.add_argument("--publication", default=None)
    batch = sub.add_parser("batch-audit")
    batch.add_argument("audits", nargs="+")
    batch.add_argument("-o", "--output", required=True)
    args = parser.parse_args(argv)

    def load(path: str) -> Any:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    try:
        if args.command == "self-test":
            self_test()
            return 0
        if args.command == "template":
            value = {"input": input_template, "result": result_template,
                     "lite": lite_template}[args.kind]()
            write_json(args.output, value)
            print(json.dumps({"status": "OK", "output": args.output}))
            return 0
        if args.command == "expand":
            full = expand_lite(load(args.lite))
            checked = validate_input(full)
            write_json(args.output, full)
            print(json.dumps({
                "status": "OK", "output": args.output, "fill_mode": "LITE",
                "active_runners": checked["numbers"],
                "scenario_count": len(checked["scenarios"]),
                "data_grade": full["race"]["data_grade"],
                "next": "analyze puis commit avant le depart",
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "retest":
            print(json.dumps(input_retest(load(args.first), load(args.second)),
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "scenario-audit":
            value = scenario_reliability([load(p) for p in args.audits])
            write_json(args.output, value)
            print(json.dumps({"status": "OK", "output": args.output,
                              "verdict": value["verdict"],
                              "mean_absolute_calibration_error":
                                  value["mean_absolute_calibration_error"],
                              "reliability_table": value["reliability_table"]},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "ablate-batch":
            value = aggregate_ablations([load(p) for p in args.ablations])
            write_json(args.output, value)
            print(json.dumps({"status": "OK", "output": args.output,
                              "inert_modules": value["inert_modules"],
                              "marginal_modules": value["marginal_modules"]},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "challenge":
            value = challenge([load(p) for p in args.champion],
                              [load(p) for p in args.challenger])
            write_json(args.output, value)
            print(json.dumps({"status": "OK", "output": args.output,
                              "verdict": value["verdict"],
                              "n_paired_races": value["n_paired_races"],
                              "mean_difference": value["mean_difference"],
                              "ci95": [value["ci95_low"], value["ci95_high"]],
                              "promotion_allowed": value["promotion_allowed"]},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "validate":
            checked = validate_input(load(args.input))
            print(json.dumps(
                {
                    "status": "OK", "active_runners": checked["numbers"],
                    "scenario_count": len(checked["scenarios"]),
                    "simulation": checked["simulation"], "clock": checked["clock"],
                }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "analyze":
            report, samples = build_report(load(args.input), engine_path=__file__)
            save_samples(args.samples, samples)
            write_json(args.output, report)
            print(render_report(report))
            return 0
        if args.command == "render":
            print(render_report(load(args.report)))
            return 0
        if args.command == "scratch":
            updated, note = apply_scratch(load(args.input), args.remove, args.as_of)
            write_json(args.output, updated)
            print(json.dumps({"status": "OK", "output": args.output, **note},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "commit":
            report = load(args.report)
            previous = None
            existing: list[dict[str, Any]] = []
            if args.supersedes and not args.journal:
                raise InputError("Une revision exige --journal pour verifier la branche supersedee")
            if args.journal:
                existing = read_journal(args.journal)
                if existing:
                    state = verify_journal(existing)
                    if not state["chain_ok"]:
                        raise InputError(f"Journal corrompu, engagement refuse: {state['problems']}")
                    previous = state["head_sha256"]
                    if args.supersedes:
                        superseded = load(args.supersedes)
                        latest_same_race = next(
                            (item for item in reversed(existing)
                             if item.get("race_key") == report.get("race_key")),
                            None,
                        )
                        if latest_same_race is None:
                            raise InputError("Aucun engagement precedent de cette course dans le journal")
                        if superseded.get("entry_sha256") != latest_same_race.get("entry_sha256"):
                            raise InputError(
                                "--supersedes doit viser le dernier engagement de cette course"
                            )
            entry = build_commit(
                report, previous_entry_sha256=previous,
                supersedes=load(args.supersedes) if args.supersedes else None,
                revision_reason=args.reason,
            )
            if args.journal:
                proposed = verify_journal([*existing, entry])
                if not proposed["chain_ok"]:
                    raise InputError(
                        f"Engagement refuse avant ecriture: {proposed['problems']}"
                    )
            write_json(args.output, entry)
            if args.journal:
                append_journal(args.journal, entry)
            print(json.dumps(
                {
                    "status": "OK", "output": args.output, "journal": args.journal,
                    "entry_sha256": entry["entry_sha256"],
                    "minutes_before_start": entry["minutes_before_start"],
                    "action": "PUBLIER CET ENREGISTREMENT MAINTENANT, AVANT LE DEPART",
                }, ensure_ascii=False))
            return 0
        if args.command == "publish":
            record = build_publication(
                load(args.commit), medium=args.medium, reference=args.reference,
                published_at=args.published_at,
            )
            write_json(args.output, record)
            if args.ledger:
                append_journal(args.ledger, record)
            print(json.dumps({
                "status": "OK", "output": args.output,
                "publication_sha256": record["publication_sha256"],
                "minutes_before_start": record["minutes_before_start"],
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "verify-journal":
            print(json.dumps(
                verify_journal(
                    read_journal(args.journal),
                    read_journal(args.publications) if args.publications else None,
                ),
                ensure_ascii=False, indent=2,
            ))
            return 0
        if args.command == "ablate":
            value = run_ablation(load(args.input), engine_path=__file__, worlds=args.worlds, races=args.races)
            write_json(args.output, value)
            print(json.dumps(
                {
                    "status": "OK", "output": args.output,
                    "most_influential": value["most_influential"], "no_effect": value["no_effect"],
                }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "delta":
            print(json.dumps(report_delta(load(args.before), load(args.after)), ensure_ascii=False, indent=2))
            return 0
        if args.command == "audit":
            commit_entry = load(args.commit) if args.commit else None
            value = audit_report(
                load(args.report), load(args.result), commit=commit_entry,
                ticket=load(args.ticket) if args.ticket else None,
                publication=load(args.publication) if args.publication else None,
            )
            write_json(args.output, value)
            print(json.dumps({"status": "OK", "output": args.output, "audit_sha256": value["audit_sha256"]}))
            return 0
        if args.command == "batch-audit":
            value = batch_audit([load(path) for path in args.audits])
            write_json(args.output, value)
            print(json.dumps({"status": "OK", "output": args.output, "n_races": value["n_races"]}))
            return 0
    except (InputError, AssertionError, KeyError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
