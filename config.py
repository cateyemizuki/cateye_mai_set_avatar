"""麦麦要换头像（cateye_mai_set_avatar）插件 — 配置模型。

WebUI 元数据约定（开发文档 §5.1「配置字段的 WebUI 元数据与翻译」）：
- 每个字段带中文短标题 ``label``（缺省会回退显示英文字段名）；
- 每个字段带英文 ``i18n`` 翻译（zh-CN 文案即中文字段本身，无需重复）；
- 每个分组带 ``__ui_label__`` + ``__ui_i18n__``（分组英文 title/description）；
- ``description`` 写完整中文说明，作为界面未命中翻译时的回退文案。
"""

from __future__ import annotations

from typing import ClassVar, Dict, Literal

from maibot_sdk import Field, PluginConfigBase

# 与 _manifest.json 的 version 保持同步。
SUPPORTED_CONFIG_VERSION = "0.1.0"

# 图片不是 1 比 1 时的处理方式。
SQUARE_MODE_CENTER_CROP = "居中切割"
SQUARE_MODE_STRETCH = "拉伸"


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；WebUI 按 i18n[locale]['label'/'hint'] 取用）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置（所有插件默认应带的配置项）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Plugin", "description": "Basic plugin settings shared by all plugins."}
    }

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件",
            **_ui_i18n("Enable plugin"),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={
            "label": "配置版本",
            "hidden": True,
            "disabled": True,
            **_ui_i18n("Config version"),
        },
    )


class CoreSectionConfig(PluginConfigBase):
    """头像处理方式与头像库路径。"""

    __ui_label__ = "核心配置"
    __ui_icon__ = "tune"
    __ui_order__ = 1
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Core", "description": "Avatar square processing and library folders."}
    }

    square_mode: Literal[SQUARE_MODE_CENTER_CROP, SQUARE_MODE_STRETCH] = Field(
        default=SQUARE_MODE_CENTER_CROP,
        description="图片不是 1 比 1 时的处理方式：居中切割 / 拉伸",
        json_schema_extra={
            "label": "非 1:1 处理",
            "hint": "居中切割：裁掉多余部分保留中间；拉伸：变形铺满正方形",
            **_ui_i18n(
                "Non-square handling",
                "Center-crop or stretch pictures that are not 1:1.",
            ),
        },
    )
    extra_dir: str = Field(
        default="",
        description="额外读取路径（头像库附加目录，绝对路径，正反斜杠均可识别；留空表示不使用）",
        json_schema_extra={
            "label": "额外读取路径",
            "input_type": "text",
            "example": "C:\\Users\\Administrator\\Desktop\\image",
            "placeholder": "C:\\Users\\Administrator\\Desktop\\image",
            "hint": "必须是绝对路径，允许混用 / 与 \\；留空则只使用插件数据目录下的 image 文件夹",
            **_ui_i18n(
                "Extra avatar folder",
                "Absolute path (both / and \\ accepted); leave empty to use the plugin data image folder only.",
            ),
        },
    )


class DailySectionConfig(PluginConfigBase):
    """每日定时随机换头像的触发与概率配置。"""

    __ui_label__ = "每日随机头像"
    __ui_icon__ = "shuffle"
    __ui_order__ = 2
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Daily random avatar", "description": "Scheduled daily avatar switching."}
    }

    enabled: bool = Field(
        default=True,
        description="是否启用每日随机换头像",
        json_schema_extra={
            "label": "启用每日随机",
            **_ui_i18n("Enable daily switch"),
        },
    )
    trigger_time: str = Field(
        default="08:30",
        description="触发更换的时间（24 小时制 HH:MM，按 bot 所在机器的本地时间）",
        json_schema_extra={
            "label": "触发时间",
            "placeholder": "08:30",
            "pattern": "^([01]?\\d|2[0-3]):[0-5]\\d$",
            "hint": "24 小时制 HH:MM",
            **_ui_i18n("Trigger time", "24h HH:MM, local time of the bot host."),
        },
    )
    drift_spec: str = Field(
        default='"-45"-"30"',
        description=(
            "触发时间的随机漂移范围（分钟），格式固定为 \"a\"-\"b\"（两侧必须带英文双引号，"
            "中间用 - 连接），负数代表向前（提前）漂移；引号或中间的 - 缺失视为格式错误，"
            "将回退到默认值"
        ),
        json_schema_extra={
            "label": "随机漂移范围",
            "example": '"-45"-"30"',
            "placeholder": '"-45"-"30"',
            "hint": '格式固定 "a"-"b"（分钟）：必须带英文双引号与中间的 -，负数提前；格式错误回退默认',
            **_ui_i18n(
                "Drift range (minutes)",
                'Fixed format "a"-"b" with quotes and the middle dash; negative means earlier. Invalid values fall back to the default.',
            ),
        },
    )
    skip_probability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="跳过今天换头像的概率（0~1，每天到达触发时间时掷一次）",
        json_schema_extra={
            "label": "跳过概率",
            "step": 0.05,
            "hint": "0~1；例如 0.5 表示约一半的天数当天不换",
            **_ui_i18n(
                "Skip probability",
                "0~1; e.g. 0.5 skips today's switch about half of the days.",
            ),
        },
    )


class ToolSectionConfig(PluginConfigBase):
    """bot 主动换头像的 LLM 工具配额与开关。"""

    __ui_label__ = "工具选项"
    __ui_icon__ = "handyman"
    __ui_order__ = 3
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Tool options", "description": "LLM tool quota for switching avatar on request."}
    }

    allow_bot: bool = Field(
        default=True,
        description="是否允许 bot 调用工具换头像（关闭时工具直接隐藏，模型不可见）",
        json_schema_extra={
            "label": "允许 bot 换头像",
            "hint": "关闭后工具不暴露，bot 无法调用",
            **_ui_i18n(
                "Allow bot to switch avatar",
                "When off, the tool is hidden from the model entirely.",
            ),
        },
    )
    daily_limit: int = Field(
        default=2,
        ge=0,
        description="工具每天调用上限（次，0 表示当天不允许调用）",
        json_schema_extra={
            "label": "每日调用上限",
            "hint": "达到上限当天工具自动隐藏；0 表示不允许调用",
            **_ui_i18n(
                "Daily call limit",
                "Max tool calls per day; the tool hides once reached. 0 disables tool calls.",
            ),
        },
    )
    cooldown_hours: float = Field(
        default=5.0,
        ge=0.0,
        description="工具调用冷却时长（小时），冷却未过时工具自动隐藏",
        json_schema_extra={
            "label": "冷却时长",
            "step": 0.5,
            "hint": "单位小时；0 表示不冷却",
            **_ui_i18n(
                "Cooldown (hours)",
                "Hours between tool calls; the tool hides during cooldown. 0 disables cooldown.",
            ),
        },
    )


class SetAvatarPluginConfig(PluginConfigBase):
    """麦麦要换头像插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    core: CoreSectionConfig = Field(default_factory=CoreSectionConfig)
    daily: DailySectionConfig = Field(default_factory=DailySectionConfig)
    tool: ToolSectionConfig = Field(default_factory=ToolSectionConfig)
