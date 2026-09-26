# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""状态栏六级降级链——解析侧（自 main.py 原位搬移，M2.1）。

内容（均为原 main.py 的模块级常量/纯函数 + QuillPlugin 方法）：

- 解析侧正则：``_STATUS_RE`` / ``_LOVE_DATA_RE`` / ``_STATUS_BLOCK_RE`` /
  ``_PLOT_PATH_RE`` / ``_build_raw_status_re``（L4 动态正则）；
- 变化标注纯函数：``_normalize_status_value`` / ``_extract_numeric`` /
  ``_format_delta`` / ``_annotate_changes``（渲染进正文与剥离标注共用）；
- 数据类：``_StatusLevelResult`` / ``_StatusLevelContext``；
- ``StatusbarParsersMixin``：驱动循环 ``_handle_status_bar`` + 注册表
  ``_SB_LEVELS`` + 六级解析器 ``_sb_l1.._sb_l6`` + 持久化/辅助解析。

搬移纪律（PLAN §M2.1）：方法体一行不改——仅两处例外：
  (a) 模块级 ``logger`` 经 quill/core/logbridge.py 桥接（方法体写法不变）；
  (b) 协议标记字面量收敛为 tokens.py 常量（``re.escape`` 拼装，
      编译 pattern 与原字面量逐字节一致）。

