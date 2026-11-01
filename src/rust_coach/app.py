import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, Conversation, Turn
from rust_coach.advice.service import AdviceService
from rust_coach.knowledge.store import EvidenceStore
from rust_coach.knowledge.web import WebResearch
from rust_coach.llm import ChatModel, CompatibleChatModel
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime
from rust_coach.calculations import Calculator, CalculationRequest
from rust_coach.calculations.presentation import render_calculation
from rust_coach.player_state.api import player_state_router
from rust_coach.presentation.contracts import PresentationPreferences, ResponseContext
from rust_coach.attachments.api import RequestSizeLimit, attachment_router
from rust_coach.attachments.store import AttachmentStore
from rust_coach.vision.service import VisionService


logger = logging.getLogger("rust_coach")
STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, model: ChatModel | None = None, vision_model: ChatModel | None = None) -> FastAPI:
    settings = settings or Settings()
    calculator = Calculator()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds, connect=10),
            follow_redirects=False,
        ) as client:
            store = ConversationStore(settings.database_path)
            attachments = AttachmentStore(store,settings)
            attachments.cleanup()
            vision = VisionService(settings,vision_model or model or CompatibleChatModel(settings.for_vision(),client,stream_responses=True),attachments,store)
            app.state.attachments,app.state.vision = attachments,vision
            app.state.evidence_store = EvidenceStore(store)
            advice_settings = settings.model_copy(update={"max_output_tokens": max(4096, settings.max_output_tokens)})
            advice = AdviceService(model or CompatibleChatModel(advice_settings, client, stream_responses=True),
                WebResearch(settings, client, app.state.evidence_store), settings.instruction_role)
            app.state.runtime = ChatRuntime(settings, model or CompatibleChatModel(settings, client, stream_responses=True), store,
                calculation_model=model or CompatibleChatModel(settings, client, stream_responses=True), advice_service=advice,vision_service=vision)
            app.state.state_model = model or CompatibleChatModel(settings, client, stream_responses=True)
            yield

    app = FastAPI(title="Rust Coach", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
    app.add_middleware(RequestSizeLimit,limit=settings.attachment_max_bytes + 65536)
    app.include_router(player_state_router(calculator, settings))
    app.include_router(attachment_router(settings))

    @app.middleware("http")
    async def local_request_guard(request: Request, call_next):
        if request.method in {"POST", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": {"code": "origin_rejected", "message": "仅接受同源页面请求。"}}, status_code=403)
            content_type = request.headers.get("content-type", "").split(";")[0]
            upload_path = bool(re.fullmatch(r"/api/conversations/[a-fA-F0-9-]{36}/attachments",request.url.path))
            expected_type = "multipart/form-data" if upload_path else "application/json"
            if request.method == "POST" and content_type != expected_type:
                return JSONResponse({"error": {"code": "json_required", "message": "请求需要 JSON 格式。"}}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, error: AppError):
        logger.warning("chat_error code=%s", error.code)
        return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, error: RequestValidationError):
        return JSONResponse({"error": {"code": "invalid_input", "message": "输入格式不正确：请检查必填项、整数数量、支持的规则集和请求标识。消息最多 6000 字。"}}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, error: Exception):
        error_id = str(uuid4())
        logger.error("unexpected_error id=%s type=%s", error_id, type(error).__name__)
        return JSONResponse({"error": {"code": "internal_error", "message": f"服务暂时出错，请重试。错误编号：{error_id}"}}, status_code=500)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/config")
    async def public_config(request: Request):
        return {
            "model": settings.model,
            "configured": settings.configured,
            "capabilities": {"chat": settings.configured, "knowledge": settings.search_provider != "off",
                             "web_search": settings.search_configured, "decision": settings.configured,
                             "calculators": True, "player_state": True},
            "search_provider": settings.search_provider,
            "history_turns": settings.history_turns,
            "presentation": {"persona_version": ResponseContext().persona_version,
                             "preferences_schema": PresentationPreferences.model_json_schema()},
            "vision":request.app.state.vision.capabilities(),
        }

    @app.get("/api/evidence/{document_id}")
    async def get_evidence(document_id: str, request: Request):
        document = request.app.state.evidence_store.get(document_id)
        if document is None:
            raise AppError("evidence_not_found", "证据快照不存在。", 404)
        return document

    @app.get("/api/calculations/catalog")
    async def calculation_catalog():
        return calculator.catalog.public()

    @app.post("/api/calculations")
    async def calculate(payload: CalculationRequest):
        result = calculator.calculate(payload)
        logger.info("calculation id=%s operation=%s snapshot=%s", result["calculation_id"],
                    payload.operation, result["provenance"]["snapshot_id"])
        return {**result, "text": render_calculation(result, calculator.catalog)}

    @app.post("/api/conversations", response_model=Conversation, status_code=201)
    async def create_conversation(request: Request):
        return request.app.state.runtime.store.create()

    @app.get("/api/conversations/{conversation_id}", response_model=Conversation)
    async def get_conversation(conversation_id: UUID, request: Request):
        return request.app.state.runtime.store.get(str(conversation_id))

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: UUID, request: Request):
        await request.app.state.runtime.delete(str(conversation_id))
        return Response(status_code=204)

    @app.post("/api/conversations/{conversation_id}/presentation", response_model=PresentationPreferences)
    async def set_presentation(conversation_id: UUID, payload: PresentationPreferences, request: Request):
        return await request.app.state.runtime.set_presentation_preferences(str(conversation_id), payload)

    @app.post("/api/conversations/{conversation_id}/messages", response_model=Turn)
    async def chat(conversation_id: UUID, payload: ChatRequest, request: Request):
        return await request.app.state.runtime.chat(str(conversation_id), payload)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/calculator")
    async def calculator_page():
        return FileResponse(STATIC / "calculator.html")

    @app.get("/player-state")
    async def player_state_page():
        return FileResponse(STATIC / "player-state.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
