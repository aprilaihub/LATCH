"""Bounded one-COMMIT planning state machine for L2/L3/L4."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable, Iterable, Mapping

from .language import CompileContext, CompiledProgram, compile_g, compile_macro, compile_p
from .observation import CommonObservation, CommonQuerySurface, ObservationError, QueryResult
from .specs import SINGLE_SUBMISSION_PLANNER_V1, canonical_json_bytes


_SYSTEM_FIELDS = frozenset({"decision_id", "proposal_id", "request_id", "catalogue_version", "catalogue_hash", "root_binding", "context_binding", "provider", "model", "route"})
_KINDS = frozenset({"QUERY", "DRAFT", "VALIDATE", "COMMIT", "STOP", "NO_VALID_SUBMISSION"})


class PlannerError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class SystemBinding:
    decision_id: str
    proposal_id: str
    request_id: str
    catalogue_version: str
    catalogue_hash: str
    root_binding: str
    context_binding: str
    provider: str
    model: str
    route: str

    @classmethod
    def create(
        cls,
        *,
        observation_hash: str,
        root_binding: str,
        catalogue_version: str,
        catalogue_hash: str,
        provider: str,
        model: str,
        route: str,
        session_nonce: str,
    ) -> "SystemBinding":
        seed = hashlib.sha256(canonical_json_bytes({"observation_hash": observation_hash, "root_binding": root_binding, "catalogue_hash": catalogue_hash, "provider": provider, "model": model, "route": route, "nonce": session_nonce})).hexdigest()
        return cls("decision-" + seed[:20], "proposal-" + seed[20:40], "request-" + seed[40:60], catalogue_version, catalogue_hash, root_binding, observation_hash, provider, model, route)

    def public(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class PlannerTurnResult:
    status: str
    feedback: tuple[str, ...] = ()
    query_result: QueryResult | None = None
    compiled: CompiledProgram | None = None


QueryProvider = Callable[[str, int], tuple[Iterable[Mapping[str, Any]], Iterable[str], float | None, str | None]]


@dataclass(slots=True)
class SingleSubmissionSession:
    level: str
    observation: CommonObservation
    queries: CommonQuerySurface
    compile_context: CompileContext
    binding: SystemBinding
    query_provider: QueryProvider
    turns_used: int = 0
    feedback_rounds: int = 0
    valid_commits: int = 0
    terminal: str | None = None
    committed: CompiledProgram | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.level not in {"L2", "L3", "L4"}:
            raise ValueError("planner_level_not_l2_l3_l4")
        if self.observation.sha256 != self.binding.context_binding:
            raise ValueError("planner_observation_binding_mismatch")

    def accept(self, message: Mapping[str, Any]) -> PlannerTurnResult:
        if self.terminal is not None:
            raise PlannerError("session_already_terminal")
        if self.turns_used >= 4:
            self.terminal = "NO_VALID_SUBMISSION"
            raise PlannerError("model_turn_budget_exhausted")
        if not isinstance(message, Mapping):
            raise PlannerError("message_not_object")
        if set(message) & _SYSTEM_FIELDS:
            return self._feedback("model_supplied_system_owned_field", message)
        if set(message) - {"kind", "query", "candidate"} or "kind" not in message:
            return self._feedback("message_schema_invalid", message)
        kind = message["kind"]
        if kind not in _KINDS:
            return self._feedback("message_kind_invalid", message)
        self.turns_used += 1
        if kind == "QUERY":
            if set(message) != {"kind", "query"} or not isinstance(message["query"], Mapping):
                return self._feedback("query_message_schema_invalid", message, turn_already_counted=True)
            query = message["query"]
            if set(query) != {"name", "top_k"}:
                return self._feedback("query_schema_invalid", message, turn_already_counted=True)
            try:
                rows, call_ids, latency, missing = self.query_provider(str(query["name"]), query["top_k"])
                result = self.queries.materialize(query_name=str(query["name"]), top_k=query["top_k"], private_rows=rows, tool_call_ids=call_ids, latency_seconds=latency, missing_reason=missing)
            except (ObservationError, TypeError, ValueError) as exc:
                reason = getattr(exc, "reason", "query_failed")
                return self._feedback(str(reason), message, turn_already_counted=True)
            self.transcript.append({"turn": self.turns_used, "kind": "QUERY", "request": dict(query), "result_sha256": result.sha256, "returned": len(result.rows), "qor_feedback": False, "mutation": False})
            return PlannerTurnResult("QUERY_RESULT", query_result=result)
        if kind in {"STOP", "NO_VALID_SUBMISSION"}:
            if set(message) != {"kind"}:
                return self._feedback("terminal_message_schema_invalid", message, turn_already_counted=True)
            self.terminal = str(kind)
            self.transcript.append({"turn": self.turns_used, "kind": kind, "qor_feedback": False, "mutation": False})
            return PlannerTurnResult(str(kind))
        if set(message) != {"kind", "candidate"} or not isinstance(message["candidate"], Mapping):
            return self._feedback("candidate_message_schema_invalid", message, turn_already_counted=True)
        candidate = message["candidate"]
        try:
            compiled = self._compile(candidate)
        except Exception as exc:
            reason = getattr(exc, "reason", "construction_failure")
            return self._feedback(str(reason), message, turn_already_counted=True)
        self.transcript.append({"turn": self.turns_used, "kind": kind, "candidate_sha256": hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(), "compiled_sha256": compiled.public_sha256, "feedback": [], "qor_feedback": False, "mutation": False})
        if kind in {"DRAFT", "VALIDATE"}:
            return PlannerTurnResult("CONSTRUCTION_VALID", compiled=compiled)
        self.valid_commits += 1
        if self.valid_commits != 1:
            raise PlannerError("more_than_one_valid_commit")
        self.committed = compiled
        self.terminal = "COMMITTED"
        return PlannerTurnResult("COMMITTED", compiled=compiled)

    def _compile(self, candidate: Mapping[str, Any]) -> CompiledProgram:
        language = candidate.get("language")
        allowed = {"L2": {"M"}, "L3": {"M", "P"}, "L4": {"M", "P", "G"}}[self.level]
        if language not in allowed:
            raise PlannerError("language_not_allowed_at_level")
        if language == "M":
            if set(candidate) != {"language", "macro"}:
                raise PlannerError("macro_candidate_schema_invalid")
            return compile_macro(str(candidate["macro"]).removeprefix("macro."), self.compile_context)
        return compile_p(candidate, self.compile_context) if language == "P" else compile_g(candidate, self.compile_context)

    def _feedback(self, reason: str, message: Mapping[str, Any], *, turn_already_counted: bool = False) -> PlannerTurnResult:
        if not turn_already_counted:
            self.turns_used += 1
        self.feedback_rounds += 1
        if self.feedback_rounds > 3:
            self.terminal = "NO_VALID_SUBMISSION"
            return PlannerTurnResult("NO_VALID_SUBMISSION", ("construction_feedback_budget_exhausted",))
        self.transcript.append({"turn": self.turns_used, "kind": str(message.get("kind", "INVALID")), "feedback": [reason], "qor_feedback": False, "mutation": False})
        return PlannerTurnResult("CONSTRUCTION_INVALID", (reason,))

    def evidence(self) -> dict[str, Any]:
        payload = {
            "schema_version": SINGLE_SUBMISSION_PLANNER_V1,
            "level": self.level,
            "binding": self.binding.public(),
            "observation_sha256": self.observation.sha256,
            "query_catalogue": self.queries.catalogue,
            "turns_used": self.turns_used,
            "feedback_rounds": self.feedback_rounds,
            "valid_commits": self.valid_commits,
            "terminal": self.terminal,
            "committed_sha256": self.committed.public_sha256 if self.committed else None,
            "precommit_qor_feedback_count": 0,
            "precommit_mutation_count": 0,
            "postcommit_resubmission_count": 0,
            "transcript": list(self.transcript),
        }
        return payload | {"evidence_sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}


__all__ = ["PlannerError", "PlannerTurnResult", "SingleSubmissionSession", "SystemBinding"]
