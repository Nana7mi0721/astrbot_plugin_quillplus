# AGENT.md — AI 代理行为规范

本文件约束在本仓库工作的 AI 编码代理（ZCode / Codex / Claude 等全部代理）。
与系统提示词冲突时，以本文件为准；用户当次指令又高于本文件。

---

## 〇、硬性禁令（违反即返工）

1. **版本号未经用户明确说明，禁止改动。** 版本号有四个落点，任何一处都不许动：
   - `quill/__init__.py` 的 `__version__`（唯一真源）
   - `metadata.yaml` 的 `version:`
   - `README.md` 顶部的版本徽章（`version-X.Y.Z-green`）
   - `CHANGELOG.md` —— 不许**新建版本条目**（`## [X.Y.Z] - 日期`）
2. **Git 操作未经用户明确说明，禁止执行。** 不 commit、不 push、不打 tag、不建分支。
   完成的改动留在工作区，由用户检查后自行决定是否提交。
3. **提交信息禁止 AI 署名尾注。** 即使提交经用户授权，信息中也禁止出现 `Co-Authored-By:`
   / `Generated with …` / 🤖 署名行等任何 AI 署名尾注，PR 标题与描述同样禁止。
   原由：GitHub 把 `noreply@anthropic.com` 识别为 claude 账号计入贡献者列表，清理必须
   改写历史并强推 main（2026-10 实际发生过一次）。
4. 新功能 / 修复的用户可见变化，默认记入 `CHANGELOG.md` 顶部 **`## [未发布]`** 小节
   （没有就新建），并在 README 对应小节同步。版本号、发布条目留给用户在发版时处理。
   **条目粒度以用户为读者**（写法对齐 v5.3.0 / v5.3.1）：一条 bullet 讲清「用户能感知到
   什么变化」，1–3 行为宜；小节用 `### 新增` / `### 修复` / `### 安全` / `### 性能` /
   `### 面板细节`。**禁止**写实现细节——文件路径与行号、类名 / 函数名 / 常量名、测试数量与
   测试名、内部字段与协议、调试过程与根因分析。这类**开发者资料**写进 `docs/`（本地目录，
   不入版本库，如 `docs/CHANGELOG_DETAIL.md`）或提交信息，不进 CHANGELOG。
   写完自查一遍：这一条普通用户看得懂吗？看不懂就删掉或改写。
5. **不许"顺手"做方案之外的改动**：版本号、代码格式化、导入重排、重命名、依赖变更等，
   即使看起来是改进，也一律先向用户提出，得到确认再动。
6. **测试只允许端到端（E2E）**。本项目**不写、不恢复单元测试 / 回归测试**：stub 驱动的
   假件测试（含行为快照、架构守卫、legacy 兼容层）已全部归档到
   `.backup/tests_v5.3.2_unit_regression/`，不许搬回工作区，也不许新写同类测试。
   任何新增测试必须是端到端测试——定义、落点与验收纪律见 §五。
   **只改代码不跑 E2E 等于没做**。

---

## 一、项目速览

AstrBot 角色扮演插件（六合一：世界书 + 写作素材库 + 角色卡 + Doc RAG + 动态记忆 + 状态栏）。
分层：`main.py`（注册桩与钩子）→ `interfaces/`（AstrBot 适配）→ `quill/`（服务层）、
`quill_rag/`（RAG 组件：embedding / vector_store / memory_store / reranker / retrieval /
astrbot_kb）→ `pages/panel/`（Web 面板前端）。

## 二、运行与测试环境

- 插件由 AstrBot 加载时 `astrbot` 必然可导入；**单文件直接自测**（`python xxx.py`）时用
  `_astrbot_bootstrap.py` 引导（写法参考 `kb.py` / `quill_rag/astrbot_kb.py` 头部）。
- 系统 Python **没有** astrbot 依赖；带完整依赖的解释器是
  `D:\Program\AstrBot\backend\python\python.exe`（真机自测用它）。
