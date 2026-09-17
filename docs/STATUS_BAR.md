# 状态栏功能运作逻辑剖析

> 基于当前代码（v5.2.4 工作区）逐行核对写成，行号对应 `main.py` / `prompt_builder.py`。
> 本文只描述**现有实现**，不含改进建议。
> 注：六级降级链已在 v5.2.4 重构为分级注册表（见「九之五」），
> 行号随之变动；引用具体行之前建议先按符号名搜索。

---

## 一、总览：它是"提示词契约 + 六级降级解析 + 双路径输出"的闭环

```
                   ┌─────────────────────────────────────────┐
   写侧（提示词）    │ prompt_builder.build_status_bar_guide()  │
                   │ 告诉 LLM：用 [LOVE_DATA] 单行 | 分隔       │
                   └──────────────────┬──────────────────────┘
                                      │ LLM 输出正文 + 状态栏
                                      ▼
                   ┌─────────────────────────────────────────┐
   读侧（解析）      │ _handle_status_bar() 六级降级             │
                   │ L1 code block → L2 [LOVE_DATA] → L3 [STATUS]│
                   │ → L4 裸字段行 → L5 宽松 → L6 LLM 提取      │
                   └──────────────────┬──────────────────────┘
                                      │ 得到 updates + 渲染后的文本
                                      ▼
                   ┌─────────────────────────────────────────┐
   存侧（状态）      │ _persist_status_vars() → session_vars    │
                   │ 下一轮经 extra_info 注入 system prompt     │
                   └─────────────────────────────────────────┘
```

**关键设计**：状态栏是**纯文本契约**，不是结构化协议。LLM 用自然语言写，插件用正则读，读不到就逐级降级。这决定了它的全部行为特征 —— 包括它的脆弱点。

---

## 二、写侧：LLM 是怎么被要求输出状态栏的

**位置**：`prompt_builder.py:600` `build_status_bar_guide()`

它在 system prompt 的 **Layer 0（priority=0，永不被截断）** 注入，
只在 `status_bar.enabled=true` 时生效（`prompt_builder.py:143-149`）。

契约内容：

| 项 | 要求 |
|---|---|
| 格式 | `[LOVE_DATA] {好感度} \| {关系阶段} \| {心情} \| {位置} \| {穿着} \| {当前想法}` |
| 位置 | 故事正文**之后**，单行，管道符分隔 |
| 分隔符 | 只能用 `\|`，值内不得含管道符 |

提示词里同时给了：
- **3 个负例**（缺标签 / 用 `→` 代替直接写值 / 换行输出 / 代码块）
- **3 个正例**（初遇 15/100、日常 55/100、亲密 85/100）
- 好感度分档参考（0-20 陌生人 … 91-100 深爱）
- 一句话豁免：`好感度只是参考字段名，可替换为其他可量化指标`（催眠度/服从度等）

**这里埋着第一个结构性事实**：契约用 `[LOVE_DATA]` 定义，但解析器支持 6 种格式。
也就是说 —— **解析器比契约宽容得多**。这是刻意的（见下节）。

另外 `prompt_builder.py:546-550` 显示 `send_message_to_user` 的调用指南也复述了这条契约，
并区分「工具模式」与「纯文本模式」：工具模式要求把正文与状态栏**打包进同一个 text 字段**；
纯文本模式则**明令禁止**输出任何状态栏（由插件自己走兜底）。

---

## 三、读侧：六级降级链（核心）

**入口**：`main.py:1200` `_handle_status_bar(text, target_id) -> (new_text, updates, handled)`

函数开头先读一次 `prev_vars`（`session_vars` 的当前值），整个调用只读这一次 ——
既用于变化标注，也用于 L4/L5 的缺失字段补齐。

### L1 — `**状态栏**` 代码块（`main.py:1240`）

正则：`_STATUS_BLOCK_RE = /r'\*\*状态栏\*\*[\s\S]*?```([\s\S]*?)```'/`（`main.py:108`）

