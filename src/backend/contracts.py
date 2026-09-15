"""定义后端返回给前端的 API 响应结构。

数据流：应用用例产生的字典 -> Pydantic 契约校验与序列化 -> HTTP JSON 响应。
原理：显式契约把字段类型和可选性固定下来，防止内部数据结构无意泄漏到前端。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import CriterionCategory, CriterionType, EvidenceType, Layer, MatchStatus, StoredReviewDecision


class ApiContract(BaseModel):
    """前后端共享的严格 API 契约基类。"""

    model_config = ConfigDict(extra="forbid")


class RequestTrackedResponse(ApiContract):
    """所有响应共享的请求标识，用于把前端报错关联到服务端日志。"""
    request_id: str


class JobView(ApiContract):
    """任务主状态视图，由任务元数据转换后嵌入多个接口响应。"""
    job_role: str
    jd_text: str
    candidate_limit: int
    selected_role: str | None = None


class ImportSummary(ApiContract):
    """展示 CSV 导入总量、岗位过滤量和实际候选人数。"""
    total: int
    valid: int
    invalid: int
    duplicates: int
    invalid_rows: list[dict[str, Any]] = Field(default_factory=list)
    duplicate_rows: list[dict[str, Any]] = Field(default_factory=list)


class CriterionView(ApiContract):
    """向前端暴露一条可解释、可确认的筛选标准。"""
    id: int
    name: str
    category: CriterionCategory
    type: CriterionType
    source: str
    source_evidence: str = ""
    source_evidence_valid: bool = False
    requirement: str = ""
    minimum_value: float | None = None


class AnalysisProgress(ApiContract):
    """把分析计数转换为轮询页所需的进度信息。"""
    status: str | None = None
    source_total: int | None = None
    selected_total: int | None = None
    processed: int | None = None
    succeeded: int | None = None
    failed: int | None = None
    remaining: int | None = None
    progress: float | None = None
    limit_applied: bool | None = None


class HealthResponse(RequestTrackedResponse):
    """服务健康状态及模型是否已正确配置。"""
    status: Literal["ok"]
    storage: str
    llm_configured: bool
    demo_mode: bool
    max_candidates_per_run: int


class JobCreatedResponse(RequestTrackedResponse):
    """任务创建成功后的标识、状态和导入摘要。"""
    job_id: str
    status: str
    job: JobView
    import_summary: ImportSummary


class JobDetailResponse(RequestTrackedResponse):
    """任务详情及标准、分析、复核阶段的当前状态。"""
    job_id: str
    status: str
    job: JobView
    import_summary: ImportSummary
    criteria_version: int | None
    analysis_progress: AnalysisProgress | None
    created_at: str


class CriteriaResponse(RequestTrackedResponse):
    """标准草稿或确认版本，以及其来源和可编辑状态。"""
    job_id: str
    criteria_version: int | None
    criteria: list[CriterionView]
    confirmed_at: str | None = None


class CriteriaConfirmedResponse(RequestTrackedResponse):
    """HR 确认后返回的新版本号、标准内容和失效分析数量。"""
    job_id: str
    criteria_version: int
    status: Literal["criteria_confirmed"]
    confirmed_at: str


class AnalysisStartedResponse(RequestTrackedResponse):
    """后台分析登记结果，幂等重放时明确标记复用。"""
    analysis_job_id: str
    job_id: str
    status: str
    source_total: int
    selected_total: int
    limit_applied: bool
    idempotent_replay: bool = False


class FailureDetail(ApiContract):
    """单名候选人分析失败的标识、错误码和可读消息。"""
    resume_id: str | None = None
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class AnalysisStatusResponse(RequestTrackedResponse):
    """后台任务状态、进度、失败详情和对应标准版本。"""
    analysis_job_id: str
    job_id: str
    criteria_version: int
    status: str
    source_total: int
    selected_total: int
    processed: int
    succeeded: int
    failed: int
    remaining: int
    progress: float
    limit_applied: bool
    failures: list[FailureDetail]
    fatal_error: dict[str, Any] | None = None


class ResultsSummary(ApiContract):
    """全量结果按最终分层统计，并单列已复核和失败数量。"""
    result_total: int
    selected_total: int
    processed: int
    succeeded: int
    failed: int
    remaining: int
    recommend: int
    pending: int
    reject: int
    reviewed: int
    source_total: int
    limit_applied: bool


class ResultItem(ApiContract):
    """结果列表中的候选人摘要，合并 AI 层和当前最终层。"""
    resume_id: str
    resume_excerpt: str
    ai_layer: Layer
    final_layer: Layer | None
    effective_layer: Layer
    review_status: Literal["reviewed", "unreviewed"]
    review_reason: str | None
    risk: bool
    match_score: float
    evidence_confidence: float


class ResultsResponse(RequestTrackedResponse):
    """带过滤、排序、分页元数据的候选人结果集合。"""
    job_id: str
    criteria_version: int
    summary: ResultsSummary
    page: int
    page_size: int
    items: list[ResultItem]
    failures: list[FailureDetail]


class CandidateView(ApiContract):
    """候选人的稳定标识与姓名展示信息。"""
    resume_text: str


class EvidenceQuoteView(ApiContract):
    """可回查到简历位置的一段原文证据。"""
    text: str
    valid: bool


class AnalysisItemView(ApiContract):
    """单条筛选标准的匹配状态、评分、解释和证据。"""
    condition: str
    status: MatchStatus
    evidence_quotes: list[EvidenceQuoteView]
    evidence_location: Literal["resume_text"]
    type: EvidenceType
    match_score: float
    evidence_confidence: float
    semantic_relation: str
    evidence_valid: bool
    actual_value: float | None = None
    required_value: float | None = None


class RecommendationView(ApiContract):
    """AI 对候选人的总体分层、分数、不确定点与风险。"""
    layer: Layer
    match_score: float
    evidence_confidence: float
    analysis: list[AnalysisItemView]
    uncertainties: list[str]
    risks: list[str]
    model_version: str
    prompt_version: str


class ReviewView(ApiContract):
    """最新 HR 决策及其理由、版本和时间。"""
    status: Literal["reviewed"]
    decision: StoredReviewDecision
    ai_layer: Layer
    final_layer: Layer
    reason: str
    reviewed_at: str
    version: int


class CandidateDetailResponse(RequestTrackedResponse):
    """详情页所需的候选人、逐项分析、AI 建议和人工复核。"""
    resume_id: str
    candidate: CandidateView
    analysis_status: Literal["completed", "analysis_failed"]
    analysis_error: dict[str, Any] | None = None
    ai_recommendation: RecommendationView | None
    hr_review: ReviewView | None
    review_version: int


class ReviewSubmittedResponse(RequestTrackedResponse):
    """人工复核写入后的最终层、版本和幂等重放标记。"""
    review_id: str
    resume_id: str
    version: int
    ai_layer: Layer
    final_layer: Layer
    review_status: Literal["reviewed"]
    reason: str
    reviewed_at: str
    idempotent_replay: bool = False


class AuditTrailResponse(RequestTrackedResponse):
    """任务或候选人的有序审计事件列表。"""
    job_id: str
    resume_id: str | None
    events: list[dict[str, Any]]


class ImportPreviewRole(ApiContract):
    """导入预览中的岗位名称、行数和可分析候选人数。"""
    name: str
    count: int
    jd_valid: bool
    jd_text: str | None = None
    issues: list[str] = Field(default_factory=list)
    preview_rows: list[dict[str, str]] = Field(default_factory=list)


class ImportPreviewResponse(RequestTrackedResponse):
    """上传 CSV 的预览标识、表头、岗位选项和提示信息。"""
    preview_id: str
    file_name: str
    headers: list[str]
    roles: list[ImportPreviewRole]
