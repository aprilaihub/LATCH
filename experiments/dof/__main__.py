"""Portable WP015 migration trial: mock, prepared-real or fresh real; not historical replay."""
import argparse
import asyncio
import json
from pathlib import Path

from .runtime.prepared import CONDITIONS, mock_material, run_prepared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", choices=list(CONDITIONS), required=True)
    parser.add_argument("--mode", choices=["mock", "prepared-real", "real"], required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", type=Path, help="Root-bound captured O/Q/compiler context JSON")
    parser.add_argument("--messages", type=Path, help="Fixed planner-response JSON array")
    parser.add_argument("--python", type=Path, help="Exact approved worker interpreter")
    parser.add_argument("--vivado", type=Path, help="Exact approved Vivado 2025.1 executable")
    parser.add_argument("--rapidwright", type=Path, help="Exact approved checkout with jars/")
    parser.add_argument("--java-home", type=Path, help="Exact approved Java home with bin/java")
    parser.add_argument("--tool-config", type=Path, help="Explicit python/vivado/rapidwright/java_home path JSON; no hidden environment discovery")
    parser.add_argument("--provider", help="Exact shared provider profile; no fallback")
    parser.add_argument("--model", help="Exact provider model ID")
    parser.add_argument("--cost-config", type=Path, help="Current price evidence and accounted/request limits JSON")
    parser.add_argument("--validation-config", type=Path, help="Explicit target clock, seeds, vectors and timeouts JSON")
    parser.add_argument("--golden", type=Path, help="Golden checkpoint used for functional comparison")
    parser.add_argument("--scorer-script", type=Path, help="Read-only scorer; defaults to installed full checkout resource")
    parser.add_argument("--capture", type=Path, help="Existing complete migrated observation directory; otherwise real captures fresh")
    parser.add_argument("--acceptance-config", type=Path, help="Opt-in shared batch ledger and bounded client")
    args = parser.parse_args()
    try:
        output = args.output.resolve()
        if output.exists():
            raise ValueError("Output must be a new directory")
        planner = post_commit = None
        if args.tool_config:
            from optimizer_pipeline.backends.xilinx.configured_environment import load_tool_config
            tools = load_tool_config(args.tool_config)
            for name in ("python", "vivado", "rapidwright", "java_home"):
                given = getattr(args, name)
                configured = tools.get(name)
                if configured is None:
                    raise ValueError("Tool configuration is missing: " + name)
                if given and given.resolve() != Path(configured).resolve():
                    raise ValueError("Conflicting explicit tool paths: " + name)
                setattr(args, name, Path(configured))
        if args.mode == "real" or args.tool_config or args.rapidwright or args.java_home:
            import os
            from optimizer_pipeline.backends.xilinx.configured_environment import configured_environment
            os.environ.update(configured_environment(python=args.python, vivado=args.vivado,
                rapidwright=args.rapidwright, java_home=args.java_home))
        if args.mode == "real":
            import os
            if not args.provider or not args.model:
                raise ValueError("Real requires an exact provider/model, not automatic selection")
            required = (args.input, args.python, args.vivado, args.cost_config, args.validation_config, args.golden)
            if not all(p and p.is_file() for p in required):
                raise ValueError("Real requires input, approved Python/Vivado, cost/validation configuration and golden DCP")
            if not all(os.access(p, os.X_OK) for p in (args.python, args.vivado)):
                raise ValueError("Approved Python and Vivado must be executable")
            from .runtime.model_planning import ModelPlanner
            from .runtime.post_commit import PostCommit, validate_config
            validation_config = validate_config(json.loads(args.validation_config.read_text()))
            client = None
            if args.acceptance_config:
                from integrations.llm.runtime.batch_budget import client_from_config
                client = client_from_config(args.acceptance_config, "dof-" + args.condition)
            planner = ModelPlanner(condition=args.condition, provider=args.provider, model=args.model,
                cost_config=json.loads(args.cost_config.read_text()), client=client)
            root = args.input.resolve(strict=True)
            scorer = args.scorer_script or Path(__file__).resolve().parents[2] / "scripts/e1_score_dcp.tcl"
            if not scorer.is_file():
                raise ValueError("Full checkout scorer resource missing")
            post_commit = PostCommit(config=validation_config, golden=args.golden.resolve(strict=True),
                python=args.python.absolute(), vivado=args.vivado.resolve(), scorer_script=scorer.resolve())
            if args.context:
                context = json.loads(args.context.read_text())
            else:
                from .runtime.captured_context import context_from_capture
                capture = args.capture.resolve() if args.capture else output.with_name(output.name + "-capture")
                if not args.capture:
                    from optimizer_pipeline.backends.xilinx.owned_process import run_owned
                    completed = run_owned([str(args.python.absolute()), "-m", "optimizer_pipeline.backends.xilinx.observation",
                        "--input", str(root), "--output", str(capture), "--vivado", str(args.vivado.resolve()),
                        "--target-clock", validation_config["target_clock"], "--score-timeout", str(validation_config["score_timeout_seconds"])],
                        timeout=28800, text=True)
                    if completed.returncode != 0:
                        raise RuntimeError("Fresh observation failed; retained capture is incomplete, no trial or substitution performed")
                context = context_from_capture(root=root, capture=capture, target_clock=validation_config["target_clock"])
            context.update(provider=args.provider, model=args.model)
            messages = []
        elif args.mode == "prepared-real":
            if not all(value and value.is_file() for value in (args.input, args.context, args.messages, args.python, args.vivado)):
                raise ValueError("Prepared-real requires input, captured context, messages, approved Python and Vivado files")
            context = json.loads(args.context.read_text())
            messages = json.loads(args.messages.read_text())
            root = args.input.resolve(strict=True)
            if args.validation_config:
                if not args.golden or not args.golden.is_file():
                    raise ValueError("Post-commit validation requires explicit golden DCP")
                from .runtime.post_commit import PostCommit
                scorer = args.scorer_script or Path(__file__).resolve().parents[2] / "scripts/e1_score_dcp.tcl"
                if not scorer.is_file():
                    raise ValueError("Read-only scorer resource missing")
                post_commit = PostCommit(config=json.loads(args.validation_config.read_text()), golden=args.golden.resolve(),
                    python=args.python.absolute(), vivado=args.vivado.resolve(), scorer_script=scorer.resolve())
        else:
            context, messages = mock_material(args.condition)
            if args.context:
                context = json.loads(args.context.read_text())
            if args.messages:
                messages = json.loads(args.messages.read_text())
            if args.input:
                root = args.input.resolve(strict=True)
            else:
                # Separate sibling fixture: never create or change a caller-owned input.
                root = output.with_name(output.name + "-mock-input.dcp")
                root.parent.mkdir(parents=True, exist_ok=True)
                with root.open("x") as handle:
                    handle.write("mock root, not a physical checkpoint\n")
        if not isinstance(context, dict) or not isinstance(messages, list):
            raise ValueError("Context must be an object; messages must be an array")
        facts = asyncio.run(run_prepared(condition=args.condition, mode=args.mode, root=root,
            output=output, context=context, messages=messages, python=args.python, vivado=args.vivado,
            planner=planner, post_commit=post_commit))
        print(json.dumps(facts, indent=2))
    except (ValueError, RuntimeError, OSError, ImportError, KeyError) as exc:
        parser.exit(2, f"Cannot run requested trial: {exc}\n")
    if args.mode == "real" or args.validation_config:
        passed = (facts["planning_terminal"] == "COMMITTED" and facts.get("physical_valid") is True
            and isinstance(facts["post_commit_scoring"], dict) and facts["post_commit_scoring"]["status"] == "MEASURED"
            and facts["functional_validation"]["passed"] is True)
    else:
        passed = facts["planning_terminal"] == "COMMITTED" and facts.get("physical_valid") is not False
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
