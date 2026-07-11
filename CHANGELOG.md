# Changelog

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
