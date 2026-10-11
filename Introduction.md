# 羽笔 v5.3 插件架构与配置说明

## 一、整体架构

运行时为「注册桩 → 适配层 → 服务层」三级结构（v5.3.0 重构），由 **5 大功能模块** 组成，
在一次 LLM 请求生命周期中协同工作：

| 层级 | 核心文件 | 职责 |
|------|----------|------|
| 注册桩 | `main.py` | `@register` 元数据 + 6 个 AstrBot 钩子的薄转发，不含业务逻辑 |
| 适配层 | `interfaces/` | AstrBot API 适配：`astrbot_hooks.py` 负责注入编排，provider / `kb_manager` 等由此取用 |
| 服务层 | `quill/` | 纯逻辑服务（配置投影、状态、Prompt 装配、状态栏、记忆…），**不 import astrbot** |

| 模块 | 核心文件 | 职责 |
|------|----------|------|
| **激活门控** | `activation.py` + `state.py` | 判断是否进入 RP 模式 |
| **Prompt 装配** | `prompt_builder.py` + `quill/services/prompt.py` | 4 层 Prompt 结构化组装 |
| **内容注入源** | `kb.py`(WR) + `worldbook.py`(WB) + `persona_manager.py` + `quill_rag/*` | 提供素材/设定/记忆/文档 |
| **输出后处理** | `main.py` 转发的 hook + `quill/services/statusbar/*` | 状态栏解析、拒绝检测、Markdown 清理 |
| **状态持久化** | `state.py` + `persona_manager.py` | 双轴隔离 + 原子写入 |
| **Web 面板** | `web_routes.py`(HTTP) + `_route_core.py`(纯逻辑) + `pages/panel/` | 配置与内容管理界面 |

与 AstrBot 原生数据的两处接入（v5.3.2，均为**只读**，不改动 AstrBot 侧数据）：

- `quill_rag/astrbot_kb.py` —— 把 AstrBot 面板「知识库」里已配置的库并入可绑定源
  （`kb:库名`），检索经 `context.kb_manager`；由 `rag.astrbot_kb_enabled` 总开关控制。
- `persona_import.py` —— 把 AstrBot 面板「人格」经摘要模型转换为角色卡（「由人格导入」按钮），
  人格本身不被修改、不被删除。

---

## 二、一次 LLM 请求的完整生命周期

> 下列钩子的实现都在 `interfaces/astrbot_hooks.py`（`main.py` 只做转发）；钩子内部调用的服务
> 位于 `quill/`（纯逻辑）与 `quill_rag/`（检索组件）。

### 阶段 1：流式决策 — `on_waiting_llm_request`

读取 `state.stream_mode`：
- `"off"` → 强制关闭流式
- `"on"` → 强制开启
- `"auto"`（默认） → 激活词/【】括号命中时关闭流式（保证状态栏正则解析可靠）

### 阶段 2：激活检测 + Prompt 装配 — `on_llm_request`

**2.1 恢复工具描述**：`_restore_smt_tool` 先恢复上一轮被改写的 `send_message_to_user` 工具描述。

**2.2 上下文恢复**：若 `req.contexts` 为空且 `rag.enable_chat_logging=True`，从对话日志拉最近 8 条垫入。

**2.3 角色卡注入**：
- 切断 AstrBot 原生人格（`req.conversation.persona_id = "[%None]"`）
- 首次激活时注入 `core_prompts.first_message`
- 切换角色卡时自动重置 `first_message_injected=False`

**2.4 激活检测**：三层并联判定 — 激活词匹配 / CJK【】方括号检测 / WR 关键词回退激活。`always_activate=True` 时短路跳过所有检测。

**2.5 工具描述重写**：仅当有 persona_id 时重写 `send_message_to_user` 工具描述为强约束版本。

**2.6 emergency 检测**：若上一轮检测到 refusal，本轮注入 `emergency_protocol`。

**2.7 构建 extra_info**：包含 11 个字段（`user_input`、`context_text`、`persona_data` 等）。

**2.8 调用 PromptBuilder**：返回 `(stable_prompt, dynamic_prompt)`。

**2.9 RAG 检索注入**：追加文档 RAG + 动态记忆到 `dynamic_prompt`。文档源的绑定标识（`source`）
有四种形态，`allowed_sources` 为精确字符串匹配：

| 绑定写法 | 含义 |
|----------|------|
| `文件名` | 本插件上传的文档（走插件自己的向量库与检索预算） |
| `grp:分组名` | 插件内子知识库（分组）。纯元数据、共用同一份 FAISS 索引，检索前展开成组内文件名列表 |
| `kb:库名` | AstrBot 内置知识库整库（只读，需 `rag.astrbot_kb_enabled`） |
| `kb:库名/文档名` | 内置库中的单篇文档：候选池放大到 30 再按 `doc_name` 过滤，最后截回 `rag.top_k` |

