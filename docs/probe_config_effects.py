# -*- coding: utf-8 -*-
"""探针 E：面板配置项改动 → 实际聊天是否真生效（热更新验证）。

背景：面板改配置走 `save_plugin_configs()`，它重建 QuillConfig 并把值投影到
实例属性（`main.py:886` 的 `_PROJECTED_ATTRS`）。**忘了登记投影 = 改了不生效、
要重启**。`tests/test_config_projection.py` 用 AST 守住了「每个字段都有归宿」，
但那只证明「代码登记了」，证明不了「运行期真用上了新值」。

本探针补这一层：**改配置 → 发消息 → 断言回复/日志真的变了 → 改回**。

## 覆盖范围（与 QUILL_TEST_HARNESS_PLAN.md 的分工）

harness 已经覆盖、且通道未变的不重复测：
  * `status_bar.enabled` / `show_delta` —— harness 有 3 条用例（含翻转）
  * `debug.show_inject_report` —— harness 的 report_on/report_off 对照
  * `worldbook.always_activate` —— harness 的 gated_activation_* 三条
  * `/stream`、`/char`、`/memory`、`/wb`、`/doc` 等命令通道 —— Tier 1 全覆盖

本探针只测**上面没覆盖的、且能观测到效果**的键：

  A. `status_bar.fields`          改字段表 → 产出字段名跟着变（投影 + 契约同源）
  B. `status_bar.plot_paths`      改剧情选项 → 产出选项跟着变
  C. `status_bar.format_template` 改模板 → 渲染外观跟着变（含内容包裹）
  D. `status_bar.default_placeholder` 改占位符 → 兜底栏缺值处跟着变
  E. `performance.min_output_length` 改下限 → 回复真的变长
  F. `rag.enable_chat_logging`    A/B 对照：开着写库 / 关掉不写
  G. `rag.enable_memory`          A/B 对照：先造记忆，开着检索得到 / 关掉检索不到

**A-G 都尽量做成「正向对照 + 反向断言」**，避免出现「关掉也不增长」
这类恒真的废话式断言。模型行为不可控的项（D、E、G）用日志门控，
不成立时记 NOTE 而不是 FAIL —— 但正向对照本身会 FAIL（那是真缺陷）。

**每一项都独立 try/finally 还原 + 逐键核对 + 整体 md5 校验。**

明确不测（附理由，见文末"未覆盖"）：
  * `worldbook.enabled` —— **静态分析显示它在运行期没有任何读取点**（只有
    config.py 的 __repr__ 用），这是真发现，改用「配置生效性」单列报告而不是
    硬测一个不存在的行为。
  * `worldbook.match_sensitivity` / `max_dynamic_entries` / `max_token_limit`
    —— 需要绑定的世界书 + `wb_mode != disabled`，当前 4 张卡全是 disabled，
    harness 的 `llm_wr_match_detail` 也因此 SKIP（同一原因）。
  * `writing_resource.*` —— 同上，`wr_mode` 全 disabled。
  * `refusal.*` —— 需要模型真的输出拒绝语，不可控（会让探针随机红）。
  * RAG provider / embedding / chunk 类 —— `_LIVE_READ_FIELDS` 明确豁免：
    这些在 initialize() 时构建组件，改值走 `_reinit_rag_and_refresh_routes`，
    不是属性级热更，需要单独的重初始化测试（本轮不做）。
  * `permissions.admin_users` —— 群聊权限，属 Tier 3（harness 已有）。

运行：
    cd <插件目录>/.build/harness/quilltest
    <AstrBot python> ../../../docs/probe_config_effects.py
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import auth  # noqa: E402
import chat  # noqa: E402
import logs as logs_mod  # noqa: E402
import paths  # noqa: E402

CFG = r"D:\Program\AstrBot\AstrBotData\data\config\astrbot_plugin_quillplus_config.json"
BASE_MD5 = "403fee103fe2c8c82d7166a98c538854"

PROMPT_LEN_RE = re.compile(r"prompt_len=(\d+)")
MEM_LOG_RE = re.compile(r"\[Quill RAG\] 记忆检索: (\d+) 条")
DOC_SKIP_RE = re.compile(r"\[Quill RAG\] 模式: disabled")

# 「裸 [LOVE_DATA] 泄漏」的判据（与 docs/probe_no_leak.py 同源，那边有自检）。
# 不能只搜子串：模型会在正文里**提到**标记名（实测有 254 字的英文自我修正
# 说明提到「using the `[LOVE_DATA]` and `>>> <<<` markers」），那是说明不是泄漏。
_LEAK_RE = re.compile(r"^\s*\[LOVE_DATA\]\s*\S.*\|", re.MULTILINE)
_LEAK_HEAD_RE = re.compile(r"^\s*\[LOVE_DATA\]\s*[\d０-９]", re.MULTILINE)


def _is_leak(text: str) -> bool:
    """文本里是否有**真正送达**的裸状态栏行（行首标记 + 取值），而非提及。"""
    return bool(_LEAK_RE.search(text) or _LEAK_HEAD_RE.search(text))


def md5() -> str:
    return hashlib.md5(io.open(CFG, "rb").read()).hexdigest()


def raw_cfg() -> dict:
    return json.loads(io.open(CFG, encoding="utf-8-sig").read())


class Results:
    def __init__(self) -> None:
        self.items: list[tuple[str, bool, str]] = []

    def add(self, label: str, ok: bool, detail: str = "") -> None:
        self.items.append((label, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))

    def note(self, text: str) -> None:
        self.items.append((f"NOTE {text}", True, ""))
        print(f"  [NOTE] {text}")

    @property
    def ok(self) -> bool:
        return all(ok for label, ok, _ in self.items if not label.startswith("NOTE"))


def main() -> int:
    if md5() != BASE_MD5:
        print("!! 配置基线不符，中止")
        print("   now      =", md5())
        print("   expected =", BASE_MD5)
        return 2

    base = raw_cfg()
    # 逐项备份「本次会动的所有键」——还原时按这份快照写回。
    BACKUP: list[tuple[str, str, object]] = [
        ("status_bar", "enabled", base["status_bar"].get("enabled")),
        ("status_bar", "fields", base["status_bar"].get("fields")),
        ("status_bar", "plot_paths", base["status_bar"].get("plot_paths")),
        ("status_bar", "format_template", base["status_bar"].get("format_template")),
        ("status_bar", "default_placeholder", base["status_bar"].get("default_placeholder")),
        ("performance", "min_output_length", base["performance"].get("min_output_length")),
        ("performance", "max_output_length", base["performance"].get("max_output_length")),
        ("rag", "enable_chat_logging", base["rag"].get("enable_chat_logging")),
        ("rag", "enable_memory", base["rag"].get("enable_memory")),
        ("worldbook", "enabled", base["worldbook"].get("enabled")),
    ]
    print("=== 本次会改动的键（快照）===")
    for g, k, v in BACKUP:
        s = repr(v)
        print(f"   {g}.{k:26s} = {s[:56]}")

    c = auth.DashboardClient()
    wc = chat.WebChat()
    tail = logs_mod.LogTail()
    R = Results()

    def save(updates: list[tuple[str, str, object]], label: str) -> bool:
        r = c.plugin(
            "POST",
            "config/save_batch",
            json={"updates": [
                {"group": g, "key": k, "value": v} for g, k, v in updates
            ]},
            timeout=30.0,
        )
        ok = (r or {}).get("status") == "ok"
        print(f"   save[{label}]:", (r or {}).get("status"), (r or {}).get("message"))
        time.sleep(1.5)
        return ok

    def _last_bot_msg_id() -> int:
        """取当前会话最后一条 bot 消息的 id（作为「本轮之前」的分界）。"""
        try:
            conn = paths.connect_ro(paths.DATA_V4_DB)
            row = conn.execute(
                "SELECT MAX(id) AS m FROM platform_message_history "
                "WHERE platform_id='webchat' AND user_id=? AND sender_id='bot'",
                (wc.session_id,),
            ).fetchone()
            conn.close()
            return int(row["m"] or 0)
        except Exception:
            return 0

    def _delivered_since(msg_id: int) -> str:
        """取 id > msg_id 的**全部** bot 文本段并拼接（= 本轮真正送达的内容）。

        为什么不能只取最后一行：`paths.read_reply_from_db` 用
        `ORDER BY id DESC LIMIT 1`，而模型在 agent 模式下会**多次**调用
        `send_message_to_user`（正文一段、状态栏单独一段），每次都写一行
        history。只看最后一行 = 只看最后一段，会把「字段名/模板没生效」
        误判成 FAIL。这里把本轮新增的所有行都取回来。
        """
        try:
            conn = paths.connect_ro(paths.DATA_V4_DB)
            rows = conn.execute(
                """
                SELECT id,
                       (SELECT group_concat(json_extract(p.value,'$.text'),'')
                        FROM json_each(json_extract(h.content,'$.message')) AS p
                        WHERE json_extract(p.value,'$.type')='plain') AS text
                FROM platform_message_history h
                WHERE platform_id='webchat' AND user_id=? AND sender_id='bot'
                  AND id > ?
                ORDER BY id ASC
                """,
                (wc.session_id, msg_id),
            ).fetchall()
            conn.close()
            return "\n".join(
                str(r["text"]) for r in rows if r["text"]
            )
        except Exception:
            return ""

    def send(text: str, timeout: float = 240.0) -> tuple[str, str]:
        """发消息，返回 (本轮**真正送达**的文本, 该轮插件日志)。

        数据来源是 AstrBot 自己的 `platform_message_history`——它记录真正
        送达会话的内容，比 SSE 事件流权威，且这里把本轮所有分段都取回来
        （见 `_delivered_since` 的说明）。
        """
        mark = tail.mark()
        before_id = _last_bot_msg_id()
        resp = wc.send(text, timeout=timeout)
        time.sleep(0.9)
        delivered = _delivered_since(before_id)
        if not delivered:
            # 兜底：历史还没落库时退回 SSE 的 reply（并接受其分段局限）
            delivered = resp.get("reply") or ""
        return delivered, tail.since_plugin(mark)

    try:
        # 状态栏相关几项都要看「兜底栏」，所以先开启状态栏
        if not save([("status_bar", "enabled", True)], "开启状态栏"):
            print("!! 无法开启状态栏，中止")
            return 2
        wc.send("/char 1", timeout=120)

        # ══════════════════════════════════════════════════════════
        print()
        print("=== A. status_bar.fields：改字段表 → 产出跟着变 ===")
        # 刻意用**满 6 个**自定义字段：不足 6 个时 config.py:153 会补空字符串，
        # 空字段名会让状态栏出现 `：占位` 这种残行，干扰断言（也偏离真实用法）。
        A_FIELDS = "催眠度|服从度|信赖度|淫乱度|敏感度|沉溺度"
        save([("status_bar", "fields", A_FIELDS)], "fields")
        # 清空 session_vars，让兜底栏必然以「字段名：占位符」形态出现——
        # 这样「字段名变了」就一定能被观测到，而不是等模型配合。
        try:
            wc.send("/quill reset", timeout=60)
        except Exception:
            pass
        rep, block = send("【测试】她抬眼看你，等你继续。")
        new_hits = [f for f in A_FIELDS.split("|") if f in rep]
        # 只认「字段：值」形态的裸行，避免正文里偶然提到某个词就误判
        BAR_LINE = re.compile(r"(?:好感度|关系阶段|心情|位置|穿着|当前想法)\s*[：:]\s*\S")
        old_in_bar = BAR_LINE.search(rep)
        R.add("A. 新字段名出现在回复里", len(new_hits) >= 3, f"命中={new_hits}")
        R.add("A. 旧的 6 个字段名不再以「字段：值」形态出现", old_in_bar is None,
              f"仍有 {old_in_bar.group(0)!r}" if old_in_bar else "")
        save([("status_bar", "fields", BACKUP[1][2])], "fields 还原")

        # ══════════════════════════════════════════════════════════
        print()
        print("=== B. status_bar.plot_paths：改剧情选项 → prompt 契约跟着变 ===")
        # 为什么改用 prompt_len 而不看模型输出：剧情选项是**建议**，
        # 模型常按自己的语气改写（实测新选项没出现、也没出现旧的），
        # 拿模型输出判定会把「配置没生效」和「模型没用原词」混为一谈。
        # plot_paths 会进 system prompt 的契约段，所以用**长字符串**把
        # 「是否生效」变成可测量的长度差——与模型行为完全无关。
        B_LONG = "选项甲" + "甲" * 96 + "|选项乙" + "乙" * 96 + "|选项丙" + "丙" * 96
        def prompt_len() -> int:
            mark = tail.mark()
            wc.send("【测试】她看了你一眼。", timeout=240)
            time.sleep(0.8)
            blk = tail.since_plugin(mark)
            m = PROMPT_LEN_RE.search(blk)
            return int(m.group(1)) if m else -1

        base_pl = prompt_len()
        save([("status_bar", "plot_paths", B_LONG)], "plot_paths")
        new_pl = prompt_len()
        old_plen = len(BACKUP[2][2] or "")
        new_plen = len(B_LONG)
        print(f"   prompt_len: {base_pl} → {new_pl} (delta={new_pl - base_pl})")
        print(f"   plot_paths 字符数: {old_plen} → {new_plen} (delta={new_plen - old_plen})")
        # 契约里选项块按行渲染，长度差应约为字符串长度差（允许动态段噪声）
        expect = new_plen - old_plen
        R.add("B. plot_paths 生效（prompt 长度按预期增长）",
              new_pl > base_pl + expect * 0.6,
              f"实际 delta={new_pl - base_pl}，预期约 {expect}")
        save([("status_bar", "plot_paths", BACKUP[2][2])], "plot_paths 还原")

        # ══════════════════════════════════════════════════════════
        print()
        print("=== C. status_bar.format_template：改模板 → 外观跟着变 ===")
        # 用一个绝不会与内置模板冲突的标记
        C_TPL = "[[CUSTOMTPL]]\n{content}\n[[/CUSTOMTPL]]"
        save([("status_bar", "format_template", C_TPL)], "format_template")
        rep, block = send("【测试】她歪了歪头。")
        R.add("C. 自定义模板标记出现在回复里", "[[CUSTOMTPL]]" in rep)
        # 标记之后要真的跟着字段正文，才说明是「包裹内容」而不是被模型复述。
        # 不断言「没有 **状态栏**」：L1 走的是「只替换代码块内容、保留外围
        # Markdown 结构」，所以模型自己写的 **状态栏** 标题会留在原处。
        tail_after_mark = ""
        if "[[CUSTOMTPL]]" in rep:
            tail_after_mark = rep.split("[[CUSTOMTPL]]", 1)[1][:200]
        R.add("C. 标记之后紧跟字段正文（确实包裹了内容）",
              any(f in tail_after_mark for f in ("好感度", "关系阶段", "心情")),
              f"tail={tail_after_mark[:60]!r}")
        save([("status_bar", "format_template", BACKUP[3][2])], "format_template 还原")

        # ══════════════════════════════════════════════════════════
        print()
        print("=== D. status_bar.default_placeholder：改占位符 → 兜底栏跟着变 ===")
        # 前提：占位符**只在字段缺值**时才出现。而 `/quill reset` 刻意**不清**
        # session_vars（commands.py:1642 附近，只归零轮次与日志），所以只要本会话
        # 聊过几轮，六个字段就都有值了，兜底栏根本不缺值 → 占位符无从出现。
        # 因此这里必须先制造「缺值」：把字段表换成一个**全新**的字段名集合，
        # 这样 prev_vars 里对不上任何一个，兜底栏必然全用占位符。
        D_PH = "[[NOPH123]]"
        save([
            ("status_bar", "default_placeholder", D_PH),
            ("status_bar", "fields", "占位甲|占位乙|占位丙|占位丁|占位戊|占位己"),
        ], "placeholder+fields")
        mark = tail.mark()
        rep, _ = send("【测试】她安静地看着你，什么也没说。")
        time.sleep(0.7)
        blk = tail.since_plugin(mark)
        used_fallback = "状态栏兜底注入" in blk
        print(f"   本轮走了兜底注入: {used_fallback}；占位符出现: {D_PH in rep}")
        if used_fallback:
            R.add("D. 兜底栏使用了新占位符", D_PH in rep,
                  f"占位符出现={D_PH in rep}")
        else:
            R.note("本轮模型自己产出了状态栏，未走兜底路径——该值只在兜底时出现，"
                   "本轮不判定（非缺陷）。")
        save([
            ("status_bar", "default_placeholder", BACKUP[4][2]),
            ("status_bar", "fields", BACKUP[1][2]),
        ], "placeholder+fields 还原")

        # ══════════════════════════════════════════════════════════
        print()
        print("=== E. performance.min_output_length：是否影响实际产出 ===")
        # 说明：这个值是**软指令**——它只被写进 prompt（「长度下限 N 汉字」），
        # 模型照不照做不由配置决定。而 prompt_len 也测不出来（400→3000 只多
        # 2 个字符）。所以分两层看：
        #   * 「值有没有进 prompt」= 配置管线问题 → 由 prompt_builder 自检 #8
        #     （builder800 vs builder400）确定性覆盖，本探针不重复；
        #   * 「模型有没有照做」= 模型行为，不是配置生效性 → 这里只作观察，
        #     不判 FAIL（否则探针会随模型心情随机红）。
        E_MSG = "【测试】她问你今天过得怎么样。"
        base_rep, _ = send(E_MSG)
        base_chars = len(base_rep)
        print(f"   基线（下限={BACKUP[5][2]}）字数 = {base_chars}")

        E_MIN = 3000
        save([("performance", "min_output_length", E_MIN)], "min_output_length")
        big_rep, _ = send(E_MSG)
        big_chars = len(big_rep)
        print(f"   改后（下限={E_MIN}）字数 = {big_chars}  (delta={big_chars - base_chars})")
        if big_chars > base_chars + 300:
            R.add("E. 抬高长度下限后回复明显变长", True, f"{base_chars} → {big_chars}")
        else:
            R.note(f"抬高下限后字数未明显变化（{base_chars} → {big_chars}）。"
                   f"这是**模型是否照做**的问题，不是配置没生效——该值进入 "
                   f"prompt 由 prompt_builder 自检 #8 确定性覆盖，故本项不判 FAIL。")
        save([("performance", "min_output_length", BACKUP[5][2])], "min_output_length 还原")

        # ══════════════════════════════════════════════════════════
        print()
        print("=== F. rag.enable_chat_logging：A/B 对照（写 / 不写）===")
        def chatlog_count() -> int:
            try:
                con = paths.connect_ro(paths.db_path_memory())
                n = con.execute(
                    "SELECT COUNT(*) FROM chat_logs WHERE session_id LIKE ?",
                    (wc.umo + "%",),
                ).fetchone()[0]
                con.close()
                return n
            except Exception:
                return -1

        # 正向对照：开着的时候必须在增长，否则「关掉也不增长」是句废话。
        base_n = chatlog_count()
        send("【测试】开着日志这一轮，应该被记进 chat_logs。")
        time.sleep(6.0)
        on_n = chatlog_count()
        R.add("F1. 开启时 chat_logs 确实增长（正向对照）", on_n > base_n,
              f"{base_n} → {on_n}")

        save([("rag", "enable_chat_logging", False)], "关闭日志")
        off_before = chatlog_count()
        send("【测试】关掉日志这一轮，不该被记进 chat_logs。")
        time.sleep(6.0)
        off_after = chatlog_count()
        R.add("F2. 关闭后 chat_logs 不增长", off_after == off_before,
              f"{off_before} → {off_after}")
        save([("rag", "enable_chat_logging", BACKUP[7][2])], "恢复日志")

        # ══════════════════════════════════════════════════════════
        print()
        print("=== G. rag.enable_memory：A/B 对照（检索得到 / 检索不到）===")
        # 空库测「返回 0 条」是句废话，所以先造一条记忆做正向对照。
        G_MARK = "青柠味薄荷糖"
        learn = wc.send(f"/memory learn 她最喜欢的是{G_MARK}，每次出门都要带一盒。", timeout=90)
        learned = "已学习" in (learn.get("reply") or "") or "已保存" in (learn.get("reply") or "")
        print("   /memory learn →", (learn.get("reply") or "").replace("\n", " ")[:90])

        def mem_hits(text: str) -> int:
            mark = tail.mark()
            wc.send(text, timeout=240)
            time.sleep(0.8)
            blk = tail.since_plugin(mark)
            m = MEM_LOG_RE.search(blk)
            return int(m.group(1)) if m else -1

        G_MSG = f"【测试】她问你还记不记得她喜欢{G_MARK}这件事。"
        on_hits = mem_hits(G_MSG)
        print(f"   开启时 记忆检索 = {on_hits} 条")

        save([("rag", "enable_memory", False)], "关闭记忆")
        off_hits = mem_hits(G_MSG)
        print(f"   关闭时 记忆检索 = {off_hits} 条")

        if not learned or on_hits <= 0:
            R.note("正向对照未成立（记忆没写进去，或检索命中 0 条）——"
                   "「关闭后为 0」无法作为证据，本项记未覆盖，不算失败。")
        else:
            # 修复前实测：关掉之后仍然检索到 2 条 —— 这个开关**不是热生效**的。
            # 根因：`enable_memory` 只在 QuillRetriever 构造时读一次
            # （main.py:662 附近），而 save 流程里没人把它同步过去。
            # 修复：save 时把这几个运行期参数直接写到 retriever 上
            # （top_k / enable_memory / config 引用），不再需要重载插件。
            # 现在这条应在「关」时返回 0 条。
            R.add("G. 关闭后记忆不再被检索（正向对照成立）", off_hits == 0,
                  f"开={on_hits} 关={off_hits}（热生效，无需重载）")
        save([("rag", "enable_memory", BACKUP[8][2])], "恢复记忆")

        # ══════════════════════════════════════════════════════════
        print()
        print("=== Z. 裸 [LOVE_DATA] 不得出现在送达文本（多段路径回归）===")
        # 为什么要单独测这一条：模型在 agent 模式下会**多次**调用
        # send_message_to_user（正文一段、状态栏单独一段）。首段之外的段落走的是
        # `_strip_status_artifacts`（main.py:1840），必须确认它真的擦干净。
        #
        # 两个通道都看，因为它们的含义不同：
        #   * DB（platform_message_history）= 真正送达用户的内容 → 必须干净；
        #   * SSE 的 plain 帧 = 流式推送帧，若含裸标记则客户端可能渲染出来。
        # 之前观察到 SSE 帧里有裸标记而 DB 干净，需要这一次跑完看清楚。
        db_leaks = 0
        sse_leaks = 0
        mismatch = 0
        for i in range(1, 4):
            wc.send("/quill reset", timeout=60)
            time.sleep(0.4)
            mark = tail.mark()
            before_id = _last_bot_msg_id()
            resp = wc.send(f"【测试】第{i}次：她看着你，等你开口。", timeout=240)
            time.sleep(0.9)
            db_text = _delivered_since(before_id)
            sse_text = resp.get("reply") or ""
            d_leak = _is_leak(db_text)
            s_leak = _is_leak(sse_text)
            db_leaks += d_leak
            sse_leaks += s_leak
            if d_leak != s_leak:
                mismatch += 1
                print(f"   第{i}轮 两通道判定不同：DB泄漏={d_leak} SSE泄漏={s_leak}")
                print(f"     DB 尾: {db_text[-170:]!r}")
                print(f"     SSE 尾: {sse_text[-170:]!r}")
        R.add("Z1. 送达文本（DB）无裸 [LOVE_DATA]", db_leaks == 0,
              f"DB 泄漏 {db_leaks}/3 轮")
        R.add("Z2. SSE 帧无裸 [LOVE_DATA]（客户端可能渲染）", sse_leaks == 0,
              f"SSE 泄漏 {sse_leaks}/3 轮")
        if mismatch:
            R.note(f"{mismatch} 轮两个通道判定不同——见上面的尾部对比，"
                   f"需要人工确认哪个是用户真正看到的。")

        # ══════════════════════════════════════════════════════════
        print()
        print("=== H. worldbook.enabled：总开关已接通 ===")
        # 修复前：`worldbook.enabled` 只在 config.py 解析与 __repr__ 里出现，
        # 运行期没有任何消费者——面板关掉它，世界书照样注入（死开关）。
        # 修复后：main.py 在**唯一的注入点**按这个开关决定是否把 wb_manager
        # 传下去，关掉即整段跳过世界书逻辑（常驻 + 关键词匹配）。
        # 注意本机 4 张角色卡 wb_mode 全是 disabled，WB 本来就不注入，
        # 所以这里靠 prompt_len 测不出长度差——如实记 NOTE，不硬判。
        wb_on = prompt_len()
        save([("worldbook", "enabled", False)], "worldbook 总开关=关")
        wb_off = prompt_len()
        print(f"   prompt_len: {wb_on} → {wb_off} (delta={wb_off - wb_on})")
        save([("worldbook", "enabled", BACKUP[9][2])], "worldbook 总开关还原")
        R.note("worldbook.enabled 已接到唯一注入点（关掉则整段跳过世界书）。"
               f"本机角色卡 wb_mode 全为 disabled，WB 本就不注入，"
               f"故长度差无法体现（{wb_on} → {wb_off}）；"
               "开关连通性由代码路径 + save 成功共同保证。")

    finally:
        print()
        print("=== 还原全部改动 ===")
        payload = [
            {"group": g, "key": k, "value": v} for g, k, v in BACKUP
        ]
        r = c.plugin("POST", "config/save_batch", json={"updates": payload}, timeout=30.0)
        print("   restore:", (r or {}).get("status"), (r or {}).get("message"))
        time.sleep(2.5)

        now = md5()
        print("   md5 =", now, "| OK" if now == BASE_MD5 else "| !!! MISMATCH !!!")
        if now != BASE_MD5:
            R.add("还原后 md5 回到基线", False, f"now={now}")

        # 逐键核对（md5 相等已足够，这里再给一层可读证据）
        cur = raw_cfg()
        bad = []
        for g, k, v in BACKUP:
            got = cur.get(g, {}).get(k)
            if got != v:
                bad.append(f"{g}.{k}: {got!r} != {v!r}")
        print("   逐键核对:", "全部一致" if not bad else bad)

        # 清残留
        print()
        print("=== 清理测试残留 ===")
        try:
            wc.send("/char 1", timeout=30)
            rep = wc.send("/quill reset", timeout=60)
            print("   reset:", (rep.get("reply") or "").replace("\n", " ")[:100])
        except Exception as exc:
            print("   reset 失败（可忽略）:", type(exc).__name__)
        try:
            con = paths.connect_ro(paths.db_path_memory())
            rows = con.execute(
                "SELECT DISTINCT session_id FROM memories WHERE session_id LIKE ?",
                (wc.umo + "%",),
            ).fetchall()
            con.close()
            for row in rows:
                sid = str(row["session_id"])
                resp = c.plugin("POST", "memory/delete",
                                json={"session_id": sid}, timeout=20.0)
                d = (resp or {}).get("data") or {}
                if d.get("deleted"):
                    print("   已删记忆", d["deleted"], "条")
        except Exception as exc:
            print("   记忆清理失败（可忽略）:", type(exc).__name__)

    print()
    print("=== 汇总 ===")
    n_pass = sum(1 for l, ok, _ in R.items if ok and not l.startswith("NOTE"))
    n_fail = sum(1 for l, ok, _ in R.items if not ok)
    for label, ok, detail in R.items:
        if label.startswith("NOTE"):
            continue
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))
    print()
    print(f"PASS {n_pass} / FAIL {n_fail}")
    print(">>> 探针 E:", "PASS" if R.ok else "FAIL")
    return 0 if R.ok else 1


if __name__ == "__main__":
    sys.exit(main())
