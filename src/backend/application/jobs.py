"""负责 CSV 导入预览、岗位选择与筛选任务创建。

数据流：上传的 CSV 字节 -> 表头/行解析 -> 岗位统计预览 -> 选择单一岗位 ->
写入任务及该岗位简历。原理：先预览再创建，可在写入生产任务前发现格式和选择错误。
"""

from __future__ import annotations

from .common import (
    Any, AppError, Path, _UseCaseBase, _display_number, _event, _normalized,
    _parse_number, _read_csv_source, _text_hash, hashlib, utc8_now, uuid,
)

class JobUseCases(_UseCaseBase):
    """把 CSV 输入分两阶段转换为岗位预览和单岗位筛选任务。"""
    def preview_import(self, file_name: str, file_bytes: bytes) -> dict[str, Any]:
        """解析上传 CSV、统计岗位和可用数据，并保存二阶段预览。"""
        headers, raw_rows = _read_csv_source(file_name, file_bytes)
        role_names = sorted(
            {
                str(row.get("job_role") or "").strip()
                for row in raw_rows
                if str(row.get("job_role") or "").strip()
            }
        )
        roles: list[dict[str, Any]] = []
        preview_fields = ("resume_id", "resume_text", "job_role", "job_description")
        for role in role_names:
            rows = [row for row in raw_rows if str(row.get("job_role") or "").strip() == role]
            jd_values = {
                _normalized(row.get("job_description")): str(row.get("job_description") or "").strip()
                for row in rows
                if str(row.get("job_description") or "").strip()
            }
            issues: list[str] = []
            if any(not str(row.get("job_description") or "").strip() for row in rows):
                issues.append("存在缺少原始完整 JD 的记录")
            if len(jd_values) != 1:
                issues.append("同一岗位存在不同的原始 JD")
            roles.append(
                {
                    "name": role,
                    "count": len(rows),
                    "jd_valid": not issues,
                    "jd_text": next(iter(jd_values.values())) if len(jd_values) == 1 else None,
                    "issues": issues,
                    "preview_rows": [
                        {field: str(row.get(field) or "") for field in preview_fields}
                        for row in rows[:5]
                    ],
                }
            )
        if not roles:
            raise AppError("NO_JOB_ROLES", "CSV 中没有可选择的 job_role")

        preview_id = f"preview-{uuid.uuid4().hex[:12]}"
        metadata = {
            "preview_id": preview_id,
            "headers": headers,
            "roles": roles,
            "created_at": utc8_now(),
        }
        self.store.save_import_preview(preview_id, file_name, file_bytes, metadata)
        return {
            "preview_id": preview_id,
            "file_name": Path(file_name).name,
            "headers": headers,
            "roles": roles,
        }

    def create_job_from_preview(
        self,
        preview_id: str,
        selected_role: str,
        candidate_limit: int,
    ) -> dict[str, Any]:
        """从已保存的预览读取原文件，再进入统一任务创建流程。"""
        file_name, file_bytes, _ = self.store.get_import_preview(preview_id)
        return self.create_job(file_name, file_bytes, selected_role, candidate_limit)

    def create_job(
        self,
        file_name: str,
        file_bytes: bytes,
        selected_role: str,
        candidate_limit: int,
    ) -> dict[str, Any]:
        """校验岗位与数量，将 CSV 中对应简历写入正式任务。

        数据流：原始行 + 用户选岗 -> 岗位过滤和字段规范化 -> 任务、简历、参考数据；
        生产源与基准标签分开落盘，避免答案字段反向污染真实筛选。
        """
        role = selected_role.strip()
        if not role:
            raise AppError("SELECTED_ROLE_REQUIRED", "必须选择一个具体岗位")
        if candidate_limit < 1 or candidate_limit > 20:
            raise AppError("CANDIDATE_LIMIT_INVALID", "本轮分析数量必须在 1 到 20 之间")
        _, raw_rows = _read_csv_source(file_name, file_bytes)

        available_roles = sorted(
            {
                str(row.get("job_role") or "").strip()
                for row in raw_rows
                if str(row.get("job_role") or "").strip()
            }
        )
        if role not in available_roles:
            raise AppError("SELECTED_ROLE_NOT_FOUND",
                "所选岗位不在 CSV 中",
                {"selected_role": role, "available_roles": available_roles},
            )

        selected_rows = [
            (source_row, row)
            for source_row, row in enumerate(raw_rows, start=2)
            if str(row.get("job_role") or "").strip() == role
        ]
        jd_rows = [
            (source_row, str(row.get("job_description") or "").strip())
            for source_row, row in selected_rows
        ]
        empty_jd_rows = [source_row for source_row, jd in jd_rows if not jd]
        if empty_jd_rows:
            raise AppError("JD_REQUIRED",
                "所选岗位的每一行都必须包含原始完整 JD",
                {"rows": empty_jd_rows[:50]},
            )
        distinct_jd = {_normalized(jd): jd for _, jd in jd_rows}
        if len(distinct_jd) != 1:
            raise AppError("INCONSISTENT_JOB_DESCRIPTION",
                "同一岗位存在不同的原始 JD，请先统一后再创建任务",
                {"distinct_count": len(distinct_jd)},
            )
        original_jd = next(iter(distinct_jd.values()))

        valid_resumes: list[dict[str, Any]] = []
        benchmark_references: list[dict[str, Any]] = []
        invalid_rows: list[dict[str, Any]] = []
        duplicate_rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_text_hashes: set[str] = set()

        for ordinal, (source_row, row) in enumerate(selected_rows, start=1):
            resume_id = str(row.get("resume_id") or "").strip()
            resume_text = str(row.get("resume_text") or "").strip()
            problems: list[str] = []
            if not resume_id:
                problems.append("resume_id 为空")
            if not resume_text:
                problems.append("resume_text 为空")
            if None in row:
                problems.append("列数量与表头不一致")
            if problems:
                invalid_rows.append({"row": source_row, "errors": problems})
                continue

            resume_hash = _text_hash(resume_text)
            if resume_id in seen_ids or resume_hash in seen_text_hashes:
                duplicate_rows.append({"row": source_row, "resume_id": resume_id})
                continue
            seen_ids.add(resume_id)
            seen_text_hashes.add(resume_hash)

            valid_resumes.append(
                {
                    "resume_id": resume_id,
                    "resume_text": resume_text,
                    "job_role": role,
                    "source_row": source_row,
                    "text_hash": resume_hash,
                    "import_order": ordinal,
                }
            )
            benchmark_references.append(
                {
                    "resume_id": resume_id,
                    "resume_skills": str(row.get("resume_skills") or "").strip(),
                    "experience_years": _display_number(_parse_number(row.get("experience_years"))),
                    "projects": str(row.get("projects") or "").strip(),
                    "certifications": str(row.get("certifications") or "").strip(),
                    "education_level": str(row.get("education_level") or "").strip(),
                    "required_skills": str(row.get("required_skills") or "").strip(),
                    "job_experience_required": _display_number(
                        _parse_number(row.get("job_experience_required"))
                    ),
                    "reference_only": True,
                }
            )

        if not valid_resumes:
            raise AppError("NO_VALID_RESUMES",
                "所选岗位没有可导入的有效简历",
                {"invalid_rows": invalid_rows[:50]},
            )

        job_id = f"job-{uuid.uuid4().hex[:12]}"
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        import_summary = {
            "total": len(selected_rows),
            "valid": len(valid_resumes),
            "invalid": len(invalid_rows),
            "duplicates": len(duplicate_rows),
            "invalid_rows": invalid_rows[:50],
            "duplicate_rows": duplicate_rows[:50],
        }
        job = {
            "job_id": job_id,
            "job_role": role,
            "selected_role": role,
            "jd_text": original_jd,
            "candidate_limit": candidate_limit,
            "status": "criteria_pending",
            "criteria_version": None,
            "current_analysis_id": None,
            "analysis_progress": None,
            "created_at": utc8_now(),
            "source_file": {"name": Path(file_name).name, "sha256": file_hash},
            "import_summary": import_summary,
        }
        audit_events = [
            _event(
                "job_created",
                {
                    "job_id": job_id,
                    "selected_role": role,
                    "file_name": Path(file_name).name,
                    "file_hash": file_hash,
                    "candidate_limit": candidate_limit,
                    "import_summary": import_summary,
                },
            )
        ]
        audit_events.extend(
            _event(
                "input_imported",
                {"file_hash": file_hash, "resume_id": resume["resume_id"], "source_row": resume["source_row"]},
                resume["resume_id"],
            )
            for resume in valid_resumes
        )
        self.store.create_job(job, valid_resumes, benchmark_references, audit_events)
        return {
            "job_id": job_id,
            "status": job["status"],
            "job": {
                "job_role": role,
                "jd_text": original_jd,
                "candidate_limit": candidate_limit,
            },
            "import_summary": import_summary,
        }

    def get_job_detail(self, job_id: str) -> dict[str, Any]:
        """汇总任务元数据、导入统计和各流程阶段状态。"""
        job = self.store.get_job(job_id)
        return {
            "job_id": job_id,
            "status": job["status"],
            "job": {
                "job_role": job["job_role"],
                "selected_role": job["selected_role"],
                "jd_text": job["jd_text"],
                "candidate_limit": job.get("candidate_limit", 20),
            },
            "import_summary": job["import_summary"],
            "criteria_version": job["criteria_version"],
            "analysis_progress": job.get("analysis_progress"),
            "created_at": job["created_at"],
        }
