"""负责从原始 JD 提取筛选标准，并完成 HR 确认和版本化。

数据流：任务中的原始 JD -> 模型提取 -> 业务规则清洗 -> 标准草稿 -> HR 确认版本；
确认后的版本号和指纹成为后续分析可追溯的输入。
原理：AI 只生成可编辑草稿，明确确认后才允许进入候选人分析。
"""

from __future__ import annotations

from .common import (
    Any, AppError, PROMPT_VERSION, SENSITIVE_CRITERION, _UseCaseBase,
    _contains_exact, _event, _explicit_required_language,
    _extract_jd_experience_minimum, re, utc8_now,
)
from ..schemas import CriteriaUpdateRequest

class CriteriaUseCases(_UseCaseBase):
    """管理 AI 标准草稿、查询以及 HR 确认后的版本化标准。"""
    async def extract_criteria(self, job_id: str) -> dict[str, Any]:
        """将原始 JD 交给模型，再清洗证据、类型和年限门槛形成草稿。"""
        job = self.store.get_job(job_id)
        extraction_job = dict(job)
        result = None
        criteria: list[dict[str, Any]] = []
        filtered_sensitive: list[str] = []
        validation_issues: list[str] = []

        for attempt in range(2):
            result = await self.llm.extract_criteria(extraction_job)
            criteria = []
            filtered_sensitive = []
            validation_issues = []
            seen: set[str] = set()
            for extracted in result.parsed.criteria:
                name_key = extracted.name.casefold()
                if name_key in seen:
                    continue
                seen.add(name_key)
                if SENSITIVE_CRITERION.search(f"{extracted.name} {extracted.requirement}"):
                    filtered_sensitive.append(extracted.name)
                    continue
                evidence_valid = _contains_exact(job["jd_text"], extracted.source_evidence)
                criterion_type = extracted.type
                if (
                    extracted.category == "domain"
                    and criterion_type == "必要条件"
                    and not _explicit_required_language(extracted.source_evidence)
                ):
                    criterion_type = "加分条件"
                criteria.append(
                    {
                        "id": len(criteria) + 1,
                        "name": extracted.name,
                        "category": extracted.category,
                        "type": criterion_type,
                        "source": "AI 提取",
                        "source_evidence": extracted.source_evidence,
                        "source_evidence_valid": evidence_valid,
                        "requirement": extracted.requirement or extracted.source_evidence,
                        "minimum_value": extracted.minimum_value,
                    }
                )
                if not evidence_valid:
                    validation_issues.append(f"条件“{extracted.name}”的 JD 引用无法定位")
                if extracted.name.casefold() in {"skills", "skill", "experience", "经验", "技能"}:
                    validation_issues.append(f"条件“{extracted.name}”名称过于宽泛")

            if not criteria:
                validation_issues.append("没有安全可用的岗位条件")
            expected_years = _extract_jd_experience_minimum(job["jd_text"])
            experience_criteria = [item for item in criteria if item["category"] == "experience"]
            if expected_years is not None and not any(
                item.get("minimum_value") == expected_years for item in experience_criteria
            ):
                validation_issues.append(f"未完整提取 JD 中的最低经验年限 {expected_years:g} 年")
            if re.search(r"skills?\s+required|required\s+skills?|技能要求|要求.*(?:掌握|熟悉)", job["jd_text"], re.I):
                if not any(item["category"] == "skill" for item in criteria):
                    validation_issues.append("未提取 JD 中明确出现的技能要求")

            if not validation_issues:
                break
            extraction_job["extraction_feedback"] = validation_issues

        if result is None or not criteria:
            raise AppError("NO_SAFE_CRITERIA", "模型没有返回可用的岗位筛选条件")
        if validation_issues:
            raise AppError("CRITERIA_EXTRACTION_INCOMPLETE",
                "AI 未能完整、可靠地提取岗位条件",
                {"issues": validation_issues, "attempts": 2},
            )

        draft = {
            "job_id": job_id,
            "criteria_version": None,
            "criteria": criteria,
            "model_version": self.settings.llm_model,
            "prompt_version": PROMPT_VERSION,
            "provider_request_id": result.provider_request_id,
            "raw_model_output": result.raw,
            "validation_attempts": 2 if extraction_job.get("extraction_feedback") else 1,
            "created_at": utc8_now(),
        }
        self.store.save_criteria_draft(job_id, draft)
        self.store.append_audit(
            job_id,
            _event(
                "criteria_extracted",
                {
                    "model_version": self.settings.llm_model,
                    "prompt_version": PROMPT_VERSION,
                    "criteria_count": len(criteria),
                    "filtered_sensitive_criteria": filtered_sensitive,
                    "validation_attempts": draft["validation_attempts"],
                },
            ),
        )
        return {"job_id": job_id, "criteria_version": None, "criteria": criteria}

    def get_criteria(self, job_id: str) -> dict[str, Any]:
        """优先返回已确认标准，否则返回模型草稿供继续编辑。"""
        job = self.store.get_job(job_id)
        current_version = job.get("criteria_version")
        if current_version:
            saved = self.store.get_criteria_version(job_id, int(current_version))
            if saved:
                return {
                    "job_id": job_id,
                    "criteria_version": current_version,
                    "criteria": saved["criteria"],
                    "confirmed_at": saved["confirmed_at"],
                }
        draft = self.store.get_criteria_draft(job_id)
        if draft:
            return {"job_id": job_id, "criteria_version": None, "criteria": draft["criteria"]}
        raise AppError("CRITERIA_NOT_EXTRACTED", "尚未提取或保存筛选条件")

    def confirm_criteria(self, job_id: str, request: CriteriaUpdateRequest) -> dict[str, Any]:
        """保存 HR 提交的新标准版本，并让基于旧标准的分析失效。"""
        job = self.store.get_job(job_id)
        draft = self.store.get_criteria_draft(job_id) or {"criteria": []}
        draft_by_name = {item["name"].casefold(): item for item in draft["criteria"]}
        draft_by_id = {item.get("id"): item for item in draft["criteria"]}
        version = int(job.get("criteria_version") or 0) + 1
        criteria: list[dict[str, Any]] = []
        for index, item in enumerate(request.criteria, start=1):
            if SENSITIVE_CRITERION.search(item.name):
                raise AppError("SENSITIVE_CRITERION_FORBIDDEN",
                    "学历、学校、性别、年龄等非岗位能力信息不能作为筛选条件",
                    {"criterion": item.name},
                )
            source_item = draft_by_id.get(item.id) or draft_by_name.get(item.name.casefold())
            criteria.append(
                {
                    "id": index,
                    "name": item.name,
                    "category": item.category,
                    "type": item.type,
                    "source": source_item["source"] if source_item else "HR 新增",
                    "source_evidence": source_item.get("source_evidence", "") if source_item else "",
                    "source_evidence_valid": source_item.get("source_evidence_valid", False) if source_item else False,
                    "requirement": item.requirement
                    or (source_item.get("requirement", "") if source_item else item.name),
                    "minimum_value": item.minimum_value,
                }
            )

        confirmed_at = utc8_now()
        saved = {
            "job_id": job_id,
            "criteria_version": version,
            "criteria": criteria,
            "confirmed_at": confirmed_at,
        }
        stale_count = self.store.mark_analyses_stale(job_id)
        self.store.save_criteria_version(job_id, version, saved)
        job["criteria_version"] = version
        job["status"] = "criteria_confirmed"
        if stale_count:
            job["analysis_progress"] = {"status": "stale"}
        else:
            job["current_analysis_id"] = None
            job["analysis_progress"] = None
        self.store.save_job(job)
        self.store.append_audit(
            job_id,
            _event(
                "criteria_confirmed",
                {
                    "criteria_version": version,
                    "criteria": criteria,
                    "stale_analysis_count": stale_count,
                },
            ),
        )
        return {
            "job_id": job_id,
            "criteria_version": version,
            "status": "criteria_confirmed",
            "confirmed_at": confirmed_at,
        }
