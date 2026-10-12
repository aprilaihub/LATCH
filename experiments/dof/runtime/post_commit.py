"""WP015 post-COMMIT study policy over shared scoring/validation adapters."""
from __future__ import annotations
import json
import math
from pathlib import Path
from optimizer_pipeline.scoring import file_sha256, score_fmax_delta
from .validation_scope import describe_validation, public_validation_plan


def validate_config(config):
    seeds = config.get("seeds")
    if not isinstance(seeds, list) or not seeds or any(type(s) is not int or not 0 <= s <= 0xffffffff for s in seeds):
        raise ValueError("Explicit nonempty uint32 validation seeds are required")
    if len(set(seeds)) != len(seeds):
        raise ValueError("Duplicate validation seeds")
    for key in ("vectors", "precheck_vectors", "prepare_timeout_seconds", "seed_timeout_seconds", "score_timeout_seconds"):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Explicit positive validation configuration required: " + key)
    for key in ("vectors", "precheck_vectors"):
        if type(config[key]) is not int:
            raise ValueError("Vector counts must be integers")
    if not isinstance(config.get("target_clock"), str) or not config["target_clock"]:
        raise ValueError("Explicit target clock required; root and child must use the same clock")
    epsilon = config.get("epsilon_fmax")
    if not isinstance(epsilon, (float, int)) or not math.isfinite(epsilon) or epsilon < 0:
        raise ValueError("Explicit nonnegative epsilon_fmax required")
    return config


