from pathlib import Path


def test_process_audit_excludes_license_daemon_and_bounds_interval() -> None:
    from scripts.audit_wp014g_processes import EXACT_EXECUTABLES

    source = (Path(__file__).parents[1] / "scripts" / "audit_wp014g_processes.py").read_text()
    assert 'EXACT_EXECUTABLES = {"vivado", "xsim", "xelab", "xvlog"}' in source
    assert "lmgrd" not in EXACT_EXECUTABLES
    assert "min(args.interval_seconds, 10.0)" in source


def test_process_audit_excludes_complete_runner_ancestry_including_pid_one(
    monkeypatch,
) -> None:
    from scripts import audit_wp014g_processes as audit

    monkeypatch.setattr(audit.os, "getpid", lambda: 44)
    monkeypatch.setattr(audit.os, "getppid", lambda: 22)

    class FakeStat:
        def read_text(self) -> str:
            return "1 (runner) S 1 0 0"

    original_path = audit.Path

    def fake_path(value: str):
        if value == "/proc/22/stat":
            return FakeStat()
        return original_path(value)

    monkeypatch.setattr(audit, "Path", fake_path)
    assert audit.ancestors() == {1, 22, 44}
