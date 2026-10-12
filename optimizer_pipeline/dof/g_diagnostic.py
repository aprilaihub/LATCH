"""Isolated WP-012-01 G-utilisation diagnostic contracts.

Nothing in this module is reachable from a primary K/U/H job.  It provides a
small prompt overlay and, for C4 decision zero only, a one-action catalogue.
The common base observation and inherited action definitions remain unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifacts import fingerprint_file
from .catalogues import ActionCatalogueManifest, nested_action_catalogue
from .common_observation import CommonPolicyContext, build_common_policy_context
from .contracts import DoFLevel, UnknownRegime
from .e1v2 import UNKNOWN_PROVIDER_REQUEST_RESERVE_USD, canonical_bytes, content_hash
from .generated import GENERATED_ACTION_KEY
from .openrouter import OPENROUTER_E1_MODELS
from .provenance import observe_repository


G_DIAGNOSTIC_PROTOCOL = "dof.g-diagnostic.v1"
G_DIAGNOSTIC_MATRIX = "WP012_01_G_DIAGNOSTIC_V1"
G_DIAGNOSTIC_STUDY_STATUS = "DIAGNOSTIC_ONLY_NOT_PRIMARY_DOF"
G_DIAGNOSTIC_BUNDLE_SCHEMA = "dof.g-diagnostic-bound-bundle.v1"
G_DIAGNOSTIC_SPEC_SCHEMA = "dof.g-diagnostic-job-spec.v1"
G_DIAGNOSTIC_RESULT_SCHEMA = "dof.g-diagnostic-trajectory-result.v1"
G_DIAGNOSTIC_PROBE_SCHEMA = "dof.g-diagnostic-provider-probe.v1"
G_DIAGNOSTIC_AGGREGATE_SCHEMA = "dof.g-diagnostic-aggregate.v1"
G_DIAGNOSTIC_ROOTS = ("vexriscv_re-place", "external_compatibility_a")
G_ONLY_CATALOGUE_VERSION = "dof.g-diagnostic.first-decision-g-only.v1"
G_DIAGNOSTIC_EXECUTION_EPOCH = "fresh-2-after-controller-interruption"


class GCue(str, Enum):
    C0 = "C0_NEUTRAL_CONTROL"
    C1 = "C1_CAPABILITY_NOTICE"
    C2 = "C2_PROCEDURAL_EXAMPLE_GUIDANCE"
    C3 = "C3_ACTIVE_G_USE_REQUIRED"
    C4 = "C4_FIRST_DECISION_G_ONLY_OR_STOP"


_OVERLAYS: Mapping[GCue, bytes] = {
    GCue.C0: b"",
    GCue.C1: (
        b"`generated.pblock_region` is an available L4 action. It creates a bounded "
        b"pblock constraint as a light mutation. Select it only if you judge it useful; "
        b"all other legal actions and STOP remain available."
    ),
    GCue.C2: (
        b"`generated.pblock_region` selects a bounded set of cells using "
        b"`sequential_cells` or `high_fanout_cells`, constrains them to the supplied "
        b"SLICE rectangle, and is a light mutation that does not itself rerun placement "
        b"or routing. A later decision may use `macro.place_route` or equivalent typed "
        b"placement/routing primitives to evaluate the resulting timing effect. "
        b"Illustrative step only, not a recommended device region: "
        b"{\"step_id\":\"step-g-example\",\"action_key\":\"generated.pblock_region\","
        b"\"parameters\":{\"pblock_id\":\"g_example\",\"selector_kind\":"
        b"\"sequential_cells\",\"cell_limit\":16,\"slice_x0\":0,\"slice_y0\":0,"
        b"\"slice_x1\":10,\"slice_y1\":99}}. Bind the current decision and catalogue "
        b"dynamically from the supplied context."
    ),
    GCue.C3: (
        b"For this diagnostic trajectory, attempt at least one valid "
        b"`generated.pblock_region` action before STOP. You choose when to use it, all "
        b"parameters, and all preceding/following actions. No retry or repair is provided "
        b"if your proposal is invalid."
    ),
    GCue.C4: (
        b"For decision 1 of this diagnostic trajectory, the only admissible mutation is "
        b"exactly one `generated.pblock_region` ACTION step; STOP remains available. You "
        b"must choose every generated parameter. No parameter, coordinate, selector, or "
        b"action will be auto-filled. After an admitted generated action, later decisions "
        b"restore the neutral L4 M+P+G surface."
    ),
}


def overlay_bytes(cue: GCue | str) -> bytes:
    parsed = cue if isinstance(cue, GCue) else GCue(str(cue))
    return _OVERLAYS[parsed]


def overlay_manifest() -> dict[str, Any]:
    rows = []
    for cue in GCue:
        raw = overlay_bytes(cue)
        rows.append(
            {
                "cue": cue.value,
                "overlay_utf8": raw.decode("utf-8"),
                "overlay_sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
        )
    payload = {
        "schema_version": "dof.g-diagnostic-overlays.v1",
        "protocol": G_DIAGNOSTIC_PROTOCOL,
        "rows": rows,
    }
    return payload | {"manifest_sha256": content_hash(payload)}


def g_only_catalogue() -> ActionCatalogueManifest:
    inherited = nested_action_catalogue(DoFLevel.L4)
    action = inherited.action(GENERATED_ACTION_KEY)
    if action is None:
        raise RuntimeError("g_action_missing_from_l4")
    return ActionCatalogueManifest(
        version=G_ONLY_CATALOGUE_VERSION,
        level_contract="L4_G_ONLY_DIAGNOSTIC_DECISION_1",
        actions=(action,),
    )


def diagnostic_catalogue(cue: GCue | str, decision_index: int) -> ActionCatalogueManifest:
    parsed = cue if isinstance(cue, GCue) else GCue(str(cue))
    if parsed is GCue.C4 and decision_index == 0:
        return g_only_catalogue()
    return nested_action_catalogue(DoFLevel.L4)


@dataclass(frozen=True, slots=True)
class GDiagnosticPolicyContext:
    common: CommonPolicyContext
    cue: GCue
    decision_index: int
    overlay: bytes
    overlay_sha256: str
    action_surface: tuple[str, ...]
    primary_prompt_sha256: str

    def evidence(self) -> dict[str, Any]:
        return {
            "schema_version": "dof.g-diagnostic-context-evidence.v1",
            "protocol": G_DIAGNOSTIC_PROTOCOL,
            "cue": self.cue.value,
            "decision_index": self.decision_index,
            "overlay_utf8": self.overlay.decode("utf-8"),
            "overlay_sha256": self.overlay_sha256,
            "base_observation_hash": self.common.base_observation_hash,
            "common_prompt_core_hash": self.common.common_prompt_core_hash,
            "primary_prompt_sha256": self.primary_prompt_sha256,
            "diagnostic_prompt_sha256": self.common.provider_prompt_hash,
            "provider_context_sha256": self.common.provider_context_hash,
            "action_surface": list(self.action_surface),
            "first_decision_restricted": self.cue is GCue.C4 and self.decision_index == 0,
        }


def build_g_diagnostic_policy_context(
    *,
    source: Mapping[str, Any],
    current_artifact_sha256: str,
    decision_index: int,
    cue: GCue | str,
) -> GDiagnosticPolicyContext:
    parsed = cue if isinstance(cue, GCue) else GCue(str(cue))
    primary = build_common_policy_context(
        level=DoFLevel.L4,
        source=source,
        current_artifact_sha256=current_artifact_sha256,
        decision_index=decision_index,
        regime=UnknownRegime.KNOWN,
    )
    catalogue = diagnostic_catalogue(parsed, decision_index)
    if parsed is GCue.C4 and decision_index == 0:
        common = build_common_policy_context(
            level=DoFLevel.L4,
            source=source,
            current_artifact_sha256=current_artifact_sha256,
            decision_index=decision_index,
            regime=UnknownRegime.KNOWN,
            catalogue=catalogue,
        )
    else:
        common = primary
    effective_overlay = overlay_bytes(parsed)
    if parsed is GCue.C4 and decision_index > 0:
        effective_overlay = b""
    if effective_overlay:
        prompt = common.provider_prompt_bytes + b"\nG_DIAGNOSTIC_OVERLAY=" + effective_overlay
        common = replace(
            common,
            provider_prompt_bytes=prompt,
            provider_prompt_hash=hashlib.sha256(prompt).hexdigest(),
        )
    return GDiagnosticPolicyContext(
        common=common,
        cue=parsed,
        decision_index=decision_index,
        overlay=effective_overlay,
        overlay_sha256=hashlib.sha256(effective_overlay).hexdigest(),
        action_surface=tuple(action.name for action in catalogue.actions),
        primary_prompt_sha256=primary.provider_prompt_hash,
    )


def validate_diagnostic_spec(spec: Mapping[str, Any]) -> GCue:
    if (
        spec.get("schema_version") != G_DIAGNOSTIC_SPEC_SCHEMA
        or spec.get("protocol_version") != G_DIAGNOSTIC_PROTOCOL
        or spec.get("matrix") != G_DIAGNOSTIC_MATRIX
        or spec.get("study_status") != G_DIAGNOSTIC_STUDY_STATUS
        or spec.get("job", {}).get("level") != "L4"
        or spec.get("job", {}).get("regime") != "known"
        or spec.get("diagnostic", {}).get("protocol") != G_DIAGNOSTIC_PROTOCOL
        or spec.get("diagnostic", {}).get("execution_epoch")
        != G_DIAGNOSTIC_EXECUTION_EPOCH
    ):
        raise ValueError("g_diagnostic_spec_boundary_invalid")
    if spec.get("job", {}).get("benchmark") not in G_DIAGNOSTIC_ROOTS:
        raise ValueError("g_diagnostic_root_not_allowed")
    cue = GCue(str(spec["diagnostic"]["cue"]))
    core = {key: value for key, value in spec.items() if key != "job_spec_sha256"}
    if spec.get("job_spec_sha256") != content_hash(core):
        raise ValueError("g_diagnostic_spec_hash_invalid")
    return cue


def _jobs(phase: str, roots: Sequence[str]) -> list[dict[str, Any]]:
    selected_roots = (roots[0],) if phase != "REAL_TRAJECTORY" else tuple(roots)
    rows = []
    for root in selected_roots:
        for model in OPENROUTER_E1_MODELS:
            for cue in GCue:
                suffix = cue.value.split("_", 1)[0].lower()
                rows.append(
                    {
                        "benchmark": root,
                        "level": "L4",
                        "model": model,
                        "repeat": 1,
                        "regime": "known",
                        "phase": phase,
                        "cue": cue.value,
                        "job_id": (
                            f"{root}__l4__{model.replace('/', '_')}__{suffix}__"
                            f"{phase.lower()}__gdiag_v1e2"
                        ),
                    }
                )
    return [row | {"order": index} for index, row in enumerate(rows)]


def _spec(
    *,
    job: Mapping[str, Any],
    root: Mapping[str, Any],
    freeze_sha256: str,
    repository: Mapping[str, Any],
) -> dict[str, Any]:
    cue = GCue(str(job["cue"]))
    core = {
        "schema_version": G_DIAGNOSTIC_SPEC_SCHEMA,
        "protocol_version": G_DIAGNOSTIC_PROTOCOL,
        "matrix": G_DIAGNOSTIC_MATRIX,
        "study_status": G_DIAGNOSTIC_STUDY_STATUS,
        "job": dict(job),
        "root": dict(root),
        "freeze_sha256": freeze_sha256,
        "repository": dict(repository),
        "diagnostic": {
            "protocol": G_DIAGNOSTIC_PROTOCOL,
            "execution_epoch": G_DIAGNOSTIC_EXECUTION_EPOCH,
            "cue": cue.value,
            "overlay_sha256": hashlib.sha256(overlay_bytes(cue)).hexdigest(),
            "primary_aggregate_eligible": False,
            "sealed_holdout_access_allowed": False,
        },
        "budgets": {
            "heavy_calls": 4,
            "model_requests": 4,
            "trajectory_wall_seconds": 3700,
            "provider_cost_usd": 4 * UNKNOWN_PROVIDER_REQUEST_RESERVE_USD,
        },
    }
    return core | {"job_spec_sha256": content_hash(core)}


def freeze_g_diagnostic_bundle(
    roots: Mapping[str, str | Path],
    root_audit: Mapping[str, Any],
    provider_discovery: Mapping[str, Any],
    probe_observation: Mapping[str, Any],
    *,
    repository_anchor: str | Path,
    fake_startup_attestation_sha256: str,
) -> dict[str, Any]:
    if tuple(roots) != G_DIAGNOSTIC_ROOTS:
        raise ValueError("g_diagnostic_roots_must_be_exact_development_roots")
    if root_audit.get("all_accepted") is not True:
        raise ValueError("g_diagnostic_root_audit_not_accepted")
    if provider_discovery.get("all_profiles_available") is not True:
        raise ValueError("g_diagnostic_provider_profiles_unavailable")
    if probe_observation.get("origin") != "real_read_only_vivado":
        raise ValueError("g_diagnostic_probe_observation_not_real")
    observed_probe_core = {
        key: value for key, value in probe_observation.items() if key != "observation_sha256"
    }
    if probe_observation.get("observation_sha256") != content_hash(observed_probe_core):
        raise ValueError("g_diagnostic_probe_observation_hash_invalid")
    repository = observe_repository(repository_anchor)
    if repository.tracked_tree_dirty:
        raise RuntimeError("tracked_product_tree_not_clean")
    repository_row = {
        "branch_name": repository.branch_name,
        "head_commit": repository.head_commit,
        "head_tree": repository.head_tree,
        "binary_diff_sha256": repository.binary_diff_sha256,
    }
    audit_rows = {row["benchmark"]: row for row in root_audit.get("rows", ())}
    root_rows: dict[str, Any] = {}
    for name, path in roots.items():
        artifact = fingerprint_file(Path(path).resolve())
        row = audit_rows.get(name, {})
        if row.get("artifact") != {
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }:
            raise ValueError("g_diagnostic_root_identity_mismatch:" + name)
        root_rows[name] = {
            "pseudonym": name,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "score": row["score"],
        }
    if probe_observation.get("current_artifact") != {
        "sha256": root_rows[G_DIAGNOSTIC_ROOTS[0]]["sha256"],
        "size_bytes": root_rows[G_DIAGNOSTIC_ROOTS[0]]["size_bytes"],
    }:
        raise ValueError("g_diagnostic_probe_observation_root_mismatch")
    action_manifest = nested_action_catalogue(DoFLevel.L4)
    action_hashes = {
        action.name: content_hash(action.to_dict()) for action in action_manifest.actions
    }
    freeze = {
        "protocol": G_DIAGNOSTIC_PROTOCOL,
        "execution_epoch": G_DIAGNOSTIC_EXECUTION_EPOCH,
        "matrix": G_DIAGNOSTIC_MATRIX,
        "repository": repository_row,
        "roots": root_rows,
        "root_audit_sha256": root_audit["audit_sha256"],
        "provider_discovery_sha256": content_hash(provider_discovery),
        "probe_source_observation_sha256": probe_observation["observation_sha256"],
        "probe_source_artifact_sha256": probe_observation["current_artifact"]["sha256"],
        "overlays": overlay_manifest(),
        "l4_catalogue_version": action_manifest.version,
        "l4_catalogue_sha256": action_manifest.sha256,
        "g_only_catalogue_sha256": g_only_catalogue().sha256,
        "action_definition_sha256": action_hashes,
        "fake_startup_attestation_sha256": fake_startup_attestation_sha256,
        "sealed_holdouts_accessed": False,
    }
    freeze_sha = content_hash(freeze)
    real_jobs = _jobs("REAL_TRAJECTORY", G_DIAGNOSTIC_ROOTS)
    probe_jobs = _jobs("PROVIDER_ONLY_PROBE", G_DIAGNOSTIC_ROOTS)
    admission_jobs = [
        row
        for row in _jobs("PROVIDER_ADMISSION", G_DIAGNOSTIC_ROOTS)
        if row["cue"] == GCue.C0.value
    ]
    payload = {
        "schema_version": G_DIAGNOSTIC_BUNDLE_SCHEMA,
        "protocol_version": G_DIAGNOSTIC_PROTOCOL,
        "matrix": G_DIAGNOSTIC_MATRIX,
        "freeze": freeze,
        "freeze_sha256": freeze_sha,
        "probe_observation_template": dict(probe_observation),
        "provider_admissions": [
            _spec(job=row, root=root_rows[row["benchmark"]], freeze_sha256=freeze_sha, repository=repository_row)
            for row in admission_jobs
        ],
        "provider_probes": [
            _spec(job=row, root=root_rows[row["benchmark"]], freeze_sha256=freeze_sha, repository=repository_row)
            for row in probe_jobs
        ],
        "real_jobs": [
            _spec(job=row, root=root_rows[row["benchmark"]], freeze_sha256=freeze_sha, repository=repository_row)
            for row in real_jobs
        ],
        "guards": {
            "provider_and_real_cost_usd": 4.0,
            "aggregate_wall_seconds": 86400,
            "max_concurrency_weekend": 16,
            "max_concurrency_weekday": 8,
        },
    }
    return payload | {"bundle_sha256": content_hash(payload)}


def validate_g_diagnostic_bundle(bundle: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(canonical_bytes(dict(bundle)))
    claimed = value.pop("bundle_sha256", None)
    if value.get("schema_version") != G_DIAGNOSTIC_BUNDLE_SCHEMA:
        raise ValueError("g_diagnostic_bundle_schema_invalid")
    if claimed != content_hash(value):
        raise ValueError("g_diagnostic_bundle_hash_invalid")
    if value.get("freeze_sha256") != content_hash(value.get("freeze", {})):
        raise ValueError("g_diagnostic_freeze_hash_invalid")
    inventories = {
        "provider_admissions": 3,
        "provider_probes": 15,
        "real_jobs": 30,
    }
    ids: set[str] = set()
    for name, expected in inventories.items():
        rows = value.get(name, ())
        if len(rows) != expected:
            raise ValueError("g_diagnostic_inventory_invalid:" + name)
        for row in rows:
            validate_diagnostic_spec(row)
            job_id = row["job"]["job_id"]
            if job_id in ids:
                raise ValueError("g_diagnostic_job_id_duplicate")
            ids.add(job_id)
    real_roots = {row["job"]["benchmark"] for row in value["real_jobs"]}
    if real_roots != set(G_DIAGNOSTIC_ROOTS):
        raise ValueError("g_diagnostic_real_roots_invalid")
    return value | {"bundle_sha256": claimed}


def rebind_probe_observation(
    template: Mapping[str, Any], spec: Mapping[str, Any]
) -> dict[str, Any]:
    validate_diagnostic_spec(spec)
    if template.get("origin") != "real_read_only_vivado":
        raise ValueError("g_diagnostic_probe_observation_not_real")
    value = dict(json.loads(canonical_bytes(dict(template))))
    source_sha = value.pop("observation_sha256", None)
    if source_sha != content_hash(value):
        raise ValueError("g_diagnostic_probe_source_hash_invalid")
    value["source_observation_sha256"] = source_sha
    value["job_spec_sha256"] = spec["job_spec_sha256"]
    value["current_artifact"] = dict(spec["root"])
    return value | {"observation_sha256": content_hash(value)}


__all__ = [
    "GCue",
    "G_DIAGNOSTIC_AGGREGATE_SCHEMA",
    "G_DIAGNOSTIC_BUNDLE_SCHEMA",
    "G_DIAGNOSTIC_EXECUTION_EPOCH",
    "G_DIAGNOSTIC_MATRIX",
    "G_DIAGNOSTIC_PROBE_SCHEMA",
    "G_DIAGNOSTIC_PROTOCOL",
    "G_DIAGNOSTIC_RESULT_SCHEMA",
    "G_DIAGNOSTIC_ROOTS",
    "G_DIAGNOSTIC_SPEC_SCHEMA",
    "G_DIAGNOSTIC_STUDY_STATUS",
    "GDiagnosticPolicyContext",
    "build_g_diagnostic_policy_context",
    "diagnostic_catalogue",
    "freeze_g_diagnostic_bundle",
    "g_only_catalogue",
    "overlay_bytes",
    "overlay_manifest",
    "rebind_probe_observation",
    "validate_diagnostic_spec",
    "validate_g_diagnostic_bundle",
]
