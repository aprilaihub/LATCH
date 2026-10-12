"""Executable compact retrieval adapter over the frozen WP-015 session."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from .single_submission.planner_v2 import result_public
from .wp016 import (
    RetrievalBudgetState, compact_protocol_definition, sha256,
)


COMPACT_SESSION_ADAPTER_VERSION = "wp015-fork01.compact-session-adapter.v1"
RETRIEVAL_ENDPOINTS = frozenset({
    "LIST_CAPABILITIES", "GET_ACTION_SPEC", "GET_SCHEMA_FRAGMENT", "GET_EXAMPLE",
})
SEMANTIC_ENDPOINTS = frozenset({
    "QUERY_DESIGN", "VALIDATE_PROGRAM", "COMMIT", "NO_VALID_SUBMISSION",
})


@dataclass(slots=True)
class CompactSessionAdapter:
    """Own compact budget state while reusing frozen planner semantics."""

    state: RetrievalBudgetState
    frozen_session: Any
    protocol: Mapping[str, Any] = field(default_factory=compact_protocol_definition)
    response_validation_count: int = 0
    postterminal_certificate_feedback_count: int = 0
    semantic_events: list[dict[str, Any]] = field(default_factory=list)

    def begin_turn(self, request: Mapping[str, Any], *, output_tokens: int) -> dict[str, Any]:
        started = self.state.begin_turn()
        if started["status"] != "TURN_STARTED":
            self._validate_response(started)
            return started
        accounted = self.state.record_output_tokens(output_tokens)
        if accounted["status"] != "OUTPUT_ACCOUNTED":
            self._validate_response(accounted)
            return accounted
        history = self.state.add_history(request)
        if history["status"] != "OK":
            response = self.state._response(str(history["status"]), reason=str(history.get("reason", "history_cap")))
            self._validate_response(response)
            return response
        return started

    def _request_valid(self, request: Mapping[str, Any]) -> bool:
        endpoint = request.get("endpoint") if isinstance(request, Mapping) else None
        schema = self.protocol["request_schemas"].get(endpoint)
        return bool(schema and not list(Draft202012Validator(schema).iter_errors(request)))

    def _validate_response(self, response: Mapping[str, Any]) -> None:
        schema = self.protocol["response_schemas_by_status"].get(response.get("status"))
        if schema is None:
            raise ValueError("compact_response_status_not_in_visible_schema")
        errors = sorted(error.message for error in Draft202012Validator(schema).iter_errors(response))
        if errors:
            raise ValueError("compact_response_schema_invalid:" + "|".join(errors))
        self.response_validation_count += 1

    @staticmethod
    def _validation_content(public: Mapping[str, Any]) -> dict[str, Any]:
        feedback = [dict(row) for row in public.get("feedback", ()) if isinstance(row, Mapping)]
        accepted = public.get("status") == "CONSTRUCTION_VALID"
        return {
            "accepted": accepted,
            "canonical_status": public.get("status"),
            "errors": [{
                "error_code": row.get("error_code"),
                "location": row.get("location"),
                "expected_shape_or_schema_ref": row.get("expected_shape_or_schema_ref"),
            } for row in feedback],
            "canonical_ir_sha256": public.get("compiled_sha256") if accepted else None,
            "precommit_qor_feedback": False,
            "proposal_certificate_feedback": False,
        }

    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        endpoint = request.get("endpoint") if isinstance(request, Mapping) else None
        if endpoint not in RETRIEVAL_ENDPOINTS | SEMANTIC_ENDPOINTS or not self._request_valid(request):
            response = self.state.dispatch(request)
            self._validate_response(response)
            return response
        if endpoint in RETRIEVAL_ENDPOINTS:
            response = self.state.dispatch(request)
            self._validate_response(response)
            self.semantic_events.append({"endpoint": endpoint, "canonical_event": response["status"]})
            return response
        if endpoint == "QUERY_DESIGN":
            if self.state.query_count >= self.state.max_queries:
                response = self.state.dispatch(request)
            else:
                message = {"kind": "QUERY", "queries": [{
                    "name": request["query_id"],
                    "top_k": request["arguments"].get("top_k"),
                }]}
                public = result_public(self.frozen_session.accept(message))
                if public["status"] != "QUERY_RESULT":
                    content = self._validation_content(public)
                    response = self.state.add_schema_feedback(content)
                    response = response | ({"content": content} if response["status"] == "OK" else {})
                else:
                    mutable = self.state.query_results
                    if not isinstance(mutable, dict):
                        raise TypeError("compact_query_results_must_be_mutable")
                    mutable[request["query_id"]] = public["query_batch"]
                    response = self.state.dispatch(request)
        elif endpoint == "VALIDATE_PROGRAM":
            public = result_public(self.frozen_session.accept({
                "kind": "VALIDATE", "candidate": request["program"],
            }))
            semantic = self._validation_content(public)
            prior = self.state.program_validator
            self.state.program_validator = lambda _program: semantic
            try:
                response = self.state.dispatch(request)
            finally:
                self.state.program_validator = prior
        elif endpoint == "COMMIT":
            program_hash = sha256(request["program"])
            if self.state.validated_program_sha256 != program_hash:
                response = self.state.dispatch(request)
            else:
                public = result_public(self.frozen_session.accept({
                    "kind": "COMMIT", "candidate": request["program"],
                }))
                if public["status"] != "COMMITTED":
                    semantic = self._validation_content(public)
                    response = self.state.add_schema_feedback(semantic)
                    response = response | ({"content": semantic} if response["status"] == "OK" else {})
                else:
                    response = self.state.dispatch(request)
        else:
            self.frozen_session.accept({"kind": "NO_VALID_SUBMISSION"})
            response = self.state.dispatch(request)
        self._validate_response(response)
        self.semantic_events.append({
            "endpoint": endpoint, "canonical_event": response["status"],
            "legacy_session_terminal": self.frozen_session.terminal,
            "proposal_certificate_visible": False, "qor_feedback_visible": False,
        })
        return response

    def accounting(self) -> dict[str, int]:
        return {
            "retrieval_bytes": self.state.retrieval_bytes,
            "query_result_bytes": self.state.query_result_bytes,
            "schema_feedback_bytes": self.state.schema_feedback_bytes,
            "history_bytes": self.state.history_bytes,
            "semantic_spent_bytes": self.state.spent_bytes,
            "semantic_remaining_bytes": self.state.remaining_reserve_bytes,
        }


__all__ = [
    "COMPACT_SESSION_ADAPTER_VERSION", "CompactSessionAdapter",
    "RETRIEVAL_ENDPOINTS", "SEMANTIC_ENDPOINTS",
]
