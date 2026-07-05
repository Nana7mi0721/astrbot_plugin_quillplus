# QuillPlus - 羽笔 · 创意写作增强插件

> **为世界注入灵魂**：世界书 + 写作素材库 + 角色卡 + 文档知识库 + 动态记忆，五合一 RP 增强引擎

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-indigo.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/version-5.0.0-green.svg)]()
[![License](https://img.shields.io/badge/license-MIT-orange.svg)]()

---

## 🎯 简介

**QuillPlus（羽笔）** 是一个专为沉浸式角色扮演（RP）和跑团（TRPG）设计的 AstrBot 增强插件。它通过五大核心系统的协同联动，让 AI 角色真正"活"起来——拥有自己的记忆、知识、世界设定和灵魂。

无论是手机聊天窗口的快捷指令，还是 PC 端 Web 面板的深度管理，QuillPlus 都能让你在任意终端掌控全局。

---

## ✨ 核心特性 (Key Features)

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
| `/memory clear` | 清空当前会话所有记忆 |
| `/memory learn <内容>` | 手动添加一条新记忆 |
| `/memory search <关键词>` | 关键词搜索记忆 |

### 文档知识库 (`/doc`)

| 指令 | 说明 |
|------|------|
| `/doc list` | 列出已加载的外部文档 |
| `/doc search <关键词>` | RAG 检索返回原文片段 |

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
- 链接触发器管理

### 动态记忆
- **系统总览**：记忆总数、活跃会话、向量索引状态
- **记忆浏览**：搜索过滤 + 数据表格 + 单条/批量删除
- **导入导出**：JSON 备份与恢复
- **链接触发器触发历史**

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
| `[状态栏]` | 开关、字段定义、格式模板 |
| `[Agent]` | 工具调用后停止开关 |
| `[反拒绝]` | 开关、匹配模式 |
| `[调试]` | 调试日志开关 |

**推荐配置**：
- 仅使用关键词匹配 → 无需额外配置，开箱即用
- 启用向量检索 → 在 AstrBot 配置 Embedding Provider，填入 `rag.embedding_provider_id`
- 启用重排 → 配置 `rag.rerank_provider_id`，检索效果显著提升

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
├── state.py                 # 用户状态管理
├── activation.py            # 激活检测
├── commands.py              # 指令业务逻辑（19 个 dispatch 函数）
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
- 📄 **文档 RAG 聊天检索**（`/doc list/search`）
- 🔧 **世界书重载**（`/wb reload`）
- 🎨 **管理面板 UI 全面升级**

🥂 **世界因创作而美好，角色因记忆而鲜活。**
