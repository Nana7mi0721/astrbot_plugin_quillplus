# QuillPlus - 羽笔 · 企业级 RP 核心引擎

> **为世界注入灵魂**：世界书 + 写作素材库 + 角色卡 + 文档知识库 + 动态记忆，五合一沉浸式 RP 核心引擎

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-indigo.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/version-5.2.0-green.svg)]()
[![License](https://img.shields.io/badge/license-MIT-orange.svg)]()
[![Authors](https://img.shields.io/badge/authors-Nana7mi0721%20%26%20Gemini%20%26%20GLM--5.2%20%26%20DeepSeek-purple.svg)]()

---

## 🎯 简介

**QuillPlus（羽笔）** 是一个专为沉浸式角色扮演（RP）和跑团（TRPG）设计的 AstrBot 增强插件。它通过五大核心系统的协同联动，让 AI 角色真正"活"起来——拥有自己的记忆、知识、世界设定和灵魂。

无论是手机聊天窗口的快捷指令，还是 PC 端 Web 面板的深度管理，QuillPlus 都能让你在任意终端掌控全局。

---

## 🚀 v5.2 核心架构亮点（竞品绝对没有的杀手级 Feature）

### 🌌 平行宇宙双轴隔离 (target_id::persona_id)

**彻底根治群聊切卡串戏问题**。每个群 × 每个角色 = 独立平行宇宙，状态、记忆、世界书互不干扰。

- 复合主键 `target_id::persona_id` 精确切分状态空间
- 群 A 切角色 X，群 B 切角色 Y，两群对话互不污染
- 同一群切换不同角色，各自保留独立的好感度、剧情进度、记忆

### 💾 JSON 原子化状态机（拔电源级防损坏）

**断电、崩溃、进程被杀，数据零丢失**。

- `tmp` 临时文件 + `f.flush()` + `os.fsync()` + `os.replace()` 四连招
- 状态写入与缓存更新分离，避免锁内 IO 阻塞
- 损坏条目自动跳过并告警，不影响其他用户

### ⚡ 全链路纯异步防阻塞

**AstrBot 事件循环永不卡顿**。所有重 IO / CPU 操作均通过 `asyncio.to_thread()` 卸载到线程池。

- 磁盘读写、SQLite 操作、FAISS 检索全部异步化
- `threading.Lock` 保护 SQLite（`check_same_thread=False`），`asyncio.Lock` 保护缓存状态流转
- 后台任务通过 `_spawn` + `_bg_tasks` 集合防 GC 中断

### 📚 无损对话日志归档（重启零失忆）

**服务器重启、崩溃、手动关机，角色记忆与剧情永远无缝衔接**。

- `on_llm_request` 中检测 `req.contexts` 为空时，自动从 `chat_logs` 表捞取最近 N 条日志垫入
- 配合"First Message 智能抑制"，避免重启后突兀复读开场白
- 无人值守日志清理：反思调度中自动清理过期日志，避免长期运行膨胀



---

## ✨ 核心特性

### 👤 角色卡系统 (Character Card V2)

**全面支持 Character Card V2 标准**，实现真正的"一次导出，到处使用"。

- **V2 标准兼容**：完整支持 PNG / JPG / JSON 三种 V2 卡片格式
- **万能文本导入**：内置强大的正则解析引擎，支持 W++、Raw Text 等社区常见纯文本格式一键粘贴解析
- **沙盒穿透前端**：首创"全链路 Base64"交互方案，完美突破 WebUI iframe 沙箱的跨域与下载限制
- **Avatar 内嵌**：头像以 Base64 DataURL 直接嵌入 API 响应，彻底告别 CORS 问题
- **双向导入/导出**：支持从 Character.AI / Chub / SVG 等平台导入卡片，也能导出为标准 V2 PNG

### 📖 世界书系统 (Worldbook)

复杂角色扮演的基石——不可能把大量设定全部塞进 prompt，世界书让设定"按需唤醒"。

- **常驻注入**：核心世界观时刻陪伴，构建沉浸式基底
- **智能触发**：关键词模糊匹配 + 灵敏度可调，细节设定按需提取
- **贴脸输出**：支持注入到用户消息之前（"贴脸"模式），强制 AI 遵守
- **多角色切换**：每个角色可绑定专属世界书，切换自动挂载/卸载

### 📝 写作素材库 (Knowledge Base)

145+ 条预设写作指引，覆盖用词规范、节奏控制、感官描写、角色动作等。

- **SQLite + FTS5**：全文检索，毫秒级关键词命中
- **四层 Prompt 装配**：协议层 → 素材层 → 触发层 → 安全层，智能截断
- **分类绑定**：不同角色可绑定不同的素材库分类，避免设定串味
- **反拒绝协议**：检测 LLM 拒绝行为，自动注入应急协议

### 📄 文档知识库 (Doc RAG)

上传外部文档（规则书、设定集、小说原文），AI 检索后注入 prompt。

- **多格式支持**：.txt / .md / .pdf
- **智能分块**：段落优先 + 固定长度兜底，overlap 保持上下文连贯
- **向量检索**：支持 API Embedding（如 SiliconFlow）或本地模型 fallback
- **重排精排**：独立 Rerank Provider，提升检索精准度

### 🧠 动态记忆 (Vector Memory)

自动摘要对话，向量化存储，跨会话检索注入。

- **Session 隔离**：`event.unified_msg_origin` 天然分组，绝无串群风险
- **LLM 摘要**：调用配置的 LLM 将对话精炼为短文本
- **NumPy 余弦相似度**：SQLite BLOB 存储向量，矩阵运算极速检索
- **聊天指令管理**：`/memory list/del/clear/learn/search` 全程掌控记忆

### 💘 状态栏系统 (Status Bar) ⭐ v5.2 重构

追踪角色关系状态的变化，将 LLM 输出自动结构化为可读面板。

- **强制格式输出**：通过 5 级解析器（code block / LOVE_DATA / STATUS / raw / lenient）确保 LLM 输出严格符合 `[LOVE_DATA] 好感度|关系阶段|心情|位置|穿着|当前想法` 格式
- **双重防重复**：工具调用钩子与响应钩子智能协作，避免重复注入兜底状态栏
- **禁用绝对保证**：关闭状态栏后，7 重正则剥离所有残留痕迹
- **自定义字段**：支持替换字段名（如催眠度、服从度、发情度等）
- **分支剧情选项**：自动生成 3 个剧情走向选项
- **兜底安全网**：LLM 未输出状态栏时自动注入默认值

### 🛡️ 安全与并发加固 ⭐ v5.2 重构

v5.2 对全量代码进行了安全审计与并发加固，修复 30+ 个问题，覆盖以下维度：

- **群聊权限控制**：admin_users 白名单仅作用于 QQ/Discord 等聊天平台的群聊写指令；Web 面板已由 AstrBot 鉴权保护，无需二次校验
- **XSS 防御**：前端全量 `esc()` 转义 + `jsAttr()` 双重转义 + mode 值白名单校验，防止恶意角色卡注入
- **路径遍历防护**：世界书导入 name 参数校验，仅允许字母数字/下划线/短横线/CJK
- **原子写入**：角色卡/世界书/状态文件统一采用 tmp+fsync+os.replace 模式，防崩溃损坏
- **FAISS 一致性**：ID 基于 SQLite rowid（非 ntotal），L2 归一化使 IndexFlatIP 等价余弦相似度，切换 provider 自动重建索引
- **并发安全**：persona_manager 全链路 deepcopy + `_cache_lock` + `_load_all_locked` 避免 TOCTOU；state.py 锁内序列化 + 锁外写盘
- **后台任务保护**：`_spawn` + `_bg_tasks` 集合保留引用防 GC，terminate 时统一取消等待
- **异常降级**：所有 LLM hooks 顶层 try/except，异常时降级放行不阻塞对话
- **可观测性**：全量静默 except 补 logger 日志，数据库异常可追溯

---

## 🎮 指令说明

### 角色卡管理 (`/char`)

| 指令 | 说明 |
|------|------|
| `/char` | 列出所有可用角色卡 |
| `/char <序号\|名字>` | 切换到指定角色卡 |
| `/char info [序号\|名字]` | 查看角色卡详情及绑定信息 |
| `/char export [序号\|名字]` | 导出角色卡 JSON（可复制复用） |
| `/char import <JSON>` | 从 JSON 文本导入角色卡 |
| `/char unset` | 取消当前角色卡 |

### 世界书管理 (`/wb`)

| 指令 | 说明 |
|------|------|
| `/wb` | 列出世界书（区分角色自带/玩家挂载） |
| `/wb <序号\|名字>` | 绑定世界书到当前用户 |
| `/wb off` | 解绑全部世界书 |
| `/wb info <序号\|名字>` | 查看世界书详情 |
| `/wb reload` | 从磁盘重载全部世界书 |

### 系统状态 (`/quill`)

| 指令 | 说明 |
|------|------|
| `/quill` | 查看五大系统状态总览 |
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
| `/memory learn [内容]` | 手动添加一条新记忆（无内容时增量总结） |
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
| `/reinject` / `/重新注入` | 重置注入状态，下次触发重新注入全部常驻内容 |

---

## 🖥️ 管理面板

进入 **AstrBot WebUI → 插件 → Pages / 插件配置**，左侧导航栏包含：

### 角色卡管理
- 网格化卡片展示，一键切换
- 新建/编辑/删除角色卡（含人设、开场白、对话示例子面板）
- V2 卡片导入（PNG/JPG/JSON 拖拽上传）
- 纯文本粘贴自动解析（W++ / Raw Text）
- 头像上传与预览

### 写作素材库
- 全文搜索 + 分类筛选
- 新建/编辑/删除条目
- 关键词标签管理
- 匹配测试

### 世界书
- 活跃世界书多选配置
- 新建/编辑/删除世界书
- 条目管理（常驻/触发、关键词、内容）
- 绑定/解绑人格和用户
- ST 格式导入/导出

### 文档知识库
- 拖拽上传文档（.txt / .md / .pdf）
- 已上传文档管理
- 语义检索测试

### 动态记忆
- **系统总览**：记忆总数、活跃会话、向量索引状态
- **记忆浏览**：搜索过滤 + 数据表格 + 单条/批量删除
- **导入导出**：JSON 备份与恢复

---

## 🚀 安装说明

### 依赖安装

```bash
# 核心必需（V2 角色卡图片解析）
pip install Pillow>=10.0.0

# Web 服务（通常由 AstrBot 自带，缺失时手动安装）
pip install fastapi quart

# 向量检索 + 本地嵌入 + SQLite 异步
pip install faiss-cpu>=1.8.0 numpy>=1.24.0 aiosqlite>=0.19.0
```

### 插件安装

1. 将插件目录放入 AstrBot 的 `data/plugins/`
2. 启动 AstrBot
3. 进入 WebUI → 插件管理 → 找到"羽笔"
4. 点击"重载"启用插件

> 如果 `Pillow` 未安装，角色卡的 PNG/JPG 导入导出功能将不可用，但 JSON 格式仍然可用。

---

## ⚙️ 配置推荐

通过 AstrBot WebUI 可视化配置，主要分组：

| 分组 | 说明 |
|------|------|
| `[世界书]` | 开关、容量、Token 上限、匹配灵敏度、注入位置、触发日志 |
| `[写作素材库]` | 开关、最大注入条数、回退条数、去重上限 |
| `[RAG]` | Embedding/Rerank 提供商、本地模型、分块参数、检索数量、记忆开关 |
| `[性能]` | Prompt 截断上限、最低回复字数 |
| `[状态栏]` | 开关、字段定义、格式模板、剧情走向选项 |
| `[反拒绝]` | 开关、匹配模式 |
| `[调试]` | 调试日志开关、面板主题 |
| `[权限]` | 管理员 ID 白名单（仅群聊写指令需要） |

> 💡 **权限说明**
>
> `admin_users` 仅作用于 QQ/Discord 等聊天平台的**群聊写指令**（切卡/清记忆/改流式等）。配置后仅白名单用户可在群聊执行写操作，留空时群聊写指令被拦截。**私聊与 Web 面板编辑不受此限制**——Web 面板已由 AstrBot 鉴权保护。

**推荐配置**：
- **群聊使用**：在 `[权限]` 填写 admin_users（通过 `/sid` 获取 UID），仅白名单用户可在群聊执行写指令
- 仅使用关键词匹配 → 无需额外配置，开箱即用
- 启用向量检索 → 在 AstrBot 配置 Embedding Provider，填入 `rag.embedding_provider_id`
- 启用重排 → 配置 `rag.rerank_provider_id`，检索效果显著提升
- 切换 Embedding 提供商 → 已上传的 RAG 文档需重新上传（维度变化会自动重建索引）

---

## 🏗️ 架构

```
astrbot_plugin_quillplus/
├── main.py                  # 插件主类 + LLM hooks + 指令注册
├── web_routes.py            # Web API 路由注册（persona/kb/wb/rag/memory）
├── _route_core.py           # 业务 handler 实现
├── config.py                # 配置解析层
├── persona_manager.py       # 角色卡 JSON CRUD + V2 导入导出
├── worldbook.py             # 世界书 JSON 管理 + 绑定系统
├── kb.py                    # 写作素材库 SQLite
├── prompt_builder.py        # 四层 Prompt 装配
├── encryption.py            # Base64 [B:...] 编解码
├── state.py                 # 用户状态管理（session_vars 持久化）
├── activation.py            # 激活检测
├── commands.py              # 指令业务逻辑
├── quill_desktop.py         # 独立桌面启动器（Quart 服务端）
├── quill_rag/               # RAG + 记忆共享模块
│   ├── embedding.py         # Embedding Provider 封装
│   ├── vector_store.py      # FAISS 向量存储 (Doc RAG)
│   ├── memory_store.py      # SQLite BLOB + NumPy (动态记忆)
│   ├── chunker.py           # 文档分块
│   ├── reranker.py          # Rerank Provider 封装
│   ├── llm_summarizer.py    # LLM 摘要生成
│   └── retrieval.py         # 统一检索入口
├── pages/panel/index.html   # 管理面板（沙盒穿透全链路 Base64）
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

## ❓ FAQ

**Q: 为什么不直接用市场上的世界书插件？**
A: 大多数世界书是关键词提示器，AI 不会主动调用。QuillPlus 是世界书+知识库+角色卡+文档RAG+动态记忆五合一，AI 主动模糊匹配并注入 prompt。

**Q: 可以在手机端使用吗？**
A: 可以！通过发送指令（`/char`、`/wb`、`/memory`、`/quill` 等）在手机聊天窗口直接管理全部功能，无需打开 Web 面板。

**Q: 角色卡和其他插件冲突吗？**
A: 不会。QuillPlus 完全脱离 AstrBot 原生的 persona 系统，使用独立的 JSON 存储，不会影响其他插件的数据。

**Q: Embedding 和 Rerank 模型选什么？**
A: 推荐 SiliconFlow 的 `Qwen3-Embedding-8B` 和 `bge-reranker-v2-m3`。不配置时自动使用本地模型 `BAAI/bge-small-zh-v1.5`。

**Q: 动态记忆会串群吗？**
A: 不会。使用 `event.unified_msg_origin` 作为 session_id，SQL `WHERE session_id = ?` 天然隔离。

**Q: 状态栏不显示或显示不正确怎么办？**
A: 检查配置中的"状态栏→启用"是否开启；确认 LLM 有足够能力遵循格式指令；可尝试开启"贴脸模式"提升服从度。

**Q: 群聊指令不能用了？**
A: 检查 `[权限]` 分组的 `admin_users` 是否已配置（通过 `/sid` 获取 UID）。留空时群聊写指令被拦截，配置后仅白名单用户可在群聊执行写操作。私聊与 Web 面板编辑不受此限制。

**Q: 切换 Embedding 模型后 RAG 检索失效？**
A: 切换提供商会导致向量维度变化，插件会自动检测并重建 FAISS 索引，旧文档需重新上传。这是预期行为，日志会提示"FAISS 索引 dim 不一致，重建索引"。

---

## 📄 License

本插件遵循 AstrBot 生态惯例，请遵守所使用 LLM 服务商的相关条款。

---

## 鸣谢与声明 (Acknowledgments)

**本项目是对原版 Quill 插件的深度二次开发与增强。** 感谢原作者 Quill 提供优秀的底层框架！

Plus 版本新增了以下功能：

- 🎭 **Character Card V2 全量支持**（PNG/JPG/JSON 双向导入导出、Avatar 内嵌、沙盒穿透前端）
- 🔍 **万能文本解析引擎**（W++、Raw Text 一键粘贴解析）
- 📱 **手机端指令系统**（完整覆盖五大系统，聊天窗口即是指令台）
- 🧠 **动态记忆聊天管理**（`/memory learn/list/del/clear/search`）
- 📄 **文档 RAG 聊天检索**（`/doc list/search/bind/unbind/reload`）
- 🔧 **世界书重载**（`/wb reload`）
- 💘 **状态栏系统**（5 级解析器 + 双重防重复 + 自定义字段）
- 🎨 **管理面板 UI 全面升级**
- 🛡️ **安全审计与并发加固**（群聊权限控制、XSS 防御、原子写入、FAISS 一致性、TOCTOU 修复、30+ 问题修复）

🥂 **世界因创作而美好，角色因记忆而鲜活。**
