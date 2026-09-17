# 验证计划：对话隔离 + 状态栏四项改动

> 本文是**待执行**的验证清单，供压缩上下文后的新会话按序执行。
> 所有命令都已在本机验证过可跑通；预期结果写的是**实测值**，不是推测。

---

## 五轮执行记录（2026-09-18：审计 8 项缺陷修复）

来源：`.build/union alpha.txt`（独立审计）。逐条核验后确认 **8 项全部真实存在**，
按危害排序修复。**未发现误报**。

| # | 缺陷 | 核验 | 修复要点 |
|---|---|---|---|
| ① | 反思删未参与总结的日志 + 可能清空核心记忆 | **存在，且危害高于原定级** | 按 id 批次删 + 结构校验 + 先写后删 |
| ② | 状态旧快照覆盖新快照 | 存在 | 写锁串行 + 世代号 + 等线程结束 |
| ③ | 向量失败被当成功 | 存在 | 抛异常 + 返回条数 + 上传报错 |
| ④ | RAG 重建无生命周期保护 | 存在 | 等在途任务 + 串行化去重 |
| ⑤ | Retriever 字段未回滚 | 存在（含此前引入的回归） | 保存前三元组备份 + except 还原 |
| ⑥ | Embedding 切换无隔离 | 存在（部分修） | 向量失败降级为纯关键词 |
| ⑦ | 素材库丢弃 FTS 命中 | 存在 | 区分「故障」与「命中不足」+ 合入去重 |
| ⑧ | 检索失败计为成功 | 存在 | `RagResult` 带 `_rag_ok` |

**两项需要修正判断的地方**：

1. **① 比原文档说的更糟**——不只是「可能触发更新」，是会把核心记忆
   **清空**：`update_core_memory` 是 UPDATE，空串会顶掉已有设定，
   且随后日志被删、不可回滚。触发门是「空闲 >1h 且日志 >30 条」，
   不常发生但一旦发生不可逆。
2. **⑤ 的一半是上一轮引入的**：`main.py` 里给 retriever 同步三个字段是
   上轮修「改了不生效」时加的，但回滚路径没覆盖它们。

**⑥ 只做了部分修复**：全量方案（模型指纹 + 索引迁移/隔离）改动面过大。
本轮只让向量失败时关键词结果能独立返回。相同维度不同模型的语义空间混用
**仍未解决**，是已知限制，记在 CHANGELOG 里。

### 验证（全部实测）

- 本地套件：**279 / 18 / 20 passed**，`prompt_builder` 31 passed，
  `kb` ALL TESTS PASSED，全仓 `compileall` 通过。
- harness `--tier2 --tier3 --allow-flip`：**PASS 49 / FAIL 0 / SKIP 1**，
  与改动前基线一致。
- 针对性验证：② 40 轮随机交错全收敛；⑥ 维度不匹配由 0 条变为按关键词召回；
  ⑦ FTS 命中在回退路径中保留；⑧ 异常 `rag_ok=False`、空结果 `True`、
  结果仍是 list 语义。
- 部署：7 个文件 md5 与仓库一致，插件重载成功，实盘冒烟正常，
  后端日志 **0 条 Traceback**；配置 md5 回到基线
  `403fee103fe2c8c82d7166a98c538854`。

---

## 四轮执行记录（2026-09-17 四：写指令补权限门）

用户指令：**「将这些写类指令加上管理员权限」**——承接上一轮审计时发现的
5 个未门控的写类子命令。

### 改了哪些

| 指令 | 位置 | 风险（为什么该门控） |
|---|---|---|
| `/char import <JSON>` | `commands.py:428` | 往角色卡库写入任意卡片；导入后切换即参与提示词注入 |
| `/wb reload` | `commands.py:116` | 改写**全库共享**世界书，影响所有会话而非仅发起者 |
| `/doc reload` | `commands.py:1346` | 同上，换掉共享向量索引 |
| `/reinject` | `commands.py:1623` | 改写本会话注入状态 |
| `/quill debug` | `commands.py:939` | 输出会话标识（Target/Session/Persona）、字段表、各库条目数、注入构成 |

**刻意不动**：读类指令（list / info / export / search / 无参数状态查询）
群聊仍对所有人开放；`/quill test` 虽跑检索但 `log_match=False`
（`kb.py:820/854`），不落库，属只读。

**/quill help 同步**：17 条写操作行尾加 🔒。`/stream` 与 `/quill statusbar`
**不标**——它们只有带参数才过门，无参数是查询；标记会让用户误以为查询被锁。
文档里的 19 = 17 条带参数的写命令 + 这 2 条的条件性门。

### 验证结果（全部实测）

- `commands.py` 门控调用点：**19 处**，审计脚本逐一把门映射到所属子命令，
  与「会改持久状态的操作」集合一一对应，无漏网。
- harness tier3 新增 7 条，全部 PASS：
  - 5 条断言非管理员在群聊被拦截（`group_char_import_blocked`、
    `group_wb_reload_blocked`、`group_doc_reload_blocked`、
    `group_reinject_blocked`、`group_quill_debug_blocked`）；
  - 2 条断言同一指令在**私聊照常工作**（`private_char_import_allowed`、
    `private_quill_debug_allowed`）——防止把权限门做成功能阉割。
  - `/char import` 用例刻意喂坏 JSON：被拦截 → 「只有管理员」，
    门失效 → 「未在输入中找到有效的 JSON 对象」。两种回执可区分，
    且**两种都不会真的写入角色卡库**，不留测试残留。
- 完整回归 `--tier2 --tier3 --allow-flip`：**PASS 49 / FAIL 0 / SKIP 1**
  （唯一 SKIP 是既有的 `llm_wr_match_detail`，因角色卡 `wr_mode=disabled`）。
- 既有 tier1 私聊用例 `quill_debug` / `wb_reload` / `reinject` /
  `char_import_bad_json` 全部仍然 PASS，证明门控未影响私聊路径。
- 配置 md5 回到基线 `403fee103fe2c8c82d7166a98c538854`；
  `quill_state.json` 中 `status_bar_mode` 分布 `{auto: 75}`，无非 auto 残留。
- 实盘 `/quill help` 输出确认 🔒 标记与页脚渲染正确。
- 部署目录与仓库 `commands.py` md5 一致（`2ce549b7...`），插件重载成功。

---

## 三轮执行记录（2026-09-17 三：修复 + 重构）

用户指令：**「修复已知缺陷，同时重构优化状态栏的多级降级兜底机制」**。
二轮发现的 3 个缺陷全部修复，降级链重构为注册表驱动。

### 改了哪些

