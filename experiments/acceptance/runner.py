"""Thin serial batch over the existing module entries and tool adapters."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone
from importlib import metadata

from integrations.llm.runtime.batch_budget import BatchBudget, BatchStopped
from .config import VALIDATION, TOOL_ORDER, PAID_ORDER, validate_config, validate_tools_config


def dump(path, value):
    path = Path(path)
    with path.open("x") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write("\n")
    os.chmod(path, 0o600)


def dof_reached(facts):
    validation = facts.get("functional_validation") or {}
    reports = validation.get("seed_reports", [])
    return (facts.get("planning_terminal") == "COMMITTED" and facts.get("execution_started") is True
            and facts.get("physical_valid") is True and facts.get("root_unchanged") is True
            and (facts.get("post_commit_scoring") or {}).get("status") == "MEASURED"
            and [r.get("stimulus_seed") for r in reports] == VALIDATION["seeds"]
            and validation.get("all_seeds_present_exactly_once") is True
            and validation.get("passed") in (True, False)
            and not any(r.get("infrastructure_failure") for r in reports))


def public_dof(facts):
    from experiments.dof.runtime.validation_scope import public_validation_scope
    validation = facts.get("functional_validation") or {}
    strict = facts.get("strict_boundary_certificate") or {}
    return {key: facts.get(key) for key in ("planning_terminal", "execution_started", "physical_valid", "physical_terminal",
        "root_unchanged", "post_commit_scoring", "research_judgement", "infrastructure_failure")} | {
        "functional_status": validation.get("status"), "functional_passed": validation.get("passed"),
        "strict_g": {k: strict.get(k) for k in ("strict_boundary_exercised", "mechanism_family", "noncertificate_reason")},
        "seeds": [{k: r.get(k) for k in ("stimulus_seed", "passed", "cycles_simulated", "mismatch_count",
                   "protocol_mismatch_count", "infrastructure_failure", "infrastructure_reason")}
                  for r in validation.get("seed_reports", [])], "execution_scoring_validation_reached": dof_reached(facts)} | public_validation_scope(facts)


def public_ablation(output):
    output = Path(output)
    summary_path, gate_path = output / "pipeline_summary.json", output / "equivalence_gate.json"
    summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {}
    gate = json.loads(gate_path.read_text()) if gate_path.is_file() else {}
    result = {"configured_attempt_budget": summary.get("max_total_runs"),
        "scheduled_attempts": summary.get("scheduled_runs"), "physical_validity": summary.get("validation_counts"),
        "stop_reason": (summary.get("state") or {}).get("stop_reason"),
        "has_final_output": bool(summary.get("final_output_dcp")),
        "functional_check": {k:gate.get(k) for k in ("status","passed","phase1_passed","phase2_passed","phase2_skipped","infrastructure_failure")},
        "functional_error_present": bool(gate.get("error")), "executions": [],
        "research_judgement": "single-design smoke; not manuscript evidence"}
    if (output / "run.db").is_file():
        db = sqlite3.connect(f"file:{output / 'run.db'}?mode=ro",uri=True)
        try:
            for status,error_type,encoded in db.execute("SELECT status,error_type,metrics_json FROM outcomes"):
                metrics = json.loads(encoded)
                result["executions"].append({"status":status,"error_type":error_type,
                    "scoring":{k:metrics.get(k) for k in ("final_fmax_mhz","final_wns_ns","validation_status")}})
        finally:
            db.close()
    result["executed_candidates"] = len(result["executions"])
    result["unexplained_execution_failures"] = sum(r["status"] != "SUCCEEDED" for r in result["executions"])
    return result


def ablation_research_stop(name, summary):
    """A paid research early-stop is not physical acceptance or an A exemption."""
    state = summary.get("state") or {}
    return (name in {"S1", "S2", "S3"} and summary.get("final_output_dcp") is None
            and state.get("status") == "COMPLETED"
            and state.get("stop_reason") in {"NO_VALID_CANDIDATE", "LLM_REQUESTED_STOP"})


def module(argv, *, output, timeout=120000):
    from optimizer_pipeline.backends.xilinx.owned_process import run_owned
    result = run_owned([sys.executable, "-m", *argv], timeout=timeout, text=True)
    # Raw stdout may contain model/tool data. Restricted local record only.
    logs = Path(output)
    logs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with logs.open("x") as handle:
        handle.write(result.stdout + "\n" + result.stderr)
    os.chmod(logs, 0o600)
    return result


class AcceptanceBatch:
    def __init__(self, cfg, config_path):
        self.cfg = validate_tools_config(cfg)
        self.root, self.output = Path(cfg["input"]), Path(cfg["output"])
        self.config_path = Path(config_path).resolve()
        self.ledger = None

    def checklist(self):
        return {"design": self.root.name, "golden_is_input": True, "clock": VALIDATION["target_clock"],
            "period_ns": 1.5, "tools": self.cfg["tools"], "rapidwright_revision": self.cfg["rapidwright_revision"],
            "validation": VALIDATION, "execution_timeout_seconds": 7200,
            "cost": self.cfg["cost"], "condition_order_tools": TOOL_ORDER, "condition_order_paid": PAID_ORDER,
            "output": str(self.output), "separate_tool_and_paid_authorizations_required": True,
            "executed": False, "historical_replay": False, "wp016": "historical/nondefault"}

    def prepare(self):
        # All research batches, including paid callers, retain the model budget
        # checks. A no-model generic harness only uses versions/memory helpers.
        validate_config(self.cfg)
        if self.output.exists() and not (self.output / "batch.db").is_file():
            raise ValueError("Existing output without this batch ledger cannot be reused")
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.output, 0o700)
        self.ledger = BatchBudget(Path(self.cfg["ledger_path"]), self.cfg["cost"])
        materials = self.output / "materials"
        if not materials.exists():
            materials.mkdir(mode=0o700)
            dump(materials / "tools.json", self.cfg["tools"])
            dump(materials / "validation.json", VALIDATION)
            dump(materials / "dof-cost.json", self.cfg["cost"] | {"max_requests": 4})
            dump(materials / "locked-config.json", self.cfg)
        elif json.loads((materials / "locked-config.json").read_text()) != self.cfg:
            raise ValueError("Cannot change inputs or parameters of an existing batch")
        return materials

    def once(self, name, operation):
        states = self.ledger.summary()["conditions"]
        if name in states:
            if states[name] in {"REACHED", "RESEARCH_RESULT", "INTERRUPTED"}:
                return
            raise BatchStopped("Started or incomplete condition cannot be replayed: " + name)
        self.ledger.begin_condition(name)
        try:
            status = operation()
        except (KeyboardInterrupt, SystemExit):
            self.ledger.finish_condition(name, "INTERRUPTED")
            # Never restart that condition. A pending network reservation still
            # blocks paid resumption as an uncertain exchange.
            raise
        except BaseException:
            self.ledger.finish_condition(name, "INCOMPLETE")
            self.ledger.stop("CONDITION_INFRASTRUCTURE_OR_UNREACHED")
            raise
        self.ledger.finish_condition(name, status or "REACHED")

    def versions(self):
        from optimizer_pipeline.backends.xilinx.configured_environment import configured_environment
        from optimizer_pipeline.backends.xilinx.owned_process import run_owned
        from optimizer_pipeline.backends.xilinx.toolchain import check_environment
        tools = self.cfg["tools"]
        if sys.version_info[:2] != (3, 12) or Path(sys.executable).absolute().parent != Path(tools["python"]).parent:
            raise ValueError("Use the isolated, configured Python 3.12 interpreter")
        os.environ.update(configured_environment(**tools))
        revision = run_owned(["git", "-C", tools["rapidwright"], "rev-parse", "HEAD"], timeout=30, text=True)
        if revision.returncode or revision.stdout.strip() != self.cfg["rapidwright_revision"]:
            raise ValueError("RapidWright revision mismatch; no update is performed")
        record = check_environment(strict=True, owned=True)
        java = run_owned([str(Path(tools["java_home"]) / "bin/java"), "-version"], timeout=120, text=True)
        if java.returncode or '"21.0.1"' not in java.stdout + java.stderr:
            raise ValueError("Java version mismatch")
        record["python"] = sys.version.split()[0]
        record["rapidwright_revision"] = revision.stdout.strip()
        record["dependencies"] = sorted({(d.metadata["Name"], d.version) for d in metadata.distributions()})
        dump(self.output / "versions.json", record)

    def memory_check(self):
        from optimizer_pipeline.dof.validation_scheduler import linux_memory_bytes, MemoryAwareSeedScheduler, ValidationSeed
        total, available = linux_memory_bytes()
        scheduler = MemoryAwareSeedScheduler([ValidationSeed("smoke", "root", "check", 0, "unknown")],
            maximum=1, target=1, reserve_fraction=.20)
        if not scheduler.refill(running=[], total_memory=total, available_memory=available):
            raise RuntimeError("Memory refuses an unknown-size task with 20% reserve; no wait or reduced validation")

    def check_root(self):
        # Reuse the capture's existing input binding; do not invent a new
        # baseline or turn an uncommitted code tree into a hash contract.
        from optimizer_pipeline.scoring import file_sha256
        context = json.loads((self.output / "materials/context.json").read_text())
        if file_sha256(self.root) != context["root_sha256"]:
            raise RuntimeError("Original input changed since capture")

    def golden_self(self):
        self.memory_check()
        from optimizer_pipeline.backends.xilinx.functional_validation import prepare_workspace
        tools = self.cfg["tools"]
        report = prepare_workspace(golden=self.root, revised=self.root, output=self.output / "golden-self",
            python=Path(tools["python"]), vivado=Path(tools["vivado"]), seeds=VALIDATION["seeds"],
            vectors=200, precheck_vectors=100, timeout_seconds=10800)
        if report["workspace"] is None:
            raise RuntimeError("Golden self precheck did not pass")

    def capture(self):
        self.memory_check()
        from experiments.dof.runtime.captured_context import context_from_capture
        from .templates import fixed_messages
        result = module(["optimizer_pipeline.backends.xilinx.observation", "--input", str(self.root),
            "--output", str(self.output / "capture"), "--vivado", self.cfg["tools"]["vivado"],
            "--target-clock", VALIDATION["target_clock"], "--score-timeout", "900"],
            output=self.output / "logs/capture.log", timeout=28800)
        if result.returncode:
            raise RuntimeError("Capture failed; no fabricated observations are substituted")
        context = context_from_capture(root=self.root, capture=self.output / "capture", target_clock=VALIDATION["target_clock"])
        if context["observation"]["target_clock"]["clock_period_ns"] != 1.5:
            raise ValueError("Captured clock period differs from the approved 1.5 ns")
        # Validate all three templates before dispatching any COMMIT.
        messages = {c: fixed_messages(c, root=self.root, context=context) for c in ("recipe", "direct", "constructed")}
        dump(self.output / "materials/context.json", context)
        for c, value in messages.items():
            dump(self.output / ("materials/" + c + ".json"), value)

    def dof(self, condition, *, paid):
        self.check_root()
        self.memory_check()
        name = ("dof-" if paid else "fixed-") + condition
        output = self.output / name
        material = self.output / "materials"
        argv = ["experiments.dof", "--condition", condition, "--mode", "real" if paid else "prepared-real",
            "--input", str(self.root), "--golden", str(self.root), "--output", str(output),
            "--context", str(material / "context.json"), "--tool-config", str(material / "tools.json"),
            "--validation-config", str(material / "validation.json")]
        if paid:
            argv += ["--provider", "openai", "--model", "gpt-5.6-luna", "--cost-config", str(material / "dof-cost.json"),
                     "--acceptance-config", str(self.config_path)]
        else:
            argv += ["--messages", str(material / (condition + ".json"))]
        module(argv, output=self.output / ("logs/" + name + ".log"), timeout=7200 + 28800 + 10800 + 5*3600)
        facts = json.loads((output / "summary.json").read_text())
        if facts.get("infrastructure_failure") or facts.get("root_unchanged") is not True:
            raise RuntimeError("DoF infrastructure/input protection failure")
        if paid and not facts.get("execution_started") and facts.get("planning_terminal") in {
                "NO_VALID_SUBMISSION", "INVALID_SUBMISSION", "ABORTED", "STOP"}:
            return "RESEARCH_RESULT"  # Not an execution/validation pass.
        if not dof_reached(facts):
            raise RuntimeError("DoF execution/scoring/all-seed validation not reached")

    def ablation(self, name):
        self.check_root()
        self.memory_check()
        material = self.output / "materials"
        output = self.output / name
        source = self.root
        condition = "S0" if name.startswith("S0-") else name
        argv = ["experiments.ablation", "--condition", condition, "--mode", "real", "--output", str(output),
            "--tool-config", str(material / "tools.json"), "--acceptance-config", str(self.config_path)]
        if name == "S0-extension":
            prior = self.output / "S0-baseline"
            summary = json.loads((prior / "pipeline_summary.json").read_text())
            source = Path(summary.get("final_output_dcp") or summary.get("input_dcp") or summary.get("root_dcp") or "")
            if not source.is_file():
                raise ValueError("S0 baseline recorded output missing")
            argv += ["--s0-phase", "extension", "--prior-run", str(prior)]
        if condition != "S0":
            argv += ["--provider", "openai", "--model", "gpt-5.6-luna", "--cost-budget-usd", "10"]
        argv += ["--input", str(source)]
        result = module(argv, output=self.output / ("logs/" + name + ".log"))
        self.check_root()
        start = result.stdout.rfind("\n{")
        facts = json.loads(result.stdout[start + 1:] if start >= 0 else result.stdout)
        gate = facts.get("functional_check") or {}
        failures = [p for p in output.rglob("probe-status.json") if json.loads(p.read_text())["status"] != "MEASURED"]
        if self.ledger.summary()["stopped"] or failures or gate.get("infrastructure_failure"):
            raise RuntimeError("Ablation infrastructure or billing failure")
        if not (output / "run.db").is_file():
            raise RuntimeError("Ablation execution record missing")
        from optimizer_pipeline.core.store import SQLiteRunStore
        store = SQLiteRunStore(output / "run.db")
        try:
            outcomes = store.outcomes()
            unresolved = [o for o in outcomes if o.status.value != "SUCCEEDED"]
            if unresolved:
                raise RuntimeError("Ablation execution failures require review; not a physical acceptance pass")
        finally:
            store.close()
        summary = json.loads((output / "pipeline_summary.json").read_text())
        if ablation_research_stop(name, summary):
            dump(output / "acceptance-result.json", {"condition": name,
                "execution_scoring_validation_reached": False,
                "research_result": "normal early stop without a final candidate; not acceptance"} | public_ablation(output))
            return "RESEARCH_RESULT"
        if not outcomes:
            raise RuntimeError("Ablation physical candidate execution not reached")
        if gate.get("passed") not in (True, False) or gate.get("phase2_skipped") is not False:
            raise RuntimeError("Ablation final functional check not executed")
        dump(output / "acceptance-result.json", {k: facts.get(k) for k in ("condition", "pipeline_ok", "execution_completed")} | public_ablation(output))

    def run(self, phase, price_evidence=None):
        self.prepare()
        if phase == "tools":
            self.once("versions", self.versions)
            # On resume reconstruct the same explicit environment, without changing versions.
            from optimizer_pipeline.backends.xilinx.configured_environment import configured_environment
            os.environ.update(configured_environment(**self.cfg["tools"]))
            self.once("golden-self", self.golden_self)
            self.once("capture", self.capture)
            for c in ("recipe", "direct", "constructed"):
                self.once("fixed-" + c, lambda c=c: self.dof(c, paid=False))
            self.once("S0-baseline", lambda: self.ablation("S0-baseline"))
            self.once("S0-extension", lambda: self.ablation("S0-extension"))
        else:
            states = self.ledger.summary()["conditions"]
            if any(states.get(name) != "REACHED" for name in TOOL_ORDER):
                raise BatchStopped("Every no-model capability must be reached before paid calls")
            self.run_paid_conditions(price_evidence)

    def run_paid_conditions(self, price_evidence=None, *, names=None):
        """Shared paid dispatch; callers must establish their explicit A evidence."""
        evidence = json.loads(Path(price_evidence).read_text()) if price_evidence else self.cfg["cost"]
        stamp = datetime.fromisoformat(evidence["verified_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Price verification time must include a timezone")
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if age < 0 or age > 86400:
            raise BatchStopped("Refresh official price evidence and obtain paid approval before starting")
        self.ledger.record_price_evidence(evidence)
        from optimizer_pipeline.backends.xilinx.configured_environment import configured_environment
        os.environ.update(configured_environment(**self.cfg["tools"]))
        selected = PAID_ORDER if names is None else list(names)
        if selected != [n for n in PAID_ORDER if n in selected]:
            raise ValueError("Paid dispatch must be an ordered, unique approved subset")
        for name in selected:
            self.once(name, (lambda c=name[4:]: self.dof(c, paid=True)) if name.startswith("dof-") else (lambda n=name: self.ablation(n)))

    def report(self):
        result = {"scope": "one design; migration coverage only", "historical_replay": False,
                  "performance_update": False, "paths": {}, "accounting": None}
        evidence_path = self.output / "accepted-tool-evidence.json"
        if evidence_path.is_file():
            result["accepted_tool_evidence"] = json.loads(evidence_path.read_text())
        for name in TOOL_ORDER + PAID_ORDER:
            output = self.output / name
            if name.startswith(("dof-", "fixed-")) and (output / "summary.json").is_file():
                result["paths"][name] = public_dof(json.loads((output / "summary.json").read_text()))
            elif (output / "acceptance-result.json").is_file():
                result["paths"][name] = json.loads((output / "acceptance-result.json").read_text())
            elif name.startswith("S") and (output / "run.db").is_file():
                result["paths"][name] = public_ablation(output)
            else:
                result["paths"][name] = {"status": "NOT_STARTED_OR_SEE_RESTRICTED_RUN_RECORD"}
        if (self.output / "batch.db").is_file():
            db = sqlite3.connect(f"file:{self.output / 'batch.db'}?mode=ro", uri=True)
            try:
                result["accounting"] = {"stopped": db.execute("SELECT stopped FROM batch WHERE id=1").fetchone()[0],
                    "requests": [dict(zip(("scope", "status", "reserved_usd", "accounted_usd", "input_tokens", "output_tokens"), row))
                                 for row in db.execute("SELECT scope,status,reserved,accounted,input_tokens,output_tokens FROM requests")],
                    "conditions": dict(db.execute("SELECT name,status FROM conditions")), "hard_billing_cap": False}
                for name, status in result["accounting"]["conditions"].items():
                    result["paths"].setdefault(name,{})["batch_condition_status"] = status
            finally:
                db.close()
        return result
