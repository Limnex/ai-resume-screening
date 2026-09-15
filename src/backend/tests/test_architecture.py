"""以源码契约测试守住后端分层、响应模型和 Windows 启动方式。

数据流：源码/脚本文本 -> 必须或禁止模式搜索 -> 架构断言；
原理：这些约束无法从单个 API 响应观察，需用轻量静态检查防回归。
"""

from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND.parents[1]


def test_api_routes_publish_explicit_response_models() -> None:
    """验证主要路由声明响应契约且返回前执行模型校验。"""
    main = (BACKEND / "main.py").read_text(encoding="utf-8")
    assert main.count("response_model=") >= 11
    assert "response_model.model_validate" in main


def test_application_layers_do_not_depend_on_http_or_file_adapters() -> None:
    """验证应用层不反向依赖 FastAPI 或具体文件仓储。"""
    application = BACKEND / "application"
    for path in application.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "from fastapi" not in source
        assert "import httpx" not in source
        assert "from ..storage import" not in source


def test_infrastructure_errors_do_not_carry_http_status_codes() -> None:
    """验证基础设施异常只使用业务错误码。"""
    for relative in ("storage.py", "llm.py"):
        source = (BACKEND / relative).read_text(encoding="utf-8")
        assert "AppError(4" not in source
        assert "AppError(5" not in source


def test_windows_start_script_preserves_runtime_contract() -> None:
    """验证 Windows 启动脚本保留后端运行约定。"""
    launcher = (PROJECT_ROOT / "start.cmd").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "scripts" / "start.ps1").read_text(encoding="utf-8")
    assert "scripts\\start.ps1" in launcher
    assert "-ExecutionPolicy Bypass" in launcher
    assert 'if /I "%~1"=="--check"' in launcher
    assert ".venv\\Scripts\\python.exe" in script
    assert "src.backend.main:app" in script
    assert "--workers 1" in script
    assert "http://127.0.0.1:8000/frontend/index.html" in script
    assert "Settings.from_env().llm_configured" in script
