# Changelog

## v5.0.5 — 前端 MD3 重构 + 命名统一 + 代码审查修复

本版本完成前端面板 Material Design 3 重构、写作素材库命名统一（kb→wr）、以及 10 项代码审查修复。无破坏性变更。

**前端 MD3 重构：**
- 前端面板 `pages/panel/index.html` 按 Google Material Design 3 标准全面重构
- 采用 MD3 配色体系、圆角卡片、涟漪按钮、状态层交互等视觉规范
- 响应式布局适配移动端，网格/列表视图切换
- 表单控件统一为 MD3 Filled/Outlined 风格

**命名统一（kb → wr）：**
- 写作素材库类名 `KnowledgeBaseManager` → `WritingResourceManager`（`kb.py`）
- API 路由 `/kb/*` → `/wr/*`（10 条路由，`web_routes.py` + `_route_core.py`）
- 配置节名 `knowledge_base` → `writing_resource`（`_conf_schema.json` + `config.py`）
- SQL 表名 `knowledge_base` → `writing_resource`（含 FTS 虚拟表与触发器）
- DB 文件名自动迁移：`quill_kb.db` → `quill_wr.db`（`main.py` 启动时 `os.rename`）
- 前端 DOM id/函数名/变量名 `kb*` → `wr*`（`index.html`）
- 角色卡扩展字段 `bound_knowledge_base` → `bound_writing_resource`
- 删除所有 kb→wr 向后兼容回退代码（无外部用户，无需兼容）
- 修复表名迁移遗漏：新增 `_migrate_legacy_tables` 自动迁移旧 SQLite 表名

**10 项代码审查修复：**
- P1: `vector_store.py` `add()` 增加 embedding 维度校验，不匹配时抛 `ValueError`
- P2: `state.py` autoflush 增加连续失败上限（3 次），超过后停止后台重试
- P2: `main.py` Prompt 装配失败日志脱敏，只记录 `persona_id` 和字段长度
- P3: `main.py` `req.contexts` 增加 `isinstance(list)` 防御性类型守卫
- P3: `vector_store.py` `delete_by_source` 改为先删 FAISS 再删 SQLite，避免幽灵向量
- P3: `state.py` `_atomic_write` 改用 `tempfile.mkstemp` 生成安全临时文件名
- P3: 清理 `_commit_msg.txt`，`.gitignore` 新增 `.opencode/`、`.trae/`、`_commit_msg.txt`
- P4: `kb.py` `match_count` 更新统一为 `_increment_match_counts` 批量方法
- P4: `kb.py`/`worldbook.py` 异常类型细化（`sqlite3.Error`、`OSError`、`json.JSONDecodeError`）
- P4: `persona_manager.py` 三态 mode 增加白名单校验（`_normalize_mode`），非法值归一化为 `disabled`

**文档更新：**
- 新增 `Introduction.md`：全插件工作流程与配置项说明（6 章节架构文档）
- `README.md` 更新：写作素材库英文名、架构说明、版本号、MD3 重构说明

## v5.0.4 — 核心记忆锚定 + 模型路由 + 多项 Bug 修复

本版本新增两项核心功能（P0 级升级）并修复多个影响体验的 Bug，无破坏性变更。

**P0-1 核心记忆锚定（Core Memory Anchoring）：**
- 新增 `is_core` 字段（SQLite 热迁移，兼容老数据库），支持将关键记忆钉住为核心锚定记忆
- 核心记忆**不参与 Top-K 竞争**，无条件注入 `<core_memory>` XML 标签，类似人设基石
- `prune_memories` 遗忘清理跳过核心记忆（`is_core=1` 永不清理）
- Web 面板记忆列表新增钉住/取消钉住按钮 + 核心锚定标签（黄色高亮整行）
- 记忆详情弹窗显示核心锚定状态
- 新增 API：`POST /memory/pin`（`{memory_id, is_core}`）

**P0-2 模型路由（Model Routing）：**
- 状态栏 LLM 智能提取支持独立配置 provider（`status_bar.llm_provider_id`）
- 留空时自动回退到 RAG 摘要 LLM（`rag.llm_provider_id`），向后兼容
- 建议配置轻量/便宜的模型（如 GPT-4o-mini）做 JSON 提取，降低成本
- 前端状态栏配置卡片新增 LLM 模型下拉框