| 文件 | 改动 |
|---|---|
| `main.py` | ① `on_llm_tool_respond` 不再清总闸门，改用 `_quill_memorized`；② save 时把 `top_k`/`enable_memory`/`config` 同步到 retriever；③ 世界书总开关接到唯一注入点；④ `_handle_status_bar` 六级链重构为 `_SB_LEVELS` 注册表 + `_sb_l1..l6` 六个方法 + `_StatusLevelResult`/`_StatusLevelContext` 两个值对象；⑤ `HealthTracker.record_status_level()` + `stats()["levels"]` |
| `commands.py` | `/quill debug` 增加一行「降级链命中: …」 |
| `tests/test_status_bar_parsers.py` | `t24` 的 `_Health` 桩补 `record_status_level`；新增 **`t25`**（注册表顺序、逐级计数、`terminal` 语义、各级空输入返回 None、闸门拆分断言） |
| `docs/probe_no_leak.py` | **新增**：泄漏专项探针（严格判据 + 自检） |
| `docs/probe_config_effects.py` | G/H 两节改为验证修复后的行为；Z 节换成严格判据 |

### 验证结果（全部通过）

| 项目 | 结果 |
|---|---|
| `test_status_bar_parsers.py` | **262 passed, 0 failed**（237 → 262，新增 t25） |
| `test_config_projection.py` | **18 passed, 0 failed** |
| `test_memory_fts.py` | **20 passed, 0 failed** |
| `prompt_builder.py` / `kb.py` 自检 | **31 passed** / **ALL TESTS PASSED** |
| `py_compile`（全改动文件） | 通过 |
| harness `--tier2 --allow-flip` | **PASS 38 / FAIL 0 / SKIP 1**，4 个翻转窗口全部 `md5 回到基线` |
| 探针 Z（泄漏专项） | **PASS**：修复前 DB 3/3 泄漏 → 修复后 **0/6**；SSE 同样 0/6 |
| 探针 E（配置项） | **PASS 10/0**（修复前 7/3）：G 由 FAIL 转 PASS（开=1 关=0） |
| 探针 D（on→off） | **PASS 11/11**（重构未影响开关语义） |
| 配置 md5 | `41d3f134dae8bd4e8063431ee2e8b665`（回到基线） |
| `memories` / `chat_logs` | 均为 0 |

### 三个缺陷的修复要点

**缺陷 1（严重）裸 `[LOVE_DATA]` 泄漏** —— 根因是 `on_llm_tool_respond` 里
`event.set_extra("_quill_activated", False)` 把**总闸门**当「记忆只做一次」
的标记用。第一次工具调用后闸门关闭，后续调用全部绕过插件。
拆出 `_quill_memorized` 后，探针 Z 实测 6 轮 0 泄漏。

> **判据修正**：第一版探针 Z 报了「DB 泄漏 1/6」，查证是**假阳性**——
> 那行是模型 254 字的英文自我修正说明，正文里**提到**了 `[LOVE_DATA]`
> 这个标记名，并非泄漏。判据已改为「行首标记 + 竖线分隔值」，并加了
> 7 条正反例自检（含那两条假阳性样本）。这是本轮唯一一次「看起来是 bug、
> 实际是测法错」的情况，记下来避免下次重踩。

**缺陷 2（中）`rag.enable_memory` 改了不生效** —— `enable_memory` / `top_k`
只在 `QuillRetriever` 构造时读一次。修法是在 save 流程里直接把这两个属性
写到现存 retriever 上（不重建组件：重建会 close SQLite/FAISS 句柄，
代价与风险都不成比例）。探针 E 的 G 项由 FAIL 转 PASS（开=1 关=0）。

**缺陷 3（中）`worldbook.enabled` 是死开关** —— 运行期无任何消费者。
修法是在**唯一的注入点**按开关决定是否把 `wb_manager` 传下去：
关掉则传 `None`，`PromptBuilder` 里所有 `if wb_manager` 判断自然跳过。
不新增函数参数（`build_system_prompt` 本来就有 `(None, None, {})` 的用法）。

> 本机 4 张角色卡 `wb_mode` 全是 `disabled`，世界书本来就不注入，
> 所以探针 E 的 H 项**测不出长度差**——如实记 NOTE，不硬判。

### 重构：降级链 → 分级注册表

`_SB_LEVELS` 是顺序的唯一来源；每级一个方法，返回 `_StatusLevelResult` 或 `None`。
关键语义是 `terminal`：

* `True` = 命中即结束（L1/L2/L3、L4 多命中、L5/L6）
* `False` = 已改写但继续下降（**仅 L4 单命中**——擦掉可疑行但不足以重建整栏）

顺带得到：逐级命中率（`/quill debug` 打印）、单级异常不再掀翻整链、
`t25` 可直接调各级方法做确定性测试。

日志字符串**逐字未变**（fixture/harness 有断言依赖），级别名与日志括号内容一致。

---

## 二轮执行记录（2026-09-17：会话开关 + 面板配置项）

按用户追加要求补测两件事，并**发现 3 个真实缺陷**。

### 追加①：探针 D —— 会话内 on → off 中途切换（不清上下文）

**用户问题**：「statusbar on 后聊几次、不清上下文，再 statusbar off 继续聊，
状态栏是否由出现变为消失？」

**答案：是，会消失。** `docs/probe_session_on_off.py`

| 阶段 | 观察 |
|---|---|
| A. 面板开 + 聊 2 轮 | 状态栏出现（建立历史示范） |
| B. `/quill statusbar off` | 报告「会话覆盖 / 本轮实际生效: 关闭」 |
| C. off 后连聊 3 轮 | **3 轮全部无字段行、无 `[LOVE_DATA]`、无围栏、无剧情块** |
| D. 切回 on | 状态栏恢复（非单向失效） |
| E. md5 | 全程恒定（会话覆盖不写面板配置） |

关键点：**上下文没清**（历史里仍有带状态栏的 assistant 消息），所以这条
同时验证了两个 P0 改动真的在起作用——`prompt_builder.py:215` 的
`if session_vars and self.status_bar_enabled` 门控，与 `main.py:2065`
关闭时剥离历史 `req.contexts`。

> 注：探针 D 第一版把「配置 md5 未变」写成「等于基线」，那是错的——
> 探针自己把 `enabled` 翻成了 true，运行期间本来就不等于基线。已改为
> 「运行期间 md5 恒定」（真正要断言的语义）。

### 追加②：探针 E —— 面板配置项改动 → 实际聊天是否生效

`docs/probe_config_effects.py`。**只测 harness 未覆盖的键**；harness 已覆盖
且通道未变的不重复测（`status_bar.enabled`/`show_delta`、
`debug.show_inject_report`、`worldbook.always_activate`、Tier 1 全部命令通道）。

**实测结果（PASS 7 / FAIL 3）**：