- 验收靠**真实实例上的端到端测试**（见 §五）：桌面实例默认
  `http://127.0.0.1:6185`（数据根 `D:\Program\AstrBot\AstrBotData`），需要干净环境时另起
  隔离实例（6186 / `C:\Users\REISEN\.astrbot-test`）。**绝不杀 AstrBot 进程**。
- 改 `pages/panel/` 下 JS 后校验语法：面板脚本是 ES Module，`node --check` 不认，用
  `node --no-warnings --experimental-vm-modules -e "new vm.SourceTextModule(require('fs').readFileSync('<file>','utf8'))"`。

## 三、代码规范

- **日志**：一律 `from astrbot.api import logger`，**严禁** Python 内置 logging（上架规范）。
- **注释**：中文、密度高，写「为什么 / 约束 / 修复编号（F4、M3.2 D4、P1-4 这类）」，
  不写「这行在做什么」；与所在文件现有密度对齐。
- 新文件的头部（编码声明 / Copyright / SPDX / docstring）参考同目录现有文件。
- 组件初始化失败**不许抛出**阻断插件加载：参考 `_init_rag` 的 try/except + 降级日志模式。
- **分层禁令**（原由已归档的 `tests/arch/test_layering.py` 守护，现为人工约定）：
  `quill/` 包内**禁止** import astrbot（含函数内延迟 import）；只有 `quill_rag/*`、
  `interfaces/*` 与插件根模块可以碰 `astrbot.api`。

## 四、RAG 与注入链路契约

- **失败协议**：检索结果统一用 `quill_rag/retrieval.py` 的 `RagResult`（list 子类，
  带 `_rag_ok` / `_rag_error`）。空结果 =「确实没找到」（算成功）；异常 = 失败。
  健康度经 `rag_ok()` 记入 `health_tracker.record_rag`。
- **合并多路结果前必须先取各路的失败标记**——`list(x)` / `extend` 会丢掉 RagResult 子类标记
  （见 `main.py::_run_rag_retrieval` 的 local_ok / kb_ok 写法）。
- 文档检索结果形状：`{content, source, chunk_index, score}`；`source` 是绑定标识
  （本地 = 上传文件名，AstrBot 内置库 = `kb:库名`，前缀常量 `KB_SOURCE_PREFIX`）；
  `allowed_sources` 为精确字符串匹配。
- **改注入链路前先读 `interfaces/astrbot_hooks.py::handle_llm_request` 的编排注释**：
  22 步的顺序即行为契约（例如 `format_for_prompt` 必须在 retriever 实例上调用，第 17 步），
  改动调用位置 / 方式会改变注入段序；改完必须用端到端测试验证（真面板 + `/quill debug`
  看注入报告与降级链分布），不能只看代码。

## 五、测试：只允许端到端（E2E）

- 单元测试 / 回归测试在本项目**已废弃**：stub 驱动的假件测试（含行为快照、架构守卫
  `tests/arch/test_layering.py`、legacy 兼容层）已全部归档到
  `.backup/tests_v5.3.2_unit_regression/`（目录内有 README 与恢复命令）。
  **不许恢复、不许搬回工作区、不许新写同类测试**；`pytest.ini`、`requirements-ci.txt`
  与跑 pytest 的 CI workflow 一并归档，工作区已没有 `tests/`。
- 端到端测试的定义（三条全中才算）：
  1. 驱动**真实 AstrBot 实例**（桌面 6185 或隔离 6186），不 mock 框架、不塞假件；
  2. 走**真实入口**：dashboard / 插件 HTTP 路由、真实事件流水线（`/quill`、`/doc` 等指令
     经 WebChat 注入后看真实回复）、真面板（CDP 真 Chrome 打开插件 Pages，真点击 / 真输入）；
  3. **自带断言与收尾**：脚本自己核对服务端状态（接口回读 / 面板 DOM / 日志），结束时把
     临时数据清干净（临时角色卡、人格、分组、文档），跑完不留残留。
- 落点 `.build/harness/`（`.gitignore` 内，不部署、不入库）：脚本命名 `xxx.py`，可直接
  `python xxx.py` 跑；复用既有基建 `quilltest/{auth,chat,paths,logs}.py`、`cdp.py`、
  `panel.py`、`liveprobe.py`（面板入口 = `GET /api/plugin/page/entry?name=…&page=panel`
  取 `data.content_path`）。可参考的现成实现：`ab1.py`（人格导入全流程）、`sel1.py`
  （自绘下拉）、`scrim1.py`（遮罩守卫）。
