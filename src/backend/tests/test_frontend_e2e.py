"""检查前端保留审计展示与演示边界提示的关键契约。"""

from src.backend.config import PROJECT_ROOT


def test_detail_page_contains_audit_ledger_contract() -> None:
    detail = (PROJECT_ROOT / "src" / "frontend" / "detail.html").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "src" / "frontend" / "pages" / "detail-page.js").read_text(encoding="utf-8")
    api = (PROJECT_ROOT / "src" / "frontend" / "api.js").read_text(encoding="utf-8")

    assert 'id="auditTimeline"' in detail
    assert "证据账本" in detail
    assert "loadAuditTrail" in script
    assert "getAuditTrail" in api


def test_every_page_identifies_demo_boundary() -> None:
    frontend = PROJECT_ROOT / "src" / "frontend"
    for name in ("index.html", "criteria.html", "results.html", "detail.html"):
        content = (frontend / name).read_text(encoding="utf-8")
        assert "演示模式" in content
        assert "结论须由 HR 复核" in content
