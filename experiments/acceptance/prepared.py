"""Fresh no-model DoF acceptance using existing capture and research entries.

This is an acceptance organizer, not a second experiment protocol or cost
ledger. An existing output is never resumed/replayed by this module.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .config import VALIDATION, validate_tools_config
from .runner import AcceptanceBatch, dump, public_dof


def run(cfg, config_path, output):
    from optimizer_pipeline.scoring import file_sha256
    validate_tools_config(cfg)
    output = Path(output).absolute()
    source, root = Path(cfg["output"]).resolve(), Path(cfg["input"]).resolve(strict=True)
    if (output.exists() or output.is_symlink() or root.is_relative_to(output.resolve())
            or output.resolve().is_relative_to(source) or source.is_relative_to(output.resolve())):
        raise ValueError("Fresh independent output required; no source replay or nesting")
    original = file_sha256(root)  # Reuse the existing input-protection mechanism.
    os.umask(0o077)
    output.mkdir(mode=0o700)
    result = dict(scope="fresh fixed proposals; installed DoF prepared-real; no historical replay",
        model_requests=0, promotes_old_acceptance=False, status="UNREACHED", conditions={})
    try:
        # The common scope parser binds the nominal ledger location even when
        # this no-model utility never opens/creates that ledger.
        batch = AcceptanceBatch(cfg | {"output": str(output),
            "ledger_path": str(output / "batch.db")}, config_path)
        batch.versions()
        material = output / "materials"
        material.mkdir(mode=0o700)
        dump(material / "tools.json", cfg["tools"])
        dump(material / "validation.json", VALIDATION)
        batch.golden_self()
        batch.capture()  # Checks all three proposals before any COMMIT.
        for condition in ("recipe", "direct", "constructed"):
            result["conditions"][condition] = dict(status="STARTED")
            dump(output / (condition + "-started.json"), dict(condition=condition))
            batch.dof(condition, paid=False)
            facts = json.loads((output / ("fixed-" + condition) / "summary.json").read_text())
            result["conditions"][condition] = public_dof(facts)
        coverage = all(row.get("execution_scoring_validation_reached") is True
                       for row in result["conditions"].values())
        strict = result["conditions"]["constructed"].get("strict_g", {})
        result["status"] = ("DOF_COVERAGE_REACHED" if coverage and
            strict.get("strict_boundary_exercised") is True else "CONDITIONS_FINISHED_COVERAGE_INCOMPLETE")
    except (Exception, KeyboardInterrupt) as exc:
        result.update(status="INFRASTRUCTURE_OR_UNREACHED", error_type=type(exc).__name__)
        # Include factual partial coverage, never raw context/logs or promotion.
        for condition in result["conditions"]:
            summary = output / ("fixed-" + condition) / "summary.json"
            if summary.is_file():
                result["conditions"][condition] = public_dof(json.loads(summary.read_text()))
    finally:
        result["original_input_unchanged"] = root.is_file() and file_sha256(root) == original
        if not result["original_input_unchanged"]: result["status"] = "INPUT_PROTECTION_FAILURE"
        result["captured_at_utc"] = datetime.now(timezone.utc).isoformat()
        dump(output / "report.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=["checklist", "run", "report"], default="checklist")
    parser.add_argument("--authorized-tools", action="store_true")
    args = parser.parse_args()
    if args.phase == "report":
        print((args.output / "report.json").read_text())
        return 0
    cfg = json.loads(args.config.read_text())
    validate_tools_config(cfg)
    if args.phase == "checklist":
        print(json.dumps(dict(scope="fresh three-interface fixed-proposal DoF acceptance",
            design=Path(cfg["input"]).name, tools=cfg["tools"], validation=VALIDATION,
            output=str(args.output), model_requests=0), indent=2))
        return 0
    if not args.authorized_tools: parser.error("real-tool authorization required")
    result = run(cfg, args.config, args.output)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "DOF_COVERAGE_REACHED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
