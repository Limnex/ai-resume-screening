"""验证模型配置、HTTP 请求数据边界及提示词证据约束。

数据流：设置和 MockTransport -> 模型适配器 -> 捕获请求/模拟响应 -> 断言；
原理：不访问真实网络也能检验生产适配器的发送与解析行为。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from src.backend.config import PROJECT_ROOT, Settings
from src.backend.llm import ANALYSIS_SYSTEM_PROMPT, CRITERIA_SYSTEM_PROMPT, OpenAICompatibleLLM


def make_settings(tmp_path: Path) -> Settings:
    """构造使用临时目录和测试模型参数的隔离设置。"""
    return Settings(
        project_root=PROJECT_ROOT,
        runtime_dir=tmp_path / "runtime",
        llm_api_key="real-looking-test-key",
        llm_base_url="https://provider.example/v1",
        llm_model="provider-model",
        llm_timeout_seconds=5,
        llm_max_retries=0,
        llm_concurrency=1,
        llm_max_candidates_per_run=20,
    )


def test_settings_are_loaded_from_dotenv(tmp_path: Path, monkeypatch) -> None:
    """验证 dotenv 字符串被正确解析成有类型设置。"""
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=dotenv-key\n"
        "LLM_BASE_URL=https://dotenv.example/v1\n"
        "LLM_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(env_file)
    assert settings.llm_api_key == "dotenv-key"
    assert settings.llm_base_url == "https://dotenv.example/v1"
    assert settings.llm_model == "dotenv-model"
    assert settings.llm_configured is True


def test_real_llm_contract_sends_only_raw_source_fields(tmp_path: Path) -> None:
    """验证模型请求只发送生产所需的原始来源字段。"""
    seen_criteria_payload: dict | None = None
    seen_analysis_payload: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_criteria_payload, seen_analysis_payload
        assert request.url == "https://provider.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer real-looking-test-key"
        body = json.loads(request.content)
        assert body["model"] == "provider-model"
        assert body["temperature"] == 0
        user_payload = json.loads(body["messages"][1]["content"])

        if "confirmed_criteria" in user_payload:
            seen_analysis_payload = user_payload
            content = {
                "proposed_layer": "recommend",
                "match_score": 92,
                "evidence_confidence": 0.95,
                "analysis": [
                    {
                        "condition": "Python后端开发能力",
                        "status": "匹配",
                        "evidence_quotes": ["Built APIs with Python"],
                        "evidence_location": "resume_text",
                        "type": "直接证据",
                        "match_score": 92,
                        "evidence_confidence": 0.95,
                        "semantic_relation": "原始简历明确展示了Python后端开发能力",
                    }
                ],
                "uncertainties": [],
                "risks": [],
            }
        else:
            seen_criteria_payload = user_payload
            content = {
                "criteria": [
                    {
                        "name": "Python后端开发能力",
                        "category": "skill",
                        "type": "必要条件",
                        "requirement": "具备Python后端开发能力",
                        "minimum_value": None,
                        "source_evidence": "Python backend development is required",
                    }
                ]
            }
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-request-1"},
            json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]},
        )

    provider = OpenAICompatibleLLM(
        make_settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    raw_jd = "Python backend development is required"
    job = {
        "job_role": "Software Engineer",
        "jd_text": raw_jd,
        "required_skills": ["SHOULD_NOT_BE_SENT"],
        "job_experience_required": 99,
    }
    criteria_result = asyncio.run(provider.extract_criteria(job))
    assert criteria_result.parsed.criteria[0].category == "skill"
    assert seen_criteria_payload == {
        "job_role": "Software Engineer",
        "original_job_description": raw_jd,
        "extraction_feedback": [],
    }

    raw_resume = "Original resume text. Built APIs with Python."
    resume = {
        "resume_id": "R1",
        "resume_text": raw_resume,
        "resume_skills": "SHOULD_NOT_BE_SENT",
        "experience_years": 99,
        "projects": "SHOULD_NOT_BE_SENT",
        "certifications": "SHOULD_NOT_BE_SENT",
        "education_level": "SHOULD_NOT_BE_SENT",
    }
    confirmed = [
        {
            "name": "Python后端开发能力",
            "category": "skill",
            "type": "必要条件",
            "requirement": "具备Python后端开发能力",
            "minimum_value": None,
            "source_evidence": "Python backend development is required",
        }
    ]
    analysis_result = asyncio.run(provider.analyze_candidate(job, confirmed, resume))

    assert analysis_result.parsed.proposed_layer == "recommend"
    assert analysis_result.parsed.analysis[0].evidence_quotes == ["Built APIs with Python"]
    assert seen_analysis_payload == {
        "confirmed_criteria": confirmed,
        "original_job_description": raw_jd,
        "candidate": {"resume_id": "R1", "resume_text": raw_resume},
    }
    serialized = json.dumps(seen_analysis_payload, ensure_ascii=False)
    for excluded in (
        "resume_skills",
        "experience_years",
        "projects",
        "certifications",
        "education_level",
        "required_skills",
        "job_experience_required",
    ):
        assert excluded not in serialized


def test_analysis_prompt_requires_skill_semantics_and_evidence_arrays() -> None:
    """验证分析提示词要求语义关系和数组证据。"""
    assert "必须先分析本次判断涉及的所有技能 skills 的含义" in ANALYSIS_SYSTEM_PROMPT
    assert "识别同义技术、相近职责和可迁移能力" in ANALYSIS_SYSTEM_PROMPT
    assert 'evidence_quotes 必须是数组' in ANALYSIS_SYSTEM_PROMPT
    assert '"evidence_quotes"' in ANALYSIS_SYSTEM_PROMPT
    assert "固定评分规则" not in ANALYSIS_SYSTEM_PROMPT
    assert "必要条件权重为 2" not in ANALYSIS_SYSTEM_PROMPT
    assert "整体 match_score 必须按照" not in ANALYSIS_SYSTEM_PROMPT
    assert "proposed_layer 填写 reject" not in ANALYSIS_SYSTEM_PROMPT


def test_criteria_prompt_forbids_supplementing_jd_requirements() -> None:
    """验证标准提示词禁止自行补充 JD 未提出的门槛。"""
    assert "任务是“提取”，不是完善、解释、改写、扩展或补充 JD" in CRITERIA_SYSTEM_PROMPT
    assert "不得使用技术常识、岗位常识、行业惯例或岗位名称" in CRITERIA_SYSTEM_PROMPT
    assert "extraction_feedback 只是格式和遗漏修正提示" in CRITERIA_SYSTEM_PROMPT
    assert "再仅依据这段 source_evidence 生成" in CRITERIA_SYSTEM_PROMPT
    assert "requirement 不得增加 source_evidence 中没有明确表达的信息" in CRITERIA_SYSTEM_PROMPT
    assert "岗位名称不能用于补充这一限制" in CRITERIA_SYSTEM_PROMPT
    assert "要求具备Node.js" in CRITERIA_SYSTEM_PROMPT
    assert "禁止扩写为：熟练掌握React并用于前端开发" in CRITERIA_SYSTEM_PROMPT
    assert "技能列表只写“要求具备某技能”" in CRITERIA_SYSTEM_PROMPT
    assert "是否使用技术常识扩写了技能名称" in CRITERIA_SYSTEM_PROMPT
