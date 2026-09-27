"""麦麦要换头像（cateye_mai_set_avatar）— MaiBot 插件入口。

功能：
- 每天在配置的触发时间（默认 08:30）叠加随机漂移后，从头像库随机抽取一张
  更换 bot 的 QQ 头像（经适配器的 set_qq_avatar 接口）。
- /msa add [名称]：引用一条含图片的消息，把图片加入头像库。
- /msa debug random：强制立即随机换一次头像。
- LLM 工具 change_avatar：供 bot 主动换头像；每日上限 + 冷却，不可用时自动隐藏。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import aiohttp
from maibot_sdk import Command, MaiBotPlugin, Tool
from maibot_sdk.types import CONFIG_RELOAD_SCOPE_SELF

from .avatar_store import (
    AvatarStoreError,
    add_image_bytes,
    list_avatar_files,
    load_square_png,
)
from .config import SetAvatarPluginConfig, SUPPORTED_CONFIG_VERSION
from .schedule import (
    DEFAULT_DRIFT_SPEC,
    DEFAULT_TRIGGER_TIME,
    format_drift_minutes,
    parse_drift_spec,
    parse_trigger_time,
    roll_planned_datetime,
)

# 与 _manifest.json 的 id 一致；工具组件全名 = PLUGIN_ID.TOOL_NAME。
PLUGIN_ID = "github.cateye.mai.set_avatar"
TOOL_NAME = "change_avatar"

# 适配器提供的换头像 API：SnowLuma（合并版）与 NapCat 适配器使用同一命名空间。
# 无硬性插件依赖，运行时按「短名 → 全名候选」自动探测（两者都装时短名歧义，回退全名）。
API_SET_QQ_AVATAR_SHORT = "adapter.napcat.account.set_qq_avatar"
API_SET_QQ_AVATAR_CANDIDATES = (
    API_SET_QQ_AVATAR_SHORT,
    "maibot-team.snowluma-adapter.adapter.napcat.account.set_qq_avatar",
    "maibot-team.napcat-adapter.adapter.napcat.account.set_qq_avatar",
)

TICK_SECONDS = 30
MAX_DAILY_ATTEMPTS = 3
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
IMAGE_DIR_NAME = "image"
STATE_FILE_NAME = "state.json"

# 命令匹配：宿主用 re.search；回复场景下命令不在文本开头，
# 用 (?<!\S) 负向前瞻代替行首锚点（文档 §7.2）。
MSA_COMMAND_PATTERN = r"(?<!\S)/?msa(?:\s+(?P<sub>add|debug)(?:\s+(?P<arg>.+))?)?\s*$"

_MSA_USAGE = (
    "用法：引用一条含图片的消息发送 /msa add [名称]\n"
    "- /msa add —— 用图片哈希值命名（相同图片不会重复入库）\n"
    "- /msa add 头像1 —— 按名称保存（同名直接覆盖）\n"
    "- /msa debug random —— 强制立即随机换一次头像（不影响每日计划与工具配额）"
)
_MSA_DEBUG_USAGE = "未知的 debug 子命令，可用：/msa debug random（强制随机换一次头像）。"


def _find_image_segment(node: Any) -> dict[str, Any] | None:
    """在消息段结构中寻找第一张图片段（image / emoji），要求带二进制或链接。"""
    if isinstance(node, dict):
        if str(node.get("type") or "") in ("image", "emoji"):
            has_binary = bool(str(node.get("binary_data_base64") or "").strip())
            if has_binary or _segment_image_url(node):
                return node
        for value in node.values():
            found = _find_image_segment(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_image_segment(item)
            if found is not None:
                return found
    return None


def _find_reply_target_id(message: dict[str, Any]) -> str:
    """从消息结构中提取被回复消息的 message_id（reply 段 data.target_message_id）。"""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if str(node.get("type") or "") == "reply":
                data = node.get("data")
                if isinstance(data, dict):
                    target = str(data.get("target_message_id") or "").strip()
                    if target:
                        found.append(target)
                elif data:
                    found.append(str(data).strip())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(message)
    return found[0] if found else ""


def _segment_image_url(segment: dict[str, Any]) -> str:
    """读取图片段上的 url（段顶层或 data 内）。"""
    for source in (segment, segment.get("data")):
        if isinstance(source, dict):
            url = str(source.get("url") or "").strip()
            if url:
                return url
    return ""


class SetAvatarPlugin(MaiBotPlugin):
    """每日随机头像插件。"""

    config_model: ClassVar[type[SetAvatarPluginConfig] | None] = SetAvatarPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._tool_visible: bool | None = None  # None = 尚未确认（组件注册后默认可见）
        self._daily_attempts = 0
        self._set_avatar_api_name = ""  # 已探测到的换头像 API 名（空 = 未解析）

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        self.ctx.logger.info(
            "麦麦要换头像插件已加载（version=%s，enabled=%s，每日随机=%s，工具=%s）",
            SUPPORTED_CONFIG_VERSION,
            self.config.plugin.enabled,
            self.config.daily.enabled,
            self.config.tool.allow_bot,
        )
        try:
            self._image_dir().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.ctx.logger.warning("创建头像库目录失败：%s", exc)
        state = self._load_state()
        self._ensure_daily_plan(state)
        self._sync_scheduler()
        await self._refresh_tool_visibility()

    async def on_unload(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None
        self.ctx.logger.info("麦麦要换头像插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self.ctx.logger.info("配置已热更新（version=%s）", version)
        state = self._load_state()
        daily = state.get("daily")
        reshuffle = isinstance(daily, dict) and daily.get("date") == self._today_str() and not daily.get("result")
        self._ensure_daily_plan(state, reshuffle=reshuffle)
        self._sync_scheduler()
        await self._refresh_tool_visibility()

    # ==================== 定时调度 ====================

    def _sync_scheduler(self) -> None:
        """按插件启用状态启动/停止调度循环。"""
        should_run = bool(self.config.plugin.enabled)
        running = self._scheduler_task is not None and not self._scheduler_task.done()
        if should_run and not running:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        elif not should_run and running:
            self._scheduler_task.cancel()
            self._scheduler_task = None

    async def _scheduler_loop(self) -> None:
        self.ctx.logger.info("定时调度器已启动（每 %d 秒检查一次）", TICK_SECONDS)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("调度检查异常：%s", exc)
            try:
                await self._refresh_tool_visibility()
            except Exception as exc:
                self.ctx.logger.warning("刷新工具可见性异常：%s", exc)
            await asyncio.sleep(TICK_SECONDS)

    async def _tick(self) -> None:
        if not self.config.plugin.enabled or not self.config.daily.enabled:
            return
        state = self._load_state()
        self._ensure_daily_plan(state)
        daily = state.get("daily")
        if not isinstance(daily, dict) or daily.get("result"):
            return
        if time.time() < float(daily.get("planned_ts") or 0):
            return
        async with self._state_lock:
            # 锁内二次确认，避免与工具换头像并发打架。
            state = self._load_state()
            daily = state.get("daily") if isinstance(state.get("daily"), dict) else {}
            if daily.get("result") or time.time() < float(daily.get("planned_ts") or 0):
                return
            skip_probability = float(self.config.daily.skip_probability)
            if random.random() < skip_probability:
                daily["result"] = "skipped_random"
                self._save_state(state)
                self.ctx.logger.info("今日掷中跳过概率（%.2f），今天不换头像", skip_probability)
                return
            self._daily_attempts += 1
            ok, detail = await self._change_avatar_randomly(source="每日随机")
            if ok:
                daily["result"] = "changed"
            elif self._daily_attempts >= MAX_DAILY_ATTEMPTS:
                daily["result"] = "failed"
            self._save_state(state)

    def _ensure_daily_plan(self, state: dict[str, Any], reshuffle: bool = False) -> None:
        """确保状态里存在今天的换头像计划；配置变化时（当天未完成）可重排。"""
        daily = state.get("daily")
        if not isinstance(daily, dict):
            daily = {}
            state["daily"] = daily
        today = self._today_str()
        has_plan = daily.get("date") == today and bool(daily.get("planned_ts"))
        if has_plan and daily.get("result"):
            return  # 今天已出结果（更换/跳过/失败放弃），保持不动
        if has_plan and not reshuffle:
            return
        trigger = parse_trigger_time(self.config.daily.trigger_time)
        if trigger is None:
            self.ctx.logger.warning(
                "trigger_time 配置非法：%r（应为 HH:MM），回退 %s",
                self.config.daily.trigger_time,
                DEFAULT_TRIGGER_TIME,
            )
            trigger = parse_trigger_time(DEFAULT_TRIGGER_TIME)
        drift = parse_drift_spec(self.config.daily.drift_spec)
        if drift is None:
            self.ctx.logger.warning(
                "drift_spec 配置非法（缺少引号或中间的 - ）：%r，回退 %s",
                self.config.daily.drift_spec,
                DEFAULT_DRIFT_SPEC,
            )
            drift = parse_drift_spec(DEFAULT_DRIFT_SPEC)
        planned, drift_minutes = roll_planned_datetime(trigger, drift)
        daily["date"] = today
        daily["planned_ts"] = planned.timestamp()
        daily["result"] = None
        self._daily_attempts = 0
        self._save_state(state)
        self.ctx.logger.info(
            "今日换头像计划时间：%s（触发 %s，漂移 %s）",
            planned.strftime("%Y-%m-%d %H:%M:%S"),
            trigger.strftime("%H:%M"),
            format_drift_minutes(drift_minutes),
        )

    # ==================== 更换头像 ====================

    async def _change_avatar_randomly(self, source: str) -> tuple[bool, str]:
        """从头像库随机抽一张，处理后经适配器设置为 QQ 头像。"""
        files = list_avatar_files(self._image_dir(), self._extra_dir())
        if not files:
            self.ctx.logger.warning("头像库为空（数据目录 image 子目录与额外路径均无图片），无法更换头像")
            return False, "头像库为空"
        path = random.choice(files)
        square_mode = str(self.config.core.square_mode)
        try:
            png_bytes = await asyncio.to_thread(load_square_png, path, square_mode)
            await self._set_avatar_bytes(png_bytes)
        except Exception as exc:
            self.ctx.logger.error("[%s] 更换头像失败（%s）：%s", source, path.name, exc)
            return False, str(exc)
        self.ctx.logger.info(
            "[%s] 已从 %d 张头像中抽取「%s」并更换 QQ 头像", source, len(files), path.name
        )
        return True, path.stem

    async def _set_avatar_bytes(self, png_bytes: bytes) -> None:
        """探测并调用适配器的 set_qq_avatar（file 支持 base64:// 前缀内联）。"""
        api_name = await self._resolve_set_avatar_api()
        payload = "base64://" + base64.b64encode(png_bytes).decode("ascii")
        response = await self.ctx.api.call(api_name, params={"file": payload})
        if isinstance(response, dict) and response.get("success") is False:
            # 适配器可能已被更换，清掉缓存下次重新探测。
            self._set_avatar_api_name = ""
            raise RuntimeError(str(response.get("error") or "适配器返回失败"))

    async def _resolve_set_avatar_api(self) -> str:
        """解析当前环境可用的 set_qq_avatar API 名（成功结果缓存）。

        短名在恰好安装一个适配器时命中；两个适配器并存导致短名歧义
        （或短名未注册）时，按全名候选依次探测。
        """
        if self._set_avatar_api_name:
            return self._set_avatar_api_name
        problems: list[str] = []
        for name in API_SET_QQ_AVATAR_CANDIDATES:
            try:
                meta = await self.ctx.api.get(name)
            except Exception as exc:
                meta = None
                problems.append(f"{name}: 探测异常 {exc}")
            if meta:
                self._set_avatar_api_name = name
                self.ctx.logger.info("换头像接口已解析：%s", name)
                return name
            problems.append(f"{name}: 未注册")
        raise RuntimeError(
            "未找到可用的换头像接口，请安装 maibot-team.snowluma-adapter 或 "
            f"maibot-team.napcat-adapter 适配器之一（{'；'.join(problems)}）"
        )

    # ==================== 工具（LLM） ====================

    def _tool_unavailable_reason(self) -> str:
        """工具当前不可用的原因；可用时返回空串。"""
        tool = self.config.tool
        if not tool.allow_bot:
            return "功能已在配置中关闭"
        state = self._load_state()
        tool_state = state.get("tool")
        tool_state = tool_state if isinstance(tool_state, dict) else {}
        today = self._today_str()
        count = int(tool_state.get("count") or 0) if tool_state.get("date") == today else 0
        if count >= int(tool.daily_limit):
            return f"今日次数已用完（{count}/{tool.daily_limit}）"
        cooldown_seconds = float(tool.cooldown_hours) * 3600.0
        last_ts = float(tool_state.get("last_ts") or 0.0)
        if cooldown_seconds > 0 and last_ts > 0:
            remaining = cooldown_seconds - (time.time() - last_ts)
            if remaining > 0:
                return f"冷却中，约 {int(remaining // 60) + 1} 分钟后可再次调用"
        return ""

    async def _refresh_tool_visibility(self) -> None:
        """按配置与配额状态启用/禁用工具组件（不可用即不暴露给模型）。"""
        if self._tool_visible is None:
            self._tool_visible = True
        should_show = bool(self.config.plugin.enabled) and not self._tool_unavailable_reason()
        if should_show == self._tool_visible:
            return
        full_name = f"{PLUGIN_ID}.{TOOL_NAME}"
        try:
            if should_show:
                await self.ctx.component.enable_component(full_name, "tool", scope="global")
            else:
                await self.ctx.component.disable_component(full_name, "tool", scope="global")
        except Exception as exc:
            self.ctx.logger.warning("切换工具可见性失败（目标：%s）：%s", "显示" if should_show else "隐藏", exc)
            return
        self._tool_visible = should_show
        self.ctx.logger.info("换头像工具已%s", "暴露" if should_show else "隐藏")

    @Tool(
        TOOL_NAME,
        brief_description="更换 bot 的 QQ 头像（从头像库随机抽一张）",
        detailed_description=(
            "从头像库中随机抽取一张图片，处理后设置为 bot 自己的 QQ 头像。\n"
            "仅当用户明确要求「换头像 / 更换头像 / 随机换个头像」等时才调用；"
            "不要在其他话题下调用。本工具无需参数，也无法指定具体图片或图片名。"
        ),
    )
    async def tool_change_avatar(self, **kwargs: Any) -> dict[str, Any]:
        reason = self._tool_unavailable_reason()
        if reason:
            return {"success": False, "content": f"当前无法更换头像：{reason}"}
        async with self._state_lock:
            reason = self._tool_unavailable_reason()
            if reason:
                return {"success": False, "content": f"当前无法更换头像：{reason}"}
            ok, detail = await self._change_avatar_randomly(source="工具")
            if not ok:
                return {"success": False, "content": f"换头像失败：{detail}"}
            state = self._load_state()
            tool_state = state.get("tool")
            tool_state = tool_state if isinstance(tool_state, dict) else {}
            today = self._today_str()
            count = int(tool_state.get("count") or 0) if tool_state.get("date") == today else 0
            state["tool"] = {"date": today, "count": count + 1, "last_ts": time.time()}
            self._save_state(state)
        await self._refresh_tool_visibility()
        return {"success": True, "content": f"已从头像库随机抽取并换上新头像「{detail}」。"}

    # ==================== 命令 ====================

    @Command("msa", description="头像库命令：/msa add [名称] 添加头像；/msa debug random 强制换头像", pattern=MSA_COMMAND_PATTERN)
    async def cmd_msa(self, **kwargs: Any) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        groups = kwargs.get("matched_groups")
        groups = groups if isinstance(groups, dict) else {}
        sub = str(groups.get("sub") or "").strip().lower()
        arg = str(groups.get("arg") or "").strip()

        if sub == "debug":
            debug_cmd = arg.split()[0].lower() if arg else ""
            if debug_cmd != "random":
                await self.ctx.send.text(_MSA_DEBUG_USAGE, stream_id)
                return True, "msa debug usage", True
            ok, detail = await self._change_avatar_randomly(source="调试")
            if ok:
                await self.ctx.send.text(f"已强制随机更换头像：「{detail}」。", stream_id)
                return True, "debug random ok", True
            await self.ctx.send.text(f"强制换头像失败：{detail}", stream_id)
            return False, f"debug random failed: {detail}", True

        if sub != "add":
            await self.ctx.send.text(_MSA_USAGE, stream_id)
            return True, "msa usage", True

        name = arg

        data, source_desc = await self._collect_image_bytes(kwargs)
        if data is None:
            await self.ctx.send.text(f"添加失败：{source_desc}", stream_id)
            return False, source_desc, True

        try:
            path, md5_hex, status = await asyncio.to_thread(
                add_image_bytes, data, self._image_dir(), name or None
            )
        except AvatarStoreError as exc:
            await self.ctx.send.text(f"添加失败：{exc}", stream_id)
            return False, str(exc), True

        if status == "duplicate":
            text = f"该图片已在头像库中（{path.name}，md5={md5_hex}），未重复添加。"
        elif status == "overwritten":
            text = f"已用{source_desc}的图片覆盖头像库中的「{path.stem}」。"
        elif name:
            text = f"已把{source_desc}的图片加入头像库：「{path.stem}」。"
        else:
            text = f"已把{source_desc}的图片加入头像库（哈希命名：{path.stem}）。"
        await self.ctx.send.text(text, stream_id)
        return True, "added", True

    async def _collect_image_bytes(self, kwargs: dict[str, Any]) -> tuple[bytes | None, str]:
        """解析命令上下文，取出图片字节。返回 (字节, 来源描述)；失败时字节为 None。"""
        message = kwargs.get("message")
        message = message if isinstance(message, dict) else {}
        own_message_id = str(message.get("message_id") or "")

        segment = _find_image_segment(message.get("raw_message"))
        source_desc = "本条消息"
        fetched_remote = False
        if segment is None:
            target_id = _find_reply_target_id(message)
            if not target_id:
                return None, "没有引用图片（请引用一条含图片的消息发送 /msa add，或在本条消息中直接带图）"
            remote = await self._fetch_message_with_binary(target_id)
            fetched_remote = True
            if remote is None:
                return None, f"无法读取被引用的消息（id={target_id}）"
            segment = _find_image_segment(remote.get("raw_message"))
            source_desc = "引用消息"
            if segment is None:
                return None, "引用的消息里没有图片"

        assert segment is not None
        binary = str(segment.get("binary_data_base64") or "").strip()
        url = _segment_image_url(segment)
        if not binary and not url and not fetched_remote and own_message_id:
            # 命令上下文里的消息不含二进制：回读完整消息再取一次。
            own_full = await self._fetch_message_with_binary(own_message_id)
            if own_full is not None:
                segment = _find_image_segment(own_full.get("raw_message")) or segment
                binary = str(segment.get("binary_data_base64") or "").strip()
                url = _segment_image_url(segment)

        if binary:
            try:
                return base64.b64decode(re.sub(r"\s+", "", binary)), source_desc
            except Exception as exc:
                return None, f"图片数据解码失败：{exc}"
        if url:
            try:
                return await self._download_bytes(url), source_desc
            except Exception as exc:
                return None, f"图片下载失败：{exc}"
        return None, "消息中的图片没有可用的二进制数据或链接"

    async def _fetch_message_with_binary(self, message_id: str) -> dict[str, Any] | None:
        """按 ID 读取消息（含二进制数据）。"""
        try:
            msg = await self.ctx.message.get_by_id(message_id, include_binary_data=True)
        except Exception as exc:
            self.ctx.logger.warning("message.get_by_id(%s) 失败：%s", message_id, exc)
            return None
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        return msg if isinstance(msg, dict) and msg else None

    async def _download_bytes(self, url: str) -> bytes:
        """下载图片链接（带大小上限与超时）。"""
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": "MaiBot-SetAvatar/0.1"}
        ) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                data = await resp.read()
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"图片超过 {MAX_DOWNLOAD_BYTES // (1024 * 1024)}MB 限制")
        return data

    # ==================== 路径与状态 ====================

    def _today_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _image_dir(self) -> Path:
        """头像库目录：插件数据文件夹下的 image 子文件夹。"""
        return Path(self.ctx.paths.data_dir) / IMAGE_DIR_NAME

    def _extra_dir(self) -> Path | None:
        """额外读取路径（绝对路径；正反斜杠均由 pathlib 识别）。"""
        raw = str(self.config.core.extra_dir or "").strip().strip('"').strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            self.ctx.logger.warning("额外读取路径不是绝对路径，已忽略：%r", raw)
            return None
        return path

    def _state_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / STATE_FILE_NAME

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.ctx.logger.warning("读取状态文件失败：%s", exc)
        return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            self.ctx.logger.warning("写入状态文件失败：%s", exc)


def create_plugin() -> SetAvatarPlugin:
    """Runner 加载入口。"""
    return SetAvatarPlugin()
