"""为作品集提供不联网、可重复的受控演示模型。

该适配器只用于演示系统工作流，不模拟真实模型能力，也不能用于招聘判断。
它按固定规则从 JD 提取条件，再从简历原文中寻找可定位证据，让没有 API Key
的查看者也能体验“条件确认—证据分析—人工复核—审计留痕”的完整链路。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from .config import Settings
from .ports import LLMResult
from .schemas import (
    CandidateAnalysisOutput,
    CriteriaExtractionOutput,
    ExtractedCriterion,
    ModelAnalysisItem,
)


_CLAUSE_SPLIT = re.compile(r"(?<=[。！？.!?；;])\s*|[\r\n]+")
_REQUIRED_WORDS = re.compile(r"要求|必须|应当|需要|熟悉|掌握|具备|具有", re.I)
_BONUS_WORDS = re.compile(r"优先|加分|preferred|nice\s+to\s+have", re.I)
_YEAR_PATTERN = re.compile(r"(?:至少|不少于|具备|具有)?\s*(\d+(?:\.\d+)?)\s*年(?:以上|及以上)?")

# 每一组都把岗位语言与常见的简历表达连接起来。这里只用于离线演示，不是生产语义模型。
_SKILL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Python 与 FastAPI 开发", ("python", "fastapi", "django", "flask")),
    ("大模型 API 应用", ("大模型", "llm", "openai", "ai 应用", "ai应用", "prompt")),
    ("SQL 与数据处理", ("sql", "pandas", "数据分析", "数据处理")),
    ("REST API 开发", ("rest api", "restful", "api 开发", "接口开发")),
)


def _clauses(text: str) -> list[str]:
    """保留原文片段，便于后端继续验证引用确实存在。"""

    return [item.strip() for item in _CLAUSE_SPLIT.split(text) if item.strip()]


def _criterion_type(clause: str) -> str:
    if _BONUS_WORDS.search(clause):
        return "加分条件"
    return "必要条件" if _REQUIRED_WORDS.search(clause) else "加分条件"


def _sentence_with(text: str, aliases: tuple[str, ...]) -> tuple[str | None, bool]:
    """返回包含关键词的原文句子，并标记是否属于同义技术栈的有限推断。"""

    for sentence in _clauses(text):
        lowered = sentence.casefold()
        for index, alias in enumerate(aliases):
            if alias.casefold() in lowered:
                return sentence, index > 0
    return None, False


class DemoLLM:
    """实现与真实模型相同的端口，但所有输出均由本地确定性规则生成。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def extract_criteria(self, job: dict[str, Any]) -> LLMResult[CriteriaExtractionOutput]:
        await asyncio.sleep(0)
        jd_text = str(job.get("jd_text", ""))
        clauses = _clauses(jd_text)
        criteria: list[ExtractedCriterion] = []

        for clause in clauses:
            year_match = _YEAR_PATTERN.search(clause)
            if year_match:
                years = float(year_match.group(1))
                criteria.append(
                    ExtractedCriterion(
                        name=f"至少 {years:g} 年相关经验",
                        category="experience",
                        type="必要条件",
                        requirement=f"至少 {years:g} 年相关工作经验",
                        minimum_value=years,
                        source_evidence=clause,
                    )
                )
                break

        seen_groups: set[str] = set()
        for clause in clauses:
            lowered = clause.casefold()
            for name, aliases in _SKILL_GROUPS:
                if name in seen_groups or not any(alias.casefold() in lowered for alias in aliases):
                    continue
                seen_groups.add(name)
                criteria.append(
                    ExtractedCriterion(
                        name=name,
                        category="skill",
                        type=_criterion_type(clause),
                        requirement=clause,
                        source_evidence=clause,
                    )
                )

        if not criteria:
            evidence = clauses[0] if clauses else jd_text.strip()
            criteria.append(
                ExtractedCriterion(
                    name="岗位核心职责",
                    category="responsibility",
                    type="加分条件",
                    requirement=evidence,
                    source_evidence=evidence,
                )
            )

        parsed = CriteriaExtractionOutput(criteria=criteria[:8])
        return LLMResult(
            parsed=parsed,
            raw={"demo_mode": True, **parsed.model_dump()},
            provider_request_id=f"demo-criteria-{uuid.uuid4().hex[:8]}",
        )

    async def analyze_candidate(
        self,
        job: dict[str, Any],
        criteria: list[dict[str, Any]],
        resume: dict[str, Any],
    ) -> LLMResult[CandidateAnalysisOutput]:
        await asyncio.sleep(0)
        resume_text = str(resume.get("resume_text", ""))
        items: list[ModelAnalysisItem] = []

        for criterion in criteria:
            haystack = f"{criterion.get('name', '')} {criterion.get('requirement', '')}".casefold()
            aliases: tuple[str, ...] = ()
            for _, group_aliases in _SKILL_GROUPS:
                if any(alias.casefold() in haystack for alias in group_aliases):
                    aliases = group_aliases
                    break

            if criterion.get("category") == "experience":
                year_match = _YEAR_PATTERN.search(resume_text)
                evidence = year_match.group(0) if year_match else None
                candidate_years = float(year_match.group(1)) if year_match else None
                required_years = criterion.get("minimum_value")
                meets_requirement = (
                    candidate_years is not None
                    and (required_years is None or candidate_years >= float(required_years))
                )
                status = (
                    "信息不足"
                    if candidate_years is None
                    else "匹配"
                    if meets_requirement
                    else "不满足"
                )
                items.append(
                    ModelAnalysisItem(
                        condition=criterion["name"],
                        status=status,
                        evidence_quotes=[evidence] if evidence else [],
                        evidence_location="resume_text",
                        type="直接证据" if evidence else "无证据",
                        match_score=90 if meets_requirement else 25 if evidence else 20,
                        evidence_confidence=0.92 if evidence else 0.2,
                        semantic_relation=(
                            "简历经验年限达到岗位要求"
                            if meets_requirement
                            else "简历经验年限低于岗位要求"
                            if evidence
                            else "简历未明确给出可比较的经验年限"
                        ),
                    )
                )
                continue

            evidence, inferred = _sentence_with(resume_text, aliases) if aliases else (None, False)
            if evidence:
                items.append(
                    ModelAnalysisItem(
                        condition=criterion["name"],
                        status="部分匹配" if inferred else "匹配",
                        evidence_quotes=[evidence],
                        evidence_location="resume_text",
                        type="有限推断" if inferred else "直接证据",
                        match_score=72 if inferred else 90,
                        evidence_confidence=0.74 if inferred else 0.92,
                        semantic_relation=(
                            "简历包含同一技术生态中的相关实践，需要 HR 进一步确认"
                            if inferred
                            else "简历原文直接包含对应能力或实践"
                        ),
                    )
                )
            else:
                items.append(
                    ModelAnalysisItem(
                        condition=criterion["name"],
                        status="信息不足",
                        evidence_quotes=[],
                        evidence_location="resume_text",
                        type="无证据",
                        match_score=20,
                        evidence_confidence=0.2,
                        semantic_relation="演示规则未找到可定位的相关原文",
                    )
                )

        match_score = sum(item.match_score for item in items) / len(items)
        evidence_confidence = sum(item.evidence_confidence for item in items) / len(items)
        proposed_layer = "recommend" if match_score >= 75 and evidence_confidence >= 0.7 else "pending"
        parsed = CandidateAnalysisOutput(
            proposed_layer=proposed_layer,
            match_score=match_score,
            evidence_confidence=evidence_confidence,
            analysis=items,
            uncertainties=["当前结果由离线演示规则生成，不代表真实招聘结论"],
            risks=[],
        )
        return LLMResult(
            parsed=parsed,
            raw={"demo_mode": True, **parsed.model_dump()},
            provider_request_id=f"demo-analysis-{uuid.uuid4().hex[:8]}",
        )