L6 说明：``_sb_l6_llm_extract`` 本体不触 astrbot 运行时对象（只读
self.config 开关、转调 self._llm_extract_status），随链搬入 Mixin；
它转调的 ``_llm_extract_status`` 依赖 self.context / provider（astrbot
运行时对象），保留在 main.py。``_SB_LEVELS`` 注册表本就用字符串方法名
（getattr(self, name) 解析），六级顺序与优先级语义原样保留。
"""

from __future__ import annotations

import re

from ...core.logbridge import logger  # noqa: F401  # 方法体 logger 引用经 logbridge 桥接
from .tokens import LOVE_DATA_TAG, STATUS_END_TAG, STATUS_TAG

# ── 解析侧正则 ────────────────────────────────────────────────────
# 协议标记经 tokens.py 收敛：re.escape(标记) 与原转义字面量逐字节一致。
_STATUS_RE = re.compile(re.escape(STATUS_TAG) + r'([\s\S]*?)' + re.escape(STATUS_END_TAG))
_LOVE_DATA_RE = re.compile(re.escape(LOVE_DATA_TAG) + r'\s*(.+)')
_STATUS_BLOCK_RE = re.compile(r'\*\*状态栏\*\*[\s\S]*?```([\s\S]*?)```')
# F5（M3.0b）：字符组加入全角 ＞＜——输出侧（H2/H6）已把箭头归一为全角，
# 模型模仿已归一的历史时会写出全角标记，解析侧必须同样能认。ASCII 语义
# 不变（渲染模板 render.py/parsers.py:487 仍产 ASCII）。
_PLOT_PATH_RE = re.compile(
    r'[>|＞]{2,}\s*(?:Plot\s*Paths|剧情走向|剧情选项)\s*[|<＜]{2,}\s*(.+?)\s*[|<＜]{2,}\s*(?:Select|请选择|选择)\s*[>|＞]{2,}',
    re.DOTALL | re.IGNORECASE
)


def _build_raw_status_re(fields: list, max_value_len: int | None = 30) -> re.Pattern:
    """方案A: 动态构建 L4 正则 — 用配置字段名替代硬编码白名单，扩展分隔符。

    分隔符扩展: [：:=→] 覆盖 '好感度→85' 等非标准格式。
    字段名动态: 支持用户自定义字段（如 饥饿度|thirst）。

    设计约束（检测与删除共用本正则，见 _handle_status_bar 分支 4）：
    - 行首锚定 (?:^|\\n) 且消费前导换行符，使 re.sub 能整行干净移除（含列表符号前缀）；
    - 值上限 {1,30}：状态值是短文本；行首的 "字段：长句" 更可能是叙事而非状态栏，
      宁可漏检交给 L5 宽松解析兜底，也不误删正文；
      上限只属于解析侧。剥离侧（_strip_status_artifacts）以 max_value_len=None
      调用，刻意不设限：关闭状态栏时要保证不漏，长值行也必须擦掉。二者的不对称
      是设计，不是遗漏——把长度统一会让长值裸字段行漏到用户屏幕上；
    - 已知裂缝：本正则只漏 1 个字段时 L5 不会兜底（_lenient_parse_status 内部要求
      ≥2），该区间最终走默认状态栏兜底，属可接受的兜底行为；
    - [^\\S\\n] 作空白类：覆盖全角空格等 Unicode 空白，但不跨行。
    """
    # 转义字段名并过滤空值
    valid_fields = [re.escape(f) for f in fields if f and f.strip()]
    if not valid_fields:
        valid_fields = [re.escape(f) for f in _DEFAULT_LOVE_FIELDS_RAW]
    value_pat = (
        r'([^\n]{1,%d}?)' % max_value_len if max_value_len else r'([^\n]+?)'
    )
    pattern = (
        r'(?:^|\n)'
        r'[^\S\n]*'
        r'(?:[-\*\•]*[^\S\n]*)?'
        r'(' + '|'.join(valid_fields) + r')'
        r'[^\S\n]*\**[^\S\n]*[：:=→][^\S\n]*'
        + value_pat +
        r'[^\S\n]*(?=\n|$)'
    )
    return re.compile(pattern, re.MULTILINE)


# 默认字段名（用于 _build_raw_status_re 兜底）
_DEFAULT_LOVE_FIELDS_RAW = ["好感度", "关系阶段", "心情", "位置", "穿着", "当前想法", "服从度", "发情度"]


# ── 状态栏数值变化标注 ─────────────────────────────────────────────
# 标注形如「好感度：70（↑5）」。之所以用括号包住箭头而不是裸写 `70 ↑`：
#   1. 裸箭头会与合法值混淆 —— 「心情：上升↑」是模型自己可能写出的正常值，
#      用裸箭头做标记就无法区分「值本身」与「我们加的标记」；
#   2. 标注会随 assistant 回复回显进下一轮上下文，模型会模仿。裸箭头一旦
#      被模仿就会在值里逐轮累积（`70 ↑ ↑ ↑`），括号语法则能被下面的
#      _normalize_status_value 精确剥离。
# 该正则同时承担「渲染时匹配字段行」与「解析时剥离标记」两个职责，
# 二者必须对称，否则标记会渗进 session_vars 并被注入提示词。
_DELTA_MARK_RE = re.compile(r"\s*[（(]\s*[↑↓]\s*\d*\s*[）)]\s*$")


def _normalize_status_value(value: str) -> str:
    """剥掉值尾部的变化标注，返回干净取值。

    必须在「比对上一轮」与「写入 session_vars」之前调用：标注是我们自己
    渲染进消息文本的，若被下一轮的 L1/L4/L5 解析器当成取值的一部分读回，
    就会同时污染两处——状态值逐轮漂移（`70（↑5）（↑5）`），以及
    session_vars 经 prompt_builder 注入 system prompt 时带上标记。
    """
    if not value:
        return value
    return _DELTA_MARK_RE.sub("", value).strip()


def _extract_numeric(value: str) -> float | None:
    """从状态值里取出可比对的数值；取不到返回 None。

    状态值是自由文本，这里只认**以数字开头**的形态：纯数字（含正负号、
    小数点），以及数字后跟单位/区间/百分号的写法（`65/100`、`3 级`、`80%`）。
    刻意不做「从任意位置抠数字」——`好感度很高`、`心情：很好` 取不到值是对的，
    比错误地把某处的数字当成状态值要安全。取不到时调用方不给标注（见
    `_format_delta` 的设计说明）。
    """
    if not value:
        return None
    m = re.match(r"\s*([+-]?\d+(?:\.\d+)?)\s*(?:$|[/、,，%级点分])", value)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _format_delta(old: str, new: str) -> str:
    """生成变化标注：仅在两侧都能取到数值时给出 `（↑5）`，否则不给标注。

    为什么不给文本值标一个只有方向的空箭头（`（↑）`）：状态栏里多数字段是
    自由文本（心情、穿着、当前想法…），它们**每轮都在变**——「当前想法」
    本来就该换。给这类字段挂箭头不传达任何信息，还会让有价值的数值变化
    淹没在噪声里。文本字段的新值本身就摆在眼前，用户读到新值便知变化；
    而数值的「变化幅度」是光看新值拿不到的（`84/100` 看不出是涨了 2 还是 20），
    这才是标注真正要补的信息。
    """
    old_num, new_num = _extract_numeric(old), _extract_numeric(new)
    if old_num is None or new_num is None:
        return ""
    diff = new_num - old_num
    if diff == 0:
        return ""
    arrow = "↑" if diff > 0 else "↓"
    # 整数差值不显示小数点（65→70 显示 ↑5 而不是 ↑5.0）
    magnitude = abs(int(diff)) if float(diff).is_integer() else round(abs(diff), 2)
    return f"（{arrow}{magnitude}）"


def _annotate_changes(content: str, changed: dict) -> str:
    """重写状态栏正文：先剥净旧标注，再给发生变化的字段行追加新标注。

    `content` 为 `字段：值` 逐行文本；`changed` 形如 {字段名: 上一轮值}。

    两个职责合并在一处是刻意的：
    - 剥旧标注：标注会随 assistant 回复回显进下一轮上下文，模型可能模仿着
      再写一遍。不剥就会出现 `70（↑5）（↑5）` 逐轮累积。
    - 加新标注：仅在 `changed` 命中时追加，未变化或取不到旧值的字段保持原样。

    匹配失败不做任何事——标注失败远比标错好。非「字段：值」的行
    （剧情走向等）原样透传。
    """
    if not content:
        return content
    out_lines = []
    for line in content.split("\n"):
        m = re.match(r"^([^：:]{1,20})[：:]\s*(.+)$", line)
        if m:
            field, value = m.group(1).strip(), m.group(2).strip()
            clean = _normalize_status_value(value)
            if clean:
                # 只有出现在 changed 里的字段才谈得上「变化」。字段缺席表示本轮
                # 未检出变化，绝不能落到 _format_delta 的「无旧值」分支——
                # 那会给每个未变化字段都挂上一个空箭头。
                mark = (
                    _format_delta(changed[field], clean)
                    if changed and field in changed
                    else ""
                )
                line = f"{field}：{clean}{mark}"
        out_lines.append(line)
    return "\n".join(out_lines)


class _StatusLevelResult:
    """一级降级解析的结果。

    terminal 的语义是这次重构的核心：
      * True  —— 命中即**结束**降级链（这一级产出了可用的状态数据）；
      * False —— 文本已改写但**继续下降**。L4 单命中就是这种：它只擦掉那行
                 可疑的裸字段，不足以重建整栏，所以还要让 L5/L6/兜底接手。

    旧实现把这两种语义藏在 `if not handled:` 的嵌套里（L4 单命中不置 handled
    就落下去），能跑但读不出意图，也无法统计「哪一级真的产出过状态栏」。
    """

    __slots__ = ("new_text", "updates", "terminal")

    def __init__(self, new_text: str, updates: dict | None = None,
                 terminal: bool = True) -> None:
        self.new_text = new_text
        self.updates = updates or {}
        self.terminal = terminal


class _StatusLevelContext:
    """降级链各级共享的输入（避免每级签名拖一长串参数）。

    text      —— 模型原始输出，各级**解析**都用它；
    new_text  —— 当前输出基座，各级**改写**用它。两者必须分开：
                 L4 单命中会把裸字段行从 new_text 里剥掉，而 L5 仍要按原文
                 解析——若改写也从 text 重建，那行裸字段会被重新带回来。
    """

    __slots__ = ("text", "new_text", "template", "prev_vars", "mk_changed",
                 "target_id")

    def __init__(self, text: str, new_text: str, template: str, prev_vars: dict,
                 mk_changed, target_id: str) -> None:
        self.text = text
        self.new_text = new_text
        self.template = template
        self.prev_vars = prev_vars
        self.mk_changed = mk_changed
        self.target_id = target_id


class StatusbarParsersMixin:
    """状态栏六级降级链的解析侧 Mixin（自 QuillPlugin 原位搬移）。

    只承载方法与注册表，不持有状态：``love_fields`` / ``status_bar_*`` /
    ``state_manager`` / ``health_tracker`` / ``config`` 等全部由组合后的
    QuillPlugin 提供（MRO：QuillPlugin → 本 Mixin → StatusbarRenderMixin
    → Star）。``_llm_extract_status`` 仍留在 main.py（依赖 astrbot 运行时
    对象），经 self 转调解析。
    """

    @staticmethod
    def _lenient_parse_status(text: str, love_fields: list) -> dict:
        """宽松解析：扫描文本中 key=value 或 key：value 的行，匹配 love_fields。
        作为严格正则失败后的回退解析器。"""
        updates = {}
        seen = set()
        field_pattern = re.compile(
            r'(?:^|\n)\s*(?:[-\*\•]*\s*)?'
            r'([^\s：:=]+?)\s*[：:=]\s*(.+?)(?=\n(?:[^\s：:=]+\s*[：:=])|\n\n|\n(?:>>>|＞＞＞)|$)',
            re.MULTILINE | re.DOTALL
        )
        for m in field_pattern.finditer(text):
            key = m.group(1).strip()
            val = m.group(2).strip()
            matched_field = None
            for lf in love_fields:
                if lf in key or key in lf:
                    matched_field = lf
                    break
            if matched_field and matched_field not in seen and val:
                seen.add(matched_field)
                updates[matched_field] = val
        return updates if len(updates) >= 2 else {}

    async def _handle_status_bar(
        self, text: str, target_id: str, bar_template: str | None = None
    ) -> tuple:
        """统一状态栏处理入口。

        返回 (formatted_text: str, updates: dict, handled: bool)。
        handled=True 表示文本中已存在有效状态栏并完成了格式化+持久化。
        handled=False 表示未找到状态栏，调用方应注入兜底。

        与上一轮取值的比对读一次 `session_vars` 后全程复用（内存操作），
        同时供变化标注（L1-L6）与 L4/L5 的缺失字段补齐使用。

        bar_template：本次渲染用的模板。None 时用默认（Markdown）模板——
        保持既有调用点行为不变。平台分治由调用方经 _status_bar_template_for
        选好传进来。
        """
        updates = {}
        new_text = text
        handled = False
        bar_template = bar_template or self.status_bar_format_template
        prev_vars = await self.state_manager.get_session_vars(target_id)

        def _mk_changed(ups: dict) -> dict:
            """本次取值相对上一轮的变化 {字段: 旧值}；关闭标注时返回空。

            两侧都先归一化：模型有时会照抄上一轮我们渲染的标注
            （`70（↑5）`），不剥掉就会与干净的旧值比较失败、每轮都误报变化。
            """
            if not self.status_bar_show_delta:
                return {}
            changed = {}
            for k, v in ups.items():
                clean_v = _normalize_status_value(v)
                old = _normalize_status_value(prev_vars.get(k, ""))
                if old and clean_v and old != clean_v and clean_v != self.status_bar_default_placeholder:
                    changed[k] = old
            if changed:
                logger.debug(f"[Quill] 状态栏字段变化: {changed}")
            return changed

        # ── 六级降级链：越靠前越严格，命中即停 ──────────────────
        # 每级是一个独立方法（_sb_l1.._sb_l6），返回 _StatusLevelResult 或 None。
        # 拆成注册表而不是一串 `if not handled:` 块，是为了三件事：
        #   1. 逐级命中率可统计（此前只能 grep 日志文本，看不出比例）；
        #   2. 「命中即结束」与「已改写但仍需下降」两种语义显式化
        #      （见 _StatusLevelResult.terminal）；
        #   3. 某一级抛异常时只降级该级、继续往下走，而不是让整链崩掉
        #      （旧写法下任何一级抛异常都会冒泡到调用方，状态栏直接消失）。
        # 顺序即优先级，不要随意调整：越靠前的格式越严格、越可信。
        ctx = _StatusLevelContext(
            text=text,
            new_text=new_text,
            template=bar_template,
            prev_vars=prev_vars,
            mk_changed=_mk_changed,
            target_id=target_id,
        )
        for _lv_name, _lv_attr in self._SB_LEVELS:
            try:
                _res = await getattr(self, _lv_attr)(ctx)
            except Exception:
                logger.warning(
                    f"[Quill] 状态栏降级链「{_lv_name}」级异常，继续下降",
                    exc_info=True,
                )
                continue
            if _res is None:
                continue
            new_text = _res.new_text
            ctx.new_text = new_text
            if _res.updates:
                updates = _res.updates
            self.health_tracker.record_status_level(_lv_name)
            if _res.terminal:
                handled = True
                break

        # P1-1: 所有降级解析均失败时，记录原始文本片段便于调试（不暴露给用户）
        # 注意：本函数只在「本轮最终开关为开」时才被调用（调用方已用
        # _effective_status_bar_enabled 判过），所以这里不再看全局开关——
        # 否则 /quill statusbar on 覆盖全局关时，这两个分支会被错误跳过。
        if not handled:
            preview = (text or "")[:200].replace("\n", "\\n")
            logger.info(f"[Quill] 状态栏解析失败（L1-L5 全部未匹配），使用兜底默认状态栏 | target={target_id} | preview={preview!r}")

        # P1-4: 记录状态栏解析成功率
        self.health_tracker.record_status(handled)

        # P2-4 修复：所有分支统一在此提交一次状态字段，消除多次独立 await 的竞态
        # 审查修复：变化标注仅渲染进消息文本，绝不写入 session_vars——此前会把
        # dict 一并持久化进 quill_state.json，并被 prompt_builder 无白名单遍历注入
        # system prompt（dict repr 污染模型输入）。
        # 落库前统一归一化：模型可能照抄上一轮的标注，不清洗就会随
        # update_session_vars 存进状态并注入提示词，逐轮累积。
        if updates and handled:
            persist_updates = {
                k: _normalize_status_value(v)
                for k, v in updates.items()
                if not k.startswith("_") and isinstance(v, str)
            }
            updates.update(persist_updates)
            await self._persist_status_vars(persist_updates, target_id)

        return new_text, updates, handled

    # ── 降级链各级实现 ─────────────────────────────────────────
    # 级别名会进日志与统计，改名字要同步 docs/STATUS_BAR.md 与 harness 断言。
    _SB_LEVELS: tuple = (
        ("code block", "_sb_l1_code_block"),
        ("LOVE_DATA inline", "_sb_l2_love_data"),
        ("STATUS legacy", "_sb_l3_legacy"),
        ("raw key:value, 动态字段", "_sb_l4_raw"),
        ("lenient + 部分提取", "_sb_l5_lenient"),
        ("LLM 智能提取", "_sb_l6_llm_extract"),
    )

    async def _sb_l1_code_block(self, ctx) -> "_StatusLevelResult | None":
        """L1：`**状态栏** ``` ... ``` ` —— 只替换块内内容，保留外围 Markdown。

        保留外围结构是刻意的：模型常把标题写在外面，整块重渲染会把它吃掉。
        """
        m = _STATUS_BLOCK_RE.search(ctx.text)
        if not m:
            return None
        raw_content = m.group(1)
        updates = self._parse_status_block(raw_content)
        if not updates:
            return None
        # 首尾空白必须原样带回去——group 1 含包裹内容的换行符，丢掉会把
        # ``` 围栏与内容挤到同一行，代码块随之失效。
        lead = raw_content[: len(raw_content) - len(raw_content.lstrip())]
        trail = raw_content[len(raw_content.rstrip()):]
        annotated = _annotate_changes(raw_content.strip(), ctx.mk_changed(updates))
        new_text = (
            ctx.text[: m.start(1)] + lead + annotated + trail + ctx.text[m.end(1):]
        )
        logger.info("[Quill] 状态栏已处理 (code block)")
        return _StatusLevelResult(new_text, updates)

    async def _sb_l2_love_data(self, ctx) -> "_StatusLevelResult | None":
        """L2：`[LOVE_DATA] a | b | c` 单行 —— **实际最常命中的一级**。

        guide 明确要求模型输出这个格式（还把代码块列为错误示例），实测 40 次
        解析里 36 次走这里。此前这一级不套模板、直接把裸字段行替换进正文，
        导致 format_template 配置在整个子系统的主力路径上从未生效。
        """
        love_updates, love_formatted, raw_line = self._format_love_data(ctx.text)
        if not love_updates:
            return None
        annotated = _annotate_changes(love_formatted, ctx.mk_changed(love_updates))
        # 套用本平台模板，与其余五级一致
        new_text = ctx.text.replace(
            raw_line, ctx.template.replace("{content}", annotated)
        )
        logger.info("[Quill] 状态栏已处理 (LOVE_DATA inline)")
        return _StatusLevelResult(new_text, love_updates)

    async def _sb_l3_legacy(self, ctx) -> "_StatusLevelResult | None":
        """L3：`[STATUS]...[/STATUS]` 旧格式。

        注意这一级**不校验解析结果是否为空**：只要标签在，就算认领（旧行为，
        有意保留——标签本身就是「模型在写状态栏」的确证，哪怕内容不合格式）。
        """
        m = _STATUS_RE.search(ctx.text)
        if not m:
            return None
        status_content = m.group(1).strip()
        updates = self._parse_legacy_status(status_content)
        annotated = _annotate_changes(status_content, ctx.mk_changed(updates))
        formatted = ctx.template.replace("{content}", annotated)
        new_text = _STATUS_RE.sub(formatted, ctx.text)
        logger.info("[Quill] 状态栏已处理 (STATUS legacy)")
        return _StatusLevelResult(new_text, updates)

    async def _sb_l4_raw(self, ctx) -> "_StatusLevelResult | None":
        """L4：裸 `字段：值` 多行（动态字段 + 分隔符扩展）。

        阈值取「去重后 ≥2」而非 ≥1：单命中更可能是叙事（「他想起她当时的心情：
        那份悸动」这类行首恰好是字段名的句子），拿一行叙事重建整栏，其余字段
        全靠历史值补齐，等于用一个可疑值造出一整栏陈旧状态。

        但单命中**必须仍然把那行擦掉**——它会原样发给用户，看着像漏处理。
        所以拆成两条路：≥2 重建整栏（terminal），单命中只剥离该行并继续下降
        （non-terminal，交给 L5/L6/兜底补栏）。
        """
        # 预处理：移除 LLM 可能添加的 --- 分隔线干扰（仅用于解析，不影响输出文本）
        clean_text_for_parse = re.sub(
            r'^[-*_]{3,}[ \t]*$', '', ctx.text, flags=re.MULTILINE
        )
        raw_re = _build_raw_status_re(self.love_fields)
        raw_matches = raw_re.findall(clean_text_for_parse)
        _seen = {fn for fn, _fv in raw_matches if fn in self.love_fields}

        if len(_seen) >= 2:
            updates: dict = {}
            matched_fields = set()
            for fn, fv in raw_matches:
                if fn in self.love_fields and fn not in matched_fields:
                    updates[fn] = fv.strip()
                    matched_fields.add(fn)
            changed = ctx.mk_changed(updates)
            # 补齐缺失字段后再整体标注，保证变化字段与非变化字段同样被剥净旧标注
            for f_name in self.love_fields:
                if f_name not in matched_fields:
                    updates[f_name] = (
                        ctx.prev_vars.get(f_name, "")
                        or self.status_bar_default_placeholder
                    )
            parsed_lines = [
                f"{f}：{_normalize_status_value(str(updates.get(f, '')))}"
                for f in self.love_fields
            ]
            # 用与检测完全相同的正则做对称删除——整行移除（含列表符号前缀，
            # 前导换行一并消费，不留空行）。此前按字段名单独构造无锚定模式
            # (rf'{fn}\s*[：:=→].*')，会误删叙事句中间的同名字段到行尾。
            new_text = raw_re.sub('', ctx.new_text)
            # 剧情走向
            plot_str = ""
            pm = _PLOT_PATH_RE.search(new_text)
            if pm:
                plot_content = pm.group(1).strip()
                new_text = new_text.replace(pm.group(0), "").strip()
                plot_str = f"\n\n>>> 剧情走向 <<<\n{plot_content}\n<<< 请选择 >>>"
            block_content = _annotate_changes("\n".join(parsed_lines), changed) + plot_str
            beautiful_bar = ctx.template.replace("{content}", block_content)
            new_text = new_text.strip() + "\n\n" + beautiful_bar
            logger.info("[Quill] 状态栏已处理 (raw key:value, 动态字段)")
            return _StatusLevelResult(new_text, updates)

        if raw_matches:
            _stripped = raw_re.sub('', ctx.new_text)
            if _stripped != ctx.new_text:
                logger.info(
                    "[Quill] L4 单命中（%s），不重建整栏，仅剥离该行",
                    "/".join(sorted(_seen)),
                )
                return _StatusLevelResult(_stripped, None, terminal=False)
        return None

    async def _sb_l5_lenient(self, ctx) -> "_StatusLevelResult | None":
        """L5：宽松解析（key 双向子串匹配）+ 历史值融合。

        阈值 ≥2 由 `_lenient_parse_status` 内部把关，外层不再复述——旧代码在
        这里写了个恒真的 `>= 1`，让读者以为阈值被调过，实际上空 dict 早已被
        `if lenient_updates` 挡掉。
        """
        lenient_updates = self._lenient_parse_status(ctx.text, self.love_fields)
        if not lenient_updates:
            return None
        merged: dict = {}
        for f in self.love_fields:
            new_val = lenient_updates.get(f)
            merged[f] = new_val if new_val else (
                ctx.prev_vars.get(f, "") or self.status_bar_default_placeholder
            )
        lines = [f"{f}：{merged[f]}" for f in self.love_fields]
        bar = ctx.template.replace(
            "{content}", _annotate_changes("\n".join(lines), ctx.mk_changed(merged))
        )
        logger.info(
            f"[Quill] 状态栏已处理 (lenient + 部分提取 "
            f"{len(lenient_updates)}/{len(self.love_fields)} 字段)"
        )
        return _StatusLevelResult(ctx.new_text + "\n\n" + bar, merged)

    async def _sb_l6_llm_extract(self, ctx) -> "_StatusLevelResult | None":
        """L6：LLM 智能提取（可选，默认关闭——额外 token 消耗）。"""
        if not getattr(self.config, "status_bar_llm_extract", False):
            return None
        llm_extracted = await self._llm_extract_status(ctx.text, ctx.target_id)
        if not llm_extracted:
            return None
        merged: dict = {}
        for f in self.love_fields:
            merged[f] = (
                llm_extracted.get(f)
                or ctx.prev_vars.get(f, "")
                or self.status_bar_default_placeholder
            )
        lines = [f"{f}：{merged[f]}" for f in self.love_fields]
        bar = ctx.template.replace(
            "{content}", _annotate_changes("\n".join(lines), ctx.mk_changed(merged))
        )
        logger.info("[Quill] 状态栏已处理 (LLM 智能提取)")
        return _StatusLevelResult(ctx.new_text + "\n\n" + bar, merged)

    async def _persist_status_vars(self, updates: dict, target_id: str) -> None:
        """Persist parsed status fields to session_vars."""
        if updates:
            await self.state_manager.update_session_vars(target_id, updates)

    @staticmethod
    def _parse_legacy_status(status_content: str) -> dict:
        """Parse legacy [STATUS] multi-line format into key-value dict."""
        updates = {}
        for line in status_content.split("\n"):
            line = line.strip()
            if not line:
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                updates[k.strip()] = v.strip()
        return updates

    def _parse_status_block(self, block_content: str) -> dict:
        """Parse **状态栏** code block content into key-value dict."""
        updates = {}
        for line in block_content.split("\n"):
            line = line.strip()
            if not line:
                continue
            # F5（M3.0b）：全角箭头标记行同样跳过（输出侧已归一为全角，
            # 模型模仿历史时可能写出全角标记 + 冒号，不跳过会被当字段读走）
            if ">>>" in line or "<<<" in line or "＞＞＞" in line or "＜＜＜" in line:
                continue
            if "：" in line:
                k, v = line.split("：", 1)
                updates[k.strip()] = v.strip()
            elif ":" in line:
                k, v = line.split(":", 1)
                updates[k.strip()] = v.strip()
            elif "=" in line:
                k, v = line.split("=", 1)
                updates[k.strip()] = v.strip()
        return updates
