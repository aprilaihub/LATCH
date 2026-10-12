"""WP015 conditioned planning messages, independent of provider transport.

Objective/prompt/context and model feedback follow WP015 at 32d852a.
New trial identifiers describe new runs; they do not assert historical replay.
"""
from __future__ import annotations

from ..protocol.contract_v6 import PROTOCOL_EPOCH, contract_for_cell, prompt_for_cell
from ..protocol.specs import canonical_json_bytes
from .prepared import CONDITIONS
from .wp015_feedback import model_feedback


def conditioned_job(session, condition):
    level, language_condition = CONDITIONS[condition]
    return {
        "track": "C_CONDITIONED", "condition": language_condition,
        "language_condition": language_condition, "agent_level": level,
        "coverage_regime": "FULL_M10", "strict_g_required": condition == "constructed",
        "strict_mechanism": session.required_family,
        "job_id": session.observation.payload["root_pseudonym"] + ":" + condition,
        "root_pseudonym": session.observation.payload["root_pseudonym"],
    }


def objective(job):
    if job["track"] != "C_CONDITIONED":
        return "Choose freely among the languages admitted at this neutral level."
    if job["condition"] == "M_ONLY":
        return "Return one direct M-language COMMIT using one admitted Macro."
    if job["condition"] == "P_BOUNDARY":
        return "Return one P-exclusive COMMIT; inherited CALL_MACRO alone is insufficient."
    mechanism = job.get("strict_mechanism")
    suffix = ""
    if mechanism:
        suffix = " The required sufficient mechanism family is " + str(mechanism) + "."
    return (
        "Return one G COMMIT matching a frozen sufficient strict-G family; "
        "a merely G-labelled or P-reachable program is insufficient." + suffix
    )


def planning_messages(*, session, condition, previous_result):
    job = conditioned_job(session, condition)
    admitted = tuple("macro." + key for key in session.compile_context.admitted_macros)
    contract = contract_for_cell(study_track=job["track"], coverage_regime=job["coverage_regime"],
        language_condition=job["language_condition"], level="C", admitted_action_keys=admitted)
    # Preserve even the original prompt's wording: changing prompts is not a
    # presentation-only cleanup. Execution provenance remains a new trial.
    prompt = prompt_for_cell(study_track=job["track"], language_condition=job["language_condition"],
                             level=job["agent_level"])
    prompt += b" WP015 frozen single-repeat trial. " + objective(job).encode()
    context = {
        "schema_version": "dof.wp015-planning-context.v1",
        "protocol_id": "WP015_LEAN_R1_V3", "protocol_epoch": PROTOCOL_EPOCH,
        "job_id": job["job_id"], "root_pseudonym": job["root_pseudonym"],
        "study_track": job["track"], "coverage_regime": job["coverage_regime"],
        "language_condition": job["language_condition"],
        "strict_g_required": bool(job["strict_g_required"]),
        "required_strict_mechanism": job["strict_mechanism"],
        "objective": objective(job), "common_objective": contract["common_objective"],
        "common_observation": dict(session.observation.payload),
        "common_observation_sha256": session.observation.sha256,
        "common_query_catalogue": session.queries.catalogue,
        "language_contract": contract, **session.public_status(),
        "previous_result": model_feedback(previous_result,
            source_constructs=getattr(session, "_feedback_constructs", None)),
        "qor_feedback_present": False,
        "private_reasoning_requested_or_persisted": False,
    }
    return [
        {"role": "system", "content": prompt.decode()},
        {"role": "user", "content": canonical_json_bytes(context).decode()},
    ]