- 抓 `**状态栏**` 后面第一个 ``` 围栏内的全部内容
- 交给 `_parse_status_block()`（`main.py:1633`）：逐行按首个 `：` / `:` / `=` 切成 KV
- 含 `>>>`/`<<<` 的行跳过（那是剧情选项，单独处理）
- **输出时只替换 group(1) 的 span**，首尾空白原样带回 —— 否则围栏会与内容挤到同一行、代码块失效
- `updates` 非空才算 handled

顺带一提：L1 恰恰是 `build_status_bar_guide()` 明确列为「**错误示例**」的格式
（❌ ` ```\n好感度：85\n``` `）。契约与实现不一致，但解析器照样接受。

### L2 — `[LOVE_DATA]` 单行（`main.py:1258`）

正则：`_LOVE_DATA_RE = /r'\[LOVE_DATA\]\s*(.+)'/`（`main.py:107`）

- 由 `_format_love_data()`（`main.py:1515`）处理
- 按 `|` 切段，按 `love_fields` 顺序**位置映射**（第 i 段 → 第 i 个字段）
- 段数不足：补空串；段数超出：丢弃（`updates` 长度恒等于 `love_fields` 长度）
- 用 `text.replace(raw_line, formatted)` 替换原文

**这是契约指定的主路径**，正常情况下走这里。

### L3 — `[STATUS]...[/STATUS]` 遗留格式（`main.py:1275`）

正则：`_STATUS_RE`（`main.py:106`）；解析：`_parse_legacy_status()`（`main.py:1532`）

- **只认 `=`**（不认冒号），逐行切 KV
- 命中后用 `status_bar_format_template` 渲染，再 `_STATUS_RE.sub()` 替换

这是为老版本兼容保留的路径。

### L4 — 裸 `字段：值` 行（`main.py:1287`）

正则：`_build_raw_status_re(self.love_fields)`（`main.py:120`），动态构建。

**这一级的正则承担双重职责**（检测 + 删除共用同一个 pattern），设计约束写在
`main.py:127-129` 的 docstring 里：

| 约束 | 原因 |
|---|---|
| 行首锚定 `(?:^\|\n)` 且消费前导换行 | 让 `re.sub` 能整行干净移除，不留空行 |
| 值上限 `[^\n]{1,30}` | 状态值是短文本；行首的「字段：长句」更可能是叙事，**宁可漏检也不误删正文** |
| `[^\S\n]` 作空白类 | 覆盖全角空格等 Unicode 空白，但不跨行 |
| 分隔符 `[：:=→]` | 兼容 `好感度→85` 等写法 |

流程：
1. 先 `re.sub` 去掉 `---` 分隔线（**仅用于解析**，不改输出文本）
2. `findall` 取所有匹配 → 填充 `updates`
3. 对比上一轮、算出 changed、补齐未出现的字段
4. `raw_re.sub('', new_text)` 对称删除（与检测同一正则）
5. 拼 `block_content`，套模板，追加到正文末尾

**这一级是实际生产中最常走的路径** —— 因为 LLM 常忽略 `[LOVE_DATA]` 而直接写
`好感度：85/100`。实测日志里 `状态栏已处理 (raw key:value, 动态字段)` 出现频率最高。

### L5 — 宽松扫描（`main.py:1345`）

`_lenient_parse_status()`（`main.py:1099`）：

- 正则按 `(?:^|\n)` 抓「像 KV 的行」，字段名与 `love_fields` 做**双向包含**匹配
  （`if lf in key or key in lf`）—— 所以 `好感：65` 也能认到 `好感度`
- **阈值 `>= 2` 个字段**才返回（`main.py:1099` 函数内、返回前判定），否则返回 `{}`
- 命中后走「部分提取 + 历史值融合」：取到的用新值，没取到的用上一轮值或占位符

### L6 — LLM 智能提取（`main.py:1273`）

`_llm_extract_status()`（`main.py:1565`），需 `status_bar_llm_extract=true`。

守卫条件（`main.py:1575-1449`）：文本中必须出现 **≥2 个字段关键词**才触发，
否则直接 `None`。三重风险控制：

- 3 秒超时，失败即放弃
- JSON 解析失败即放弃
- 字段名白名单过滤（仅保留 `love_fields` 内的）

用独立 provider（`status_bar_llm_provider_id`，留空回退到 RAG 摘要 LLM）。

### 全部失败

`main.py:1351` 记 INFO 日志（带 200 字预览，**不暴露给用户**），
然后由上层走兜底（见下节）。

健康度：`main.py:1398` `health_tracker.record_status(handled)` 记成功率 ——
这就是面板「状态栏解析成功率」那行的数据源。

---

## 四、变化标注（本次新增）

**位置**：`main.py:168-243` 四个模块级函数 + 在每个分支的渲染点调用。

### 数据流

```
prev_vars（本轮开头读一次）
        │
        ├─→ _mk_changed(updates)          算出 {字段: 旧值}
        │         │
        │         ├─ 先 _normalize_status_value 剥掉旧标注
        │         ├─ 两侧都非空且不等
        │         └─ 新值 != 占位符
        │
        └─→ _annotate_changes(content, changed)   重写正文
                  │
                  ├─ 逐行匹配 ^(字段)[：:](值)$
                  ├─ 先剥净值尾部旧标注（_DELTA_MARK_RE）
                  └─ 仅当字段在 changed 里，才追加 _format_delta 的标注
