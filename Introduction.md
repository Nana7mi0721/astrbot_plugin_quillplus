# 羽笔 v5.0 插件架构与配置说明

## 一、整体架构

插件由 **5 大模块** 组成，在一次 LLM 请求生命周期中协同工作：

| 模块 | 核心文件 | 职责 |
|------|----------|------|
| **激活门控** | `activation.py` + `state.py` | 判断是否进入 RP 模式 |
| **Prompt 装配** | `prompt_builder.py` | 4 层 Prompt 结构化组装 |
| **内容注入源** | `kb.py`(WR) + `worldbook.py`(WB) + `persona_manager.py` + `quill_rag/*` | 提供素材/设定/记忆 |
| **输出后处理** | `main.py` 多个 hook | 状态栏解析、拒绝检测、Markdown 清理 |
| **状态持久化** | `state.py` + `persona_manager.py` | 双轴隔离 + 原子写入 |

---

## 二、一次 LLM 请求的完整生命周期

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

**2.9 RAG 检索注入**：追加文档 RAG + 动态记忆到 `dynamic_prompt`。

**2.10 最终合并**：根据 `injection_position` 决定顺序（`system_end` / `user_prefix`）。

**2.11 Tail Message**：若有 persona，在 `req.prompt` 末尾追加状态栏格式强制要求。

### 阶段 3：LLM 推理 + 工具调用

### 阶段 4：工具参数拦截 — `on_using_llm_tool`

**4.1 Markdown 清理**：仅 Telegram 平台。

**4.2 状态栏解析 L1-L5**：

| 层级 | 匹配模式 | 说明 |
|------|----------|------|
| L1 | `**状态栏**\n\`\`\`\n...\n\`\`\`` | 最严格 code block |
| L2 | `[LOVE_DATA] key\|key\|key` | 单行管道分隔 |
| L3 | `[STATUS]...[/STATUS]` | 旧式多行 key=value |
| L4 | 动态字段白名单 `key[：:=→]value` | 用配置的 fields 构建 |
| L5 | Lenient ≥1 字段 | 部分提取 + 历史值融合 |
| L6（可选）| LLM 智能提取 | 3 秒超时，实验性 |

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
| `enable_memory` | `false` | 动态记忆总开关 |
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

### `[refusal]` — 反拒绝

| 配置项 | 默认 | 作用 |
|--------|------|------|
| `enabled` | `true` | 拒绝检测开关 |
| `patterns` | 见配置 | 命中任一模式 → 下轮注入 emergency_protocol |

### `[debug]` / `[permissions]`

| 配置项 | 默认 | 作用 |
|--------|------|------|
| `debug.enabled` | `false` | 详细匹配日志 |
| `debug.panel_theme` | `light` | 面板主题 |
| `permissions.admin_users` | `""` | 群聊写指令白名单，留空时 fail-close |

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

3. **L1-L6 状态栏降级解析**：从最严格的 code block 到最宽松的单字段 + 历史值融合，6 级降级保证状态栏尽可能提取成功。

4. **FAISS + SQLite 事务一致性**：SQLite 先插 pending 行（faiss_id=-1）→ FAISS 写入 → 失败精确回滚 SQLite → 成功回填 faiss_id。

5. **核心记忆三重免疫**：`is_core=1` 的记忆不参与 Top-K 竞争、不参与时间衰减、不被 `prune_memories` 清理。

6. **维度漂移自愈**：启动时检测 FAISS 索引维度 ≠ embedding 维度 → 清空索引 + 重建。

7. **双库分离**：`quill_rag.db`（文档 chunks + FAISS 元数据）与 `quill_memory.db`（memories + chat_logs）物理隔离。