**Bug 修复：**
- **私聊管理员权限误判**：`_check_group_permission` 用字符串 `"PrivateMessage"` 判断私聊，但 AstrBot 统一使用 `MessageType.FRIEND_MESSAGE` 枚举，导致私聊用户被误判为无权限；改用枚举判断
- **`/quill reset` 后旧记忆残留**：reset 时 `persona_id` 为空只清理了 `target_id` 的日志，未清理 `target_id::persona_id` 的旧日志；新增 `delete_all_session_memories` 和 `delete_all_session_chat_logs`，用 SQL `LIKE target_id::%` 批量清理
- **流式控制按钮报错**：后端返回扁平结构 `{"status":"ok","message":...}` 但前端 `api()` 期望 `result.data` 字段；统一为 `{"status":"ok","data":{...}}` 格式
- **头像裁剪 `request.args` 报错**：`PluginRequest` 对象没有 `args` 属性；`persona_avatar` 和 `persona_export` 端点改用 `request.query.get()`
- **头像重新裁剪只能基于小图**：裁剪后的 300×300 小图覆盖了 `avatar_path`，重新裁剪时拿到的是已裁剪的小图；新增 `_originalAvatarDataUrl` 缓存，上传时缓存原图，重新裁剪时优先用缓存
- **删除确认按钮无法点击**：角色名含特殊字符（如 `│`）时全名匹配失败；改为首词匹配（输入第一个空格前的单词即可确认）
- **管理员白名单重载后丢失**：前端发送 `permission.admin_users` 但后端 schema 定义为 `permissions.admin_users`，键名不匹配；统一为 `permissions`

**新功能：**
- **Web 面板流式输出控制**：基础配置区域新增流式模式批量控制卡片，支持一键设置所有会话的流式模式（自动/开启/关闭），含统计信息
- **头像自定义裁剪**：Web 面板角色卡编辑弹窗内置裁剪器，支持拖拽移动 + 滚轮缩放，Canvas 生成 300×300 方形 PNG 上传
- **指令回复文本全面修正**：全量核查 3 个文件（commands.py / main.py / _route_core.py）共 15 处不一致，修复 9 处

## v5.0.3 — UI 优化 + 代码冗余清理

本版本聚焦于前端管理面板的 UI/UX 打磨与全代码库的冗余清理，无破坏性变更。

**UI 界面优化：**
- **P0-P3 全量优化**：实施 qwen3.7-plus 版本对比报告中的全部 P0-P3 优化建议（图标可以有但不滥用 emoji）
- **统一骨架屏**：KB 列表（skeleton-card）、WB/角色卡列表（skeleton-item）、角色卡导入/RAG 检索状态统一使用骨架屏占位
- **统计数字语义色**：健康状态统计应用 `stat-value--success/--warning/--danger` 语义色类，替代内联样式
- **卡片折叠**：状态栏配置卡 + RAG/动态记忆卡支持折叠，状态持久化到 localStorage
- **修复删除二次确认按钮 Bug**：`sandboxConfirm` 函数中 `btn.disabled` 状态管理缺陷 — 危险操作后所有后续确认按钮保持 disabled 不可点击；修复为函数入口重置 + input 事件监听启用

**HTML 体积优化（178KB → 170KB，节省 2.4%）：**
- 内联 style 批量抽取为 CSS 工具 class（`.text-muted-12`、`.hint-block`、`.danger-box` 等）
- JS helper 函数抽取（`apiPost()`、`downloadJSON()`、`renderWBEntryCard()`）
- 模板抽取（WB 条目卡片渲染）
- 删除 CSS 重复定义、未使用 class、未使用 JS 函数/变量

**Python 代码冗余清理：**
- **P0 修复**：`commands.py:709` 键名不匹配 — 读取 `total_documents` 但 `vector_store.py` 返回 `total_docs`，导致 `/quill status` 的 Doc RAG 计数永远为 0
- **P1 补全**：`reranker.py` 的 `fallback_llm_id` 属性此前被存储但 LLM 降级重排逻辑未实现；新增 `_rerank_with_llm` 方法，在 rerank provider 不可用时通过 LLM 对候选内容打分排序
- **删除 15 处未使用 import**（涉及 10 个文件）
- **清理 3 处局部 re-import 遮蔽**（`web_routes.py` 的 `tempfile`/`os` 和 `file_response`）
- **删除 9 项死代码**：
  - `config.py` — `_DEFAULTS` 字典（46 行，从未被读取）
  - `prompt_builder.py` — `status_bar_prompt_template` 属性
  - `kb.py` — `@conn.setter`、`get_entry_by_id` 方法
  - `web_routes.py` — `serve_panel` 路由（引用不存在的 `web_panel/` 目录）
  - `memory_store.py` — `list_sessions` 方法
  - `state.py` — `is_first_message_injected` 方法
  - `retrieval.py` — `store_memory` 方法（已被 `store_memory_direct` 取代）
  - `persona_manager.py` — `invalidate_cache` 方法
