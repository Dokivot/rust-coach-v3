import base64
import io
import json
import secrets
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

from rust_coach.contracts import AppError, ImagePart, ModelMessage, TextPart


def image_part(raw, detail="high"):
    return ImagePart(data_url="data:image/png;base64," + base64.b64encode(raw).decode(), detail=detail)


def parse_json(reply):
    if reply.truncated:
        raise ValueError("truncated_json")
    content = reply.content.strip()
    if content.startswith("```json") and content.endswith("```"):
        content = content[7:-3].strip()
    return json.loads(content)


async def probe_model(model, settings):
    codes = [str(secrets.randbelow(900000) + 100000) for _ in range(2)]
    parts = [TextPart(text='Read the six-digit CODE in each image, in image order. Return only JSON {"codes":["...","..."]}.')]
    for code, color in zip(codes, ("#b7d8ff", "#ffdcb7")):
        canvas = Image.new("RGB", (800, 300), color)
        draw = ImageDraw.Draw(canvas)
        draw.text((40, 40), "CODE", fill="black", font=ImageFont.load_default(size=40))
        draw.text((40, 120), code, fill="black", font=ImageFont.load_default(size=90))
        output = io.BytesIO()
        canvas.save(output, format="PNG")
        parts.append(image_part(output.getvalue(), settings.vision_detail))
    reply = await model.complete([ModelMessage(role="user", content=parts)])
    try:
        parsed = parse_json(reply)
        if parsed != {"codes": codes}:
            raise ValueError("Image contents not correctly read")
    except (ValueError, TypeError) as error:
        raise AppError("vision_probe_failed", "模型未正确读取测试图片，截图能力暂不开启。请检查视觉模型与接口配置。", 422) from error
    return {"status": "passed", "tested_at": datetime.now(timezone.utc).isoformat(),
            "checks": ["two_images", "visible_text", "image_order", "json_response"],
            "usage": reply.usage, "limit": "仅证明测试图读取成功，不证明 Rust 图标、数量或空间判断准确。"}
