"""编排候选人分析任务、并发调用模型，并把模型结果归一化为业务结论。

数据流：已确认标准和原始简历 -> 分析计划 -> 并发模型调用 -> 证据校验/评分分层
-> 持久化分析项与进度。原理：模型负责语义判断，确定性的业务规则负责证据约束、
阈值分层和失败降级，从而保证结果可解释且总数守恒。
"""

from __future__ import annotations

from .common import (
    Any, AppError, EVIDENCE_RECOMMEND_THRESHOLD, EVIDENCE_REJECT_THRESHOLD,
    MATCH_RECOMMEND_THRESHOLD, MATCH_REJECT_THRESHOLD, PROMPT_VERSION,
    _UseCaseBase, _contains_exact, _event,
    _find_resume_experience, _fingerprint, _is_generic_model_risk, _normalized,
    _truncate_text, asyncio, re, utc8_now, uuid,
)
from ..schemas import AnalysisStartRequest

class AnalysisUseCases(_UseCaseBase):
    """编排分析准备、并发执行、证据校验、分层和状态查询。"""
    def prepare_analysis(
        self,
        job_id: str,
        request: AnalysisStartRequest,
    ) -> tuple[dict[str, Any], bool]:
        """校验任务、标准版本和幂等键，然后创建一次待运行的分析计划。"""
        job = self.store.get_job(job_id)
        fingerprint = _fingerprint(request.model_dump())
        idempotency_slot = f"analysis:{request.idempotency_key}"
        existing = self.store.get_idempotency(job_id, idempotency_slot)
        if existing:
            if existing.get("fingerprint") != fingerprint:
                raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已被其他请求使用")
            replay = dict(existing["response"])
            replay["idempotent_replay"] = True
            return replay, False

        if job.get("status") == "analyzing":
            raise AppError("ANALYSIS_ALREADY_RUNNING", "该任务已有分析正在运行")
        if job.get("criteria_version") != request.criteria_version:
            raise AppError("CRITERIA_VERSION_CONFLICT",
                "筛选标准版本已变化，请刷新后重试",
                {"current_version": job.get("criteria_version")},
            )
        criteria = self.store.get_criteria_version(job_id, request.criteria_version)
        if not criteria:
            raise AppError("CRITERIA_NOT_CONFIRMED", "请先确认筛选标准")

        resumes = self.store.read_resumes(job_id)
        if not resumes:
            raise AppError("EMPTY_RESUME_BATCH", "任务中没有可分析的简历")
        requested_limit = int(job.get("candidate_limit", 20))
        limit = min(len(resumes), requested_limit, self.settings.llm_max_candidates_per_run, 20)
        selected = resumes[:limit]
        analysis_id = f"analysis-{uuid.uuid4().hex[:12]}"
        meta = {
            "analysis_job_id": analysis_id,
            "job_id": job_id,
            "criteria_version": request.criteria_version,
            "status": "analyzing",
            "source_total": len(resumes),
            "selected_total": limit,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "remaining": limit,
            "progress": 0.0,
            "limit_applied": len(resumes) > limit,
            "resume_ids": [item["resume_id"] for item in selected],
            "model_version": self.settings.llm_model,
            "prompt_version": PROMPT_VERSION,
            "created_at": utc8_now(),
            "completed_at": None,
        }
        response = {
            "analysis_job_id": analysis_id,
            "job_id": job_id,
            "status": "analyzing",
            "source_total": len(resumes),
            "selected_total": limit,
            "limit_applied": len(resumes) > limit,
        }
        self.store.save_analysis_meta(job_id, analysis_id, meta)
        self.store.initialize_analysis_items(job_id, analysis_id)
        job["status"] = "analyzing"
        job["current_analysis_id"] = analysis_id
        job["analysis_progress"] = {
            key: meta[key]
            for key in (
                "source_total",
                "selected_total",
                "processed",
                "succeeded",
                "failed",
                "remaining",
                "progress",
                "limit_applied",
            )
        }
        self.store.save_job(job)
        self.store.save_idempotency(
            job_id,
            idempotency_slot,
            {"fingerprint": fingerprint, "response": response, "created_at": utc8_now()},
        )
        self.store.append_audit(
            job_id,
            _event(
                "analysis_started",
                {
                    "analysis_job_id": analysis_id,
                    "criteria_version": request.criteria_version,
                    "source_total": len(resumes),
                    "selected_total": limit,
                    "limit_applied": len(resumes) > limit,
                    "model_version": self.settings.llm_model,
                    "prompt_version": PROMPT_VERSION,
                },
            ),
        )
        return response, True

    async def run_analysis(self, job_id: str, analysis_id: str) -> None:
        """并发分析候选人并持续落盘进度；单项失败不会终止整批任务。

        数据流：分析计划 -> 信号量限制的单简历调用 -> 归一化结果 -> JSONL 与进度；
        成功、失败和总数最终保持守恒，让前端能准确解释部分失败。
        """
        meta = self.store.get_analysis_meta(job_id, analysis_id)
        if not meta:
            return
        criteria_doc = self.store.get_criteria_version(job_id, int(meta["criteria_version"]))
        if not criteria_doc:
            meta["status"] = "analysis_failed"
            meta["fatal_error"] = {
                "code": "CRITERIA_VERSION_MISSING",
                "message": "已确认的筛选标准文件不存在",
            }
            meta["completed_at"] = utc8_now()
            self.store.save_analysis_meta(job_id, analysis_id, meta)
            return
        job = self.store.get_job(job_id)
        resume_ids = set(meta["resume_ids"])
        resumes = [item for item in self.store.read_resumes(job_id) if item["resume_id"] in resume_ids]
        resumes.sort(key=lambda item: meta["resume_ids"].index(item["resume_id"]))
        semaphore = asyncio.Semaphore(self.settings.llm_concurrency)
        progress_lock = asyncio.Lock()

        async def analyze_one(resume: dict[str, Any]) -> None:
            """在并发限制内分析单份简历，并将成功或失败计入共享进度。"""
            failed = False
            async with semaphore:
                try:
                    model_result = await self.llm.analyze_candidate(job, criteria_doc["criteria"], resume)
                    item = self._normalize_analysis(
                        job_id,
                        analysis_id,
                        criteria_doc,
                        resume,
                        model_result.parsed.model_dump(),
                        model_result.raw,
                        model_result.provider_request_id,
                    )
                    event = _event(
                        "ai_analysis_completed",
                        {
                            "analysis_job_id": analysis_id,
                            "resume_id": resume["resume_id"],
                            "ai_layer": item["ai_layer"],
                            "model_version": self.settings.llm_model,
                            "prompt_version": PROMPT_VERSION,
                        },
                        resume["resume_id"],
                    )
                except AppError as exc:
                    failed = True
                    item = {
                        "resume_id": resume["resume_id"],
                        "analysis_status": "analysis_failed",
                        "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                        "created_at": utc8_now(),
                    }
                    event = _event(
                        "ai_analysis_failed",
                        {
                            "analysis_job_id": analysis_id,
                            "resume_id": resume["resume_id"],
                            "error_code": exc.code,
                        },
                        resume["resume_id"],
                    )
                except Exception as exc:
                    failed = True
                    diagnostic = {
                        "exception_type": type(exc).__name__,
                        "reason": _truncate_text(str(exc) or "未提供异常原因", 500),
                    }
                    item = {
                        "resume_id": resume["resume_id"],
                        "analysis_status": "analysis_failed",
                        "error": {
                            "code": "INTERNAL_ANALYSIS_ERROR",
                            "message": "候选人分析发生内部错误",
                            "details": diagnostic,
                        },
                        "created_at": utc8_now(),
                    }
                    event = _event(
                        "ai_analysis_failed",
                        {
                            "analysis_job_id": analysis_id,
                            "resume_id": resume["resume_id"],
                            "error_code": "INTERNAL_ANALYSIS_ERROR",
                            "error_details": diagnostic,
                        },
                        resume["resume_id"],
                    )

            self.store.append_analysis_item(job_id, analysis_id, item)
            self.store.append_audit(job_id, event)
            async with progress_lock:
                current = self.store.get_analysis_meta(job_id, analysis_id) or meta
                current["processed"] += 1
                if failed:
                    current["failed"] += 1
                else:
                    current["succeeded"] += 1
                current["remaining"] = current["selected_total"] - current["processed"]
                current["progress"] = round(current["processed"] / current["selected_total"], 4)
                self.store.save_analysis_meta(job_id, analysis_id, current)
                current_job = self.store.get_job(job_id)
                current_job["analysis_progress"] = {
                    key: current[key]
                    for key in (
                        "source_total",
                        "selected_total",
                        "processed",
                        "succeeded",
                        "failed",
                        "remaining",
                        "progress",
                        "limit_applied",
                    )
                }
                self.store.save_job(current_job)

        await asyncio.gather(*(analyze_one(resume) for resume in resumes))
        final_meta = self.store.get_analysis_meta(job_id, analysis_id) or meta
        successes = final_meta["succeeded"]
        if final_meta["processed"] != final_meta["succeeded"] + final_meta["failed"]:
            final_meta["status"] = "analysis_failed"
            final_meta["fatal_error"] = {
                "code": "ANALYSIS_COUNT_MISMATCH",
                "message": "分析计数不一致",
            }
            successes = 0
        final_meta["status"] = (
            "analysis_failed"
            if successes == 0
            else "completed_with_errors"
            if final_meta["failed"]
            else "completed"
        )
        final_meta["completed_at"] = utc8_now()
        self.store.save_analysis_meta(job_id, analysis_id, final_meta)
        job = self.store.get_job(job_id)
        job["status"] = "analysis_failed" if successes == 0 else "review_pending"
        job["analysis_progress"] = {
            key: final_meta[key]
            for key in (
                "source_total",
                "selected_total",
                "processed",
                "succeeded",
                "failed",
                "remaining",
                "progress",
                "limit_applied",
            )
        }
        self.store.save_job(job)
        self.store.append_audit(
            job_id,
            _event(
                "analysis_completed" if successes else "analysis_failed",
                {
                    "analysis_job_id": analysis_id,
                    "processed": final_meta["processed"],
                    "succeeded": final_meta["succeeded"],
                    "failed": final_meta["failed"],
                    "status": final_meta["status"],
                },
            ),
        )

    def _normalize_analysis(
        self,
        job_id: str,
        analysis_id: str,
        criteria_doc: dict[str, Any],
        resume: dict[str, Any],
        model_output: dict[str, Any],
        raw_model_output: dict[str, Any],
        provider_request_id: str | None,
    ) -> dict[str, Any]:
        """把不可信模型输出转换成有原文证据约束的业务分析结果。

        模型负责语义关联；代码核对证据是否出现在简历原文，并结合置信度、匹配分数
        与可计算的经验门槛决定 AI 分层，防止模型建议未经验证直接成为结论。
        """
        model_items = {
            str(item.get("condition", "")).casefold(): item for item in model_output.get("analysis", [])
        }
        normalized_items: list[dict[str, Any]] = []
        invalid_evidence = False
        invalid_evidence_downgraded = False
        required_reject = False

        for criterion in criteria_doc["criteria"]:
            model_item = model_items.get(criterion["name"].casefold())
            if not model_item:
                item = {
                    "condition": criterion["name"],
                    "status": "信息不足",
                    "evidence_quotes": [],
                    "evidence_location": "resume_text",
                    "type": "无证据",
                    "match_score": 0.0,
                    "evidence_confidence": 0.0,
                    "semantic_relation": "模型未返回该条件",
                    "evidence_valid": False,
                }
            else:
                item = dict(model_item)
                item["condition"] = criterion["name"]
                raw_evidence_quotes = [
                    str(quote)
                    for quote in item.get("evidence_quotes", [])
                    if str(quote).strip()
                ]
                validated_evidence_quotes = [
                    {
                        "text": quote,
                        "valid": _contains_exact(resume["resume_text"], quote),
                    }
                    for quote in raw_evidence_quotes
                ]
                valid_evidence_quotes = [
                    quote for quote in validated_evidence_quotes if quote["valid"]
                ]
                if item["type"] == "无证据":
                    item["evidence_quotes"] = []
                else:
                    item["evidence_quotes"] = validated_evidence_quotes
                    if any(not quote["valid"] for quote in validated_evidence_quotes):
                        invalid_evidence = True
                evidence_valid = item["type"] != "无证据" and bool(valid_evidence_quotes)
                item["evidence_valid"] = evidence_valid
                if item["type"] != "无证据" and not evidence_valid:
                    invalid_evidence = True
                    invalid_evidence_downgraded = True
                    item.update(
                        {
                            "status": "信息不足",
                            "type": "无证据",
                            "match_score": min(float(item["match_score"]), 20.0),
                            "evidence_confidence": min(float(item["evidence_confidence"]), 0.2),
                            "semantic_relation": "模型引用无法在原始简历正文中定位",
                            "evidence_valid": False,
                        }
                    )
                if item["type"] == "无证据" and item["status"] != "信息不足":
                    item["status"] = "信息不足"
                    item["evidence_confidence"] = min(float(item["evidence_confidence"]), 0.3)
                    item["evidence_valid"] = False
                if item["type"] == "有限推断" and item["status"] == "匹配":
                    item["status"] = "部分匹配"

            if criterion.get("category") == "experience" and criterion.get("minimum_value") is not None:
                experience = _find_resume_experience(resume["resume_text"])
                if experience:
                    actual_years, evidence = experience
                    required_years = float(criterion["minimum_value"])
                    meets = actual_years >= required_years
                    item.update(
                        {
                            "status": "匹配" if meets else "不满足",
                            "evidence_quotes": [{"text": evidence, "valid": True}],
                            "evidence_location": "resume_text",
                            "type": "直接证据",
                            "match_score": 100.0 if meets else round(min(actual_years / required_years, 1) * 100, 2),
                            "evidence_confidence": 0.98,
                            "semantic_relation": (
                                f"简历明确写明 {actual_years:g} 年经验，"
                                f"{'达到' if meets else '未达到'}岗位最低 {required_years:g} 年要求"
                            ),
                            "evidence_valid": True,
                            "actual_value": actual_years,
                            "required_value": required_years,
                        }
                    )
                else:
                    item.update(
                        {
                            "status": "信息不足",
                            "evidence_quotes": [],
                            "evidence_location": "resume_text",
                            "type": "无证据",
                            "match_score": 0.0,
                            "evidence_confidence": 0.0,
                            "semantic_relation": "无法比较经验年限门槛",
                            "evidence_valid": False,
                        }
                    )

            normalized_items.append(item)
            if criterion["type"] == "必要条件":
                if (
                    item["status"] == "不满足"
                    and item["evidence_valid"]
                    and float(item["evidence_confidence"]) >= EVIDENCE_REJECT_THRESHOLD
                ):
                    required_reject = True

        uncertainties = [str(item) for item in model_output.get("uncertainties", []) if str(item).strip()]
        reported_risks = [str(item) for item in model_output.get("risks", []) if str(item).strip()]
        ignored_generic_risks = [item for item in reported_risks if _is_generic_model_risk(item)]
        risks = [item for item in reported_risks if not _is_generic_model_risk(item)]
        if invalid_evidence:
            uncertainties.append("模型引用的部分证据无法在原始简历字段中定位")
            if invalid_evidence_downgraded:
                risks.append("存在无法定位的证据，相关条件已由规则层降级处理")
            else:
                risks.append("部分证据无法定位，已忽略无效引用并保留可验证证据")

        weights = [2.0 if criterion["type"] == "必要条件" else 1.0 for criterion in criteria_doc["criteria"]]
        weight_total = sum(weights) or 1.0
        calculated_match_score = round(
            sum(float(item["match_score"]) * weight for item, weight in zip(normalized_items, weights))
            / weight_total,
            2,
        )
        calculated_evidence_confidence = round(
            sum(float(item["evidence_confidence"]) * weight for item, weight in zip(normalized_items, weights))
            / weight_total,
            4,
        )
        match_score = round(min(float(model_output.get("match_score", 0)), calculated_match_score), 2)
        evidence_confidence = round(
            min(float(model_output.get("evidence_confidence", 0)), calculated_evidence_confidence), 4
        )
        if required_reject or (
            match_score < MATCH_REJECT_THRESHOLD
            and evidence_confidence >= EVIDENCE_REJECT_THRESHOLD
        ):
            layer = "reject"
        elif (
            match_score >= MATCH_RECOMMEND_THRESHOLD
            and evidence_confidence >= EVIDENCE_RECOMMEND_THRESHOLD
        ):
            layer = "recommend"
        else:
            layer = "pending"

        return {
            "job_id": job_id,
            "analysis_job_id": analysis_id,
            "criteria_version": criteria_doc["criteria_version"],
            "resume_id": resume["resume_id"],
            "analysis_status": "completed",
            "model_proposed_layer": model_output.get("proposed_layer"),
            "ai_layer": layer,
            "match_score": match_score,
            "evidence_confidence": evidence_confidence,
            "analysis": normalized_items,
            "uncertainties": list(dict.fromkeys(uncertainties)),
            "risks": list(dict.fromkeys(risks)),
            "ignored_generic_risks": list(dict.fromkeys(ignored_generic_risks)),
            "model_version": self.settings.llm_model,
            "prompt_version": PROMPT_VERSION,
            "provider_request_id": provider_request_id,
            "raw_model_output": raw_model_output,
            "created_at": utc8_now(),
        }

    def analysis_status(self, job_id: str) -> dict[str, Any]:
        """整理前端轮询所需的当前进度、成功数和失败摘要。"""
        job = self.store.get_job(job_id)
        analysis_id = job.get("current_analysis_id")
        if not analysis_id:
            raise AppError("ANALYSIS_NOT_STARTED", "尚未启动分析")
        meta = self.store.get_analysis_meta(job_id, analysis_id)
        if not meta:
            raise AppError("ANALYSIS_NOT_FOUND", "分析任务不存在")
        selected_total = int(meta.get("selected_total", meta.get("total", 0)))
        processed = int(meta.get("processed", 0))
        failed = int(meta.get("failed", 0))
        succeeded = int(meta.get("succeeded", processed - failed))
        items = self.store.read_analysis_items(job_id, analysis_id)
        failures = [
            {"resume_id": item.get("resume_id"), **item.get("error", {})}
            for item in items
            if item.get("analysis_status") == "analysis_failed"
        ]
        return {
            "analysis_job_id": meta["analysis_job_id"],
            "job_id": meta["job_id"],
            "criteria_version": meta["criteria_version"],
            "status": meta["status"],
            "source_total": meta["source_total"],
            "selected_total": selected_total,
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "remaining": int(meta.get("remaining", selected_total - processed)),
            "progress": meta["progress"],
            "limit_applied": meta["limit_applied"],
            "failures": failures,
            "fatal_error": meta.get("fatal_error"),
        }

    def current_analysis(self, job_id: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """返回当前未过期的分析及结果，拒绝未启动或旧标准版本数据。"""
        job = self.store.get_job(job_id)
        analysis_id = job.get("current_analysis_id")
        if not analysis_id:
            raise AppError("ANALYSIS_NOT_STARTED", "尚未启动分析")
        meta = self.store.get_analysis_meta(job_id, analysis_id)
        if not meta:
            raise AppError("ANALYSIS_NOT_FOUND", "分析任务不存在")
        if meta["status"] == "stale":
            raise AppError("ANALYSIS_STALE", "岗位标准已变化，请重新分析")
        if meta["status"] == "analyzing":
            raise AppError("ANALYSIS_NOT_READY", "分析尚未完成", {"progress": meta["progress"]})
        items = self.store.read_analysis_items(job_id, analysis_id)
        return job, meta, items
