"""以文件和 JSONL 实现筛选任务仓储，并保证关键写入的原子性。

数据流：应用层字典/记录 -> JSON 序列化 -> 任务目录中的 JSON、JSONL、CSV 文件；
读取时执行相反过程，再将数据交还应用用例。
原理：临时文件替换避免半写文件，进程内锁保护并发追加，JSONL 保留审计历史。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable

from .errors import AppError


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


def _json_text(value: Any) -> str:
    """将对象稳定序列化为中文 JSON 文本，兼顾机器读取与人工审计。"""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


class FileStore:
    """单进程 JSON/JSONL 文件仓库。

    所有替换式写入都先落到同目录临时文件，再通过 os.replace 原子替换。
    JSONL 追加同样采用“读取 + 原子替换”，避免进程中断留下半行数据。
    """

    def __init__(self, root: Path) -> None:
        """创建仓储根目录和可重入锁，为后续原子读写准备共享边界。"""
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _job_dir(self, job_id: str) -> Path:
        """校验任务标识后计算目录路径，阻止路径穿越。"""
        if not _SAFE_ID.fullmatch(job_id):
            raise AppError("INVALID_JOB_ID", "任务 ID 格式无效")
        return self.root / "jobs" / job_id

    def _require_job_dir(self, job_id: str) -> Path:
        """取得现存任务目录，不存在时转换为统一业务错误。"""
        path = self._job_dir(job_id)
        if not path.is_dir():
            raise AppError("JOB_NOT_FOUND", "筛选任务不存在", {"job_id": job_id})
        return path

    def _preview_dir(self, preview_id: str) -> Path:
        """校验预览标识并定位尚未转为任务的临时目录。"""
        if not _SAFE_ID.fullmatch(preview_id):
            raise AppError("INVALID_PREVIEW_ID", "预检 ID 格式无效")
        return self.root / "import-previews" / preview_id

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """先写同目录临时文件再原子替换，避免中断留下半份数据。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            ) as temporary:
                temporary.write(text)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _read_json(path: Path, default: Any = None) -> Any:
        """读取 JSON；文件缺失时返回调用方默认值，损坏时报告存储错误。"""
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppError("STORAGE_CORRUPTED",
                "运行数据文件无法读取",
                {"file": path.name},
            ) from exc

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        """逐行读取追加式记录，保留事件顺序并校验对象类型。"""
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("JSONL 记录必须是对象")
                records.append(value)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AppError("STORAGE_CORRUPTED",
                "运行数据文件无法读取",
                {"file": path.name, "line": line_number if "line_number" in locals() else None},
            ) from exc
        return records

    @classmethod
    def _write_jsonl(cls, path: Path, records: Iterable[dict[str, Any]]) -> None:
        """把记录集合整体序列化后原子写入，用于初始化或批量更新。"""
        text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        cls._atomic_write(path, text)

    @classmethod
    def _append_jsonl(cls, path: Path, record: dict[str, Any]) -> None:
        """在进程锁保护下追加记录，防止并发线程交叉写入。"""
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        cls._atomic_write(path, existing + json.dumps(record, ensure_ascii=False) + "\n")

    def save_import_preview(
        self,
        preview_id: str,
        file_name: str,
        file_bytes: bytes,
        metadata: dict[str, Any],
    ) -> None:
        """保存上传原文件与解析摘要，供用户选岗后的任务创建复用。"""
        with self._lock:
            preview_dir = self._preview_dir(preview_id)
            if preview_dir.exists():
                raise AppError("IMPORT_PREVIEW_ALREADY_EXISTS", "导入预检 ID 已存在")
            preview_dir.mkdir(parents=True)
            self._atomic_write(preview_dir / "source.csv", file_bytes.decode("utf-8-sig"))
            self._atomic_write(
                preview_dir / "preview.json",
                _json_text({**metadata, "file_name": Path(file_name).name}),
            )

    def get_import_preview(self, preview_id: str) -> tuple[str, bytes, dict[str, Any]]:
        """读取原文件名、字节和摘要，让任务创建复用同一份已预览输入。"""
        with self._lock:
            preview_dir = self._preview_dir(preview_id)
            if not preview_dir.is_dir():
                raise AppError("IMPORT_PREVIEW_NOT_FOUND", "导入预检不存在或已失效")
            metadata = self._read_json(preview_dir / "preview.json")
            source_path = preview_dir / "source.csv"
            if not isinstance(metadata, dict) or not source_path.is_file():
                raise AppError("STORAGE_CORRUPTED", "导入预检文件格式错误")
            return str(metadata["file_name"]), source_path.read_bytes(), metadata

    def create_job(
        self,
        job: dict[str, Any],
        resumes: list[dict[str, Any]],
        benchmark_references: list[dict[str, Any]],
        audit_events: list[dict[str, Any]],
    ) -> None:
        """一次性写入任务、生产简历、隔离参考数据和初始审计事件。"""
        with self._lock:
            job_dir = self._job_dir(job["job_id"])
            if job_dir.exists():
                raise AppError("JOB_ALREADY_EXISTS", "任务 ID 已存在")
            job_dir.mkdir(parents=True)
            self._atomic_write(job_dir / "job.json", _json_text(job))
            self._write_jsonl(job_dir / "resumes.jsonl", resumes)
            self._write_jsonl(job_dir / "benchmark-reference.jsonl", benchmark_references)
            self._write_jsonl(job_dir / "audit.jsonl", audit_events)
            self._atomic_write(job_dir / "idempotency.json", _json_text({}))

    def get_job(self, job_id: str) -> dict[str, Any]:
        """读取任务元数据和当前流程状态。"""
        with self._lock:
            job_dir = self._require_job_dir(job_id)
            value = self._read_json(job_dir / "job.json")
            if not isinstance(value, dict):
                raise AppError("STORAGE_CORRUPTED", "任务文件格式错误")
            return value

    def save_job(self, job: dict[str, Any]) -> None:
        """原子保存任务快照，使状态更新不会产生半写文件。"""
        with self._lock:
            job_dir = self._require_job_dir(job["job_id"])
            self._atomic_write(job_dir / "job.json", _json_text(job))

    def read_resumes(self, job_id: str) -> list[dict[str, Any]]:
        """读取当前任务筛选范围内的原始简历。"""
        with self._lock:
            return self._read_jsonl(self._require_job_dir(job_id) / "resumes.jsonl")

    def read_benchmark_references(self, job_id: str) -> list[dict[str, Any]]:
        """读取只供验证的基准字段，生产分析不得把它当作输入。"""
        with self._lock:
            return self._read_jsonl(self._require_job_dir(job_id) / "benchmark-reference.jsonl")

    def find_resume(self, job_id: str, resume_id: str) -> dict[str, Any] | None:
        """在任务简历中按标识查找候选人，未命中时返回空值。"""
        return next(
            (item for item in self.read_resumes(job_id) if item.get("resume_id") == resume_id),
            None,
        )

    def save_criteria_draft(self, job_id: str, draft: dict[str, Any]) -> None:
        """保存模型提取但尚未由 HR 确认的标准草稿。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "criteria" / "draft.json"
            self._atomic_write(path, _json_text(draft))

    def get_criteria_draft(self, job_id: str) -> dict[str, Any] | None:
        """读取可编辑草稿；尚未提取时返回空值。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "criteria" / "draft.json"
            value = self._read_json(path)
            return value if isinstance(value, dict) else None

    def save_criteria_version(self, job_id: str, version: int, data: dict[str, Any]) -> None:
        """保存已确认标准版本，作为分析的不可歧义输入。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "criteria" / f"version-{version}.json"
            self._atomic_write(path, _json_text(data))

    def get_criteria_version(self, job_id: str, version: int) -> dict[str, Any] | None:
        """按版本号读取确认标准，保证分析可重现。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "criteria" / f"version-{version}.json"
            value = self._read_json(path)
            return value if isinstance(value, dict) else None

    def save_analysis_meta(self, job_id: str, analysis_id: str, meta: dict[str, Any]) -> None:
        """保存分析进度快照，供后台任务更新和前端轮询共享。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "analyses" / f"{analysis_id}-meta.json"
            self._atomic_write(path, _json_text(meta))

    def get_analysis_meta(self, job_id: str, analysis_id: str) -> dict[str, Any] | None:
        """读取指定分析的进度元数据。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "analyses" / f"{analysis_id}-meta.json"
            value = self._read_json(path)
            return value if isinstance(value, dict) else None

    def initialize_analysis_items(self, job_id: str, analysis_id: str) -> None:
        """初始化本轮分析项文件，防止不同运行结果混合。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "analyses" / f"{analysis_id}.jsonl"
            self._write_jsonl(path, [])

    def append_analysis_item(self, job_id: str, analysis_id: str, item: dict[str, Any]) -> None:
        """并发安全地追加一名候选人的归一化结果。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "analyses" / f"{analysis_id}.jsonl"
            self._append_jsonl(path, item)

    def read_analysis_items(self, job_id: str, analysis_id: str) -> list[dict[str, Any]]:
        """读取指定运行的全部候选人分析项。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "analyses" / f"{analysis_id}.jsonl"
            return self._read_jsonl(path)

    def mark_analyses_stale(self, job_id: str) -> int:
        """标准变化后标记旧分析过期，并返回受影响数量。"""
        with self._lock:
            analyses_dir = self._require_job_dir(job_id) / "analyses"
            if not analyses_dir.exists():
                return 0
            changed = 0
            for path in analyses_dir.glob("*-meta.json"):
                meta = self._read_json(path)
                if isinstance(meta, dict) and meta.get("status") != "stale":
                    meta["status"] = "stale"
                    self._atomic_write(path, _json_text(meta))
                    changed += 1
            return changed

    def append_review(self, job_id: str, review: dict[str, Any]) -> None:
        """追加 HR 复核，不覆盖原 AI 结论或早期人工记录。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "reviews.jsonl"
            self._append_jsonl(path, review)

    def read_reviews(self, job_id: str) -> list[dict[str, Any]]:
        """读取全部复核，应用层再按候选人选择最新版本。"""
        with self._lock:
            return self._read_jsonl(self._require_job_dir(job_id) / "reviews.jsonl")

    def append_audit(self, job_id: str, event: dict[str, Any]) -> None:
        """追加带时间和请求标识的审计事件。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "audit.jsonl"
            self._append_jsonl(path, event)

    def read_audit(self, job_id: str) -> list[dict[str, Any]]:
        """按发生顺序返回完整审计轨迹。"""
        with self._lock:
            return self._read_jsonl(self._require_job_dir(job_id) / "audit.jsonl")

    def get_idempotency(self, job_id: str, key: str) -> dict[str, Any] | None:
        """按幂等键查找既有响应，避免重复请求再次产生副作用。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "idempotency.json"
            values = self._read_json(path, {})
            if not isinstance(values, dict):
                raise AppError("STORAGE_CORRUPTED", "幂等记录文件格式错误")
            value = values.get(key)
            return value if isinstance(value, dict) else None

    def save_idempotency(self, job_id: str, key: str, value: dict[str, Any]) -> None:
        """记录首次成功响应，后续相同请求可直接复用。"""
        with self._lock:
            path = self._require_job_dir(job_id) / "idempotency.json"
            values = self._read_json(path, {})
            if not isinstance(values, dict):
                raise AppError("STORAGE_CORRUPTED", "幂等记录文件格式错误")
            values[key] = value
            self._atomic_write(path, _json_text(values))