- **异常静默吞噬修复**：4 处 `except Exception: pass` 改为 `logger.debug(..., exc_info=True)`，符合项目硬约束
- **删除不可达分支**：`web_routes.py` 的 `wb_reload` 中 `hasattr` 永远为 True 的 `_load_all` fallback 分支

## v5.0.2 — P1 体验打磨 + 状态栏智能纠正

针对 longcat2.0 评估报告中的 P1 级问题进行打磨优化，并新增状态栏智能纠正管线：

**P1 体验打磨：**
- **P1-1 状态栏 fallback 调试增强**：所有降级解析均失败时记录原始文本前 200 字到 debug 日志
- **P1-2 导入错误详情展示**：新增 `showErrorDetail` 函数，长错误信息用 modal 显示完整内容
- **P1-3 长文本 modal 可读性**：角色卡编辑 textarea 行数增大，line-height 1.6，readonly 用等宽字体
- **P1-4 RAG 异常感知面板**：配置页新增「系统健康度」卡片，滑动窗口记录最近 20 次 RAG 检索/状态栏解析成功率，三色阈值显示
- **P1-5 角色卡网格懒加载**：`<img loading="lazy">` + IntersectionObserver 无限滚动

**状态栏智能纠正（A+B+C+D 方案）：**
- **方案A 动态字段白名单**：L4 正则改为动态构建，支持用户自定义字段名；分隔符扩展 `[：:=→]` 覆盖非标准格式
- **方案B 部分提取+历史值融合**：L5 阈值从 ≥2 降为 ≥1，即使只匹配到 1 个字段也保留 LLM 值，其余用历史值补全
- **方案C LLM 智能提取（实验性）**：L1-L5 全失败时调用轻量 LLM 做结构化提取，3s 超时保护，配置开关控制（默认关闭）
- **方案D Prompt 增强**：状态栏协议加入格式强制强调 + 负例展示 + 正确示例，从源头减少格式错误

## v5.0.1 — UX 优化与安全加固

针对 longcat2.0 全链路产品体验评估报告中的 P0 级问题进行专项优化，无破坏性变更：

- **P0-1 会话 ID 友好化展示**：前端将记忆列表/详情中暴露的 `target_id::persona_id` 复合键渲染为「群号/私聊 - 角色名」可读格式，hover 仍可见原始键
- **P0-2 危险操作二次确认**：删除角色卡、清空记忆、解绑世界书等高危操作要求手动输入确认词（角色名/特定词）才执行，红色警示 UI
- **P0-3 `/quill help` 降维速查**：新增折叠式指令速查，20+ 指令按五大系统分组，纯文本输出（`use_t2i(False)`），QQ/Discord 聊天窗口内不刷屏

## v5.0.0 — Initial Public Release (重构首发版)

底层重构后的首次正式公开发布。相较于之前的内部版本，v5.0 包含以下主要变更：

- **平行宇宙双轴隔离**：通过 `target_id::persona_id` 复合主键实现同群多角色的记忆与状态隔离
- **JSON 原子化状态机**：tmp + fsync + os.replace 四步写入，避免崩溃数据损坏
- **全链路异步化**：磁盘 IO、SQLite、FAISS 检索均通过 `asyncio.to_thread` 卸载，不阻塞事件循环
- **Character Card V2 全量支持**：PNG / JPG / JSON 双向导入导出，Base64 DataURL 嵌入
- **万能文本解析引擎**：W++、Raw Text 纯文本粘贴自动解析
- **无损对话日志归档**：Context Restoration 断点续传，服务器重启后自动恢复上下文
- **安全审计与并发加固**：XSS 转义、路径遍历校验、原子写入、FAISS 一致性、TOCTOU 修复
- **管理面板 UI 重写**：角色卡 / 世界书 / 素材库 / RAG / 动态记忆统一管理
- **配置可视化**：通过 AstrBot _conf_schema.json 统一管理，无需手动编辑配置文件
