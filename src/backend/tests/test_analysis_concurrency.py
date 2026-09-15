"""验证候选人模型调用遵循默认与自定义并发上限。

数据流：多份简历 -> TrackingLLM 记录同时执行数 -> 峰值断言；
原理：直接统计重叠调用，比依赖机器速度测总耗时更稳定。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.backend.config import Settings
from src.backend.llm import LLMResult
from src.backend.main import create_app
from src.backend.schemas import CandidateAnalysisOutput

from .conftest import FakeLLM, create_job, extract_and_confirm, run_analysis


class TrackingLLM(FakeLLM):
    """在假模型调用外统计当前并发数和历史峰值。"""
    def __init__(self) -> None:
        self.active_calls = 0
        self.peak_active_calls = 0

    async def analyze_candidate(
        self,
        job: dict[str, Any],
        criteria: list[dict[str, Any]],
        resume: dict[str, Any],
    ) -> LLMResult[CandidateAnalysisOutput]:
        self.active_calls += 1
        self.peak_active_calls = max(self.peak_active_calls, self.active_calls)
        try:
            await asyncio.sleep(0.03)
            return await super().analyze_candidate(job, criteria, resume)
        finally:
            self.active_calls -= 1


def test_default_candidate_analysis_concurrency_is_five(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("LLM_CONCURRENCY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    settings = Settings.from_env(env_file)

    assert settings.llm_concurrency == 5


def test_candidate_analysis_respects_configured_concurrency(
    settings: Settings,
) -> None:
    configured_concurrency = 5
    tracking_llm = TrackingLLM()
    concurrent_settings = replace(settings, llm_concurrency=configured_concurrency)
    app = create_app(settings=concurrent_settings, llm=tracking_llm)

    with TestClient(app) as client:
        rows = [{"resume_id": f"R{index}"} for index in range(10)]
        job_id = create_job(client, rows, candidate_limit=len(rows))
        version = extract_and_confirm(client, job_id)
        started = run_analysis(client, job_id, version)
        status_response = client.get(f"/api/v1/screening-jobs/{job_id}/analysis/status")

    assert started["selected_total"] == len(rows)
    assert status_response.status_code == 200
    status = status_response.json()
    assert status["status"] == "completed"
    assert status["processed"] == len(rows)
    assert status["processed"] == status["succeeded"] + status["failed"]
    assert status["failed"] == 0
    assert tracking_llm.peak_active_calls == configured_concurrency
    assert tracking_llm.active_calls == 0
