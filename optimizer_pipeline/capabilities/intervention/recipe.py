"""Guarded recipe synthesis from allowlisted implementation primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

from optimizer_pipeline.models import to_jsonable
from optimizer_pipeline.capabilities.intervention.generation import InterventionProposalGenerator


class PrimitiveKind(str, Enum):
    MACRO_TCL = "macro_tcl"
    TYPED_VIVADO = "typed_vivado"
    OPTIMIZER_FLOW = "optimizer_flow"
    SANDBOX_TCL = "sandbox_tcl"


@dataclass(frozen=True)
class PrimitiveSpec:
    name: str
    kind: PrimitiveKind
    mutates_design: bool
    tool_name: Optional[str] = None
    flow_name: Optional[str] = None
    allowed_directives: Tuple[str, ...] = ()
    macro_command: Optional[str] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True)
class RecipeStep:
    step_id: str
    primitive: str
    args: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True)
class RecipeSpec:
    recipe_id: str
    summary: str
    steps: Tuple[RecipeStep, ...]
    source: str = "llm_synthesis"
    risk_notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class RecipeValidationResult:
    valid: bool
    recipe: Optional[RecipeSpec] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rejected_fields: List[str] = field(default_factory=list)
    raw_payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)


@dataclass
class RecipeSynthesisResult:
    recipe: Optional[RecipeSpec]
    validation: RecipeValidationResult
    audit: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recipe": self.recipe.to_dict() if self.recipe else None,
            "validation": self.validation.to_dict(),
            "audit": to_jsonable(self.audit),
        }


class PrimitiveCatalog:
    """Catalog of mutating primitives available to recipe synthesis."""

    def __init__(self, primitives: Iterable[PrimitiveSpec]) -> None:
        self._primitives = {primitive.name: primitive for primitive in primitives}

    @classmethod
    def default(cls) -> "PrimitiveCatalog":
        return cls(
            [
                PrimitiveSpec(
                    name="macro.unroute_design",
                    kind=PrimitiveKind.MACRO_TCL,
                    mutates_design=True,
                    macro_command="route_design -unroute",
                    description="Unroute all nets using a fixed macro Tcl command.",
                ),
                PrimitiveSpec(
                    name="macro.unplace_design",
                    kind=PrimitiveKind.MACRO_TCL,
                    mutates_design=True,
                    macro_command="place_design -unplace",
                    description="Remove all placement using a fixed macro Tcl command.",
                ),
                PrimitiveSpec(
                    name="vivado.place_design",
                    kind=PrimitiveKind.TYPED_VIVADO,
                    mutates_design=True,
                    tool_name="place_design",
                    allowed_directives=("Default", "Explore"),
                    description="Run Vivado place_design with an allowlisted directive.",
                ),
                PrimitiveSpec(
                    name="vivado.phys_opt_design",
                    kind=PrimitiveKind.TYPED_VIVADO,
                    mutates_design=True,
                    tool_name="phys_opt_design",
                    allowed_directives=("Explore", "AddRetime", "AlternateFlowWithRetiming"),
                    description="Run Vivado phys_opt_design with an allowlisted directive.",
                ),
                PrimitiveSpec(
                    name="vivado.route_design",
                    kind=PrimitiveKind.TYPED_VIVADO,
                    mutates_design=True,
                    tool_name="route_design",
                    allowed_directives=("Default", "Explore"),
                    description="Run Vivado route_design with an allowlisted directive.",
                ),
                PrimitiveSpec(
                    name="flow.phys_opt",
                    kind=PrimitiveKind.OPTIMIZER_FLOW,
                    mutates_design=True,
                    flow_name="phys_opt",
                    description="Run the existing phys_opt optimizer flow.",
                ),
                PrimitiveSpec(
                    name="flow.pblock",
                    kind=PrimitiveKind.OPTIMIZER_FLOW,
                    mutates_design=True,
                    flow_name="pblock",
                    description="Run the existing pblock optimizer flow.",
                ),
                PrimitiveSpec(
                    name="flow.auto_pblock",
                    kind=PrimitiveKind.OPTIMIZER_FLOW,
                    mutates_design=True,
                    flow_name="auto_pblock",
                    description="Run the existing auto_pblock optimizer flow.",
                ),
                PrimitiveSpec(
                    name="flow.lut_cone",
                    kind=PrimitiveKind.OPTIMIZER_FLOW,
                    mutates_design=True,
                    flow_name="lut_cone",
                    description="Run the existing lut_cone optimizer flow.",
                ),
                PrimitiveSpec(
                    name="flow.retime",
                    kind=PrimitiveKind.OPTIMIZER_FLOW,
                    mutates_design=True,
                    flow_name="retime",
                    description="Run the existing retime optimizer flow.",
                ),
                PrimitiveSpec(
                    name="flow.fanout",
                    kind=PrimitiveKind.OPTIMIZER_FLOW,
                    mutates_design=True,
                    flow_name="fanout",
                    description="Run the existing fanout optimizer flow.",
                ),
                PrimitiveSpec(
                    name="flow.memory_path_opt",
                    kind=PrimitiveKind.OPTIMIZER_FLOW,
                    mutates_design=True,
                    flow_name="memory_path_opt",
                    description="Run the existing memory_path_opt optimizer flow.",
                ),
                PrimitiveSpec(
                    name="flow.hardblock_relocate",
                    kind=PrimitiveKind.OPTIMIZER_FLOW,
                    mutates_design=True,
                    flow_name="hardblock_relocate",
                    description="Run the existing hardblock_relocate optimizer flow.",
                ),
                PrimitiveSpec(
                    name="flow.congestion_spread",
                    kind=PrimitiveKind.OPTIMIZER_FLOW,
                    mutates_design=True,
                    flow_name="congestion_spread",
                    description="Run the existing congestion_spread optimizer flow.",
                ),
                PrimitiveSpec(
                    name="flow.net_swap",
                    kind=PrimitiveKind.OPTIMIZER_FLOW,
                    mutates_design=True,
                    flow_name="net_swap",
                    description="Run the existing net_swap optimizer flow.",
                ),
            ]
        )

    def get(self, name: str) -> Optional[PrimitiveSpec]:
        return self._primitives.get(name)

    def names(self) -> List[str]:
        return sorted(self._primitives)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "primitive_catalog.v1",
            "primitives": [self._primitives[name].to_dict() for name in self.names()],
            "sandbox_tcl_available": False,
        }


class RecipeValidator:
    def __init__(
        self,
        catalog: Optional[PrimitiveCatalog] = None,
        *,
        max_steps: int = 8,
        allow_sandbox_tcl: bool = False,
    ) -> None:
        self.catalog = catalog or PrimitiveCatalog.default()
        self.max_steps = max(1, int(max_steps))
        self.allow_sandbox_tcl = bool(allow_sandbox_tcl)

    def validate(self, payload: Dict[str, Any], *, source: str = "llm_synthesis") -> RecipeValidationResult:
        if not isinstance(payload, dict):
            return RecipeValidationResult(valid=False, errors=["payload_not_object"], raw_payload={})
        raw_payload = dict(payload)
        recipe_payload = payload.get("recipe") if isinstance(payload.get("recipe"), dict) else payload
        steps_payload = recipe_payload.get("steps")
        if not isinstance(steps_payload, list):
            reason = "missing_recipe_steps"
            if isinstance(payload.get("candidate_priorities"), list):
                reason = "old_candidate_priorities_schema_is_not_a_recipe"
            return RecipeValidationResult(valid=False, errors=[reason], raw_payload=raw_payload)
        if not steps_payload:
            return RecipeValidationResult(valid=False, errors=["recipe_has_no_steps"], raw_payload=raw_payload)
        if len(steps_payload) > self.max_steps:
            return RecipeValidationResult(
                valid=False,
                errors=[f"too_many_steps:{len(steps_payload)}>{self.max_steps}"],
                raw_payload=raw_payload,
            )

        rejected_fields = sorted(set(recipe_payload) - {"recipe_id", "summary", "steps", "risk_notes"})
        steps: List[RecipeStep] = []
        errors: List[str] = []
        warnings: List[str] = []
        mutating_steps = 0
        for idx, raw_step in enumerate(steps_payload, start=1):
            if not isinstance(raw_step, dict):
                errors.append(f"step_{idx}_not_object")
                continue
            primitive_name = str(raw_step.get("primitive") or raw_step.get("action") or raw_step.get("name") or "").strip()
            if not primitive_name:
                errors.append(f"step_{idx}_missing_primitive")
                continue
            if primitive_name in {"run_tcl", "shell", "bash", "write_checkpoint"}:
                errors.append(f"step_{idx}_forbidden_primitive:{primitive_name}")
                continue
            primitive = self.catalog.get(primitive_name)
            if primitive is None:
                errors.append(f"step_{idx}_unknown_primitive:{primitive_name}")
                continue
            if primitive.kind is PrimitiveKind.SANDBOX_TCL and not self.allow_sandbox_tcl:
                errors.append(f"step_{idx}_sandbox_tcl_disabled")
                continue
            args = raw_step.get("args") if isinstance(raw_step.get("args"), dict) else {}
            step_args, step_errors, step_warnings = self._validate_args(idx, primitive, args)
            errors.extend(step_errors)
            warnings.extend(step_warnings)
            if primitive.mutates_design:
                mutating_steps += 1
            steps.append(
                RecipeStep(
                    step_id=str(raw_step.get("step_id") or f"step_{idx:02d}"),
                    primitive=primitive_name,
                    args=step_args,
                    rationale=str(raw_step.get("rationale") or raw_step.get("reason") or ""),
                )
            )
        if mutating_steps <= 0:
            errors.append("recipe_has_no_mutating_steps")
        if errors:
            return RecipeValidationResult(
                valid=False,
                errors=errors,
                warnings=warnings,
                rejected_fields=rejected_fields,
                raw_payload=raw_payload,
            )
        recipe = RecipeSpec(
            recipe_id=str(recipe_payload.get("recipe_id") or "synthesized_recipe_v1"),
            summary=str(recipe_payload.get("summary") or "LLM synthesized recipe from allowlisted primitives."),
            steps=tuple(steps),
            source=source,
            risk_notes=tuple(str(x) for x in recipe_payload.get("risk_notes", []) if isinstance(x, (str, int, float))),
        )
        return RecipeValidationResult(
            valid=True,
            recipe=recipe,
            warnings=warnings,
            rejected_fields=rejected_fields,
            raw_payload=raw_payload,
        )

    def _validate_args(self, idx: int, primitive: PrimitiveSpec, args: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[str]]:
        errors: List[str] = []
        warnings: List[str] = []
        if not isinstance(args, dict):
            return {}, [f"step_{idx}_args_not_object"], warnings
        forbidden = sorted(key for key in args if any(part in str(key).lower() for part in ("command", "tcl", "shell", "path", "file")))
        if forbidden:
            errors.append(f"step_{idx}_forbidden_arg_keys:{','.join(forbidden)}")
        if primitive.kind is PrimitiveKind.MACRO_TCL:
            unknown = sorted(set(args))
            if unknown:
                errors.append(f"step_{idx}_macro_args_not_allowed:{','.join(unknown)}")
            return {}, errors, warnings
        if primitive.kind is PrimitiveKind.TYPED_VIVADO:
            unknown = sorted(set(args) - {"directive"})
            if unknown:
                errors.append(f"step_{idx}_unknown_arg_keys:{','.join(unknown)}")
            directive = str(args.get("directive") or "").strip()
            if not directive:
                errors.append(f"step_{idx}_missing_directive")
                return {}, errors, warnings
            if directive not in primitive.allowed_directives:
                errors.append(f"step_{idx}_directive_not_allowed:{directive}")
                return {}, errors, warnings
            return {"directive": directive}, errors, warnings
        if primitive.kind is PrimitiveKind.OPTIMIZER_FLOW:
            unknown = sorted(set(args))
            if unknown:
                errors.append(f"step_{idx}_flow_args_not_allowed:{','.join(unknown)}")
            if not primitive.flow_name:
                errors.append(f"step_{idx}_flow_name_missing")
            return {}, errors, warnings
        errors.append(f"step_{idx}_unsupported_primitive_kind:{primitive.kind.value}")
        return {}, errors, warnings


def deterministic_place_reset_recipe(*, source: str = "deterministic_fallback") -> RecipeSpec:
    return RecipeSpec(
        recipe_id="synth_place_reset_retime_v1",
        summary="Deterministic place reset and retime recipe.",
        source=source,
        risk_notes=("Large placement-space reset; use as an experimental rescue branch.",),
        steps=(
            RecipeStep("step_01", "macro.unroute_design", rationale="Reset routing before exploring a new placement."),
            RecipeStep("step_02", "macro.unplace_design", rationale="Reset placement to open a larger search space."),
            RecipeStep("step_03", "vivado.place_design", {"directive": "Explore"}, "Explore a fresh placement."),
            RecipeStep("step_04", "vivado.phys_opt_design", {"directive": "AddRetime"}, "Try timing-focused retiming after placement."),
            RecipeStep("step_05", "vivado.route_design", {"directive": "Explore"}, "Route with a broader exploration directive."),
            RecipeStep("step_06", "vivado.phys_opt_design", {"directive": "Explore"}, "Polish timing after route."),
        ),
    )


class RecipeSynthesisController:
    """Validate one untrusted recipe proposal from an injected generator."""

    def __init__(
        self,
        *,
        config: Any,
        catalog: Optional[PrimitiveCatalog] = None,
        max_steps: int = 8,
        fallback_policy: str = "deterministic",
        generator: Optional[InterventionProposalGenerator] = None,
    ) -> None:
        self.config = config
        self.catalog = catalog or PrimitiveCatalog.default()
        self.validator = RecipeValidator(self.catalog, max_steps=max_steps, allow_sandbox_tcl=False)
        self.fallback_policy = str(fallback_policy or "deterministic").strip().lower()
        self.generator = generator

    def synthesize_mock(self, *, context: Dict[str, Any]) -> RecipeSynthesisResult:
        recipe = deterministic_place_reset_recipe(source="mock_synthesis")
        validation = self.validator.validate(recipe.to_dict(), source="mock_synthesis")
        return RecipeSynthesisResult(
            recipe=validation.recipe,
            validation=validation,
            audit={
                "schema_version": "recipe_synthesis_audit.v1",
                "mode": "mock",
                "context": context,
                "catalog": self.catalog.to_dict(),
                "fallback_policy": self.fallback_policy,
            },
        )

    def synthesize(self, *, context: Dict[str, Any]) -> RecipeSynthesisResult:
        audit: Dict[str, Any] = {
            "schema_version": "recipe_synthesis_audit.v1",
            "mode": "external_adapter",
            "context": context,
            "catalog": self.catalog.to_dict(),
            "fallback_policy": self.fallback_policy,
        }
        if self.generator is None:
            validation = RecipeValidationResult(valid=False, errors=["external_generator_not_configured"])
            audit["error"] = "external_generator_not_configured"
            return RecipeSynthesisResult(recipe=None, validation=validation, audit=audit)
        try:
            generated = self.generator.generate_recipe(
                context=context,
                catalog=self.catalog.to_dict(),
            )
            payload = dict(generated.payload)
            audit.update(generated.audit)
        except Exception as exc:
            validation = RecipeValidationResult(valid=False, errors=[f"llm_synthesis_failed:{type(exc).__name__}: {exc}"])
            audit["error"] = validation.errors[0]
            return RecipeSynthesisResult(recipe=None, validation=validation, audit=audit)
        validation = self.validator.validate(payload, source="llm_synthesis")
        return RecipeSynthesisResult(recipe=validation.recipe, validation=validation, audit=audit)
