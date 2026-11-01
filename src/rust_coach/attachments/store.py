import hashlib
import io
import os
import warnings
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from rust_coach.contracts import AppError


def normalize_image(raw, max_bytes, max_pixels):
    if not raw or len(raw) > max_bytes:
        raise AppError("attachment_too_large", "图片为空或超过单张大小限制，请裁剪后重试。", 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
                    raise AppError("image_format", "仅支持静态 PNG、JPEG、WebP 图片。", 415)
                if source.width * source.height > max_pixels or max(source.size) > 8192:
                    raise AppError("image_dimensions", "图片像素或边长过大，请先裁剪所需区域。", 413)
                source.load()
                normalized = ImageOps.exif_transpose(source).convert("RGB")
                output = io.BytesIO()
                normalized.save(output, format="PNG")
                thumbnail = normalized.copy()
                thumbnail.thumbnail((480, 320))
                preview = io.BytesIO()
                thumbnail.save(preview, format="PNG")
                return output.getvalue(), preview.getvalue(), normalized.size
    except AppError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise AppError("image_invalid", "图片无法解码，请重新导出为 PNG 或 JPEG。", 422) from error


class AttachmentStore:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.root = settings.database_path.parent / (settings.database_path.stem + "-attachments")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with store.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    sha256 TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    bytes INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS attachments_owner ON attachments(conversation_id);
            """)

    def put(self, conversation_id, normalized, preview, size):
        self.store.get(conversation_id)
        self.cleanup()
        with self.store.connection() as connection:
            total = connection.execute("SELECT COALESCE(SUM(bytes),0) FROM attachments WHERE deleted=0").fetchone()[0]
        if total + len(normalized) > 256 * 1024 * 1024:
            raise AppError("attachment_quota", "本地图片空间已满，请删除不再需要的图片。", 413)
        identifier = str(uuid4())
        now = datetime.now(timezone.utc)
        paths = [self.root / (identifier + suffix) for suffix in (".png", ".thumb.png")]
        try:
            for path, data in zip(paths, (normalized, preview)):
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
            with self.store.connection() as connection:
                connection.execute("INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?,0)",
                    (identifier, conversation_id, hashlib.sha256(normalized).hexdigest(), size[0], size[1], len(normalized),
                     now.isoformat(), (now + timedelta(hours=self.settings.attachment_retention_hours)).isoformat()))
        except Exception:
            for path in paths:
                path.unlink(missing_ok=True)
            raise
        return self.get(conversation_id, identifier)

    def get(self, conversation_id, identifier, *, allow_deleted=False):
        with self.store.connection() as connection:
            row = connection.execute("SELECT * FROM attachments WHERE id=? AND conversation_id=?", (identifier, conversation_id)).fetchone()
        if row is None:
            raise AppError("attachment_not_found", "图片不存在或不属于当前会话。", 404)
        result = dict(row)
        result["available"] = (not result["deleted"] and datetime.fromisoformat(result["expires_at"]) > datetime.now(timezone.utc)
                               and (self.root / (identifier + ".png")).is_file())
        if not result["available"] and not allow_deleted:
            raise AppError("attachment_expired", "图片已删除或过期，请重新选择有效图片。", 410)
        result["url"] = f"/api/conversations/{conversation_id}/attachments/{identifier}"
        result["thumbnail_url"] = result["url"] + "?thumbnail=true"
        return result

    def path(self, conversation_id, identifier, thumbnail=False):
        self.get(conversation_id, identifier)
        path = self.root / (identifier + (".thumb.png" if thumbnail else ".png"))
        if not path.is_file():
            raise AppError("attachment_missing", "图片文件已不可用，请重新上传。", 410)
        return path

    def image(self, conversation_id, identifier, bbox=None):
        raw = self.path(conversation_id, identifier).read_bytes()
        if bbox is None:
            return raw
        with Image.open(io.BytesIO(raw)) as source:
            left, top, right, bottom = bbox
            if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
                raise AppError("invalid_region", "截图区域无效。", 422)
            cropped = source.crop((int(left * source.width), int(top * source.height),
                max(int(left * source.width) + 1, int(right * source.width)), max(int(top * source.height) + 1, int(bottom * source.height))))
            output = io.BytesIO()
            cropped.save(output, format="PNG")
            return output.getvalue()

    def delete(self, conversation_id, identifier):
        self.get(conversation_id, identifier, allow_deleted=True)
        with self.store.connection() as connection:
            connection.execute("UPDATE attachments SET deleted=1 WHERE id=?", (identifier,))
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='vision_cache'").fetchone():
                connection.execute("DELETE FROM vision_cache WHERE conversation_id=?",(conversation_id,))
        for suffix in (".png", ".thumb.png"):
            (self.root / (identifier + suffix)).unlink(missing_ok=True)

    def delete_conversation(self, conversation_id):
        with self.store.connection() as connection:
            identifiers = [row[0] for row in connection.execute("SELECT id FROM attachments WHERE conversation_id=?", (conversation_id,))]
        for identifier in identifiers:
            self.delete(conversation_id, identifier)

    def cleanup(self):
        now = datetime.now(timezone.utc).isoformat()
        with self.store.connection() as connection:
            rows = connection.execute("SELECT id,conversation_id FROM attachments WHERE deleted=0 AND expires_at<=? LIMIT 50", (now,)).fetchall()
            valid = {row[0] for row in connection.execute("SELECT id FROM attachments WHERE deleted=0")}
        for row in rows:
            self.delete(row["conversation_id"], row["id"])
        for path in list(self.root.glob("*.png"))[:500]:
            if path.name.split(".")[0] not in valid:
                path.unlink(missing_ok=True)
