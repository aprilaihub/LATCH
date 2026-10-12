"""Study-owned sampled-validation descriptions, not execution or aggregation."""
from __future__ import annotations

from collections.abc import Mapping


# Actual WP015: 32d852a scripts/finalize_wp015_cell_validation.py, SEEDS and
# functional plan. Matching these fields is not full protocol certification.
WP015_SEEDS = (0xC0FFEE, 0x5EED1234, 0x1BADB002, 0xD15EA5E, 0xA11CE55)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _completion(plan, functional, identities, cycles, precheck):
    if functional.get("status") == "NOT_RUN" or plan is None:
        return None
    if identities is False or cycles is False or precheck is False:
        return False
    return None if precheck is None else True


def _display(functional, complete, reports_passed, profile_match):
    if functional.get("status") == "NOT_RUN":
        return "NOT_RUN"
    if functional.get("passed") is True:
        if complete is True and reports_passed:
            return "WP015_PROFILE_SAMPLED_PASS" if profile_match else "CUSTOM_SAMPLED_PASS"
        return "CONFIGURED_PASS_EXECUTION_UNVERIFIED"
    return "CONFIGURED_VALIDATION_FAILED" if functional.get("passed") is False else "CONFIGURED_VALIDATION_INCOMPLETE"


def public_validation_plan(config):
    """Copy only nonsecret seed/vector parameters, never unknown config keys."""
    if not isinstance(config, Mapping):
        return None
    seeds = config.get("seeds")
    if (not isinstance(seeds, list) or not seeds
            or any(type(seed) is not int or not 0 <= seed <= 0xffffffff for seed in seeds)
            or any(type(config.get(key)) is not int or config[key] <= 0
                   for key in ("vectors", "precheck_vectors"))):
        return None
    return {"seeds": list(seeds), "vectors": config["vectors"],
            "precheck_vectors": config["precheck_vectors"]}


def describe_validation(plan, functional, *, preparation=None):
    """Qualify legacy status using configured strength and observed completion.

    The existing aggregate status/passed and scientific delta are not changed.
    Missing execution evidence remains unknown, not assumed successful.
    """
    plan = public_validation_plan(plan)
    mismatch = None if plan is None else [
        key for key, matches in (
            ("seeds", len(plan["seeds"]) == len(WP015_SEEDS)
             and set(plan["seeds"]) == set(WP015_SEEDS)),
            ("vectors", plan["vectors"] == 200),
            ("precheck_vectors", plan["precheck_vectors"] == 100),
        ) if not matches
    ]
    profile_match = None if mismatch is None else not mismatch
    rows = functional.get("seed_reports") or []
    rows = [row if isinstance(row, Mapping) else {} for row in rows] if isinstance(rows, list) else []
    identities_complete = None if plan is None else (
        [row.get("stimulus_seed") for row in rows] == plan["seeds"])
    cycles_complete = None if plan is None else (
        identities_complete and all(type(row.get("cycles_simulated")) is int
                                    and row["cycles_simulated"] == plan["vectors"]
                                    and row.get("vectors_requested", plan["vectors"]) == plan["vectors"]
                                    and row.get("infrastructure_failure") is False
                                    and not row.get("simulator_failed")
                                    for row in rows))
    reports_support_pass = bool(rows) and all(row.get("passed") is True
        and row.get("mismatch_count") == 0 and row.get("protocol_mismatch_count") == 0
        and not row.get("result_fail_seen") and not row.get("simulator_failed")
        and row.get("infrastructure_failure") is False for row in rows)
    preparation = preparation if isinstance(preparation, Mapping) else {}
    simulation = preparation.get("simulation_report") or {}
    simulation = simulation if isinstance(simulation, Mapping) else {}
    precheck = simulation.get("precheck_report") or {}
    precheck = precheck if isinstance(precheck, Mapping) else {}
    precheck_expected = None if plan is None else plan["precheck_vectors"] < plan["vectors"]
    if plan is None or not simulation:
        precheck_complete = None
    elif not precheck_expected:
        precheck_complete = simulation.get("precheck_vectors") is None
    elif simulation.get("precheck_vectors") is None or precheck.get("cycles_simulated") is None:
        precheck_complete = None
    else:
        precheck_complete = (simulation.get("precheck_vectors") == plan["precheck_vectors"]
            and type(precheck.get("cycles_simulated")) is int
            and precheck["cycles_simulated"] == plan["precheck_vectors"]
            and simulation.get("precheck_passed") is True and precheck.get("passed") is True
            and precheck.get("infrastructure_failure") is False
            and precheck.get("mismatch_count") == 0 and precheck.get("protocol_mismatch_count") == 0
            and not precheck.get("result_fail_seen") and not precheck.get("simulator_failed"))
    complete = _completion(plan, functional, identities_complete, cycles_complete, precheck_complete)
    passed = functional.get("passed")
    display = _display(functional, complete, reports_support_pass, profile_match)
    return {
        "display_status": display, "configured_validation_passed": passed,
        "wp015_profile_match": profile_match, "wp015_profile_mismatch_fields": mismatch,
        "wp015_profile_sampled_pass": profile_match is True and passed is True and complete is True and reports_support_pass,
        "execution_complete": complete,
        "seed_identities_complete": identities_complete, "seed_vectors_complete": cycles_complete,
        "seed_reports_support_pass": reports_support_pass,
        "precheck_execution_expected": precheck_expected,
        "precheck_complete": precheck_complete,
        "precheck_vectors_reported": _count(simulation.get("precheck_vectors")),
        "precheck_cycles_simulated": _count(precheck.get("cycles_simulated")),
        "reported_seed_count": len(rows),
        "profile_comparison_scope": "seed identities and vector counts only; not full protocol or formal equivalence",
    }


def public_validation_scope(facts):
    """Safe paired description for public consumers, including older results."""
    plan = public_validation_plan(facts.get("validation_plan"))
    functional = facts.get("functional_validation") or {"status": "NOT_RUN", "passed": None}
    supplied = facts.get("validation_assessment")
    scope = describe_validation(plan, functional)
    if isinstance(supplied, Mapping):
        # Do not expose arbitrary adapter/config/report fields in summaries.
        for key in ("precheck_complete",):
            if supplied.get(key) is None or type(supplied.get(key)) is bool:
                scope[key] = supplied.get(key)
        for key in ("precheck_vectors_reported", "precheck_cycles_simulated"):
            if supplied.get(key) is None or (type(supplied.get(key)) is int and supplied[key] >= 0):
                scope[key] = supplied.get(key)
        # The writer's precheck flag must still have the recorded count
        # evidence. Configuration and seed completion are derived afresh.
        if plan and scope["precheck_execution_expected"] and scope["precheck_complete"] is True:
            if (scope["precheck_vectors_reported"] != plan["precheck_vectors"]
                    or scope["precheck_cycles_simulated"] != plan["precheck_vectors"]):
                scope["precheck_complete"] = None
        scope["execution_complete"] = _completion(plan, functional,
            scope["seed_identities_complete"], scope["seed_vectors_complete"], scope["precheck_complete"])
        scope["display_status"] = _display(functional, scope["execution_complete"],
            scope["seed_reports_support_pass"], scope["wp015_profile_match"])
        scope["wp015_profile_sampled_pass"] = (scope["wp015_profile_match"] is True
            and functional.get("passed") is True and scope["execution_complete"] is True
            and scope["seed_reports_support_pass"])
    return {"validation_plan": plan, "validation_assessment": scope}
