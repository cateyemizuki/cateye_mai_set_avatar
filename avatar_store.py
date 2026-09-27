"""每日随机头像插件 — 头像库存储与 1:1 处理（不依赖 SDK，可离线测试）。"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

from PIL import Image, ImageOps

# 头像库识别的图片后缀。
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 处理成 1:1 后的最大边长（QQ 头像展示尺寸有限，超过则等比缩小）。
MAX_SQUARE_SIZE = 1024

# PIL 图片格式 → 文件后缀。
_FORMAT_EXTS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
}

# 文件名非法字符（含路径分隔符，防止路径穿越）与 Windows 保留名。
_INVALID_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class AvatarStoreError(Exception):
    """头像库操作失败。"""


def list_avatar_files(*dirs: Path | None) -> list[Path]:
    """列出各头像库目录（扁平）下的图片文件，去重并按文件名排序。

    目录不存在或不可读时跳过该目录。
    """
    files: list[Path] = []
    seen: set[str] = set()
    for directory in dirs:
        if not directory:
            continue
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for path in entries:
            try:
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
            except OSError:
                continue
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    return files


def sanitize_avatar_name(raw: object, fallback: str) -> str:
    """把用户输入的名称整理为安全文件名（不含后缀）。"""
    name = _INVALID_NAME_RE.sub("_", str(raw or "")).strip()
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > 64:
        name = name[:64].strip()
    if not name:
        return fallback
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name


def add_image_bytes(
    data: bytes,
    image_dir: Path,
    name: object = None,
) -> tuple[Path, str, str]:
    """把图片字节加入头像库。

    Args:
        data: 图片原始字节。
        image_dir: 头像库目录（自动创建）。
        name: 期望的文件名（不含后缀）；为空时使用内容 MD5 哈希命名。

    Returns:
        (落盘路径, 内容 md5, 状态)。

    状态取值：
    - ``created``：新写入；
    - ``overwritten``：自定义命名撞上同名文件，直接覆盖；
    - ``duplicate``：哈希命名且库内已存在同内容文件（按内容去重，未写入）。
    """
    if not data:
        raise AvatarStoreError("图片数据为空")
    md5_hex = hashlib.md5(data).hexdigest()
    try:
        with Image.open(io.BytesIO(data)) as img:
            image_format = (img.format or "").upper()
            img.load()
    except Exception as exc:
        raise AvatarStoreError(f"无法识别的图片文件：{exc}") from exc
    if not image_format:
        raise AvatarStoreError("无法识别的图片格式")
    ext = _FORMAT_EXTS.get(image_format, ".png")

    image_dir.mkdir(parents=True, exist_ok=True)

    raw_name = str(name or "").strip()
    if raw_name:
        stem = sanitize_avatar_name(raw_name, fallback=md5_hex)
        path = image_dir / f"{stem}{ext}"
        status = "overwritten" if path.exists() else "created"
    else:
        # 哈希命名：按内容去重。
        path = image_dir / f"{md5_hex}{ext}"
        if path.exists():
            return path, md5_hex, "duplicate"
        status = "created"

    path.write_bytes(data)
    return path, md5_hex, status


def _to_square(img: Image.Image, square_mode: str) -> Image.Image:
    """把图片处理为 1:1：默认居中切割，可选拉伸。"""
    width, height = img.size
    if width <= 0 or height <= 0 or width == height:
        return img
    if square_mode == "拉伸":
        side = max(width, height)
        return img.resize((side, side), Image.LANCZOS)
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    return img.crop((left, top, left + side, top + side))


def load_square_png(path: Path, square_mode: str) -> bytes:
    """读取图片并处理为 1:1 PNG 字节（动图取第一帧）。"""
    with Image.open(path) as img:
        try:
            img = ImageOps.exif_transpose(img)  # 尊重 EXIF 方向
        except Exception:
            pass
        square = _to_square(img, square_mode)
        if square.width > MAX_SQUARE_SIZE:
            square = square.resize((MAX_SQUARE_SIZE, MAX_SQUARE_SIZE), Image.LANCZOS)
        if square.mode not in ("RGB", "RGBA"):
            square = square.convert("RGBA")
        buffer = io.BytesIO()
        square.save(buffer, format="PNG")
        return buffer.getvalue()
