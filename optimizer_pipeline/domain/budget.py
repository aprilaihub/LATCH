"""Small run-time budget ledger for hand-written run pipelines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class BudgetLedger:
    max_eda_runs: int
    max_parallel_jobs: int = 1
    scheduled_eda_runs: int = 0
    completed_eda_runs: int = 0
    failed_eda_runs: int = 0
    max_analysis_sessions: int = 0
    analysis_sessions: int = 0

    def __post_init__(self) -> None:
        self.max_eda_runs = max(0, int(self.max_eda_runs))
        self.max_parallel_jobs = max(1, int(self.max_parallel_jobs))
        self.scheduled_eda_runs = max(0, int(self.scheduled_eda_runs))
        self.completed_eda_runs = max(0, int(self.completed_eda_runs))
        self.failed_eda_runs = max(0, int(self.failed_eda_runs))
        self.max_analysis_sessions = max(0, int(self.max_analysis_sessions))
        self.analysis_sessions = max(0, int(self.analysis_sessions))

    @property
    def remaining_eda_runs(self) -> int:
        return max(0, self.max_eda_runs - self.scheduled_eda_runs)

    @property
    def remaining_analysis_sessions(self) -> int:
        if self.max_analysis_sessions <= 0:
            return 0
        return max(0, self.max_analysis_sessions - self.analysis_sessions)

    def reserve_eda_run(self, *, count: int = 1) -> bool:
        count = max(1, int(count))
        if self.remaining_eda_runs < count:
            return False
        self.scheduled_eda_runs += count
        return True

    def release_eda_reservation(self, *, count: int = 1) -> None:
        count = max(1, int(count))
        self.scheduled_eda_runs = max(self.completed_eda_runs + self.failed_eda_runs, self.scheduled_eda_runs - count)

    def complete_eda_run(self, *, success: bool = True) -> None:
        if success:
            self.completed_eda_runs += 1
        else:
            self.failed_eda_runs += 1

    def reserve_analysis_session(self) -> bool:
        if self.max_analysis_sessions <= 0:
            return False
        if self.analysis_sessions >= self.max_analysis_sessions:
            return False
        self.analysis_sessions += 1
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_eda_runs": self.max_eda_runs,
            "scheduled_eda_runs": self.scheduled_eda_runs,
            "completed_eda_runs": self.completed_eda_runs,
            "failed_eda_runs": self.failed_eda_runs,
            "remaining_eda_runs": self.remaining_eda_runs,
            "max_parallel_jobs": self.max_parallel_jobs,
            "max_analysis_sessions": self.max_analysis_sessions,
            "analysis_sessions": self.analysis_sessions,
            "remaining_analysis_sessions": self.remaining_analysis_sessions,
        }
