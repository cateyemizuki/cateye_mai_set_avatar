"""每日随机头像插件 — 头像库存储与 1:1 处理（不依赖 SDK，可离线测试）。"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
from pathlib import Path

from PIL import Image, ImageOps

# 头像库识别的图片后缀。
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 处理成 1:1 后的最大边长（QQ 头像展示尺寸有限，超过则等比缩小）。
MAX_SQUARE_SIZE = 1024

# 解码前的像素数上限（防"解压缩炸弹"：小体积文件解出超大位图吃满内存）。
# 压缩体积上限在 plugin.py（32MB），两者是独立的两道闸。
MAX_IMAGE_PIXELS = 64_000_000

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


def check_image_pixels(img: Image.Image) -> None:
    """在真正解码（``load()``）之前检查像素数，超出上限直接拒绝。

    Pillow 自带的 ``Image.MAX_IMAGE_PIXELS``（DecompressionBomb）只是兜底且默认仅告警，
    这里按本插件需要设一道显式硬闸：体积小、尺寸畸形的图片（解压缩炸弹）不解码。
    """
    width, height = img.size
    if width <= 0 or height <= 0:
        raise AvatarStoreError("无法识别的图片尺寸")
    if width * height > MAX_IMAGE_PIXELS:
        raise AvatarStoreError(
            f"图片尺寸过大（{width}x{height}，上限约 {MAX_IMAGE_PIXELS // 1_000_000} 兆像素）"
        )


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
            check_image_pixels(img)  # 解码前先卡像素数
            img.load()
    except AvatarStoreError:
        raise
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
        check_image_pixels(img)  # 解码前先卡像素数（防解压缩炸弹）
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


# ==================== 序号注册表 ====================
#
# /msa list 的序号按添加时间紧凑排列：图片库每次更新（添加或移除）后重新
# 紧凑排序为 1..N，序号会随增删动态变化；缩略图缓存按图片内容哈希（md5）
# 命名，天然指向图片本身而不是序号，重排无需失效缓存。

REGISTRY_VERSION = 1


def load_registry(path: Path) -> dict:
    """读取序号注册表；文件缺失或损坏时返回空表（序号从 1 重新分配）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            entries: list[dict] = []
            for raw in data["entries"]:
                if not isinstance(raw, dict):
                    continue
                try:
                    seq = int(raw.get("seq") or 0)
                except (TypeError, ValueError):
                    continue
                file_path = str(raw.get("path") or "").strip()
                if seq <= 0 or not file_path:
                    continue
                try:
                    added = float(raw.get("added") or 0.0)
                except (TypeError, ValueError):
                    added = 0.0
                entries.append({"seq": seq, "path": file_path, "added": added})
            max_seq = max((entry["seq"] for entry in entries), default=0)
            next_seq = max(int(data.get("next_seq") or 0), max_seq + 1)
            return {"version": REGISTRY_VERSION, "next_seq": next_seq, "entries": entries}
    except FileNotFoundError:
        pass
    except Exception:
        pass  # 损坏的注册表按空表处理，靠 sync_registry 重新补登记
    return {"version": REGISTRY_VERSION, "next_seq": 1, "entries": []}


def _path_key(path: Path | str) -> str:
    """路径去重键：resolve 规范化（Windows 顺带统一盘符/目录大小写，Linux 保留大小写）。

    同一文件经不同拼写的路径发现时按同一键去重；而 Linux 上 genuinely 不同的
    大小写文件名（A.png / a.png）不会互相误伤。
    """
    try:
        return str(Path(path).resolve())
    except OSError:
        return str(path)


def save_registry(path: Path, registry: dict) -> bool:
    """原子写入注册表。

    Returns:
        是否写入成功。失败时调用方应记日志——序号变更未持久化，下次读取会回退。
    """
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
        return True
    except OSError:
        return False


