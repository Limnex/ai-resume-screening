"""定义进入应用层的请求结构以及模型必须返回的结构化数据。

数据流：HTTP 请求或模型 JSON -> Pydantic 字段/跨字段校验 -> 可信的类型化对象。
原理：在数据刚进入系统时拒绝多余字段和矛盾组合，减少下游防御性判断。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CriterionType = Literal["必要条件", "加分条件"]
CriterionCategory = Literal["experience", "skill", "responsibility", "domain", "certification", "other"]
MatchStatus = Literal["匹配", "部分匹配", "不满足", "信息不足", "信息冲突"]
EvidenceType = Literal["直接证据", "有限推断", "无证据"]
Layer = Literal["recommend", "pending", "reject"]
ReviewDecision = Literal["adopt", "override"]
StoredReviewDecision = Literal["adopt", "modify", "override"]


class StrictModel(BaseModel):
    """禁止未声明字段的基类，避免系统接收无法正确解释的数据。"""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CriterionInput(StrictModel):
    """表示 HR 可确认的一条筛选标准及其类别、要求和数值门槛。"""
    id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    category: CriterionCategory
    type: CriterionType
    requirement: str = Field(default="", max_length=2000)
    minimum_value: float | None = Field(default=None, ge=0)


class CriteriaUpdateRequest(StrictModel):
    """接收整组标准并验证名称唯一，避免同一条件被重复分析。"""
    criteria: list[CriterionInput] = Field(min_length=1, max_length=50)

    @field_validator("criteria")
    @classmethod
    def names_must_be_unique(cls, value: list[CriterionInput]) -> list[CriterionInput]:
        """按忽略大小写的名称查重；合格数据原样流向确认用例。"""
        names = [item.name.casefold() for item in value]
        if len(names) != len(set(names)):
            raise ValueError("筛选条件名称不能重复")
        return value


class AnalysisStartRequest(StrictModel):
    """指定分析所依据的标准版本和防止重复启动的幂等键。"""
    criteria_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=200)


class ReviewRequest(StrictModel):
    """承载 HR 采纳或改判信息，并约束人工改判必须说明原因。"""
    resume_id: str = Field(min_length=1, max_length=100)
    decision: ReviewDecision
    final_layer: Layer
    reason: str = Field(default="", max_length=2000)
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def override_reason_is_required(self) -> "ReviewRequest":
        """校验跨字段组合，防止产生没有责任说明的人工改判。"""
        if self.decision == "override" and not self.reason:
            raise ValueError("推翻 AI 建议时必须填写复核理由")
        return self


class ExtractedCriterion(StrictModel):
    """模型从 JD 提取的标准草稿，保留原文依据供 HR 核对。"""
    name: str = Field(min_length=1, max_length=200)
    category: CriterionCategory
    type: CriterionType
    source_evidence: str = Field(default="", max_length=1000)
    requirement: str = Field(default="", max_length=2000)
    minimum_value: float | None = Field(default=None, ge=0)


class CriteriaExtractionOutput(StrictModel):
    """约束模型标准提取响应必须是一组数量有限的有效标准。"""
    criteria: list[ExtractedCriterion] = Field(min_length=1, max_length=50)


class ModelAnalysisItem(StrictModel):
    """模型针对一条标准输出的判断、评分、证据和语义关系。"""
    condition: str = Field(min_length=1, max_length=200)
    status: MatchStatus
    evidence_quotes: list[str] = Field(default_factory=list, max_length=3)
    evidence_location: Literal["resume_text"]
    type: EvidenceType
    match_score: float = Field(ge=0, le=100)
    evidence_confidence: float = Field(ge=0, le=1)
    semantic_relation: str = Field(default="", max_length=3000)

    @field_validator("evidence_quotes")
    @classmethod
    def evidence_quotes_must_be_bounded(cls, value: list[str]) -> list[str]:
        """限制证据的非空性和长度，合格引句再进入真实性校验。"""
        if any(not quote or len(quote) > 1000 for quote in value):
            raise ValueError("每段证据必须为 1 到 1000 个字符")
        return value


class CandidateAnalysisOutput(StrictModel):
    """模型对单名候选人的完整输出，随后由确定性规则再次归一化。"""
    proposed_layer: Layer
    match_score: float = Field(ge=0, le=100)
    evidence_confidence: float = Field(ge=0, le=1)
    analysis: list[ModelAnalysisItem] = Field(min_length=1, max_length=50)
    uncertainties: list[str] = Field(default_factory=list, max_length=50)
    risks: list[str] = Field(default_factory=list, max_length=50)