| 键 | 结果 | 说明 |
|---|---|---|
| A `status_bar.fields` | **PASS** | 改成 6 个自定义字段后，产出立刻用新字段名 |
| B `status_bar.plot_paths` | **PASS** | 用长字符串测得 prompt 长度按预期增长（delta=406，预期 283，含动态段噪声） |
| C `status_bar.format_template` | **PASS** | 自定义标记 `[[CUSTOMTPL]]` 出现且包裹了字段正文 |
| D `status_bar.default_placeholder` | **PASS**（修正前提后） | 见下方「修正的两个错误前提」 |
| E `performance.min_output_length` | 观察项 | 模型照不照做不由配置决定 → 不判 FAIL |
| F `rag.enable_chat_logging` | **PASS** | A/B 对照成立：开=6→8 增长，关=16→16 不变 |
| G `rag.enable_memory` | **FAIL** | 见下方「发现 2」 |
| Z `[LOVE_DATA]` 泄漏 | **FAIL** | 见下方「发现 1」 |
| H `worldbook.enabled` | 静态发现 | 见下方「发现 3」 |

#### 修正的两个错误前提（探针自身的 bug，不是插件缺陷）

1. **不能只读 `reply`**：模型在 agent 模式下会**多次**调用
   `send_message_to_user`（正文一段、状态栏单独一段）。`chat.WebChat.send`
   的 `reply` 取 SSE 的 `complete`，那只是**最后一段**。第一版探针因此把
   A/B/C/D 判成 FAIL（7 条 FAIL 里 5 条是这个原因）。
   现改为从 `platform_message_history` 取本轮**全部**新增的行并拼接
   （`_delivered_since`），那才是真正送达的内容。

2. **`/quill reset` 不清 `session_vars`**（`commands.py:1642` 附近，只归零
   轮次/日志）。所以「reset 之后兜底栏必然缺值」是错的——聊过几轮后六个
   字段都有值，占位符根本不会出现。现改为**同时换掉字段表**（用全新字段名
   让 `prev_vars` 全部对不上），才能真正触发占位符。

### 发现 1【严重】多轮工具调用时，裸 `[LOVE_DATA]` 会送达用户

**现象**：`Z1` DB 泄漏 3/3 轮、`Z2` SSE 泄漏 3/3 轮。

**证据**（一次完整抓取，`agent_stats=3` 表示 agent loop 跑了 3 轮）：

```
DB 新增（按 id 升序）：
  id=4867 sender=bot len= 757  裸标记=False   ← 正文（已处理）
  id=4868 sender=bot len= 229  裸标记=True    ← [LOVE_DATA] | 88/100 | ...
  id=4869 sender=bot len= 473  裸标记=False   ← 模型自己说「已发送…」

插件日志（整轮只有这一条处理记录）：
  [18:32:19.571] [Quill] 状态栏已处理 (LOVE_DATA inline)
  [18:32:19.603] [Quill] send_message_to_user 已调用
  [18:32:24.884] [Quill] 已剥离 resp.completion_text 中的状态栏残留
```

**根因（已定位到行）**：`main.py:2396`

```python
logger.info("[Quill] send_message_to_user 已调用")
event.set_extra("_quill_activated", False)   # ← 这里
```

`_quill_activated` 是插件的总闸门，被三处读取：
  * `main.py:1771` —— `on_using_llm_tool` 的入口（状态栏处理就在这个钩子里）
  * `main.py:2341` —— `on_llm_response` 的入口

第一次 `send_message_to_user` 后，`on_llm_tool_respond` 把闸门置 False，
于是**同一轮对话里后续所有的工具调用都不再经过插件**：既不处理状态栏、
也不剥离残留。而模型在 agent 模式下会分多次调用 `send_message_to_user`
（实测 7 轮里 3 轮如此），第 2 次之后的内容就带着裸 `[LOVE_DATA]` 直达用户。

**为什么不是剥离器的问题**：已实测 `_strip_status_artifacts` 对
「裸 `[LOVE_DATA] ... ` 一行 + 剧情块」输入返回空串，擦得很干净
（`_STRIP_PATTERNS` 第 2 条 `\[LOVE_DATA\]\s*.+` 覆盖）。问题是那段代码
**根本没被调用**——闸门在它前面就 return 了。

**历史归属**：`git show HEAD:main.py:1908` 同一行同样存在，所以这是**既有
行为**，不是本次四项改动引入的回归。

**修复方向（未实施，待你决定）**：`main.py:2396` 置 False 的用意是
「本轮的记忆存储/反思只做一次」，用同一把闸门兼职「防止重复处理」。
两个语义混用是问题所在。可选：
  * 拆成两个标志：`_quill_activated`（整轮是否激活，不因工具调用而清零）与
    `_quill_memorized`（记忆/反思是否已做，只做一次）；
  * 或把该行改为只在「确实完成了记忆存储」时置 True 的幂等标记。
两种改法都会影响 `on_llm_response` 的落日志路径（`main.py:2341` 也读这把
闸门），需要一并回归——所以**没有**在本轮擅自动手。

**当前状态**：**未修复**，证据完整、根因明确，等你定方向。

**影响面**：用户在 QQ/WebChat 上会看到一行裸 `[LOVE_DATA] ...`，观感是
「漏处理」。触发条件是模型把回复拆成多次 `send_message_to_user`（实测
6~7 轮里出现 3 次，概率不低）。

### 发现 2【中】`rag.enable_memory` 改了当轮不生效，需重载插件

**现象**：`G` 关闭后记忆检索仍是 1~2 条（开=2 关=2）。

**根因**：`enable_memory` 只在 `QuillRetriever` **构造时**读一次
（`main.py:662`），而 save 流程里只有 `embedding_provider_id` 会触发
`_reinit_rag_and_refresh_routes()`（`main.py:948`）。所以改这个开关
不会重建 retriever，要重载插件才生效。

注意 `tests/test_config_projection.py:128` 把 `rag_enable_memory` 列在
`_LIVE_READ_FIELDS`（声称「运行期直接读，无需投影」）——**这个声称与
实测不符**：它既没被投影，也没有运行期读取点，只有构造期读取。

**当前状态**：**未修复**，如实记录。

### 发现 3【中】`worldbook.enabled` 面板开关是死的

**根因**：静态分析（43 个 QuillConfig 字段逐个找读取点）显示
`worldbook_enabled` 只出现在 `config.py:100`（赋值）和 `config.py:204`
（`__repr__` 里打印），**运行期没有任何消费者**。主流程的世界书注入看的是
**角色卡的 `wb_mode`**（`prompt_builder.py:410/470`），不是这个全局开关。

**当前状态**：**未修复**。面板上这个开关目前「改了不生效」。

