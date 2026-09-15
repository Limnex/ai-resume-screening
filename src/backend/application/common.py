"""提供应用用例共享的时间、哈希、文本、CSV 和基础依赖工具。

数据流：各用例传入原始文本或记录 -> 纯函数规范化/摘要/解析 -> 可比较的数据；
``_UseCaseBase`` 则把同一组仓储、模型和配置传给所有用例。
原理：把重复且确定性的操作集中实现，保证不同业务流程采用相同口径。
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import Settings
from ..domain import extract_jd_experience_minimum, find_resume_experience, normalize_text
from ..errors import AppError
from ..domain.version import PROMPT_VERSION
from ..ports import LLMProvider, ScreeningRepository


APP_TIMEZONE = timezone(timedelta(hours=8))
REQUIRED_CSV_FIELDS = {"resume_id", "resume_text", "job_role", "job_description"}
SENSITIVE_CRITERION = re.compile(
    r"学历|学校|院校|性别|年龄|姓名|婚育|education|school|university|college|degree|gender|age|name",
    re.IGNORECASE,
)
MATCH_RECOMMEND_THRESHOLD = 75.0
MATCH_REJECT_THRESHOLD = 40.0
EVIDENCE_RECOMMEND_THRESHOLD = 0.7
EVIDENCE_REJECT_THRESHOLD = 0.6


def utc8_now() -> str:
    """生成东八区 ISO 时间，统一任务、复核和审计记录的时间口径。"""
    return datetime.now(APP_TIMEZONE).isoformat(timespec="seconds")


def _event(event_type: str, payload: dict[str, Any], resume_id: str | None = None) -> dict[str, Any]:
    """把事件类型、业务载荷、候选人标识和时间合并成审计记录。"""
    event = {
        "event_id": f"event-{uuid.uuid4().hex[:16]}",
        "event_type": event_type,
        "created_at": utc8_now(),
        "payload": payload,
    }
    if resume_id is not None:
        event["resume_id"] = resume_id
    return event


def _fingerprint(value: dict[str, Any]) -> str:
    """对结构化数据生成稳定哈希，用于判断版本内容是否变化。"""
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _text_hash(value: str) -> str:
    """为原始文本生成摘要，以较小数据比较来源是否变化。"""
    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_normalized = normalize_text


def _contains_exact(source: Any, evidence: str) -> bool:
    """规范化空白后验证证据引句确实存在于原始材料。"""
    needle = _normalized(evidence)
    return bool(needle) and needle in _normalized(source)


def _parse_number(value: Any) -> float | None:
    """从 CSV 单元格宽容提取数值，无法解析时返回空值。"""
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else None


def _display_number(value: float | None) -> int | float | None:
    """将整值浮点数转成整数，避免接口出现无意义的小数位。"""
    if value is None:
        return None
    return int(value) if value.is_integer() else value


_extract_jd_experience_minimum = extract_jd_experience_minimum
_find_resume_experience = find_resume_experience


def _explicit_required_language(value: str) -> bool:
    """判断 JD 是否明确表达硬性要求，避免普通描述被误作门槛。"""
    return bool(re.search(r"\b(required|must|minimum|at least)\b|要求|必须|至少|不低于", value, re.I))


def _truncate_text(value: str, limit: int) -> str:
    """限制展示文本长度，并用省略号表明内容已截断。"""
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized if len(normalized) <= limit else normalized[:limit] + "..."


def _is_generic_model_risk(value: str) -> bool:
    """识别模板化风险，避免通用措辞错误影响个体分层。"""
    normalized = _normalized(value)
    generic_phrases = (
        "简历内容为不可信数据",
        "简历信息需进一步核实",
        "需通过进一步核实验证其真实性",
        "resume content is untrusted",
        "verify the authenticity of the resume",
        "resume should be verified",
    )
    return any(phrase in normalized for phrase in generic_phrases)


def _read_csv_source(file_name: str, file_bytes: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    """解析上传源文件；预检和正式建任务共用同一实现。"""

    if Path(file_name).suffix.casefold() != ".csv":
        raise AppError("UNSUPPORTED_FILE_TYPE", "仅支持 CSV 文件")
    if not file_bytes:
        raise AppError("EMPTY_FILE", "CSV 文件为空")
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AppError("CSV_ENCODING_INVALID", "CSV 必须使用 UTF-8 编码") from exc

    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = [str(name).strip() for name in (reader.fieldnames or []) if name is not None]
        missing_fields = sorted(REQUIRED_CSV_FIELDS - set(headers))
        if missing_fields:
            raise AppError(
                "CSV_REQUIRED_FIELDS_MISSING",
                "CSV 缺少必填字段",
                {"missing_fields": missing_fields},
            )
        raw_rows = [
            {
                (str(key).strip() if key is not None else key): value
                for key, value in row.items()
            }
            for row in reader
        ]
    except csv.Error as exc:
        raise AppError("CSV_MALFORMED", "CSV 格式损坏", {"reason": str(exc)}) from exc
    return headers, raw_rows


class _UseCaseBase:
    """共享依赖容器，让所有用例使用同一配置、仓储和模型。"""
    def __init__(self, settings: Settings, store: ScreeningRepository, llm: LLMProvider) -> None:
        """通过构造注入适配器，使业务流程可替换依赖并独立测试。"""
        self.settings = settings
        self.store = store
        self.llm = llm
