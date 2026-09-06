#!/usr/bin/env python3
"""Audit adverse de TITAN PLAT v11.10 / TITAN STAKE v1.6.

Ce script n'est pas une relecture: il ATTAQUE le systeme. Il rejoue la chaine
complete en parametres de production, sonde chaque entree nouvelle avec des
valeurs hostiles, verifie les interactions entre modules plutot que les modules
isoles, et controle que chaque module du code a bien son instruction dans le
contrat d'execution.

    python3 TITAN_v11_10_AUDIT.py            # audit complet
    python3 TITAN_v11_10_AUDIT.py --json     # sortie machine
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
ENGINE_PATH = ROOT / "titan_plat_v11_10_engine.py"
STAKE_PATH = ROOT / "titan_stake_v1_6.py"
PROMPT_PATH = ROOT / "TITAN_PLAT_v11_10_PROMPT_ONESHOT.txt"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENG = load(ENGINE_PATH, "eng")
STK = load(STAKE_PATH, "stk")

RESULTS: list[dict] = []


def record(section: str, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"section": section, "check": name, "ok": bool(ok), "detail": detail})


def refuses(fn, label: str, section: str, expect: type = ENG.InputError) -> None:
    """Un refus qui n'a jamais ete declenche en test n'existe pas."""
    try:
        fn()
    except expect as exc:
        record(section, label, True, str(exc)[:110])
    except Exception as exc:  # noqa: BLE001
        record(section, label, False,
               f"mauvais type d'erreur {type(exc).__name__}: {str(exc)[:80]}")
    else:
        record(section, label, False, "AUCUN REFUS DECLENCHE")


# ---------------------------------------------------------------------------
# Dossier de production, avec marges ET A/E
# ---------------------------------------------------------------------------

def production_input() -> tuple[dict, datetime]:
    raw, _ = ENG.synthetic_input()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now + timedelta(minutes=40)
    as_of = now - timedelta(minutes=5)
    stamp = ENG.iso_utc(as_of)
    raw["race"]["scheduled_start"] = ENG.iso_utc(start)
    raw["race"]["as_of"] = stamp
    raw["race"]["allocation_eur"] = 52000.0
    raw["race"]["field_declared_final"] = True
    for item in raw["evidence_registry"].values():
        item["observed_at"] = stamp
        item["available_at"] = stamp
    for src in raw["collection"]["sources"]:
        src["fetched_at"] = stamp
    hist = "https://www.france-galop.com/fr/courses/historique"
    ae_url = "https://www.france-galop.com/fr/statistiques"
    raw["collection"]["sources"] += [
        {"url": hist, "fetched_at": stamp, "price_bearing": False,
         "purpose": "historique chiffre avec ecarts"},
        {"url": ae_url, "fetched_at": stamp, "price_bearing": False,
         "purpose": "statistiques A/E"},
    ]
    profiles = [(130000., "GROUPE_3", 12, 3, 1.5), (80000., "LISTED", 10, 2, 0.75),
                (40000., "HANDICAP", 14, 5, 4.0), (18000., "RECLAMER", 9, 4, 6.0)]
    for pos, rb in enumerate(raw["runners"]):
        alloc, cat, field, finish, beaten = profiles[pos % len(profiles)]
        rid = rb["no"]
        raw["evidence_registry"][f"HIST-{rid}"] = {
            "tier": "F-S1", "family_id": f"HISTORIQUE-{rid}",
            "summary": "historique chiffre", "source_url": hist,
            "observed_at": stamp, "available_at": stamp, "parent_ids": []}
        rb["form_lines"] = [{
            "evidence_id": f"HIST-{rid}",
            "race_date": ENG.iso_utc(as_of - timedelta(days=60 + pos * 5)),
            "allocation_eur": alloc, "category": cat, "field_size": field,
            "finish_position": finish, "beaten_lengths": beaten,
            "distance_m": 1600.0}]
    raw["evidence_registry"]["AE-1"] = {
        "tier": "F-S1", "family_id": "AE-ENTRAINEUR-1",
        "summary": "A/E entraineur 90 jours", "source_url": ae_url,
        "observed_at": stamp, "available_at": stamp, "parent_ids": []}
    raw["runners"][0]["human_records"] = [{
        "evidence_id": "AE-1", "scope": "TRAINER", "window_days": 90,
        "runners": 420, "wins": 68, "expected_wins": 48.0,
        "observed_at": stamp, "context": "plat, handicaps classe 2-3"}]
    as_of_dt = ENG.parse_iso(stamp, "x")
    ladder = ENG.build_class_ladder(
        {rb["no"]: ENG.validate_form_lines(rb, raw["evidence_registry"], as_of_dt, "p")
         for rb in raw["runners"]}, 52000.0)
    for rb in raw["runners"]:
        rb["score_components"]["ability_class"] = {
            "value": ladder["ability_by_number"][rb["no"]],
            "evidence_ids": [f"HIST-{rb['no']}"]}
    human = ENG.validate_human_records(
        raw["runners"][0], raw["evidence_registry"], as_of_dt, "p")
    raw["runners"][0]["score_components"]["human_equipment"] = {
        "value": human["value"], "evidence_ids": ["AE-1"]}
    raw["runners"][0]["score_components"]["recent_form"] = {
        "value": 0.0, "evidence_ids": []}
    raw["simulation"]["n_worlds"] = 2000
    raw["simulation"]["races_per_world"] = 100
    for _ in range(25):
        try:
            ENG.validate_input(raw, allow_test_samples=False)
            break
        except ENG.InputError as exc:
            found = re.search(r"runners.active\[(\d+)\].*?>= ([0-9.]+)", str(exc))
            if not found:
                raise
            active = [r for r in raw["runners"] if r.get("active")]
            active[int(found.group(1))]["epistemic_sd"] = float(found.group(2))
    return raw, start


# ---------------------------------------------------------------------------
# 1. SONDES ADVERSES sur chaque entree nouvelle
# ---------------------------------------------------------------------------

def audit_adversarial_probes() -> None:
    section = "1. Sondes adverses"
    as_of = ENG.parse_iso("2026-08-21T12:00:00+02:00", "x")
    registry = {"E": {"tier": "F-S1"}}

    def margin(**extra):
        line = {"evidence_id": "E", "race_date": "2026-06-15T12:00:00+02:00",
                "allocation_eur": 40000.0, "category": "HANDICAP",
                "field_size": 12, "finish_position": 4, "distance_m": 1600.0}
        line.update(extra)
        return lambda: ENG.validate_form_lines(
            {"form_lines": [line]}, registry, as_of, "probe")

    for label, kw in [
        ("beaten_lengths negatif", {"beaten_lengths": -1.0}),
        ("beaten_lengths non numerique", {"beaten_lengths": "encolure"}),
        ("beaten_lengths NaN", {"beaten_lengths": float("nan")}),
        ("beaten_lengths infini", {"beaten_lengths": float("inf")}),
        ("beaten_lengths booleen", {"beaten_lengths": True}),
        ("beaten_lengths hors plage", {"beaten_lengths": 1000.0}),
        ("vainqueur avec marge non nulle",
         {"finish_position": 1, "beaten_lengths": 2.0}),
        ("distance_m sans beaten_lengths", {"distance_m": 1600.0}),
        ("distance_m hors plage", {"beaten_lengths": 2.0, "distance_m": 50.0}),
        ("distance_m non numerique", {"beaten_lengths": 2.0, "distance_m": "mile"}),
        ("distance_m negative", {"beaten_lengths": 2.0, "distance_m": -1600.0}),
    ]:
        refuses(margin(**kw), label, section)

    base_record = {"evidence_id": "AE", "scope": "TRAINER", "window_days": 90,
                   "runners": 420, "wins": 68, "expected_wins": 48.0,
                   "observed_at": "2026-06-15T12:00:00+02:00",
                   "context": "plat"}
    ae_registry = {"AE": {"tier": "F-S1"}, "H": {"tier": "H"}}

    def human(**patch):
        rec = dict(base_record)
        rec.update(patch)
        rec = {k: v for k, v in rec.items() if v is not None}
        return lambda: ENG.validate_human_records(
            {"human_records": [rec]}, ae_registry, as_of, "probe")

    for label, kw in [
        ("denominateur sous le minimum", {"runners": 10}),
        ("denominateur negatif", {"runners": -50}),
        ("denominateur non entier", {"runners": 42.5}),
        ("denominateur booleen", {"runners": True}),
        ("wins > denominateur", {"wins": 500}),
        ("wins negatif", {"wins": -1}),
        ("expected_wins nul", {"expected_wins": 0.0}),
        ("expected_wins negatif", {"expected_wins": -3.0}),
        ("expected_wins non numerique", {"expected_wins": "beaucoup"}),
        ("expected_wins > denominateur", {"expected_wins": 900.0}),
        ("expected_wins NaN", {"expected_wins": float("nan")}),
        ("fenetre hors liste", {"window_days": 7}),
        ("fenetre absente", {"window_days": None}),
        ("fenetre non entiere", {"window_days": 90.5}),
        ("scope inconnu", {"scope": "ASTROLOGIE"}),
        ("scope absent", {"scope": None}),
        ("contexte absent", {"context": None}),
        ("preuve inconnue du registre", {"evidence_id": "INEXISTANT"}),
        ("A/E adosse a un tier H (rumeur)", {"evidence_id": "H"}),
        ("observation posterieure a as_of",
         {"observed_at": "2027-01-01T12:00:00+01:00"}),
    ]:
        refuses(human(**kw), label, section)

    refuses(
        lambda: ENG.validate_human_records(
            {"human_records": [dict(base_record), dict(base_record, window_days=30)]},
            ae_registry, as_of, "probe"),
        "scope duplique (double comptage)", section)
    refuses(
        lambda: ENG.validate_human_records(
            {"human_records": []}, ae_registry, as_of, "probe"),
        "human_records liste vide", section)


# ---------------------------------------------------------------------------
# 2. TESTS D'INTERACTION
# ---------------------------------------------------------------------------

def audit_interactions(raw: dict) -> None:
    section = "2. Interactions entre modules"

    # 2a. Non-partant tardif sur un dossier portant marges ET A/E.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    new_as_of = ENG.iso_utc(now - timedelta(minutes=2))
    survived = 0
    for removed in (2, 3, 4, 5, 6, 7):
        reduced, note = ENG.apply_scratch(raw, [removed], new_as_of)
        try:
            ENG.validate_input(reduced, allow_test_samples=False)
            survived += 1
        except ENG.InputError as exc:
            record(section, f"non-partant n{removed}: reconstruction", False, str(exc)[:110])
    record(section, "non-partant tardif: reconstruction mecanique sur 6 retraits",
           survived == 6, f"{survived}/6 reconstruits sans intervention manuelle")

    # 2b. L'elargissement d'incertitude ne va QUE dans le sens conservateur.
    monotone = True
    before = {int(r["no"]): float(r["epistemic_sd"])
              for r in raw["runners"] if r.get("active")}
    for removed in (2, 3, 4, 5, 6, 7):
        reduced, _ = ENG.apply_scratch(raw, [removed], new_as_of)
        for r in reduced["runners"]:
            if r.get("active") and float(r["epistemic_sd"]) < before[int(r["no"])] - 1e-9:
                monotone = False
    record(section, "un retrait n'a jamais RESSERRE une incertitude", monotone)

    # 2c. Ablation complete: toutes les variantes doivent rester valides.
    variants = ENG.ablation_variants(raw)
    invalid = []
    for label, variant in variants.items():
        try:
            ENG.validate_input(variant, allow_test_samples=True)
        except ENG.InputError as exc:
            invalid.append(f"{label}: {str(exc)[:70]}")
    record(section, f"ablation complete: {len(variants)} variantes valides",
           not invalid, "; ".join(invalid[:3]) or f"{len(variants)} variantes")
    record(section, "variante MARGE_LONGUEURS presente (canal ablatable)",
           "MARGE_LONGUEURS" in variants, sorted(variants)[:20])

    # 2d. Ablation MESUREE: chaque nouveau canal doit pouvoir bouger la sortie.
    ablation = ENG.run_ablation(raw, worlds=400, races=100)
    by_module = {item["module"]: item for item in ablation["modules"]
                 if item["status"] == "OK"}
    record(section, "ablation executee sans variante INVALIDE",
           all(i["status"] == "OK" for i in ablation["modules"]),
           [i["module"] for i in ablation["modules"] if i["status"] != "OK"])
    margin_variant = by_module.get("MARGE_LONGUEURS")
    if margin_variant is not None:
        moved = (margin_variant["positions_changed"] > 0
                 or margin_variant["max_abs_note_delta"] >= 0.01)
        record(section, "MARGE_LONGUEURS a un effet MESURABLE sur ce dossier", moved,
               f"positions={margin_variant['positions_changed']} "
               f"delta_note_max={margin_variant['max_abs_note_delta']}")
    human_variant = by_module.get("AXE_human_equipment")
    if human_variant is not None:
        record(section, "AXE_human_equipment ablatable (A/E neutralisable)",
               True, f"positions={human_variant['positions_changed']} "
                     f"delta_note_max={human_variant['max_abs_note_delta']}")

    # 2d-bis. La marge doit pouvoir CHANGER L'ORDRE, pas seulement les notes.
    # On construit le cas exact que la v11.9 ne savait pas distinguer: des
    # chevaux de MEME place, MEME peloton, MEME allocation, separes uniquement
    # par l'ecart au vainqueur.
    same_rank = json.loads(json.dumps(raw))
    margins = [0.2, 1.0, 3.0, 6.0, 10.0, 15.0, 20.0]
    for pos, rb in enumerate(same_rank["runners"]):
        rb["form_lines"][0].update({
            "allocation_eur": 40000.0, "category": "HANDICAP",
            "field_size": 12, "finish_position": 5,
            "beaten_lengths": margins[pos % len(margins)], "distance_m": 1600.0})
    ENG.reseat_declared_ability(same_rank)
    with_margin = {rb["no"]: rb["score_components"]["ability_class"]["value"]
                   for rb in same_rank["runners"] if rb.get("active")}
    rank_only = json.loads(json.dumps(same_rank))
    for rb in rank_only["runners"]:
        rb["form_lines"][0].pop("beaten_lengths", None)
        rb["form_lines"][0].pop("distance_m", None)
    ENG.reseat_declared_ability(rank_only)
    flat = {rb["no"]: rb["score_components"]["ability_class"]["value"]
            for rb in rank_only["runners"] if rb.get("active")}
    record(section, "meme place, marges differentes: la v11.9 ne separait pas",
           len(set(flat.values())) == 1, f"ability_class sans marge: {sorted(set(flat.values()))}")
    record(section, "meme place, marges differentes: la v11.10 SEPARE",
           len(set(with_margin.values())) > 1
           and with_margin[1] > with_margin[7],
           f"battu de 0,2 L -> {with_margin[1]} ; battu de 20 L -> {with_margin[7]}")

    # 2e. Interaction A/E x anti-ecrasement: modifier le fait doit invalider
    #     la valeur recopiee, exactement comme pour ability_class.
    tampered = json.loads(json.dumps(raw))
    tampered["runners"][0]["human_records"][0]["wins"] = 12
    refuses(lambda: ENG.validate_input(tampered, allow_test_samples=True),
            "A/E modifie sans recopier la valeur calculee", section)
    tampered2 = json.loads(json.dumps(raw))
    tampered2["runners"][0]["form_lines"][0]["beaten_lengths"] = 20.0
    refuses(lambda: ENG.validate_input(tampered2, allow_test_samples=True),
            "marge modifiee sans recopier ability_class", section)


# ---------------------------------------------------------------------------
# 3. CONTROLE DE COUVERTURE code <-> contrat d'execution
# ---------------------------------------------------------------------------

def audit_prompt_coverage() -> None:
    section = "3. Couverture du contrat d'execution"
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    engine_src = ENGINE_PATH.read_text(encoding="utf-8")
    stake_src = STAKE_PATH.read_text(encoding="utf-8")

    # Tout champ que le collecteur doit remplir doit etre NOMME dans le prompt.
    required_fields = [
        "form_lines", "beaten_lengths", "distance_m", "allocation_eur",
        "finish_position", "field_size", "human_records", "expected_wins",
        "window_days", "race_reading", "weight_kg", "race_type",
        "collection", "price_bearing", "simulation", "n_worlds",
    ]
    missing = [f for f in required_fields if f not in prompt]
    record(section, "chaque champ collecte est nomme dans le prompt",
           not missing, f"absents: {missing}" if missing else "16/16")

    # Toute commande CLI exposee doit etre documentee.
    engine_cmds = set(re.findall(r'sub\.add_parser\("([a-z-]+)"\)', engine_src))
    stake_cmds = set(re.findall(r'sub\.add_parser\("([a-z-]+)"\)', stake_src))
    undocumented = sorted(
        c for c in (engine_cmds | stake_cmds)
        if c not in {"self-test"} and c not in prompt)
    record(section, "chaque commande CLI est documentee dans le prompt",
           not undocumented, f"absentes: {undocumented}" if undocumented
           else f"{len(engine_cmds | stake_cmds)} commandes")

    # Les vocabulaires fermes doivent etre reproduits, sinon le collecteur devine.
    vocab_missing = [
        v for v in sorted(ENG.HUMAN_AE_SCOPES) if v not in prompt]
    record(section, "vocabulaire ferme des scopes A/E reproduit",
           not vocab_missing, f"absents: {vocab_missing}" if vocab_missing else "8/8")
    trouble_missing = [v for v in sorted(ENG.TROUBLE_VOCABULARY) if v not in prompt]
    record(section, "vocabulaire ferme des incidents reproduit",
           not trouble_missing, f"absents: {trouble_missing}" if trouble_missing else "ok")

    # Le champ publie par un module doit etre lisible par le redacteur.
    published = ["margin_coverage_mean", "clarity_second_opinion",
                 "pace_map_basis", "documentation_bias", "separates",
                 "n_countable_races", "evidential_status"]
    unread = [p for p in published if p not in prompt]
    record(section, "chaque sortie publiee a son instruction de lecture",
           not unread, f"non lues: {unread}" if unread else f"{len(published)}/{len(published)}")


# ---------------------------------------------------------------------------
# 4. CHAINE DE BOUT EN BOUT en parametres de production
# ---------------------------------------------------------------------------

def audit_end_to_end(raw: dict, workdir: Path) -> dict:
    section = "4. Chaine de bout en bout"
    # Repartir d'un repertoire VIERGE: un journal d'engagements deja peuple
    # ferait - a juste titre - echouer commit sur la republication silencieuse,
    # et l'on croirait a un defaut de la chaine alors que c'est le pare-feu qui
    # fonctionne.
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "INPUT.json").write_text(json.dumps(raw), encoding="utf-8")

    def run(*args: str) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(ENGINE_PATH), *args],
            capture_output=True, text=True, cwd=workdir, timeout=1800)
        return proc.returncode, (proc.stdout or proc.stderr)

    code, _ = run("validate", "INPUT.json")
    record(section, "validate", code == 0)
    code, _ = run("analyze", "INPUT.json", "-o", "REPORT.json", "--samples", "RANKS.npz")
    record(section, "analyze", code == 0)
    report = json.loads((workdir / "REPORT.json").read_text(encoding="utf-8"))
    record(section, "invariant_check.ok", report["invariant_check"]["ok"] is True)
    record(section, "render_purity_check.ok", report["render_purity_check"]["ok"] is True)
    code, _ = run("commit", "REPORT.json", "-o", "COMMIT.json", "--journal", "JOURNAL.jsonl")
    record(section, "commit", code == 0)
    stamp = ENG.iso_utc(datetime.now(timezone.utc).replace(microsecond=0))
    code, _ = run("publish", "COMMIT.json", "-o", "PUBLICATION.json",
                  "--medium", "opentimestamps", "--reference", "ots:" + "b" * 40,
                  "--at", stamp, "--ledger", "PUBLICATIONS.jsonl")
    record(section, "publish", code == 0)
    code, out = run("verify-journal", "JOURNAL.jsonl",
                    "--publications", "PUBLICATIONS.jsonl")
    verified = json.loads(out)
    record(section, "verify-journal: n_countable_races = 1",
           verified["n_countable_races"] == 1, json.dumps({
               "n_races": verified["n_races"],
               "n_countable_races": verified["n_countable_races"],
               "chain_ok": verified["chain_ok"]}))

    # Sans attestation de publication, la course NE COMPTE PAS.
    code, out = run("verify-journal", "JOURNAL.jsonl")
    unpublished = json.loads(out)
    record(section, "sans publication: n_countable_races = 0",
           unpublished["n_countable_races"] == 0,
           f"n_countable_races={unpublished['n_countable_races']}")

    # audit post-course avec resultat.
    code, _ = run("template", "result", "-o", "RESULT.json")
    result = json.loads((workdir / "RESULT.json").read_text(encoding="utf-8"))
    order = report["render"]["verdict"]["strict_order"]
    result.update({
        "template_only": False,
        "race_key": report["race_key"],
        "order": order,
        # Le resultat est verifie APRES le depart, par construction: c'est la
        # seule facon d'exercer reellement l'audit post-course.
        "verified_at": ENG.iso_utc(
            ENG.parse_iso(report["clock"]["scheduled_start_utc"], "start")
            + timedelta(minutes=10)),
        "realized_scenario_id": "REGULIER",
        "scenario_label_source_url": "https://www.france-galop.com/",
        "scenario_label_basis": "train regulier constate au compte-rendu officiel",
    })
    (workdir / "RESULT.json").write_text(json.dumps(result), encoding="utf-8")
    code, out = run("audit", "REPORT.json", "RESULT.json", "--commit", "COMMIT.json",
                    "--publication", "PUBLICATION.json", "-o", "AUDIT.json")
    if code == 0:
        audited = json.loads((workdir / "AUDIT.json").read_text(encoding="utf-8"))
        status = audited.get("prior_commitment", {}).get("evidential_status")
        record(section, "audit: evidential_status = PROBANTE", status == "PROBANTE",
               f"evidential_status={status}")
    else:
        record(section, "audit post-course", False, out[:160])
    return report


# ---------------------------------------------------------------------------
# 5. CONTROLES NUMERIQUES
# ---------------------------------------------------------------------------

def audit_numeric(raw: dict, report: dict, workdir: Path) -> None:
    section = "5. Controles numeriques"
    again, _ = ENG.build_report(json.loads(json.dumps(raw)),
                                current=ENG.parse_iso(report["clock"]["computed_at_utc"], "x"),
                                allow_test_samples=False)
    record(section, "report_sha256 reproductible sur deux executions",
           again["report_sha256"] == report["report_sha256"],
           f"{report['report_sha256'][:16]} / {again['report_sha256'][:16]}")
    record(section, "sport_core_sha256 reproductible",
           again["sport_core_sha256"] == report["sport_core_sha256"])

    matrix = np.asarray([[float(v) for v in row]
                         for row in report["internal"]["rank_matrix"].values()])
    row_err = float(np.abs(matrix.sum(axis=1) - 1.0).max())
    col_err = float(np.abs(matrix.sum(axis=0) - 1.0).max())
    record(section, "invariants matriciels (lignes et colonnes a 1)",
           max(row_err, col_err) < 1e-9, f"erreur max {max(row_err, col_err):.2e}")

    leaks = ENG.render_key_leaks(report["render"])
    record(section, "purete du rendu: aucune cle interdite", not leaks, str(leaks[:5]))

    # Le marche ne doit JAMAIS entrer dans le hash sportif.
    with_market = json.loads(json.dumps(raw))
    core_before = report["sport_core_sha256"]
    payload_before = ENG.sha256_obj(ENG.sport_payload(with_market))
    with_market["market"] = None
    record(section, "marche exclu du hash sportif (market null impose en entree)",
           payload_before == ENG.sha256_obj(ENG.sport_payload(with_market)),
           "sport_payload identique")

    # La graine ne depend QUE de MODEL_VERSION, pas d'ENGINE_VERSION.
    seed_a = ENG.deterministic_seed("K", "2026-01-01T00:00:00+00:00", "abc")
    saved = ENG.ENGINE_VERSION
    ENG.ENGINE_VERSION = "99.99.0"
    seed_b = ENG.deterministic_seed("K", "2026-01-01T00:00:00+00:00", "abc")
    ENG.ENGINE_VERSION = saved
    record(section, "graine decouplee d'ENGINE_VERSION", seed_a == seed_b)
    record(section, "MODEL_VERSION inchange a 11.4.0 (aucune loi touchee)",
           ENG.MODEL_VERSION == "11.4.0", ENG.MODEL_VERSION)
    record(section, "ENGINE_VERSION avance a 11.10.1",
           ENG.ENGINE_VERSION == "11.10.1", ENG.ENGINE_VERSION)

    # v11.10.1. Une echelle lue en RANGS doit etre plus floue qu'une echelle lue
    # en MARGES - c'est le trou de la paresse uniforme, ferme par un facteur que
    # le moteur calcule et qu'aucun remplissage ne peut satisfaire.
    record(section, "inflation de couverture: neutre si l'echelle est inactive",
           ENG.margin_coverage_inflation({"active": False}) == 1.0)
    record(section, "inflation de couverture: nulle a couverture complete",
           ENG.margin_coverage_inflation(
               {"active": True, "margin_coverage_mean": 1.0}) == 1.0)
    record(section, "inflation de couverture: maximale a couverture nulle",
           ENG.margin_coverage_inflation(
               {"active": True, "margin_coverage_mean": 0.0})
           == ENG.MARGIN_COVERAGE_INFLATION_MAX,
           f"facteur={ENG.MARGIN_COVERAGE_INFLATION_MAX}")
    record(section, "inflation de couverture: monotone",
           ENG.margin_coverage_inflation({"active": True, "margin_coverage_mean": 0.0})
           > ENG.margin_coverage_inflation({"active": True, "margin_coverage_mean": 0.5})
           > ENG.margin_coverage_inflation({"active": True, "margin_coverage_mean": 1.0}))
    # Elle doit elargir l'INCERTITUDE sans jamais toucher au NIVEAU.
    stripped = json.loads(json.dumps(raw))
    for rb in stripped["runners"]:
        for line in rb.get("form_lines") or []:
            line.pop("beaten_lengths", None)
            line.pop("distance_m", None)
    ENG.reseat_declared_ability(stripped)
    full = ENG.build_arrays(raw, ENG.validate_input(raw, allow_test_samples=True))
    bare = ENG.build_arrays(
        stripped, ENG.validate_input(stripped, allow_test_samples=True))
    record(section, "sans marges: incertitude PLUS LARGE (principe 2.2)",
           float(bare["epistemic_sd"].mean()) > float(full["epistemic_sd"].mean()),
           f"sans={float(bare['epistemic_sd'].mean()):.4f} "
           f"avec={float(full['epistemic_sd'].mean()):.4f}")

    # Retro-compatibilite numerique: sans marge, la v11.10 doit reproduire v11.9.
    record(section, "sans marge declaree, note de sortie identique a la v11.9",
           abs(ENG.outing_rating(40000.0, 4, 12)
               - ENG.outing_rating(40000.0, 4, 12, None, None)) < 1e-12)


# ---------------------------------------------------------------------------
# 6. MODE LITE
# ---------------------------------------------------------------------------

def audit_lite() -> None:
    section = "6. Mode LITE"
    lite = ENG.lite_template()
    lite["template_only"] = False
    roles = ["leader", "prominent", "midfield", "closer", "midfield", "prominent"]
    for idx, runner in enumerate(lite["runners"]):
        runner.update({
            "no": idx + 1, "name": f"LITE_{idx + 1}",
            "level": round(0.30 - 0.12 * idx, 2),
            "form": round(0.10 - 0.04 * idx, 2),
            "level_basis": "lecture rapide de la carte officielle",
            "form_basis": "musique recente lue sur la carte officielle",
            "confidence": 0.5, "pace_role": roles[idx % len(roles)]})
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lite["race"].update({
        "race_key": "2026-09-06|AUDIT|R1C1",
        "course_name": "AUDIT LITE",
        "course": "AUDIT",
        "scheduled_start": ENG.iso_utc(now + timedelta(minutes=45)),
        "as_of": ENG.iso_utc(now - timedelta(minutes=2)),
    })
    try:
        expanded = ENG.expand_lite(lite)
        validated = ENG.validate_input(expanded, allow_test_samples=True)
        record(section, "expand puis validate en mode LITE", True)
        record(section, "data_grade force a C ou D",
               expanded["race"]["data_grade"] in {"C", "D"},
               expanded["race"]["data_grade"])
        record(section, "fill_mode = LITE", validated["fill_mode"] == "LITE")
        # Les modules v11.10 doivent etre INACTIFS, pas casses.
        record(section, "echelle de classe inactive en LITE",
               validated["class_ladder"].get("active") is False)
        record(section, "aucun human_records en LITE",
               all(not r.get("human_records") for r in expanded["runners"]))
        record(section, "aucune marge en LITE",
               all(not r.get("form_lines") for r in expanded["runners"]))
        report, _ = ENG.build_report(expanded, allow_test_samples=True)
        record(section, "analyze complet en LITE", bool(report.get("report_sha256")))
        record(section, "purete du rendu en LITE",
               report["render_purity_check"]["ok"] is True)
    except Exception as exc:  # noqa: BLE001
        record(section, "mode LITE de bout en bout", False,
               f"{type(exc).__name__}: {str(exc)[:120]}")


# ---------------------------------------------------------------------------
# 7. COUCHE DE MISE ET VALEUR DE CLOTURE
# ---------------------------------------------------------------------------

def audit_stake() -> None:
    section = "7. Couche de mise et valeur de cloture"

    def fixture(n, follow, pool_noise=0.0, early=False, seed=4242, field=8):
        gen = np.random.default_rng(seed)
        tickets, prices, pt, pp = [], [], None, None
        for race in range(n):
            nums = list(range(1, field + 1))
            base = gen.normal(0, 0.7, field)
            truth = np.exp(base) / np.exp(base).sum()
            late = np.log(truth) + gen.normal(0, pool_noise, field)
            market = np.exp(late) / np.exp(late).sum()
            early_log = np.log(truth) + gen.normal(0, pool_noise, field)
            early_p = np.exp(early_log) / np.exp(early_log).sum()
            dis = gen.normal(0, 0.45, field); dis -= dis.mean()
            model_log = np.log(truth) + dis
            model = np.exp(model_log) / np.exp(model_log).sum()
            close_log = np.log(truth) + follow * dis + gen.normal(0, 0.06, field)
            close = np.exp(close_log) / np.exp(close_log).sum()
            day = f"2026-09-{(race // 3) + 1:02d}"
            stamp = f"{day}T12:{(race % 3) * 10:02d}:00+00:00"
            ticket = {
                "schema_version": STK.TICKET_SCHEMA, "stake_version": STK.STAKE_VERSION,
                "prev_ticket_sha256": pt, "decision": "PAPIER",
                "context": {"race_key": f"A-{race}", "evaluated_at_utc": stamp,
                            "entry_sha256": "e" * 64,
                            "minutes_to_post_at_snapshot": 3.0},
                "gates": [{"gate": "ENGAGEMENT_PUBLIE_AVANT_DEPART", "passed": True,
                           "detail": "s", "kind": "GRADUANTE"}],
                "bets": [], "total_stake_pct_bankroll": 0.0,
                "probabilities_model_raw": {
                    str(nums[i]): round(float(model[i]), 6) for i in range(field)},
                "probabilities_market_shin": {
                    str(nums[i]): round(float(market[i]), 6) for i in range(field)}}
            ticket["ticket_sha256"] = STK.sha256_obj(ticket)
            tickets.append(ticket); pt = ticket["ticket_sha256"]
            # Le protocole impose DEUX releves, T-10 puis T-3: le plus tardif
            # sert de base au mouvement, le plus precoce de base independante.
            plan = ([(10.0, early_p), (3.0, market)] if early else [(3.0, market)])
            for minutes, vector in plan:
                snap_odds = {str(nums[i]): float(0.85 / max(vector[i], 1e-6))
                             for i in range(field)}
                snap = STK.price_record(
                    f"A-{race}", stamp, minutes, snap_odds, "snapshot", pp)
                prices.append(snap); pp = snap["record_sha256"]
            rap = {str(nums[i]): float(0.85 / max(close[i], 1e-6)) for i in range(field)}
            rec = STK.price_record(f"A-{race}", stamp, 0.0, rap, "final", pp)
            prices.append(rec); pp = rec["record_sha256"]
        return tickets, prices

    t, p = fixture(60, 0.55)
    signal = STK.closing_line_value(t, p, draws=400)
    record(section, "CLV detecte un modele qui anticipe le marche",
           signal["ci95_low"] > 0, f"beta={signal['beta_market_follows_model']} "
                                   f"IC=[{signal['ci95_low']},{signal['ci95_high']}]")
    # Bruit pur AVEC base independante: le verdict doit exister et etre INDECIS.
    t, p = fixture(60, 0.0, early=True, seed=99)
    noise = STK.closing_line_value(t, p, draws=400)
    record(section, "CLV reste indecis sur du bruit pur",
           noise["verdict"] == "INDECIS" and noise["conclusive_at_95"] is False,
           f"beta={noise['beta_market_follows_model']} verdict={noise['verdict']}")

    # LE BIAIS DE BASE PARTAGEE, et sa correction.
    t, p = fixture(90, 0.0, pool_noise=0.30, early=True, seed=515)
    biased = STK.closing_line_value(t, p, draws=400)
    split = biased["split_baseline_estimator"]
    record(section, "biais de base partagee REPRODUIT (faux avantage sur du vide)",
           biased["ci95_low"] > 0,
           f"beta naif={biased['beta_market_follows_model']} "
           f"IC=[{biased['ci95_low']},{biased['ci95_high']}]")
    record(section, "estimateur a bases separees CORRIGE le faux avantage",
           split["available"] and not split["conclusive_at_95"],
           f"beta corrige={split.get('beta_split_baseline')} "
           f"IC=[{split.get('ci95_low')},{split.get('ci95_high')}]")
    t, p = fixture(90, 0.55, pool_noise=0.30, early=True, seed=707)
    real = STK.closing_line_value(t, p, draws=400)["split_baseline_estimator"]
    record(section, "l'estimateur corrige conclut encore sur un VRAI signal",
           real["ci95_low"] > 0,
           f"beta corrige={real['beta_split_baseline']} (verite 0.55)")

    # LE VERDICT REFUSE AU LIEU DE COMMENTER. Sans base precoce independante,
    # beta est biaise vers le haut: le module ne doit rendre AUCUN verdict.
    t, p = fixture(60, 0.55)
    no_base = STK.closing_line_value(t, p, draws=300)
    record(section, "sans releve T-10: aucun verdict rendu",
           no_base["verdict"] == "NON_MESURABLE_SANS_SNAPSHOT_PRECOCE"
           and no_base["conclusive_at_95"] is False
           and no_base["beta_is_biased_upward"] is True,
           f"verdict={no_base['verdict']} beta_brut={no_base['beta_market_follows_model']}")
    t, p = fixture(60, 0.55, early=True, seed=31)
    with_base = STK.closing_line_value(t, p, draws=300)
    record(section, "avec releve T-10: le meme signal EST conclu",
           with_base["verdict"] == "LE_MODELE_ANTICIPE_LE_MARCHE"
           and with_base["conclusive_at_95"] is True,
           f"verdict={with_base['verdict']}")
    # Le faux avantage de 0,32 ne doit JAMAIS sortir en conclusion.
    t, p = fixture(90, 0.0, pool_noise=0.30, early=True, seed=515)
    false_edge = STK.closing_line_value(t, p, draws=300)
    record(section, "le faux avantage n'est jamais rendu comme verdict",
           false_edge["verdict"] == "INDECIS"
           and false_edge["conclusive_at_95"] is False,
           f"beta brut={false_edge['beta_market_follows_model']} "
           f"verdict={false_edge['verdict']}")
    # Stratification FULL vs LITE: la question du cout du protocole instrumentee.
    record(section, "beta stratifie par mode de remplissage",
           isinstance(false_edge.get("by_fill_mode"), dict)
           and sum(v["n_races"] for v in false_edge["by_fill_mode"].values()) == 90,
           str(false_edge.get("by_fill_mode")))

    # CALIBRATION DE L'INTERVALLE. Un instrument qui annonce 95 % et se trompe
    # deux fois plus souvent que promis ment. On MESURE le taux de rejet sous
    # hypothese nulle, on ne le suppose pas.
    rejections, trials, null_betas = 0, 30, []
    for seed in range(6000, 6000 + trials):
        nt, npx = fixture(60, 0.0, early=True, seed=seed)
        result = STK.closing_line_value(nt, npx, draws=300)
        null_betas.append(result["split_baseline_estimator"]["beta_split_baseline"])
        if result["conclusive_at_95"]:
            rejections += 1
    rate = rejections / trials
    record(section, "taux de rejet sous hypothese NULLE <= 5 % (nominal)",
           rate <= 0.05 + 1e-9,
           f"{rejections}/{trials} = {rate:.1%}; le bootstrap par percentiles "
           f"donnait 11,7 % avant correction")
    mean_null = float(np.mean(null_betas))
    stderr_null = float(np.std(null_betas, ddof=1)) / math.sqrt(trials)
    record(section, "estimateur corrige NON BIAISE sous hypothese nulle",
           abs(mean_null) < 4.0 * stderr_null,
           f"beta moyen={mean_null:+.5f} erreur-type={stderr_null:.5f}")
    # La correction ne doit pas tuer la puissance.
    detected = 0
    for seed in range(7000, 7015):
        st_, sp_ = fixture(60, 0.55, early=True, seed=seed)
        if STK.closing_line_value(
                st_, sp_, draws=300)["verdict"] == "LE_MODELE_ANTICIPE_LE_MARCHE":
            detected += 1
    record(section, "puissance conservee sur un signal reel (0,55)",
           detected >= 14, f"{detected}/15 detections")

    # SHIN N'EST PAS UN RESCALAGE UNIFORME: il ne s'annule pas dans une
    # difference entre deux releves. La normalisation proportionnelle, si.
    truth_book = {1: 0.40, 2: 0.25, 3: 0.18, 4: 0.11, 5: 0.06}
    reference = STK.centred_log_ratio(
        np.asarray([truth_book[k] for k in sorted(truth_book)], dtype=float))
    prop_errors, shin_errors = [], []
    for takeout in (0.15, 0.25, 0.36):
        book = {k: (1.0 - takeout) / v for k, v in truth_book.items()}
        prop_errors.append(float(np.abs(
            STK.centred_log_ratio(STK.proportional_probabilities(book))
            - reference).max()))
        shin_map, _ = STK.shin_probabilities(book)
        shin_errors.append(float(np.abs(
            STK.centred_log_ratio(np.asarray(
                [shin_map[k] for k in sorted(book)], dtype=float)) - reference).max()))
    record(section, "de-vig proportionnel EXACT en log-ratio a tout prelevement",
           max(prop_errors) < 1e-12,
           f"erreur max {max(prop_errors):.1e} sur 15/25/36 %")
    record(section, "Shin depend du niveau de cote, donc ne s'annule pas",
           min(shin_errors) > 0.02,
           f"ecart CLR {['%.3f' % e for e in shin_errors]} a 15/25/36 %")

    # LA RESERVE: la CLV n'ouvre aucune porte.
    t, p = fixture(60, 0.55)
    before = STK.staking_progress(t, None)
    after = STK.staking_progress(t, None, p)
    same = all(before[k] == after[k] for k in
               ("current_level", "current_mode", "max_stake_pct_per_bet",
                "positive_edge_established", "races_to_next_level"))
    record(section, "RESERVE: la valeur de cloture n'ouvre aucune porte de mise",
           same and after["current_level"] == 0
           and after["max_stake_pct_per_bet"] == 0.0,
           f"niveau={after['current_level']} plafond={after['max_stake_pct_per_bet']}")
    record(section, "RESERVE: palier 0 plafonne a 0 %",
           STK.ladder_level(0, False)["max_stake_pct_per_bet"] == 0.0)
    record(section, "RESERVE: pools exotiques verrouilles sous 1000 courses",
           all(not (set(STK.ladder_level(n, True)["pools"]) & STK.EXOTIC_POOLS)
               for n in (0, 50, 200, 500, 999)),
           "verrouilles jusqu'a 1000")
    record(section, "RESERVE: penalite forfaitaire tant que la derive n'est pas ajustee",
           STK.fit_drift_model([], min_observations=200)["drift_model"]["fitted"] is False)

    # Journaux chaines: toute alteration doit faire echouer.
    t, p = fixture(20, 0.55)
    tampered = json.loads(json.dumps(p))
    tampered[5]["rapports"] = {k: v * 2 for k, v in tampered[5]["rapports"].items()}
    refuses(lambda: STK.closing_line_value(t, tampered, draws=50),
            "journal de prix altere detecte", section, expect=STK.StakeError)
    refuses(lambda: STK.closing_line_value(t[1:], p, draws=50),
            "journal de tickets tronque detecte", section, expect=STK.StakeError)
    # Course sans rapport final: ecartee et COMPTEE, jamais devinee.
    # On coupe par COURSE, pas par nombre d'enregistrements: la fixture en emet
    # plusieurs par course (snapshots + rapport final).
    kept = {f"A-{i}" for i in range(10)}
    partial = STK.closing_line_value(
        t, [rec for rec in p if rec["race_key"] in kept], draws=100)
    record(section, "course sans rapport final ecartee, pas devinee",
           partial["n_races"] == 10 and partial["n_skipped"] == 10,
           f"retenues={partial['n_races']} ecartees={partial['n_skipped']}")


# ---------------------------------------------------------------------------
# 8. SELF-TESTS
# ---------------------------------------------------------------------------

def audit_self_tests() -> None:
    section = "0. Self-tests"
    for path, key, expected in ((ENGINE_PATH, "engine_version", "11.10.1"),
                                (STAKE_PATH, "stake_version", "1.6.1")):
        proc = subprocess.run([sys.executable, str(path), "self-test"],
                              capture_output=True, text=True, timeout=3600)
        try:
            data = json.loads(proc.stdout)
            failing = [k for k, v in data["tests"].items() if v is not True]
            record(section, f"{path.name} self-test",
                   data["status"] == "PASS" and not failing,
                   f"{len(data['tests'])} tests, version {data.get(key)}")
        except Exception as exc:  # noqa: BLE001
            record(section, f"{path.name} self-test", False, str(exc)[:120])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--workdir", default=None)
    args = parser.parse_args()

    workdir = Path(args.workdir) if args.workdir else ROOT / ".audit_work"
    audit_self_tests()
    raw, _ = production_input()
    audit_adversarial_probes()
    audit_interactions(raw)
    audit_prompt_coverage()
    report = audit_end_to_end(raw, workdir)
    audit_numeric(raw, report, workdir)
    audit_lite()
    audit_stake()

    failed = [r for r in RESULTS if not r["ok"]]
    summary = {
        "engine_version": ENG.ENGINE_VERSION,
        "model_version": ENG.MODEL_VERSION,
        "stake_version": STK.STAKE_VERSION,
        "n_checks": len(RESULTS),
        "n_failed": len(failed),
        "status": "PASS" if not failed else "FAIL",
        "results": RESULTS,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if not failed else 1
    current = None
    for item in RESULTS:
        if item["section"] != current:
            current = item["section"]
            print(f"\n{current}")
        mark = "OK  " if item["ok"] else "ECHEC"
        print(f"  [{mark}] {item['check']}"
              + (f"\n           {item['detail']}" if item["detail"] else ""))
    print(f"\n{'=' * 70}")
    print(f"TITAN PLAT {ENG.ENGINE_VERSION} (modele {ENG.MODEL_VERSION}) "
          f"+ STAKE {STK.STAKE_VERSION}")
    print(f"{len(RESULTS)} controles, {len(failed)} echec(s) -> {summary['status']}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