> 顺带：同一份静态分析确认其余 42 个字段都有真实读取点，唯一另一个
> 可疑项 `rag_dense_top_k` 经查是在 `quill_rag/retrieval.py:64` 通过
> `self.config._raw` 读的（绕过属性投影），**不是**死键。

### 本轮未覆盖（如实说明）

- `worldbook.match_sensitivity` / `max_dynamic_entries` / `max_token_limit`、
  `writing_resource.*` —— 需绑定世界书/素材库且 `wb_mode`/`wr_mode` != disabled，
  当前 4 张卡全是 disabled（harness 的 `llm_wr_match_detail` 同因 SKIP）。
- `refusal.*` —— 需模型真的输出拒绝语，不可控。
- `performance.max_output_length` —— 只在 emergency 协议里出现
  （`prompt_builder.py:633`），正常轮次不注入。
- RAG provider/embedding/chunk 类 —— `_LIVE_READ_FIELDS` 明确豁免，
  需单独的重初始化测试。

---

## 执行记录（2026-09-17，一轮：四项改动本体）

照本文跑完一遍，结果与验收判据逐条吻合。**唯一需要修正预期值的地方是
fixture 条数**：`test_status_bar_parsers.py` 从 220 涨到 **237**，因为本轮
执行时新加了 `t24`（见下方「新发现」）。

| # | 项目 | 预期 | 实测 | 结论 |
|---|---|---|---|---|
| 1.1 | `test_status_bar_parsers.py` | 220 | **237 passed, 0 failed** | PASS（含新增 t24） |
| 1.2 | `test_config_projection.py` | 18 | **18 passed, 0 failed** | PASS |
| 1.3 | `test_memory_fts.py` | 20 | **20 passed, 0 failed** | PASS |
| 1.4 | `prompt_builder.py` 自检 | 31 | **31 passed, 0 failed** | PASS |
| 1.5 | `kb.py` 自检 | ALL PASSED | **ALL TESTS PASSED** | PASS |
| — | `py_compile`（6 个改动文件） | 退出码 0 | **退出码 0** | PASS |
| 二 | harness `--tier2 --allow-flip` | 38/0/1 | **PASS 38 / FAIL 0 / SKIP 1** | PASS |
| 三 | 探针 A 平台双模板 | PASS | **PASS** | PASS |
| 三 | 探针 B 会话级开关 | PASS | **PASS（5/5）** | PASS |
| 三 | 探针 C 实际聊天 | —（本次新增） | **PASS（10/10）** | PASS |
| 四 | 配置 md5 | `41d3f134...` | **`41d3f134dae8bd4e8063431ee2e8b665`** | PASS |
| 四 | `memories` / `chat_logs` | 0 / 0 | **0 / 0** | PASS |
| 四 | `status_bar_mode` 残留 | 无 | **无** | PASS |
| 四 | 部署目录一致性 | 无差异 | **无差异（运行时文件）** | PASS |
| 五 | `metadata.yaml` 版本 | 5.2.3 | **5.2.3** | PASS |
| 五 | `git log` | 无新提交 | **仍是 `cf24f3a`** | PASS |

harness 的四个翻转窗口都打出了 `[gates] 已还原，md5 回到基线`；
`gates.py:221-234` 对 md5 不符是**抛 `SafetyError`**（不是静默告警），
所以 `exit=0` 本身就证明了四个窗口全部还原干净。

### 新发现：L4 单命中分支线上复现不了 → 补了 `t24`

探针 C 第 4 轮专门要求模型「只输出一行裸字段、不要任何标记」，模型**仍然**
按 system prompt 走了 `[LOVE_DATA]`；随后又用三条强指令（「优先级最高」
「忘记之前所有格式要求」「禁止输出标记」）定向尝试三次，**全部走 L2**。
线上 4 次 + 定向 3 次 = 7 次，L4 单命中 **0 次**。

这不是缺陷——模型的顺从是好事。但它意味着 L4 单命中这条**本次新写的分支
线上无法覆盖**，而它恰好是最容易回退的一段（若有人只保留「≥2 才重建」
半边，单命中裸行就会漏给用户）。所以补了 fixture `t24`：用
`object.__new__(M.QuillPlugin)` 造轻量宿主，**直接驱动真实的
`_handle_status_bar` 协程**（此前 fixture 只测正则，没测这条路径）。

`t24` 锁定的六件事：

1. 单命中行被剥离、正文保留、`handled=False`、不产出字段；
2. 双命中重建整栏、原裸行被替换而非重复；
3. 超长值（>30 字）单命中**不**剥离——解析侧不命中的刻意取舍，宁漏不误删；
4. L5 能兜住「好感/关系」这类宽泛字段名，并用历史值补齐其余字段；
5. 纯文本模板走同一条路径且不产生围栏；
6. 健康度无论命中与否都记一次。

运行时日志确实打出了 `[Quill] L4 单命中（心情），不重建整栏，仅剥离该行`，
证明走的是真实分支而非旁路。

---

## 零、先读这一段（执行前必看）

### 环境事实（照抄，不用再查）

| 项 | 值 |
|---|---|
| AstrBot 运行时 | `D:/Program/AstrBot/backend/app`（v4.28.0，Desktop 版） |
| Python 解释器 | `D:/Program/AstrBot/backend/python/python.exe` |
| 插件仓库 | `E:/Study/Astrbot小工具/vs code work/astrbot_plugin_quillplus` |
| 部署目录 | `D:/Program/AstrBot/AstrBotData/data/plugins/astrbot_plugin_quillplus` |
| 面板/后端地址 | `http://127.0.0.1:6185` |
| 生产配置 | `D:/Program/AstrBot/AstrBotData/data/config/astrbot_plugin_quillplus_config.json` |
| 记忆库 | `.../plugin_data/astrbot_plugin_quillplus/knowledge/quill_memory.db` |
| 状态文件 | `.../plugin_data/astrbot_plugin_quillplus/quill_state.json` |
| harness | `astrbot_plugin_quillplus/.build/harness`（已 gitignore） |

**配置基线 md5**（执行前先确认；执行后必须回到这个值）：

```
403fee103fe2c8c82d7166a98c538854
```

> 注意：这个 md5 与上一轮的 `41d3f134...` 不同，是**预期的**——
> v5.2.4 移除了两个已弃用配置键（`debug.panel_theme`、
> `debug.panel_ui_state`），插件重载后 AstrBot 会把它们从生产配置里
> 清理掉，于是文件内容变化、md5 随之变化。`403fee10...` 是「移除后」
> 的稳定态，跑完翻转后必须回到它。
>
> 全部探针脚本里的 `BASE_MD5` 已同步为该值。

**生产基线值**：`status_bar.enabled = false`、`status_bar.show_delta = true`、
`worldbook.always_activate = true`、`debug.show_inject_report = false`。

