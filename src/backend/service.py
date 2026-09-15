"""兼容导入层；新代码应从 src.backend.application 导入用例。"""

from .application import (
    AnalysisUseCases,
    ApplicationServices,
    CriteriaUseCases,
    JobUseCases,
    ResultReviewUseCases,
)
from .config import Settings
from .ports import LLMProvider, ScreeningRepository

__all__ = [
    "AnalysisUseCases",
    "ApplicationServices",
    "CriteriaUseCases",
    "JobUseCases",
    "ResultReviewUseCases",
    "ScreeningService",
]


class ScreeningService:
    """旧导入路径的兼容门面；新代码应直接注入具体应用用例。"""

    def __init__(self, settings: Settings, store: ScreeningRepository, llm: LLMProvider) -> None:
        """沿用旧入口创建新版用例集合，保证依赖注入方式不变。"""
        self._services = ApplicationServices(settings, store, llm)

    def __getattr__(self, name: str):
        """把旧服务方法名转发给对应的新用例对象。"""
        for component in (
            self._services.jobs,
            self._services.criteria,
            self._services.analyses,
            self._services.results,
        ):
            if hasattr(component, name):
                return getattr(component, name)
        raise AttributeError(name)
