import asyncio
from types import SimpleNamespace

from optimizer_pipeline.backends.xilinx import observation
from optimizer_pipeline.dof import processes, preflight, vivado_readonly_worker as worker


def test_measure_requests_obey_retained_worker_protocol(tmp_path, monkeypatch):
    calls = []
    root = tmp_path / "root.dcp"
    root.write_bytes(b"offline fixture; no tools")

    class Session:
        def __init__(self, argv, *, close_command, **kwargs):
            assert worker.parse_request_line(close_command).operation_id == "worker.exit"
            self.startup_output = SimpleNamespace(data=b"")

        async def start(self):
            pass

        async def call(self, raw, *, terminal_marker, timeout_seconds):
            request = worker.parse_request_line(raw)
            assert request.timeout_seconds == timeout_seconds == 600
            assert terminal_marker == worker.terminal_marker(request.call_token)
            calls.append(request.operation_id)
            return SimpleNamespace(stdout=SimpleNamespace(data=b"fixture result"))

        async def close(self):
            return SimpleNamespace(to_dict=lambda **kwargs: {"cleanup_ok": True})

    monkeypatch.setattr(processes, "OwnedLineProtocolSession", Session)
    monkeypatch.setattr(preflight, "_verify_worker_attestation", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "parse_tcl_result", lambda *args: (0, "fixture"))
    monkeypatch.setattr(preflight, "_validate_reports", lambda reports: {
        "valid": True, "timing": {}, "utilization": {}, "route_status": {}
    })
    output = tmp_path / "capture"
    result = asyncio.run(observation.measure(root, output, tmp_path / "vivado"))
    assert calls == ["vivado.open_checkpoint", "vivado.report_timing_summary",
                     "vivado.report_route_status", "vivado.report_utilization", "vivado.close_design"]
    assert result["historical_replay"] is False