def sync_registry(registry: dict, *dirs: Path | None) -> bool:
    """注册表与头像库目录对账，并在更新后紧凑重排序号。

    - 清掉文件已不存在的条目（被外部删除）；
    - 把未登记的图片按（修改时间, 文件名）补登记（兼容旧库 / 手工放图）；
    - 每次更新（增或删）后按添加时间紧凑重排为 1..N（删除 3 则原 4 变 3）。

    Returns:
        注册表是否有变动（由调用方决定是否落盘）。
    """
    changed = False
    kept: list[dict] = []
    for entry in registry.get("entries", []):
        try:
            if Path(entry["path"]).is_file():
                kept.append(entry)
            else:
                changed = True
        except OSError:
            changed = True

    known = {_path_key(entry["path"]) for entry in kept}
    fresh: list[tuple[float, str, str]] = []
    for directory in dirs:
        if not directory:
            continue
        try:
            iter_entries = list(Path(directory).iterdir())
        except OSError:
            continue
        for path in iter_entries:
            try:
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
            except OSError:
                continue
            if _path_key(path) in known:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            fresh.append((mtime, path.name, str(path)))
    if fresh:
        changed = True
        now = time.time()
        for mtime, _name, path_str in fresh:
            kept.append(
                {"seq": 0, "path": path_str, "added": mtime or now}
            )
    registry["entries"] = kept
    if _compact_registry(registry):
        changed = True
    return changed


def _compact_registry(registry: dict) -> bool:
    """按添加时间紧凑重排序号为 1..N。返回序号是否发生变化。"""
    entries = sorted(
        registry.get("entries", []),
        key=lambda e: (float(e.get("added") or 0.0), int(e.get("seq") or 0)),
    )
    changed = False
    for index, entry in enumerate(entries, start=1):
        if int(entry.get("seq") or 0) != index:
            changed = True
            entry["seq"] = index
    registry["entries"] = entries
    registry["next_seq"] = len(entries) + 1
    return changed


def register_file(registry: dict, path: Path) -> int:
    """给文件分配序号（已登记则返回原序号），返回序号。

    新登记的条目序号取末位占位；调用方随后经 sync_registry 紧凑重排。
    """
    key = _path_key(path)
    for entry in registry.get("entries", []):
        if _path_key(entry.get("path", "")) == key:
            return int(entry["seq"])
    seq = int(registry.get("next_seq") or (len(registry.get("entries", [])) + 1))
    registry["next_seq"] = seq + 1
    registry.setdefault("entries", []).append(
        {"seq": seq, "path": str(path), "added": time.time()}
    )
    return seq


def find_entry_by_seq(registry: dict, seq: int) -> dict | None:
    for entry in registry.get("entries", []):
        if int(entry.get("seq") or 0) == seq:
            return entry
    return None


def find_entry_by_path(registry: dict, path: Path) -> dict | None:
    key = _path_key(path)
    for entry in registry.get("entries", []):
        if _path_key(entry.get("path", "")) == key:
            return entry
    return None


def remove_entry_by_seq(registry: dict, seq: int) -> dict | None:
    """移除指定序号的条目并返回（调用方随后经 sync_registry 紧凑重排）。"""
    entries = registry.get("entries", [])
    for index, entry in enumerate(entries):
        if int(entry.get("seq") or 0) == seq:
            return entries.pop(index)
    return None


# ==================== 列表缩略图压缩 ====================
#
# 宿主对单帧消息有 16MB 上限：/msa list 每组最多 20 张，压缩到单张 500KB
# （20 × 500KB ≈ 10MB，base64 后约 13.4MB < 16MB）。

THUMB_MAX_BYTES = 500 * 1024
THUMB_MAX_SIDE = 720

# 降质量迭代序列；仍超限则把边长减半再来一轮，直到下限边长。
_THUMB_QUALITIES = (90, 82, 74, 66, 58, 50, 42, 35)
_THUMB_MIN_SIDE = 160


def compress_jpeg(
    data: bytes, max_bytes: int = THUMB_MAX_BYTES, max_side: int = THUMB_MAX_SIDE
) -> bytes:
    """把图片字节压成 ≤ max_bytes 的 JPEG（等比缩小 + 逐级降质量）。

    用于列表预览：不裁 1:1（保留原始比例），动图取第一帧。
    压到最小边长仍超限时返回最后一次结果（尽力而为）。
    """
    with Image.open(io.BytesIO(data)) as img:
        check_image_pixels(img)
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = Image.new("RGBA", img.size, (255, 255, 255, 255))
            background.alpha_composite(img)
            img = background.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        while True:
            width, height = img.size
            longest = max(width, height)
            work = img
            if longest > max_side:
                scale = max_side / longest
                work = img.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.LANCZOS,
                )
            output = b""
            for quality in _THUMB_QUALITIES:
                buffer = io.BytesIO()
                work.save(buffer, format="JPEG", quality=quality)
                output = buffer.getvalue()
                if len(output) <= max_bytes:
                    return output
            if max_side <= _THUMB_MIN_SIDE:
                return output
            max_side = max(_THUMB_MIN_SIDE, max_side // 2)
