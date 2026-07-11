# QuillPlus (羽笔) - 多维沉浸式 RP 增强插件

> 世界书 + 写作素材库 + 角色卡 + 文档 RAG + 动态记忆，五合一沉浸式 RP 增强插件。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-indigo.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/version-5.0.2-green.svg)]()
[![License](https://img.shields.io/badge/license-MIT-orange.svg)]()
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.26.0-purple.svg)]()

---

## 简介

QuillPlus 是一个面向 AstrBot 的沉浸式角色扮演（RP）增强插件。它通过世界书、写作素材库、角色卡、文档 RAG 和动态记忆五个模块的联动，为 LLM 驱动的角色提供结构化记忆、上下文注入和状态追踪能力。

支持通过聊天指令（手机端可用）和 Web 管理面板两种方式进行交互。

---

## 特性

### 角色卡系统 (Character Card V2)

完整实现 Character Card V2 标准，兼容多种导入来源。

- 支持 PNG / JPG / JSON 三种 V2 卡片格式的双向导入导出
- 内置正则解析引擎，支持 W++、Raw Text 等纯文本格式导入
- 头像以 Base64 DataURL 嵌入 API 响应，避免 WebUI iframe 沙箱的跨域限制
- 支持从 Character.AI / Chub 等平台导入卡片

### 世界书系统 (Worldbook)

关键词触发的设定条目管理，prompt 按需注入。

- 常驻条目全局注入，构建稳定基调设定
- 关键词模糊匹配 + 灵敏度调节，命中时注入对应条目
- 支持注入到用户消息之前（前置模式）
- 每个角色可绑定专属世界书集合，角色切换时自动挂载/卸载

### 写作素材库 (Knowledge Base)

145+ 条预设写作指引，覆盖用词、节奏、描写、动作等维度。

- SQLite + FTS5 全文检索，关键词毫秒级命中
- 四层 Prompt 装配（协议层 → 素材层 → 触发层 → 安全层），自动截断
- 不同角色可绑定不同分类，避免设定串扰
- 反拒绝协议：检测 LLM 拒绝行为后注入应急提示

### 文档知识库 (Doc RAG)

上传外部文档，AI 检索后注入 prompt。

- 支持 .txt / .md / .pdf 格式
- 段落优先分块 + 固定长度兜底，overlap 保持上下文连贯
- 支持 API Embedding（如 SiliconFlow）和本地模型 fallback
- 独立 Rerank Provider 提升检索精度

### 动态记忆 (Vector Memory)

自动摘要对话内容，向量化存储后跨会话检索注入。

- 通过 `event.unified_msg_origin` 天然隔离不同会话
- 调用 LLM 将对话精炼为短文本并生成向量
- SQLite BLOB 存储向量 + NumPy 余弦相似度检索
- 聊天指令管理：`/memory list/del/clear/learn/search`

### 状态栏系统 (Status Bar)

追踪角色与用户的交互状态，将 LLM 输出结构化为可读面板。

- 5 级解析器（code block / LOVE_DATA / STATUS / raw / lenient）确保格式兼容
- 工具调用与响应两路钩子协作，避免重复注入
- 关闭状态栏后自动剥离残留格式
- 字段名可自定义，支持分支剧情选项生成

### 安全与并发

- 群聊权限控制：admin_users 白名单仅作用于聊天平台的写指令；Web 面板由 AstrBot 鉴权保护
- 全量 HTML 转义 + 模式值白名单，防止 XSS 注入
- 世界书导入名称校验，仅允许字母数字 / 下划线 / 短横线 / CJK
- 状态文件采用 tmp + fsync + os.replace 原子写入，防崩溃损坏
- FAISS 索引基于 SQLite rowid，L2 归一化确保检索一致性
- 后台任务统一 `_spawn` + `_bg_tasks` 管理，防止 GC 中断

---

## 指令说明

### 角色卡管理 (`/char`)

| 指令 | 说明 |
|------|------|
| `/char` | 列出所有可用角色卡 |
| `/char <序号\|名字>` | 切换到指定角色卡 |
| `/char info [序号\|名字]` | 查看角色卡详情及绑定信息 |
| `/char export [序号\|名字]` | 导出角色卡 JSON |
| `/char import <JSON>` | 从 JSON 文本导入角色卡 |
| `/char unset` | 取消当前角色卡 |

### 世界书管理 (`/wb`)

| 指令 | 说明 |
|------|------|
| `/wb` | 列出世界书 |
| `/wb <序号\|名字>` | 绑定世界书到当前用户 |
| `/wb off` | 解绑全部世界书 |
| `/wb info <序号\|名字>` | 查看世界书详情 |
| `/wb reload` | 从磁盘重载全部世界书 |

