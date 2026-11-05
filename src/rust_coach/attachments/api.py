import asyncio
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, Response
from starlette.datastructures import UploadFile

from rust_coach.attachments.store import normalize_image
from rust_coach.contracts import AppError
from rust_coach.vision.schemas import VisualConfirmation


class RequestSizeLimit:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope,receive,send)
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body",b""))
                if total > self.limit:
                    raise AppError("request_too_large","上传内容超过大小限制，请裁剪后重试。",413)
            return message

        await self.app(scope,bounded_receive,send)


def attachment_router(settings):
    router = APIRouter()
    upload_lock = asyncio.Lock()

    @router.post("/api/conversations/{conversation_id}/attachments",status_code=201)
    async def upload(conversation_id: UUID,request: Request):
        with request.app.state.runtime.performance.run("attachment_upload", conversation_id=str(conversation_id)):
            return await upload_image(conversation_id, request)

    async def upload_image(conversation_id, request):
        runtime = request.app.state.runtime
        runtime.store.get(str(conversation_id))
        if upload_lock.locked():
            raise AppError("upload_busy","图片正在处理，请稍后上传下一张。",409)
        async with upload_lock:
            async with request.form(max_files=1,max_fields=0) as form:
                uploaded = form.get("file")
                if not isinstance(uploaded,UploadFile) or len(form) != 1:
                    raise AppError("image_required","请选择一张图片。",422)
                raw = bytearray()
                while chunk := await uploaded.read(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > settings.attachment_max_bytes:
                        raise AppError("attachment_too_large","单张图片超过大小限制，请先裁剪。",413)
                normalized,preview,size = await asyncio.to_thread(normalize_image,bytes(raw),settings.attachment_max_bytes,settings.attachment_max_pixels)
                return request.app.state.attachments.put(str(conversation_id),normalized,preview,size)

    @router.get("/api/conversations/{conversation_id}/attachments/{attachment_id}")
    async def read_image(conversation_id: UUID,attachment_id: UUID,request: Request,thumbnail: bool = Query(default=False)):
        path = request.app.state.attachments.path(str(conversation_id),str(attachment_id),thumbnail)
        return FileResponse(path,media_type="image/png")

    @router.delete("/api/conversations/{conversation_id}/attachments/{attachment_id}",status_code=204)
    async def delete_image(conversation_id: UUID,attachment_id: UUID,request: Request):
        if request.app.state.runtime.lock.locked():
            raise AppError("chat_busy","请等待回复完成后删除图片。",409)
        request.app.state.attachments.delete(str(conversation_id),str(attachment_id))
        return Response(status_code=204)

    @router.post("/api/vision/probe")
    async def probe(request: Request):
        if request.app.state.runtime.lock.locked():
            raise AppError("chat_busy","请等待当前任务完成。",409)
        with request.app.state.runtime.performance.run("vision_probe"):
            return await request.app.state.vision.probe()

    @router.get("/api/conversations/{conversation_id}/visual-drafts/{draft_id}")
    async def draft(conversation_id: UUID,draft_id: UUID,request: Request):
        return request.app.state.vision.drafts.get(str(conversation_id),str(draft_id),allow_inactive=True)

    @router.post("/api/conversations/{conversation_id}/visual-drafts/{draft_id}/confirm")
    async def confirm(conversation_id: UUID,draft_id: UUID,payload: VisualConfirmation,request: Request):
        with request.app.state.runtime.performance.run("visual_confirmation", conversation_id=str(conversation_id),
                                                       request_id=str(payload.confirmation_id)):
            return await request.app.state.runtime.confirm_visual(str(conversation_id),str(draft_id),payload)

    @router.post("/api/conversations/{conversation_id}/visual-drafts/{draft_id}/cancel",status_code=204)
    async def cancel(conversation_id: UUID,draft_id: UUID,request: Request):
        if request.app.state.runtime.lock.locked():
            raise AppError("chat_busy","请等待回复完成后取消草稿。",409)
        request.app.state.vision.drafts.cancel(str(conversation_id),str(draft_id))
        return Response(status_code=204)

    return router
