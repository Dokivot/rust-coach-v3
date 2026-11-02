from fastapi import APIRouter, Request

from rust_coach.contracts import AppError
from rust_coach.memory.schemas import MemoryPreferenceUpdate, MemorySettingsUpdate


def memory_router():
    router = APIRouter()

    @router.get("/api/memory")
    async def inspect(request: Request):
        return request.app.state.runtime.memory.store.inspect()

    def available(request):
        runtime = request.app.state.runtime
        if runtime.lock.locked():
            raise AppError("chat_busy", "请等待当前回复完成后修改记忆。", 409)
        return runtime.memory

    @router.post("/api/memory/settings")
    async def settings(payload: MemorySettingsUpdate, request: Request):
        return available(request).store.control("on" if payload.enabled else "off")

    @router.post("/api/memory/preferences")
    async def preferences(payload: MemoryPreferenceUpdate, request: Request):
        return {"message": available(request).set_preference(payload.key, payload.value)}

    @router.delete("/api/memory/{identifier}")
    async def forget(identifier: str, request: Request):
        available(request).store.control("forget", identifier)
        return {"forgotten": True}

    return router
