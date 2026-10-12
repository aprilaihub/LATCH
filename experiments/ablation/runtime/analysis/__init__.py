"""Evidence-only deep analysis layer.

Analysis inspects existing DCP-state nodes and produces evidence for expansion
policies.  It does not create graph nodes or execute transitions.
"""

from .controller import OnlineAnalysisController
from .dynamic_loop import (
    AnalysisDecisionPolicy,
    AnalysisLoopState,
    MockLLMSelfStopPolicy,
    OpenAIAnalysisDecisionPolicy,
    ProviderAnalysisDecisionPolicy,
    ReservedLLMSelfStopPolicy,
    StaticPlanDecisionPolicy,
    build_analysis_decision_policy,
)
from .memory import AnalysisMemory
from .policies import (
    AnalysisBudgetPolicy,
    AnalysisStopPolicy,
    AnalysisTriggerPolicy,
    DiagnosisSynthesizer,
    HeuristicDiagnosisSynthesizer,
    NaiveOnlineAnalysisTriggerPolicy,
    StaticAnalysisBudgetPolicy,
    TimePressureStopPolicy,
)
from .report_probe_backends import (
    CachedAnalysisContextReportBackend,
    LocalVivadoReportBackend,
    ReportProbeBackend,
    VivadoMCPReportBackend,
    build_report_probe_backend,
)
from .runner import AnalysisRunner
from .schemas import (
    AnalysisAgentConfig,
    AnalysisBudget,
    AnalysisFeedbackRecord,
    AnalysisLoopDecision,
    AnalysisSessionResult,
    AnalysisStopDecision,
    AnalysisToolCall,
    AnalysisTriggerDecision,
    AnalysisUpdate,
    CandidatePrior,
    NodeDiagnosis,
)
from .tool_plan_policies import AnalysisToolPlanPolicy, MinimalReadOnlyToolPlanPolicy, StaticAnalysisToolPlanPolicy, build_analysis_tool_plan_policy

__all__ = [
    "AnalysisAgentConfig",
    "AnalysisBudget",
    "AnalysisBudgetPolicy",
    "AnalysisDecisionPolicy",
    "AnalysisFeedbackRecord",
    "AnalysisLoopDecision",
    "AnalysisLoopState",
    "AnalysisMemory",
    "AnalysisRunner",
    "AnalysisSessionResult",
    "AnalysisStopDecision",
    "AnalysisStopPolicy",
    "AnalysisToolCall",
    "AnalysisToolPlanPolicy",
    "AnalysisTriggerDecision",
    "AnalysisTriggerPolicy",
    "AnalysisUpdate",
    "CachedAnalysisContextReportBackend",
    "CandidatePrior",
    "DiagnosisSynthesizer",
    "HeuristicDiagnosisSynthesizer",
    "LocalVivadoReportBackend",
    "MinimalReadOnlyToolPlanPolicy",
    "MockLLMSelfStopPolicy",
    "NaiveOnlineAnalysisTriggerPolicy",
    "NodeDiagnosis",
    "OnlineAnalysisController",
    "OpenAIAnalysisDecisionPolicy",
    "ProviderAnalysisDecisionPolicy",
    "ReportProbeBackend",
    "ReservedLLMSelfStopPolicy",
    "StaticAnalysisBudgetPolicy",
    "StaticAnalysisToolPlanPolicy",
    "StaticPlanDecisionPolicy",
    "TimePressureStopPolicy",
    "VivadoMCPReportBackend",
    "build_analysis_decision_policy",
    "build_analysis_tool_plan_policy",
    "build_report_probe_backend",
]
