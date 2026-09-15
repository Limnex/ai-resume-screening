"""组装 FastAPI 应用，并把 HTTP 请求路由到对应的应用用例。

数据流：浏览器请求 -> 中间件/请求模型校验 -> ``ApplicationServices`` -> 响应契约 -> JSON；
后台分析任务由路由登记后异步执行，查询接口负责读取其进度与结果。
原理：该文件只处理传输层职责，业务规则留在 application/domain 层。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, TypeVar

from fastapi import BackgroundTasks, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings
from .contracts import (
    AnalysisStartedResponse,
    AnalysisStatusResponse,
    AuditTrailResponse,
    CandidateDetailResponse,
    CriteriaConfirmedResponse,
    CriteriaResponse,
    HealthResponse,
    ImportPreviewResponse,
    JobCreatedResponse,
    JobDetailResponse,
    ResultsResponse,
    ReviewSubmittedResponse,
)
from .errors import AppError
from .error_mapping import http_status_for
from .demo_llm import DemoLLM
from .llm import OpenAICompatibleLLM
from .ports import LLMProvider
from .schemas import AnalysisStartRequest, CriteriaUpdateRequest, ReviewRequest
from .application import ApplicationServices
from .storage import FileStore


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


def _request_id(request: Request) -> str:
    """读取请求标识，供响应与审计事件串联同一次调用。"""
    return getattr(request.state, "request_id", f"req-{uuid.uuid4().hex[:16]}")


def _response(
    request: Request,
    payload: dict[str, Any],
    response_model: type[ResponseModel],
) -> ResponseModel:
    """在离开 API 边界前验证响应，防止前后端契约静默漂移。"""

    return response_model.model_validate({**payload, "request_id": _request_id(request)})


def _error_response(request: Request, payload: dict[str, Any], status_code: int) -> JSONResponse:
    """把错误载荷转换为统一 JSON 响应并回显请求标识。"""
    return JSONResponse({**payload, "request_id": _request_id(request)}, status_code=status_code)


def create_app(
    settings: Settings | None = None,
    llm: LLMProvider | None = None,
) -> FastAPI:
    """把配置、仓储、模型和应用用例组装为可运行的 FastAPI 实例。

    外部注入的模型用于测试，未注入时创建真实适配器；两者都通过相同端口进入用例，
    因此测试与生产的数据流保持一致而不让业务层依赖具体网络实现。
    """
    active_settings = settings or Settings.from_env()
    store = FileStore(active_settings.runtime_dir)
    provider = llm or (
        DemoLLM(active_settings)
        if active_settings.demo_mode
        else OpenAICompatibleLLM(active_settings)
    )
    services = ApplicationServices(active_settings, store, provider)

    app = FastAPI(
        title="AI 简历初筛后端",
        version="1.0.0",
        description="真实大模型分析、HR 复核和文件化追溯接口。",
    )
    app.state.settings = active_settings
    app.state.store = store
    app.state.services = services

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        """接收或生成请求 ID，使它贯穿处理链并写回响应头。"""
        request.state.request_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:16]}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        """捕获预期业务失败，按稳定错误码映射 HTTP 响应。"""
        return _error_response(
            request,
            {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
            http_status_for(exc.code),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """把字段校验错误整理为前端可展示的统一格式。"""
        validation_errors = [
            {
                "location": [str(part) for part in item.get("loc", [])],
                "message": item.get("msg", "输入无效"),
                "type": item.get("type", "validation_error"),
            }
            for item in exc.errors()
        ]
        return _error_response(
            request,
            {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求参数不符合要求",
                    "details": {"validation_errors": validation_errors},
                }
            },
            422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """兜底隐藏内部异常细节，并保留请求 ID 供服务端定位。"""
        return _error_response(
            request,
            {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务发生内部错误",
                    "details": {},
                }
            },
            500,
        )

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        """汇总模型配置和服务状态，返回无需业务数据的健康信息。"""
        return _response(
            request,
            {
                "status": "ok",
                "storage": "json_files",
                "llm_configured": active_settings.llm_configured,
                "demo_mode": active_settings.demo_mode,
                "max_candidates_per_run": active_settings.llm_max_candidates_per_run,
            },
            HealthResponse,
        )

    @app.post("/api/v1/import-previews", response_model=ImportPreviewResponse, status_code=201)
    async def preview_import(
        request: Request,
        resume_file: UploadFile = File(...),
    ) -> ImportPreviewResponse:
        """读取上传 CSV 字节并生成岗位预览，此阶段不创建正式任务。"""
        content = await resume_file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise AppError("FILE_TOO_LARGE", "CSV 文件不能超过 100 MB")
        payload = services.jobs.preview_import(resume_file.filename or "resumes.csv", content)
        return _response(request, payload, ImportPreviewResponse)

    @app.post("/api/v1/screening-jobs", response_model=JobCreatedResponse, status_code=201)
    async def create_screening_job(
        request: Request,
        resume_file: UploadFile | None = File(default=None),
        preview_id: str | None = Form(default=None),
        selected_role: str | None = Form(default=None),
        candidate_limit: int = Form(default=20, ge=1, le=20),
    ) -> JobCreatedResponse:
        """接收预览标识与岗位选择，委托用例创建正式筛选任务。"""
        if preview_id:
            payload = services.jobs.create_job_from_preview(preview_id, selected_role or "", candidate_limit)
        else:
            if resume_file is None:
                raise AppError("EMPTY_FILE", "请上传 CSV 文件或提供预检 ID")
            content = await resume_file.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                raise AppError("FILE_TOO_LARGE", "CSV 文件不能超过 100 MB")
            payload = services.jobs.create_job(
                resume_file.filename or "resumes.csv",
                content,
                selected_role or "",
                candidate_limit,
            )
        return _response(request, payload, JobCreatedResponse)

    @app.get("/api/v1/screening-jobs/{job_id}", response_model=JobDetailResponse)
    async def get_screening_job(request: Request, job_id: str) -> JobDetailResponse:
        """按任务 ID 读取导入摘要和当前流程状态。"""
        return _response(request, services.jobs.get_job_detail(job_id), JobDetailResponse)

    @app.post("/api/v1/screening-jobs/{job_id}/criteria/extract", response_model=CriteriaResponse)
    async def extract_criteria(request: Request, job_id: str) -> CriteriaResponse:
        """触发 JD 标准提取，返回供 HR 编辑确认的草稿。"""
        return _response(request, await services.criteria.extract_criteria(job_id), CriteriaResponse)

    @app.get("/api/v1/screening-jobs/{job_id}/criteria", response_model=CriteriaResponse)
    async def get_criteria(request: Request, job_id: str) -> CriteriaResponse:
        """读取任务当前的标准草稿或已确认版本。"""
        return _response(request, services.criteria.get_criteria(job_id), CriteriaResponse)

    @app.put("/api/v1/screening-jobs/{job_id}/criteria", response_model=CriteriaConfirmedResponse)
    async def confirm_criteria(
        request: Request,
        job_id: str,
        body: CriteriaUpdateRequest,
    ) -> CriteriaConfirmedResponse:
        """版本化 HR 确认标准，使其成为候选人分析的正式输入。"""
        return _response(request, services.criteria.confirm_criteria(job_id, body), CriteriaConfirmedResponse)

    @app.post(
        "/api/v1/screening-jobs/{job_id}/analysis",
        response_model=AnalysisStartedResponse,
        status_code=202,
    )
    async def start_analysis(
        request: Request,
        job_id: str,
        body: AnalysisStartRequest,
        background_tasks: BackgroundTasks,
    ) -> AnalysisStartedResponse:
        """建立分析元数据后登记后台任务，并立即返回分析标识。"""
        payload, should_start = services.analyses.prepare_analysis(job_id, body)
        if should_start:
            background_tasks.add_task(services.analyses.run_analysis, job_id, payload["analysis_job_id"])
        return _response(request, payload, AnalysisStartedResponse)

    @app.get(
        "/api/v1/screening-jobs/{job_id}/analysis/status",
        response_model=AnalysisStatusResponse,
    )
    async def get_analysis_status(request: Request, job_id: str) -> AnalysisStatusResponse:
        """读取后台分析进度、成功数和失败详情。"""
        return _response(request, services.analyses.analysis_status(job_id), AnalysisStatusResponse)

    @app.get("/api/v1/screening-jobs/{job_id}/results", response_model=ResultsResponse)
    async def get_results(
        request: Request,
        job_id: str,
        layer: str | None = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        sort: str = Query(default="match_score_desc"),
    ) -> ResultsResponse:
        """根据分页与分层过滤条件返回当前有效分析结果。"""
        return _response(
            request,
            services.results.get_results(job_id, layer, page, page_size, sort),
            ResultsResponse,
        )

    @app.get(
        "/api/v1/screening-jobs/{job_id}/candidates/{resume_id}",
        response_model=CandidateDetailResponse,
    )
    async def get_candidate(request: Request, job_id: str, resume_id: str) -> CandidateDetailResponse:
        """合并原始候选人、AI 证据和最新人工复核供详情页展示。"""
        return _response(
            request,
            services.results.get_candidate_detail(job_id, resume_id),
            CandidateDetailResponse,
        )

    @app.post(
        "/api/v1/screening-jobs/{job_id}/reviews",
        response_model=ReviewSubmittedResponse,
        status_code=201,
    )
    async def submit_review(
        request: Request,
        job_id: str,
        body: ReviewRequest,
    ) -> ReviewSubmittedResponse:
        """通过版本和幂等校验后追加 HR 复核，不覆盖原 AI 结论。"""
        return _response(request, services.results.submit_review(job_id, body), ReviewSubmittedResponse)

    @app.get("/api/v1/screening-jobs/{job_id}/audit-trail", response_model=AuditTrailResponse)
    async def audit_trail(
        request: Request,
        job_id: str,
        resume_id: str | None = Query(default=None),
    ) -> AuditTrailResponse:
        """读取不可覆盖的任务事件序列，供责任链和处理过程回查。"""
        return _response(request, services.results.audit_trail(job_id, resume_id), AuditTrailResponse)

    frontend_dir: Path = active_settings.frontend_dir
    if active_settings.serve_frontend and frontend_dir.is_dir():
        app.mount("/frontend", StaticFiles(directory=frontend_dir, html=True), name="frontend")

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            """将根路径重定向到前端入口，避免重复维护首页响应。"""
            return RedirectResponse(url="/frontend/index.html")

    return app


app = create_app()