### 系统状态 (`/quill`)

| 指令 | 说明 |
|------|------|
| `/quill` | 查看五大系统状态总览 |
| `/quill help` | 折叠式指令速查（按五大系统分组，聊天窗口内可读） |
| `/quill test kb <文字>` | 测试写作素材库匹配 |
| `/quill test wb <文字>` | 测试世界书命中 |
| `/quill test mem <文字>` | 测试记忆检索 |

### 动态记忆 (`/memory`)

| 指令 | 说明 |
|------|------|
| `/memory` | 查看记忆统计 |
| `/memory list [页码]` | 分页列出当前会话记忆 |
| `/memory del <序号>` | 删除指定记忆 |
| `/memory clear` | 清空当前会话所有记忆及对话日志 |
| `/memory learn [内容]` | 手动添加新记忆 |
| `/memory search <关键词>` | 关键词搜索记忆 |

### 文档知识库 (`/doc`)

| 指令 | 说明 |
|------|------|
| `/doc list` | 列出已加载的外部文档 |
| `/doc search <关键词>` | RAG 检索返回原文片段 |
| `/doc bind <序号>` | 绑定文档到当前角色卡 |
| `/doc unbind <序号>` | 解绑文档 |
| `/doc reload` | 重新加载文档索引 |

### 其他控制

| 指令 | 说明 |
|------|------|
| `/stream on\|off\|auto` | 控制流式输出模式 |
| `/reinject` / `/重新注入` | 重置注入状态，触发重新注入常驻内容 |

---

## 管理面板

进入 AstrBot WebUI → 插件 → Pages / 插件配置：

- **角色卡管理**：网格化卡片展示，新建/编辑/删除，V2 卡片导入，纯文本解析，头像上传
- **写作素材库**：全文搜索，分类筛选，条目编辑，匹配测试
- **世界书**：多选配置，条目管理，ST 格式导入/导出
- **文档知识库**：拖拽上传，已上传文档管理，语义检索测试
- **动态记忆**：系统总览，搜索过滤，数据表格，JSON 备份与恢复

---

## 安装

### 依赖

```bash
pip install Pillow>=10.0.0
pip install faiss-cpu>=1.8.0 numpy>=1.24.0 aiosqlite>=0.19.0
```

Web 依赖（fastapi、quart）通常由 AstrBot 自带，缺失时手动安装。

### 插件安装

1. 将插件目录放入 AstrBot 的 `data/plugins/`
2. 启动 AstrBot
3. 进入 WebUI → 插件管理 → 找到"羽笔"
4. 点击"重载"启用插件

> Pillow 未安装时角色卡的 PNG/JPG 导入导出不可用，JSON 格式不受影响。

---

## 配置

通过 AstrBot WebUI 可视化配置，主要分组：

| 分组 | 说明 |
|------|------|
| 世界书 | 开关、容量、Token 上限、匹配灵敏度、注入位置、触发日志 |
| 写作素材库 | 开关、最大注入条数、回退条数、去重上限 |
| RAG | Embedding/Rerank 提供商、本地模型、分块参数、检索数量、记忆开关 |
| 性能 | Prompt 截断上限、最低回复字数 |
| 状态栏 | 开关、字段定义、格式模板、剧情走向选项 |
| 反拒绝 | 开关、匹配模式 |
| 调试 | 调试日志开关、面板主题 |
| 权限 | 管理员 ID 白名单（仅群聊写指令需要） |

> **权限说明**：`admin_users` 仅作用于聊天平台的群聊写指令。配置后仅白名单用户可在群聊执行写操作，留空时群聊写指令被拦截。私聊与 Web 面板编辑不受此限制——Web 面板由 AstrBot 鉴权保护。

---

## 架构

```
astrbot_plugin_quillplus/
├── main.py                  # 插件主类 + LLM hooks + 指令注册
├── web_routes.py            # Web API 路由
├── _route_core.py           # 业务 handler 实现
├── config.py                # 配置解析层
├── persona_manager.py       # 角色卡 JSON CRUD + V2 导入导出
├── worldbook.py             # 世界书 JSON 管理
├── kb.py                    # 写作素材库 SQLite
├── prompt_builder.py        # 四层 Prompt 装配
├── encryption.py            # Base64 编解码
├── state.py                 # 用户状态管理（session_vars 持久化）
├── activation.py            # 激活检测
├── commands.py              # 指令业务逻辑
├── quill_rag/               # RAG + 记忆共享模块
│   ├── embedding.py         # Embedding 封装
│   ├── vector_store.py      # FAISS 向量存储 (Doc RAG)
│   ├── memory_store.py      # SQLite BLOB + NumPy (动态记忆)
│   ├── chunker.py           # 文档分块
│   ├── reranker.py          # Rerank 封装
│   ├── llm_summarizer.py    # LLM 摘要生成
│   └── retrieval.py         # 统一检索入口
├── pages/panel/index.html   # 管理面板
├── knowledge/               # 数据目录（gitignore）
└── worldbooks/              # 世界书目录（gitignore）
```