```

### 三个关键设计决策

**1. 标注语法用 `（↑5）` 而非裸 `↑`**

理由写在 `main.py:168-167`：裸箭头会与合法值混淆（`心情：上升↑` 是模型可能写出的正常值），
而且标注随回复回显进下一轮上下文、模型会模仿 —— 裸箭头一旦被模仿就在值里逐轮累积
（`70 ↑ ↑ ↑`）。括号语法可被精确剥离。

**2. 文本字段不给标注**（`main.py:210-215`）

`_format_delta` 只在**两侧都能解析出数字**时才返回标注，否则返回空串。

> 状态栏里多数字段是自由文本（心情、穿着、当前想法），它们每轮都在变 ——
> 「当前想法」本来就该换。给这类字段挂箭头不传达任何信息，还会让有价值的
> 数值变化淹没在噪声里。

这是实测后的修正：初版给所有变化字段挂空箭头 `（↑）`，实际跑起来满屏都是箭头。

`_extract_numeric()`（`main.py:187`）只认**以数字开头**的形态：
纯数字、`65/100`、`3 级`、`80%`。刻意不做「从任意位置抠数字」——
`好感度很高` 取不到值是对的。

**3. 落库前必须归一化**（`main.py:1401-1252`）

```python
persist_updates = {
    k: _normalize_status_value(v)
    for k, v in updates.items()
    if not k.startswith("_") and isinstance(v, str)
}
```

标注是我们渲染进消息文本的，会随 assistant 回复回显进下一轮上下文。
不在解析侧剥掉，模型照抄一次就会 `70（↑5）（↑5）…` 逐轮累积，
并经 `prompt_builder` 注入 system prompt 污染模型输入。

这条有双重锁定：fixture 用例 `t12_delta_roundtrip`（往返闭合）
+ harness 端到端 `status_delta_no_drift`（断言 session_vars 无标注残留）。

### 实测效果

```
好感度：94/100（心跳同频，藏不住的爱意）（↑6）
关系阶段：亲密恋人
心情：甜蜜到发晕（刚把心里话全说出来了）
位置：度假村海滩浅水区
```

只有可量化的「好感度」带标注，文本字段干净。

---

## 五、存侧：session_vars 与下一轮

**写入**：`_persist_status_vars()`（`main.py:1418`）→ `state_manager.update_session_vars()`

- 所有分支**统一在函数末尾提交一次**（`main.py:1401`），消除多次独立 await 的竞态
- 以 `_` 开头的键被过滤掉（历史上 `_changed` 曾被误写进去，污染过提示词）
- `state.py` 侧有 64KB 上限，超了按插入顺序淘汰最早的键

**读取/注入**：`main.py:2177` 把 `session_vars` 放进 `extra_info`，
`prompt_builder.py:151-158` 拼成 `## 当前状态\n字段=值 | 字段=值`，
以 priority=0（永不被截断）进 system prompt。

**这就形成了闭环**：本轮解析出的值 → 下一轮作为「当前状态」告诉 LLM →
LLM 在此基础上增减 → 再被解析。所以状态栏的数值是**累积演进**的，不是每轮独立采样。

---

## 六、双输出路径（决定用户看到什么）

`_handle_status_bar` 被调用两次，覆盖两种 LLM 输出方式：

### 路径 A：工具模式（`main.py:1815`，`on_using_llm_tool`）

LLM 调用 `send_message_to_user`，正文在 `tool_args.messages[].text` 里。

- 遍历 `messages`，对**首条** plain 消息执行 `_handle_status_bar`
- 之后的消息只做 `_strip_status_artifacts`（清理残留标记）
- 命中则 `event.set_extra("_quill_status_handled", True)` 打标记

### 路径 B：纯文本模式（`main.py:2064`）

- 若 `_quill_status_handled` 已置位 → 仅剥离残留（避免重复处理）
- 否则执行 `_handle_status_bar`，**未命中时兜底**：

