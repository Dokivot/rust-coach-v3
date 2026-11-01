import logging
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
from rust_coach.llm import ChatModel, CompatibleChatModel
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime
from rust_coach.calculations import Calculator, CalculationRequest
from rust_coach.calculations.presentation import render_calculation


logger = logging.getLogger("rust_coach")
STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, model: ChatModel | None = None) -> FastAPI:
    settings = settings or Settings()
    calculator = Calculator()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds, connect=10),
            follow_redirects=False,
        ) as client:
            store = ConversationStore(settings.database_path)
            app.state.runtime = ChatRuntime(settings, model or CompatibleChatModel(settings, client), store,
                calculation_model=model or CompatibleChatModel(settings, client, stream_responses=True))
            yield

    app = FastAPI(title="Rust Coach", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_request_guard(request: Request, call_next):
        if request.method in {"POST", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": {"code": "origin_rejected", "message": "仅接受同源页面请求。"}}, status_code=403)
            if request.method == "POST" and request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": {"code": "json_required", "message": "请求需要 JSON 格式。"}}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'"
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
    async def public_config():
        return {
            "model": settings.model,
            "configured": settings.configured,
            "capabilities": {"chat": settings.configured, "knowledge": False,
                             "web_search": False, "decision": False, "calculators": True},
            "history_turns": settings.history_turns,
        }

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

    @app.post("/api/conversations/{conversation_id}/messages", response_model=Turn)
    async def chat(conversation_id: UUID, payload: ChatRequest, request: Request):
        return await request.app.state.runtime.chat(str(conversation_id), payload)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/calculator")
    async def calculator_page():
        return FileResponse(STATIC / "calculator.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
