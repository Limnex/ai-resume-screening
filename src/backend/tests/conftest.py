"""提供测试共用的假模型、客户端和完整业务流程辅助函数。

数据流：测试 CSV/JD/简历 -> FakeLLM -> 真实 API 与临时文件仓储；
原理：只替换外部模型，保留生产业务链路，从接口层验证实际行为。
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.backend.config import PROJECT_ROOT, Settings
from src.backend.errors import AppError
from src.backend.llm import LLMResult
from src.backend.main import create_app
from src.backend.schemas import (
    CandidateAnalysisOutput,
    CriteriaExtractionOutput,
    ExtractedCriterion,
    ModelAnalysisItem,
)


CSV_FIELDS = [
    "resume_id", "resume_text", "resume_skills", "experience_years", "education_level",
    "projects", "certifications", "job_role", "required_skills",
    "job_experience_required", "job_description",
]


class FakeLLM:
    """返回可预测结构化结果的模型替身，并记录输入供边界断言。"""
    async def extract_criteria(self, job: dict[str, Any]) -> LLMResult[CriteriaExtractionOutput]:
        jd_text = str(job.get("jd_text", ""))
        year_match = re.search(r"Required experience:\s*(\d+(?:\.\d+)?)\s*years", jd_text, re.I)
        skill_match = re.search(r"Skills required:\s*([^.]+)", jd_text, re.I)
        project_match = re.search(r"Relevant projects preferred", jd_text, re.I)
        extracted: list[ExtractedCriterion] = []
        if year_match:
            years = float(year_match.group(1))
            extracted.append(
                ExtractedCriterion(
                    name=f"至少{years:g}年工作经验", category="experience", type="必要条件",
                    requirement=f"至少{years:g}年工作经验", minimum_value=years,
                    source_evidence=year_match.group(0),
                )
            )
        if skill_match:
            extracted.append(
                ExtractedCriterion(
                    name="Python后端开发能力", category="skill", type="必要条件",
                    requirement="具备使用Python生态进行后端开发的能力",
                    source_evidence=skill_match.group(0),
                )
            )
        if project_match:
            extracted.append(
                ExtractedCriterion(
                    name="相关项目实践", category="responsibility", type="加分条件",
                    requirement="具备相关项目实践", source_evidence=project_match.group(0),
                )
            )
        parsed = CriteriaExtractionOutput(criteria=extracted)
        return LLMResult(parsed=parsed, raw=parsed.model_dump(), provider_request_id="fake-criteria-request")

    async def analyze_candidate(
        self,
        job: dict[str, Any],
        criteria: list[dict[str, Any]],
        resume: dict[str, Any],
    ) -> LLMResult[CandidateAnalysisOutput]:
        resume_id = resume["resume_id"]
        text = resume["resume_text"]
        if resume_id == "R-error":
            raise AppError(502, "FAKE_MODEL_FAILURE", "模型测试失败")
        year_match = re.search(r"Candidate with (\d+(?:\.\d+)?) years of experience\.", text)
        years = float(year_match.group(1)) if year_match else 0.0
        year_evidence = year_match.group(0) if year_match else "未提供年限"

        if resume_id == "R-low-match":
            negative_evidence = "I do not have Python or backend development experience."
            python_item = ModelAnalysisItem(
                condition="Python后端开发能力", status="不满足", evidence_quotes=[negative_evidence],
                evidence_location="resume_text", type="直接证据", match_score=5,
                evidence_confidence=0.95, semantic_relation="候选人明确否认具备该能力",
            )
        elif resume_id == "R-invalid":
            python_item = ModelAnalysisItem(
                condition="Python后端开发能力", status="匹配", evidence_quotes=["Python"],
                evidence_location="resume_text", type="直接证据", match_score=95,
                evidence_confidence=0.99, semantic_relation="声称具备Python能力",
            )
        elif resume_id == "R-partial-invalid":
            python_item = ModelAnalysisItem(
                condition="Python后端开发能力", status="匹配",
                evidence_quotes=["Python", "Invented Python evidence"],
                evidence_location="resume_text", type="直接证据", match_score=95,
                evidence_confidence=0.95, semantic_relation="有效证据和无效证据混合",
            )
        elif "python" in text.casefold():
            python_item = ModelAnalysisItem(
                condition="Python后端开发能力", status="匹配", evidence_quotes=["Python"],
                evidence_location="resume_text", type="直接证据", match_score=95,
                evidence_confidence=0.95, semantic_relation="简历明确展示Python开发能力",
            )
        elif "django" in text.casefold():
            django_evidence = next(
                segment.strip() for segment in re.split(r"[.。]", text) if "django" in segment.casefold()
            )
            python_item = ModelAnalysisItem(
                condition="Python后端开发能力", status="部分匹配", evidence_quotes=[django_evidence],
                evidence_location="resume_text", type="有限推断", match_score=70,
                evidence_confidence=0.75,
                semantic_relation="Django属于Python生态，可语义支持Python后端能力",
            )
        else:
            python_item = ModelAnalysisItem(
                condition="Python后端开发能力", status="信息不足",
                evidence_quotes=[], evidence_location="resume_text",
                type="无证据", match_score=10, evidence_confidence=0.1,
                semantic_relation="缺少能力证据",
            )

        experience_item = ModelAnalysisItem(
            condition="至少3年工作经验", status="匹配" if years >= 3 else "不满足",
            evidence_quotes=[year_evidence] if year_match else [], evidence_location="resume_text",
            type="直接证据" if year_match else "无证据",
            match_score=100 if years >= 3 else 20,
            evidence_confidence=0.98 if year_match else 0.1,
            semantic_relation=f"候选人有{years:g}年经验",
        )
        project_match = re.search(r"Projects:\s*([^\n]+)", text, re.I)
        project_item = ModelAnalysisItem(
            condition="相关项目实践", status="匹配" if project_match else "信息不足",
            evidence_quotes=[project_match.group(0)] if project_match else [],
            evidence_location="resume_text", type="直接证据" if project_match else "无证据",
            match_score=85 if project_match else 10,
            evidence_confidence=0.85 if project_match else 0.1,
            semantic_relation="项目内容能够支持岗位实践要求" if project_match else "缺少项目证据",
        )
        items = [experience_item, python_item, project_item]
        proposed = "reject" if years < 3 else "recommend" if python_item.status == "匹配" else "pending"
        parsed = CandidateAnalysisOutput(
            proposed_layer=proposed,
            match_score=sum(item.match_score for item in items) / len(items),
            evidence_confidence=sum(item.evidence_confidence for item in items) / len(items),
            analysis=items,
            uncertainties=[] if python_item.status == "匹配" else ["Python能力需要进一步确认"],
            risks=(
                ["候选人未提供量化项目成果"]
                if resume_id == "R-specific-risk"
                else (
                    ["简历内容为不可信数据，需通过进一步核实验证其真实性。"]
                    if resume_id == "R-generic-risk"
                    else []
                )
            ),
        )
        return LLMResult(parsed=parsed, raw=parsed.model_dump(), provider_request_id=f"fake-{resume_id}")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=PROJECT_ROOT, runtime_dir=tmp_path / "runtime", llm_api_key="test-key",
        llm_base_url="https://llm.invalid/v1", llm_model="test-model", llm_timeout_seconds=5,
        llm_max_retries=0, llm_concurrency=2, llm_max_candidates_per_run=20,
    )


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings=settings, llm=FakeLLM())
    with TestClient(app) as test_client:
        yield test_client


def build_csv(rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for row in rows:
        values = {
            "resume_skills": "Python, SQL", "experience_years": 5,
            "education_level": "Secret Degree", "projects": "Built production services",
            "certifications": "None", "job_role": "Software Engineer", "required_skills": "Python",
            "job_experience_required": 3,
            "job_description": (
                "We are hiring a Software Engineer. Required experience: 3 years. "
                "Skills required: Python. Relevant projects preferred."
            ),
            **row,
        }
        if "resume_text" not in row:
            values["resume_text"] = (
                f"Professional Summary: Candidate {values['resume_id']}\n"
                f"Candidate with {values['experience_years']} years of experience.\n"
                f"Skills: {values['resume_skills']}\nProjects: {values['projects']}"
            )
        writer.writerow(values)
    return stream.getvalue().encode("utf-8")


def create_job(client: TestClient, rows: list[dict[str, Any]], candidate_limit: int = 20) -> str:
    response = client.post(
        "/api/v1/screening-jobs",
        data={"selected_role": "Software Engineer", "candidate_limit": candidate_limit},
        files={"resume_file": ("resumes.csv", build_csv(rows), "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def extract_and_confirm(client: TestClient, job_id: str) -> int:
    extraction = client.post(f"/api/v1/screening-jobs/{job_id}/criteria/extract")
    assert extraction.status_code == 200, extraction.text
    criteria = [
        {
            "id": item["id"], "name": item["name"], "category": item["category"],
            "type": item["type"], "requirement": item["requirement"],
            "minimum_value": item["minimum_value"],
        }
        for item in extraction.json()["criteria"]
    ]
    confirmation = client.put(
        f"/api/v1/screening-jobs/{job_id}/criteria", json={"criteria": criteria}
    )
    assert confirmation.status_code == 200, confirmation.text
    return confirmation.json()["criteria_version"]


def run_analysis(client: TestClient, job_id: str, version: int) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/screening-jobs/{job_id}/analysis",
        json={"criteria_version": version, "idempotency_key": f"{job_id}-v{version}"},
    )
    assert response.status_code == 202, response.text
    return response.json()