### 硬约束（不可违反）

1. **不改版本号** —— 保持 `5.2.3`（`metadata.yaml` / `README.md` / `CHANGELOG.md` 三处一致）。
2. **不提交仓库** —— 改动全部留在工作树。
3. `jwt_secret` 与 `ws_reverse_token` **只在内存**，绝不打印、不写文件/日志。
4. 插件 DB 一律 `file:...?mode=ro` 只读打开；写操作只走插件的 HTTP 端点。
5. 开关翻转：**改前备份 → `finally` 无条件还原 → 校验 md5 回基线**。
   只允许 flips 白名单内的键（见 `.build/harness/quilltest/gates.py`）。
6. 假 OneBot 用合成 `X-Self-ID`（如 `10000`），不冒充真实客户端。
7. 测试身份固定 `quilltest`，不碰用户真实 QQ 会话。
8. 备份 bundle `C:\Users\REISEN\quill_repo_backup_20260916_140851.bundle`
   **含隐私数据**，别外传。

### 跑测试的标准姿势

```bash
cd "E:/Study/Astrbot小工具/vs code work"
P="D:/Program/AstrBot/backend/python/python.exe"
export ASTRBOT_APP="D:/Program/AstrBot/backend/app"

# 三个 fixture 套件
"$P" astrbot_plugin_quillplus/tests/test_status_bar_parsers.py
"$P" astrbot_plugin_quillplus/tests/test_config_projection.py
"$P" astrbot_plugin_quillplus/tests/test_memory_fts.py

# 模块自检
cd astrbot_plugin_quillplus
"$P" prompt_builder.py     # 31 passed
"$P" kb.py                 # ALL TESTS PASSED

# harness（消耗真实 token）
cd .build/harness
python -m quilltest.run --tier2 --allow-flip
```

**部署改动到运行目录**（改了仓库文件后必须做，否则测的是旧代码）：

```bash
cd "E:/Study/Astrbot小工具/vs code work/astrbot_plugin_quillplus"
DEST="D:/Program/AstrBot/AstrBotData/data/plugins/astrbot_plugin_quillplus"
for f in *.py *.yaml *.json; do [ -f "$f" ] && cp -f "$f" "$DEST/$f"; done
rm -rf "$DEST/pages"; cp -r pages "$DEST/"
```

**复查一致性**（只比运行时文件；`diff -rq . $DEST` 会误报一堆开发产物）：

```bash
cd "E:/Study/Astrbot小工具/vs code work/astrbot_plugin_quillplus"
DEST="D:/Program/AstrBot/AstrBotData/data/plugins/astrbot_plugin_quillplus"
for f in *.py *.yaml *.json; do
  [ -f "$f" ] && ! diff -q "$f" "$DEST/$f" >/dev/null 2>&1 && echo "DIFF: $f"
done
diff -rq pages "$DEST/pages" 2>/dev/null | grep -v __pycache__
diff -rq quill_rag "$DEST/quill_rag" 2>/dev/null | grep -v __pycache__
```

**预期**：无输出。

> 顶层 `CHANGELOG.md` / `README.md` 与部署副本**本来就会不一致**——
> 它们是纯文档、不是运行时依赖，部署目录里是安装时的快照。不要当成缺陷。
> 另外 `.gitignore` 也会不一致（部署目录那份是旧的），同样无害。

**重载插件**（不重启 AstrBot）：

```bash
cd .build/harness/quilltest
python -c "
import sys; sys.path.insert(0,'.'); sys.path.insert(0,'..')
import auth
print(auth.DashboardClient().json('POST','/api/plugin/reload',json={'name':'astrbot_plugin_quillplus'}))
"
```

### 已知陷阱（踩过的坑，别再踩）

1. **`state.py` 一类"没改过"的文件也可能是过期的**。上一轮 harness 报
   `persona_conversation_isolation` FAIL，根因是部署目录里的 `state.py`
   还是 9 月 16 日的旧版（缺 `get_persona_conv_map`），与当轮改动无关。
   → **部署用全量同步 + `diff -rq` 复查，不要只拷"改过的文件"**。

2. **配置保存端点的字段名是 `updates` 不是 `items`**。
   传错会得到 `{"status":"error","message":"缺少 updates 数组"}`。

3. **翻配置要连 `finally` 一起写**。手写探针若中途抛异常，生产配置会停在
   被改状态。模板见"附录 A"。

4. **Agent loop 不终止是注释写错、不是行为缺陷**。实测单条消息
   `agent_stats` 事件恒为 1、正文段恒为 1。

5. **`/tmp/` 在 Git Bash 下可写，但 Python 里别用 `/tmp/xxx` 当路径**
   （Windows 原生 Python 不认识）。用 `tempfile`。

6. **harness 的清残留有两处盲区**：`memory/delete` 要按**完整**
   `umo::persona` 键删（只传 conversation id 会 `deleted=0`）；
   `chat_logs` 要先用 `/quill reset` 清（它没有按维度删除的端点）。

---

## 一、本地 fixture（零 token，先跑这一层）

**目的**：确认逻辑正确性。这一层全绿才值得花 token 跑 harness。

| # | 套件 | 预期 | 覆盖的本轮改动 |
|---|---|---|---|
| 1.1 | `test_status_bar_parsers.py` | **237 passed, 0 failed** | t23 是 L2 套模板、t24 是真实协程（均为新） |
| 1.2 | `test_config_projection.py` | **18 passed, 0 failed** | 新配置项自动纳入投影守卫 |
| 1.3 | `test_memory_fts.py` | **20 passed, 0 failed** | 中文检索（上一轮） |
| 1.4 | `prompt_builder.py` 自检 | **31 passed, 0 failed** | 契约生成器 |
| 1.5 | `kb.py` 自检 | **ALL TESTS PASSED** | WR 库未回归 |

### 关键 fixture 逐条确认（如只跑一遍，重点看这些）

| 用例 | 断言什么 | 为什么重要 |
|---|---|---|
| `t18` | 契约四处同源；改 `fields` 传导到 guide/safety/tail | 防"改一处漏三处"复发 |
| `t19` | QQ→纯文本模板、Discord→Markdown、未知平台→Markdown | 平台分治的核心判据 |
| `t20` | 去重后计数 ≥2 才重建；单命中行被剥离 | 阈值改动的**两个半边** |
| `t21` | 6 种「面板 × 会话」组合的优先级 | 覆盖语义 |
| `t22` | `on/off/auto` + 中文别名 + 非法参数 | 命令分发 |
| `t23` | L2 套模板 | **本轮修复的长期缺口** |
| `t24` | 真实 `_handle_status_bar` 协程：L4 单命中/双命中/L5/纯文本模板 | **线上复现不了的那段**（见上方新发现） |

