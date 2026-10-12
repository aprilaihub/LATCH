"""Select a full-runtime source copy with explicit user documentation.

Not a redistribution/license clearance tool. No Git history, submodule binaries,
paper datasets, credentials, captures or generated results are copied. The
external fixed RapidWright installation is still required for physical runs.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CODE_SCOPES = ("optimizer_pipeline/", "integrations/", "experiments/dof/",
    "experiments/ablation/", "experiments/acceptance/", "experiments/llm/",
    "RapidWrightMCP/", "VivadoMCP/", "scripts/", "tools/", "tests/", "examples/latch/", "experiments/tuning/")
TOP = {"README.md", "README-LATCH.md", "AGENTS.md", "SOURCE_CREDITS.md", "LICENSING.md", "CONTRIBUTORS.md",
    "pyproject.toml", "pytest.ini", ".gitignore", ".gitmodules", "LICENSE",
    "LICENSE.txt", "LICENSE.TXT", "LICENSE-APACHE-2.0.txt", "LICENSE-MCP-MIT.txt", "NOTICE", "NOTICE.txt", "Makefile",
    "dcp_optimizer.py", "validate_dcps.py", "mcp_stdio_asyncio.py", "experiments/__init__.py"}
SOURCE_SUFFIXES = {".py", ".tcl", ".java", ".sh", ".json", ".md", ".toml", ".xml"}
# A named source scope prevents later internal report/package generators from
# entering exports merely because they have a source-code suffix.
PUBLIC_SCRIPTS = {"scripts/" + name for name in (
    'WP01202RapidWrightReadonly.java',
    'analyze_wp012.py',
    'analyze_wp012_01.py',
    'analyze_wp012_02_readonly.py',
    'analyze_wp013_e0.py',
    'analyze_wp013_macro_panel.py',
    'analyze_wp013a_e0.py',
    'analyze_wp013b_e0.py',
    'analyze_wp014.py',
    'analyze_wp014b_admission.py',
    'analyze_wp014b_properties.py',
    'analyze_wp014c.py',
    'analyze_wp014e_admission.py',
    'analyze_wp014e_matrix.py',
    'analyze_wp014f.py',
    'analyze_wp014g.py',
    'analyze_wp014i_macro.py',
    'analyze_wp015.py',
    'analyze_wp016b_core.py',
    'analyze_wp016b_expansion.py',
    'audit_holdout_dcp.tcl',
    'audit_wp012_01_processes.py',
    'audit_wp013_admission.py',
    'audit_wp013_e0_processes.py',
    'audit_wp013a_e0_processes.py',
    'audit_wp013a_gate0.py',
    'audit_wp013b_context_fit.py',
    'audit_wp013b_semantic_parity.py',
    'audit_wp014_interface_blocker.py',
    'audit_wp014_processes.py',
    'audit_wp014_vivado_command_surface.tcl',
    'audit_wp014b_handoff.py',
    'audit_wp014b_place_semantics_conflict.py',
    'audit_wp014c_action_surface.py',
    'audit_wp014c_handoff.py',
    'audit_wp014d_functional_admission.py',
    'audit_wp014e_gate0.py',
    'audit_wp014e_processes.py',
    'audit_wp014g_processes.py',
    'audit_wp014h_gate0.py',
    'audit_wp015_canary.py',
    'audit_wp015a_gate1.py',
    'audit_wp015b_gate1.py',
    'audit_wp015b_gate2.py',
    'audit_wp015b_gate4.py',
    'audit_wp016a_metered_frontier.py',
    'aws_watchdog_probe.sh',
    'build_wp014b_registry.py',
    'build_wp015_m10_panel.py',
    'build_wp016a_replay_capsule.py',
    'build_wp016a_retrieval_protocol_v3.py',
    'build_wp016b_core.py',
    'build_wp016b_expansion.py',
    'build_wp016b_expansion_a2.py',
    'capture_wp014b_installed_help.py',
    'capture_wp014b_properties.py',
    'derive_wp014_stress.py',
    'e1_score_dcp.tcl',
    'export_git_archive.sh',
    'finalize_wp014h.py',
    'finalize_wp014i.py',
    'finalize_wp015_cell_validation.py',
    'finalize_wp015c_wave_validation.py',
    'freeze_wp013_protocol.py',
    'freeze_wp013a_protocol.py',
    'freeze_wp013b_protocol.py',
    'freeze_wp014b_admission.py',
    'freeze_wp014b_supplement.py',
    'freeze_wp014c_action_surface.py',
    'freeze_wp014h_benchmark_gate.py',
    'freeze_wp014h_gate1.py',
    'freeze_wp015_core.py',
    'freeze_wp015_environment.py',
    'freeze_wp015_inventory.py',
    'generate_candidate_figures.py',
    'prepare_wp015_bundle.py',
    'rebind_wp016a_replay_capsule.py',
    'reconcile_wp015_entry.py',
    'reconcile_wp015_physical_aggregate.py',
    'reconcile_wp015a_entry.py',
    'reconcile_wp015b_entry.py',
    'repair_wp014i_integrated_validation.py',
    'replay_wp016a_r1_adversarial.py',
    'replay_wp016b_expansion_a2_orchestrator.py',
    'resume_wp015c_post_agent_references.py',
    'run_fixed_sequence_expansion.py',
    'run_flow_matrix.py',
    'run_frontier_expansion.py',
    'run_iterative_optimizer_workflow.py',
    'run_tuning_harness.py',
    'run_wp002a_probes.py',
    'run_wp003_offline_probes.py',
    'run_wp004_fake_matrix.py',
    'run_wp004_provider_smoke.py',
    'run_wp004_real_mutation.py',
    'run_wp004a_worker_attestation.py',
    'run_wp005_e0.py',
    'run_wp005_openrouter.py',
    'run_wp005a_e0.py',
    'run_wp006_e1.py',
    'run_wp007.py',
    'run_wp010_admission.py',
    'run_wp011_process_admission.py',
    'run_wp011_provider_admission.py',
    'run_wp012.py',
    'run_wp012_01.py',
    'run_wp012_02.py',
    'run_wp012_02_admission.py',
    'run_wp012_02_planning.py',
    'run_wp013_admission.py',
    'run_wp013_e0.py',
    'run_wp013_macro_panel.py',
    'run_wp013a_e0.py',
    'run_wp013b_e0.py',
    'run_wp014.py',
    'run_wp014b_admission.py',
    'run_wp014c.py',
    'run_wp014c_admission.py',
    'run_wp014c_scientific.py',
    'run_wp014d_functional_admission.py',
    'run_wp014e.py',
    'run_wp014f_validation_recon.py',
    'run_wp014g_model_usability.py',
    'run_wp014g_probes.py',
    'run_wp014g_validation.py',
    'run_wp014h_benchmark_admission.py',
    'run_wp014h_language_admission.py',
    'run_wp014h_macro_readmission.py',
    'run_wp014h_macro_validation.py',
    'run_wp014h_strict_validation.py',
    'run_wp014h_strict_witnesses.py',
    'run_wp014h_synthetic_timing_recon.py',
    'run_wp014i_development_admission.py',
    'run_wp014i_integrated_preflight.py',
    'run_wp014i_m8_validation.py',
    'run_wp014i_macro_panel.py',
    'run_wp014i_rapidwright_collateral.py',
    'run_wp014i_synthetic_revalidation.py',
    'run_wp014i_validation_canary.py',
    'run_wp015_cells.py',
    'run_wp015_gemini_admission.py',
    'run_wp015_m10_fresh.py',
    'run_wp015_post_agent_references.py',
    'run_wp015_rapidwright_census.py',
    'run_wp015_seed_parallel_canary.py',
    'run_wp015_seed_validation_batch.py',
    'run_wp015a_construction_admission.py',
    'run_wp015a_region_capacity.py',
    'run_wp015a_runtime_admission.py',
    'run_wp015b_construction_admission.py',
    'run_wp015b_hardblock_scan.py',
    'run_wp015b_runtime_admission.py',
    'run_wp015c_disk_bounded_validation.py',
    'run_wp015c_reference_validation.py',
    'run_wp016a_canary.py',
    'run_wp016a_completion.py',
    'run_wp016a_r10.py',
    'run_wp016b_core.py',
    'run_wp016b_expansion.py',
    'run_wp016b_expansion_a2.py',
    'summarize_wp002a_mock_runs.py',
    'summarize_wp003_gate0_mock_runs.py',
    'validate_paper_evidence_capsule_pass2.py',
    'verify_wp014i_source_transfer.py',
    'wp012_02_readonly_probe.tcl',
    'wp014b_admission_job.tcl',
    'wp014b_property_probe.tcl',
    'wp014h_root_admission.tcl',
    'wp014h_synthetic_clock_topology.tcl',
    'wp014h_synthetic_timing_probe.tcl',
)}
PUBLIC_DOCS = {
    "docs/assets/README.md",
    "docs/index.md", "docs/start-here.md", "docs/latch-harness.md",
    "docs/latch-tools.md", "docs/latch-organization.md", "docs/latch-data-reproduction.md",
    "docs/architecture/overview.md", "docs/architecture/state-graph-and-records.md",
    "docs/outputs-and-artifacts.md", "docs/real-vivado-runs.md",
    "docs/reference/troubleshooting.md", "experiments/dof/README.md",
    "docs/reference/cli-reference.md", "docs/reference/glossary.md",
    "docs/rescue-approach-mental-model.md",
    "experiments/ablation/README.md", "experiments/acceptance/README.md",
    "optimizer_pipeline/README.md", "RapidWrightMCP/README.md",
    "examples/expansion_pipeline_authoring.md",
}
PUBLIC_FIGURES = {"docs/assets/" + name + suffix
    for name in ("latch-overview", "dof-results", "ablation-results")
    for suffix in (".png", ".pdf")}
# Markdown prompts are executable study resources, not documentation archives.
PROMPT_SCOPES = ("experiments/dof/prompts/", "experiments/llm/prompts/",
                 "integrations/llm/prompts/", "experiments/ablation/runtime/prompts/")
HISTORICAL_RUNTIME_RESOURCES = {"configs/wp014e/provider-profiles-v1.json",
    "configs/wp014e/pricing-snapshot-2026-08-12.json",
    "configs/wp015/benchmark-timing-contract-v1.json",
    "configs/wp015a/provider-output-envelope-v1.json",
    "configs/wp015a/strict-runtime-model-order-v1.json",
    ".research-flow/source/WP015-Pricing-Snapshot-2026-08-14.json"}
SYNTHETIC_CONTRACT_RESOURCES = {"configs/experiments/dof_contract/" + name for name in (
    "01_l0_known.json", "02_l1_known.json", "03_l2_legal_known.json", "04_l2_invalid_raw.json",
    "05_l3_legal_plan.json", "06_l3_forbidden_flow.json", "07_l2_forced_unknown_masked.json",
    "08_l4_reference_pending.json", "mock_root.mockdcp")}
FORBIDDEN_PARTS = {"private", ".git", ".env", "__pycache__", "artifacts", "outputs",
    "results", "raw", "captures", "capture", "logs", "node_modules"}
SECRET = re.compile(rb"sk-[A-Za-z0-9_-]{24,}|BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY")


def source_path(path: str) -> bool:
    p = Path(path)
    if p.is_absolute() or ".." in p.parts or FORBIDDEN_PARTS.intersection(p.parts):
        return False
    if path in TOP or path in PUBLIC_FIGURES:
        return True
    if path in HISTORICAL_RUNTIME_RESOURCES or path in SYNTHETIC_CONTRACT_RESOURCES:
        return True
    if p.suffix == ".md":
        return path in PUBLIC_DOCS or path.startswith(PROMPT_SCOPES)
    if path.startswith("configs/"):
        # These named non-secret historical profiles are needed by retained
        # collector imports/tests. They never supply current paid prices.
        return path in HISTORICAL_RUNTIME_RESOURCES or (p.suffix == ".json" and
            (len(p.parts) == 2 or p.parts[1] in {"providers", "pricing"}))
    if path.startswith("scripts/") and path not in PUBLIC_SCRIPTS:
        return False
    if not path.startswith(CODE_SCOPES) or p.suffix not in SOURCE_SUFFIXES:
        return False
    # Structured data is source only in named package/config resources, never
    # automatically every JSON beneath historical experiment trees or tests.
    if p.suffix == ".json":
        return path.startswith(("experiments/dof/configs/", "experiments/ablation/configs/",
            "experiments/ablation/runtime/knowledge/data/", "integrations/llm/runtime/data/",
            "examples/latch/")) or (p.parent.as_posix() == "experiments/acceptance")
    return True


def export(output: Path, *, root: Path = ROOT):
    root = root.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(root) or root.is_relative_to(output.resolve()):
        raise ValueError("Use a new independent source destination, outside the original checkout")
    paths = subprocess.check_output(["git", "-C", str(root), "ls-files", "--cached", "-z"]).decode().split("\0")
    included, excluded = [], Counter()
    # Inspect all selected files before creating anything. A secret match is
    # reported by source path only, never by value. Gitlinks are not traversed.
    for name in sorted(set(filter(None, paths))):
        if not source_path(name):
            excluded["outside_source_scope"] += 1
            continue
        file = root / name
        if file.is_symlink() or not file.is_file():
            raise ValueError("Selected source must be a regular file: " + name)
        if SECRET.search(file.read_bytes()):
            raise ValueError("Credential-like source requires review before copying: " + name)
        included.append(name)
    if not included:
        raise ValueError("No committed runtime source selected")
    output.mkdir(mode=0o700, parents=True)
    for name in included:
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, destination)
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True))
    scope = dict(source_revision=revision, source_worktree_dirty=dirty,
        included_files=included, excluded_counts=dict(excluded), copies_git_history=False,
        copies_rapidwright_submodule=False, redistribution_authorized=False,
        scope="full-runtime source selection; external tools/data/licenses required")
    # Internal review metadata is returned to the caller, not embedded in the
    # deliverable. Callers may record it outside both source and export trees.
    return scope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = export(args.output)
    print(json.dumps({"source_revision": report["source_revision"],
        "files": len(report["included_files"]), "redistribution_authorized": False}, indent=2))


if __name__ == "__main__":
    main()