本地路与内置库路**各自独立成败**：必须分别取出失败标记后再合并（`list(x)` / `extend` 会丢掉
`RagResult` 子类上的 `_rag_ok` / `_rag_error` 标记），健康度记入 `health_tracker.record_rag`。

**2.10 最终合并**：根据 `injection_position` 决定顺序（`system_end` / `user_prefix`）。

**2.11 Tail Message**：若有 persona，在 `req.prompt` 末尾追加状态栏格式强制要求。

### 阶段 3：LLM 推理 + 工具调用

### 阶段 4：工具参数拦截 — `on_using_llm_tool`

**4.1 Markdown 清理**：仅 Telegram 平台。

**4.2 状态栏解析 L1-L6**（实现为分级注册表 `_SB_LEVELS`，越靠前越严格、命中即停）：

| 层级 | 匹配模式 | 说明 |
|------|----------|------|
| L1 | `**状态栏**\n\`\`\`\n...\n\`\`\`` | 最严格 code block；只替换块内内容，保留外围 Markdown |
| L2 | `[LOVE_DATA] key\|key\|key` | 单行管道分隔。**实际最常命中**的一级 |
| L3 | `[STATUS]...[/STATUS]` | 旧式多行 key=value |
| L4 | 动态字段白名单 `key[：:=→]value` | 用配置的 fields 构建。去重后 ≥2 才重建整栏；单命中只剥离该行并**继续下降** |
| L5 | Lenient ≥2 字段 | 部分提取 + 历史值融合（阈值 ≥2 由解析器内部把关） |
| L6（可选）| LLM 智能提取 | 3 秒超时，实验性，默认关闭 |

每级命中会记入 `HealthTracker`，可用 `/quill debug` 查看逐级分布
（例如 `降级链命中: LOVE_DATA inline×3`）。单级抛异常只降级该级、
继续往下走，不会让整条链失效。

**4.3 拒绝检测**：扫描 `tool_args.messages` 中的 refusal patterns。

**4.4 状态栏变量持久化**：`update_session_vars` 更新到 `quill_state.json`。

### 阶段 5：响应后处理 — `on_llm_response`

Base64 解密 + 状态栏二次处理 + 兜底注入 + refusal 扫描。

### 阶段 6：Agent Loop 终止 + 反思触发 — `on_llm_tool_respond`

终止 Agent Loop + 对话日志写入 + 反思摘要触发。

---

## 三、4 层 Prompt 装配

| 层 | 优先级 | 内容 | 稳定/动态 |
|----|--------|------|-----------|
| Layer 0 | 0（永不截断）| anti_refusal 协议 + send_message_guide + status_bar_guide + session_state + persona_card + emergency_protocol | stable |
| Layer 1 | 3 | WR 常驻素材 + WB 常驻条目 + 随机池采样 | stable（常驻）+ dynamic（随机池）|
| Layer 2 | 10（先丢弃）| WR 关键词匹配 + WB 关键词匹配 | dynamic |
| Layer 3 | 1 | safety_wrapper（强调工具使用） | dynamic |

**截断策略**：仅截断 dynamic sections。按优先级丢弃（Layer 2 先丢）。

### 三态模式（auto / custom / disabled）

每个内容源（WR/WB/RAG）在角色卡 `quill_extensions` 中独立配置：

| 模式 | 行为 |
|------|------|
| `disabled` | 完全跳过 |
| `auto` | 使用全库 |
| `custom` | 只用绑定的列表 |

`custom` 的绑定值支持**三级粒度**（角色卡「高级扩展」页的勾选树）：大组全选（纯 UI 层）→
子知识库组（`grp:组名` / `kb:库名`）→ 单篇（`文件名` / `kb:库名/文档名`）。
`grp:` 在检索前展开：组不存在则忽略该绑定，组为空则展开为空（**不退化**成检索全库）。

---

## 四、各配置项在流程中的作用

### `[rag]` — 文档知识库与动态记忆

| 配置项 | 默认 | 作用 |
|--------|------|------|
| `llm_provider_id` | `""` | 摘要 LLM + Reranker fallback |
| `embedding_provider_id` | `""` | 向量化。留空用本地 `BAAI/bge-small-zh-v1.5`（512 维）。切换后旧文档需重新上传 |
| `rerank_provider_id` | `""` | 二次精排。留空跳过精排 |
| `enable_local_embedding` | `true` | API 失败时回退本地模型 |
| `chunk_size` | `500` | 文档分块字符数 |
| `chunk_overlap` | `50` | 相邻块重叠 |
| `top_k` | `3` | 最终注入片段数 |
| `dense_top_k` | `5` | FAISS 粗排数量 |
| `astrbot_kb_enabled` | `true` | 把 AstrBot 内置知识库并入可绑定源（`kb:库名`）。关闭后完全不触碰 `kb_manager` |
| `enable_memory` | `false` | 动态记忆总开关 |
| `enable_autonomous_reflection` | `true` | 闲时反思守护进程：每小时扫描空闲会话，自动总结并提纯记忆 |
| `enable_chat_logging` | `true` | 对话日志归档 |
| `chat_log_retention_days` | `30` | 日志保留天数 |

