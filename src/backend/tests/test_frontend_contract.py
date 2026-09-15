"""验证前端只通过规定后端 API 和拆分资源完成工作流。

数据流：HTML/JavaScript 源码 -> 接口路径与绑定模式搜索 -> 契约断言；
原理：无需启动浏览器即可发现夹具数据、内联逻辑或字段漂移。
"""

from pathlib import Path
import re


FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def read_page(name: str) -> str:
    """读取指定前端页面，供多个契约测试复用。"""
    return (FRONTEND / name).read_text(encoding="utf-8")


def read_asset(path: str) -> str:
    """读取前端静态资源，统一测试文件定位方式。"""
    return (FRONTEND / path).read_text(encoding="utf-8")


def test_normal_pages_use_backend_api_instead_of_fixture_data() -> None:
    """验证正常页面读取后端 API 而不是演示夹具。"""
    for page_name in ("index.html", "criteria.html", "results.html", "detail.html"):
        page = read_page(page_name)
        assert '<script src="api.js"></script>' in page
        assert '<script src="data.js"></script>' not in page
        assert "<script>" not in page
        assert "MOCK_" not in page


def test_create_page_requires_one_specific_role_and_calls_backend() -> None:
    """验证创建页要求单一岗位并调用预览、建任务接口。"""
    page = read_page("index.html") + read_asset("pages/index-page.js")
    assert "全部岗位" not in page
    assert 'id="candidateLimit"' in page
    assert 'min="1"' in page
    assert 'max="20"' in page
    assert "本轮分析数量必须是 1 到 20 的整数" in page
    assert "previewImport" in page
    assert "createJobFromPreview" in page
    assert "ScreeningApi.extractCriteria" in page
    assert "splitCSVRecords" not in page
    assert "parseCSVLine" not in page


def test_remaining_pages_cover_the_real_workflow() -> None:
    """验证标准、结果和详情页覆盖真实后端流程。"""
    criteria = read_page("criteria.html") + read_asset("pages/criteria-page.js")
    results = read_page("results.html") + read_asset("pages/results-page.js")
    detail = read_page("detail.html") + read_asset("pages/detail-page.js")

    assert "ScreeningApi.confirmCriteria" in criteria
    assert "ScreeningApi.startAnalysis" in criteria
    assert "ScreeningApi.getAnalysisStatus" in criteria
    assert 'id="criteriaBody"' in criteria
    assert "<table" in read_page("criteria.html")
    assert "数字下限" not in criteria
    assert "criterion-minimum" not in criteria
    assert 'colspan="7"' in criteria
    assert 'class="criteria-table"' in criteria
    assert 'class="criterion-name criteria-table-control"' in criteria
    assert 'pages/criteria-page.js?v=criteria-layout-2' in read_page("criteria.html")
    assert "status.selected_total" in criteria
    assert "status.succeeded" in criteria
    assert "status.failed" in criteria
    assert "formatFailure" in criteria
    assert "ScreeningApi.getResults" in results
    assert "summary.result_total" in results
    assert "summary.processed" in results
    assert "summary.succeeded" in results
    assert "summary.failed" in results
    assert "item.match_score" in results
    assert "item.evidence_confidence" in results
    assert "formatFailure" in results
    assert "ScreeningApi.getCandidate" in detail
    assert "ScreeningApi.submitReview" in detail
    assert "reviewer_id" not in detail
    assert "recommendation.match_score" in detail
    assert "recommendation.evidence_confidence" in detail
    assert "item.evidence_quotes" in detail
    assert "item.evidence === 'string'" in detail
    assert "quote.valid" in detail
    assert 'value="modify"' not in detail
    assert 'value="override"' in detail
    assert "复核理由（选填）" in detail
    assert "复核理由（必填）" in detail
    assert "decision === 'override' && !reason" in detail
    assert 'pages/detail-page.js?v=review-ui-2' in read_page("detail.html")
    assert "currentCandidate" not in detail
    assert "localStorage.getItem('reviews')" not in detail


def test_api_contract_sends_request_id_and_no_redundant_identity_fields() -> None:
    """验证请求携带追踪标识且不重复提交身份字段。"""
    api = read_page("api.js")
    assert "X-Request-ID" in api
    assert "candidate_limit" in api
    assert "confirmed_by" not in api
    assert "created_by" not in api
    assert "reviewer_id" not in api


def test_frontend_responsibilities_are_split_into_assets() -> None:
    """验证页面逻辑拆分到公共静态资源。"""
    api = read_asset("api.js")
    state = read_asset("state.js")
    feedback = read_asset("feedback.js")
    assert "fetch(" in api
    assert "localStorage" not in api
    assert "showToast" not in api
    assert "localStorage" in state
    assert "setButtonLoading" in feedback


def test_pages_do_not_embed_inline_event_handlers() -> None:
    """验证页面不使用内联事件处理。"""
    inline_event = re.compile(r"\bon(?:click|change|input|load|submit|drop|dragover|dragleave)\s*=", re.I)
    for page_name in ("index.html", "criteria.html", "results.html", "detail.html"):
        assert not inline_event.search(read_page(page_name)), page_name
    for asset_name in (
        "pages/index-page.js",
        "pages/criteria-page.js",
        "pages/results-page.js",
        "pages/detail-page.js",
    ):
        assert not inline_event.search(read_asset(asset_name)), asset_name
