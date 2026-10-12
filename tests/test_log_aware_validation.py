import json
import tempfile
import unittest
from pathlib import Path

from optimizer_pipeline.models import ExecutionResult
from optimizer_pipeline.validators import OutputValidator


class LogAwareValidationTest(unittest.TestCase):
    def _execution(self, run_dir: Path, success=True):
        out = run_dir / "output.dcp"
        out.write_text("fake dcp")
        summary = run_dir / "run_summary.json"
        summary.write_text(json.dumps({"total_runtime_seconds": 1.0}))
        return ExecutionResult(
            success=success,
            flow="lut_cone",
            input_dcp="in.dcp",
            output_dcp=str(out),
            run_dir=str(run_dir),
            summary_path=str(summary),
            metrics={"total_runtime_seconds": 1.0},
            exit_code=0 if success else 1,
        )

    def test_route_design_failure_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "vivado.log").write_text(
                "ERROR: [Common 17-39] 'route_design' failed due to earlier errors\n"
                "# of nets with routing errors: 1\n"
                "# of nets with some unrouted pins: 1\n"
                "# of nets with antennas/islands: 1\n"
            )
            result = OutputValidator().validate(self._execution(run_dir))
            self.assertFalse(result.valid)
            self.assertEqual(result.status, "INVALID")
            self.assertTrue(any("route_design_failed" in item for item in result.errors))

    def test_rapidwright_edif_fallback_is_warning_not_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "rapidwright.log").write_text(
                "ERROR - Error loading design\n"
                "Unable to find a readable EDIF file for the DCP\n"
                "Failed to auto-generate an EDIF file\n"
                "ERROR: Couldn't find vivado on PATH\n"
            )
            result = OutputValidator().validate(self._execution(run_dir))
            self.assertTrue(result.valid)
            self.assertEqual(result.status, "FALLBACK_USED")
            self.assertTrue(result.fallback_used)
            self.assertGreaterEqual(len(result.warnings), 1)


if __name__ == "__main__":
    unittest.main()
