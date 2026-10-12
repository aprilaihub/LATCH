#!/usr/bin/env python3
"""Bounded eight-session planning/repair usability pilot for WP-012-02."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from optimizer_pipeline.dof.action_language_probe import (
    ActionLanguageProbeError,
    PROBE_IDENTITY,
    validate_g_ast,
    validate_p_program,
)
from optimizer_pipeline.dof.contracts import DoFLevel
from optimizer_pipeline.dof.mutation_worker_binding import verified_fake_attestation_evidence_sha256
from optimizer_pipeline.dof.openrouter import (
    OPENROUTER_ADAPTER_VERSION,
    OPENROUTER_E1_PROFILES,
    OpenRouterChatCompletionsTransport,
    build_openrouter_outbound,
    discover_openrouter_endpoints,
    discover_openrouter_models,
)
from optimizer_pipeline.dof.provider import (
    DoFProviderObserver,
    ProviderRequestBinding,
    ProviderRunLedger,
    ProviderSettings,
    verify_provider_attempts,
)
from optimizer_pipeline.dof.provenance import observe_repository
from optimizer_pipeline.dof.real_mutation import (
    build_e0_dispatch_authorization,
    execute_e0_real_gated_plan,
    freeze_e0_real_gated_plan,
)


PROTOCOL = "ACTION_LANGUAGE_DESIGN_PROBE_V1_PLANNING"
SCHEMA = "dof.wp012-02-planning-pilot.v1"
MODELS = ("openai/gpt-5.6-luna", "anthropic/claude-sonnet-5")
LANGUAGES = ("P", "G_AST")
ROOT_HASHES = {
    "vexriscv_re-place": "8baf6451f9b20b8a82f035d14187c7350529a6ea0330e02d93c7100b19753149",
    "external_compatibility_a": "7fe49fa4a73f43cb20b57fff9c92ecfb2d6a771372b0229b7fa5199fa86c01e1",
}
MAX_TURNS = 3
MAX_FEEDBACK = 2
COST_CAP_USD = 1.50
RESERVE = {"openai/gpt-5.6-luna": 0.05, "anthropic/claude-sonnet-5": 0.15}

P_ACTIONS = {
    "probe.p.call_macro": "CALL_MACRO",
    "probe.p.unroute_all": "UNROUTE_ALL",
    "probe.p.unplace_all": "UNPLACE_ALL",
    "probe.p.place": "PLACE",
    "probe.p.route": "ROUTE",
    "probe.p.phys_opt": "PHYS_OPT",
    "probe.p.select_target": "SELECT_TARGET",
    "probe.p.create_standard_pblock": "CREATE_STANDARD_PBLOCK",
}
G_ACTIONS = {
    "probe.g.select": "SELECT",
    "probe.g.create_pblock": "CREATE_PBLOCK",
    "probe.g.unroute_all": "UNROUTE_ALL",
    "probe.g.unplace_all": "UNPLACE_ALL",
    "probe.g.place": "PLACE",
    "probe.g.route": "ROUTE",
    "probe.g.phys_opt": "PHYS_OPT",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _hash(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else _canonical(value)).hexdigest()


def _write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _language_contract(language: str) -> dict[str, Any]:
    common = {
        "max_steps": 6,
        "initial_stage": "ROUTED",
        "final_execution_budget": {"IMPLEMENTATION_HEAVY": 1, "MUTATION_LIGHT": 1},
        "one_commit": True,
        "no_qor_before_commit": True,
        "envelope": {
            "schema_version": "action_proposal.v1",
            "kind": "PLAN or STOP",
            "step_fields": ["step_id", "action_key", "parameters"],
        },
    }
    if language == "P":
        return common | {
            "language": "P",
            "actions": {
                "probe.p.call_macro": {"name": ["phys_opt", "retime"]},
                "probe.p.phys_opt": {"directive": ["Explore", "AddRetime", "AlternateFlowWithRetiming"]},
                "probe.p.unroute_all": {},
                "probe.p.unplace_all": {},
                "probe.p.place": {"directive": ["Default", "Explore"]},
                "probe.p.route": {"directive": ["Default", "Explore"]},
                "probe.p.select_target": {"selector": ["critical_cells", "high_fanout_cells", "sequential_cells"], "limit": "integer 1..64"},
                "probe.p.create_standard_pblock": {"pblock_id": "lowercase id", "region": ["device_quadrant_nw", "device_quadrant_se", "critical_path_window"]},
            },
            "follow_up": "SELECT_TARGET must precede CREATE_STANDARD_PBLOCK. Programs must finish ROUTED.",
        }
    return common | {
        "language": "G_AST",
        "actions": {
            "probe.g.select": {"object_type": "cell", "predicates": [{"property": ["IS_SEQUENTIAL", "REF_NAME", "FANOUT", "SLACK_NS"], "operator": ["EQ", "GE", "LE"], "value": "scalar"}], "limit": "integer 1..64"},
            "probe.g.create_pblock": {"pblock_id": "lowercase id", "x0": "0..999", "y0": "0..9999", "x1": ">=x0", "y1": ">=y0"},
            "probe.g.phys_opt": {"directive": ["Explore", "AddRetime", "AlternateFlowWithRetiming"]},
            "probe.g.unroute_all": {},
            "probe.g.unplace_all": {},
            "probe.g.place": {"directive": ["Default", "Explore"]},
            "probe.g.route": {"directive": ["Default", "Explore"]},
        },
        "containment": "No Tcl, shell, file, path, source, package, socket, network, process, loop, dynamic action name or hidden heavy operation.",
        "follow_up": "SELECT must precede CREATE_PBLOCK. Programs must finish ROUTED.",
    }


def _prompt(session: Mapping[str, Any], decision_id: str) -> bytes:
    return (
        "Return exactly one JSON action-proposal object and no prose or markdown. "
        f"Use schema_version action_proposal.v1, decision_id {decision_id}, "
        f"catalogue_version {session['catalogue_version']}, catalogue_hash {session['catalogue_hash']}. "
        "Use kind PLAN with one or more documented steps, or kind STOP with steps []. "
        "Do not invent fields/actions, Tcl, shell, files, paths, network, loops, retries, QoR claims, or a second submission. "
        "Choose one intervention to improve setup timing while respecting the visible final execution budget."
    ).encode()


def freeze(args: argparse.Namespace) -> int:
    repository = observe_repository(REPOSITORY_ROOT)
    if repository.tracked_tree_dirty:
        raise SystemExit("tracked_product_tree_not_clean")
    observations = {}
    roots = {}
    for root, observation_path, dcp_path in args.root:
        if root not in ROOT_HASHES or root.startswith("external_family_holdout"):
            raise SystemExit("development_root_not_allowed")
        dcp = Path(dcp_path).resolve()
        if hashlib.sha256(dcp.read_bytes()).hexdigest() != ROOT_HASHES[root]:
            raise SystemExit("development_root_hash_mismatch")
        observation = json.loads(Path(observation_path).read_text())
        source = observation["source"]
        observations[root] = {
            "schema_version": "dof.wp012-02-planning-base-observation.v1",
            "root_pseudonym": root,
            "state": source["state"],
            "validation": source["validation"],
            "history": [],
            "known_priors": {},
        }
        roots[root] = {"sha256": ROOT_HASHES[root], "path": os.fspath(dcp)}
    sessions = []
    for root in sorted(ROOT_HASHES):
        for model in MODELS:
            for language in LANGUAGES:
                contract = _language_contract(language)
                catalogue_version = f"dof.wp012-02.{language.lower()}.prototype.v1"
                catalogue_hash = _hash(contract)
                session_id = f"{root}__{model.replace('/', '_')}__{language.lower()}"
                sessions.append(
                    {
                        "session_id": session_id,
                        "root": root,
                        "model": model,
                        "language": language,
                        "catalogue_version": catalogue_version,
                        "catalogue_hash": catalogue_hash,
                        "language_contract": contract,
                        "max_turns": MAX_TURNS,
                        "max_feedback_rounds": MAX_FEEDBACK,
                        "provider_transport_retries": 0,
                        "provider_fallbacks": 0,
                        "final_submissions": 1,
                    }
                )
    core = {
        "schema_version": SCHEMA,
        "protocol": PROTOCOL,
        "probe_identity": PROBE_IDENTITY,
        "primary_aggregate_eligible": False,
        "sealed_holdouts_accessed": False,
        "repository": {"head_commit": repository.head_commit, "head_tree": repository.head_tree, "binary_diff_sha256": repository.binary_diff_sha256},
        "roots": roots,
        "observations": observations,
        "objective": "Improve setup timing under one committed intervention and one final QoR-scored execution.",
        "resource_guards": {"cost_cap_usd": COST_CAP_USD, "max_turns": MAX_TURNS, "max_feedback_rounds": MAX_FEEDBACK, "max_real_heavy_calls": 8},
        "sessions": sessions,
    }
    bundle = core | {"bundle_sha256": _hash(core)}
    _write_new(args.output, bundle)
    print(json.dumps({"status": "FROZEN", "sessions": len(sessions), "bundle_sha256": bundle["bundle_sha256"]}))
    return 0


async def preflight(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
    models, endpoints = await asyncio.gather(discover_openrouter_models(key), discover_openrouter_endpoints(key))
    import httpx
    async with httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", timeout=60, trust_env=False, follow_redirects=False) as client:
        response = await client.get("/auth/key", headers={"Authorization": "Bearer " + key})
    raw = response.json()
    data = raw.get("data", {}) if isinstance(raw, dict) else {}
    public_key = {
        "http_status": response.status_code,
        "limit_usd": data.get("limit"),
        "usage_usd": data.get("usage"),
        "limit_remaining_usd": data.get("limit_remaining"),
        "is_free_tier": data.get("is_free_tier"),
        "key_label_included": False,
        "key_hash_included": False,
    }
    selected = {model: endpoints["models"][model] for model in MODELS}
    payload = {
        "schema_version": "dof.wp012-02-openrouter-preflight.v1",
        "models": {model: models["required_models"][model] for model in MODELS},
        "endpoints": selected,
        "key_budget": public_key,
        "exact_models_available": all(models["required_models"][model] for model in MODELS),
        "exact_endpoints_available": all(
            selected[model]["endpoint_available"]
            and selected[model]["required_parameters_supported"]
            for model in MODELS
        ),
        "cost_cap_usd": COST_CAP_USD,
    }
    payload["accepted"] = bool(payload["exact_models_available"] and payload["exact_endpoints_available"] and response.status_code == 200)
    payload["preflight_sha256"] = _hash(payload)
    _write_new(args.output, payload)
    print(json.dumps({"accepted": payload["accepted"], "models": payload["models"], "limit_remaining_usd": public_key["limit_remaining_usd"]}))
    return 0 if payload["accepted"] else 2


def _binding(session: Mapping[str, Any], turn: int, context: bytes) -> ProviderRequestBinding:
    decision_id = f"wp01202-{_hash(session['session_id'])[:16]}-t{turn}"
    prompt = _prompt(session, decision_id)
    identity = f"{session['session_id']}:{turn}"
    return ProviderRequestBinding(
        run_id="wp01202-plan-" + _hash(identity)[:24],
        spec_hash=_hash("spec:" + identity),
        experiment_id="wp012-02-planning-pilot",
        condition_id="condition-" + _hash(session["session_id"])[:20],
        repeat_id="planning-1",
        decision_id=decision_id,
        context_bytes=context,
        prompt_bytes=prompt,
        prompt_version="dof.wp012-02-planning-prompt.v1",
        action_interface_version="action_proposal.v1",
        action_interface_hash=_hash("action_proposal.v1"),
        context_interface_version="dof.wp012-02-planning-context.v1",
        context_interface_hash=_hash(context),
        catalogue_version=session["catalogue_version"],
        catalogue_hash=session["catalogue_hash"],
        capability_hash=_hash(session["language_contract"]),
        mask_evidence_hash=_hash("no-primary-mask"),
        provider_role="wp01202_construction_planner",
        provider="openrouter",
        model_id=session["model"],
        adapter_version=OPENROUTER_ADAPTER_VERSION,
        settings=ProviderSettings(temperature=None, max_tokens=4096, seed=None, timeout_seconds=180, max_retries=0, retry_on=(), max_requests=1, max_usd=RESERVE[session["model"]], max_cost_per_request_usd=RESERVE[session["model"]], unknown_cost_policy="reject", unknown_usage_policy="reject"),
    )


def _binding_evidence(binding: ProviderRequestBinding) -> dict[str, Any]:
    """Return secret-free binding evidence without assuming a to_dict API."""

    return {
        "request_contract_version": binding.request_contract_version,
        "request_contract_hash": binding.request_contract_hash,
        "run_id": binding.run_id,
        "spec_hash": binding.spec_hash,
        "experiment_id": binding.experiment_id,
        "condition_id": binding.condition_id,
        "repeat_id": binding.repeat_id,
        "decision_id": binding.decision_id,
        "prompt_hash": binding.prompt_hash,
        "context_hash": binding.context_hash,
        "catalogue_version": binding.catalogue_version,
        "catalogue_hash": binding.catalogue_hash,
        "provider": binding.provider,
        "model_id": binding.model_id,
        "adapter_version": binding.adapter_version,
        "settings": binding.settings.to_dict(),
        "prompt_bytes_included": False,
        "context_bytes_included": False,
    }


def _program_from_proposal(language: str, proposal: Any) -> dict[str, Any]:
    if proposal.kind.value == "STOP":
        raise ActionLanguageProbeError("model_chose_stop")
    mapping = P_ACTIONS if language == "P" else G_ACTIONS
    steps = []
    for step in proposal.steps:
        if step.action_key not in mapping:
            raise ActionLanguageProbeError("documented_action_not_used")
        steps.append({"op": mapping[step.action_key], "parameters": dict(step.parameters)})
    raw = {"program_id": "commit_" + proposal.proposal_hash[:16], "steps": steps}
    compiled = validate_p_program(raw) if language == "P" else validate_g_ast(raw)
    if compiled["heavy_calls"] > 1:
        raise ActionLanguageProbeError("final_heavy_budget_exceeded")
    if compiled["final_stage"] != "ROUTED":
        raise ActionLanguageProbeError("final_stage_not_routed")
    for item in compiled["compiled_steps"]:
        if item.get("legacy_symbolic"):
            raise ActionLanguageProbeError("macro_not_real_admissible_in_prototype")
    if not any(item.get("action_key") for item in compiled["compiled_steps"]):
        raise ActionLanguageProbeError("program_has_no_dispatchable_intervention")
    return compiled


@dataclass
class SharedCost:
    spent: float = 0.0
    reserved: float = 0.0
    lock: asyncio.Lock | None = None

    def __post_init__(self) -> None:
        self.lock = asyncio.Lock()

    async def acquire(self, amount: float) -> bool:
        assert self.lock
        async with self.lock:
            if self.spent + self.reserved + amount > COST_CAP_USD + 1e-12:
                return False
            self.reserved += amount
            return True

    async def settle(self, reserved: float, actual: float) -> None:
        assert self.lock
        async with self.lock:
            self.reserved -= reserved
            self.spent += actual


async def _planning_session(
    session: Mapping[str, Any],
    bundle: Mapping[str, Any],
    output: Path,
    key: str,
    cost: SharedCost,
    initial_lost_turns: int,
) -> dict[str, Any]:
    started = time.monotonic()
    feedback: list[str] = []
    turns = []
    if initial_lost_turns:
        feedback.append("controller_persistence_failure_no_model_output_available")
        turns.append(
            {
                "turn": 1,
                "provider_endpoint": "CONTROLLER_PERSISTENCE_FAILURE",
                "provider_accepted": None,
                "proposal_hash": None,
                "validation_error": feedback[-1],
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cost_usd": None,
                "route": None,
                "physical_requests": 1,
                "evidence_completeness": "LOST_BEFORE_PERSISTENCE",
            }
        )
    committed = None
    final_proposal_hash = None
    for turn in range(initial_lost_turns, MAX_TURNS):
        reserve = RESERVE[session["model"]]
        if not await cost.acquire(reserve):
            feedback.append("global_cost_guard_would_exceed")
            break
        context_value = {
            "schema_version": "dof.wp012-02-planning-context.v1",
            "objective": bundle["objective"],
            "base_observation": bundle["observations"][session["root"]],
            "language_contract": session["language_contract"],
            "construction_feedback": feedback[-1:] if turn else [],
            "feedback_contains_qor": False,
            "turn": turn + 1,
            "turns_remaining_including_this": MAX_TURNS - turn,
        }
        context = _canonical(context_value)
        binding = _binding(session, turn, context)
        transport = OpenRouterChatCompletionsTransport(api_key=key, expected_outbound_binding=build_openrouter_outbound(binding))
        decision = await DoFProviderObserver(run_ledger=ProviderRunLedger.for_binding(binding)).request_async(binding, transport)
        attempts = [item.to_dict() for item in decision.attempts]
        errors = verify_provider_attempts(decision.attempts, binding)
        actual = float(decision.attempts[-1].cost_usd or decision.attempts[-1].accounted_cost_usd or 0.0)
        await cost.settle(reserve, actual)
        exchange = transport.last_exchange or {}
        if exchange.get("failure_class") == "MODEL_OR_ROUTE_DRIFT":
            raise RuntimeError("MODEL_OR_ROUTE_DRIFT")
        route = exchange.get("route") or {}
        if decision.accepted and (route.get("returned_model") != session["model"] or route.get("selected_upstream_provider") != OPENROUTER_E1_PROFILES[session["model"]]["upstream_provider"] or route.get("fallback_allowed") is not False):
            raise RuntimeError("MODEL_OR_ROUTE_DRIFT")
        turn_dir = output / "private" / session["session_id"] / f"turn-{turn + 1}"
        _write_new(turn_dir / "binding.json", _binding_evidence(binding))
        _write_new(turn_dir / "exchange.json", exchange)
        _write_new(turn_dir / "attempts.json", attempts)
        validation_error = None
        proposal_hash = None
        if errors:
            validation_error = "provider_attempt_evidence_invalid"
        elif not decision.accepted or decision.bound_proposal is None:
            validation_error = "provider_output:" + ";".join(decision.reasons)
        else:
            proposal = decision.bound_proposal.verify_for_dispatch(binding)
            proposal_hash = proposal.proposal_hash
            try:
                committed = _program_from_proposal(session["language"], proposal)
                final_proposal_hash = proposal_hash
            except ActionLanguageProbeError as exc:
                validation_error = str(exc)
        turns.append({"turn": turn + 1, "provider_endpoint": decision.endpoint, "provider_accepted": decision.accepted, "proposal_hash": proposal_hash, "validation_error": validation_error, "input_tokens": decision.attempts[-1].input_tokens, "output_tokens": decision.attempts[-1].output_tokens, "total_tokens": decision.attempts[-1].total_tokens, "cost_usd": actual, "route": route, "physical_requests": transport.physical_requests})
        if committed is not None:
            break
        feedback.append(validation_error or "unknown_construction_error")
    if committed is not None:
        _write_new(output / "commits" / (session["session_id"] + ".json"), committed)
    return {
        "session_id": session["session_id"], "root": session["root"], "model": session["model"], "language": session["language"],
        "terminal": "COMMIT" if committed is not None else "NO_VALID_SUBMISSION", "turns": turns,
        "model_turns": len(turns), "validation_attempts": len(turns), "feedback_rounds": max(0, len(turns) - 1),
        "valid_first_draft": committed is not None and len(turns) == 1, "valid_within_budget": committed is not None,
        "committed_program_sha256": committed["program_sha256"] if committed else None, "final_proposal_hash": final_proposal_hash,
        "committed_complexity_steps": len(committed["compiled_steps"]) if committed else None, "committed_heavy_calls": committed["heavy_calls"] if committed else None,
        "provider_requests": len(turns), "transport_retries": 0, "fallbacks": 0, "qor_feedback_before_commit": False,
        "cost_usd": sum(row["cost_usd"] or 0.0 for row in turns), "total_tokens": sum(row["total_tokens"] or 0 for row in turns), "wall_seconds": time.monotonic() - started,
    }


def _gated(program: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows, stage = [], program["initial_stage"]
    for item in program["compiled_steps"]:
        action = item.get("action_key")
        if not action:
            continue
        final = item.get("expanded", {}).get("final_stage", stage)
        if action == "generated.pblock_region":
            final = stage
        rows.append({"step_index": len(rows), "step_id": f"commit-step-{len(rows):02d}", "action_key": action, "parameters": item["parameters"], "stage_before": stage, "stage_after": final})
        stage = final
    return rows


def _execute_committed(session: Mapping[str, Any], row: dict[str, Any], bundle: Mapping[str, Any], output: Path, args: argparse.Namespace) -> dict[str, Any]:
    if row["terminal"] != "COMMIT":
        return row | {"execution_endpoint": "NOT_EXECUTED_NO_VALID_SUBMISSION", "valid_child": False, "final_execution_count": 0}
    program = json.loads((output / "commits" / (session["session_id"] + ".json")).read_text())
    root = Path(bundle["roots"][session["root"]]["path"])
    causal_core = {"schema_version": "dof.wp012-02-planning-commit-evidence.v1", "probe_identity": PROBE_IDENTITY, "primary_aggregate_eligible": False, "session_id": session["session_id"], "program_sha256": program["program_sha256"], "qor_available_before_commit": False, "single_submission": True}
    causal = causal_core | {"causal_evidence_sha256": _hash(causal_core)}
    plan = freeze_e0_real_gated_plan(root, root_pseudonym=session["root"], level=DoFLevel.L3 if session["language"] == "P" and all(item.get("action_key") != "generated.pblock_region" for item in program["compiled_steps"]) else DoFLevel.L4, decision_id="commit-" + _hash(session["session_id"])[:20], proposal_id="proposal-" + _hash(session["session_id"])[:20], proposal_hash=row["final_proposal_hash"], gate_decision_sha256=_hash({"accepted": True, "program_sha256": program["program_sha256"]}), gated_steps=_gated(program), causal_evidence_sha256=causal["causal_evidence_sha256"], run_id="wp01202-session-" + _hash(session["session_id"])[:20], repository_anchor=REPOSITORY_ROOT, python_exec=args.python_exec, vivado_exec=args.vivado_exec, run_dir=output / "execution" / session["session_id"], fake_startup_attestation_sha256=verified_fake_attestation_evidence_sha256(args.fake_attestation), protocol="e1v2", scientific_selection_eligible=False)
    authorization = build_e0_dispatch_authorization(plan, causal)
    result = execute_e0_real_gated_plan(plan, root, output / "execution" / session["session_id"], dispatch_authorization=authorization, causal_evidence=causal, vivado_exec=args.vivado_exec, python_exec=args.python_exec, repository_anchor=REPOSITORY_ROOT)
    _write_new(output / "private" / session["session_id"] / "execution-result.json", result)
    return row | {"execution_endpoint": result["endpoint"], "valid_child": result["endpoint"] == "TECHNICAL_MUTATION_VALID_NONSCIENTIFIC", "root_unchanged": result["root_unchanged"], "actual_heavy_calls": result["real_mutation_invocations"], "final_execution_count": 1}


async def run(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY_missing")
    bundle = json.loads(args.bundle.read_text())
    preflight_value = json.loads(args.preflight.read_text())
    if not preflight_value.get("accepted"):
        raise SystemExit("provider_preflight_not_accepted")
    repo = observe_repository(REPOSITORY_ROOT)
    if repo.tracked_tree_dirty or repo.head_commit != bundle["repository"]["head_commit"] or repo.head_tree != bundle["repository"]["head_tree"]:
        raise SystemExit("repository_binding_mismatch")
    args.output.mkdir(parents=True, exist_ok=False)
    shared = SharedCost(spent=args.initial_cost_usd)
    if args.initial_lost_turns:
        incident = {
            "schema_version": "dof.wp012-02-planning-controller-incident.v1",
            "predecessor_output": os.fspath(args.predecessor_output) if args.predecessor_output else None,
            "lost_turns_per_session": args.initial_lost_turns,
            "sessions_affected": len(bundle["sessions"]),
            "conservative_physical_request_attempts": len(bundle["sessions"]),
            "key_usage_delta_usd": args.initial_cost_usd,
            "model_output_available": False,
            "root_execution_started": False,
            "recovery": "resume_within_original_three-turn_envelope",
        }
        incident["incident_sha256"] = _hash(incident)
        _write_new(args.output / "controller-incident.json", incident)
    planning_rows = await asyncio.gather(*[
        _planning_session(
            session,
            bundle,
            args.output,
            key,
            shared,
            args.initial_lost_turns,
        )
        for session in bundle["sessions"]
    ])
    session_map = {row["session_id"]: row for row in bundle["sessions"]}
    semaphore = asyncio.Semaphore(8)
    async def execute_one(row: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await asyncio.to_thread(_execute_committed, session_map[row["session_id"]], row, bundle, args.output, args)
    final_rows = await asyncio.gather(*[execute_one(row) for row in planning_rows])
    aggregate = {
        "schema_version": SCHEMA, "protocol": PROTOCOL, "probe_identity": PROBE_IDENTITY, "primary_aggregate_eligible": False,
        "sessions": final_rows, "session_count": len(final_rows), "valid_first_draft_rate": sum(row["valid_first_draft"] for row in final_rows) / len(final_rows),
        "valid_within_budget_rate": sum(row["valid_within_budget"] for row in final_rows) / len(final_rows), "executed_valid_child_rate": sum(row["valid_child"] for row in final_rows) / len(final_rows),
        "physical_provider_requests": sum(row["provider_requests"] for row in final_rows), "known_cost_usd": shared.spent, "cost_cap_usd": COST_CAP_USD,
        "actual_heavy_calls": sum(row.get("actual_heavy_calls", 0) for row in final_rows), "second_submission_count": 0, "qor_feedback_before_commit_count": 0,
        "sealed_holdouts_accessed": False,
    }
    aggregate["aggregate_sha256"] = _hash(aggregate)
    _write_new(args.output / "planning-aggregate.json", aggregate)
    print(json.dumps({key: aggregate[key] for key in ("session_count", "valid_first_draft_rate", "valid_within_budget_rate", "executed_valid_child_rate", "physical_provider_requests", "known_cost_usd", "actual_heavy_calls")}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("preflight"); p.add_argument("--output", type=Path, required=True)
    f = sub.add_parser("freeze"); f.add_argument("--root", action="append", nargs=3, metavar=("PSEUDONYM", "OBSERVATION", "DCP"), required=True); f.add_argument("--output", type=Path, required=True)
    r = sub.add_parser("run"); r.add_argument("--bundle", type=Path, required=True); r.add_argument("--preflight", type=Path, required=True); r.add_argument("--output", type=Path, required=True); r.add_argument("--vivado-exec", type=Path, required=True); r.add_argument("--python-exec", type=Path, required=True); r.add_argument("--fake-attestation", type=Path, required=True); r.add_argument("--initial-lost-turns", type=int, choices=(0, 1), default=0); r.add_argument("--initial-cost-usd", type=float, default=0.0); r.add_argument("--predecessor-output", type=Path)
    args = parser.parse_args()
    if args.command == "freeze": return freeze(args)
    if args.command == "preflight": return asyncio.run(preflight(args))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
