# QuillPlus - 羽笔 · 创意写作增强插件

> **为世界注入灵魂**：世界书 + 写作素材库 + 角色卡 + 文档知识库 + 动态记忆，五合一 RP 增强引擎

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-indigo.svg)](https://github.com/AstrBotDevs/AstrBot)

---

## ✨ 核心特性

### 📖 世界书系统 (WHERE)

解决复杂角色扮演中 System Prompt 容量受限的痛点——不可能把大量设定全部塞进 prompt。

- **常驻注入**：核心世界观时刻陪伴，构建沉浸式基底
- **按需触发**：关键词模糊匹配，细节设定按需提取，杜绝 Token 爆炸
- **智能去重**：同 category 条目自动上限控制，避免某一类内容过多
- **Token 安全上限**：硬性截断保护，永远挤占不了主对话空间
- **灵敏度可调**：从精确匹配到宽松匹配，滑动条一键控制
- **注入位置可选**：系统提示词末尾 / 用户消息之前，两种"服从度"模式

### 📝 写作素材库 (HOW)

145 条预设写作指引，覆盖用词规范、节奏控制、感官描写、角色动作等。

- **SQLite + FTS5**：全文检索，毫秒级关键词命中
- **四层 Prompt 装配**：协议层 → 素材层 → 触发层 → 安全层，智能截断
- **反拒绝协议**：检测 LLM 拒绝行为，自动注入应急协议
- **状态栏持久化**：`[LOVE_DATA]` 单行状态追踪，跨轮次记忆好感度、关系阶段等

### 👤 角色卡 (WHO)

一键切换 AstrBot 人格列表中的角色卡，自动绑定对应世界书。

### 📄 文档知识库 (Doc RAG)

上传外部文档，语义检索后注入写作 prompt。

- **多格式支持**：.txt / .md / .pdf
- **智能分块**：段落优先 + 固定长度兜底，overlap 保持上下文连贯
- **向量化检索**：支持 API Embedding（如 SiliconFlow）或本地模型 fallback
- **重排精排**：独立 Rerank Provider，提升检索精准度

### 🧠 动态记忆 (Vector Memory)

自动摘要对话，向量化存储，跨会话检索注入。

- **Session 隔离**：`event.unified_msg_origin` 天然分组，绝无串群风险
- **LLM 摘要**：调用配置的 LLM 将对话精炼为短文本（支持规则摘要 fallback）
- **NumPy 余弦相似度**：SQLite BLOB 存储向量，矩阵运算极速检索
- **二级管理面板**：系统总览 + 记忆浏览 + 导入导出

---

## 🚀 快速开始

### 安装

将插件目录放入 AstrBot 的 `data/plugins/`：

```bash
cd data/plugins
git clone <repository-url> astrbot_plugin_quillplus
```

### 激活

1. 启动 AstrBot
2. 进入 WebUI → 插件管理 → 找到"羽笔"
3. 点击"重载"启用插件

### 使用

发送激活词（如"继续"、"开写"、"进入剧情"等），Quill 自动检测并注入写作协议和素材。

---

## 🎮 指令

| 指令 | 说明 |
|------|------|
| `/wb` | 世界书列表 |
| `/wb <名字\|序号>` | 绑定世界书 |
| `/wb off` | 解绑全部 |
| `/wb info <名字>` | 查看世界书详情 |
| `/char` | 角色卡列表 |
| `/char <名字\|序号>` | 切换角色卡 |
| `/char unset` | 取消角色卡 |
| `/quill` | 查看 Quill 状态 |
| `/quill test <文字>` | 测试素材库匹配 |
| `/stream on\|off\|auto` | 控制流式输出 |
| `/reinject` | 重置注入状态 |

---

## 🖥️ 管理面板

进入 **插件 → Pages / 插件配置**，左侧导航栏包含：

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
- 拖拽上传文档
- 已上传文档管理
- 语义检索测试

### 动态记忆
- **系统总览**：记忆总数、活跃会话、向量索引状态
- **记忆浏览**：搜索过滤 + 数据表格 + 单条删除
- **导入导出**：JSON 备份与恢复

---

## ⚙️ 配置

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
├── main.py                  # 插件主类 + LLM hooks
├── web_routes.py            # Web API 路由注册
├── _route_core.py           # 业务 handler
├── config.py                # 配置解析层
├── worldbook.py             # 世界书 JSON 管理
├── kb.py                    # 写作素材库 SQLite
├── prompt_builder.py        # 四层 Prompt 装配
├── encryption.py            # Base64 [B:...] 编解码
├── state.py                 # 用户状态管理
├── activation.py            # 激活检测
├── commands.py              # 指令业务逻辑
├── quill_desktop.py         # 独立桌面启动器
├── quill_rag/               # RAG + 记忆共享模块
│   ├── embedding.py         # Embedding Provider 封装
│   ├── vector_store.py      # FAISS 向量存储 (Doc RAG)
│   ├── memory_store.py      # SQLite BLOB + NumPy (动态记忆)
│   ├── chunker.py           # 文档分块
│   ├── reranker.py          # Rerank Provider 封装
│   ├── llm_summarizer.py    # LLM 摘要生成
│   └── retrieval.py         # 统一检索入口
├── pages/panel/index.html   # 管理面板
├── knowledge/               # 数据目录（gitignore）
└── worldbooks/              # 世界书目录（gitignore）
```

---

## ❓ FAQ

**Q: 为什么不直接用市场上的世界书插件？**
A: 大多数世界书是关键词提示器，AI 不会主动调用。Quill 是世界书+知识库+角色卡三合一，AI 会主动模糊匹配并读取 AstrBot 人格列表。

**Q: 145 条素材库条目太多怎么办？**
A: 在管理面板按需禁用/删除，或通过 `knowledge_base.max_entries` 控制最大注入条数（默认 7 条）。

**Q: Embedding 和 Rerank 模型选什么？**
A: 推荐 SiliconFlow 的 `Qwen3-Embedding-8B` 和 `bge-reranker-v2-m3`。不配置时自动使用本地模型 `BAAI/bge-small-zh-v1.5`。

**Q: 动态记忆会串群吗？**
A: 不会。使用 `event.unified_msg_origin` 作为 session_id，SQL `WHERE session_id = ?` 天然隔离。

---

## 📄 License

本插件遵循 AstrBot 生态惯例，请遵守所使用 LLM 服务商的相关条款。