```python
if not handled:
    persona_id = await self.state_manager.get_persona_id(target_id)
    if persona_id:                                    # 仅在绑定了角色卡时兜底
        default_bar = await self._build_default_love_data(target_id)
        new_text = (new_text or "") + "\n" + default_bar
```

`_build_default_love_data()`（`main.py:1544`）用 `session_vars` 的历史值 +
占位符拼一个状态栏，剧情选项用配置里的固定三项。

**这么做的意义**：保证用户**每一轮都看得到状态栏**，即使 LLM 这轮没输出。

### 关于「终止 agent loop」：注释与框架实际行为不符

`on_llm_tool_respond` 的 docstring（`main.py:2376`）写着「检测
send_message_to_user 调用，终止 agent loop」，但函数体只做记忆与日志，
**没有任何终止动作**，`main.py` 里也搜不到 `agent_stop_requested`。

框架侧（AstrBot 4.28.0）能让循环停下的只有两条路：

1. **工具返回 `None`** —— `tool_loop_agent_runner.py:1294-1301` 把 `resp is None`
   当作「工具已直接发给用户」，转 `AgentState.DONE`。内置
   `SendMessageToUserTool.call` 返回的是字符串 `f"Message sent to session …"`
   （`core/tools/message_tools.py:362`），走不到这条分支。
2. **`event.set_extra("agent_stop_requested", True)`** ——
   `astr_agent_run_util.py:26-27` 的 `_should_stop_agent` 会读它并
   `request_stop()`。插件没有设置。

`_has_send_oper`（`message_tools.py:348` 设置）只用于
`pipeline/process_stage/stage.py:57` 判断是否跳过整个 LLM 阶段，
**runner 不读它**，所以它也不能终止循环。

**实测结论（WebChat，绑定角色卡，多轮）**：单条用户消息产生的
`agent_stats` 事件恒为 1，`plain` 正文段恒为 1，即**循环实际只跑了一轮**。
原因是最新一次 `on_llm_request` 返回后 LLM 直接给出最终回复（无工具调用），
走 `_complete_with_assistant_response` → `AgentState.DONE`，
而不是走「工具已发送仍需再问一轮」那条路。

**结论**：docstring 那句话不准确（机制上确实没有终止代码），但在当前
提示词契约下没有产生实际的重复轮次。若将来观察到同一条消息出现两段正文
或第二次 LLM 调用，正确的修法是在 `on_llm_tool_respond` 里设
`event.set_extra("agent_stop_requested", True)`——注意它会把该 run 标记为
`aborted`，而 `internal.py:483/503/515` 对 `user_aborted=True` 有分支差异
（跳过部分历史保存），不是无条件安全的改动。

---

## 七、剥离器：另一套独立正则

`_strip_status_artifacts()`（`main.py:1078`）+ `_STRIP_PATTERNS`（`main.py:1047`，6 条）

用于两种场景：
1. `status_bar.enabled=false` —— 彻底擦除所有状态栏痕迹
2. dedup 清理 —— 首条之后的消息里可能有残留

这 6 条是**与字段名无关**的部分（靠 `[LOVE_DATA]`/`**状态栏**` 这类标签识别）；
裸字段行那条由 `_strip_bare_fields_re(self.love_fields)` 按当前配置动态构建，
因此**与解析侧的字段来源已经统一**。

### 字段名统一了，但值长上限刻意保持不对称

| | 值上限 | 理由 |
|---|---|---|
| 解析侧 `_build_raw_status_re(fields)` | `{1,30}` | 行首的「字段：长句」更可能是叙事，宁漏检不误删正文 |
| 剥离侧 `_build_raw_status_re(fields, max_value_len=None)` | 不限 | 关闭状态栏时要保证不漏，长值行也必须擦掉 |

这个不对称是**设计而非遗漏**：把两边长度统一会让长值裸字段行漏到用户屏幕上。
唯一改动的是字段名来源（此前剥离侧写死 8 个字段名），现在两边都由
`love_fields` 生成，并带 32 项 LRU 上限的编译缓存（剥离是每条消息的热路径）。

---

## 七之二、关闭状态栏时的三处一致性（本轮修复）

关闭开关后，以下三处此前**没有跟着切换**，属于「一边禁止、一边示范」的自相矛盾。

### 1. `session_vars` 注入已加门控（`prompt_builder.py:154`）

