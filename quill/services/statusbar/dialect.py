# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""状态栏平台方言能力档案（M4.3）。

为什么需要这一层：宿主适配器的出站能力不是「平台名 → 是否 Markdown」的二元表。
QQ 官方有 msg_type=2 的 Markdown 分支，也有 use_markdown(False) 的纯文本分支；
Telegram 会把 Markdown 转成 MarkdownV2，失败再回退纯文本；Slack 用 mrkdwn，
粗体是单个星号而不是 CommonMark 的双星号；KOOK 默认把 Plain 转 KMARKDOWN。
模板选型与末端 Markdown 清理必须消费同一份档案，否则会出现「状态栏按 Markdown
渲染、工具消息却被剥掉标记」的分裂行为。

本模块只做纯逻辑：不 import astrbot、不读配置、不碰网络。证据来自
AstrBot 4.29.3 源码观察（见 docs/QuillPlus_开发规划_5.3.4及以后.md 的 M4.3 表），
不是所有客户端的真机认证，所以每份档案都带 evidence / confidence；
未知适配器按保守 plain 处理，用户可用 dialect_overrides 显式覆盖。
"""

from __future__ import annotations

import re

NL = chr(10)
TICK = chr(96)
BS = chr(92)
ZWSP = chr(0x200B)

#: 初版支持的方言。名字同时作为配置值（dialect_overrides=platform=dialect）。
DIALECTS = ("plain", "markdown", "telegram", "slack", "kmarkdown")

#: 平台名归一：宿主里同一适配器可能以别名出现（旧脚本/人工配置常见 tg、onebot）。
_PLATFORM_ALIASES = {
    "tg": "telegram",
    "telegram": "telegram",
    "onebot": "aiocqhttp",
    "aiocqhttp": "aiocqhttp",
    "qq_official_webhook": "qq_official_webhook",
}

#: 配置里可写的方言别名。只接受能明确落地的名字，拼错时宁可忽略也不猜。
_DIALECT_ALIASES = {
    "plain": "plain",
    "text": "plain",
    "plaintext": "plain",
    "markdown": "markdown",
    "md": "markdown",
    "commonmark": "markdown",
    "telegram": "telegram",
    "markdownv2": "telegram",
    "mdv2": "telegram",
    "slack": "slack",
    "mrkdwn": "slack",
    "kmarkdown": "kmarkdown",
    "kook": "kmarkdown",
    "kmd": "kmarkdown",
}

#: 方言内置模板。plain / markdown 继续用用户配置的两份模板（兼容旧设置）；
#: 其余方言的语法差异太大，内置一份可信模板，避免把 CommonMark 原样发出去。
_BUILTIN_TEMPLATES = {
    "telegram": "*状态栏*" + NL + TICK * 3 + NL + "{content}" + NL + TICK * 3,
    "slack": "*状态栏*" + NL + TICK * 3 + NL + "{content}" + NL + TICK * 3,
    "kmarkdown": "**状态栏**" + NL + TICK * 3 + NL + "{content}" + NL + TICK * 3,
}


def _profile(
    dialect: str,
    *,
    bold: str,
    fence: bool,
    table: bool,
    max_chars: int,
    streaming: str,
    escape: str,
    evidence: str,
    confidence: float,
    conditional: bool = False,
) -> dict:
    """构造一份能力档案（调用方只读，不要原地改）。"""
    return {
        "dialect": dialect,
        "bold": bold,
        "fence": fence,
        "table": table,
        "max_chars": max_chars,
        "streaming": streaming,
        "escape": escape,
        "evidence": evidence,
        "confidence": confidence,
        "conditional": conditional,
    }


#: 适配器 → 能力档案。resolve 只看键，顺序不重要。
PROFILES = {
    "aiocqhttp": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="AstrBot 4.29.3 aiocqhttp 普通出站为字符串；自定义 OneBot 实现可能不同",
        confidence=0.95,
    ),
    # QQ 官方不是「不支持 Markdown」，而是有条件：原生 msg_type=2 与
    # use_markdown(False) 文本分支并存，权限/平台配置决定实际路径。
    # 当前链路拿不到 bot 权限的可靠信号，按保守 plain；用户可覆盖。
    "qq_official": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="4.29.3 有 msg_type=2 Markdown payload 与 use_markdown(False)；取决于 bot 权限与平台配置",
        confidence=0.55, conditional=True,
    ),
    "qq_official_webhook": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="与 QQ 官方共享发送逻辑，存在默认 use_markdown 配置；私聊/群/频道差异未逐一验证",
        confidence=0.5, conditional=True,
    ),
    "telegram": _profile(
        "telegram", bold="*", fence=True, table=False, max_chars=4096,
        streaming="escaped", escape="markdownv2_code",
        evidence="telegramify_markdown 转 MarkdownV2，失败回退纯文本；流式另有路径",
        confidence=0.85,
    ),
    "discord": _profile(
        # M6 P1-11：Discord 文本消息不保证解析 Markdown 表格；与注释一致
        # 按 table=False 处理，避免表格被原样显示成竖线噪声。
        "markdown", bold="**", fence=True, table=False, max_chars=2000,
        streaming="fenced", escape="fence",
        evidence="文本发送，Discord 客户端解释其 Markdown 子集；不能假定表格/HTML",
        confidence=0.85,
    ),
    "slack": _profile(
        "slack", bold="*", fence=True, table=False, max_chars=4000,
        streaming="fenced", escape="fence",
        evidence="Plain 映射 Block Kit mrkdwn section；粗体单星号，非 CommonMark 全套",
        confidence=0.85,
    ),
    "lark": _profile(
        "plain", bold="", fence=False, table=False, max_chars=3000,
        streaming="plain", escape="none",
        evidence="普通 post 与 interactive card 是不同路径；普通 post 不保证解释 Markdown",
        confidence=0.45, conditional=True,
    ),
    "dingtalk": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="默认 sampleMarkdown，use_markdown(False) 改 sampleText；Quill 取保守分支",
        confidence=0.6, conditional=True,
    ),
    "kook": _profile(
        "kmarkdown", bold="**", fence=True, table=False, max_chars=5000,
        streaming="fenced", escape="fence",
        evidence="Plain 默认转 KMARKDOWN；卡片与普通文本各有类型",
        confidence=0.8,
    ),
    "mattermost": _profile(
        "markdown", bold="**", fence=True, table=True, max_chars=4000,
        streaming="fenced", escape="fence",
        evidence="适配器发送 post 文本，服务端/客户端具 Markdown 消息语义；宽表格不宜作默认布局",
        confidence=0.75,
    ),
    "misskey": _profile(
        "plain", bold="", fence=False, table=False, max_chars=3000,
        streaming="plain", escape="none",
        evidence="MFM 是独立标记语法，不是 CommonMark；按消息模式降级 plain",
        confidence=0.5,
    ),
    "satori": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="输出 Satori 元素字符串，最终能力取决于下游平台；不能由名字直接判 Markdown",
        confidence=0.2,
    ),
    "wecom": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="应用/客服消息 Plain 走 text；企业微信其他产品的 markdown 接口不代表此路径",
        confidence=0.9,
    ),
    "wecom_ai_bot": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="stream/text 与主动/webhook 通道并存；与 wecom 不是同一个 API，待核实",
        confidence=0.4, conditional=True,
    ),
    "weixin_oc": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="当前 Plain 出站是文本组件，未见统一 Markdown 转换",
        confidence=0.85,
    ),
    "weixin_official_account": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="公众号 text 路径；图文/网页能力不是普通消息 Markdown",
        confidence=0.95,
    ),
    "line": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="Plain 转 text 类型；Flex 是另一类消息",
        confidence=0.95,
    ),
    "webchat": _profile(
        "markdown", bold="**", fence=True, table=True, max_chars=8000,
        streaming="fenced", escape="fence",
        evidence="Dashboard 前端负责富文本显示；按所用 WebChat 前端确认具体插件/代码块能力",
        confidence=0.9,
    ),
    "vocechat": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="插件 metadata 声明支持，4.29.3 宿主未带实现，未取得出站路径证据",
        confidence=0.2,
    ),
    "matrix": _profile(
        "plain", bold="", fence=False, table=False, max_chars=2000,
        streaming="plain", escape="none",
        evidence="插件 metadata 声明支持，宿主未带实现；Matrix plain/HTML 能力不能代替适配器行为",
        confidence=0.2,
    ),
}

#: 未收录适配器的保守档案：不猜 Markdown，先按纯文本发；用户可覆盖。
UNKNOWN_PROFILE = _profile(
    "plain", bold="", fence=False, table=False, max_chars=2000,
    streaming="plain", escape="none",
    evidence="未收录的第三方适配器；按保守 plain 处理，可用 dialect_overrides 覆盖",
    confidence=0.0,
)


class DialectTemplate(str):
    """带方言标记的模板字符串。

    apply_template 需要同时知道模板文本与方言才能先转义正文；把方言挂在
    str 子类上，可以让现有 6 个 apply_template(template, content) 调用点
    不必全部改签名。模板本身仍是 str，旧代码/配置比较不受影响。
    """

    def __new__(cls, text: str, dialect: str):
        obj = super().__new__(cls, text)
        obj.dialect = dialect
        return obj


def normalize_platform(platform: str) -> str:
    """适配器名归一（去空白、小写、常见别名）。空值返回空串。"""
    raw = str(platform or "").strip().lower()
    return _PLATFORM_ALIASES.get(raw, raw)


def normalize_dialect(value: str) -> str | None:
    """配置值 → 方言名；无法识别返回 None（调用方忽略该条覆盖）。"""
    raw = str(value or "").strip().lower()
    return _DIALECT_ALIASES.get(raw)


def parse_dialect_overrides(raw) -> dict:
    """解析 platform=dialect 覆盖串（支持竖线/逗号/分号/换行分隔）。

    也接受现成的 dict（面板/接口传入）。非法项静默跳过：这是显示层配置，
    拼错一个平台名不应该让整条状态栏链路报错。
    """
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, str):
        pairs = []
        for chunk in re.split("[" + "|,;" + NL + "]+", raw):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            key, _, value = chunk.partition("=")
            pairs.append((key, value))
        items = pairs
    else:
        return {}
    out = {}
    for key, value in items:
        platform = normalize_platform(key)
        dialect = normalize_dialect(value)
        if platform and dialect:
            out[platform] = dialect
    return out


def resolve_dialect(
    platform: str,
    *,
    overrides=None,
    plain_platforms=None,
    confirmed: str | None = None,
) -> tuple:
    """按四级优先级解析平台方言，返回 (dialect, source)。

    1. 入口覆盖：dialect_overrides（含 * 通配）与旧 plain_platforms；
    2. 可确认出站模式：适配器明确告知本轮走 Markdown/纯文本（confirmed）；
    3. 适配器版本预设：PROFILES；
    4. 保守 plain：未知适配器不猜 Markdown。
    """
    canonical = normalize_platform(platform)
    ov = overrides or {}
    if canonical and canonical in ov:
        return ov[canonical], "override"
    if "*" in ov:
        return ov["*"], "override"
    plain = {normalize_platform(p) for p in (plain_platforms or ()) if p}
    if canonical and canonical in plain:
        return "plain", "legacy_plain"
    if "*" in plain:
        return "plain", "legacy_plain"
    confirmed_dialect = normalize_dialect(confirmed or "")
    if confirmed_dialect:
        return confirmed_dialect, "confirmed"
    profile = PROFILES.get(canonical)
    if profile:
        return profile["dialect"], "preset"
    return "plain", "fallback"


def describe(
    platform: str,
    *,
    overrides=None,
    plain_platforms=None,
    confirmed: str | None = None,
) -> dict:
    """返回可直接给诊断接口/面板的档案快照（不泄漏任何配置密钥）。"""
    canonical = normalize_platform(platform)
    dialect, source = resolve_dialect(
        platform,
        overrides=overrides,
        plain_platforms=plain_platforms,
        confirmed=confirmed,
    )
    profile = dict(PROFILES.get(canonical, UNKNOWN_PROFILE))
    if profile["dialect"] != dialect:
        # override / confirmed 可以把平台切到另一种方言：此时能力字段必须
        # 跟着方言走，否则诊断接口会显示「telegram 覆盖但 bold 为空」这种
        # 与真实模板相矛盾的信息。platform 的原始证据仍保留，另注明覆盖来源。
        same = next(
            (p for p in PROFILES.values() if p["dialect"] == dialect),
            UNKNOWN_PROFILE,
        )
        for key in ("bold", "fence", "table", "max_chars", "streaming", "escape", "conditional"):
            profile[key] = same[key]
        profile["evidence"] = (
            str(profile.get("evidence") or "")
            + "；当前方言被覆盖为 " + dialect + "，能力字段取自同方言档案"
        )
    profile["dialect"] = dialect
    return {
        "platform": platform,
        "canonical": canonical,
        "dialect": dialect,
        "source": source,
        "known": canonical in PROFILES,
        "profile": profile,
    }


def build_template(dialect: str, markdown_template: str, plain_template: str) -> DialectTemplate:
    """按方言挑模板；plain/markdown 沿用用户配置，其余用内置可信模板。"""
    dialect = normalize_dialect(dialect) or "plain"
    if dialect == "plain":
        return DialectTemplate(plain_template, "plain")
    if dialect == "markdown":
        return DialectTemplate(markdown_template, "markdown")
    return DialectTemplate(_BUILTIN_TEMPLATES[dialect], dialect)


def escape_content(dialect: str, text: str) -> str:
    """在套模板前转义正文，避免字段值破坏模板语法。

    转义只做「会破坏当前模板」的最小集合，不把正文改得面目全非：

    - telegram：正文落在 MarkdownV2 的代码块里，出站由宿主
      `telegramify_markdown` 统一转义；这里预转义反斜杠反而会产生可见的
      `\\`，因此与 markdown 方言一样只打断围栏。
    - markdown / slack / kmarkdown：正文在反引号围栏内，围栏只认三个连续
      反引号；把正文里的三连反引号打断成「两连 + 零宽空格 + 单反引号」，
      未闭合的流式围栏就不会提前关掉模板围栏。
    - plain：没有语法，原样返回。
    """
    if not isinstance(text, str) or not text:
        return text
    if dialect == "telegram":
        return text.replace(TICK * 3, TICK * 2 + ZWSP + TICK)
    if dialect in ("markdown", "slack", "kmarkdown"):
        return text.replace(TICK * 3, TICK * 2 + ZWSP + TICK)
    return text


def should_strip_markdown(dialect: str) -> bool:
    """工具消息是否需要在发送前剥掉 Markdown 标记。

    只有 plain 方言需要：Markdown 方言由客户端渲染，剥掉反而破坏格式；
    这正是 M4.3 要消除的「模板与末端清洗各行其是」。
    """
    return (normalize_dialect(dialect) or "plain") == "plain"
