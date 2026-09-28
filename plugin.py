"""麦麦要换头像（cateye_mai_set_avatar）— MaiBot 插件入口。

功能：
- 每天在配置的触发时间（默认 08:30）叠加随机漂移后，从头像库随机抽取一张
  更换 bot 的 QQ 头像（经适配器的 set_qq_avatar 接口）。
- 插件首次运行时自动捕获 bot 当前头像入库（失败则每次启动重试直到成功）。
- /msa bot：捕获 bot 当前头像入库（**管理员**）。
- /msa list [组号]：按添加时间序号合并转发查看头像（每组 20 张，单张压缩到
  500KB 以内；缩略图缓存 5 小时，每次重启清空）（**管理员**）。
- /msa remove 序号：移除指定序号的头像（**管理员**）。
- /msa add [名称]：引用一条含图片的消息，把图片加入头像库（**管理员**）。
- /msa debug random：强制立即随机换一次头像（**管理员**）。
- 管理员 = 宿主操作员名单（plugin.permission / command_permissions，本地控制台
  天然放行）∪ 插件配置 core.admins，指向同一人自动去重（文档 §7.2 方式二）。
- 更换头像后按配置向 planner / replyer 注入提醒（「你在 …… 更换了一次头像」），
  保留时长可配置（默认 24 小时），持久化存储，超时自动失效。
- LLM 工具 change_avatar：供 bot 主动换头像；每日上限 + 冷却，不可用时自动隐藏。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import random
import re
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import aiohttp
import yarl
from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import (
    CONFIG_RELOAD_SCOPE_SELF,
    ErrorPolicy,
    HookMode,
    HookOrder,
)

from .avatar_store import (
    AvatarStoreError,
    add_image_bytes,
    compress_jpeg,
    find_entry_by_path,
    find_entry_by_seq,
    list_avatar_files,
    load_registry,
    load_square_png,
    remove_entry_by_seq,
    save_registry,
    sync_registry,
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
# get_login_info：取 bot 自己的 QQ 号，用于拼接 QQ 头像下载地址（文档 §13-11：
# 不要反查历史消息取机器人信息，用适配器 API 查询并缓存）。
API_GET_LOGIN_INFO_CANDIDATES = (
    "adapter.napcat.system.get_login_info",
    "maibot-team.snowluma-adapter.adapter.napcat.system.get_login_info",
    "maibot-team.napcat-adapter.adapter.napcat.system.get_login_info",
)

# QQ 头像下载地址（多域名兜底；s=640 为大图）。
QQ_AVATAR_URL_TEMPLATES = (
    "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640",
    "https://q0.qlogo.cn/g?b=qq&nk={qq}&s=640",
)

TICK_SECONDS = 30
MAX_DAILY_ATTEMPTS = 3
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
IMAGE_DIR_NAME = "image"
STATE_FILE_NAME = "state.json"
REGISTRY_FILE_NAME = "avatar_index.json"
THUMB_CACHE_DIR_NAME = "thumb_cache"

# 首次运行捕获 bot 头像：适配器可能比插件晚就绪，按固定节奏重试。
INITIAL_CAPTURE_MAX_ATTEMPTS = 10
INITIAL_CAPTURE_RETRY_SECONDS = 30

# /msa list：每组张数（宿主单帧 16MB 上限：20 张 × 500KB → base64 后约 13.4MB）。
LIST_PAGE_SIZE = 20
# 缩略图缓存有效期；每次启动整个缓存目录直接清空（防缓存遗留）。
THUMB_TTL_SECONDS = 5 * 3600

# 图片下载（SSRF 护栏，见开发文档 §7.1「工具参数安全」/§14 陷阱 14）：
# 只允许 http/https，且解析后的目标地址不得落在环回/私网/链路本地/保留网段
# （含云元数据地址 169.254.169.254）。重定向逐跳重新校验。
ALLOWED_URL_SCHEMES = ("http", "https")
DOWNLOAD_TIMEOUT_SECONDS = 30
MAX_DOWNLOAD_REDIRECTS = 3
DOWNLOAD_USER_AGENT = "MaiBot-SetAvatar/0.1"

# 命令匹配：宿主用 re.search；回复场景下命令不在文本开头，
# 用 (?<!\S) 负向前瞻代替行首锚点（文档 §7.2）。
MSA_COMMAND_PATTERN = r"(?<!\S)/?msa(?:\s+(?P<sub>add|debug|bot|list|remove)(?:\s+(?P<arg>.+))?)?\s*$"

_MSA_USAGE = (
    "用法（仅管理员）：\n"
    "- 引用一条含图片的消息发送 /msa add [名称] —— 把图片加入头像库\n"
    "  （/msa add 用哈希命名且按内容去重；/msa add 头像1 按名称保存，同名覆盖）\n"
    "- /msa bot —— 捕获 bot 当前头像入库\n"
    "- /msa list [组号] —— 合并转发查看头像（按添加时间序号排列，每组 20 张；"
    "省略组号 = 第 1 组，/msa list 2 从序号 21 继续）\n"
    "- /msa remove 序号 —— 移除指定序号的头像\n"
    "- /msa debug random —— 强制立即随机换一次头像（不影响每日计划与工具配额）"
)
_MSA_DEBUG_USAGE = "未知的 debug 子命令，可用：/msa debug random（强制随机换一次头像）。"

# 宿主侧管理员来源（bot_config.toml 的 global_config，点分割取嵌套键，文档 §5.3）：
# - plugin.permission：跨平台操作员 "platform:user_id" 列表
# - command_permissions.allow_users：命令放行用户列表
# 与插件配置 core.admins 合并去重后共同构成管理员名单（文档 §7.2 方式二）。
HOST_ADMIN_CONFIG_KEYS = ("plugin.permission", "command_permissions.allow_users")

# 更换提醒的时间戳键（state.json，持久化，重启保留）。
NOTICE_LAST_TS_KEY = "notice_last_ts"


def _split_admin_entry(raw: object) -> tuple[str, str]:
    """管理员条目 → (平台前缀(小写，可空), user_id)。兼容 "qq:123" 与 "123"。"""
    text = str(raw or "").strip()
    if ":" in text:
        platform, _, ident = text.partition(":")
        return platform.strip().lower(), ident.strip()
    return "", text


def build_admin_map(entries: object) -> dict[str, set[str]]:
    """把管理员名单合并去重为 user_id → 平台集合。

    平台集合中的空串 ``""`` 是哨兵，表示"任意平台"（纯 ID 条目）。指向同一人
    （user_id 相同）的多条条目（宿主名单与插件配置、或不同平台前缀）自动去重
    合并，取并集：带过纯 ID 条目后该用户在任意平台都算管理员。user_id 按宿主
    规则区分大小写，仅平台前缀转小写。
    """
    admin_map: dict[str, set[str]] = {}
    for raw in entries or ():
        platform, ident = _split_admin_entry(raw)
        if not ident:
            continue
        admin_map.setdefault(ident, set()).add(platform)
    return admin_map


def is_admin_in_map(admin_map: dict[str, set[str]], platform: str, user_id: str) -> bool:
    """判断 (platform, user_id) 是否命中管理员名单。"""
    platforms = admin_map.get(str(user_id or "").strip())
    if not platforms:
        return False
    if "" in platforms:  # 纯 ID 条目：任意平台命中
        return True
    return str(platform or "").strip().lower() in platforms


def _ip_is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


# 解析器基类按 aiohttp 版本兼容取用：顶层导出（旧版）/ aiohttp.abc（新版），
# 都没有时退化为普通类（TCPConnector 对 resolve/close 是鸭子类型调用）。
try:
    from aiohttp.abc import AbstractResolver as _AiohttpResolverBase
except ImportError:  # pragma: no cover - 取决于 aiohttp 版本
    try:
        from aiohttp.resolver import AbstractResolver as _AiohttpResolverBase
    except ImportError:
        _AiohttpResolverBase = object  # type: ignore[assignment,misc]


class _GuardedResolver(_AiohttpResolverBase):
    """SSRF 加固解析器：让地址校验与实际连接使用同一组解析结果。

    aiohttp 默认在建立连接时重新解析域名，与下载前的 getaddrinfo 校验之间存在
    TOCTOU 窗口（域名可在此间隙切到内网 IP，即 DNS rebinding）。这里把解析收口：
    只有解析出的地址全部安全时才交给连接器，任何一个内网地址都直接失败。
    基类按 aiohttp 版本兼容取用（顶层导出 / aiohttp.abc），都没有时退化为鸭子类型。
    """

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_UNSPEC
    ) -> list[Any]:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        result: list[Any] = []
        for fam, _type, _proto, canonname, sockaddr in infos:
            try:
                ip = ipaddress.ip_address(str(sockaddr[0]).split("%")[0])
            except ValueError:
                continue
            if _ip_is_forbidden(ip):
                continue
            result.append((fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, canonname, sockaddr))
        if not result:
            raise OSError(f"no safe address to connect for {host!r}")
        return result

    async def close(self) -> None:
        return None


def _is_forbidden_target(host: str) -> bool:
    """判断主机名是否指向本机/内网/元数据等不应访问的地址。"""
    raw = str(host or "").strip().strip("[]").rstrip(".").lower()
    if not raw:
        return True
    if raw in ("localhost", "localhost.localdomain") or raw.endswith(".localhost"):
        return True
    try:
        infos = socket.getaddrinfo(raw, None, proto=socket.IPPROTO_TCP)
    except OSError:
        # 解析不了的目标直接拒绝（宁可少下载，不放行不可信地址）。
        return True
    if not infos:
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            return True
        if _ip_is_forbidden(ip):
            return True
    return False


def _validate_download_url(url: str) -> str:
    """校验图片下载地址；不合法时抛 RuntimeError（异常文本不回显 HTTP 状态码）。"""
    candidate = str(url or "").strip()
    if not candidate:
        raise RuntimeError("图片链接为空")
    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise RuntimeError("图片链接格式不合法") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise RuntimeError("仅支持 http/https 图片链接")
    host = parts.hostname or ""
    if not host:
        raise RuntimeError("图片链接缺少主机名")
    if _is_forbidden_target(host):
        raise RuntimeError("图片链接指向本机或内网地址，已拒绝下载")
    return candidate


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
        self._initial_capture_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._tool_visible: bool | None = None  # None = 尚未确认（组件注册后默认可见）
        self._daily_attempts = 0
        self._set_avatar_api_name = ""  # 已探测到的换头像 API 名（空 = 未解析）
        self._login_api_name = ""  # 已探测到的 get_login_info API 名（空 = 未解析）
        self._notice_last_ts = 0.0  # 上次更换头像的时间戳（内存缓存，持久化见 state.json）

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
        self._clear_thumb_cache()  # 每次启动清空缩略图缓存，防缓存遗留
        self._sync_registry(save=True)
        state = self._load_state()
        self._notice_last_ts = float(state.get(NOTICE_LAST_TS_KEY) or 0.0)
        self._ensure_daily_plan(state)
        self._sync_scheduler()
        await self._refresh_tool_visibility()
        if not bool(state.get("initial_avatar_captured")):
            if bool(self.config.plugin.enabled):
                self._initial_capture_task = asyncio.create_task(self._initial_capture_loop())
            else:
                self.ctx.logger.info(
                    "插件未启用，跳过首次运行头像捕获（启用后重启会再次自动尝试）"
                )

    async def on_unload(self) -> None:
        for task in (self._scheduler_task, self._initial_capture_task):
            if task:
                task.cancel()
        self._scheduler_task = None
        self._initial_capture_task = None
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
            # _change_avatar_randomly 内部会写入提醒时间戳（_record_change_notice），
            # 必须重新加载状态再写 result，否则旧副本会把时间戳覆盖回滚。
            state = self._load_state()
            daily = state.get("daily") if isinstance(state.get("daily"), dict) else {}
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
        self._record_change_notice()
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

    async def _probe_api(self, candidates: tuple[str, ...], what: str) -> str:
        """按候选顺序探测已注册的适配器 API 名，找不到时抛 RuntimeError。"""
        problems: list[str] = []
        for name in candidates:
            try:
                meta = await self.ctx.api.get(name)
            except Exception as exc:
                meta = None
                problems.append(f"{name}: 探测异常 {exc}")
            if meta:
                return name
            problems.append(f"{name}: 未注册")
        raise RuntimeError(
            f"未找到可用的{what}接口，请安装 maibot-team.snowluma-adapter 或 "
            f"maibot-team.napcat-adapter 适配器之一（{'；'.join(problems)}）"
        )

    async def _resolve_set_avatar_api(self) -> str:
        """解析当前环境可用的 set_qq_avatar API 名（成功结果缓存）。

        短名在恰好安装一个适配器时命中；两个适配器并存导致短名歧义
        （或短名未注册）时，按全名候选依次探测。
        """
        if self._set_avatar_api_name:
            return self._set_avatar_api_name
        name = await self._probe_api(API_SET_QQ_AVATAR_CANDIDATES, "换头像")
        self._set_avatar_api_name = name
        self.ctx.logger.info("换头像接口已解析：%s", name)
        return name

    async def _resolve_login_api(self) -> str:
        """解析当前环境可用的 get_login_info API 名（成功结果缓存）。"""
        if self._login_api_name:
            return self._login_api_name
        name = await self._probe_api(API_GET_LOGIN_INFO_CANDIDATES, "获取登录信息")
        self._login_api_name = name
        self.ctx.logger.info("登录信息接口已解析：%s", name)
        return name

    # ==================== 捕获 bot 头像 ====================

    async def _initial_capture_loop(self) -> None:
        """首次运行自动捕获 bot 当前头像入库（适配器晚就绪时按节奏重试）。"""
        for attempt in range(1, INITIAL_CAPTURE_MAX_ATTEMPTS + 1):
            try:
                ok, detail = await self._capture_bot_avatar(source="首次运行")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                ok, detail = False, str(exc)
            if ok:
                state = self._load_state()
                state["initial_avatar_captured"] = True
                self._save_state(state)
                self.ctx.logger.info("首次运行头像捕获完成：%s", detail)
                return
            self.ctx.logger.warning(
                "首次运行头像捕获失败（第 %d/%d 次）：%s",
                attempt,
                INITIAL_CAPTURE_MAX_ATTEMPTS,
                detail,
            )
            await asyncio.sleep(INITIAL_CAPTURE_RETRY_SECONDS)
        self.ctx.logger.warning(
            "首次运行头像捕获在 %d 次尝试后放弃；重启插件后可再次自动尝试，"
            "或用 /msa bot 手动捕获",
            INITIAL_CAPTURE_MAX_ATTEMPTS,
        )

    async def _capture_bot_avatar(self, source: str) -> tuple[bool, str]:
        """下载 bot 当前 QQ 头像并加入头像库（哈希命名，同图去重）。"""
        try:
            qq = await self._get_bot_qq()
            data: bytes | None = None
            last_error = "未知错误"
            for template in QQ_AVATAR_URL_TEMPLATES:
                try:
                    data = await self._download_bytes(template.format(qq=qq))
                    break
                except Exception as exc:
                    last_error = str(exc)
            if data is None:
                return False, f"下载头像失败：{last_error}"
            path, md5_hex, status = await asyncio.to_thread(
                add_image_bytes, data, self._image_dir(), None
            )
            registry = self._sync_registry(save=True)
            entry = find_entry_by_path(registry, path)
            seq = int(entry["seq"]) if entry else 0
            if status == "duplicate":
                text = f"bot 当前头像已在库中：「{path.name}」（序号 {seq}，md5={md5_hex}）"
            else:
                text = f"已捕获 bot 当前头像入库：「{path.name}」（序号 {seq}，md5={md5_hex}）"
            self.ctx.logger.info("[%s] %s", source, text)
            return True, text
        except Exception as exc:
            self.ctx.logger.error("[%s] 捕获 bot 头像失败：%s", source, exc)
            return False, str(exc)

    async def _get_bot_qq(self) -> str:
        """经适配器 get_login_info 取 bot 自己的 QQ 号。"""
        api_name = await self._resolve_login_api()
        response = await self.ctx.api.call(api_name)
        if isinstance(response, dict) and response.get("success") is False:
            self._login_api_name = ""
            raise RuntimeError(str(response.get("error") or "适配器返回失败"))
        payload = response
        if isinstance(response, dict) and isinstance(response.get("data"), dict):
            payload = response["data"]
        qq = str((payload or {}).get("user_id") or "").strip()
        if not qq:
            raise RuntimeError("get_login_info 未返回 user_id，无法定位 bot QQ 号")
        return qq

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
        brief_description="更换你自己的QQ头像",
        detailed_description=(
            "从头像库中随机抽取一张图片，处理后设置为你的 QQ 头像。\n"
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

    @Command(
        "msa",
        description=(
            "头像库命令（管理员）：/msa bot 捕获当前头像入库；/msa list [组号] 查看头像；"
            "/msa remove 序号 移除头像；/msa add [名称] 添加头像；/msa debug random 强制换头像"
        ),
        pattern=MSA_COMMAND_PATTERN,
        # 鉴权走插件自管（文档 §7.2 方式二）：管理员 = 宿主操作员名单
        # （plugin.permission / command_permissions.allow_users）∪ 配置 core.admins，
        # 指向同一人自动去重；本地控制台（is_local_operator）天然放行。
    )
    async def cmd_msa(self, **kwargs: Any) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        if not await self._is_admin(kwargs):
            await self.ctx.send.text("权限不足：/msa 仅管理员可用。", stream_id)
            return False, "msa permission denied", True
        groups = kwargs.get("matched_groups")
        groups = groups if isinstance(groups, dict) else {}
        sub = str(groups.get("sub") or "").strip().lower()
        arg = str(groups.get("arg") or "").strip()

        if sub == "debug":
            debug_cmd = arg.split()[0].lower() if arg else ""
            if debug_cmd != "random":
                await self.ctx.send.text(_MSA_DEBUG_USAGE, stream_id)
                return True, "msa debug usage", True
            # 与每日/工具路径共用 _state_lock：防止并发读写 state.json 互相丢更新。
            async with self._state_lock:
                ok, detail = await self._change_avatar_randomly(source="调试")
            if ok:
                await self.ctx.send.text(f"已强制随机更换头像：「{detail}」。", stream_id)
                return True, "debug random ok", True
            await self.ctx.send.text(f"强制换头像失败：{detail}", stream_id)
            return False, f"debug random failed: {detail}", True

        if sub == "bot":
            # /msa bot 捕获当前头像入库；/msa bot <组号> 等效 /msa <组号>（查看）。
            if arg:
                if not arg.isdigit() or int(arg) < 1:
                    await self.ctx.send.text(
                        "用法：/msa bot（捕获当前头像入库）或 /msa bot 组号（查看对应组）",
                        stream_id,
                    )
                    return True, "msa bot usage", True
                return await self._handle_list(stream_id, int(arg))
            ok, detail = await self._capture_bot_avatar(source="命令")
            await self.ctx.send.text(
                detail if ok else f"捕获失败：{detail}", stream_id
            )
            return ok, ("bot captured" if ok else "bot capture failed"), True

        if sub == "list":
            page = 1
            if arg:
                if not arg.isdigit() or int(arg) < 1:
                    await self.ctx.send.text(
                        "用法：/msa list [组号]（组号为正整数，省略时为第 1 组）", stream_id
                    )
                    return True, "msa list usage", True
                page = int(arg)
            return await self._handle_list(stream_id, page)

        if sub == "remove":
            if not arg.isdigit() or int(arg) < 1:
                await self.ctx.send.text("用法：/msa remove 序号（可用 /msa list 查看序号）", stream_id)
                return True, "msa remove usage", True
            return await self._handle_remove(stream_id, int(arg))

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

        registry = self._sync_registry(save=True)
        entry = find_entry_by_path(registry, path)
        seq = int(entry["seq"]) if entry else 0

        if status == "duplicate":
            text = f"该图片已在头像库中（{path.name}，序号 {seq}，md5={md5_hex}），未重复添加。"
        elif status == "overwritten":
            text = f"已用{source_desc}的图片覆盖头像库中的「{path.stem}」（序号 {seq}）。"
        elif name:
            text = f"已把{source_desc}的图片加入头像库：「{path.stem}」（序号 {seq}）。"
        else:
            text = f"已把{source_desc}的图片加入头像库（哈希命名：{path.stem}，序号 {seq}）。"
        await self.ctx.send.text(text, stream_id)
        return True, "added", True

    # ==================== 管理员（宿主 ∪ 配置，自动去重） ====================

    async def _is_admin(self, kwargs: dict[str, Any]) -> bool:
        """命令鉴权：本地操作员 / 宿主名单 / 插件配置名单 任一命中即放行。"""
        if bool(kwargs.get("is_local_operator")):
            return True  # 本地控制台（bot_console 等）天然放行，与宿主规则一致
        admin_map = await self._load_admin_map()
        return is_admin_in_map(
            admin_map,
            str(kwargs.get("platform") or ""),
            str(kwargs.get("user_id") or ""),
        )

    async def _load_admin_map(self) -> dict[str, set[str]]:
        """读取宿主操作员名单并合并插件配置 core.admins（指向同一人自动去重）。"""
        entries: list[str] = list(self.config.core.admins or [])
        for key in HOST_ADMIN_CONFIG_KEYS:
            try:
                raw = await self.ctx.config.get(key, [])
            except Exception as exc:
                self.ctx.logger.warning("读取宿主管理员配置 %s 失败：%s", key, exc)
                continue
            if isinstance(raw, (list, tuple)):
                entries.extend(str(item) for item in raw)
            elif raw:
                entries.append(str(raw))
        return build_admin_map(entries)

    # ==================== 更换提醒注入 ====================

    def _record_change_notice(self) -> None:
        """每次成功更换头像后刷新提醒时间戳（持久化，重启保留）。"""
        self._notice_last_ts = time.time()
        state = self._load_state()
        state[NOTICE_LAST_TS_KEY] = self._notice_last_ts
        self._save_state(state)

    def _notice_text(self) -> str:
        """当前应注入的提醒文案；不在保留期内返回空串。"""
        core = self.config.core
        if not core.change_notice_enabled or self._notice_last_ts <= 0:
            return ""
        duration = float(core.change_notice_hours) * 3600.0
        if duration <= 0 or time.time() - self._notice_last_ts > duration:
            return ""  # 超出保留时长且未再次更换 → 取消注入
        changed_at = datetime.fromtimestamp(self._notice_last_ts)
        return f"你在{changed_at.strftime('%Y-%m-%d %H:%M:%S')}更换了一次头像"

    def _inject_into_system_items(self, items: Any, text: str) -> list[Any] | None:
        """在系统开头的 SystemMessageItem 之后插入提醒条目，返回新条目列表。

        宿主条目结构未在开发文档中完整公开，这里做防御式处理：仅以 system 条目
        为模板复制结构、替换内容（不借用消息条目的类型，避免注入出错误类型的
        条目）；找不到 system 条目时，在最前插入一个最小的 {"type": "system",
        "content": text}。返回 None 表示无法注入。
        """
        if not isinstance(items, list) or not items:
            return None
        insert_at = 0
        template: dict | None = None
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or item.get("item_type") or "").lower()
            if "system" in kind:
                template = item
                insert_at = index + 1
                break
        if template is not None:
            notice = dict(template)
            for key in list(notice):
                notice[key] = None
            for key in ("type", "item_type"):
                if key in template:
                    notice[key] = template[key]
            content_key = "text" if ("content" not in notice and "text" in notice) else "content"
            notice[content_key] = text
        else:
            notice = {"type": "system", "content": text}
        new_items = list(items)
        new_items.insert(insert_at, notice)
        return new_items

    @HookHandler(
        "maisaka.planner.before_request",
        name="avatar_change_notice_planner",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
        timeout_ms=0,
    )
    async def hook_planner_change_notice(self, **kwargs: Any) -> dict[str, Any] | None:
        """planner 注入：在系统开头的 SystemMessageItem 后插入更换提醒条目。"""
        if not self.config.core.change_notice_to_planner:
            return None
        text = self._notice_text()
        if not text:
            return None
        new_items = self._inject_into_system_items(kwargs.get("items"), text)
        if new_items is None:
            return None
        modified = dict(kwargs)
        modified["items"] = new_items
        return {"action": "continue", "modified_kwargs": modified}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="avatar_change_notice_replyer",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
        timeout_ms=0,
    )
    async def hook_replyer_change_notice(self, **kwargs: Any) -> dict[str, Any] | None:
        """replyer 注入：把更换提醒追加到附加提示词。"""
        if not self.config.core.change_notice_to_replyer:
            return None
        text = self._notice_text()
        if not text:
            return None
        modified = dict(kwargs)
        extra = str(kwargs.get("extra_prompt") or "").strip()
        modified["extra_prompt"] = f"{extra}\n{text}" if extra else text
        return {"action": "continue", "modified_kwargs": modified}

    # ==================== 列表 / 移除 ====================

    async def _handle_list(self, stream_id: str, page: int) -> tuple[bool, str, bool]:
        """按序号分组合并转发头像（每组 LIST_PAGE_SIZE 张，单张压缩 ≤500KB）。"""
        registry = self._sync_registry(save=True)
        entries = sorted(registry.get("entries", []), key=lambda e: int(e.get("seq") or 0))
        total = len(entries)
        if total == 0:
            await self.ctx.send.text("头像库是空的，可先用 /msa add 或 /msa bot 添加。", stream_id)
            return True, "list empty", True
        pages = (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
        if page < 1:
            page = 1
        if page > pages:
            await self.ctx.send.text(
                f"头像库共 {total} 张，只有 {pages} 组（/msa list {pages} 是最后一组）。", stream_id
            )
            return True, "list page overflow", True
        chunk = entries[(page - 1) * LIST_PAGE_SIZE : page * LIST_PAGE_SIZE]

        nodes: list[dict[str, Any]] = []
        next_hint = (
            "" if page >= pages
            else f"/msa list {page + 1} 查看下一组（序号从 {chunk[-1]['seq'] + 1} 继续）；"
        )
        header = (
            f"头像库共 {total} 张 ｜ 第 {page}/{pages} 组"
            f"（序号 {chunk[0]['seq']}–{chunk[-1]['seq']}）\n"
            "序号按添加时间紧凑排列，增删头像后会自动重排；" + next_hint
            + "/msa remove 序号 可移除对应头像"
        )
        nodes.append(
            {
                "user_id": "0",
                "nickname": "头像库",
                "segments": [{"type": "text", "content": header}],
            }
        )
        missing: list[int] = []
        for entry in chunk:
            path = Path(str(entry.get("path") or ""))
            seq = int(entry.get("seq") or 0)
            caption = f"序号 {seq} ｜ {path.name}"
            thumb = await self._thumb_for(path)
            segments: list[dict[str, str]] = [{"type": "text", "content": caption}]
            if thumb is not None:
                segments.append({"type": "image", "content": base64.b64encode(thumb).decode("ascii")})
            else:
                missing.append(seq)
            nodes.append({"user_id": "0", "nickname": "头像库", "segments": segments})

        sent = False
        try:
            sent = bool(await self.ctx.send.forward(nodes, stream_id))
        except Exception as exc:
            self.ctx.logger.warning("合并转发头像列表失败，回退纯文本：%s", exc)
        if not sent:
            lines = [f"{int(e.get('seq') or 0)}. {Path(str(e.get('path') or '')).name}" for e in chunk]
            await self.ctx.send.text(
                "头像库第 "
                f"{page}/{pages} 组（合并转发不可用，仅列名称）：\n" + "\n".join(lines),
                stream_id,
            )
            return True, "list fallback text", True
        if missing:
            await self.ctx.send.text(
                f"以下序号的缩略图生成失败，未附图：{'、'.join(str(s) for s in missing)}", stream_id
            )
        return True, "list sent", True

    async def _handle_remove(self, stream_id: str, seq: int) -> tuple[bool, str, bool]:
        registry = self._sync_registry(save=True)
        entry = find_entry_by_seq(registry, seq)
        if entry is None:
            await self.ctx.send.text(f"没有序号为 {seq} 的头像（/msa list 查看现有序号）。", stream_id)
            return True, "remove not found", True
        path = Path(str(entry.get("path") or ""))
        name = path.name
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            await self.ctx.send.text(f"移除失败：无法删除文件「{name}」", stream_id)
            self.ctx.logger.warning("移除头像 %s 失败：%s", path, exc)
            return False, "remove unlink failed", True
        remove_entry_by_seq(registry, seq)
        # 紧凑重排：删除序号 N 后，N 之后的条目前移补位（缓存按图片内容哈希
        # 命名、动态映射到新序号，无需失效）。
        sync_registry(registry, self._image_dir(), self._extra_dir())
        if not save_registry(self._registry_path(), registry):
            self.ctx.logger.warning("写入序号注册表失败，移除结果未持久化（下次读取将回退）")
        text = f"已移除头像「{name}」（原序号 {seq}），后续头像序号已自动前移。"
        await self.ctx.send.text(text, stream_id)
        return True, "removed", True

    # ==================== 缩略图缓存 ====================

    def _thumb_cache_dir(self) -> Path:
        return Path(self.ctx.paths.data_dir) / THUMB_CACHE_DIR_NAME

    def _clear_thumb_cache(self) -> None:
        """每次启动清空缩略图缓存（防缓存遗留）；缓存条目另有 5 小时 TTL。"""
        shutil.rmtree(self._thumb_cache_dir(), ignore_errors=True)

    async def _thumb_for(self, path: Path) -> bytes | None:
        """取列表用缩略图（≤500KB JPEG），带 5 小时 TTL 的磁盘缓存。"""
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            self.ctx.logger.warning("读取头像 %s 失败：%s", path.name, exc)
            return None
        digest = hashlib.md5(data).hexdigest()
        cache_path = self._thumb_cache_dir() / f"{digest}.jpg"
        try:
            if cache_path.is_file() and time.time() - cache_path.stat().st_mtime <= THUMB_TTL_SECONDS:
                return cache_path.read_bytes()
        except OSError:
            pass
        try:
            thumb = await asyncio.to_thread(compress_jpeg, data)
        except Exception as exc:
            self.ctx.logger.warning("生成缩略图失败（%s）：%s", path.name, exc)
            return None
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(thumb)
        except OSError:
            pass
        return thumb

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
        """下载图片链接（SSRF 护栏：仅 http/https + 拒绝内网/元数据地址 + 逐跳重定向校验）。

        失败原因只返回可读的中文说明，不回显 HTTP 状态码等远端细节。
        """
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
        target = _validate_download_url(url)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": DOWNLOAD_USER_AGENT},
            # 自定义解析器把 SSRF 校验与连接固定在同一组地址上（防 DNS rebinding）。
            connector=aiohttp.TCPConnector(resolver=_GuardedResolver()),
        ) as session:
            for _ in range(MAX_DOWNLOAD_REDIRECTS + 1):
                # allow_redirects=False：每一跳都重新做一次地址校验，避免被重定向到内网。
                async with session.get(target, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = str(resp.headers.get("Location") or "").strip()
                        if not location:
                            raise RuntimeError("图片下载失败：重定向缺少目标地址")
                        # Location 可能是相对路径：先按 RFC 3986 拼成绝对地址再校验。
                        try:
                            absolute = str(yarl.URL(target).join(yarl.URL(location)))
                        except (ValueError, TypeError) as exc:
                            raise RuntimeError("图片下载失败：重定向目标不合法") from exc
                        target = _validate_download_url(absolute)
                        continue
                    if resp.status != 200:
                        raise RuntimeError("图片下载失败：服务器未返回图片内容")
                    # 流式限量读取：进入内存的内容最多 MAX+1 字节，再做大小校验，
                    # 防止超大响应在 30s 超时窗口内吃满内存。
                    data = await resp.content.read(MAX_DOWNLOAD_BYTES + 1)
                    break
            else:
                raise RuntimeError("图片下载失败：重定向次数过多")
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

    def _registry_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / REGISTRY_FILE_NAME

    def _sync_registry(self, save: bool = False) -> dict:
        """读取注册表并与头像库目录对账（补登记新文件、清掉已消失的文件）。"""
        registry = load_registry(self._registry_path())
        changed = sync_registry(registry, self._image_dir(), self._extra_dir())
        if changed or save:
            if not save_registry(self._registry_path(), registry):
                self.ctx.logger.warning("写入序号注册表失败，序号变更未持久化（下次读取将回退）")
        return registry

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
