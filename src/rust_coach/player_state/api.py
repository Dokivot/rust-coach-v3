import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request

from rust_coach.contracts import AppError
from rust_coach.player_state.schemas import ScopeCreate, StateBinding, StatePreview, StateUpdate, field_catalog
from rust_coach.player_state.service import preview_changes


def player_state_router(calculator, settings):
    router = APIRouter()
    preview_lock = asyncio.Lock()

    @router.get("/api/player-state/fields")
    async def fields():
        items = {key: value["name"] for key, value in calculator.catalog.items.items()}
        items.update({"scrap": "废料", "workbench1": "一级工作台", "workbench2": "二级工作台",
                      "workbench3": "三级工作台", "door.hinged.metal": "金属门", "lock.code": "密码锁",
                      "cupboard.tool": "工具柜"})
        return {"fields": field_catalog(), "items": items, "inventory_ttl_minutes": 30,
                "ruleset": "pc_vanilla", "schema": StateUpdate.model_json_schema()}

    @router.get("/api/player-state/scopes")
    async def scopes(request: Request):
        return request.app.state.runtime.player_states.list_scopes()

    @router.post("/api/player-state/scopes", status_code=201)
    async def create_scope(payload: ScopeCreate, request: Request):
        return request.app.state.runtime.player_states.create(payload)

    @router.get("/api/player-state/scopes/{scope_id}")
    async def snapshot(scope_id: UUID, request: Request, revision: int | None = Query(default=None, ge=0)):
        return request.app.state.runtime.player_states.get(str(scope_id), revision=revision)

    @router.post("/api/player-state/scopes/{scope_id}/observations")
    async def observe(scope_id: UUID, payload: StateUpdate, request: Request):
        if payload.changes.visual_observations:
            raise AppError("visual_confirmation_required", "截图观测请通过识别确认卡片保存。", 422)
        return request.app.state.runtime.player_states.update(str(scope_id), payload)

    @router.post("/api/player-state/scopes/{scope_id}/preview")
    async def preview(scope_id: UUID, payload: StatePreview, request: Request):
        if preview_lock.locked():
            raise AppError("state_preview_busy", "已有状态解析正在进行，请稍后重试。", 409)
        async with preview_lock:
            current = request.app.state.runtime.player_states.get(str(scope_id))
            observed_at = datetime.now(timezone.utc).isoformat()
            try:
                async with asyncio.timeout(settings.request_timeout_seconds):
                    result = await preview_changes(request.app.state.state_model, payload.message,
                        (await fields())["items"], settings.instruction_role)
            except TimeoutError as error:
                raise AppError("model_timeout", "状态解析超时，请使用表单或稍后重试。", 504) from error
            if result["status"] == "draft":
                result["update"] = {"observation_id": str(uuid4()), "expected_revision": current["revision"],
                                    "observed_at": observed_at, "source": "text_confirmed", "changes": result.pop("changes")}
            return result

    @router.get("/api/conversations/{conversation_id}/player-state")
    async def conversation_state(conversation_id: UUID, request: Request):
        store = request.app.state.runtime.player_states
        identifier = store.binding(str(conversation_id))
        return {"scope_id": identifier, "snapshot": store.get(identifier) if identifier else None}

    @router.post("/api/conversations/{conversation_id}/player-state")
    async def bind_state(conversation_id: UUID, payload: StateBinding, request: Request):
        identifier = str(payload.scope_id) if payload.scope_id else None
        await request.app.state.runtime.bind_player_state(str(conversation_id), identifier)
        return {"scope_id": identifier}

    return router