### `[worldbook]` — 世界书

| 配置项 | 默认 | 作用 |
|--------|------|------|
| `enabled` | `true` | 全局开关 |
| `max_dynamic_entries` | `4` | Layer 2 关键词触发上限 |
| `max_token_limit` | `4000` | 注入 Token 估算上限 |
| `match_sensitivity` | `0.7` | 关键词匹配灵敏度 |
| `injection_position` | `user_prefix` | `system_end` / `user_prefix`（推荐） |
| `show_trigger_log` | `false` | 调试：查看最近命中词条 |
| `always_activate` | `false` | 全局常驻激活 |

### `[writing_resource]` — 写作素材库

| 配置项 | 默认 | 作用 |
|--------|------|------|
| `enabled` | `true` | 全局开关 |
| `max_entries` | `4` | 单次注入素材上限 |
| `fallback_top_count` | `2` | 无匹配时兜底 |
| `category_dedup_limit` | `3` | 同分类最多注入条数 |

### `[performance]` — 表现控制

| 配置项 | 默认 | 作用 |
|--------|------|------|
| `max_prompt_length` | `50000` | Prompt 硬截断上限 |
| `min_output_length` | `400` | 最低回复字数 |
| `max_output_length` | `0` | 限制最高字数，0=不限 |

### `[status_bar]` — 状态栏

| 配置项 | 默认 | 作用 |
|--------|------|------|
| `enabled` | `true` | 核心开关 |
| `fields` | `好感度\|关系阶段\|心情\|位置\|穿着\|当前想法` | L4 动态构建正则的字段白名单 |
| `format_template` | 见代码 | UI 渲染样式 |
| `plot_paths` | `继续当前话题\|转换场景\|结束互动` | 分支选项 |
| `llm_extract` | `false` | LLM 智能提取（实验性） |
| `llm_provider_id` | `""` | 状态栏提取 LLM，留空回退到 RAG 摘要 LLM |
| `default_placeholder` | `未设置` | 字段缺位时的占位文案 |
| `show_delta` | `true` | 与上一轮比较并标注变化（↑↓） |
| `format_template_plain` | 见代码 | 纯文本平台（如 QQ）的渲染模板 |
| `plain_platforms` | `""` | 强制走纯文本模板的平台列表，留空用内置默认 |
| `jev_enabled` | `false` | JEV 判定模式开关（**面板专有键**，不在 `_conf_schema.json`） |
| `jev_provider_id` | `""` | JEV 判定所用 AstrBot 提供商（需指向 `api.typesafe.ai`、模型 `jev-latest`） |
| `jev_confidence_floor` | `0.6` | 置信度下限，低于此值判定作废、静默跳过 |

> JEV 三个键由面板「配置 → 状态栏」卡片写入（`_ALLOWED_CONFIG_KEYS` 放行），AstrBot 原生配置页
> 不显示它们。JEV 只做结构化判定（`noul` / `choice` / `score`），不生成文本；失败一律 fail-open。

### `[refusal]` — 反拒绝

| 配置项 | 默认 | 作用 |
|--------|------|------|
| `enabled` | `true` | 拒绝检测开关 |
| `patterns` | 见配置 | 命中任一模式 → 下轮注入 emergency_protocol |

### `[debug]` / `[permissions]`

| 配置项 | 默认 | 作用 |
|--------|------|------|
| `debug.enabled` | `false` | 详细匹配日志 |
| `debug.show_inject_report` | `false` | 每轮在回复末尾附注入报告（各段命中/降级情况） |
| `permissions.admin_users` | `""` | 群聊写指令白名单，留空时 fail-close |

**写指令与权限门**：所有会改动持久状态的子命令都过 `_check_group_permission`
（`commands.py:33`）。私聊一律放行；群聊仅 `admin_users` 白名单内用户可执行，
白名单留空时 fail-close 并回显当前发送者 ID 引导配置。

覆盖的写操作：切换/取消/导入角色卡、绑定/解绑世界书与文档、重载世界书与文档索引、
删除/清空/写入记忆（含 `@记住：` 自然语言入口）、重开剧情、重置注入状态、
流式与状态栏的**写入**（无参数查询不受限）、`/quill debug`。读类指令
（list / info / search / 各类无参数状态查询）在群聊对所有人开放。

---

## 五、状态隔离与持久化机制

### 双轴隔离