- 用 `QUILL_BASE` / `QUILL_ROOT` 切换实例（默认桌面 6185 / `D:\Program\AstrBot\AstrBotData`）；
  `quilltest` 与各 `xxx.py` 脚本都认这两个变量，需要干净环境时先起隔离实例，再
  `QUILL_BASE=http://localhost:6186 QUILL_ROOT=C:\Users\REISEN\.astrbot-test`。
  后端日志路径自动探测（桌面 `<root>/logs/backend.log`、隔离 `<root>/data/logs/astrbot.log`）；
  桌面实例掉线时不要碰它的进程，直接在隔离实例上部署 + 验证（`astrbot_start` → `astrbot_deploy`
  → `QUILL_BASE/QUILL_ROOT` 跑用例），并在汇报里说明「本轮验的是隔离实例」。
- **只改代码不跑 E2E 等于没做**：宣布完成前必须有一次真实运行通过（附回复文本 / 面板截图 /
  日志证据），并说明哪些行为验不到（真实平台专属字段只能靠隔离实例 + 日志推断）。
- **已有成套 E2E 套件，优先复用它**：`.build/harness/quilltest/`（`run.py` +
  `auth/chat/paths/logs/gates/snap/onebot/cases.py/cases_toggle.py`，目录内 README 是完整说明）。
  `python run.py` 默认跑 Tier 1（24 条 WebChat 指令用例，零 token、几十秒）；`--api` 加面板 REST
  用例、`--toggle` 加配置开关矩阵、`--tier2` 加 LLM 钩子用例（耗 token）、`--tier3` 加假 OneBot
  群聊/权限、`--full` 全量（约 13 分钟）。翻转危险开关有白名单 + 备份 + finally 还原 + md5 校验。
  **新增回归用例加进 `cases.py`（数据驱动）**，别再写一个孤立脚本。
- 静态防线（不依赖测试）：改版本落点后跑 `tools/check_version.py`；改面板 JS 后跑 §二 的
  ES Module 语法校验。

### 运行期硬约束（与测试无关，改代码前必读）

- 插件 `__getattr__` 对未知属性抛 `AttributeError`：运行期读「可能未初始化的实例属性」
  必须 `getattr(self, "x", None)` 防御，不能直接 `self.x`。
- `QuillConfig` **新增字段必须有归宿**：props.py 访问器 / 改名别名表 / `_LIVE_READ_FIELDS`
  豁免（须写明理由）三选一；需要面板热生效的值还要在 `save_plugin_configs` 的热更新分支
  同步到持有组件。
- `self.context` 上的**可选属性**（如 `kb_manager`、`persona_manager`）一律 getattr 防御，
  兼容旧版 AstrBot。
- `docs/`、`tools/`、CI 配置与已归档的 `tests/` 都**不在版本库**（本地保留，见 commit
  7d0d6ee）：不要把它们加回 git。

## 六、Web 面板

- 路由分两层：`web_routes.py`（HTTP / 参数解析）+ `_route_core.py`（纯逻辑处理器，
  不碰框架）；组件引用经 `routes.rag` dict 传递，`_refresh_routes_refs` 负责组件重建后同步。
- 前端在 `pages/panel/js/pages/*.js`，复用 `api / apiPost / esc / showToast` 等公共件，
  不要重复造轮子；后端响应遵循"只增字段"的向后兼容惯例。

## 七、执行纪律

- 动手前先弄清现有实现的**行为契约**（`interfaces/` 与 `quill/` 的编排注释、失败协议、
  注入段序），再改代码。
- 每完成一个逻辑单元跑一次相关 E2E，收尾跑一遍覆盖本次改动的完整 E2E 流程；E2E 红了要查
  根因，不许绕过、不许把断言放宽来"变绿"。
- 汇报用中文、结论先行；被用户指出的问题要落回本文件补规范，防止复发。