### LLM Hooks 执行流程

```
on_waiting_llm_request (priority=100)  →  控制流式模式
        ↓
on_llm_request (priority=100)  →  检测激活 + 注入 System Prompt + 改写 tool desc + 追加 tail
        ↓
on_using_llm_tool (priority=200)  →  Markdown 清理 + 状态栏解析/格式化/剥离
        ↓
on_llm_response (priority=10)  →  Base64 解密 + 状态栏兜底/剥离 + 拒绝检测
        ↓
on_llm_tool_respond (priority=10)  →  停止 agent loop + 记忆存储
```

---

## FAQ

**Q: 为什么不直接用市场上的世界书插件？**
A: 大多数世界书插件仅提供关键词注入。QuillPlus 在此基础上补充了角色卡管理、写作素材库、文档 RAG 和动态记忆四个附加模块，构成完整的 RP 工作流。

**Q: 可以在手机端使用吗？**
A: 可以。通过发送指令（`/char`、`/wb`、`/memory`、`/quill` 等）在聊天窗口直接管理功能，无需打开 Web 面板。

**Q: 角色卡和其他插件冲突吗？**
A: QuillPlus 使用独立的 JSON 存储，不依赖 AstrBot 原生的 persona 系统，不会影响其他插件数据。

**Q: Embedding 和 Rerank 模型推荐？**
A: 推荐 SiliconFlow 的 `Qwen3-Embedding-8B` 和 `bge-reranker-v2-m3`。不配置时自动使用本地模型 `BAAI/bge-small-zh-v1.5`。

**Q: 动态记忆会串群吗？**
A: 使用 `event.unified_msg_origin` 作为 session_id，SQL 按 session_id 过滤，天然隔离。

**Q: 状态栏不显示或显示不正确？**
A: 检查配置中状态栏是否开启；确认 LLM 输出格式是否匹配；可尝试开启前置模式提升服从度。

**Q: 群聊指令不能用了？**
A: 检查 `admin_users` 是否已配置。留空时群聊写指令被拦截，配置后仅白名单用户可执行。

**Q: 切换 Embedding 模型后 RAG 检索失效？**
A: 切换提供商会导致向量维度变化，插件会自动检测并重建 FAISS 索引，旧文档需重新上传。

---

## Changelog

### v5.0.2 — P1 体验打磨

针对 longcat2.0 评估报告中的 P1 级问题进行打磨优化：

- **P1-1 状态栏 fallback 调试增强**：所有降级解析均失败时记录原始文本前 200 字到 debug 日志，便于排查 LLM 非标准格式输出
- **P1-2 导入错误详情展示**：新增 `showErrorDetail` 函数，长错误信息（>80 字符）用 modal 显示完整内容，短错误降级为 toast
- **P1-3 长文本 modal 可读性**：角色卡编辑 textarea 行数增大（人格 5→8、场景/范例 4→6、开场白 3→4），line-height 1.6，readonly textarea 使用等宽字体

### v5.0.1 — UX 优化与安全加固

针对 longcat2.0 全链路产品体验评估报告中的 P0 级问题进行专项优化，无破坏性变更：

- **P0-1 会话 ID 友好化展示**：前端将记忆列表/详情中暴露的 `target_id::persona_id` 复合键渲染为「群号/私聊 - 角色名」可读格式，hover 仍可见原始键
- **P0-2 危险操作二次确认**：删除角色卡、清空记忆、解绑世界书等高危操作要求手动输入确认词（角色名/特定词）才执行，红色警示 UI
- **P0-3 `/quill help` 降维速查**：新增折叠式指令速查，20+ 指令按五大系统分组，纯文本输出（`use_t2i(False)`），QQ/Discord 聊天窗口内不刷屏

### v5.0.0 — Initial Public Release (重构首发版)

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

---

## License

本插件遵循 AstrBot 生态惯例，请遵守所使用 LLM 服务商的相关条款。

---

## 鸣谢

感谢原作者 Quill 提供的底层框架。本项目在其基础上进行了深度重构与增强。

- [AstrBot](https://github.com/Soulter/AstrBot) — 提供可扩展的机器人插件框架
