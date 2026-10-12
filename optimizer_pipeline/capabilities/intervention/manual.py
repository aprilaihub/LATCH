"""Manual rescue planning and one-action synthesis.

This module is intentionally separate from recipe synthesis.  Manual rescue is
the tail path after normal optimizer flows have failed or are not trusted, so
its catalog exposes only low-level typed actions and never ``flow.*`` entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

from optimizer_pipeline.capabilities.intervention.generation import InterventionProposalGenerator
from optimizer_pipeline.models import to_jsonable


class ManualPrimitiveKind(str, Enum):
    MACRO_TCL = "macro_tcl"
    TYPED_VIVADO = "typed_vivado"


@dataclass(frozen=True)
class ManualPrimitiveSpec:
    name: str
    kind: ManualPrimitiveKind
    mutates_design: bool
    tool_name: Optional[str] = None
    allowed_directives: Tuple[str, ...] = ()
    macro_command: Optional[str] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True)
class ManualActionSpec:
    action_id: str
    primitive: str
    args: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    expected_observation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True)
class ManualCheckpointState:
    """Typed implementation state attached to a checkpoint, not a global run."""

    placement: str = "placed"
    routing: str = "routed"
    source: str = "inferred_completed_checkpoint"
    parent_node_id: Optional[str] = None
    transition_history: Tuple[Dict[str, str], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        payload = to_jsonable(self)
        payload["next_action_constraints"] = {
            "manual.unroute_all": "requires routing=routed",
            "manual.unplace_all": "requires placement=placed and routing=unrouted",
            "manual.place_design": "requires placement=unplaced",
            "manual.route_design": "requires placement=placed and routing=unrouted",
            "manual.phys_opt_design": "requires placement=placed",
        }
        return payload

    @classmethod
    def from_dict(cls, payload: Optional[Dict[str, Any]], *, parent_node_id: Optional[str] = None) -> "ManualCheckpointState":
        data = payload if isinstance(payload, dict) else {}
        history = data.get("transition_history") if isinstance(data.get("transition_history"), list) else []
        return cls(
            placement=str(data.get("placement") or "placed"),
            routing=str(data.get("routing") or "routed"),
            source=str(data.get("source") or "inferred_completed_checkpoint"),
            parent_node_id=str(data.get("parent_node_id") or parent_node_id) if (data.get("parent_node_id") or parent_node_id) else None,
            transition_history=tuple(item for item in history if isinstance(item, dict)),
        )


@dataclass(frozen=True)
class ManualRescuePlan:
    plan_id: str
    hypothesis: str
    target_bottleneck: str
    steps: Tuple[ManualActionSpec, ...]
    revise_conditions: Tuple[str, ...] = ()
    abort_conditions: Tuple[str, ...] = ()
    source: str = "llm_manual_plan"

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class ManualValidationResult:
    valid: bool
    action: Optional[ManualActionSpec] = None
    plan: Optional[ManualRescuePlan] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    raw_payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class ManualSynthesisResult:
    plan: Optional[ManualRescuePlan] = None
    action: Optional[ManualActionSpec] = None
    validation: ManualValidationResult = field(default_factory=lambda: ManualValidationResult(valid=False))
    audit: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


class ManualPrimitiveCatalog:
    """Manual rescue primitive catalog with no optimizer-flow primitives."""

    def __init__(self, primitives: Iterable[ManualPrimitiveSpec]) -> None:
        self._primitives = {primitive.name: primitive for primitive in primitives}

    @classmethod
    def default(cls) -> "ManualPrimitiveCatalog":
        return cls(
            [
                ManualPrimitiveSpec(
                    name="manual.unroute_all",
                    kind=ManualPrimitiveKind.MACRO_TCL,
                    mutates_design=True,
                    macro_command="route_design -unroute",
                    description="Unroute all nets using a fixed runtime Tcl macro.",
                ),
                ManualPrimitiveSpec(
                    name="manual.unplace_all",
                    kind=ManualPrimitiveKind.MACRO_TCL,
                    mutates_design=True,
                    macro_command="place_design -unplace",
                    description="Remove placement using a fixed runtime Tcl macro.",
                ),
                ManualPrimitiveSpec(
                    name="manual.place_design",
                    kind=ManualPrimitiveKind.TYPED_VIVADO,
                    mutates_design=True,
                    tool_name="place_design",
                    allowed_directives=("Default", "Explore"),
                    description="Run Vivado place_design with an allowlisted directive.",
                ),
                ManualPrimitiveSpec(
                    name="manual.route_design",
                    kind=ManualPrimitiveKind.TYPED_VIVADO,
                    mutates_design=True,
                    tool_name="route_design",
                    allowed_directives=("Default", "Explore"),
                    description="Run Vivado route_design with an allowlisted directive.",
                ),
                ManualPrimitiveSpec(
                    name="manual.phys_opt_design",
                    kind=ManualPrimitiveKind.TYPED_VIVADO,
                    mutates_design=True,
                    tool_name="phys_opt_design",
                    allowed_directives=("Explore", "AddRetime", "AlternateFlowWithRetiming"),
                    description="Run Vivado phys_opt_design with an allowlisted directive.",
                ),
            ]
        )

    def get(self, name: str) -> Optional[ManualPrimitiveSpec]:
        return self._primitives.get(str(name))

    def names(self) -> List[str]:
        return sorted(self._primitives)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "manual_primitive_catalog.v1",
            "primitives": [self._primitives[name].to_dict() for name in self.names()],
            "flow_primitives_available": False,
            "raw_tcl_available": False,
        }


class ManualActionValidator:
    def __init__(self, catalog: Optional[ManualPrimitiveCatalog] = None) -> None:
        self.catalog = catalog or ManualPrimitiveCatalog.default()

    def validate_action(self, payload: Dict[str, Any], *, source: str = "manual_action") -> ManualValidationResult:
        if not isinstance(payload, dict):
            return ManualValidationResult(valid=False, errors=["payload_not_object"], raw_payload={})
        raw_payload = dict(payload)
        action_payload = payload.get("action") if isinstance(payload.get("action"), dict) else payload
        primitive_name = str(action_payload.get("primitive") or action_payload.get("name") or "").strip()
        if not primitive_name:
            return ManualValidationResult(valid=False, errors=["missing_primitive"], raw_payload=raw_payload)
        if primitive_name.startswith("flow.") or primitive_name in {"retime", "phys_opt", "auto_pblock", "pblock", "run_tcl", "shell", "bash", "write_checkpoint"}:
            return ManualValidationResult(valid=False, errors=[f"forbidden_primitive:{primitive_name}"], raw_payload=raw_payload)
        primitive = self.catalog.get(primitive_name)
        if primitive is None:
            return ManualValidationResult(valid=False, errors=[f"unknown_primitive:{primitive_name}"], raw_payload=raw_payload)
        args = action_payload.get("args") if isinstance(action_payload.get("args"), dict) else {}
        step_args, errors = self._validate_args(primitive, args)
        if errors:
            return ManualValidationResult(valid=False, errors=errors, raw_payload=raw_payload)
        action = ManualActionSpec(
            action_id=str(action_payload.get("action_id") or "action_000"),
            primitive=primitive_name,
            args=step_args,
            rationale=str(action_payload.get("rationale") or ""),
            expected_observation=str(action_payload.get("expected_observation") or ""),
        )
        return ManualValidationResult(valid=True, action=action, raw_payload=raw_payload)

    def validate_plan(self, payload: Dict[str, Any], *, source: str = "manual_plan") -> ManualValidationResult:
        if not isinstance(payload, dict):
            return ManualValidationResult(valid=False, errors=["payload_not_object"], raw_payload={})
        raw_payload = dict(payload)
        plan_payload = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
        steps_payload = plan_payload.get("steps")
        if not isinstance(steps_payload, list) or not steps_payload:
            return ManualValidationResult(valid=False, errors=["missing_plan_steps"], raw_payload=raw_payload)
        actions: List[ManualActionSpec] = []
        errors: List[str] = []
        for idx, item in enumerate(steps_payload, start=1):
            validation = self.validate_action(item if isinstance(item, dict) else {}, source=source)
            if validation.action is not None:
                action = validation.action
                actions.append(
                    ManualActionSpec(
                        action_id=str(action.action_id or f"action_{idx:03d}"),
                        primitive=action.primitive,
                        args=dict(action.args),
                        rationale=action.rationale,
                        expected_observation=action.expected_observation,
                    )
                )
            errors.extend(f"step_{idx}_{error}" for error in validation.errors)
        if errors:
            return ManualValidationResult(valid=False, errors=errors, raw_payload=raw_payload)
        plan = ManualRescuePlan(
            plan_id=str(plan_payload.get("plan_id") or "manual_rescue_plan"),
            hypothesis=str(plan_payload.get("hypothesis") or ""),
            target_bottleneck=str(plan_payload.get("target_bottleneck") or ""),
            steps=tuple(actions),
            revise_conditions=tuple(str(x) for x in plan_payload.get("revise_conditions", []) if isinstance(x, (str, int, float))),
            abort_conditions=tuple(str(x) for x in plan_payload.get("abort_conditions", []) if isinstance(x, (str, int, float))),
            source=source,
        )
        return ManualValidationResult(valid=True, plan=plan, raw_payload=raw_payload)

    def validate_plan_state(self, plan: ManualRescuePlan, state: Dict[str, Any]) -> List[str]:
        """Validate that a guidance plan can progress from the current DCP state."""
        projected = dict(state)
        errors: List[str] = []
        for index, action in enumerate(plan.steps):
            step_errors = manual_action_state_errors(action, projected)
            if step_errors:
                errors.extend(f"step_{index}_{error}" for error in step_errors)
                continue
            projected = advance_manual_design_state(projected, action)
        return errors

    def _validate_args(self, primitive: ManualPrimitiveSpec, args: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        errors: List[str] = []
        forbidden = sorted(key for key in args if any(part in str(key).lower() for part in ("command", "tcl", "shell", "path", "file", "checkpoint")))
        if forbidden:
            errors.append(f"forbidden_arg_keys:{','.join(forbidden)}")
        if primitive.kind is ManualPrimitiveKind.MACRO_TCL:
            unknown = sorted(set(args))
            if unknown:
                errors.append(f"macro_args_not_allowed:{','.join(unknown)}")
            return {}, errors
        unknown = sorted(set(args) - {"directive"})
        if unknown:
            errors.append(f"unknown_arg_keys:{','.join(unknown)}")
        directive = str(args.get("directive") or "").strip()
        if not directive:
            errors.append("missing_directive")
            return {}, errors
        if directive not in primitive.allowed_directives:
            errors.append(f"directive_not_allowed:{directive}")
            return {}, errors
        return {"directive": directive}, errors


def manual_action_state_errors(action: ManualActionSpec, state: Dict[str, Any]) -> List[str]:
    """Reject manual actions that cannot change the current implementation state.

    Rescue starts from a routed checkpoint.  Re-running place or route against
    that unchanged checkpoint was the failure mode seen in the first APRIL
    batch, so state preconditions are explicit rather than left to Vivado's
    no-op behaviour.
    """
    placement = str(state.get("placement") or "placed")
    routing = str(state.get("routing") or "routed")
    primitive = action.primitive
    if primitive == "manual.unroute_all" and routing != "routed":
        return ["manual_unroute_requires_routed_design"]
    if primitive == "manual.unplace_all" and (placement != "placed" or routing != "unrouted"):
        return ["manual_unplace_requires_placed_unrouted_design"]
    if primitive == "manual.place_design" and placement != "unplaced":
        return ["manual_place_requires_unplaced_design"]
    if primitive == "manual.route_design" and (placement != "placed" or routing != "unrouted"):
        return ["manual_route_requires_placed_unrouted_design"]
    if primitive == "manual.phys_opt_design" and placement != "placed":
        return ["manual_phys_opt_requires_placed_design"]
    return []


def advance_manual_design_state(state: Dict[str, Any], action: ManualActionSpec) -> Dict[str, Any]:
    """Return the placement/routing state expected after one valid action."""
    projected = dict(state)
    if action.primitive == "manual.unroute_all":
        projected["routing"] = "unrouted"
    elif action.primitive == "manual.unplace_all":
        projected["placement"] = "unplaced"
        projected["routing"] = "unrouted"
    elif action.primitive == "manual.place_design":
        projected["placement"] = "placed"
        projected["routing"] = "unrouted"
    elif action.primitive == "manual.route_design":
        projected["routing"] = "routed"
    return projected


class ManualRescueSynthesisController:
    """Generate a manual plan and one manual action at a time."""

    def __init__(
        self,
        *,
        config: Any,
        catalog: Optional[ManualPrimitiveCatalog] = None,
        initial_cost_usd: Optional[float] = None,
        generator: Optional[InterventionProposalGenerator] = None,
    ) -> None:
        self.config = config
        self.catalog = catalog or ManualPrimitiveCatalog.default()
        self.validator = ManualActionValidator(self.catalog)
        self.initial_cost_usd = _optional_float(initial_cost_usd)
        self.generator = generator

    def usage_summary(self) -> Dict[str, Any]:
        return self.generator.usage_summary() if self.generator is not None else {"total_llm_calls": 0}

    def _cost_budget_reached(self) -> bool:
        budget = _optional_float(getattr(self.config, "llm_cost_budget_usd", None))
        if budget is None:
            return False
        spent = _sum_optional_float(self.initial_cost_usd, _optional_float(self.usage_summary().get("total_cost_usd")))
        return spent is not None and spent >= budget

    def synthesize_plan_mock(self, *, context: Dict[str, Any]) -> ManualSynthesisResult:
        full_plan = deterministic_manual_plan()
        state = context.get("manual_design_state") if isinstance(context.get("manual_design_state"), dict) else {}
        remaining_steps = full_plan.steps[-1:]
        start_index = min(max(0, int(context.get("action_index", 0) or 0)), len(full_plan.steps) - 1)
        for index in range(start_index, len(full_plan.steps)):
            candidate_steps = full_plan.steps[index:]
            candidate = ManualRescuePlan(
                plan_id=f"{full_plan.plan_id}_from_{index:03d}",
                hypothesis=full_plan.hypothesis,
                target_bottleneck=full_plan.target_bottleneck,
                steps=candidate_steps,
                revise_conditions=full_plan.revise_conditions,
                abort_conditions=full_plan.abort_conditions,
                source=full_plan.source,
            )
            if not self.validator.validate_plan_state(candidate, state):
                remaining_steps = candidate_steps
                break
        payload = ManualRescuePlan(
            plan_id=f"{full_plan.plan_id}_from_state",
            hypothesis=full_plan.hypothesis,
            target_bottleneck=full_plan.target_bottleneck,
            steps=remaining_steps,
            revise_conditions=full_plan.revise_conditions,
            abort_conditions=full_plan.abort_conditions,
            source=full_plan.source,
        ).to_dict()
        validation = self.validator.validate_plan(payload, source="mock_manual_plan")
        return ManualSynthesisResult(plan=validation.plan, validation=validation, audit=self._audit("mock_plan", context, payload))

    def synthesize_action_mock(self, *, context: Dict[str, Any], plan: Optional[ManualRescuePlan], action_index: int) -> ManualSynthesisResult:
        steps = list(plan.steps) if plan is not None else list(deterministic_manual_plan().steps)
        state = context.get("manual_design_state") if isinstance(context.get("manual_design_state"), dict) else {}
        planned = steps[min(max(0, action_index), len(steps) - 1)]
        action = planned if not manual_action_state_errors(planned, state) else next(
            (step for step in steps if not manual_action_state_errors(step, state)),
            planned,
        )
        validation = self.validator.validate_action(action.to_dict(), source="mock_manual_action")
        return ManualSynthesisResult(action=validation.action, validation=validation, audit=self._audit("mock_action", context, action.to_dict()))

    def synthesize_plan(self, *, context: Dict[str, Any]) -> ManualSynthesisResult:
        payload, audit = self._call_llm(kind="plan", context=context, plan=None, action_index=None)
        validation = self.validator.validate_plan(payload, source="llm_manual_plan")
        return ManualSynthesisResult(plan=validation.plan, validation=validation, audit={**audit, "validation": validation.to_dict()})

    def synthesize_action(self, *, context: Dict[str, Any], plan: Optional[ManualRescuePlan], action_index: int) -> ManualSynthesisResult:
        payload, audit = self._call_llm(kind="action", context=context, plan=plan, action_index=action_index)
        validation = self.validator.validate_action(payload, source="llm_manual_action")
        return ManualSynthesisResult(action=validation.action, validation=validation, audit={**audit, "validation": validation.to_dict()})

    def _call_llm(self, *, kind: str, context: Dict[str, Any], plan: Optional[ManualRescuePlan], action_index: Optional[int]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        audit = {
            "schema_version": "manual_rescue_synthesis_audit.v1",
            "kind": kind,
            "mode": "external_adapter",
            "context": context,
            "catalog": self.catalog.to_dict(),
        }
        if self.generator is None:
            return {}, {**audit, "error": "external_generator_not_configured"}
        if self._cost_budget_reached():
            return {}, {**audit, "error": "llm_cost_budget_reached", "usage": self.usage_summary()}
        try:
            generated = self.generator.generate_manual(
                kind=kind,
                context=context,
                catalog=self.catalog.to_dict(),
                plan=plan.to_dict() if plan is not None else None,
                action_index=action_index,
            )
            audit.update(generated.audit)
            audit["usage"] = self.usage_summary()
            return dict(generated.payload), audit
        except Exception as exc:
            return {}, {**audit, "error": f"llm_manual_rescue_failed:{type(exc).__name__}: {exc}"}

    def _audit(self, mode: str, context: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "schema_version": "manual_rescue_synthesis_audit.v1",
            "mode": mode,
            "context": context,
            "catalog": self.catalog.to_dict(),
            "payload": payload,
        }


def deterministic_manual_plan() -> ManualRescuePlan:
    return ManualRescuePlan(
        plan_id="manual_place_reset_retime_v1",
        hypothesis="Reset routing/placement pressure and test retiming-oriented physical optimization.",
        target_bottleneck="timing_or_route_pressure",
        steps=(
            ManualActionSpec("action_000", "manual.unroute_all", {}, "Clear routed nets before manual placement repair.", "route status becomes dirty but design remains open."),
            ManualActionSpec("action_001", "manual.unplace_all", {}, "Clear placement so Explore can rebuild placement.", "placement is reset for a fresh place attempt."),
            ManualActionSpec("action_002", "manual.place_design", {"directive": "Explore"}, "Try a broader placement search.", "WNS should not become dramatically worse."),
            ManualActionSpec("action_003", "manual.phys_opt_design", {"directive": "AddRetime"}, "Try retiming after placement.", "critical path logic/register balance may improve."),
            ManualActionSpec("action_004", "manual.route_design", {"directive": "Explore"}, "Route the repaired placement.", "route status should become clean."),
            ManualActionSpec("action_005", "manual.phys_opt_design", {"directive": "Explore"}, "Polish post-route timing.", "final WNS/Fmax should improve if branch is useful."),
        ),
        revise_conditions=("validation becomes invalid with no output checkpoint", "WNS regresses sharply after a routed step"),
        abort_conditions=("Vivado action fails without output",),
        source="deterministic_manual_plan",
    )


def _optional_float(value: Any) -> Optional[float]:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _sum_optional_float(left: Optional[float], right: Optional[float]) -> Optional[float]:
    if left is None:
        return right
    if right is None:
        return left
    return left + right
