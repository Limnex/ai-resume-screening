"""声明应用层依赖的模型和存储端口，而不绑定具体实现。

数据流：应用用例调用 Protocol -> 运行时注入 LLM/FileStore 适配器 -> 返回领域数据。
原理：像统一规格的插座，业务层只认接口，便于替换真实服务或测试替身。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from .schemas import CandidateAnalysisOutput, CriteriaExtractionOutput


T = TypeVar("T")


@dataclass(slots=True)
class LLMResult(Generic[T]):
    """同时携带类型化结果、模型原始 JSON 和供应商请求标识。"""
    parsed: T
    raw: dict[str, Any]
    provider_request_id: str | None


class LLMProvider(Protocol):
    """规定模型适配器必须支持标准提取和候选人分析两条数据流。"""

    # 原始任务数据进入模型，校验后的标准草稿返回应用层。
    async def extract_criteria(self, job: dict[str, Any]) -> LLMResult[CriteriaExtractionOutput]: ...

    # 原始 JD、确认标准与简历共同进入模型，输出逐项分析。
    async def analyze_candidate(
        self,
        job: dict[str, Any],
        criteria: list[dict[str, Any]],
        resume: dict[str, Any],
    ) -> LLMResult[CandidateAnalysisOutput]: ...


class ScreeningRepository(Protocol):
    """应用层依赖的持久化端口；文件或数据库都可实现。"""

    # 任务创建一次写入任务、简历、隔离的参考数据与初始审计事件。
    def create_job(
        self,
        job: dict[str, Any],
        resumes: list[dict[str, Any]],
        benchmark_references: list[dict[str, Any]],
        audit_events: list[dict[str, Any]],
    ) -> None: ...

    # 以下读写方法让应用层按任务聚合保存状态，不暴露文件路径等实现细节。
    def get_job(self, job_id: str) -> dict[str, Any]: ...
    def save_job(self, job: dict[str, Any]) -> None: ...
    def read_resumes(self, job_id: str) -> list[dict[str, Any]]: ...
    def find_resume(self, job_id: str, resume_id: str) -> dict[str, Any] | None: ...
    # 标准先保存 AI 草稿，HR 确认后再按版本持久化正式输入。
    def save_criteria_draft(self, job_id: str, draft: dict[str, Any]) -> None: ...
    def get_criteria_draft(self, job_id: str) -> dict[str, Any] | None: ...
    def save_criteria_version(self, job_id: str, version: int, data: dict[str, Any]) -> None: ...
    def get_criteria_version(self, job_id: str, version: int) -> dict[str, Any] | None: ...
    # 分析元数据保存总体进度，JSONL 分析项保存逐候选人结果。
    def save_analysis_meta(self, job_id: str, analysis_id: str, meta: dict[str, Any]) -> None: ...
    def get_analysis_meta(self, job_id: str, analysis_id: str) -> dict[str, Any] | None: ...
    def initialize_analysis_items(self, job_id: str, analysis_id: str) -> None: ...
    def append_analysis_item(self, job_id: str, analysis_id: str, item: dict[str, Any]) -> None: ...
    def read_analysis_items(self, job_id: str, analysis_id: str) -> list[dict[str, Any]]: ...
    def mark_analyses_stale(self, job_id: str) -> int: ...
    # 复核和审计均采用追加式数据流，历史记录不被覆盖。
    def append_review(self, job_id: str, review: dict[str, Any]) -> None: ...
    def read_reviews(self, job_id: str) -> list[dict[str, Any]]: ...
    def append_audit(self, job_id: str, event: dict[str, Any]) -> None: ...
    def read_audit(self, job_id: str) -> list[dict[str, Any]]: ...
    # 幂等记录缓存首个成功响应，重复请求可重放而不重复产生副作用。
    def get_idempotency(self, job_id: str, key: str) -> dict[str, Any] | None: ...
    def save_idempotency(self, job_id: str, key: str, value: dict[str, Any]) -> None: ...
    # 上传先进入临时预览区，用户选岗后才转换成正式任务。
    def save_import_preview(
        self,
        preview_id: str,
        file_name: str,
        file_bytes: bytes,
        metadata: dict[str, Any],
    ) -> None: ...
    def get_import_preview(self, preview_id: str) -> tuple[str, bytes, dict[str, Any]]: ...