关闭状态下 tail message 明令「禁止输出好感度、关系阶段、心情」，但
system prompt 仍持续注入 `## 当前状态\n好感度=85 | 心情=开心`。模型很可能
照着注入的字段名输出「类状态栏」内容，然后被剥离器擦掉——白耗 token，
且与「关闭即干净」的用户预期相悖。

现在 `if session_vars and self.status_bar_enabled:` 才注入。**只停止注入、
不清空 `session_vars`**：重新打开时旧状态还在，这是有意的连续性体验。

### 2. 历史 `contexts` 里的状态栏已抹除（`main.py:2057`）

不清对话切换开关时，历史 assistant 消息里的已渲染状态栏仍在 `req.contexts`
里。关闭后模型看几轮「好感度：85」的历史会倾向于模仿——可见性由读侧剥离
兜住，但这是在「禁止输出」与「历史示范」之间拉锯。

处理位置紧跟在已有的 `_scrub_inject_report` 之后，且**必须在上下文恢复
（`recent_logs` 垫入）之后**——恢复来的历史同样带状态栏。
开启方向不处理：历史里本来就没有栏，tail message 会教它写。

### 3. 剥离器字段动态化（见上节）

---

## 七之三、字段名：插件自己在教模型用白名单外的名字

`prompt_builder.build_status_bar_guide()` 里有一行：

> 好感度只是参考字段名，可根据剧情需要替换为其他可量化指标，如：**催眠度、服从度、淫乱度、信赖度**等

而剥离器此前只认 8 个硬编码字段名（`好感度|关系阶段|心情|位置|穿着|当前想法|服从度|发情度`）——
「催眠度」「信赖度」「淫乱度」都不在其中。也就是说，**不需要用户自定义配置**，
模型照着插件自己的建议写，关闭状态栏时裸字段行就会原样漏到屏幕上。

这处不一致现已随字段动态化一并消除，并有 fixture `t15` 固定行为。

---

## 七之四、L5 阈值：维持 ≥2（CHANGELOG 的历史声明是错的）

`_lenient_parse_status`（`main.py:1099`）末尾是 `return updates if len(updates) >= 2 else {}`，
而 CHANGELOG v5.0.2 第 696 行曾宣称「L5 阈值从 ≥2 降为 ≥1」。**该降级从未生效**：
调用点当时写的是 `if lenient_updates and len(lenient_updates) >= 1`，
其中 `>= 1` 是恒真判断（空 dict 已被 `if lenient_updates` 挡掉）。

现已把注释、CHANGELOG、文档统一到「维持 ≥2」。理由是维持 ≥2 才对：
L5 只在 L1–L4 全败后运行，此时「命中 1 个字段就重建整栏」会让其余字段
全用历史值/占位符补齐——等于拿叙事里一句「心情：……」造出一整栏陈旧状态。

**已知裂缝**（有意保留，非缺陷）：L4 只漏 1 个字段时，L5 的 ≥2 不会兜底
（命中 1 个即返回空），该区间最终走默认状态栏兜底。

---

## 八、剧情走向（状态栏的附属输出）

**正则**：`_PLOT_PATH_RE`（`main.py:109`）

```python
/[>|]{2,}\s*(?:Plot\s*Paths|剧情走向|剧情选项)\s*[|<]{2,}\s*(.+?)\s*[|<]{2,}\s*(?:Select|请选择|选择)\s*[>|]{2,}/
```

- 在 L4 分支里被单独 `search`（`main.py:1321`），命中后从正文删除、重新拼成
  `>>> 剧情走向 <<<\n{内容}\n<<< 请选择 >>>` 追加到状态栏块末尾
- 中英文标记都认（`剧情走向` / `Plot Paths`）
- 兜底路径下用配置的 `plot_paths` 生成 `1. xxx\n2. xxx\n3. xxx`

**当前没有交互机制**：选项只是文本，用户要自己打字。`main.py` 里没有任何
按键/序号解析逻辑。

---

## 九、配置项全貌

`_conf_schema.json` 的 `status_bar.items`（10 项）：

