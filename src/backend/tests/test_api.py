"""通过真实 HTTP 测试客户端验证主流程、规则边界和审计行为。

数据流：测试 CSV 与请求 -> FastAPI -> 应用用例 -> 临时仓储 -> JSON 断言；
原理：以用户可观察结果为准，并检查基准字段不会污染生产判断。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from .conftest import build_csv, create_job, extract_and_confirm, run_analysis
from src.backend.application.results import ResultReviewUseCases
from src.backend.domain import find_resume_experience


def test_selected_role_is_required_and_request_id_is_echoed(client: TestClient) -> None:
    """验证未选岗位会失败，且错误响应保留请求追踪标识。"""
    response = client.post(
        "/api/v1/screening-jobs",
        data={"selected_role": ""},
        files={"resume_file": ("resumes.csv", build_csv([{"resume_id": "R1"}]), "text/csv")},
        headers={"X-Request-ID": "ui-request-123"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SELECTED_ROLE_REQUIRED"
    assert response.json()["request_id"] == "ui-request-123"
    assert response.headers["X-Request-ID"] == "ui-request-123"


def test_import_preview_moves_csv_inspection_to_backend(client: TestClient) -> None:
    """验证 CSV 在后端解析并返回可选岗位预览。"""
    response = client.post(
        "/api/v1/import-previews",
        files={"resume_file": ("resumes.csv", build_csv([{"resume_id": "R-preview"}]), "text/csv")},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["preview_id"].startswith("preview-")
    role = next(item for item in payload["roles"] if item["name"] == "Software Engineer")
    assert role["jd_valid"] is True
    created = client.post(
        "/api/v1/screening-jobs",
        data={
            "preview_id": payload["preview_id"],
            "selected_role": "Software Engineer",
            "candidate_limit": "1",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["import_summary"]["valid"] == 1


def test_raw_sources_are_production_data_and_extracted_columns_are_reference_only(
    client: TestClient,
) -> None:
    raw_resume = "Candidate with 5 years of experience. Built APIs with Django."
    job_id = create_job(
        client,
        [
            {
                "resume_id": "R-raw",
                "resume_text": raw_resume,
                "resume_skills": "COBOL",
                "experience_years": 1,
                "projects": "Benchmark project label",
                "certifications": "Benchmark certificate label",
                "education_level": "Benchmark education label",
                "required_skills": "Benchmark required skills",
                "job_experience_required": 99,
            }
        ],
        candidate_limit=1,
    )

    store = client.app.state.store
    production_resume = store.read_resumes(job_id)[0]
    assert production_resume["resume_text"] == raw_resume
    assert set(production_resume) == {
        "resume_id",
        "resume_text",
        "job_role",
        "source_row",
        "text_hash",
        "import_order",
    }
    reference = store.read_benchmark_references(job_id)[0]
    assert reference["reference_only"] is True
    assert reference["resume_skills"] == "COBOL"
    assert reference["experience_years"] == 1
    assert reference["job_experience_required"] == 99

    detail = client.get(f"/api/v1/screening-jobs/{job_id}").json()
    assert detail["job"]["jd_text"].startswith("We are hiring a Software Engineer")
    assert detail["job"]["candidate_limit"] == 1


def test_original_jd_is_required_and_must_be_consistent(client: TestClient) -> None:
    """验证原始 JD 必填，且同岗位的 JD 内容不能互相冲突。"""
    missing = client.post(
        "/api/v1/screening-jobs",
        data={"selected_role": "Software Engineer", "candidate_limit": 2},
        files={
            "resume_file": (
                "resumes.csv",
                build_csv([{"resume_id": "R1", "job_description": ""}]),
                "text/csv",
            )
        },
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "JD_REQUIRED"

    inconsistent = client.post(
        "/api/v1/screening-jobs",
        data={"selected_role": "Software Engineer", "candidate_limit": 2},
        files={
            "resume_file": (
                "resumes.csv",
                build_csv(
                    [
                        {"resume_id": "R1", "job_description": "Required experience: 3 years."},
                        {"resume_id": "R2", "job_description": "Required experience: 4 years."},
                    ]
                ),
                "text/csv",
            )
        },
    )
    assert inconsistent.status_code == 422
    assert inconsistent.json()["error"]["code"] == "INCONSISTENT_JOB_DESCRIPTION"


def test_full_flow_review_idempotency_and_no_benchmark_fields(
    client: TestClient, settings
) -> None:
    job_id = create_job(
        client,
        [
            {"resume_id": "R1", "resume_skills": "Python, SQL", "experience_years": 5},
            {
                "resume_id": "R2",
                "resume_skills": "SQL",
                "experience_years": 6,
                "projects": "Built machine learning pipelines",
            },
            {"resume_id": "R3", "resume_skills": "Python", "experience_years": 1},
        ],
    )
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)

    status = client.get(f"/api/v1/screening-jobs/{job_id}/analysis/status")
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert status.json()["processed"] == 3
    assert status.json()["processed"] == status.json()["succeeded"] + status.json()["failed"]

    results = client.get(f"/api/v1/screening-jobs/{job_id}/results?page_size=100")
    assert results.status_code == 200
    assert results.json()["summary"]["recommend"] == 1
    assert results.json()["summary"]["pending"] == 1
    assert results.json()["summary"]["reject"] == 1
    for benchmark_field in (
        "education_level",
        "resume_skills",
        "experience_years",
        "projects",
        "certifications",
    ):
        assert benchmark_field not in results.text

    detail = client.get(f"/api/v1/screening-jobs/{job_id}/candidates/R2")
    assert detail.status_code == 200
    assert detail.json()["ai_recommendation"]["layer"] == "pending"
    assert detail.json()["resume_id"] == "R2"
    assert set(detail.json()["candidate"]) == {"resume_text"}

    review_body = {
        "resume_id": "R2",
        "decision": "override",
        "final_layer": "recommend",
        "reason": "已人工核实项目使用 Python，因此推翻原建议并改为推荐。",
        "expected_version": 0,
        "idempotency_key": "review-r2-v1",
    }
    first_review = client.post(f"/api/v1/screening-jobs/{job_id}/reviews", json=review_body)
    replay = client.post(f"/api/v1/screening-jobs/{job_id}/reviews", json=review_body)
    assert first_review.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["review_id"] == first_review.json()["review_id"]
    assert replay.json()["idempotent_replay"] is True

    recommend_results = client.get(
        f"/api/v1/screening-jobs/{job_id}/results?layer=recommend&page_size=100"
    )
    assert {item["resume_id"] for item in recommend_results.json()["items"]} == {"R1", "R2"}

    audit = client.get(f"/api/v1/screening-jobs/{job_id}/audit-trail?resume_id=R2")
    review_events = [
        event for event in audit.json()["events"] if event["event_type"] == "hr_review_submitted"
    ]
    assert len(review_events) == 1
    assert "reviewer_id" not in first_review.text
    assert not list(Path(settings.runtime_dir).rglob("*.db"))
    assert not list(Path(settings.runtime_dir).rglob("*.sqlite"))


def test_adopt_review_reason_is_optional(client: TestClient) -> None:
    """验证 HR 采纳 AI 建议时可以不填写额外理由。"""
    job_id = create_job(client, [{"resume_id": "R1"}])
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    response = client.post(
        f"/api/v1/screening-jobs/{job_id}/reviews",
        json={
            "resume_id": "R1",
            "decision": "adopt",
            "final_layer": "recommend",
            "expected_version": 0,
            "idempotency_key": "adopt-without-reason",
        },
    )
    assert response.status_code == 201
    assert response.json()["reason"] == ""


def test_override_review_reason_is_required(client: TestClient) -> None:
    """验证 HR 推翻 AI 建议时必须留下可审计理由。"""
    job_id = create_job(client, [{"resume_id": "R1"}])
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    response = client.post(
        f"/api/v1/screening-jobs/{job_id}/reviews",
        json={
            "resume_id": "R1",
            "decision": "override",
            "final_layer": "pending",
            "reason": "   ",
            "expected_version": 0,
            "idempotency_key": "override-without-reason",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_override_review_must_change_ai_layer(client: TestClient) -> None:
    """验证改判操作必须真正改变 AI 原分层。"""
    job_id = create_job(client, [{"resume_id": "R1"}])
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    response = client.post(
        f"/api/v1/screening-jobs/{job_id}/reviews",
        json={
            "resume_id": "R1",
            "decision": "override",
            "final_layer": "recommend",
            "reason": "希望推翻建议",
            "expected_version": 0,
            "idempotency_key": "override-without-layer-change",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "OVERRIDE_LAYER_UNCHANGED"


def test_removed_modify_review_decision_is_rejected(client: TestClient) -> None:
    """验证已移除的旧决策值不会被新接口接受。"""
    job_id = create_job(client, [{"resume_id": "R1"}])
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    response = client.post(
        f"/api/v1/screening-jobs/{job_id}/reviews",
        json={
            "resume_id": "R1",
            "decision": "modify",
            "final_layer": "pending",
            "reason": "旧操作不应再被接受",
            "expected_version": 0,
            "idempotency_key": "removed-modify-decision",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_model_evidence_is_visible_and_downgraded_to_pending(client: TestClient) -> None:
    """验证虚构证据会被标出并把结论降为待人工判断。"""
    job_id = create_job(
        client,
        [
            {
                "resume_id": "R-invalid",
                "resume_skills": "SQL",
                "experience_years": 5,
                "projects": "Built reporting dashboards",
            }
        ],
    )
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    detail = client.get(f"/api/v1/screening-jobs/{job_id}/candidates/R-invalid")
    assert detail.status_code == 200
    recommendation = detail.json()["ai_recommendation"]
    assert recommendation["layer"] == "pending"
    python_item = next(
        item for item in recommendation["analysis"] if item["condition"] == "Python后端开发能力"
    )
    assert python_item["evidence_valid"] is False
    assert python_item["evidence_quotes"] == [{"text": "Python", "valid": False}]
    assert python_item["status"] == "信息不足"
    assert recommendation["uncertainties"]
    assert recommendation["risks"]


def test_valid_evidence_quote_survives_when_another_quote_is_invalid(
    client: TestClient,
) -> None:
    raw_resume = (
        "Candidate with 5 years of experience.\n"
        "Skills: Python\n"
        "Projects: Built Python APIs."
    )
    job_id = create_job(
        client,
        [{"resume_id": "R-partial-invalid", "resume_text": raw_resume}],
    )
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)

    detail = client.get(
        f"/api/v1/screening-jobs/{job_id}/candidates/R-partial-invalid"
    ).json()
    recommendation = detail["ai_recommendation"]
    python_item = next(
        item
        for item in recommendation["analysis"]
        if item["condition"] == "Python后端开发能力"
    )
    assert python_item["status"] == "匹配"
    assert python_item["evidence_valid"] is True
    assert python_item["evidence_quotes"] == [
        {"text": "Python", "valid": True},
        {"text": "Invented Python evidence", "valid": False},
    ]
    assert "部分证据无法在" in recommendation["uncertainties"][0]
    assert "保留可验证证据" in recommendation["risks"][0]


def test_legacy_string_evidence_is_adapted_without_rewriting_history() -> None:
    """验证旧证据格式可在读取时适配而不改写历史数据。"""
    legacy = {
        "condition": "Python后端开发能力",
        "status": "匹配",
        "evidence": "Python",
        "evidence_location": "resume_text",
        "type": "直接证据",
        "match_score": 95,
        "evidence_confidence": 0.95,
        "semantic_relation": "简历明确展示Python开发能力",
        "evidence_valid": True,
    }
    adapted = ResultReviewUseCases._analysis_items_for_view([legacy])[0]
    assert "evidence" not in adapted
    assert adapted["evidence_quotes"] == [{"text": "Python", "valid": True}]


def test_semantic_relation_is_used_without_literal_keyword_match(client: TestClient) -> None:
    """验证语义相关能力不要求简历逐字重复岗位关键词。"""
    raw_resume = (
        "Candidate with 5 years of experience.\n"
        "Built production APIs with Django and PostgreSQL.\n"
        "Projects: Delivered a high-traffic web service."
    )
    assert "python" not in raw_resume.casefold()
    job_id = create_job(client, [{"resume_id": "R-django", "resume_text": raw_resume}])
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)

    detail = client.get(f"/api/v1/screening-jobs/{job_id}/candidates/R-django").json()
    python_item = next(
        item
        for item in detail["ai_recommendation"]["analysis"]
        if item["condition"] == "Python后端开发能力"
    )
    assert python_item["status"] == "部分匹配"
    assert python_item["type"] == "有限推断"
    assert python_item["match_score"] == 70
    assert "Django属于Python生态" in python_item["semantic_relation"]
    assert detail["ai_recommendation"]["layer"] == "recommend"


def test_experience_threshold_uses_raw_jd_and_raw_resume_not_benchmark(client: TestClient) -> None:
    """验证年限门槛只从原始 JD 与简历计算。"""
    raw_jd = (
        "We are hiring a Data Scientist. Required experience: 4 years. "
        "Skills required: Python. Relevant projects preferred."
    )
    raw_resume = (
        "Candidate with 5 years of experience.\n"
        "Skills: Python\nProjects: Built forecasting models."
    )
    job_id = create_job(
        client,
        [
            {
                "resume_id": "R-five-years",
                "resume_text": raw_resume,
                "job_description": raw_jd,
                "experience_years": 1,
                "job_experience_required": 99,
            }
        ],
    )
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    detail = client.get(f"/api/v1/screening-jobs/{job_id}/candidates/R-five-years").json()
    experience_item = next(
        item
        for item in detail["ai_recommendation"]["analysis"]
        if item["condition"] == "至少4年工作经验"
    )
    assert experience_item["status"] == "匹配"
    assert experience_item["actual_value"] == 5
    assert experience_item["required_value"] == 4
    assert experience_item["evidence_valid"] is True


def test_experience_parser_accepts_domain_specific_raw_phrasing() -> None:
    """验证年限解析器能识别业务语境中的自然语言。"""
    chinese = find_resume_experience("拥有5年后端开发经验，负责过核心服务。")
    english = find_resume_experience("Over 6 years of data science experience in production teams.")
    assert chinese is not None and chinese[0] == 5
    assert english is not None and english[0] == 6


def test_experience_below_threshold_uses_proportional_percentage(
    client: TestClient,
) -> None:
    raw_jd = (
        "We are hiring a Data Scientist. Required experience: 4 years. "
        "Skills required: Python. Relevant projects preferred."
    )
    raw_resume = (
        "Candidate with 3 years of experience.\n"
        "Skills: Python\nProjects: Built forecasting models."
    )
    job_id = create_job(
        client,
        [
            {
                "resume_id": "R-three-years",
                "resume_text": raw_resume,
                "job_description": raw_jd,
            }
        ],
    )
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    detail = client.get(
        f"/api/v1/screening-jobs/{job_id}/candidates/R-three-years"
    ).json()
    experience_item = next(
        item
        for item in detail["ai_recommendation"]["analysis"]
        if item["condition"] == "至少4年工作经验"
    )
    assert experience_item["status"] == "不满足"
    assert experience_item["match_score"] == 75
    assert experience_item["evidence_confidence"] == 0.98
    assert detail["ai_recommendation"]["layer"] == "reject"


def test_verified_low_match_is_rejected_instead_of_dumped_into_pending(client: TestClient) -> None:
    """验证证据充分的低匹配候选人进入拒绝层而非一律待定。"""
    raw_resume = (
        "Candidate with 5 years of experience.\n"
        "I do not have Python or backend development experience.\n"
        "Projects: Managed company events."
    )
    job_id = create_job(client, [{"resume_id": "R-low-match", "resume_text": raw_resume}])
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    detail = client.get(f"/api/v1/screening-jobs/{job_id}/candidates/R-low-match").json()
    recommendation = detail["ai_recommendation"]
    assert recommendation["layer"] == "reject"
    python_item = next(
        item for item in recommendation["analysis"] if item["condition"] == "Python后端开发能力"
    )
    assert python_item["status"] == "不满足"
    assert python_item["evidence_valid"] is True
    assert python_item["evidence_confidence"] >= 0.6


def test_universal_resume_disclaimer_does_not_force_a_good_candidate_to_pending(
    client: TestClient,
) -> None:
    job_id = create_job(client, [{"resume_id": "R-generic-risk"}])
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    detail = client.get(f"/api/v1/screening-jobs/{job_id}/candidates/R-generic-risk").json()
    recommendation = detail["ai_recommendation"]
    assert recommendation["layer"] == "recommend"
    assert recommendation["risks"] == []


def test_candidate_specific_risk_is_visible_but_not_a_recommendation_gate(
    client: TestClient,
) -> None:
    job_id = create_job(client, [{"resume_id": "R-specific-risk"}])
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    detail = client.get(
        f"/api/v1/screening-jobs/{job_id}/candidates/R-specific-risk"
    ).json()
    recommendation = detail["ai_recommendation"]
    assert recommendation["match_score"] >= 75
    assert recommendation["evidence_confidence"] >= 0.7
    assert recommendation["layer"] == "recommend"
    assert recommendation["risks"] == ["候选人未提供量化项目成果"]


def test_partial_failures_are_reported_and_counts_always_balance(client: TestClient) -> None:
    """验证批量任务允许部分失败且计数始终守恒。"""
    job_id = create_job(client, [{"resume_id": "R1"}, {"resume_id": "R-error"}])
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)

    status = client.get(f"/api/v1/screening-jobs/{job_id}/analysis/status").json()
    assert status["status"] == "completed_with_errors"
    assert status["selected_total"] == 2
    assert status["processed"] == 2
    assert status["succeeded"] == 1
    assert status["failed"] == 1
    assert status["remaining"] == 0
    assert status["processed"] == status["succeeded"] + status["failed"]
    assert status["failures"] == [
        {
            "resume_id": "R-error",
            "code": "FAKE_MODEL_FAILURE",
            "message": "模型测试失败",
            "details": {},
        }
    ]

    results = client.get(f"/api/v1/screening-jobs/{job_id}/results?page_size=100").json()
    assert results["summary"]["result_total"] == results["summary"]["succeeded"] == 1
    assert results["summary"]["failed"] == len(results["failures"]) == 1
    assert len(results["items"]) == 1


def test_ui_selected_candidate_limit_controls_this_run(client: TestClient) -> None:
    """验证用户选择的候选人数控制本轮分析范围。"""
    rows = [{"resume_id": f"R{index}"} for index in range(25)]
    job_id = create_job(client, rows, candidate_limit=7)
    version = extract_and_confirm(client, job_id)
    started = run_analysis(client, job_id, version)
    assert started["source_total"] == 25
    assert started["selected_total"] == 7
    assert started["limit_applied"] is True

    results = client.get(f"/api/v1/screening-jobs/{job_id}/results?page_size=100")
    assert results.status_code == 200
    assert results.json()["summary"]["selected_total"] == 7
    assert results.json()["summary"]["result_total"] == 7
    assert results.json()["summary"]["source_total"] == 25
    assert results.json()["summary"]["limit_applied"] is True

    outside_limit = client.get(f"/api/v1/screening-jobs/{job_id}/candidates/R24")
    assert outside_limit.status_code == 404
    assert outside_limit.json()["error"]["code"] == "CANDIDATE_NOT_ANALYZED"


def test_changing_criteria_marks_previous_analysis_stale(client: TestClient) -> None:
    """验证标准变化后旧分析过期，不能继续作为当前结果。"""
    job_id = create_job(client, [{"resume_id": "R1"}])
    version = extract_and_confirm(client, job_id)
    run_analysis(client, job_id, version)
    current = client.get(f"/api/v1/screening-jobs/{job_id}/criteria").json()
    changed = [
        {
            "id": item["id"],
            "name": item["name"],
            "category": item["category"],
            "type": item["type"],
            "requirement": item["requirement"],
            "minimum_value": item["minimum_value"],
        }
        for item in current["criteria"]
    ] + [
        {
            "id": 99,
            "name": "不存在的技能",
            "category": "skill",
            "type": "必要条件",
            "requirement": "候选人必须具备不存在的技能",
            "minimum_value": None,
        }
    ]
    confirmation = client.put(
        f"/api/v1/screening-jobs/{job_id}/criteria",
        json={"criteria": changed},
    )
    assert confirmation.status_code == 200
    assert confirmation.json()["criteria_version"] == version + 1

    old_results = client.get(f"/api/v1/screening-jobs/{job_id}/results")
    assert old_results.status_code == 409
    assert old_results.json()["error"]["code"] == "ANALYSIS_STALE"
