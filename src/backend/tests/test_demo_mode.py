"""验证作品集离线演示模式能跑通真实业务链路。"""

from pathlib import Path

from fastapi.testclient import TestClient

from src.backend.config import PROJECT_ROOT, Settings
from src.backend.main import create_app


def test_demo_mode_runs_complete_screening_flow(tmp_path: Path) -> None:
    settings = Settings(
        project_root=PROJECT_ROOT,
        runtime_dir=tmp_path / "runtime",
        llm_api_key="",
        llm_base_url="",
        llm_model="offline-demo",
        llm_timeout_seconds=5,
        llm_max_retries=0,
        llm_concurrency=3,
        llm_max_candidates_per_run=20,
        demo_mode=True,
    )
    sample_path = PROJECT_ROOT / "data" / "sample" / "resumes_demo.csv"

    with TestClient(create_app(settings=settings)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["demo_mode"] is True
        assert health.json()["llm_configured"] is True

        with sample_path.open("rb") as sample_file:
            created = client.post(
                "/api/v1/screening-jobs",
                data={"selected_role": "AI 应用工程师", "candidate_limit": 3},
                files={"resume_file": (sample_path.name, sample_file, "text/csv")},
            )
        assert created.status_code == 201, created.text
        job_id = created.json()["job_id"]

        extracted = client.post(f"/api/v1/screening-jobs/{job_id}/criteria/extract")
        assert extracted.status_code == 200, extracted.text
        criteria = extracted.json()["criteria"]
        assert any(item["category"] == "experience" for item in criteria)
        assert any("Python" in item["name"] for item in criteria)

        confirmed = client.put(
            f"/api/v1/screening-jobs/{job_id}/criteria",
            json={
                "criteria": [
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "category": item["category"],
                        "type": item["type"],
                        "requirement": item["requirement"],
                        "minimum_value": item["minimum_value"],
                    }
                    for item in criteria
                ]
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        version = confirmed.json()["criteria_version"]

        started = client.post(
            f"/api/v1/screening-jobs/{job_id}/analysis",
            json={"criteria_version": version, "idempotency_key": "offline-demo-run"},
        )
        assert started.status_code == 202, started.text

        results = client.get(f"/api/v1/screening-jobs/{job_id}/results")
        assert results.status_code == 200, results.text
        assert results.json()["summary"]["result_total"] == 3
        candidate = next(item for item in results.json()["items"] if item["resume_id"] == "R001")
        assert candidate["ai_layer"] == "recommend"

        audit = client.get(
            f"/api/v1/screening-jobs/{job_id}/audit-trail",
            params={"resume_id": "R001"},
        )
        assert audit.status_code == 200, audit.text
        event_types = {event["event_type"] for event in audit.json()["events"]}
        assert {"input_imported", "ai_analysis_completed"} <= event_types


def test_demo_mode_does_not_mask_missing_real_model_configuration(tmp_path: Path) -> None:
    settings = Settings(
        project_root=PROJECT_ROOT,
        runtime_dir=tmp_path / "runtime",
        llm_api_key="",
        llm_base_url="",
        llm_model="",
        llm_timeout_seconds=5,
        llm_max_retries=0,
        llm_concurrency=1,
        llm_max_candidates_per_run=20,
        demo_mode=False,
    )
    assert settings.llm_configured is False