| 键 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关，关闭则走剥离器；可被会话级 `/quill statusbar` 覆盖 |
| `fields` | text | `好感度\|关系阶段\|心情\|位置\|穿着\|当前想法` | 字段表，`\|` 分隔，至少补足 6 个 |
| `format_template` | text | `**状态栏**\n```\n{content}\n```\n` | Markdown 平台渲染模板，`{content}` 占位 |
| `format_template_plain` | text | `───── 状态栏 ─────\n{content}\n────────────────` | 纯文本平台模板（本次新增） |
| `plain_platforms` | text | `""`（用内置列表） | 按纯文本渲染的适配器名，`\|`/`,` 分隔（本次新增） |
| `plot_paths` | text | `继续当前话题\|转换场景\|结束互动` | 兜底时的选项 |
| `llm_extract` | bool | `false` | 启用 L6 |
| `llm_provider_id` | string | `""` | L6 用的模型，留空回退 |
| `default_placeholder` | text | `未设置` | 字段缺值时的占位 |
| `show_delta` | bool | `true` | 变化标注开关 |

`fields` 会被 `config.py` 解析，**不足 6 个自动补空串** ——
所以 `_format_love_data` 的位置映射永远有 6 个槽位。

---

## 九之二、平台分治与模板选型（本次新增）

