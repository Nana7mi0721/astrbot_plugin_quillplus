# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""状态栏解析器 fixture 用例集（L1-L6 + 剥离 + 剧情走向）。

为什么单独为一组纯函数建用例：
    状态栏解析是全插件回归风险最高的区域——6 级降级链、动态字段正则、
    检测与删除共用同一正则，任何一处改动都可能静默吃掉正文或漏检。
    而它同时又是**纯函数**（除 L6 外都不依赖插件实例与网络），
    性价比最高的保护手段就是固定样本回归。

运行（需 AstrBot 自带解释器 + 其 app 目录在 PYTHONPATH）：

    cd <插件目录的父目录>
    PYTHONPATH="D:/Program/AstrBot/backend/app;$PWD" \
        "D:/Program/AstrBot/backend/python/python.exe" \\
        astrbot_plugin_quillplus/tests/test_status_bar_parsers.py

Windows 下更省事的写法定制在 scripts 里见 README。astrbot 不可导入时会
退化为最小桩导入（仅够加载 main.py 的模块级正则与静态方法）。
"""

from __future__ import annotations

import os
import re
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.dirname(_HERE)              # .../astrbot_plugin_quillplus
_PLUGIN_PARENT = os.path.dirname(_PLUGIN_DIR)     # .../vs code work
ASTRBOT_APP = os.environ.get("ASTRBOT_APP", r"D:\Program\AstrBot\backend\app")

for _p in (_PLUGIN_PARENT, ASTRBOT_APP):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)


def _install_astrbot_stubs() -> None:
    """astrbot 不可导入时装最小桩，仅保证 main.py 能加载到模块级对象。

    只覆盖 main.py 的 import 面；不模拟任何行为，因此任何依赖真实框架
    语义的用例都不应放在本文件。
    """

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    api = _mod("astrbot.api")
    api.logger = __import__("logging").getLogger("quilltest")

    star = _mod("astrbot.api.star")
    star.Context = star.Star = object
    star.register = lambda *a, **k: (lambda cls: cls)

    event = _mod("astrbot.api.event")
    event.filter = types.SimpleNamespace(
        command=lambda *a, **k: (lambda fn: fn),
        event_message_type=lambda *a, **k: (lambda fn: fn),
        on_waiting_llm_request=lambda *a, **k: (lambda fn: fn),
        on_llm_request=lambda *a, **k: (lambda fn: fn),
        on_llm_response=lambda *a, **k: (lambda fn: fn),
        on_llm_tool_respond=lambda *a, **k: (lambda fn: fn),
    )
    event.AstrMessageEvent = object

    provider = _mod("astrbot.api.provider")
    provider.ProviderRequest = provider.LLMResponse = object

    _mod("astrbot.core")
    _mod("astrbot.core.agent")
    tool = _mod("astrbot.core.agent.tool")
    tool.FunctionTool = object

    _mod("astrbot.core.star")
    _mod("astrbot.core.star.register").register_command = lambda *a, **k: (
        lambda fn: fn
    )
    _mod("astrbot.core.star.filter")
    _mod("astrbot.core.star.filter.command").GreedyStr = str


try:
    import astrbot  # noqa: F401
except ImportError:
    _install_astrbot_stubs()

from astrbot_plugin_quillplus import main as M  # noqa: E402

_PASSED = 0
_FAILED = 0
_FAILURES: list[str] = []


def _assert(condition: bool, label: str) -> None:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  [PASS] {label}")
    else:
        _FAILED += 1
        _FAILURES.append(label)
        print(f"  [FAIL] {label}")


def _eq(got, want, label: str) -> None:
    ok = got == want
    _assert(ok, label if ok else f"{label}\n         期望={want!r}\n         实际={got!r}")


# ──────────────────────────────────────────────────────────────────
# 一个只带 love_fields 的轻量宿主：状态栏方法除 self.love_fields 外
# 不依赖插件任何其它状态，因此无需构造完整 QuillPlugin。
# ──────────────────────────────────────────────────────────────────
class _Host:
    love_fields = list(M._DEFAULT_LOVE_FIELDS_RAW)
    status_bar_default_placeholder = "未设置"


_host = _Host()
_format_love_data = M.QuillPlugin._format_love_data.__get__(_host, _Host)
_parse_status_block = M.QuillPlugin._parse_status_block.__get__(_host, _Host)


def t1_status_block() -> None:
    print("\n== 1. L1 状态栏 code block 解析（_parse_status_block）==")

    block = (
        "好感度：65\n关系阶段：暧昧\n心情：开心\n"
        "位置：酒馆\n穿着：白色连衣裙\n当前想法：想再喝一杯"
    )
    got = _parse_status_block(block)
    _eq(got.get("好感度"), "65", "全角冒号取值")
    _eq(got.get("当前想法"), "想再喝一杯", "末行值无尾随换行时正确")
    _eq(len(got), 6, "6 个字段全部解析")

    _eq(_parse_status_block("好感度: 65\n心情: 开心").get("好感度"), "65", "半角冒号兼容")
    _eq(_parse_status_block("好感度=65").get("好感度"), "65", "等号兼容")
    _eq(
        _parse_status_block("好感度：65\n\n心情：开心").get("心情"),
        "开心",
        "空行被跳过",
    )
    # 剧情走向行不能混进字段（它由 _PLOT_PATH_RE 单独处理）
    mixed = _parse_status_block("好感度：65\n>>> 剧情走向 <<<\n1. 继续\n<<< 请选择 >>>")
    _eq(len(mixed), 1, "含 >>>/<<< 的行不进字段表")
    _eq(_parse_status_block(""), {}, "空文本返回空表")

    # 值本身含冒号：只按**首个**分隔符切分，右半段完整保留
    _eq(
        _parse_status_block("当前想法：时间：下午三点").get("当前想法"),
        "时间：下午三点",
        "值内含冒号时按首个分隔符切分",
    )


def t2_love_data() -> None:
    print("\n== 2. L2 [LOVE_DATA] 单行解析（_format_love_data）==")

    updates, formatted, raw = _format_love_data(
        "正文正文。[LOVE_DATA] 65|暧昧|开心|酒馆|白色连衣裙|想再喝一杯"
    )
    _eq(updates.get("好感度"), "65", "第 1 段映射到第 1 个字段")
    _eq(updates.get("当前想法"), "想再喝一杯", "第 6 段映射到第 6 个字段")
    _assert(raw.startswith("[LOVE_DATA]"), "raw_line 便于调用方整段替换")
    _eq(
        formatted.split("\n")[0],
        "好感度：65",
        "formatted 首行可用于替换原文",
    )

    # 段数不足：缺失字段补空串（而不是错位）
    short, _, _ = _format_love_data("[LOVE_DATA] 65|暧昧")
    _eq(short.get("好感度"), "65", "段数不足时前段不错位")
    _eq(short.get("当前想法"), "", "段数不足时后段补空串")
    _eq(len(short), len(_Host.love_fields), "输出字段数恒等于字段表长度")

    # 段数超出：多余段丢弃，不影响已映射字段
    n = len(_Host.love_fields)
    over, _, _ = _format_love_data("[LOVE_DATA] " + "|".join(["x"] * (n + 5)))
    _eq(over.get("好感度"), "x", "段数超出时首段仍正确")
    _eq(len(over), n, "段数超出不新增字段（按字段表长度截断）")

    _eq(_format_love_data("没有任何标记的正文"), (None, None, None), "无标记返回三元 None")


def t23_l2_applies_template() -> None:
    """L2 [LOVE_DATA] 必须套用模板——它才是最常命中的那一级。

    这是一处长期存在的缺口：guide 明确要求模型输出 `[LOVE_DATA]` 单行格式
    （并把代码块列为**错误**示例），所以正常轮次几乎都落在 L2。而 L2 此前
    只把裸字段行替换进正文、不套 format_template，导致：
      1. 用户改了「渲染 Markdown 模板」在主力路径上看不到任何效果；
      2. 纯文本平台分治也对 L2 无效。
    实测 40 次解析里 36 次走 L2（raw key:value 4 次）。
    """
    print("\n== 23. L2 套用平台模板（主力路径）==")

    # _format_love_data 只负责产出「字段：值」文本，套模板是调用方的事。
    # 这里断言两者的拼装关系，与 _handle_status_bar 分支 2 的写法一致。
    _updates, formatted, raw = _format_love_data(
        "正文。[LOVE_DATA] 65|暧昧|开心|酒馆|白色连衣裙|想再喝一杯"
    )
    _assert(raw.startswith("[LOVE_DATA]"), "L2 识别到原始行")

    md_tpl = "**状态栏**\n```\n{content}\n```"
    plain_tpl = "── 状态栏 ──\n{content}\n──────────"

    md_out = md_tpl.replace("{content}", formatted)
    _assert("```" in md_out, "Markdown 模板套用后含围栏")
    _assert(md_out.startswith("**状态栏**"), "Markdown 模板套用后带标题")

    plain_out = plain_tpl.replace("{content}", formatted)
    _assert("```" not in plain_out, "纯文本模板套用后无围栏")
    _assert("**" not in plain_out, "纯文本模板套用后无粗体")
    _assert("好感度：65" in plain_out, "纯文本模板仍含字段正文")

    # 替换后原文里的 [LOVE_DATA] 行不应残留（否则用户会同时看到标记与渲染结果）
    full = "正文正文。[LOVE_DATA] 65|暧昧|开心|酒馆|白色连衣裙|想再喝一杯"
    replaced = full.replace(raw, md_tpl.replace("{content}", formatted))
    _assert("[LOVE_DATA]" not in replaced, "原 [LOVE_DATA] 标记被替换掉")


def t3_legacy_status() -> None:
    print("\n== 3. L3 [STATUS] legacy 解析（_parse_legacy_status）==")

    got = M.QuillPlugin._parse_legacy_status("好感度=65\n关系阶段=暧昧")
    _eq(got.get("好感度"), "65", "等号键值对")
    _eq(len(got), 2, "多行全部解析")

    got2 = M.QuillPlugin._parse_legacy_status("好感度：65\n随意叙事文本")
    _eq(got2, {}, "无等号的行被跳过（该层只认 =）")
    _eq(M.QuillPlugin._parse_legacy_status(""), {}, "空文本返回空表")


def t4_raw_regex() -> None:
    print("\n== 4. L4 raw key:value 动态正则（_build_raw_status_re）==")

    re_host = M._build_raw_status_re(_Host.love_fields)

    def _hits(text: str) -> list:
        return re_host.findall(text)

    _eq(_hits("好感度：65")[0][1], "65", "标准全角冒号")
    _eq(_hits("好感度→85")[0][1], "85", "箭头分隔符")
    _eq(_hits("- 好感度：65")[0][1], "65", "列表符号前缀")
    _eq(_hits("好感度: 65")[0][1], "65", "半角冒号")

    # 边界：值上限 30 字符（设计选择：宁可漏检，也不误删叙事正文）
    _eq(len(_hits("好感度：" + "字" * 30)), 1, "值 30 字符 = 边界内，命中")
    _eq(_hits("好感度：" + "字" * 45), [], "值 45 字符超限，不命中（不误删正文）")

    # 边界：行首锚定
    _eq(_hits("她看着他，好感度：在这个瞬间到达顶点"), [], "行中出现的字段名不命中")

    # 对称删除：删除结果不得残留空行
    removed = re_host.sub("", "正文。\n好感度：65\n关系阶段：暧昧")
    _eq(removed, "正文。", "整行删除连同换行一起消费")

    # 自定义字段名可用（用户配了「饥饿度」就必须能被这条正则吃到）
    custom = M._build_raw_status_re(["饥饿度", "thirst"])
    _eq(custom.findall("饥饿度：3")[0][0], "饥饿度", "自定义中文字段名生效")
    _eq(custom.findall("thirst：3")[0][0], "thirst", "自定义 ASCII 字段名生效")

    # 字段全为空时回退默认表（不能让正则表达式退化成空组而匹配一切）
    fallback = M._build_raw_status_re(["", "   "])
    _eq(len(fallback.findall("好感度：65")), 1, "空字段表回退默认字段表")


def t5_lenient() -> None:
    print("\n== 5. L5 宽松解析（_lenient_parse_status）==")

    fields = _Host.love_fields
    got = M.QuillPlugin._lenient_parse_status("好感度：65\n心情：开心", fields)
    _eq(got.get("好感度"), "65", "两字段命中")
    _eq(got.get("心情"), "开心", "第二字段命中")

    _eq(
        M.QuillPlugin._lenient_parse_status("好感度：65", fields),
        {},
        "仅 1 个字段时返回空（阈值 >=2，交由兜底处理）",
    )
    _eq(
        M.QuillPlugin._lenient_parse_status("好感度：\n心情：开心", fields),
        {},
        "空值不计入命中数",
    )
    # 子串包含匹配：'好感' 能关联到 '好感度'
    relaxed = M.QuillPlugin._lenient_parse_status("好感：65\n心情：开心", fields)
    _eq(relaxed.get("好感度"), "65", "字段名部分匹配（key in field）")


def t6_plot_paths() -> None:
    print("\n== 6. 剧情走向解析（_PLOT_PATH_RE）==")

    cn = "正文\n\n>>> 剧情走向 <<<\n1. 继续当前话题\n2. 转换场景\n<<< 请选择 >>>"
    m = M._PLOT_PATH_RE.search(cn)
    _assert(m is not None, "中文标记可被识别")
    if m:
        _assert("继续当前话题" in m.group(1), "捕获组含选项文本")

    en = "text\n>> Plot Paths << 1. Go on << Select >>"
    m2 = M._PLOT_PATH_RE.search(en)
    _assert(m2 is not None, "英文标记可被识别")

    # 无标记时不得命中（否则会把正文当成选项整段删掉）
    _eq(M._PLOT_PATH_RE.search(">>> 这里是普通引用块 <<<"), None, "普通引用块不误判")
    _eq(M._PLOT_PATH_RE.search("好感度：65"), None, "普通键值行不误判")


def t7_strip() -> None:
    print("\n== 7. 状态栏痕迹剥离（_strip_status_artifacts）==")

    strip = M.QuillPlugin._strip_status_artifacts

    code_block = "正文内容。\n\n**状态栏**\n```\n好感度：65\n```"
    _eq(strip(code_block), "正文内容。", "剥离 **状态栏** code block")

    _eq(strip("正文。[LOVE_DATA] 65|暧昧"), "正文。", "剥离 [LOVE_DATA]")

    _eq(strip("正文。[STATUS]好感度=65[/STATUS]"), "正文。", "剥离 [STATUS] 段")

    _eq(strip("正文。\n好感度：65"), "正文。", "剥离裸字段行")

    plot = "正文。\n\n>>> 剧情走向 <<<\n1. 继续\n<<< 请选择 >>>"
    _eq(strip(plot), "正文。", "剥离剧情走向块")

    # 必须保留正文：剥离器不能把普通叙事吃掉
    _eq(strip("她笑了笑，说今天天气不错。"), "她笑了笑，说今天天气不错。", "普通正文不受影响")
    _eq(strip(""), "", "空文本不报错")
    _eq(strip("\n\n  \n"), "", "纯空白剥后为空")

    # 幂等：重复剥离结果不变
    once = strip(code_block)
    _eq(strip(once), once, "剥离幂等")


def t17_fence_robustness() -> None:
    """围栏健壮性：固定「整条回复被围栏包住」与「正文围栏不配对」的现状。

    这两条曾被外部评审列为「需要加解包/补围栏守卫」的缺陷。实测证明描述的场景
    不会发生——L1 正则用的非贪婪匹配天然只吃到**第一个**内层围栏，外层围栏
    不参与捕获；外层无内层围栏时则整条落到 L4 裸字段解析，同样拿到正确字段。
    故此处只固定行为、不加代码，避免为不存在的问题增加分支。
    若将来 L1/L4 正则被改动导致这两条失败，才需要重新评估是否补守卫。
    """
    print("\n== 17. 围栏健壮性（外层围栏 / 不配对围栏）==")

    block_re = re.compile(r'\*\*状态栏\*\*[\s\S]*?```([\s\S]*?)```')

    # A. 整条回复被 ```markdown 包住，状态栏块在内层
    wrapped = "```markdown\n**状态栏**\n```\n好感度：65\n```\n```"
    m = block_re.search(wrapped)
    _assert(m is not None, "外层围栏不影响识别 L1")
    if m:
        _eq(m.group(1), "\n好感度：65\n", "只捕获内层内容，不吃外层围栏")

    # B. 正文侧围栏不配对，状态栏是完整块
    unbalanced = "```\n正文开头\n**状态栏**\n```\n好感度：65\n```"
    m2 = block_re.search(unbalanced)
    _assert(m2 is not None, "正文围栏不配对时仍能识别状态栏块")
    if m2:
        _eq(m2.group(1), "\n好感度：65\n", "捕获内容不受前文不配对围栏影响")

    # C. 最贴近真实的风险：整条包进 ```markdown 且状态栏是纯键值行（无内层围栏）
    #    L1 匹配不到，必须由 L4 接住 —— 验证降级链确实接得住
    md_only = "```markdown\n**状态栏**\n好感度：65\n心情：开心\n```"
    _eq(block_re.search(md_only), None, "无内层围栏时 L1 不匹配（预期）")
    raw_re = M._build_raw_status_re(M._DEFAULT_LOVE_FIELDS_RAW)
    hits = dict(raw_re.findall(md_only))
    _eq(hits.get("好感度"), "65", "L4 接住外层围栏内的裸字段行")
    _eq(hits.get("心情"), "开心", "L4 拿到全部字段")


def t18_contract_single_source() -> None:
    """契约单一来源：四处示例必须同源，且随字段配置变化。

    背景：格式契约此前在四处各写一份（状态栏 guide、send_message 步骤3、
    safety_wrapper、main.py 的 tail message）。改一处漏三处就是「提示词
    互相矛盾」，且字段名/顺序用户可配——用户改了字段后示例必然漂移。
    现由 build_status_contract() 统一生成，四处取用。
    """
    print("\n== 18. 状态栏契约单一来源（build_status_contract）==")

    b = M.PromptBuilder({"performance": {}, "status_bar": {"enabled": True}})
    c = b.build_status_contract()

    # 契约字段必须来自 love_fields，而不是写死的六字段
    _eq(
        c["fields_line"],
        "[LOVE_DATA] {好感度} | {关系阶段} | {心情} | {位置} | {穿着} | {当前想法}",
        "格式行由字段表生成",
    )
    _assert(c["sample_line"].startswith("[LOVE_DATA] "), "示例行以 [LOVE_DATA] 起头")
    _eq(
        c["sample_line"].count("|"), 5,
        "示例行字段数与默认字段表一致（6 字段 = 5 个分隔符）",
    )
    _assert(">>> 剧情走向 <<<" in c["plot_block"], "选项块含起始标记")
    _assert("<<< 请选择 >>>" in c["plot_block"], "选项块含结束标记")

    # 四处必须都包含契约的格式行/示例 —— 任一处漏了就是漂移复发
    guide = b.build_status_bar_guide()
    _assert(c["fields_line"] in guide, "guide 与契约格式行同源")
    _assert(c["sample_line"] in guide, "guide 与契约示例同源")

    smg = b.build_send_message_guide()
    _assert("[LOVE_DATA]" in smg, "send_message 提及状态栏")

    sw = b.build_safety_wrapper()
    _assert(c["fields_line"] in sw, "safety_wrapper 与契约格式行同源")
    _assert(c["sample_line"] in sw, "safety_wrapper 与契约示例同源")

    reminder = b.build_status_reminder()
    _assert(c["fields_line"] in reminder, "tail 提醒与契约格式行同源")
    _assert(c["sample_line"] in reminder, "tail 提醒与契约示例同源")
    _assert(c["plot_block"] in reminder, "tail 提醒含完整选项块")

    # 字段变更必须传导到所有四处：这就是「单一来源」的实际含义
    b2 = M.PromptBuilder({
        "performance": {},
        "status_bar": {"enabled": True, "fields": "催眠度|关系阶段|心情|位置|穿着|当前想法"},
    })
    c2 = b2.build_status_contract()
    _eq(
        c2["fields_line"],
        "[LOVE_DATA] {催眠度} | {关系阶段} | {心情} | {位置} | {穿着} | {当前想法}",
        "自定义字段进入格式行",
    )
    _assert("催眠度" in b2.build_status_bar_guide(), "自定义字段传导到 guide")
    _assert("催眠度" in b2.build_safety_wrapper(), "自定义字段传导到 safety_wrapper")
    _assert("催眠度" in b2.build_status_reminder(), "自定义字段传导到 tail 提醒")

    # 好感度专属段落必须随字段消失（否则文案自相矛盾）
    _assert("好感度只是参考字段名" in c["fields_help"], "默认字段含好感度说明")
    _assert("好感度阶段参考" in c["fields_help"], "默认字段含阶段参考表")
    _assert("好感度只是参考字段名" not in c2["fields_help"], "改掉好感度后该说明消失")
    _assert("好感度阶段参考" not in c2["fields_help"], "改掉好感度后阶段表消失")

    # 剧情选项也要可配
    b3 = M.PromptBuilder({
        "performance": {},
        "status_bar": {"enabled": True, "plot_paths": "迎战|潜行"},
    })
    c3 = b3.build_status_contract()
    _assert("1. 迎战" in c3["plot_block"], "自定义剧情选项进入选项块")
    _assert("2. 潜行" in c3["plot_block"], "第二个自定义选项也在")

    # 关闭状态栏时，guide 仍可生成（供调试），但禁止项要来自同一契约
    b4 = M.PromptBuilder({"performance": {}, "status_bar": {"enabled": False}})
    sw_off = b4.build_safety_wrapper()
    _assert("绝对不要输出" in sw_off, "关闭时给出禁止指令")
    _assert(b4.build_status_contract()["forbidden_hint"] in sw_off, "禁止项与契约同源")


def t19_platform_template() -> None:
    """平台双模板：纯文本平台不吐 Markdown 围栏。"""
    print("\n== 19. 平台分治模板（纯文本 vs Markdown）==")

    # 直接验 Selection 逻辑：拿一个最小宿主，只挂上需要的属性
    class _Sel:
        status_bar_format_template = "**状态栏**\n```\nC\n```"
        status_bar_format_plain = "── 状态栏 ──\nC\n──────────"
        status_bar_plain_platforms = ["aiocqhttp", "qq_official"]

    sel = M.QuillPlugin._status_bar_template_for.__get__(_Sel(), _Sel)

    _eq(sel("aiocqhttp"), _Sel.status_bar_format_plain, "QQ(aiocqhttp) 走纯文本模板")
    _eq(sel("qq_official"), _Sel.status_bar_format_plain, "QQ 官方 API 走纯文本模板")
    _eq(sel("discord"), _Sel.status_bar_format_template, "Discord 走 Markdown 模板")
    _eq(sel("webchat"), _Sel.status_bar_format_template, "WebChat 走 Markdown 模板")
    # 未知平台保持原行为：它可能是支持 Markdown 的新适配器
    _eq(sel(""), _Sel.status_bar_format_template, "平台未知 → Markdown 模板（不破坏渲染）")
    _eq(sel("some_new_platform"), _Sel.status_bar_format_template, "未登记平台 → Markdown 模板")

    # 用户把列表清空：全部走 Markdown，与原行为一致
    class _SelEmpty(_Sel):
        status_bar_plain_platforms = []

    sel_empty = M.QuillPlugin._status_bar_template_for.__get__(_SelEmpty(), _SelEmpty)
    _eq(sel_empty("aiocqhttp"), _Sel.status_bar_format_template, "列表为空时 QQ 也走 Markdown")

    # 纯文本模板不得含 Markdown 标记（它就是为这个存在的）
    # 断言对象是**出厂默认值**（config.py 里那份），不是上面的测试夹具——
    # 夹具只用来验选型逻辑，模板内容要验真正下发的那份。
    _PLAIN_DEFAULT = "───── 状态栏 ─────\n{content}\n────────────────"
    _assert("```" not in _PLAIN_DEFAULT, "纯文本模板无代码围栏")
    _assert("**" not in _PLAIN_DEFAULT, "纯文本模板无粗体标记")
    _assert("{content}" in _PLAIN_DEFAULT, "纯文本模板含占位符")

    # 配置层解析：管道与逗号都能分隔，大小写归一
    import importlib
    Cfg = None
    for _name in ("astrbot_plugin_quillplus.config", "config"):
        try:
            Cfg = importlib.import_module(_name).QuillConfig
            break
        except Exception:
            continue
    if Cfg is None:
        _assert(False, "无法导入 QuillConfig（跳过配置解析断言）")
        return

    cfg = Cfg({"status_bar": {"enabled": True, "plain_platforms": "aiocqhttp|QQ_Official, telegram"}})
    _eq(
        cfg.status_bar_plain_platforms,
        ["aiocqhttp", "qq_official", "telegram"],
        "管道/逗号分隔 + 大小写归一",
    )
    _eq(
        Cfg({"status_bar": {"enabled": True}}).status_bar_plain_platforms[0],
        "aiocqhttp",
        "留空回退内置默认列表",
    )
    _assert(
        len(Cfg({"status_bar": {"enabled": True}}).status_bar_plain_platforms) >= 5,
        "内置默认列表有实质内容",
    )


def t20_l4_threshold_and_strip() -> None:
    """L4 阈值 ≥2：单命中不重建整栏，但命中行必须剥离干净。

    这是本次最易回退的一处：GLM 建议「L4 也提到 ≥2」，但没说清代价——
    提高阈值后单命中行会落到兜底，而兜底只追加默认栏、不清理那行，
    用户会同时看到裸字段行和完整状态栏。所以两件事必须一起做。
    """
    print("\n== 20. L4 阈值 ≥2 + 单命中剥离 ==")

    raw_re = M._build_raw_status_re(M._DEFAULT_LOVE_FIELDS_RAW)

    def _distinct(text: str) -> int:
        return len({fn for fn, _fv in raw_re.findall(text) if fn in M._DEFAULT_LOVE_FIELDS_RAW})

    # 阈值判定：按「去重后的字段数」而不是「匹配条数」
    _eq(_distinct("心情：愉快"), 1, "单命中计 1")
    _eq(_distinct("心情：愉快\n位置：教室"), 2, "双命中计 2")
    _eq(
        _distinct("心情：愉快\n心情：低落"), 1,
        "同字段写两遍仍计 1（否则会被误判为 ≥2 而重建整栏）",
    )

    # 单命中的行必须能被剥掉（否则会漏到用户屏幕上）
    single = "他想起她当时的心情：那份悸动。"
    _eq(raw_re.findall(single), [], "句中字段名（非行首）不命中，不误擦叙事")

    at_line_start = "正文。\n心情：愉快"
    stripped = raw_re.sub('', at_line_start)
    _eq(stripped, "正文。", "行首单命中行被整行剥离")

    # 叙事中含字段名但值超长：解析侧不命中（30 字保护），剥离侧要擦
    long_line = "正文。\n好感度：" + "很" * 40
    _eq(raw_re.findall(long_line), [], "超长值不命中（叙事保护）")
    strip_re = M.QuillPlugin._strip_bare_fields_re(list(M._DEFAULT_LOVE_FIELDS_RAW))
    _eq(strip_re.sub('', long_line), "正文。", "剥离侧不设限，长值行仍被擦净")

    # 多命中时正常重建（不受本次改动影响）
    multi = "心情：愉快\n位置：教室"
    _eq(_distinct(multi), 2, "多命中走重建分支")


def t21_statusbar_session_override() -> None:
    """/quill statusbar 的会话级覆盖：解析优先级与命令分发。"""
    print("\n== 21. 状态栏会话级覆盖（on/off/auto 优先级）==")

    import asyncio as _aio

    class _State:
        def __init__(self, mode):
            self._mode = mode

        async def get_status_bar_mode(self, _tid):
            return self._mode

    class _Host2:
        """只带 _effective_status_bar_enabled 所需的最小宿主。"""

        status_bar_enabled = False  # 面板全局：关

        def __init__(self, mode):
            self.state_manager = _State(mode)

    resolver = M.QuillPlugin._effective_status_bar_enabled

    # 面板关 + 会话 on → 开（覆盖生效）
    _eq(_aio.run(resolver(_Host2("on"), "t")), True, "面板关 + 会话 on → 启用")
    # 面板关 + 会话 auto → 关（跟随面板）
    _eq(_aio.run(resolver(_Host2("auto"), "t")), False, "面板关 + auto → 关闭")
    # 面板关 + 会话 off → 关
    _eq(_aio.run(resolver(_Host2("off"), "t")), False, "面板关 + off → 关闭")

    class _Host3(_Host2):
        status_bar_enabled = True  # 面板全局：开

    _eq(_aio.run(resolver(_Host3("off"), "t")), False, "面板开 + 会话 off → 关闭（覆盖生效）")
    _eq(_aio.run(resolver(_Host3("on"), "t")), True, "面板开 + 会话 on → 启用")
    _eq(_aio.run(resolver(_Host3("auto"), "t")), True, "面板开 + auto → 启用")

    # state 层：未知 mode 值按 auto 的语义处理（不抛异常）
    class _StateWeird(_State):
        async def get_status_bar_mode(self, _tid):
            return "bogus"

    class _Host4(_Host3):
        def __init__(self):
            self.state_manager = _StateWeird("bogus")

    _eq(_aio.run(resolver(_Host4(), "t")), True, "未知 mode 值回退到全局开关值")

    # 读取失败不得让整轮请求崩掉
    class _StateBroken:
        async def get_status_bar_mode(self, _tid):
            raise RuntimeError("boom")

    class _Host5(_Host3):
        def __init__(self):
            self.state_manager = _StateBroken()

    _eq(_aio.run(resolver(_Host5(), "t")), True, "读 state 抛异常时回退全局开关")

    # state.py 的 setter/getter 往返
    import importlib
    StateMod = None
    for _name in ("astrbot_plugin_quillplus.state", "state"):
        try:
            StateMod = importlib.import_module(_name)
            break
        except Exception:
            continue
    if StateMod is None:
        _assert(False, "无法导入 state 模块（跳过 setter 往返断言）")
        return

    import tempfile, os as _os
    with tempfile.TemporaryDirectory() as td:
        sm = StateMod.StateManager(data_dir=td)
        _eq(_aio.run(sm.get_status_bar_mode("nobody")), "auto", "未设置的会话返回 auto")
        _aio.run(sm.set_status_bar_mode("u1", "off"))
        _eq(_aio.run(sm.get_status_bar_mode("u1")), "off", "setter 写入后 getter 读回")
        _aio.run(sm.set_status_bar_mode("u1", "on"))
        _eq(_aio.run(sm.get_status_bar_mode("u1")), "on", "可覆盖为 on")
        # 默认值：新 UserState 未显式设置时是 auto
        _eq(StateMod.UserState(user_id="x").status_bar_mode, "auto", "UserState 默认 auto")


def t22_statusbar_dispatch() -> None:
    """/quill statusbar 命令分发：无参显示状态，带参校验权限并写入。"""
    print("\n== 22. /quill statusbar 命令分发 ==")

    import asyncio as _aio
    import importlib

    Cmds = None
    for _name in ("astrbot_plugin_quillplus.commands", "commands"):
        try:
            Cmds = importlib.import_module(_name)
            break
        except Exception:
            continue
    if Cmds is None:
        _assert(False, "无法导入 commands 模块")
        return

    class _State:
        def __init__(self):
            self.mode = "auto"
            self.writes = []

        async def get_status_bar_mode(self, _tid):
            return self.mode

        async def set_status_bar_mode(self, _tid, mode):
            self.mode = mode
            self.writes.append(mode)

    class _Event:
        def __init__(self):
            self.result = None

        def set_result(self, r):
            self.result = r

        def get_message_type(self):
            # 私聊 → 权限放行
            from astrbot.core.platform.message_type import MessageType
            return MessageType.FRIEND_MESSAGE

        def get_sender_id(self):
            return "10000"

        def unified_msg_origin(self):
            return "test:umo"

    class _Plugin:
        def __init__(self):
            self.state_manager = _State()
            self.status_bar_enabled = False
            # _check_group_permission 读 plugin.config.admin_users（私聊分支
            # 其实用不到，但属性必须存在，否则直接 AttributeError）
            self.config = types.SimpleNamespace(admin_users=[])

    # 无参数：只报告，不写
    p = _Plugin()
    ev = _Event()
    _aio.run(Cmds.statusbar_dispatch(p, ev, ""))
    text = ev.result.get_plain_text() if hasattr(ev.result, "get_plain_text") else str(ev.result)
    _assert("auto" in text, "无参时报告当前 mode")
    _assert("本轮实际生效" in text, "无参时报告生效值")
    _eq(p.state_manager.writes, [], "无参不写状态")

    # on / off / 中文别名
    for arg, want in (("on", "on"), ("OFF", "off"), ("关", "off"), ("自动", "auto"), ("开", "on")):
        p2 = _Plugin()
        ev2 = _Event()
        _aio.run(Cmds.statusbar_dispatch(p2, ev2, arg))
        _eq(p2.state_manager.writes, [want], f"/quill statusbar {arg} → 写入 {want}")

    # 非法参数不写状态（当作查询）
    p3 = _Plugin()
    ev3 = _Event()
    _aio.run(Cmds.statusbar_dispatch(p3, ev3, "bogus"))
    _eq(p3.state_manager.writes, [], "非法参数不写入")


def t15_strip_dynamic_fields() -> None:
    print("\n== 15. 剥离器动态字段（自定义字段名不再漏擦）==")

    strip = M.QuillPlugin._strip_status_artifacts

    # 插件自己的协议文本（prompt_builder.build_status_bar_guide）就建议把
    # 「好感度」换成「催眠度/服从度/淫乱度/信赖度」。此前剥离器写死 8 个字段名，
    # 这些建议字段不在其中 —— 关闭状态栏后裸字段行会原样漏到用户屏幕上。
    custom = ["催眠度", "关系阶段", "心情", "位置", "穿着", "当前想法"]

    _eq(
        strip("正文。\n催眠度：50", custom),
        "正文。",
        "自定义字段（催眠度）在关闭状态下被剥净",
    )
    _eq(
        strip("正文。\n信赖度：70", ["信赖度", "心情", "位置", "穿着", "当前想法", "关系阶段"]),
        "正文。",
        "自定义字段（信赖度）被剥净",
    )

    # 未传字段表时退回默认表，保证旧调用点与既有行为不变
    _eq(strip("正文。\n好感度：65"), "正文。", "不传字段表时用默认表（旧调用点兼容）")
    _eq(
        strip("正文。\n催眠度：50"),
        "正文。\n催眠度：50",
        "不传字段表时自定义字段不剥（退回默认表，不误擦）",
    )

    # 关键：剥离侧不设值长上限。解析侧有 {1,30} 保护（避免误删叙事），
    # 剥离侧若跟着一起收窄，长值行就漏出去了。
    long_line = "正文。\n好感度：" + "很" * 40
    _eq(strip(long_line, M._DEFAULT_LOVE_FIELDS_RAW), "正文。", "长值裸字段行也被剥净（剥离侧不设限）")

    # 解析侧的长度上限仍在（这是防误删正文的保护，不能一起放开）
    parse_re = M._build_raw_status_re(M._DEFAULT_LOVE_FIELDS_RAW)
    _eq(parse_re.findall("好感度：" + "很" * 40), [], "解析侧仍拒绝超长值（叙事保护）")
    _eq(parse_re.findall("好感度：65"), [("好感度", "65")], "解析侧正常短值不受影响")

    # 叙事保护：正文句中的同名字段不能被剥离器拦腰截断
    _eq(
        strip("想读懂她的心情：那份悸动", M._DEFAULT_LOVE_FIELDS_RAW),
        "想读懂她的心情：那份悸动",
        "行首锚定：叙事句中段不误擦",
    )

    # 缓存：同字段表取到同一对象；字段变化时重建
    r_a = M.QuillPlugin._strip_bare_fields_re(["好感度", "心情"])
    r_b = M.QuillPlugin._strip_bare_fields_re(["好感度", "心情"])
    r_c = M.QuillPlugin._strip_bare_fields_re(["催眠度", "心情"])
    _assert(r_a is r_b, "同字段表命中缓存（同一对象）")
    _assert(r_a is not r_c, "字段变化时重建正则")


def t16_session_state_gate() -> None:
    print("\n== 16. 关闭状态栏后不再注入 session_vars（发现 2）==")

    import asyncio as _aio

    class _Cfg:
        max_prompt_length = 50000
        min_output_length = 400
        max_output_length = 0

        def __init__(self, sb_on: bool):
            self.status_bar_enabled = sb_on

    extra = {"session_vars": {"好感度": "85", "心情": "开心"}}

    on = M.PromptBuilder(_Cfg(True))
    off = M.PromptBuilder(_Cfg(False))

    async def _stable(builder, extra_info):
        stable, _dynamic = await builder.build_system_prompt(None, None, extra_info)
        return stable

    stable_on = _aio.run(_stable(on, extra))
    stable_off = _aio.run(_stable(off, extra))

    _assert("## 当前状态" in stable_on, "开启时注入「## 当前状态」")
    _assert("好感度=85" in stable_on, "开启时字段可见")
    _assert(
        "## 当前状态" not in stable_off,
        "关闭时不注入「## 当前状态」（否则一边禁止输出一边示范字段）",
    )
    _assert("好感度=85" not in stable_off, "关闭时字段不可见")

    # 空 session_vars：开与关都不注入
    empty_on = _aio.run(_stable(M.PromptBuilder(_Cfg(True)), {}))
    _assert("## 当前状态" not in empty_on, "无状态数据时不产生空段")


def t8_import_surface() -> None:
    print("\n== 8. 模块级对象健全性（改动的第一道防线）==")

    _assert(len(_Host.love_fields) >= 6, "默认字段表至少 6 项")
    _assert(M._STATUS_BLOCK_RE is not None, "_STATUS_BLOCK_RE 存在")
    _assert(M._LOVE_DATA_RE is not None, "_LOVE_DATA_RE 存在")
    _assert(M._STATUS_RE is not None, "_STATUS_RE 存在")
    # 字段名相关的那条不再写死在列表里（改由 _strip_bare_fields_re 按配置动态构建），
    # 故此处只固定「与字段名无关」的条数。改动时需同步本断言。
    _eq(len(M.QuillPlugin._STRIP_PATTERNS), 6, "字段名无关的剥离正则条数")


def t9_normalize() -> None:
    print("\n== 9. 标注剥离 _normalize_status_value（防状态值漂移）==")

    norm = M._normalize_status_value

    _eq(norm("70（↑5）"), "70", "剥离全角括号增量标注")
    _eq(norm("70(↑5)"), "70", "剥离半角括号增量标注")
    _eq(norm("开心（↓）"), "开心", "剥离仅方向的标注")
    _eq(norm("70 （↑5） "), "70", "标注前后空白一并清理")

    # 关键：不能误伤合法值
    _eq(norm("上升↑"), "上升↑", "值内含裸箭头时不动（不是我们的标记语法）")
    _eq(norm("70"), "70", "无标注时原样返回")
    _eq(norm(""), "", "空值安全")
    _eq(norm("心情：很好（↑）真的"), "心情：很好（↑）真的", "标注不在尾部时不剥离")

    # 幂等：重复剥离稳定（模型可能照抄两遍）
    once = norm("70（↑5）")
    _eq(norm(once + "（↑5）"), "70", "连续标注被一并剥净")


def t10_delta_format() -> None:
    print("\n== 10. 变化标注生成 _format_delta ==")

    _eq(M._format_delta("65", "70"), "（↑5）", "数值上升给增量")
    _eq(M._format_delta("70", "65"), "（↓5）", "数值下降给增量")
    _eq(M._format_delta("65", "70.5"), "（↑5.5）", "小数增量保留小数")
    _eq(M._format_delta("65", "65"), "", "值未变不标注")
    _eq(M._format_delta("65/100（说明）", "84/100（别的说明）"), "（↑19）",
        "带说明文字的数值也能比对（取首个数字）")

    # 关键设计：文本字段不给标注。
    # 文本字段（心情/穿着/当前想法…）每轮都在变，空箭头不传达信息，
    # 还会把真正有价值的数值变化淹没在噪声里。
    _eq(M._format_delta("暧昧", "恋人"), "", "文本值不给标注（不挂空箭头）")
    _eq(M._format_delta("开心", "更开心了"), "", "文本值不给标注")
    _eq(M._format_delta("", "70"), "", "无旧值不给标注（无从判断方向）")
    _eq(M._format_delta("70", ""), "", "新值缺失不给标注")

    # 可解析的数值形态
    _eq(M._extract_numeric("65"), 65.0, "纯数字")
    _eq(M._extract_numeric("65/100"), 65.0, "分数取分子")
    _eq(M._extract_numeric("65/100（心动到不行）"), 65.0, "分数带括注")
    _eq(M._extract_numeric("-3"), -3.0, "负数")
    _eq(M._extract_numeric("心情：很好"), None, "带前缀的非纯数字取不到")
    _eq(M._extract_numeric("好感度很高"), None, "纯文本取不到数值")
    _eq(M._extract_numeric(""), None, "空值取不到数值")

    # 单位/区间形态
    _eq(M._extract_numeric("3 级"), 3.0, "数字+单位可取")
    _eq(M._extract_numeric("80%"), 80.0, "百分号可取")


def t11_annotate() -> None:
    print("\n== 11. 正文标注重写 _annotate_changes ==")

    anno = M._annotate_changes

    body = "好感度：70\n心情：开心\n位置：酒馆"
    out = anno(body, {"好感度": "65", "心情": "平静", "位置": "沙滩"}).split("\n")
    _eq(out[0], "好感度：70（↑5）", "数值字段变化追加增量")
    _eq(out[1], "心情：开心", "文本字段变化不加标注（新值本身即信息）")
    _eq(out[2], "位置：酒馆", "文本字段变化不加标注")
    _eq(anno(body, {}), body, "无变化时正文原样")

    # 与归一化联动：旧标注必须被替换而不是叠加
    _eq(
        anno("好感度：70（↑5）", {"好感度": "65"}),
        "好感度：70（↑5）",
        "旧标注被剥净后重新标注（不叠加）",
    )
    _eq(
        anno("好感度：70（↑5）", {}),
        "好感度：70",
        "关闭标注时旧标注仍被剥净（防漂移）",
    )

    # 非「字段：值」行原样透传
    mixed = "好感度：70\n\n>>> 剧情走向 <<<\n1. 继续\n<<< 请选择 >>>"
    kept = anno(mixed, {"好感度": "65"})
    _assert(">>> 剧情走向 <<<" in kept, "剧情走向块不被破坏")
    _assert("1. 继续" in kept, "选项文本不被破坏")

    _eq(anno("", {"好感度": "65"}), "", "空文本安全")
    _eq(anno("好感度：65", {}), "好感度：65", "空 changed 表不做标注")


def t12_delta_roundtrip() -> None:
    print("\n== 12. 标注-解析往返（防值漂移的端到端证明）==")

    # 场景：第 1 轮 65 → 第 2 轮 70（渲染出标注）→ 第 3 轮模型照抄带标注的值。
    # 修复前：第 3 轮会读出 "70（↑5）" 存库 → 注入提示词 → 逐轮累积。
    round2_body = "好感度：70\n心情：开心"
    rendered = M._annotate_changes(round2_body, {"好感度": "65"})
    _assert("（↑5）" in rendered, "第 2 轮渲染出标注")

    # 第 3 轮：模型照抄了带标注的文本，走 L1 解析
    echoed = "**状态栏**\n```\n" + rendered + "\n```"
    m = M._STATUS_BLOCK_RE.search(echoed)
    _assert(m is not None, "带标注的状态栏仍能被 L1 正则识别")
    if m:
        parsed = _parse_status_block(m.group(1))
        stored = M._normalize_status_value(parsed.get("好感度", ""))
        _eq(stored, "70", "落库值干净（无标注残留）→ 不漂移")

    # 第 4 轮：值未变，不应再次标注（证明对比用的是干净值）
    _eq(
        M._annotate_changes("好感度：70", {"好感度": "70"}).split("\n")[0],
        "好感度：70",
        "值未变时不重复标注",
    )


def t13_inject_report() -> None:
    print("\n== 13. 注入报告渲染与清洗 ==")

    fmt = M.QuillPlugin._format_inject_report
    scrub = M.QuillPlugin._scrub_inject_report

    _eq(fmt({}), "", "空 dict（未采集）不产生报告行")
    _eq(fmt({"doc": 0, "mem": 0, "wr": 0, "wb": 0}), "〔注入〕无命中",
        "全 0 命中显示「无命中」而非消失（否则分不清开关没生效与确实没命中）")

    line = fmt({"wb": 2, "mem": 3, "wr": 2, "doc": 1, "doc_sources": ["设定集.md"]})
    _assert(line.startswith("〔注入〕"), "报告行有统一前缀")
    _assert("世界书×2" in line, "世界书命中数")
    _assert("记忆×3" in line, "记忆命中数")
    _assert("素材×2" in line, "素材命中数")
    _assert("文档×1（设定集.md）" in line, "文档来源名（引用溯源）")

    many = fmt({"doc": 5, "doc_sources": ["a.md", "b.md", "c.md", "d.md", "e.md"]})
    _assert("a.md、b.md、c.md" in many, "来源名最多列 3 个")
    _assert("等 5 份" in many, "超出部分折叠计数")

    core = fmt({"mem": 1, "core_mem": 2})
    _assert("核心记忆×2" in core, "核心记忆单独计数")

    # 清洗：报告行不得留在对话历史里（否则被模型模仿）
    hist = "正文内容。\n\n〔注入〕世界书×2 · 记忆×3"
    _eq(scrub(hist), "正文内容。", "历史里的报告行被清除")

    _eq(scrub("正文里提到〔注入〕但不在行首"), "正文里提到〔注入〕但不在行首", "行中的同形文本不被误删")
    _eq(scrub("普通正文"), "普通正文", "无报告行时原样返回")
    _eq(scrub(""), "", "空文本安全")

    # 往返：渲染出来的行必须能被清洗掉（正则对称）
    rendered = "正文。\n\n" + fmt({"wb": 1, "mem": 1, "doc": 2, "doc_sources": ["x.md"]})
    _eq(scrub(rendered), "正文。", "渲染-清洗往返闭合")


def t14_report_cache_semantics() -> None:
    print("\n== 14. 注入报告缓存的「没命中 vs 没采集」区分 ==")

    # 直接调实例方法需要一个最小宿主：这两个方法只用 self 上的缓存字典
    host = M.QuillPlugin.__new__(M.QuillPlugin)
    host._inject_reports = {}

    # 未采集过的会话：返回空 dict → 报告行不出现
    _eq(host._get_inject_report("umo-A"), {}, "未采集的会话返回空 dict")
    _eq(
        M.QuillPlugin._format_inject_report(host._get_inject_report("umo-A")),
        "",
        "未采集 → 不产生报告行（保持沉默）",
    )

    # 采集过但一条没命中：补齐标准键 → 报告行显示「无命中」
    # 这是实测踩出来的坑：RAG 未初始化时 _run_rag_retrieval 提前 return，
    # 一个键都不填，报告行整个消失，用户分不清「没命中」和「开关没生效」。
    host._remember_inject_report("umo-B", {})
    cached = host._get_inject_report("umo-B")
    _eq(cached.get("wb"), 0, "采集过但无命中：wb 补 0")
    _eq(cached.get("doc"), 0, "采集过但无命中：doc 补 0")
    _eq(
        M.QuillPlugin._format_inject_report(cached),
        "〔注入〕无命中",
        "采集过但无命中 → 显示「无命中」（与未采集可区分）",
    )

    # 有命中时正常渲染，且补齐的标准键不干扰既有计数
    host._remember_inject_report("umo-C", {"wb": 2, "mem": 3})
    line = M.QuillPlugin._format_inject_report(host._get_inject_report("umo-C"))
    _assert("世界书×2" in line and "记忆×3" in line, "补齐键不影响真实命中计数")
    _assert("素材×0" not in line and "文档×0" not in line, "0 命中的来源不出现在报告里")

    # 容量上限：超过 _INJECT_REPORT_MAX 时按「最近使用」淘汰最早的
    limit = M.QuillPlugin._INJECT_REPORT_MAX
    for i in range(limit + 5):
        host._remember_inject_report(f"umo-{i}", {"wb": 1})
    _assert(
        len(host._inject_reports) <= limit,
        f"缓存条目数不超过上限（{len(host._inject_reports)} ≤ {limit}）",
    )
    _eq(
        host._get_inject_report("umo-0"),
        {},
        "最早写入的条目被淘汰",
    )
    _assert(
        host._get_inject_report(f"umo-{limit + 4}").get("wb") == 1,
        "最新写入的条目仍在缓存里",
    )


def t24_handle_status_bar_coroutine() -> None:
    """驱动真实的 `_handle_status_bar` 协程（此前 fixture 只测正则，没测这条路径）。

    为什么补这一条：L4 单命中分支是本次改动里**线上无法复现**的一段——
    模型即使收到「禁止输出标记」的覆盖指令，仍坚持按 system prompt 走
    [LOVE_DATA]（探针 C 第 4 轮 + 三次定向尝试，全部走 L2）。线上复现不了，
    就只能在这里锁定：**单命中行必须被剥离，且不重建整栏**。

    这一段的风险很具体：若哪天有人把 `elif raw_matches:` 那半边删掉
    （只保留「≥2 才重建」），单命中行会原样发给用户，同时下面再跟一个
    完整状态栏——用户看到的是「心情：愉快」+ 一整栏，像漏处理。
    """
    print("\n== 24. _handle_status_bar 协程（L4 单命中端到端）==")

    import asyncio as _aio

    template = "**状态栏**\n```\n{content}\n```"

    class _State:
        def __init__(self, prev=None):
            self.prev = dict(prev or {})
            self.persisted = {}

        async def get_session_vars(self, _tid):
            return dict(self.prev)

        async def update_session_vars(self, _tid, updates):
            self.persisted.update(updates)

    class _Health:
        def __init__(self):
            self.records = []
            self.levels = []

        def record_status(self, ok):
            self.records.append(ok)

        def record_status_level(self, level):
            # 降级链重构后，_handle_status_bar 每命中一级都会调这个方法。
            # 桩必须提供它，否则整条链在统计处 AttributeError（会被驱动器
            # 的 try/except 吞掉并「继续下降」，表现为状态栏静默失效）。
            self.levels.append(level)

    def _mk(prev=None):
        """造一个只带 _handle_status_bar 所需属性的宿主。

        用 object.__new__ 而不是 QuillPlugin(...)：插件真正的 __init__
        会去连数据库、起后台任务，测试里既慢又不该有副作用。
        """
        host = object.__new__(M.QuillPlugin)
        host.love_fields = list(M._DEFAULT_LOVE_FIELDS_RAW)
        host.status_bar_default_placeholder = "未设置"
        host.status_bar_format_template = template
        host.status_bar_show_delta = False  # 关掉标注，断言只看剥离与重建
        host.state_manager = _State(prev)
        host.health_tracker = _Health()
        # L6 未启用：配置里 llm_extract=false，这里保持同样前提
        host.config = types.SimpleNamespace(status_bar_llm_extract=False)
        return host

    handler = M.QuillPlugin._handle_status_bar

    # ── 1. 单命中：行被剥离，且**不**重建整栏 ──
    h = _mk()
    text1 = "她听完，脚步轻快了些。\n心情：愉快"
    out1, upd1, handled1 = _aio.run(handler(h, text1, "t1", template))
    _eq("心情：愉快" in out1, False, "单命中行被剥离（不漏给用户）")
    _eq("她听完，脚步轻快了些。" in out1, True, "正文原样保留")
    _eq(handled1, False, "单命中不视为已处理（交给兜底补整栏）")
    _eq(upd1, {}, "单命中不产出状态字段（不拿一行叙事造整栏）")

    # ── 2. 双命中：重建整栏，原行被替换 ──
    h = _mk()
    text2 = "她笑了笑。\n心情：愉快\n位置：教室"
    out2, upd2, handled2 = _aio.run(handler(h, text2, "t2", template))
    _eq(handled2, True, "双命中视为已处理")
    _eq(out2.count("心情：愉快"), 1, "原裸行被替换而非重复")
    _eq(("心情" in upd2) and ("位置" in upd2), True, "两个字段都进 updates")
    _eq("**状态栏**" in out2, True, "套用了传入的模板")

    # ── 3. 单命中的值超长：解析侧不命中（叙事保护），因此不剥离 ──
    # 这是刻意的取舍：行首「字段：长句」更可能是叙事，宁漏不误删正文。
    h = _mk()
    long_tail = "很" * 40
    text3 = f"她轻声说。\n好感度：{long_tail}"
    out3, _upd3, handled3 = _aio.run(handler(h, text3, "t3", template))
    _eq(handled3, False, "超长值单命中不重建整栏")
    _eq(long_tail in out3, True, "解析侧不命中时正文原样保留（宁漏不误删）")

    # ── 4. L5 兜底：两个字段名都是「宽泛写法」，L4 精确匹配不中，由 L5 接住 ──
    # `_lenient_parse_status` 的匹配规则是子串双向包含（`lf in key or key in lf`），
    # 所以「好感」→好感度、「关系」→关系阶段。用它们才能确保走的是 L5 而不是 L4：
    # 只要出现一个精确字段名（如「心情」），L4 就先命中，轮不到 L5。
    h = _mk(prev={"位置": "教室"})
    text4 = "她想了想。\n好感：65\n关系：更近了"
    out4, _upd4, handled4 = _aio.run(handler(h, text4, "t4", template))
    _eq(handled4, True, "L5 宽松解析兜住双字段")
    _eq("**状态栏**" in out4, True, "L5 也套模板")
    _eq("位置：教室" in out4, True, "未提供的字段用历史值补齐")
    _eq("好感度：65" in out4, True, "宽泛字段名归一到配置里的规范名")

    # ── 5. 纯文本模板：同样走这条路径，不该出现 Markdown 标记 ──
    plain = "───── 状态栏 ─────\n{content}\n────────────────"
    h = _mk()
    out5, _upd5, _h5 = _aio.run(handler(h, text2, "t5", plain))
    _eq("```" in out5, False, "纯文本模板不产生围栏")
    _eq("───── 状态栏 ─────" in out5, True, "纯文本模板被套用")

    # ── 6. 健康度：无论命中与否都记一次（调用方已判过开关）──
    _eq(len(h.health_tracker.records), 1, "解析结果记入健康度")


def t25_level_registry_and_gate() -> None:
    """降级链重构 + 闸门拆分的新行为锁定。

    两件事在这里钉死：

    1. **注册表顺序与逐级统计**：`_SB_LEVELS` 是降级链的唯一顺序来源，
       每级命中都要记进 HealthTracker。此前只能 grep 日志文本看走了哪级，
       既没比例也不能呈现——现在有结构化计数。
    2. **L4 单命中是 non-terminal**：它擦掉可疑行后**继续下降**，所以
       「记了级别」但「不算成功产出」。这个区分是重构的核心语义，
       最容易被后人误改成 terminal（那会让单命中行被擦后没有兜底栏）。
    """
    print("\n== 25. 降级链注册表 + 逐级统计 + 闸门语义 ==")

    import asyncio as _aio

    # ── 1. 注册表本身 ──
    levels = M.QuillPlugin._SB_LEVELS
    _eq([n for n, _ in levels],
        ["code block", "LOVE_DATA inline", "STATUS legacy",
         "raw key:value, 动态字段", "lenient + 部分提取", "LLM 智能提取"],
        "六级顺序与既有日志文本一致")
    _eq(len({attr for _, attr in levels}), 6, "六个方法名互不重复")
    for name, attr in levels:
        _assert(hasattr(M.QuillPlugin, attr), f"{name} → {attr} 已定义")

    # ── 2. 逐级统计真的被写入 ──
    class _H:
        def __init__(self):
            self.levels = []

        def record_status_level(self, lv):
            self.levels.append(lv)

    import types
    tracker = M.HealthTracker(window_size=5)
    tracker.record_status_level("LOVE_DATA inline")
    tracker.record_status_level("LOVE_DATA inline")
    tracker.record_status_level("code block")
    st = tracker.stats()["status_bar"]
    _eq(st["levels"].get("LOVE_DATA inline"), 2, "同一级累计计数")
    _eq(st["levels"].get("code block"), 1, "另一级单独计数")
    _eq(list(st["levels"].keys())[0], "LOVE_DATA inline", "levels 按次数降序")
    _eq(tracker.stats()["status_bar"]["total"], 0,
        "levels 与 success 计数相互独立（未调 record_status）")

    # ── 3. L4 单命中：non-terminal（擦掉行、继续下降）──
    # 直接调该级方法，验证返回值语义——不经过驱动器，避免后续级别干扰。
    class _State2:
        async def get_session_vars(self, _t):
            return {}

    template = "**状态栏**\n```\n{content}\n```"
    host = object.__new__(M.QuillPlugin)
    host.love_fields = list(M._DEFAULT_LOVE_FIELDS_RAW)
    host.status_bar_default_placeholder = "未设置"
    host.status_bar_format_template = template
    host.status_bar_show_delta = False
    host.state_manager = _State2()
    host.config = types.SimpleNamespace(status_bar_llm_extract=False)

    ctx = M._StatusLevelContext(
        text="她说。\n心情：愉快",
        new_text="她说。\n心情：愉快",
        template=template,
        prev_vars={},
        mk_changed=lambda _u: {},
        target_id="t",
    )
    res = _aio.run(M.QuillPlugin._sb_l4_raw(host, ctx))
    _assert(res is not None, "L4 单命中返回结果对象")
    _eq(res.terminal, False, "L4 单命中是 non-terminal（要继续下降）")
    _eq(res.updates, {}, "L4 单命中不产出字段")
    _eq("心情：愉快" in res.new_text, False, "但那行必须被擦掉")

    # 双命中了就该 terminal
    ctx2 = M._StatusLevelContext(
        text="她说。\n心情：愉快\n位置：教室",
        new_text="她说。\n心情：愉快\n位置：教室",
        template=template,
        prev_vars={},
        mk_changed=lambda _u: {},
        target_id="t",
    )
    res2 = _aio.run(M.QuillPlugin._sb_l4_raw(host, ctx2))
    _eq(res2.terminal, True, "L4 双命中是 terminal（重建整栏即结束）")

    # ── 4. 参数缺失时各级应安全返回 None，而不是抛异常 ──
    empty_ctx = M._StatusLevelContext(
        text="毫无状态栏的普通叙事。",
        new_text="毫无状态栏的普通叙事。",
        template=template,
        prev_vars={},
        mk_changed=lambda _u: {},
        target_id="t",
    )
    for name, attr in levels:
        if attr == "_sb_l6_llm_extract":
            continue  # 默认关闭，且会去打 LLM
        r = _aio.run(getattr(M.QuillPlugin, attr)(host, empty_ctx))
        _eq(r, None, f"{name} 在无匹配时返回 None")

    # ── 5. 闸门拆分：两个标记互不影响 ──
    # 这是「多轮工具调用时裸 [LOVE_DATA] 泄漏」的修复要点：
    # 记忆标记（_quill_memorized）不能再兼职总闸门（_quill_activated）。
    # 断言必须**只看代码行**——解释性注释里会引用那行旧写法，
    # 直接对全文做子串匹配会把注释也算成代码（假失败）。
    src = open(M.__file__, encoding="utf-8").read()
    code_lines = [
        ln for ln in src.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    _assert('event.set_extra("_quill_memorized", True)' in code,
            "存在专用记忆标记")
    _assert('event.set_extra("_quill_activated", False)' not in code,
            "总闸门不再被工具回调清掉（泄漏根因）")
    _assert(code.count('event.set_extra("_quill_activated", True)') >= 1,
            "总闸门仍会被置 True")


def t26_send_hook_safety_net() -> None:
    """发送前兜底钩子（on_decorating_result）的行为锁定。

    为什么需要它：`on_using_llm_tool` 只能改写**工具参数**，而 agent loop 每轮
    迭代会**先** yield 该轮的 `result_chain`、**后**才触发工具钩子
    （`tool_loop_agent_runner.py:917` vs `:982`）。所以模型在「不调用工具」的
    那一轮直接输出的文本，会先于任何工具钩子被推送——实测抓到过 18 次工具调用
    里夹着一次直出，唯独那次绕过了全部钩子、把裸 `[LOVE_DATA]` 送到用户眼前。

    `on_decorating_result` 在发送前触发，拿到最终 MessageChain，能兜住这类残留。
    这一条锁定：钩子存在、只擦不建、且异常不外抛（抛了会中断整条回复）。
    """
    print("\n== 26. 发送前兜底钩子（on_decorating_result）==")

    import asyncio as _aio

    src = open(M.__file__, encoding="utf-8").read()
    _assert("@filter.on_decorating_result" in src, "钩子已注册")
    _assert("async def on_decorating_result" in src, "钩子方法已定义")

    code_lines = [
        ln for ln in src.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    # 只擦不建：不得在其中调用状态栏构建/渲染
    body_start = code.find("async def on_decorating_result")
    _assert(body_start > 0, "能定位到钩子方法体")
    body = code[body_start:body_start + 4000]
    _assert("_strip_raw_markers" in body,
            "开启时走「只擦原始标记」")
    _assert("_strip_status_artifacts" in body,
            "关闭时走「整套剥离」")
    _assert("_handle_status_bar" not in body,
            "不重建状态栏（此刻正文已定型，补栏会与已发分段重复）")
    _assert("_build_default_love_data" not in body,
            "不注入兜底栏（同上：发送前只做减法）")

    # 「只擦原始标记」的范围必须**只含**标记类，不能含裸字段行。
    # 理由：渲染后的状态栏内容与裸字段行在文本上完全同形，而模板用户可自定义
    # （实测用 [[CUSTOMTPL]] 时，按裸字段擦会把栏内容掏空只剩空壳）。
    raw_body_start = code.find("def _strip_raw_markers")
    _assert(raw_body_start > 0, "能定位到 _strip_raw_markers")
    raw_body = code[raw_body_start:raw_body_start + 1200]
    _assert("_strip_bare_fields_re" not in raw_body,
            "只擦标记、不擦裸字段行（避免误删自定义模板内容）")

    # 行为验证：给一段带残留的文本，走过钩子后应被擦净。
    # 必须用真实的 Plain（钩子里做 isinstance(comp, Plain) 判断），
    # 自造一个同名字段的对象会被跳过——第一版就是这么假失败的。
    from astrbot.core.message.components import Plain as _Plain

    class _Result:
        def __init__(self, texts):
            self.chain = [_Plain(t) for t in texts]

    class _Ev:
        def __init__(self, result):
            self._r = result

        def get_result(self):
            return self._r

        # 钩子内部会调 self._get_target_id(event)，它回退到
        # event.get_sender_id()。桩必须提供，否则抛异常被钩子的
        # 兜底 except 吞掉，表现为「什么都没发生」——很容易误判成逻辑错。
        def get_sender_id(self):
            return "10000"

        def unified_msg_origin(self):
            return "test:umo"

    class _State3:
        def __init__(self, mode="auto"):
            self._mode = mode

        async def get_status_bar_mode(self, _tid):
            return self._mode

    def _mk_host(enabled: bool, mode: str = "auto"):
        h = object.__new__(M.QuillPlugin)
        h.love_fields = list(M._DEFAULT_LOVE_FIELDS_RAW)
        h.status_bar_default_placeholder = "未设置"
        h.status_bar_enabled = enabled          # 面板全局
        h.state_manager = _State3(mode)         # 会话级覆盖
        return h

    dirty = "[LOVE_DATA] 88/100（爱意） | 亲密恋人 | 温柔 | 海滩 | 泳装 | 想法"
    clean = "普通的剧情正文，没有残留。"

    # ── 状态栏开启：只擦原始标记 ──
    host = _mk_host(True)
    ev = _Ev(_Result([dirty, clean]))
    _aio.run(M.QuillPlugin.on_decorating_result(host, ev))
    _eq("[LOVE_DATA]" in ev.get_result().chain[0].text, False,
        "开启时：裸 [LOVE_DATA] 段被擦净")
    _eq(ev.get_result().chain[1].text, clean, "干净段落原样保留")

    # ── 关键：开启时**不得**删掉已渲染的状态栏 ──
    # 这是第一版兜底钩子的真实回归：直接用整套 _strip_status_artifacts 会把
    # L1/L2 正常渲染出的 `**状态栏**```...``` ` 整段匹配掉，等于把状态栏删了。
    rendered = (
        "**状态栏**\n```\n好感度：88/100（爱意）\n关系阶段：亲密恋人\n"
        "心情：雀跃\n位置：海滩\n穿着：泳装\n当前想法：想法\n\n"
        ">>> 剧情走向 <<<\n1. 继续当前话题\n<<< 请选择 >>>\n```"
    )
    ev_r = _Ev(_Result([rendered]))
    _aio.run(M.QuillPlugin.on_decorating_result(host, ev_r))
    _eq(ev_r.get_result().chain[0].text, rendered,
        "开启时：已渲染的状态栏原样保留（不被兜底钩子删掉）")

    # 自定义模板（用户可任意改）同样不能被掏空——
    # 这正是「不擦裸字段行」的原因，锁一条防止后人加回裸字段擦除。
    custom = "[[CUSTOMTPL]]\n好感度：88/100（爱意）\n关系阶段：亲密恋人\n[[/CUSTOMTPL]]"
    ev_c = _Ev(_Result([custom]))
    _aio.run(M.QuillPlugin.on_decorating_result(host, ev_c))
    _eq(ev_c.get_result().chain[0].text, custom,
        "开启时：自定义模板内容原样保留（不按裸字段行误删）")

    # ── 状态栏关闭：整套剥离，渲染产物也要清 ──
    host_off = _mk_host(False)
    ev_off = _Ev(_Result([rendered]))
    _aio.run(M.QuillPlugin.on_decorating_result(host_off, ev_off))
    _eq(ev_off.get_result().chain[0].text.strip(), "",
        "关闭时：已渲染的状态栏被清掉（此时它本就不该出现）")

    # ── 会话级 off 覆盖面板 on，同样走「整套剥离」 ──
    host_ovr = _mk_host(True, mode="off")
    ev_ovr = _Ev(_Result([rendered]))
    _aio.run(M.QuillPlugin.on_decorating_result(host_ovr, ev_ovr))
    _eq(ev_ovr.get_result().chain[0].text.strip(), "",
        "会话级 off 覆盖面板 on：按关闭处理")

    # 非 Plain 组件（图片等）不应被触碰，更不能因访问 .text 抛异常
    class _Img:
        pass

    r2 = _Result([dirty])
    r2.chain.append(_Img())
    ev2 = _Ev(r2)
    _aio.run(M.QuillPlugin.on_decorating_result(host, ev2))
    _eq("[LOVE_DATA]" in ev2.get_result().chain[0].text, False,
        "混入非 Plain 组件时仍能擦净文本段")
    # 异常必须被吞掉（发送前抛异常会中断整条回复的发送）
    class _BadEv:
        def get_result(self):
            raise RuntimeError("模拟异常")

    try:
        _aio.run(M.QuillPlugin.on_decorating_result(host, _BadEv()))
        _assert(True, "钩子内部异常被吞掉，不外抛")
    except Exception as exc:
        _assert(False, f"钩子异常外抛了（会中断发送）: {type(exc).__name__}")


def main() -> int:
    print("=== Quill 状态栏解析器 Self-Test ===")
    t1_status_block()
    t2_love_data()
    t3_legacy_status()
    t4_raw_regex()
    t5_lenient()
    t6_plot_paths()
    t7_strip()
    t8_import_surface()
    t9_normalize()
    t10_delta_format()
    t11_annotate()
    t12_delta_roundtrip()
    t13_inject_report()
    t14_report_cache_semantics()
    t15_strip_dynamic_fields()
    t16_session_state_gate()
    t17_fence_robustness()
    t18_contract_single_source()
    t19_platform_template()
    t20_l4_threshold_and_strip()
    t21_statusbar_session_override()
    t22_statusbar_dispatch()
    t23_l2_applies_template()
    t24_handle_status_bar_coroutine()
    t25_level_registry_and_gate()
    t26_send_hook_safety_net()

    print(f"\n=== 结果: {_PASSED} passed, {_FAILED} failed ===")
    if _FAILURES:
        print("\n失败项：")
        for f in _FAILURES:
            print("  - " + f)
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
