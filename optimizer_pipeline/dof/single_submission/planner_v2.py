"""WP-013A four-turn planner with batched Q and a reserved final turn."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable, Iterable, Mapping

from .contract_v2 import (
    MAX_FEEDBACK,
    MAX_QUERIES,
    MAX_TURNS,
    PLANNER_MESSAGE_V2,
    QUERY_NAMES,
    compile_candidate_v2,
    schema_ref_for_error,
)
from .language import CompileContext, CompiledProgram
from .observation import CommonObservation, CommonQuerySurface, ObservationError
from .planner import PlannerError, SystemBinding
from .specs import canonical_json_bytes


_SYSTEM_FIELDS = frozenset(
    {
        "decision_id", "proposal_id", "request_id", "catalogue_version",
        "catalogue_hash", "root_binding", "context_binding", "provider", "model", "route",
    }
)
_EARLY_KINDS = frozenset(
    {"QUERY", "DRAFT", "VALIDATE", "COMMIT", "STOP", "NO_VALID_SUBMISSION"}
)
_FINAL_KINDS = frozenset({"COMMIT", "STOP", "NO_VALID_SUBMISSION"})


QueryProvider = Callable[
    [str, int],
    tuple[Iterable[Mapping[str, Any]], Iterable[str], float | None, str | None],
]


@dataclass(frozen=True, slots=True)
class PlannerTurnResultV2:
    status: str
    feedback: tuple[Mapping[str, Any], ...] = ()
    query_batch: Mapping[str, Any] | None = None
    compiled: CompiledProgram | None = None


@dataclass(slots=True)
class SingleSubmissionSessionV2:
    # Class-level protocol hooks let later representation-only epochs tighten
    # public limits without changing the accepted v2 behaviour.
    query_top_k_max = 64
    planner_message_version = PLANNER_MESSAGE_V2

    level: str
    observation: CommonObservation
    queries: CommonQuerySurface
    compile_context: CompileContext
    binding: SystemBinding
    query_provider: QueryProvider
    turns_used: int = 0
    query_requests_used: int = 0
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

    @property
    def turns_remaining(self) -> int:
        return max(0, MAX_TURNS - self.turns_used)

    @property
    def remaining_query_budget(self) -> int:
        return max(0, MAX_QUERIES - self.query_requests_used)

    @property
    def remaining_feedback_rounds(self) -> int:
        return max(0, MAX_FEEDBACK - self.feedback_rounds)

    @property
    def final_submission_turn(self) -> int:
        return 4

    def allowed_kinds_this_turn(self) -> tuple[str, ...]:
        if self.turns_used >= MAX_TURNS:
            return ()
        allowed = _FINAL_KINDS if self.turns_used == MAX_TURNS - 1 else _EARLY_KINDS
        return tuple(sorted(allowed))

    def public_status(self) -> dict[str, Any]:
        return {
            "turns_remaining": self.turns_remaining,
            "final_submission_turn": self.final_submission_turn,
            "allowed_kinds_this_turn": list(self.allowed_kinds_this_turn()),
            "remaining_query_budget": self.remaining_query_budget,
            "remaining_feedback_rounds": self.remaining_feedback_rounds,
        }

    def accept(self, message: Mapping[str, Any]) -> PlannerTurnResultV2:
        if self.terminal is not None:
            raise PlannerError("session_already_terminal")
        if self.turns_used >= MAX_TURNS:
            self.terminal = "NO_VALID_SUBMISSION"
            raise PlannerError("model_turn_budget_exhausted")
        if not isinstance(message, Mapping):
            return self._feedback("message_not_object", None, {})

        turn_number = self.turns_used + 1
        self.turns_used += 1
        if set(message) & _SYSTEM_FIELDS:
            return self._feedback("model_supplied_system_owned_field", "$", message, counted=True)
        kind = message.get("kind")
        if not isinstance(kind, str) or kind not in _EARLY_KINDS:
            return self._feedback("message_kind_invalid", "$.kind", message, counted=True)
        allowed = _FINAL_KINDS if turn_number == MAX_TURNS else _EARLY_KINDS
        if kind not in allowed:
            return self._feedback("kind_not_allowed_on_final_turn", "$.kind", message, counted=True)

        if kind == "QUERY":
            return self._query(message)
        if kind in {"STOP", "NO_VALID_SUBMISSION"}:
            if set(message) != {"kind"}:
                return self._feedback("terminal_message_fields_invalid", "$", message, counted=True)
            self.terminal = kind
            self.transcript.append(
                {"turn": turn_number, "kind": kind, "qor_feedback": False, "mutation": False}
            )
            return PlannerTurnResultV2(kind)
        return self._candidate(kind, message)

    def _query(self, message: Mapping[str, Any]) -> PlannerTurnResultV2:
        if set(message) != {"kind", "queries"} or not isinstance(message.get("queries"), list):
            return self._feedback("query_message_fields_invalid", "$", message, counted=True)
        requests = message["queries"]
        if not 1 <= len(requests) <= MAX_QUERIES:
            return self._feedback("query_batch_size_invalid", "$.queries", message, counted=True)
        if len(requests) > self.remaining_query_budget:
            return self._feedback("query_budget_exhausted", "$.queries", message, counted=True)
        normalized: list[tuple[str, int]] = []
        for index, request in enumerate(requests):
            location = f"$.queries[{index}]"
            if not isinstance(request, Mapping) or set(request) != {"name", "top_k"}:
                return self._feedback("query_item_fields_invalid", location, message, counted=True)
            name, top_k = request["name"], request["top_k"]
            if name not in QUERY_NAMES:
                return self._feedback("unknown_query", location + ".name", message, counted=True)
            if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= self.query_top_k_max:
                return self._feedback("query_top_k_out_of_range", location + ".top_k", message, counted=True)
            normalized.append((str(name), top_k))

        results = []
        try:
            for name, top_k in normalized:
                rows, call_ids, latency, missing = self.query_provider(name, top_k)
                result = self.queries.materialize(
                    query_name=name,
                    top_k=top_k,
                    private_rows=rows,
                    tool_call_ids=call_ids,
                    latency_seconds=latency,
                    missing_reason=missing,
                )
                results.append(result)
        except (ObservationError, TypeError, ValueError) as exc:
            reason = str(getattr(exc, "reason", "query_failed"))
            return self._feedback(reason, "$.queries", message, counted=True)

        self.query_requests_used += len(normalized)
        items = [
            {
                "request": {"name": name, "top_k": top_k},
                "result": result.public_payload(),
                "result_sha256": result.sha256,
                "latency_seconds": result.latency_seconds,
                "tool_call_ids": list(result.tool_call_ids),
            }
            for (name, top_k), result in zip(normalized, results, strict=True)
        ]
        core = {
            "schema_version": "dof.single-submission.query-batch.v2",
            "items": items,
            "individual_hashes": [result.sha256 for result in results],
        }
        batch = core | {
            "aggregate_sha256": hashlib.sha256(canonical_json_bytes(core)).hexdigest()
        }
        self.transcript.append(
            {
                "turn": self.turns_used,
                "kind": "QUERY",
                "query_count": len(normalized),
                "aggregate_sha256": batch["aggregate_sha256"],
                "individual_hashes": batch["individual_hashes"],
                "qor_feedback": False,
                "mutation": False,
            }
        )
        return PlannerTurnResultV2("QUERY_RESULT", query_batch=batch)

    def _candidate(self, kind: str, message: Mapping[str, Any]) -> PlannerTurnResultV2:
        if set(message) != {"kind", "candidate"} or not isinstance(message.get("candidate"), Mapping):
            return self._feedback("candidate_message_fields_invalid", "$.candidate", message, counted=True)
        candidate = message["candidate"]
        allowed = {"L2": {"M"}, "L3": {"M", "P"}, "L4": {"M", "P", "G"}}[self.level]
        if candidate.get("language") not in allowed:
            return self._feedback("language_not_allowed_at_level", "$.candidate.language", message, counted=True)
        try:
            compiled = self._compile_candidate(candidate)
        except Exception as exc:
            reason = str(getattr(exc, "reason", "construction_failure"))
            return self._feedback(reason, "$.candidate", message, counted=True)
        self.transcript.append(
            {
                "turn": self.turns_used,
                "kind": kind,
                "candidate_sha256": hashlib.sha256(canonical_json_bytes(candidate)).hexdigest(),
                "compiled_sha256": compiled.public_sha256,
                "feedback": [],
                "qor_feedback": False,
                "mutation": False,
            }
        )
        if kind in {"DRAFT", "VALIDATE"}:
            return PlannerTurnResultV2("CONSTRUCTION_VALID", compiled=compiled)
        self.valid_commits += 1
        if self.valid_commits != 1:
            raise PlannerError("more_than_one_valid_commit")
        self.committed = compiled
        self.terminal = "COMMITTED"
        return PlannerTurnResultV2("COMMITTED", compiled=compiled)

    def _compile_candidate(self, candidate: Mapping[str, Any]) -> CompiledProgram:
        return compile_candidate_v2(candidate, self.compile_context)

    def _feedback(
        self,
        error_code: str,
        location: str | None,
        message: Mapping[str, Any],
        *,
        counted: bool = False,
    ) -> PlannerTurnResultV2:
        if not counted:
            self.turns_used += 1
        self.feedback_rounds += 1
        feedback = {
            "error_code": error_code,
            "location": location,
            "expected_shape_or_schema_ref": self._schema_ref_for_error(error_code),
            "remaining_turns": self.turns_remaining,
            "remaining_feedback_rounds": self.remaining_feedback_rounds,
            "remaining_query_budget": self.remaining_query_budget,
        }
        terminal = self.feedback_rounds > MAX_FEEDBACK or self.turns_used >= MAX_TURNS
        if terminal:
            self.terminal = "NO_VALID_SUBMISSION"
        self.transcript.append(
            {
                "turn": self.turns_used,
                "kind": str(message.get("kind", "INVALID")),
                "feedback": [feedback],
                "qor_feedback": False,
                "mutation": False,
            }
        )
        return PlannerTurnResultV2(
            "NO_VALID_SUBMISSION" if terminal else "CONSTRUCTION_INVALID", (feedback,)
        )

    def evidence(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.planner_message_version,
            "level": self.level,
            "binding": self.binding.public(),
            "observation_sha256": self.observation.sha256,
            "query_catalogue": self.queries.catalogue,
            "turns_used": self.turns_used,
            "query_requests_used": self.query_requests_used,
            "feedback_rounds": self.feedback_rounds,
            "valid_commits": self.valid_commits,
            "terminal": self.terminal,
            "committed_sha256": self.committed.public_sha256 if self.committed else None,
            "precommit_qor_feedback_count": 0,
            "precommit_mutation_count": 0,
            "postcommit_resubmission_count": 0,
            "transcript": list(self.transcript),
        }
        return payload | {
            "evidence_sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        }

    def _schema_ref_for_error(self, error_code: str) -> str:
        return schema_ref_for_error(error_code)


def result_public(result: PlannerTurnResultV2) -> dict[str, Any]:
    row: dict[str, Any] = {
        "status": result.status,
        "feedback": [dict(item) for item in result.feedback],
    }
    if result.query_batch is not None:
        row["query_batch"] = json.loads(canonical_json_bytes(result.query_batch))
    if result.compiled is not None:
        row["compiled"] = result.compiled.public_payload()
        row["compiled_sha256"] = result.compiled.public_sha256
    return row


__all__ = [
    "PlannerTurnResultV2", "SingleSubmissionSessionV2", "result_public",
]
