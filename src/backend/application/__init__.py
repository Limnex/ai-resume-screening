from __future__ import annotations

from ..config import Settings
from ..ports import LLMProvider, ScreeningRepository
from .analysis import AnalysisUseCases
from .criteria import CriteriaUseCases
from .jobs import JobUseCases
from .results import ResultReviewUseCases


class ApplicationServices:
    """应用用例集合；API 层只选择用例，不接触存储或模型实现。"""

    def __init__(self, settings: Settings, store: ScreeningRepository, llm: LLMProvider) -> None:
        """用同一组依赖创建各类用例，确保整个流程采用一致配置与数据源。"""
        """用同一组依赖创建各类用例，保证一次流程使用一致数据源和配置。"""
        self.jobs = JobUseCases(settings, store, llm)
        self.criteria = CriteriaUseCases(settings, store, llm)
        self.analyses = AnalysisUseCases(settings, store, llm)
        self.results = ResultReviewUseCases(settings, store, llm, self.analyses)

__all__ = [
    "AnalysisUseCases",
    "ApplicationServices",
    "CriteriaUseCases",
    "JobUseCases",
    "ResultReviewUseCases",
]
