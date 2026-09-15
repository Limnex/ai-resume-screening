"""读取后端运行配置并把环境变量转换为有类型的设置对象。

数据流：``.env``/系统环境变量 -> 基础类型解析与默认值处理 -> ``Settings``；
API、存储和大模型适配器再从同一个设置对象取得各自需要的参数。
原理：把外部配置集中在应用边界解析，业务代码便不必反复读取字符串环境变量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """解析整数环境变量，并把结果限制在允许区间内；非法输入回退默认值。"""
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _read_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """解析浮点环境变量，并通过上下界阻止异常配置进入运行期。"""
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _read_bool(name: str, default: bool) -> bool:
    """将常见否定字符串归一化为假，其余输入按真值处理。"""
    raw = os.getenv(name, "true" if default else "false").strip().casefold()
    return raw not in {"0", "false", "no", "off"}


def _read_origins(name: str) -> tuple[str, ...]:
    """将逗号分隔的跨域来源清洗为无尾斜杠的不可变元组。"""
    default = "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:5500,http://localhost:5500"
    return tuple(origin.strip().rstrip("/") for origin in os.getenv(name, default).split(",") if origin.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """集中保存运行配置；由环境构造后注入 API、仓储和模型适配器。"""
    project_root: Path
    runtime_dir: Path
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout_seconds: float
    llm_max_retries: int
    llm_concurrency: int
    llm_max_candidates_per_run: int
    demo_mode: bool = False
    serve_frontend: bool = True
    cors_origins: tuple[str, ...] = ()

    @property
    def frontend_dir(self) -> Path:
        """根据项目根目录计算前端静态资源的绝对路径。"""
        return self.project_root / "src" / "frontend"

    @property
    def llm_configured(self) -> bool:
        """检查模型调用所需的地址、密钥和模型名是否齐备。"""
        return self.demo_mode or bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        """加载 dotenv 与系统环境，把外部字符串转换为完整设置对象。"""
        project_root = PROJECT_ROOT
        dotenv_path = env_file or project_root / ".env"
        load_dotenv(dotenv_path=dotenv_path, override=False)

        runtime_raw = os.getenv("BACKEND_RUNTIME_DIR", "src/backend/runtime").strip()
        runtime_dir = Path(runtime_raw)
        if not runtime_dir.is_absolute():
            runtime_dir = project_root / runtime_dir

        return cls(
            project_root=project_root,
            runtime_dir=runtime_dir.resolve(),
            llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
            llm_model=os.getenv("LLM_MODEL", "").strip(),
            llm_timeout_seconds=_read_float("LLM_TIMEOUT_SECONDS", 60, 5, 300),
            llm_max_retries=_read_int("LLM_MAX_RETRIES", 2, 0, 5),
            llm_concurrency=_read_int("LLM_CONCURRENCY", 5, 1, 8),
            llm_max_candidates_per_run=_read_int("LLM_MAX_CANDIDATES_PER_RUN", 20, 1, 20),
            demo_mode=_read_bool("DEMO_MODE", False),
            serve_frontend=_read_bool("SERVE_FRONTEND", True),
            cors_origins=_read_origins("CORS_ORIGINS"),
        )