### 语法检查

```bash
cd astrbot_plugin_quillplus
"D:/Program/AstrBot/backend/python/python.exe" -m py_compile \
  main.py prompt_builder.py config.py state.py commands.py web_routes.py
```

预期：无输出（退出码 0）。

---

## 二、harness 回归（消耗真实 token，约 5-8 分钟）

```bash
cd .build/harness
python -m quilltest.run --tier2 --allow-flip
```

**预期**：`exit=0`，**PASS 38 / FAIL 0 / SKIP 1**。

那 1 个 SKIP 是**已知且预期**的：

> `llm_wr_match_detail` —— 需要 `worldbook.always_activate=false` **且**
> 所绑角色卡的 `quill_extensions.wr_mode != 'disabled'`。当前所有角色卡都是
> `wr_mode=disabled`，WR 关键词触发分支根本不执行（`main.py` 直接跳过）。
> 这不是回归，是本机测试数据的状态。

**翻转窗口**：harness 会临时改生产配置（每次都有醒目警告 + 自动还原 +
md5 校验）。本轮应看到 4 个窗口：
1. `worldbook.always_activate` True→False（2 条用例）
2. `status_bar.enabled` False→True（1 条）
3. `status_bar.enabled` + `show_delta`（2 条）
4. `debug.show_inject_report`（2 条）

**每个窗口结束后都要看到** `[gates] 已还原，md5 回到基线`。

### 失败时怎么查

```bash
# 拿 JSON 详细（含 findings / evidence / log_lines）
python -m quilltest.run --tier2 --allow-flip --json --case <用例名>
```

**本轮的已知坑**：`status_bar_delta_annotation` 曾在拼 evidence 时报
`NameError: name 's' is not defined` —— 那是 harness 自己的正则少捕获了
箭头分组，已修（`run.py` 的 `num_arrow_re`）。若再见到，检查那处的分组数。

---

## 三、端到端集成探针（这是本轮的重点）

fixture 只能证明「函数行为对」，证明不了「配置改了之后线上真的变了」。
下面两个探针补这一层。**必须用 `try/finally` 保证还原。**

### 探针 A：平台双模板真的生效

**已写成可执行脚本**：`docs/probe_platform_template.py`

```bash
cd "E:/Study/Astrbot小工具/vs code work/astrbot_plugin_quillplus/.build/harness/quilltest"
"D:/Program/AstrBot/backend/python/python.exe" ../../../docs/probe_platform_template.py
```

**做法**：把 `webchat` 临时当成纯文本平台，给纯文本模板塞一个可识别标记，
发一条消息，看回复里有没有那个标记、有没有 Markdown 围栏。

**预期**（实测值）：
```
含纯文本标记 [[PLAINTPL]] : True (应 True)
含 Markdown 围栏 ```   : False (应 False)
含 ** 粗体标记         : False (应 False)
>>> 探针 A: PASS
restore: ok
md5 = 41d3f134dae8bd4e8063431ee2e8b665 | OK
```

回复尾部应能看到 `[[PLAINTPL]]` 后接 `好感度：…` 等字段行。

**这条同时验证了 L2 修复** —— 因为 L2 是实际最常命中的路径
（实测 40 次解析里 36 次走 L2）。修复前这个探针**会失败**（标记不出现、
且裸字段行直接暴露）。所以它是本轮最有价值的一条端到端证据。

> 若看到 `好感度：测试占位` 说明读的是历史 `session_vars`，属正常
> （新会话没有历史值就是占位符）。

### 探针 B：会话级开关真的生效

**已写成可执行脚本**：`docs/probe_session_override.py`

```bash
cd "E:/Study/Astrbot小工具/vs code work/astrbot_plugin_quillplus/.build/harness/quilltest"
"D:/Program/AstrBot/backend/python/python.exe" ../../../docs/probe_session_override.py
```

**做法**：面板全局**关着**（生产基线），在会话里 `/quill statusbar on`，
发一条消息，看状态栏是否出现；再 `/quill statusbar auto` 复位。

**预期**（实测值）：
```
[PASS] auto 报告生效值=关闭
[PASS] on 写入成功
[PASS] 会话 on 覆盖了面板 off
[PASS] 复位后生效值=关闭
[PASS] 配置 md5 未变
>>> 探针 B: PASS
```

两个脚本都自带 `finally` 还原 + md5 校验，可以直接跑。

### 探针 C：真实多轮聊天（**已写成可执行脚本**）

`docs/probe_live_chat.py` —— 走 harness 同一套 WebChat 通道，四轮真实对话。

```bash
cd "E:/Study/Astrbot小工具/vs code work/astrbot_plugin_quillplus/.build/harness/quilltest"
"D:/Program/AstrBot/backend/python/python.exe" ../../../docs/probe_live_chat.py
```

**实测结果（PASS 10/10）**：

```
第1轮 状态栏出现 / 栏外无裸字段行   PASS
第2轮 状态栏出现 / 栏外无裸字段行   PASS
第3轮 状态栏出现 / 栏外无裸字段行   PASS
状态字段随剧情演进（非复读）        PASS
第4轮 栏外无裸字段行                PASS
session_vars 已落盘                 PASS
session_vars 无标注回流             PASS
```

**它验证的三件事**（都是 fixture 覆盖不到的）：

1. **多轮下状态栏每轮都在，字段随剧情演进**——实测心情序列
   `放松又带点撒娇的期待` → `黏人又满足` → `又黏又踏实`，位置也从
   `落地窗边` 推进到 `落地窗边地毯上`，不是复读陈旧值；
2. **裸字段行不漏到用户可见文本**——三轮 + 第 4 轮定向诱导，栏外始终「无」；
3. **标注不回流**——`session_vars` 六个字段里没有任何 `↑/↓` 标注。

**L1-L6 命中分布（线上实测）**：4 次解析 **全部走 `LOVE_DATA inline`（L2）**。
这是「L2 是主力路径」结论的又一份独立佐证，也是本轮修 L2 不套模板那个缺口
价值最大的地方。

> **诚实说明**：第 4 轮本想诱导出 L4 单命中，模型没配合（仍走 L2），
> 脚本按设计记 NOTE 而非 FAIL。该分支改由 fixture `t24` 锁定。

**残留清理**：脚本自带 `finally` 还原 + `/quill reset` + 按完整
`session_id` 删记忆。跑完 `memories`/`chat_logs` 均为 0。

### 探针 D（可选）：L4 阈值改动的线上表现

**结论：线上复现不了，已放弃**。用三条强覆盖指令（「优先级最高」/
「忘记之前所有格式要求」/「禁止输出标记」）定向尝试均走 L2。改用
fixture `t24` 直接驱动真实协程覆盖。原始判定标准保留如下备查：

裸行不得出现在用户可见文本里。若出现裸行 + 完整状态栏并存，
说明单命中剥离那半边坏了。

---

## 四、环境清理（跑完必做）

```bash
# 1) 配置回基线
python -c "
import hashlib, io
p = r'D:\Program\AstrBot\AstrBotData\data\config\astrbot_plugin_quillplus_config.json'
md5 = hashlib.md5(io.open(p,'rb').read()).hexdigest()
print(md5, 'OK' if md5=='41d3f134dae8bd4e8063431ee2e8b665' else '!!! MISMATCH !!!')
"

