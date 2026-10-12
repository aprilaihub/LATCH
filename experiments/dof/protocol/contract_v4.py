"""WP-014 dual-track eligibility overlay.

The accepted v3 action semantics remain the only compiler truth.  This module
only narrows which final representation may be committed and records the
track/regime identity used by Dual-Track DoF Protocol v1.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any, Mapping

from .contract_v3 import (
    COMMON_OBJECTIVE,
    M_ACTION_KEYS,
    PLANNER_MESSAGE_V3,
    PLANNING_CONTEXT_V22,
    PROMPT_VERSION_V22,
    compile_candidate_v3,
    contract_for_level,
)
from .specs import canonical_json_bytes


PROTOCOL_EPOCH = "dof.dual-track.v1"
TRACKS = ("N_NEUTRAL", "C_CONDITIONED")
COVERAGE_REGIMES = ("FULL_M8", "REDUCED_M_STRESS")
LANGUAGE_CONDITIONS = ("FREE_CHOICE", "M_ONLY", "P_EXCLUSIVE", "G_EXCLUSIVE")
PLANNER_MESSAGE_V4 = "dof.single-submission.planner-message.v4"
CONDITION_CONTRACT_VERSION = "dof.dual-track.language-condition.v1"


def neutral_prompt(level: str) -> bytes:
    if level not in {"L2", "L3", "L4"}:
        raise ValueError("neutral_prompt_level_invalid")
    return (
        "Return exactly one JSON object and no prose or markdown. Follow the exact closed "
        "schemas and semantic cards in language_contract. " + COMMON_OBJECTIVE + " "
        "QUERY is {\"kind\":\"QUERY\",\"queries\":[{\"name\":QUERY_NAME,\"top_k\":INTEGER}]}. "
        "Candidate messages are {\"kind\":\"DRAFT\"|\"VALIDATE\"|\"COMMIT\",\"candidate\":CANDIDATE}. "
        "Terminal messages are {\"kind\":\"STOP\"} or {\"kind\":\"NO_VALID_SUBMISSION\"}. "
        "On final_submission_turn only use an allowed kind. Never include system-owned fields. "
        "Do not use paths, object names, Tcl, shell, files, network, Python, loops, retries, or hidden calls. "
        "Construction feedback may be repaired before COMMIT; no QoR is available before COMMIT. "
        f"The active level is {level}."
    ).encode()


def prompt_for_cell(*, study_track: str, language_condition: str, level: str) -> bytes:
    if study_track == "N_NEUTRAL":
        if language_condition != "FREE_CHOICE":
            raise ValueError("neutral_cell_invalid")
        return neutral_prompt(level)
    if study_track != "C_CONDITIONED" or language_condition == "FREE_CHOICE":
        raise ValueError("conditioned_cell_invalid")
    base_level = {"M_ONLY": "L2", "P_EXCLUSIVE": "L3", "G_EXCLUSIVE": "L4"}[
        language_condition
    ]
    eligibility = {
        "M_ONLY": "Eligibility rule: the final COMMIT must use M_ONLY exactly as defined in language_contract.",
        "P_EXCLUSIVE": "Eligibility rule: the final COMMIT must use P_EXCLUSIVE exactly as defined in language_contract.",
        "G_EXCLUSIVE": "Eligibility rule: the final COMMIT must use G_EXCLUSIVE exactly as defined in language_contract.",
    }[language_condition]
    return neutral_prompt(base_level) + b" " + eligibility.encode()


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
        if language_condition != "FREE_CHOICE" or level not in {"L2", "L3", "L4"}:
            raise ValueError("neutral_cell_invalid")
    elif language_condition == "FREE_CHOICE" or level != "C":
        raise ValueError("conditioned_cell_invalid")


def _without_macro_p(contract: dict[str, Any]) -> None:
    cards = contract["P"]["cards"]
    contract["P"]["cards"] = [row for row in cards if row["key"] != "CALL_MACRO"]
    items = contract["P"]["program_schema"]["properties"]["steps"]["items"]
    items["properties"]["op"]["enum"] = [
        key for key in items["properties"]["op"]["enum"] if key != "CALL_MACRO"
    ]


def contract_for_cell(
    *,
    study_track: str,
    coverage_regime: str,
    language_condition: str,
    level: str,
    admitted_action_keys: tuple[str, ...] = M_ACTION_KEYS,
) -> dict[str, Any]:
    """Return the public contract for one exact protocol cell.

    Neutral cells are byte-identical to Protocol v2.2's v3 contract.  A
    conditioned contract is a strict projection of that same contract; no
    action, schema or semantic card is added or reworded.
    """

    validate_protocol_cell(
        study_track=study_track,
        coverage_regime=coverage_regime,
        language_condition=language_condition,
        level=level,
    )
    if any(key not in M_ACTION_KEYS for key in admitted_action_keys):
        raise ValueError("unknown_admitted_action_key")
    if study_track == "N_NEUTRAL":
        return contract_for_level(level, admitted_action_keys=admitted_action_keys)

    source_level = {"M_ONLY": "L2", "P_EXCLUSIVE": "L3", "G_EXCLUSIVE": "L4"}[
        language_condition
    ]
    contract = deepcopy(contract_for_level(source_level, admitted_action_keys=admitted_action_keys))
    contract["schema_version"] = CONDITION_CONTRACT_VERSION
    contract["level"] = "C"
    contract["language_condition"] = language_condition
    contract["eligibility_rule"] = {
        "M_ONLY": "COMMIT exactly one admitted M action; P and G are ineligible.",
        "P_EXCLUSIVE": "COMMIT a P program containing at least one non-CALL_MACRO P operation; M, G and CALL_MACRO are ineligible.",
        "G_EXCLUSIVE": "COMMIT a G program containing at least one genuinely G-only node or generated target-specific parameterisation; M and CALL_MACRO are ineligible.",
    }[language_condition]
    contract["allowed_languages"] = [{"M_ONLY": "M", "P_EXCLUSIVE": "P", "G_EXCLUSIVE": "G"}[language_condition]]

    if language_condition == "M_ONLY":
        contract["examples"] = {"M": contract["examples"]["M"]}
        return contract

    contract.pop("M", None)
    contract["examples"].pop("M", None)
    _without_macro_p(contract)
    if language_condition == "P_EXCLUSIVE":
        contract["examples"] = {"P_EXCLUSIVE": contract["examples"]["P_EXCLUSIVE"]}
        return contract

    # G inherits the safe P cards for supporting steps, but neither language
    # exposes CALL_MACRO.  Only G is an eligible final representation.
    contract["examples"].pop("P_EXCLUSIVE", None)
    g_items = contract["G"]["program_schema"]["properties"]["statements"]["items"]
    g_items["properties"]["node"]["enum"] = [
        key for key in g_items["properties"]["node"]["enum"] if key != "CALL_MACRO"
    ]
    return contract


def contract_hash_for_cell(**kwargs: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(contract_for_cell(**kwargs))).hexdigest()


def language_condition_error(candidate: Mapping[str, Any], condition: str) -> str | None:
    if condition == "FREE_CHOICE":
        return None
    expected = {"M_ONLY": "M", "P_EXCLUSIVE": "P", "G_EXCLUSIVE": "G"}[condition]
    if candidate.get("language") != expected:
        return "LANGUAGE_CONDITION_NOT_MET"
    if condition == "M_ONLY":
        return None
    if condition == "P_EXCLUSIVE":
        steps = candidate.get("steps")
        if not isinstance(steps, list) or not any(
            isinstance(row, Mapping) and row.get("op") != "CALL_MACRO" for row in steps
        ):
            return "LANGUAGE_CONDITION_NOT_MET"
        if any(isinstance(row, Mapping) and row.get("op") == "CALL_MACRO" for row in steps):
            return "LANGUAGE_CONDITION_NOT_MET"
        return None
    statements = candidate.get("statements")
    if not isinstance(statements, list):
        return "LANGUAGE_CONDITION_NOT_MET"
    for row in statements:
        if not isinstance(row, Mapping):
            continue
        if row.get("node") == "CALL_MACRO":
            return "LANGUAGE_CONDITION_NOT_MET"
        if row.get("node") == "P_PROGRAM":
            args = row.get("args")
            program = args.get("program") if isinstance(args, Mapping) else None
            steps = program.get("steps") if isinstance(program, Mapping) else None
            if isinstance(steps, list) and any(
                isinstance(step, Mapping) and step.get("op") == "CALL_MACRO"
                for step in steps
            ):
                return "LANGUAGE_CONDITION_NOT_MET"
    for row in statements:
        if not isinstance(row, Mapping):
            continue
        node, args = row.get("node"), row.get("args")
        if node in {"SELECT_WHERE", "CREATE_REGION", "ASSIGN_REGION"}:
            return None
        if node == "RELOCATE_HARDBLOCK" and isinstance(args, Mapping) and {
            "candidate_sites", "site_index"
        } <= set(args):
            return None
    return "LANGUAGE_CONDITION_NOT_MET"


__all__ = [
    "CONDITION_CONTRACT_VERSION", "COVERAGE_REGIMES", "LANGUAGE_CONDITIONS",
    "PLANNER_MESSAGE_V4", "PLANNER_MESSAGE_V3", "PLANNING_CONTEXT_V22",
    "PROMPT_VERSION_V22", "PROTOCOL_EPOCH", "TRACKS", "COMMON_OBJECTIVE",
    "compile_candidate_v3", "contract_for_cell", "contract_hash_for_cell",
    "language_condition_error", "neutral_prompt", "prompt_for_cell",
    "validate_protocol_cell",
]