```
target_id（会话维度：群+用户）
    └─::persona_id（角色卡维度）
        → mem_session_id = "{target_id}::{persona_id}"
```

同一用户同群切换不同角色卡时，记忆库完全隔离，根治群聊切卡串戏。

### 分级落盘

| 策略 | 字段 | 原因 |
|------|------|------|
| 即时落盘 | `persona_id`、`first_message_injected`、`refusal_detected` 等 | 丢失会导致行为异常 |
| 批量 autoflush（5s） | `round_count`、`quill_rounds`、`session_vars` 等 | 高频字段，写放大严重 |

**原子写入四连**：`tmp` → `flush` → `fsync` → `os.replace`，拔电源级防损坏。

### 反思/总结触发

```
每轮 AI 回复 → increment_unsummarized_turns
                ↓
        unsummarized >= 4?  ── No → 等待
                ↓ Yes
        reset + get_recent_chat_logs(8)
                ↓
        len >= 2?  ── No → 跳过
                ↓ Yes
        summarize_contexts (LLM 压缩为 ≤80 字摘要)
                ↓
        prune_memories + cleanup_chat_logs
```

三个常量：
- `REFLECTION_TURN_THRESHOLD=4`：触发频率
- `RECENT_LOG_LIMIT=8`：总结输入范围
- `MIN_LOGS_FOR_SUMMARY=2`：最小数据门槛

---

## 六、核心设计要点

1. **4 层 Prompt 优先级截断**：Layer 0（协议）永不丢，Layer 2（关键词）先丢，保证协议完整性的同时控制 token。

2. **工具描述重写的必要性**：Prompt 强约束"必须用工具"与工具原始软约束"可选"冲突会导致 Agent Loop 死循环。有 persona 才重写。

3. **L1-L6 状态栏降级解析**：从最严格的 code block 到最宽松的单字段 + 历史值融合，6 级降级保证状态栏尽可能提取成功。实现为分级注册表（`_SB_LEVELS`），每级命中计入健康度、可用 `/quill debug` 查看分布；单级异常只降级该级不掀翻整链。

4. **FAISS + SQLite 事务一致性**：SQLite 先插 pending 行（faiss_id=-1）→ FAISS 写入 → 失败精确回滚 SQLite → 成功回填 faiss_id。

5. **核心记忆三重免疫**：`is_core=1` 的记忆不参与 Top-K 竞争、不参与时间衰减、不被 `prune_memories` 清理。

6. **维度漂移自愈**：启动时检测 FAISS 索引维度 ≠ embedding 维度 → 清空索引 + 重建。

7. **双库分离**：`quill_rag.db`（文档 chunks + FAISS 元数据）与 `quill_memory.db`（memories + chat_logs）物理隔离。

8. **全自动自迭代记忆（v5.1）**：闲时检测 + 反思守护进程自动分析未提纯记忆，总结核心特质与关键事实，迭代角色深层设定，无需人工干预。

9. **混合检索（v5.1）**：FTS5 (BM25) + Vector 混合检索，RRF 融合排序 + Ebbinghaus 时间衰减过滤，兼顾语义匹配与时效性。

10. **LRU 会话缓存（v5.1）**：基于 `OrderedDict` 的 LRU 缓存加速同会话反复检索，减少磁盘与序列化开销。

11. **对话日志查看器（v5.2）**：Web 面板新增「对话日志」子页，按会话浏览/导出 RP 对话记录，方便复盘与调试。

12. **三级运行时结构（v5.3.0）**：`main.py`（注册桩，只有 `@register` 与钩子转发）→ `interfaces/`（AstrBot 适配，唯一的框架接触面）→ `quill/`（服务层，**不 import astrbot**）。这样服务层可脱离框架推理与复用，钩子签名变化只影响适配层一处；激活判定同时改为 fail-close（检测失败按未激活处理，绝不退化成每轮全量注入）。

13. **JEV 结构化判定（v5.3.1）**：剧情分支与状态栏判定交给 TypeSafe System One（复用 AstrBot 的 openai_compatible 提供商配置，直连 `/v1/systemone`，因为 Jev 没有 `chat/completions` 端点）。它只输出类型化判定与置信度、不生成文本；显式数字回复（1/2/3）本地精确匹配、零消耗；任何失败 fail-open，绝不影响正文渲染。

14. **与 AstrBot 原生数据双向兼容（v5.3.2）**：① 内置知识库并入可绑定源（`kb:库名` / `kb:库名/文档名`，只读，总开关 `rag.astrbot_kb_enabled`）；② AstrBot 人格经摘要模型只读转换为角色卡（「由人格导入」，人格本身不变）；③ 插件内文档分组（`grp:组名`）作为**纯元数据**层，与单篇绑定共用同一份 FAISS 索引与检索预算，检索前才展开成文件列表。