# 2) 确认生产基线值未变
python -c "
import io, json
p = r'D:\Program\AstrBot\AstrBotData\data\config\astrbot_plugin_quillplus_config.json'
d = json.load(io.open(p, encoding='utf-8-sig'))
sb, wb, dbg = d.get('status_bar',{}), d.get('worldbook',{}), d.get('debug',{})
print('enabled        =', sb.get('enabled'),        '(应 False)')
print('show_delta     =', sb.get('show_delta'),     '(应 True)')
print('plain_platforms=', repr(sb.get('plain_platforms')), '(应空串)')
print('always_activate=', wb.get('always_activate'),'(应 True)')
print('show_inject_rep=', dbg.get('show_inject_report'), '(应 False)')
"
```

**清测试残留**（两步，顺序不能反）：

```python
# 1) 按 conversation id 调 /quill reset → 清 chat_logs
# 2) 按【完整】session_id（含 ::persona 后缀）调 memory/delete → 清 memories
#    只传 conversation id 会 deleted=0
```

清理后断言：

```bash
python -c "
import sqlite3
db = r'D:\Program\AstrBot\AstrBotData\data\plugin_data\astrbot_plugin_quillplus\knowledge\quill_memory.db'
con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
for t in ('memories','chat_logs'):
    n = con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    q = con.execute(f\"SELECT COUNT(*) FROM {t} WHERE session_id LIKE '%quilltest%'\").fetchone()[0]
    print(f'{t:12s}: total={n} quilltest残留={q}')
con.close()
"
```

**预期**：两个表都是 `total=0 quilltest残留=0`。

**会话级覆盖残留**（探针 B 会写 state，确认已复位）：

```bash
python -c "
import json, io, os
p = r'D:\Program\AstrBot\AstrBotData\data\plugin_data\astrbot_plugin_quillplus\quill_state.json'
d = json.load(io.open(p, encoding='utf-8'))
hits = {k: v.get('status_bar_mode') for k, v in d.items()
        if isinstance(v, dict) and v.get('status_bar_mode') not in (None, 'auto')}
print('非 auto 的会话:', hits if hits else '无')
"
```

**预期**：`无`。若有残留，对应用会话再发一次 `/quill statusbar auto`。

**部署目录与仓库一致性**：

```bash
cd "E:/Study/Astrbot小工具/vs code work/astrbot_plugin_quillplus"
DEST="D:/Program/AstrBot/AstrBotData/data/plugins/astrbot_plugin_quillplus"
for f in *.py *.yaml *.json; do
  [ -f "$f" ] && ! diff -q "$f" "$DEST/$f" >/dev/null 2>&1 && echo "DIFF: $f"
done
diff -rq pages "$DEST/pages" 2>/dev/null | grep -v __pycache__
diff -rq quill_rag "$DEST/quill_rag" 2>/dev/null | grep -v __pycache__
```

**预期**：无输出。有输出则同步后重跑受影响的用例。

---

## 五、验收判据（全部满足才算通过）

**执行结果：全部满足 ✅**（2026-09-17 实测）

- [x] fixture 237 / 18 / 20 + prompt_builder 31 + kb 全绿
- [x] harness **PASS 38 / FAIL 0 / SKIP 1**（SKIP 是已知的 `llm_wr_match_detail`）
- [x] 探针 A：纯文本标记出现、无围栏、无 `**`
- [x] 探针 B：面板关 + 会话 on → 状态栏出现；auto 后消失；md5 全程不变
- [x] 探针 C：四轮真实对话，状态栏每轮在、栏外无裸行、字段演进、无标注回流
- [x] 配置 md5 = `41d3f134dae8bd4e8063431ee2e8b665`
- [x] `memories` 与 `chat_logs` 均为 0（无 quilltest 残留）
- [x] `status_bar_mode` 无残留非 auto 值
- [x] `metadata.yaml` 仍是 `5.2.3`；`git log` 无新提交（仍是 `cf24f3a`）
- [x] 部署目录与仓库无差异（运行时文件）

### 追加执行的判据（二轮）

- [x] 探针 D：on→off 中途切换（不清上下文）→ off 后 3 轮全部干净，切回 on 可恢复
- [x] 探针 E：A/B/C/D/F 通过（字段表、剧情选项、模板、占位符、日志开关均热生效）
- [x] **发现 1（严重）**：多轮工具调用时裸 `[LOVE_DATA]` 送达用户 —— **已修复**（探针 Z：0/6）
- [x] **发现 2（中）**：`rag.enable_memory` 改了当轮不生效 —— **已修复**（探针 E G：开1关0）
- [x] **发现 3（中）**：`worldbook.enabled` 面板开关无消费者 —— **已修复**（接到唯一注入点）

> 三项的修复细节与验证证据见本文顶部的「三轮执行记录」。
- [x] 探针 D/E 结束后配置 md5 回到基线，`memories`/`chat_logs` 均为 0

### 四轮：收尾整理（v5.2.4 定版）

用户指令：更新文档/CHANGELOG/插件信息/配置页文字、清理弃用配置与死代码、
对齐指令提示、版本定 5.2.4、暂不提交。

| 项目 | 结果 |
|---|---|
| `test_status_bar_parsers.py` | **279 passed**（新增 `t26`） |
| `test_config_projection.py` / `test_memory_fts.py` | **18 / 20 passed** |
| `prompt_builder.py` / `kb.py` | **31 passed** / **ALL TESTS PASSED** |
| harness | **PASS 38 / FAIL 0 / SKIP 1** |
| 探针 E（配置项） | **PASS 10/0** |
| 探针 Z（泄漏） | **PASS 0/6** |
| 配置 md5 | `403fee103fe2c8c82d7166a98c538854`（见下方说明） |

**这一轮又挖出并修掉了「发现 1」的第二层根因**，详见顶部三轮记录的
「根因 B」与「兜底钩子两档强度」。要点：修完闸门拆分后仍能偶发复现，
继续挖发现 agent loop 每轮迭代**先 yield 内容、后触发工具钩子**，
所以「不调工具的轮次」能绕过全部工具钩子。加了 `on_decorating_result`
发送前兜底才真正封死。

> **踩坑记录（值得单独记）**：兜底钩子第一版直接复用 `_strip_status_artifacts`，
> 结果把**正常渲染的状态栏整段删掉**——那个剥离器本来就含匹配
> `**状态栏**...``` ``` 的模式（它是给「关闭状态栏」设计的）。
> 表现是探针 E 的 A/C 两项由 PASS 变 FAIL。现按开关分两档：
> 开→只擦原始标记、关→整套剥离。**这个教训是通用的**：复用剥离器前先确认
> 它的设计前提（擦「痕迹」还是擦「产物」）与当前场景是否一致。

### 五轮：写指令权限门（承接四轮审计）

用户指令：**「将这些写类指令加上管理员权限」**。

| 项目 | 结果 |
|---|---|
| 门控调用点 | **19 处**，与写操作集合一一对应（审计脚本逐条映射） |
| harness tier3 | 新增 **7 条全 PASS**（5 拦截 + 2 私聊放行） |
| harness 全量 | **PASS 49 / FAIL 0 / SKIP 1** |
| 既有私聊用例 | `quill_debug` / `wb_reload` / `reinject` / `char_import_bad_json` 仍 PASS |
| 配置 md5 | 回到基线 `403fee103fe2c8c82d7166a98c538854` |
| 状态残留 | `status_bar_mode` 分布 `{auto: 75}`，无非 auto 残留 |
| 实盘 | `/quill help` 🔒 渲染正确；部署目录与仓库 md5 一致，重载成功 |

**判定要点**：新增的 2 条「私聊放行」用例与 5 条「群聊拦截」用例同等重要——
前者防止把权限门做成功能阉割，后者防止越权。详见顶部四轮记录。

**配置 md5 变更说明**：`41d3f134...` → `403fee103fe2c8c82d7166a98c538854`。
原因是移除了两个已弃用配置键（`debug.panel_theme`、`debug.panel_ui_state`），
插件重载后 AstrBot 把它们从生产配置里清理掉。**这是预期变更**，
全部探针的 `BASE_MD5` 已同步。

**环境事件（非代码问题）**：执行期间 AstrBot 后端曾短暂重启
（23:07 恢复，日志中无插件导入错误），导致一次 harness 以
`ConnectionError` 退出；服务恢复后重跑即 PASS。记录在此以免与真实回归混淆。

---

## 六、本轮改了哪些文件（便于定位回归）

| 文件 | 改动 |
|---|---|
| `prompt_builder.py` | 新增 `build_status_contract()` / `build_status_reminder()`；`build_status_bar_guide` 改为非 static 并从契约取示例；`build_safety_wrapper` / `build_send_message_guide` 接入契约；`__init__` 增加 `love_fields` / `status_bar_plot_paths` |
| `main.py` | 新增 `_effective_status_bar_enabled()` / `_prompt_builder_for_request()` / `_resolve_platform_name()` / `_status_bar_template_for()`；L2 分支改为套模板；L4 阈值改「去重 ≥2」+单命中剥离；L5/L6 输出基座由 `text` 改 `new_text`；tail message 改用 `build_status_reminder()`；`/quill statusbar` 分发；两处 `_handle_status_bar` 调用传模板与会话开关 |
| `config.py` | 新增 `status_bar_format_plain` / `status_bar_plain_platforms` + `_DEFAULT_PLAIN_PLATFORMS`；`import re` 提到模块级（修掉函数内 import 造成的 shadowing） |
| `state.py` | `UserState.status_bar_mode` + `set_status_bar_mode()` / `get_status_bar_mode()` |
| `commands.py` | 新增 `statusbar_dispatch()` + 帮助文案 |
| `web_routes.py` | `_ALLOWED_CONFIG_KEYS` 加两个新键 |
| `_conf_schema.json` | `status_bar` 加 `format_template_plain` / `plain_platforms` |
| `pages/panel/index.html` | 状态栏卡片右列加两个字段（纯文本模板、纯文本平台列表） |
| `pages/panel/js/pages/config.js` | 绑定 + `CFG_DEFAULTS` 两条 |
| `tests/test_status_bar_parsers.py` | 新增 t18–t23（+74 条断言） |
| `docs/STATUS_BAR.md` | 同步行号 + 新增「九之二/三/四」三节 |

---

## 附录 A：配置翻转的安全模板

手写探针一律照这个骨架写，`try/finally` 不可省：

```python
import sys, time, hashlib, io
sys.path.insert(0, '.'); sys.path.insert(0, '..')
import auth, chat

CFG = r'D:\Program\AstrBot\AstrBotData\data\config\astrbot_plugin_quillplus_config.json'
BASE = '41d3f134dae8bd4e8063431ee2e8b665'
def md5(): return hashlib.md5(io.open(CFG, 'rb').read()).hexdigest()

assert md5() == BASE, '基线不符，中止（先查是不是有别的进程改了配置）'
c = auth.DashboardClient()
try:
    r = c.plugin('POST', 'config/save_batch', json={'updates': [
        {'group': 'status_bar', 'key': 'enabled', 'value': True},
        # ... 只放白名单内、且本轮确实需要的键
    ]}, timeout=30.0)
    print('save:', (r or {}).get('status'))
    time.sleep(1.5)          # 等配置生效
    # ... 探针主体 ...
finally:
    c.plugin('POST', 'config/save_batch', json={'updates': [
        {'group': 'status_bar', 'key': 'enabled', 'value': False},
    ]}, timeout=30.0)
    time.sleep(2.5)          # 等异步落盘
    now = md5()
    print('md5 =', now, 'OK' if now == BASE else '!!! MISMATCH !!!')
```

**`time.sleep` 不可省**：配置保存是异步落盘的，改完立刻读会读到旧值；
还原同样要等，否则 `md5()` 校验会假报失败。

---

## 附录 B：本轮未覆盖的事（如实说明）

1. **`update_status` 工具化** —— 未做。框架的 `@filter.llm_tool` 参数 schema
   是从 **docstring 的 Args 块**解析的（不是类型标注），而字段名用户可配、
   docstring 在导入时读取，运行期改配置不会重生成 schema。前提不成立。
2. **图片状态栏** —— 未做。框架侧可行（`Comp.Image.fromFileSystem` 等），
   但字体问题需要先决策：仓库现在**没有任何字体文件**（HarmonyOS 已按
   用户要求移除），要自带字体还是走系统字体路径需要用户定。
3. **delta 改独立汇总行** —— 未做（评估后认为不划算，理由见对话记录）。
4. **验收 2.C「围栏平衡修复」** —— 未做，**实测证明不需要**：L1 非贪婪匹配
   天然只吃第一个内层围栏；无内层围栏时落到 L4 也拿到正确字段。已用
   fixture `t17` 锁定现状。
