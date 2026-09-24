"""Immutable, private image documents in the same durable store as cards."""
import base64
import binascii
import hashlib
import io
import os
import re
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from storage import load_document, save_document
import workspace as w

MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_OWNER_BYTES = 100 * 1024 * 1024
FIELDS = ("q_image", "a_image")


def valid_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"image_[a-f0-9]{64}", value))


def location(host, image_id):
    return "media/" + image_id, os.path.join(os.path.dirname(host.USERS_PATH), image_id + ".json")


def load(host, image_id):
    if not valid_id(image_id):
        raise w.Problem("Изображение не найдено", 404)
    return load_document(*location(host, image_id))


def normalize(encoded):
    if not isinstance(encoded, str) or len(encoded) > (MAX_BYTES + 2) // 3 * 4:
        raise w.Problem("Изображение должно быть не больше 2 МБ", 413)
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_BYTES:
            raise w.Problem("Изображение должно быть не больше 2 МБ", 413)
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ("PNG", "JPEG", "WEBP"):
                    raise w.Problem("Выберите изображение PNG, JPEG или WebP")
                if image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise w.Problem("Выберите неподвижное изображение до 16 мегапикселей")
                image.load()
                image = ImageOps.exif_transpose(image)
                image.thumbnail((2400, 2400))
                clean = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                clean.info.clear()
                output = io.BytesIO()
                clean.save(output, format="WEBP", quality=90, method=4)
                result = output.getvalue()
                if len(result) > MAX_BYTES:
                    raise w.Problem("Уменьшите размер изображения", 413)
                return result
    except (ValueError, binascii.Error, OSError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise w.Problem("Не удалось прочитать изображение. Выберите PNG, JPEG или WebP") from None


def put(host, name, user, encoded):
    raw = normalize(encoded)
    image_id = "image_" + hashlib.sha256(name.encode() + b"\0" + raw).hexdigest()
    owned = user.setdefault("media", {})
    if image_id not in owned:
        if sum(item["size"] for item in owned.values()) + len(raw) > MAX_OWNER_BYTES:
            raise w.Problem("Достигнут лимит изображений профиля — 100 МБ", 413)
        save_document(*location(host, image_id), {"owner": name, "data": base64.b64encode(raw).decode(),
                                                "type": "image/webp"})
        owned[image_id] = {"size": len(raw)}
    return image_id


def referenced(cards, image_id):
    return any(card.get(field) == image_id for card in cards for field in FIELDS)


def allowed(user, image_id):
    ws = user.get("workspace", {})
    versions = [version for c in ws.get("conflicts", {}).values()
                for version in (c["current"], c["proposed"])]
    return image_id in user.get("media", {}) or referenced(
        list(ws.get("cards", {}).values()) + versions, image_id)


def validate_refs(host, user, body):
    for field in FIELDS:
        image_id = body.get(field)
        if image_id and (not valid_id(image_id) or not allowed(user, image_id) or not load(host, image_id)):
            raise w.Problem("Нет доступа к изображению. Загрузите его в своём профиле.", 403)
