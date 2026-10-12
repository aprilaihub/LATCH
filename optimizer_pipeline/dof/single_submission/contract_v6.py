"""Dual-Track DoF Protocol v3 overlay for Action Surface v3."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any, Mapping

from .action_surface_v3 import G_EXCLUSIVE_NODES, M_ACTION_KEYS, P_EXCLUSIVE_OPERATIONS
from .contract_v5 import (
    COMMON_OBJECTIVE,
    PLANNER_MESSAGE_V5,
    PLANNING_CONTEXT_V22,
    PROMPT_VERSION_V3,
    compile_candidate_v5,
    contract_for_level,
)
from .specs import canonical_json_bytes


PROTOCOL_EPOCH = "dof.dual-track.v3"
TRACKS = ("N_NEUTRAL", "S_NEUTRAL_REDUCED", "C_CONDITIONED")
COVERAGE_REGIMES = ("FULL_M8", "FULL_M10", "REDUCED_M_STRESS")
LANGUAGE_CONDITIONS = ("FREE_CHOICE", "M_ONLY", "P_BOUNDARY", "G_BOUNDARY")
PLANNER_MESSAGE_V6 = "dof.single-submission.planner-message.v6"
CONDITION_CONTRACT_VERSION = "dof.dual-track.language-condition.v3"


def neutral_prompt(level: str) -> bytes:
    if level not in {"L2", "L3", "L4"}:
        raise ValueError("neutral_prompt_level_invalid")
    return (
        "Return exactly one JSON object and no prose or markdown. Follow the exact closed "
        "schemas and semantic cards in language_contract. " + COMMON_OBJECTIVE + " "
        "QUERY is {\"kind\":\"QUERY\",\"queries\":[{\"name\":QUERY_NAME,\"top_k\":INTEGER}]}. "
        "Candidate messages are {\"kind\":\"DRAFT\"|\"VALIDATE\"|\"COMMIT\",\"candidate\":CANDIDATE}. "
        "Terminal messages are {\"kind\":\"STOP\"} or {\"kind\":\"NO_VALID_SUBMISSION\"}. "
        "Never include system-owned fields, paths, object names, Tcl, shell, files, network, Python, loops, retries, or hidden calls. "
        "Construction feedback may be repaired before COMMIT; no QoR is available before COMMIT. "
        f"The active level is {level}."
    ).encode()


def prompt_for_cell(*, study_track: str, language_condition: str, level: str) -> bytes:
    if study_track in {"N_NEUTRAL", "S_NEUTRAL_REDUCED"}:
        if language_condition != "FREE_CHOICE":
            raise ValueError("neutral_cell_invalid")
        return neutral_prompt(level)
    if study_track != "C_CONDITIONED" or language_condition == "FREE_CHOICE":
        raise ValueError("conditioned_cell_invalid")
    base_level = {"M_ONLY": "L2", "P_BOUNDARY": "L3", "G_BOUNDARY": "L4"}[language_condition]
    rule = {
        "M_ONLY": "The final COMMIT must be an M candidate.",
        "P_BOUNDARY": "The final COMMIT must be a P program containing at least one P-exclusive operation; inherited CALL_MACRO is permitted.",
        "G_BOUNDARY": "The final COMMIT must be a G program containing at least one G-exclusive generated node; inherited P_PROGRAM and M are permitted.",
    }[language_condition]
    return neutral_prompt(base_level) + b" Eligibility rule: " + rule.encode()


def validate_protocol_cell(
    *, study_track: str, coverage_regime: str, language_condition: str, level: str
) -> None:
    if study_track not in TRACKS:
        raise ValueError("study_track_invalid")
    if coverage_regime not in COVERAGE_REGIMES:
        raise ValueError("coverage_regime_invalid")
    if language_condition not in LANGUAGE_CONDITIONS:
        raise ValueError("language_condition_invalid")
    if study_track == "N_NEUTRAL":
        if coverage_regime not in {"FULL_M8", "FULL_M10"} or language_condition != "FREE_CHOICE" or level not in {"L2", "L3", "L4"}:
            raise ValueError("neutral_full_cell_invalid")
    elif study_track == "S_NEUTRAL_REDUCED":
        if coverage_regime != "REDUCED_M_STRESS" or language_condition != "FREE_CHOICE" or level not in {"L2", "L3", "L4"}:
            raise ValueError("neutral_stress_cell_invalid")
    elif language_condition == "FREE_CHOICE" or level != "C":
        raise ValueError("conditioned_cell_invalid")


def contract_for_cell(
    *,
    study_track: str,
    coverage_regime: str,
    language_condition: str,
    level: str,
    admitted_action_keys: tuple[str, ...] = M_ACTION_KEYS,
) -> dict[str, Any]:
    validate_protocol_cell(
        study_track=study_track,
        coverage_regime=coverage_regime,
        language_condition=language_condition,
        level=level,
    )
    if study_track in {"N_NEUTRAL", "S_NEUTRAL_REDUCED"}:
        return contract_for_level(level, admitted_action_keys=admitted_action_keys)
    source_level = {"M_ONLY": "L2", "P_BOUNDARY": "L3", "G_BOUNDARY": "L4"}[language_condition]
    value = deepcopy(contract_for_level(source_level, admitted_action_keys=admitted_action_keys))
    value["schema_version"] = CONDITION_CONTRACT_VERSION
    value["level"] = "C"
    value["language_condition"] = language_condition
    value["allowed_languages"] = [{"M_ONLY": "M", "P_BOUNDARY": "P", "G_BOUNDARY": "G"}[language_condition]]
    value["eligibility_rule"] = {
        "M_ONLY": "candidate language is M",
        "P_BOUNDARY": "candidate language is P and at least one step is P-exclusive; inherited CALL_MACRO is permitted",
        "G_BOUNDARY": "candidate language is G and at least one statement is G-exclusive; inherited P_PROGRAM/M is permitted",
    }[language_condition]
    return value


def contract_hash_for_cell(**kwargs: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(contract_for_cell(**kwargs))).hexdigest()


def language_condition_error(candidate: Mapping[str, Any], condition: str) -> str | None:
    if condition == "FREE_CHOICE":
        return None
    if condition not in {"M_ONLY", "P_BOUNDARY", "G_BOUNDARY"}:
        raise ValueError("language_condition_invalid")
    expected = {"M_ONLY": "M", "P_BOUNDARY": "P", "G_BOUNDARY": "G"}[condition]
    if candidate.get("language") != expected:
        return "LANGUAGE_CONDITION_NOT_MET"
    if condition == "M_ONLY":
        return None
    rows = candidate.get("steps")
    if not isinstance(rows, list):
        return "LANGUAGE_CONDITION_NOT_MET"
    field = "op"
    boundary = P_EXCLUSIVE_OPERATIONS if condition == "P_BOUNDARY" else G_EXCLUSIVE_NODES
    return None if any(isinstance(row, Mapping) and row.get(field) in boundary for row in rows) else "LANGUAGE_CONDITION_NOT_MET"


__all__ = [
    "CONDITION_CONTRACT_VERSION", "COVERAGE_REGIMES", "LANGUAGE_CONDITIONS",
    "PLANNER_MESSAGE_V6", "PLANNER_MESSAGE_V5", "PLANNING_CONTEXT_V22",
    "PROMPT_VERSION_V3", "PROTOCOL_EPOCH", "TRACKS", "COMMON_OBJECTIVE",
    "compile_candidate_v5", "contract_for_cell", "contract_hash_for_cell",
    "language_condition_error", "neutral_prompt", "prompt_for_cell",
    "validate_protocol_cell",
]