class PostCommit:
    def __init__(self, *, config, golden: Path, python: Path, vivado: Path,
                 scorer_script: Path, scorer=None, prepare=None, seed_runner=None, memory_probe=None):
        self.config = validate_config(config)
        self.golden, self.python, self.vivado = golden, python, vivado
        self.scorer_script = scorer_script
        if scorer is None:
            from optimizer_pipeline.scoring.read_only import run_readonly_score
            scorer = run_readonly_score
        if prepare is None or seed_runner is None:
            from optimizer_pipeline.backends.xilinx.functional_validation import prepare_workspace, run_prepared_seed
            prepare = prepare or prepare_workspace
            seed_runner = seed_runner or run_prepared_seed
        self.scorer, self.prepare, self.seed_runner = scorer, prepare, seed_runner
        from optimizer_pipeline.dof.validation_scheduler import linux_memory_bytes
        self.memory_probe = memory_probe or linux_memory_bytes

    @property
    def validation_plan(self):
        return public_validation_plan(self.config)

    def __call__(self, *, root: Path, child: Path, output: Path):
        output.mkdir(parents=True, exist_ok=False)
        config = self.config
        protected = {path: file_sha256(path) for path in (root, child, self.golden)}
        result = {"scoring": {"status": "NOT_RUN", "delta_fmax_mhz": None},
                  "functional_validation": {"status": "NOT_RUN", "passed": None},
                  "validation_plan": self.validation_plan}
        preparation_report = None
        try:
            scores = []
            for name, path in (("root", root), ("child", child)):
                scores.append(self.scorer(path, vivado_exec=self.vivado,
                    scorer_script=self.scorer_script, run_dir=output / (name + "-score"),
                    frozen_target_clock=config["target_clock"], timeout_seconds=config["score_timeout_seconds"]))
            result["scoring"] = {"status": "MEASURED", **score_fmax_delta(
                root_fmax_mhz=scores[0]["score"]["fmax_mhz"],
                child_fmax_mhz=scores[1]["score"]["fmax_mhz"],
                root_sha256=protected[root], child_sha256=protected[child], epsilon_fmax=config["epsilon_fmax"])}
        except Exception as exc:
            result["scoring"] = {"status": "SCORING_FAILURE", "delta_fmax_mhz": None, "error_type": type(exc).__name__}
        try:
            prepared = self.prepare(golden=self.golden, revised=child, output=output / "prepared",
                python=self.python, vivado=self.vivado, seeds=config["seeds"], vectors=config["vectors"],
                precheck_vectors=config["precheck_vectors"], timeout_seconds=config["prepare_timeout_seconds"])
            preparation_report = prepared["report"]
            if prepared.get("workspace") is None:
                simulation = prepared["report"].get("simulation_report") or {}
                precheck = simulation.get("precheck_report") or simulation
                mismatch = bool(precheck.get("mismatch_count") or precheck.get("protocol_mismatch_count") or precheck.get("result_fail_seen"))
                structural_failure = prepared["report"].get("phase1_passed") is False
                infrastructure = bool(simulation.get("infrastructure_failure") or precheck.get("infrastructure_failure"))
                status = ("STRUCTURAL_VALIDATION_FAILURE" if structural_failure else
                    "VALIDATOR_INFRASTRUCTURE_FAILURE" if infrastructure else
                    "FUNCTIONAL_MISMATCH" if mismatch or simulation.get("precheck_passed") is False else
                    "VALIDATOR_PREPARATION_FAILURE")
                result["functional_validation"] = {"status": status,
                    "passed": False if status in {"STRUCTURAL_VALIDATION_FAILURE", "FUNCTIONAL_MISMATCH"} else None,
                    "seed_reports": []}
            else:
                from optimizer_pipeline.dof.validation_scheduler import MemoryAwareSeedScheduler, ValidationSeed
                units = [ValidationSeed("commit-1", "root", f"seed-{i:02d}", seed,
                    config.get("seed_memory_class", "unknown")) for i, seed in enumerate(config["seeds"])]
                scheduler = MemoryAwareSeedScheduler(units, maximum=1, target=1,
                    reserve_fraction=config.get("memory_reserve_fraction", 0.20))
                reports = []
                scheduler_events = []
                for index, seed in enumerate(config["seeds"]):
                    attempted = False
                    try:
                        total, available = self.memory_probe()
                        admitted = scheduler.refill(running=[], total_memory=total, available_memory=available)
                        scheduler_events.append({"seed": seed, "memory_total_bytes": total,
                            "memory_available_bytes": available, "admitted": bool(admitted)})
                        if not admitted or admitted[0].stimulus_seed != seed:
                            raise RuntimeError("Validation seed not admitted within memory reserve; not executed")
                        attempted = True
                        report = self.seed_runner(prepared_workspace=Path(prepared["workspace"]),
                            output=output / f"seed-{index:02d}", stimulus_seed=seed,
                            vectors=config["vectors"], timeout_seconds=config["seed_timeout_seconds"], vivado=self.vivado)
                    except Exception as exc:
                        report = {"stimulus_seed": seed, "cycles_simulated": 0, "mismatch_count": 0,
                            "protocol_mismatch_count": 0, "passed": False, "result_fail_seen": False,
                            "simulator_failed": True, "infrastructure_failure": True,
                            "infrastructure_reason": type(exc).__name__, "execution_attempted": attempted}
                    reports.append(report)
                # Reject unexpected/missing/duplicate seed identities explicitly;
                # never silently take the first passing row or change the seed set.
                identities = [r.get("stimulus_seed") for r in reports]
                if identities != config["seeds"]:
                    result["functional_validation"] = {"status": "FUNCTIONAL_INCOMPLETE", "passed": None,
                        "all_seeds_present_exactly_once": False, "seed_reports": reports, "scheduler_events": scheduler_events}
                else:
                    from optimizer_pipeline.dof.seed_validation import aggregate_seed_reports
                    aggregate = aggregate_seed_reports(prepared_report=prepared["report"], seed_reports=reports,
                                                       expected_seeds=config["seeds"])
                    terminal = aggregate["terminal"]
                    result["functional_validation"] = {"status": terminal,
                        "passed": True if terminal == "MULTISEED_FUNCTIONAL_PASS" else False if terminal == "FUNCTIONAL_MISMATCH" else None,
                        "all_seeds_present_exactly_once": aggregate["all_seeds_present_exactly_once"],
                        "seed_reports": reports, "scheduler_events": scheduler_events,
                        "scheduling": "WP015 MemoryAwareSeedScheduler; serial; no wait/retry on memory refusal"}
        except Exception as exc:
            result["functional_validation"] = {"status": "VALIDATOR_INFRASTRUCTURE_FAILURE", "passed": None,
                                                "error_type": type(exc).__name__}
        result["inputs_unchanged"] = all(file_sha256(path) == value for path, value in protected.items())
        if not result["inputs_unchanged"]:
            result["functional_validation"] = {"status": "INPUT_CHANGED", "passed": None}
            result["scoring"] = {"status": "INPUT_CHANGED", "delta_fmax_mhz": None}
        functional = result["functional_validation"]
        result["research_judgement"] = {"functional_scientific_delta_mhz":
            result["scoring"].get("delta_fmax_mhz") if functional["passed"] is True else None,
            "status": "FUNCTIONALLY_CERTIFIED_MEASURED" if functional["passed"] is True and result["scoring"]["status"] == "MEASURED"
                else "FUNCTIONALLY_PASSED_SCORING_UNAVAILABLE" if functional["passed"] is True else functional["status"]}
        result["validation_assessment"] = describe_validation(
            result["validation_plan"], functional, preparation=preparation_report)
        (output / "post-commit.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
