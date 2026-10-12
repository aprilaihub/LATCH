"""Single-COMMIT migrated WP015 trial. Historical collectors remain unchanged."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any, Mapping

from optimizer_pipeline.core.domain import (
    RunLifecycleEvent, RunStatus, ValidationLevel, ValidationRecord, ValidationStatus,
)
from optimizer_pipeline.domain.transition import DcpTransitionRecord, DcpTransitionOutcome, DcpTransitionHandling
from optimizer_pipeline.run.store_bridge import DatabaseTransitionSink, initialize_run_store
from optimizer_pipeline.scoring import file_sha256

from ..protocol.contract_v6 import contract_hash_for_cell
from ..protocol.handles import TrialHandleRegistry
from ..protocol.language import CompileContext
from ..protocol.macro_authority import M10_ACTION_KEYS
from ..protocol.observation import build_common_observation
from ..protocol.observation_v4 import CommonQuerySurfaceV4
from ..protocol.planner import SystemBinding
from ..protocol.planner_v2 import result_public
from .session import StudySession
from .scientific_evidence import boundary_evidence, strict_boundary_certificate
from .mechanism_evidence import augment_execution
from .validation_scope import describe_validation, public_validation_plan, public_validation_scope


CONDITIONS = {"recipe": ("L2", "M_ONLY"), "direct": ("L3", "P_BOUNDARY"),
              "constructed": ("L4", "G_BOUNDARY")}

# These are fields of the WP015 common scientific observation, including the
# collector's historical queue/cost context (WP014C configure). They do not
# authorize spending or override today's separately approved request ledger,
# per-operation timeout, or smoke budget. Keep model-visible study fields.
WP015_OBSERVATION_BUDGET = {"planning_turns": 4, "queries": 8,
    "feedback_rounds": 3, "heavy_calls": 4, "light_mutations": 8,
    "wall_time_seconds": 28800, "usd": 12.0}


def make_session(*, condition: str, root: Path, context: Mapping[str, Any], mode: str) -> StudySession:
    level, language_condition = CONDITIONS[condition]
    root_hash = file_sha256(root)
    if mode != "mock" and context.get("root_sha256") != root_hash:
        raise ValueError("Frozen context does not bind this input checkpoint; capture it before executing.")
    observation_fields = context.get("observation", {})
    if mode != "mock" and not observation_fields:
        raise ValueError("Prepared-real requires a captured observation, not a fabricated baseline.")
    observation = build_common_observation(
        root_pseudonym="root-" + root_hash[:16],
        **{name: observation_fields.get(name, [] if name == "critical_paths" else {})
           for name in ("target_clock", "timing", "utilization", "implementation", "constraints",
                        "geometry", "congestion", "critical_paths")},
        budget=WP015_OBSERVATION_BUDGET,
    )
    # Preserve the caller's captured handle namespace for saved responses. It
    # must be supplied explicitly and is never written to public summaries.
    if mode != "mock" and (not context.get("trial_id") or not context.get("handle_secret_hex")):
        raise ValueError("Prepared-real requires the captured trial_id and handle_secret_hex for exact handle resolution")
    secret = bytes.fromhex(context["handle_secret_hex"]) if context.get("handle_secret_hex") else secrets.token_bytes(32)
    registry = TrialHandleRegistry(trial_id=context.get("trial_id", "latch-migration-probe"),
                                  root_sha256=root_hash, secret=secret)
    for row in context.get("objects", []):
        registry.register(**row)
    queries = CommonQuerySurfaceV4(registry)
    admitted = tuple(context.get("admitted_action_keys", M10_ACTION_KEYS))
    compile_context = CompileContext(registry, "ROUTED", tuple(context.get("predefined_regions", [])),
        tuple(context.get("geometry_bounds", (0, 4, 0, 4))),
        tuple(key.removeprefix("macro.") for key in admitted),
        fanout_admitted=bool(context.get("fanout_admitted", False)), action_surface_version="v3",
        hardblock_compatibility_relation=context.get("hardblock_compatibility_relation"))
    binding = SystemBinding.create(observation_hash=observation.sha256, root_binding=root_hash,
        catalogue_version="dof.dual-track.v3", catalogue_hash=contract_hash_for_cell(
            study_track="C_CONDITIONED", coverage_regime="FULL_M10", language_condition=language_condition,
            level="C", admitted_action_keys=admitted),
        provider=context.get("provider", "fixed-response"), model=context.get("model", "caller-supplied"),
        route="live" if mode == "real" else "offline", session_nonce="migration")

    def query_provider(name, top_k):
        values = context.get("queries", {}).get(name)
        if values is None:
            return [], [], None, "not_captured"
        return values, ["captured-context"], 0.0, context.get("query_missing_reasons", {}).get(name)

    return StudySession(level, observation, queries, compile_context, binding, query_provider,
        language_condition=language_condition, strict_g_required=condition == "constructed",
        required_family=context.get("strict_mechanism"))


def mock_material(condition: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context = {"objects": [{"object_type": "cell", "canonical_identity": "mock/cell",
        "attributes": {"path_member": True, "path_rank": 1, "resource_type": "SLICE"}}],
        "geometry_bounds": [0, 4, 0, 4]}
    if condition == "recipe":
        candidate = {"language": "M", "action_key": "macro.phys_opt"}
    elif condition == "direct":
        candidate = {"language": "P", "steps": [
            {"step_id": "s1", "op": "UNROUTE", "args": {}},
            {"step_id": "s2", "op": "ROUTE", "args": {"directive": "Default"}}]}
    else:
        candidate = {"language": "G", "steps": [
            {"step_id": "s1", "op": "SELECT_WHERE", "args": {"source_type": "cell", "predicate":
                {"op": "COMPARE", "field": "path_member", "operator": "EQ", "value": True}, "top_k": 1}, "bind": "targets"},
            {"step_id": "s2", "op": "CREATE_REGION", "args": {"x_min": 1, "x_max": 1, "y_min": 1, "y_max": 1}, "bind": "region"},
            {"step_id": "s3", "op": "ASSIGN_REGION", "args": {"target_handle": {"ref": "targets"}, "region_handle": {"ref": "region"}}},
            {"step_id": "s4", "op": "UNROUTE", "args": {}},
            {"step_id": "s5", "op": "ROUTE", "args": {"directive": "Default"}}]}
    return context, [{"kind": "COMMIT", "candidate": candidate}]


async def run_prepared(*, condition: str, mode: str, root: Path, output: Path,
                       context: Mapping[str, Any], messages: list[Mapping[str, Any]],
                       python: Path | None = None, vivado: Path | None = None,
                       execute=None, planner=None, post_commit=None) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Output must be a new directory; inputs and historical outputs are never overwritten.")
    if mode not in {"mock", "prepared-real", "real"}:
        raise ValueError("Unsupported mode; no model or experimental condition is substituted.")
    if mode == "real" and (planner is None or post_commit is None):
        raise ValueError("Real mode requires explicit planning, scoring and multiseed validation adapters")
    session = make_session(condition=condition, root=root, context=context, mode=mode)
    output.mkdir(parents=True)
    store = initialize_run_store(run_dir=output, run_id=output.name, mode="dof:" + mode,
                                 input_dcp=root, root_metrics={})
    before = file_sha256(root)
    result = {"condition": condition, "mode": mode, "historical_replay": False,
              "source_protocol": "WP015", "migration_implementation": True,
              "execution_started": False, "physical_valid": None,
              "functional_validation": {"status": "NOT_RUN", "passed": None},
              "post_commit_scoring": "NOT_RUN", "feedback": []}
    result["validation_plan"] = public_validation_plan(getattr(post_commit, "validation_plan", None))
    result["validation_assessment"] = describe_validation(
        result["validation_plan"], result["functional_validation"])
    event_sequence = 1
    try:
        used_messages = []
        for turn in range(4):
            if session.terminal is not None:
                break  # Never request, repair, or execute a second proposal after COMMIT.
            if planner is not None:
                message = await asyncio.to_thread(planner, session=session,
                    previous_result=result["feedback"][-1] if result["feedback"] else None)
            elif turn < len(messages):
                message = messages[turn]
            else:
                break
            used_messages.append(message)
            accepted = session.accept(message)
            result["feedback"].append(result_public(accepted))
        if session.terminal is None and session.turns_used == 4:
            session.terminal = "NO_VALID_SUBMISSION"
        if session.terminal is None:
            result["planning_terminal"] = "INCOMPLETE_FIXED_RESPONSE_SEQUENCE"
        else:
            result["planning_terminal"] = session.terminal
        result["valid_commits"] = session.valid_commits
        result["turns_used"] = session.turns_used
        compiled = session.committed
        execution = None
        candidate = next((message.get("candidate") for message in reversed(used_messages)
                          if isinstance(message, Mapping) and message.get("kind") == "COMMIT"), None) if compiled else None
        if compiled is not None:
            if file_sha256(root) != before:
                raise ValueError("Input changed before dispatch")
            sink = DatabaseTransitionSink(store)
            record = DcpTransitionRecord.scheduled_transition(run_id=output.name,
                transition_id="commit-1", source="dof:" + condition,
                parent_node_id="node_000_root", parent_dcp=str(root), optimizer_name=compiled.language,
                target_node_id="node_001_child", optimizer_params=compiled.public_payload(),
                metadata={"single_commit": True, "simulation": mode == "mock"})
            sink.append(record)
            result["execution_started"] = True
            if mode == "mock" and execute is None:
                child = output / "mock-child.dcp"
                child.write_text("Simulated checkpoint, not a valid FPGA artifact\n")
                execution = {"canonical_endpoint": "MOCK_COMPLETION", "child_path": str(child),
                             "actual_heavy_calls": 0, "actual_light_mutations": 0}
            else:
                if execute is None:
                    from optimizer_pipeline.backends.xilinx.compiled_operations import execute_compiled
                    execute = execute_compiled
                execution = await execute(compiled=compiled, registry=session.compile_context.registry,
                    root=root, out=output / "execution", python=python, vivado=vivado)
                if mode != "mock":
                    result["physical_valid"] = execution.get("canonical_endpoint") == "VALID_COMPLETION"
                    child_path = execution.get("child_path")
                    if result["physical_valid"] and (not child_path or not Path(child_path).is_file()):
                        result["physical_valid"] = False
                        execution = dict(execution, canonical_endpoint="MISSING_CHILD_ARTIFACT",
                                         error="Dispatcher reported completion without a child checkpoint")
            result["physical_terminal"] = execution.get("canonical_endpoint")
            result["root_unchanged"] = file_sha256(root) == before
            if not result["root_unchanged"]:
                result["physical_valid"] = False
                result["physical_terminal"] = "ROOT_CHANGED"
            record.outcome = DcpTransitionOutcome(executed=True,
                success=execution.get("canonical_endpoint") in {"MOCK_COMPLETION", "VALID_COMPLETION"}
                        and result["root_unchanged"],
                output_dcp=execution.get("child_path"), child_node_id="node_001_child",
                validation_status="WARNING" if mode == "mock" else ("CLEAN" if result["physical_valid"] else "INVALID"),
                rejection_reason=execution.get("error"), metrics={"simulation": mode == "mock", "publish_eligible": False,
                    "physical_terminal": result["physical_terminal"],
                    "actual_heavy_calls": execution.get("actual_heavy_calls"),
                    "actual_light_mutations": execution.get("actual_light_mutations")})
            record.handling = DcpTransitionHandling(scheduled=True, final_status="executed", schedule_stage="executed")
            sink.append(record)
            subject = "node_001_child" if execution.get("child_path") and Path(execution["child_path"]).is_file() else "node_000_root"
            if mode != "mock" and result["physical_valid"] and result["root_unchanged"] and post_commit is not None:
                measured = await asyncio.to_thread(post_commit, root=root,
                    child=Path(execution["child_path"]), output=output / "post-commit")
                result["post_commit_scoring"] = measured["scoring"]
                result["functional_validation"] = measured["functional_validation"]
                result["research_judgement"] = measured["research_judgement"]
                result["validation_plan"] = public_validation_plan(measured.get("validation_plan", result["validation_plan"]))
                result["validation_assessment"] = public_validation_scope(
                    dict(measured, validation_plan=result["validation_plan"]))["validation_assessment"]
                if not measured["inputs_unchanged"]:
                    result["physical_valid"] = False
                store.add_run_event(RunLifecycleEvent("score-record", output.name, event_sequence, RunStatus.RUNNING,
                    detail={"post_commit_scoring": measured["scoring"]}))
                event_sequence += 1
                for index, seed in enumerate(measured["functional_validation"].get("seed_reports", [])):
                    store.add_validation(ValidationRecord(f"validation-seed-{index}", output.name, "scope-root", subject,
                        ValidationLevel.CERTIFICATION,
                        ValidationStatus.NOT_RUN if seed.get("infrastructure_failure") else ValidationStatus.PASSED if seed.get("passed") else ValidationStatus.FAILED,
                        details={key: seed.get(key) for key in ("stimulus_seed", "passed", "cycles_simulated", "mismatch_count", "protocol_mismatch_count", "infrastructure_failure", "infrastructure_reason")}))
                store.add_run_event(RunLifecycleEvent("research-judgement", output.name, event_sequence, RunStatus.RUNNING,
                    detail={"research_judgement": measured["research_judgement"]}))
                event_sequence += 1
            functional = result["functional_validation"]
            status = ValidationStatus.PASSED if functional["passed"] is True else ValidationStatus.FAILED if functional["passed"] is False else ValidationStatus.NOT_RUN
            store.add_validation(ValidationRecord("validation-functional", output.name, "scope-root",
                subject, ValidationLevel.CERTIFICATION, status, details=dict(functional,
                    validation_plan=result["validation_plan"], validation_assessment=result["validation_assessment"])))
        if planner is not None:
            result["provider_accounting"] = planner.accounting()
        result["boundary_evidence"] = boundary_evidence(candidate=candidate,
            execution_started=mode != "mock" and result["execution_started"], execution=execution if mode != "mock" else None)
        if mode != "mock" and execution is not None and compiled is not None:
            execution = augment_execution(candidate=candidate, compiled=compiled, execution=execution)
        result["strict_boundary_certificate"] = strict_boundary_certificate(candidate=candidate,
            execution_started=mode != "mock" and result["execution_started"], execution=execution if mode != "mock" else None)
        (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        store.add_run_event(RunLifecycleEvent("run-event-final", output.name, event_sequence, RunStatus.FINALIZED,
            detail={"migration_probe": True, "planning_terminal": result["planning_terminal"],
                    "physical_valid": result["physical_valid"], "functional_validation": result["functional_validation"]["status"]}))
        return result
    except BaseException as exc:
        result["infrastructure_failure"] = {"error_type": type(exc).__name__}
        result["root_unchanged"] = file_sha256(root) == before
        if planner is not None:
            result["provider_accounting"] = planner.accounting()
        (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        store.add_run_event(RunLifecycleEvent("run-event-error", output.name, event_sequence, RunStatus.FAILED,
            detail={"error_type": type(exc).__name__, "execution_started": result["execution_started"],
                    "provider_accounting": result.get("provider_accounting")}))
        raise
    finally:
        store.close()