状态栏最终是「模板 + 内容」拼出来的，而默认模板是 Markdown
（`**状态栏**` + 围栏）。QQ/微信这类平台不渲染 Markdown，用户会看到
`**状态栏**` 和 ``` 原样显示。所以按平台选模板：

```
平台名 ∈ status_bar_plain_platforms  →  format_template_plain
其它（含未知平台）                    →  format_template
```

- 平台名取自 `event.platform_meta.name`（回退 `get_platform_name()`），
  在 `_resolve_platform_name()`（`main.py:1172`）里做大小写归一。
- 选型在 `_status_bar_template_for()`（`main.py:1188`）。
- **未知平台走 Markdown 模板**是刻意的：它可能是支持 Markdown 的新适配器，
  贸然改成纯文本反而破坏渲染。
- 模板作为参数传进 `_handle_status_bar(..., bar_template=)`，而不是在里面
  回头去问 `event`；`_build_default_love_data()` 同样接模板参数，保证
  「正常轮次」与「兜底轮次」排版一致。

内置的纯文本平台默认列表在 `config.py`：aiocqhttp、qq_official、
qq_official_webhook、wecom、wecom_ai_bot、weixin_oc、
weixin_official_account、dingtalk、line。

> **同步修复的一处长期缺口**：L2（`[LOVE_DATA]` 单行）此前**不套模板**，
> 只把裸字段行替换进正文。而 guide 恰好要求模型输出这个格式（还把代码块
> 列为**错误**示例），所以 L2 才是实际最常命中的路径——实测 40 次解析里
> **36 次走 L2**。后果是 `format_template` 配置在整个子系统的主力路径上
> 从未生效，平台分治也会对 L2 无效。现已在 L2 分支套用模板
> （`main.py:1258-1273`），fixture `t23` 固定该行为。

---

## 九之三、会话级开关 `/quill statusbar on|off|auto`（本次新增）

面板开关是**全局**的；聊天端指令是**会话级覆盖**：

| 会话设置 | 语义 |
|---|---|
| `auto`（默认） | 跟随面板全局开关 |
| `on` | 本会话强制开（面板关着也生效） |
| `off` | 本会话强制关（面板开着也关闭） |

解析顺序见 `_effective_status_bar_enabled()`（`main.py:1130`）：
**会话覆盖 > 面板全局**。存储字段是 `UserState.status_bar_mode`
（`state.py`），随状态文件持久化，重启仍有效。

**为什么用浅拷贝而不是改共享实例**（`_prompt_builder_for_request()`，
`main.py:1153`）：`self.prompt_builder` 是共享的，直接改它的
`status_bar_enabled` 会让并发请求互相污染（A 会话设 on 影响 B 会话）。
拷贝只复制属性引用，PromptBuilder 不持有连接/任务，成本可忽略。

**必须用同一个值的三处**（否则自相矛盾）：
1. system prompt 的契约文本（`build_status_bar_guide` /
   `build_send_message_guide` / `build_safety_wrapper` 三处都读该开关）
2. tail message（开启 → 一行式提醒；关闭 → 禁止清单）
3. 历史 `contexts` 的状态栏清理（关闭时才清）

`_handle_status_bar` 内部不再看全局开关：它只在「本轮最终开关为开」时
被调用，内部再判一次会让 `/quill statusbar on` 覆盖全局关时误跳过。

---

## 九之四、格式契约单一来源（本次新增）

`PromptBuilder.build_status_contract()`（`prompt_builder.py`）是格式契约的
**唯一来源**，生成：格式行、示例行、字段说明、剧情选项块、禁止清单。

四处消费方不再手抄示例：

| 消费方 | 取用 |
|---|---|
| `build_status_bar_guide()` | 完整契约（格式行 + 示例 + 逐字段说明） |
| `build_send_message_guide()` 步骤3 | 开启时提要求；关闭时用 `forbidden_hint` |
| `build_safety_wrapper()` | 格式行 + 示例 + 选项标记（就近提醒） |
| `main.py` tail message | `build_status_reminder()` 一行式提醒 + 完整选项块 |

**动机**：四处各写一份示例，字段名或顺序一变就漂移；而字段名用户可配。
现在改 `fields` 会同时传导到四处（fixture `t18` 断言这一点）。

好感度专属的两段（「只是参考字段名」的自述、0-100 阶段参考表）**只在
字段表里真含「好感度」时生成**——用户已经把字段改掉还说「好感度只是
参考字段名」是自相矛盾。

---

## 九之五、降级链重构为分级注册表（本轮）

旧写法是六个 `if not handled:` 顺序嵌套，能跑但读不出意图，也统计不出
「哪一级真的在干活」。现改为注册表驱动：

```python
_SB_LEVELS = (
    ("code block",             "_sb_l1_code_block"),
    ("LOVE_DATA inline",       "_sb_l2_love_data"),
    ("STATUS legacy",          "_sb_l3_legacy"),
    ("raw key:value, 动态字段", "_sb_l4_raw"),
    ("lenient + 部分提取",      "_sb_l5_lenient"),
    ("LLM 智能提取",            "_sb_l6_llm_extract"),
)
```

驱动器按顺序调用，每级返回 `_StatusLevelResult` 或 `None`。

**核心语义：`terminal` 区分两种「命中」**

| 值 | 含义 | 谁在用 |
|---|---|---|
| `True` | 命中即**结束**降级链（产出了可用状态数据） | L1/L2/L3、L4 多命中、L5、L6 |
| `False` | 文本已改写但**继续下降** | **仅 L4 单命中** |

L4 单命中之所以特殊：它擦掉那行可疑的裸字段（否则裸行会漏给用户），
但一行不足以重建整栏，所以要继续让 L5/L6/兜底接手。旧代码靠「不置
`handled`」隐式表达这层意思，现在写明了。

**顺带得到的三件事**

1. **逐级命中率**：`HealthTracker.record_status_level()` 累计每级次数，
   `/quill debug` 会打一行 `降级链命中: LOVE_DATA inline×3`。
   实测确认了「L2 是主力路径」——不用再 grep 日志文本。
2. **单级异常不再掀翻整链**：某级抛异常只记 warning 并继续下降
   （旧写法下任何一级异常都会冒泡，状态栏直接消失）。
3. **可测**：`tests/test_status_bar_parsers.py` 的 `t25` 直接调各级方法
   验证返回值语义，不必构造完整的 LLM 输出。

> 日志字符串**逐字未变**（`状态栏已处理 (LOVE_DATA inline)` 等），
> 因为 fixture 与 harness 有断言依赖它们。级别名与日志括号内容一致，
> 便于对照。

---

## 七之五、裸 `[LOVE_DATA]` 泄漏（本轮修复）

**症状**：模型把回复拆成多次 `send_message_to_user` 时，第二次之后的内容
带着未处理的裸 `[LOVE_DATA]` 直达用户。实测 7 轮里 3 轮复现。

**根因**：`on_llm_tool_respond` 里有一行

```python
event.set_extra("_quill_activated", False)   # 旧代码
```

而 `_quill_activated` 是**整轮的总闸门**，被 `on_using_llm_tool`（状态栏
处理就在这里）和 `on_llm_response` 读取。第一次工具调用后清掉它，
等于宣布「本轮插件下班」——后续工具调用全部绕过插件。

**修法**：拆成两个语义单一的标记。总闸门只表示「本轮是否激活」，
新增 `_quill_memorized` 专门表示「记忆/反思是否已做」，只做一次。

```python
if event.get_extra("_quill_memorized"):
    return
