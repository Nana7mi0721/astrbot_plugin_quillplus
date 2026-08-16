# QuillPlus (羽笔) - 多维沉浸式 RP 增强插件

> 世界书 + 写作素材库 + 角色卡 + 文档 RAG + 动态记忆，五合一沉浸式 RP 增强插件。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-indigo.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/version-5.2.0-green.svg)]()
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange.svg)](./LICENSE)
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
- **头像自定义裁剪**：Web 面板内置裁剪器，支持拖拽移动 + 滚轮缩放，Canvas 生成 300×300 方形 PNG

### 世界书系统 (Worldbook)

关键词触发的设定条目管理，prompt 按需注入。

- 常驻条目全局注入，构建稳定基调设定
- 关键词模糊匹配 + 灵敏度调节，命中时注入对应条目
- 支持注入到用户消息之前（前置模式）
- 每个角色可绑定专属世界书集合，角色切换时自动挂载/卸载

### 写作素材库 (Writing Resource)

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
- **核心记忆锚定**：可将关键记忆钉住（`is_core=1`），不参与 Top-K 竞争，无条件注入 `<core_memory>` XML 标签，类似人设基石
- **核心记忆自然语言注入**：在对话中通过 `@记住：...` 指令直接写入核心记忆，无需进入面板操作
- **全自动闲时反思**：系统空闲时自动分析未提纯的记忆，总结核心特质与关键事实，迭代角色深层设定
- **混合检索**：FTS5 (BM25) + Vector 混合检索，RRF 融合 + Ebbinghaus 时间衰减过滤
- **LRU 会话缓存**：加速同会话反复检索，减少磁盘与序列化开销
- 聊天指令管理：`/memory list/del/clear/learn/search/pin`

### 状态栏系统 (Status Bar)

追踪角色与用户的交互状态，将 LLM 输出结构化为可读面板。

- 5 级解析器（code block / LOVE_DATA / STATUS / raw / lenient）确保格式兼容
- 工具调用与响应两路钩子协作，避免重复注入
- 关闭状态栏后自动剥离残留格式
- 字段名可自定义，支持分支剧情选项生成
- **LLM 智能提取**：L1-L5 全失败时调用轻量 LLM 做结构化提取（3s 超时保护，默认关闭）
- **模型路由**：状态栏提取 LLM 可独立配置（`status_bar.llm_provider_id`），留空回退到 RAG 摘要 LLM，建议配置轻量模型降低成本

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
| `/quill reset` | 重置当前会话全部记忆与对话日志 |
| `/quill status` | 查看插件健康度（RAG 检索成功率、状态栏解析成功率等） |
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
| `/memory pin <序号>` | 钉住/取消钉住指定记忆为核心记忆（永不遗忘） |

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

进入 AstrBot WebUI → 插件 → Pages / 插件配置。**前端面板已按 Google Material Design 3 标准全面重构**，视觉效果与交互体验全面升级。

- **角色卡管理**：网格化卡片展示，新建/编辑/删除，V2 卡片导入，纯文本解析，头像上传与自定义裁剪
- **写作素材库**：全文搜索，分类筛选，条目编辑，匹配测试台
- **世界书**：多选配置，条目管理，ST 格式导入/导出，匹配测试
- **文档知识库**：拖拽上传，已上传文档管理，语义检索测试
- **动态记忆**：系统总览，搜索过滤，数据表格，JSON 备份与恢复，**对话日志查看器**（按会话浏览/导出 RP 对话记录）
- **配置页面**：一键备份下载（zip 打包素材库/世界书/角色卡/记忆/文档索引），系统健康度卡片，流式模式批量控制

---

## 快速开始

### 1. 安装依赖

```bash
pip install Pillow>=10.0.0
pip install faiss-cpu>=1.8.0 numpy>=1.24.0 aiosqlite>=0.19.0
```

### 2. 安装插件

将插件目录放入 AstrBot 的 `data/plugins/`，启动 AstrBot 后进入 WebUI → 插件管理 → 找到"羽笔"→ 点击"重载"启用。

### 3. 基础配置

1. 进入 WebUI → 插件配置 → 配置 LLM Provider（RAG 摘要 LLM 建议使用轻量模型）
2. 在"权限"分组中配置 `admin_users`（群聊写指令需要，留空时群聊写指令被拦截）
3. （可选）上传角色卡、世界书、写作素材，开始 RP 对话

### 4. 验证

发送 `/quill` 查看五大系统状态，或发送 `/char` 列出角色卡。所有功能均可在聊天窗口内通过指令操作，无需打开 Web 面板。

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
├── kb.py                    # 写作素材库 SQLite（文件名保留历史兼容；内部类名 WritingResourceManager）
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

## Roadmap

QuillPlus 遵循持续迭代的开发路线，当前（v5.2）已完成以下里程碑：

- ✅ **v5.0** — 重构首发版：平行宇宙双轴隔离、JSON 原子化状态机、全链路异步化、Character Card V2 全量支持
- ✅ **v5.1** — 全自动自迭代记忆：闲时反思守护进程、核心记忆更新、混合检索 (FTS5+Vector+RRF)、LRU 会话缓存
- ✅ **v5.2** — 面板功能补全：对话日志查看器、全量备份导出、操作忙碌态、MD3 全面重构、三轮代码审查修复

**下阶段规划：**

- 🔜 **世界书匹配测试台** — 可视化测试关键词命中与注入结果
- 🔜 **备份恢复** — 上传 zip 恢复到任意历史时间点
- 🔜 **WR 批量操作** — 多选删除/移动/分类
- 🔜 **移动端底部导航** — 触屏友好导航栏
- 🔜 **i18n** — 国际化支持（待社区需求驱动）

---

## FAQ
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

### 前端面板加载说明

由于 AstrBot 框架的 Plugin Pages 静态文件服务默认设置 `Cache-Control: no-store`（强制禁用缓存），前端面板每次刷新都会全量重新加载（约 200KB）。如果加载较慢，可以通过以下方式优化：

1. **反向代理缓存**：在 AstrBot 前方部署 Nginx/Caddy，对 `/plugins/quillplus/` 路径添加缓存头
2. **本地缓存**：浏览器开发者工具中禁用 "Disable cache" 选项（仅对非 DevTools 窗口生效）
3. **减少角色卡数量**：角色卡管理页会内联所有头像数据，减少角色卡数量可加快加载

> 该限制来自 AstrBot 框架层面，插件侧已通过内联 CSS/JS 和精简代码将请求数降至最低。

---

## Changelog

完整更新日志请见 [CHANGELOG.md](./CHANGELOG.md)。

---

## License

本项目基于 [GNU AGPL-3.0](./LICENSE) 开源。

---

## 鸣谢

感谢原作者 Quill 提供的底层框架。本项目在其基础上进行了深度重构与增强。

- [AstrBot](https://github.com/Soulter/AstrBot) — 提供可扩展的机器人插件框架
