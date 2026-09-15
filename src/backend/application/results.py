"""把当前分析转换为结果视图，并处理 HR 复核和审计查询。

数据流：分析项 + 最新复核记录 -> 汇总/候选人详情；HR 决策 -> 规则校验 ->
追加复核与审计事件。原理：AI 层与人工层分开保存，既保留原判断又记录最终责任链。
"""

from __future__ import annotations

import uuid

from .analysis import AnalysisUseCases
from .common import (
    Any, AppError, _UseCaseBase, _event, _fingerprint,
    _truncate_text, utc8_now,
)
from ..schemas import ReviewRequest

class ResultReviewUseCases(_UseCaseBase):
    """提供结果列表、候选人详情、HR 复核与审计轨迹。"""
    def __init__(
        self,
        settings: Settings,
        store: ScreeningRepository,
        llm: LLMProvider,
        analyses: AnalysisUseCases,
    ) -> None:
        """注入共享依赖和分析用例，保证结果查询复用相同有效性规则。"""
        super().__init__(settings, store, llm)
        self.analyses = analyses

    def _current_analysis(self, job_id: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """复用分析用例取得当前有效运行，统一过期判断口径。"""
        return self.analyses.current_analysis(job_id)

    @staticmethod
    def _latest_reviews(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """按候选人折叠历史记录，只保留版本最高的人工复核。"""
        latest: dict[str, dict[str, Any]] = {}
        for review in reviews:
            resume_id = review["resume_id"]
            if resume_id not in latest or int(review["version"]) > int(latest[resume_id]["version"]):
                latest[resume_id] = review
        return latest

    @staticmethod
    def _analysis_items_for_view(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """复制分析项为展示数据，避免查询过程修改持久化原对象。"""
        """把历史单字符串证据和新数组证据统一成当前 API 结构。"""

        adapted_items: list[dict[str, Any]] = []
        for source_item in items:
            item = dict(source_item)
            quotes = item.get("evidence_quotes")
            if isinstance(quotes, list):
                adapted_quotes: list[dict[str, Any]] = []
                for quote in quotes:
                    if isinstance(quote, dict):
                        text = str(quote.get("text", "")).strip()
                        if text:
                            adapted_quotes.append(
                                {"text": text, "valid": bool(quote.get("valid", False))}
                            )
                    else:
                        text = str(quote).strip()
                        if text:
                            adapted_quotes.append(
                                {"text": text, "valid": bool(item.get("evidence_valid", False))}
                            )
                item["evidence_quotes"] = adapted_quotes
            else:
                legacy_evidence = str(item.get("evidence", "")).strip()
                item["evidence_quotes"] = (
                    [
                        {
                            "text": legacy_evidence,
                            "valid": bool(item.get("evidence_valid", False)),
                        }
                    ]
                    if legacy_evidence and item.get("type") != "无证据"
                    else []
                )
            item.pop("evidence", None)
            adapted_items.append(item)
        return adapted_items

    def get_results(
        self,
        job_id: str,
        layer: str | None,
        page: int,
        page_size: int,
        sort: str,
    ) -> dict[str, Any]:
        """合并 AI 结果与最新 HR 层，执行过滤、排序、分页并计算全量汇总。"""
        _, meta, analysis_items = self._current_analysis(job_id)
        resumes = {item["resume_id"]: item for item in self.store.read_resumes(job_id)}
        latest_reviews = self._latest_reviews(self.store.read_reviews(job_id))
        result_items: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for analysis in analysis_items:
            if analysis.get("analysis_status") != "completed":
                failures.append({"resume_id": analysis.get("resume_id"), **analysis.get("error", {})})
                continue
            resume = resumes.get(analysis["resume_id"])
            if not resume:
                continue
            review = latest_reviews.get(analysis["resume_id"])
            final_layer = review["final_layer"] if review else None
            effective_layer = final_layer or analysis["ai_layer"]
            result_items.append(
                {
                    "resume_id": analysis["resume_id"],
                    "resume_excerpt": _truncate_text(resume.get("resume_text", ""), 160),
                    "ai_layer": analysis["ai_layer"],
                    "final_layer": final_layer,
                    "effective_layer": effective_layer,
                    "review_status": "reviewed" if review else "unreviewed",
                    "review_reason": review["reason"] if review else None,
                    "risk": bool(analysis.get("risks")),
                    "match_score": analysis["match_score"],
                    "evidence_confidence": analysis["evidence_confidence"],
                }
            )

        selected_total = int(meta.get("selected_total", meta.get("total", 0)))
        processed = int(meta.get("processed", 0))
        failed_count = int(meta.get("failed", len(failures)))
        succeeded = int(meta.get("succeeded", processed - failed_count))
        if len(result_items) != succeeded or len(failures) != failed_count:
            raise AppError("ANALYSIS_COUNT_MISMATCH",
                "分析统计与候选人结果数量不一致",
                {
                    "selected_total": selected_total,
                    "succeeded": succeeded,
                    "failed": failed_count,
                    "result_total": len(result_items),
                    "failure_records": len(failures),
                },
            )
        summary = {
            "result_total": len(result_items),
            "selected_total": selected_total,
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed_count,
            "remaining": int(meta.get("remaining", selected_total - processed)),
            "recommend": sum(item["effective_layer"] == "recommend" for item in result_items),
            "pending": sum(item["effective_layer"] == "pending" for item in result_items),
            "reject": sum(item["effective_layer"] == "reject" for item in result_items),
            "reviewed": sum(item["review_status"] == "reviewed" for item in result_items),
            "source_total": meta["source_total"],
            "limit_applied": meta["limit_applied"],
        }
        if layer:
            if layer not in {"recommend", "pending", "reject"}:
                raise AppError("INVALID_LAYER", "分层筛选值无效")
            result_items = [item for item in result_items if item["effective_layer"] == layer]
        if sort in {"match_score_desc", "confidence_desc"}:
            result_items.sort(key=lambda item: (-float(item["match_score"]), item["resume_id"]))
        elif sort in {"match_score_asc", "confidence_asc"}:
            result_items.sort(key=lambda item: (float(item["match_score"]), item["resume_id"]))
        elif sort == "resume_id":
            result_items.sort(key=lambda item: item["resume_id"])
        else:
            raise AppError("INVALID_SORT", "排序方式无效")

        start = (page - 1) * page_size
        return {
            "job_id": job_id,
            "criteria_version": meta["criteria_version"],
            "summary": summary,
            "page": page,
            "page_size": page_size,
            "items": result_items[start : start + page_size],
            "failures": failures,
        }

    def get_candidate_detail(self, job_id: str, resume_id: str) -> dict[str, Any]:
        """组合原始简历、逐项证据、AI 建议和最新人工复核。"""
        _, _, analysis_items = self._current_analysis(job_id)
        resume = self.store.find_resume(job_id, resume_id)
        if not resume:
            raise AppError("CANDIDATE_NOT_FOUND", "候选人不属于该任务")
        analysis = next((item for item in analysis_items if item.get("resume_id") == resume_id), None)
        if not analysis:
            raise AppError("CANDIDATE_NOT_ANALYZED", "该候选人未进入本轮设置的分析范围")
        latest_review = self._latest_reviews(self.store.read_reviews(job_id)).get(resume_id)
        candidate = {
            "resume_text": resume.get("resume_text", ""),
        }
        if analysis.get("analysis_status") != "completed":
            return {
                "resume_id": resume_id,
                "candidate": candidate,
                "analysis_status": "analysis_failed",
                "analysis_error": analysis.get("error"),
                "ai_recommendation": None,
                "hr_review": None,
                "review_version": 0,
            }
        hr_review = None
        if latest_review:
            hr_review = {
                "status": "reviewed",
                "decision": latest_review["decision"],
                "ai_layer": latest_review["ai_layer"],
                "final_layer": latest_review["final_layer"],
                "reason": latest_review["reason"],
                "reviewed_at": latest_review["reviewed_at"],
                "version": latest_review["version"],
            }
        return {
            "resume_id": resume_id,
            "candidate": candidate,
            "analysis_status": "completed",
            "ai_recommendation": {
                "layer": analysis["ai_layer"],
                "match_score": analysis["match_score"],
                "evidence_confidence": analysis["evidence_confidence"],
                "analysis": self._analysis_items_for_view(analysis["analysis"]),
                "uncertainties": analysis["uncertainties"],
                "risks": analysis["risks"],
                "model_version": analysis["model_version"],
                "prompt_version": analysis["prompt_version"],
            },
            "hr_review": hr_review,
            "review_version": latest_review["version"] if latest_review else 0,
        }

    def submit_review(self, job_id: str, request: ReviewRequest) -> dict[str, Any]:
        """校验版本、改判语义和幂等键后追加人工决定与审计事件。"""
        job, meta, analysis_items = self._current_analysis(job_id)
        fingerprint = _fingerprint(request.model_dump())
        idempotency_slot = f"review:{request.idempotency_key}"
        existing = self.store.get_idempotency(job_id, idempotency_slot)
        if existing:
            if existing.get("fingerprint") != fingerprint:
                raise AppError("IDEMPOTENCY_KEY_REUSED", "幂等键已被其他请求使用")
            replay = dict(existing["response"])
            replay["idempotent_replay"] = True
            return replay

        analysis = next(
            (
                item
                for item in analysis_items
                if item.get("resume_id") == request.resume_id and item.get("analysis_status") == "completed"
            ),
            None,
        )
        if not analysis:
            raise AppError("CANDIDATE_NOT_ANALYZED", "候选人不属于当前有效分析结果")

        latest = self._latest_reviews(self.store.read_reviews(job_id)).get(request.resume_id)
        current_version = int(latest["version"]) if latest else 0
        if request.expected_version != current_version:
            raise AppError("REVIEW_VERSION_CONFLICT",
                "复核记录已更新，请刷新后重试",
                {"current_version": current_version},
            )
        if request.decision == "adopt" and request.final_layer != analysis["ai_layer"]:
            raise AppError("ADOPT_LAYER_MISMATCH", "采纳 AI 建议时，最终分层必须与 AI 建议一致")
        if request.decision == "override" and request.final_layer == analysis["ai_layer"]:
            raise AppError("OVERRIDE_LAYER_UNCHANGED", "推翻 AI 建议时，最终分层必须与 AI 建议不同")

        reviewed_at = utc8_now()
        review = {
            "review_id": f"review-{uuid.uuid4().hex[:12]}",
            "job_id": job_id,
            "analysis_job_id": meta["analysis_job_id"],
            "criteria_version": meta["criteria_version"],
            "resume_id": request.resume_id,
            "version": current_version + 1,
            "ai_layer": analysis["ai_layer"],
            "decision": request.decision,
            "final_layer": request.final_layer,
            "reason": request.reason,
            "reviewed_at": reviewed_at,
        }
        self.store.append_review(job_id, review)
        response = {
            "review_id": review["review_id"],
            "resume_id": request.resume_id,
            "version": review["version"],
            "ai_layer": analysis["ai_layer"],
            "final_layer": request.final_layer,
            "review_status": "reviewed",
            "reason": request.reason,
            "reviewed_at": reviewed_at,
        }
        self.store.save_idempotency(
            job_id,
            idempotency_slot,
            {"fingerprint": fingerprint, "response": response, "created_at": utc8_now()},
        )
        self.store.append_audit(
            job_id,
            _event(
                "hr_review_submitted",
                {
                    "review_id": review["review_id"],
                    "resume_id": request.resume_id,
                    "version": review["version"],
                    "ai_layer": analysis["ai_layer"],
                    "final_layer": request.final_layer,
                    "decision": request.decision,
                    "reason": request.reason,
                },
                request.resume_id,
            ),
        )

        pending_ids = {
            item["resume_id"]
            for item in analysis_items
            if item.get("analysis_status") == "completed" and item.get("ai_layer") == "pending"
        }
        reviewed_ids = set(self._latest_reviews(self.store.read_reviews(job_id)))
        if pending_ids.issubset(reviewed_ids):
            job["status"] = "completed"
            self.store.save_job(job)
        return response

    def audit_trail(self, job_id: str, resume_id: str | None) -> dict[str, Any]:
        """读取任务事件，可按候选人过滤后返回可追溯历史。"""
        self.store.get_job(job_id)
        events = self.store.read_audit(job_id)
        if resume_id:
            events = [
                event
                for event in events
                if event.get("resume_id") == resume_id
                or event.get("payload", {}).get("resume_id") == resume_id
            ]
        return {"job_id": job_id, "resume_id": resume_id, "events": events}