event.set_extra("_quill_memorized", True)
```

**验证**：`docs/probe_no_leak.py`，修复后 6 轮 **0 泄漏**（修复前 3/3）。

> 判据注意：不能只搜 `[LOVE_DATA]` 子串——模型会在正文里**提到**这个
> 标记名（实测抓到一条英文自我修正说明：「I noticed I split the reply ...
> using the `[LOVE_DATA]` and `>>> <<<` markers」）。那是说明不是泄漏。
> 探针用「行首标记 + 竖线分隔值」的严格判据，并自带 7 条正反例自检。

### 根因 B：不调工具的轮次会绕过所有工具钩子

修完上面那处之后，实测**仍能偶发复现**。继续挖才发现是另一个机制：

agent loop **每一轮迭代都会先 `yield` 该轮的 `llm_resp.result_chain`，
然后才走 `_handle_function_tools` 触发工具钩子**
（`tool_loop_agent_runner.py:917` vs `:982`）。所以模型在「这一轮不调用工具、
直接输出文本」时，那段文本会**先于任何工具钩子**被推送——插件没有机会处理。

实测抓到一轮里模型被纠正后连发 18 次 `send_message_to_user`，其中 17 次都被
正常处理，唯独夹在中间那次「直接输出一行裸 `[LOVE_DATA]`」绕过了全部钩子。

**修法**：新增 `on_decorating_result` 发送前兜底钩子
（`core/pipeline/result_decorate/stage.py:158` 触发），拿到最终 MessageChain
再擦一遍。这个钩子位置的好处是：**任何来源**的残留都会在发送前被过一遍。

### 兜底钩子的两档强度（踩坑记录）

第一版兜底直接调 `_strip_status_artifacts`，结果**把正常渲染的状态栏删掉了**——
那个剥离器的第一条模式就是 `**状态栏**[\s\S]*?```[\s\S]*?``` `，
而开启状态栏时，同样的文本正是 L1/L2 的**正常产出**。表现为配置项 A/C 两项
实测由 PASS 变 FAIL，回复里的状态栏整段消失。

现在按本轮开关分两档：

| 本轮开关 | 用哪个剥离 | 理由 |
|---|---|---|
| **开** | `_strip_raw_markers` | 只擦 `[LOVE_DATA]` / `[STATUS]` 两种**原始标记**。渲染产物（Markdown 代码块、纯文本边线、剧情选项块）一律保留 |
| **关** | `_strip_status_artifacts` | 整套剥离。此时渲染过的状态栏本就不该出现，擦掉正是期望行为 |

**`_strip_raw_markers` 刻意不擦「裸字段行」**——这是又一层的坑：
渲染后的状态栏内容与裸字段行在文本上**完全同形**（渲染出来本来就是
`好感度：88` 这样的行），而模板是用户可自定义的
（`format_template` / `format_template_plain` 都能改）。
按「行首裸字段」擦会在自定义模板下误删栏内容——实测用 `[[CUSTOMTPL]]`
这类自定义模板时，整个栏会被掏空只剩空壳。

权衡依据：实测抓到的**全部**泄漏样本都是模型直接输出的 `[LOVE_DATA]` 行
（模型照契约走就会带标记）；裸字段块那种偏离契约的输出，常规路径上的
`on_using_llm_tool` 已在处理。为兜住它而引入「可能误删用户自定义模板内容」
的风险不划算。这条已用 fixture `t26` 锁定（含自定义模板不掏空的断言）。

### 并发钩子一览（谁在什么时机清洗）

| 钩子 | 时机 | 覆盖面 | 力度 |
|---|---|---|---|
| `on_using_llm_tool` | 工具参数写出前 | 只覆盖 `send_message_to_user` 的参数 | 解析 + 渲染 + 剥离 |
| `on_llm_response` | 一轮 LLM 结束时 | 最终 `completion_text` | 剥离 |
| `on_decorating_result` | **发送前** | **最终 MessageChain（全部来源）** | 开：只擦标记 / 关：整套剥离 |

---

## 十、一句话总结各环节的可靠性

| 环节 | 机制 | 可靠性来源 |
|---|---|---|
| LLM 写出状态栏 | system prompt 契约（含正负例） | 靠提示词说服，**无强制力** |
| 解析 | 六级降级 | L4 兜住大多数实际情况 |
| 值可信度 | 白名单字段 + LLM 提取时再校验 | 中 |
| 用户可见性 | 双路径 + 兜底注入 | **高**（保证每轮都有） |
| 跨轮一致性 | session_vars 回灌 prompt | 高（但依赖 LLM 遵守） |
| 数值准确性 | 无校验，LLM 说多少是多少 | 低（设计如此） |

**核心特征**：这是一个「尽力而为」的软契约系统 ——
LLM 不配合时靠降级链和兜底保住用户体验，但**不保证数值正确性**。
