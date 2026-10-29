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
7. **只能修改插件源码，禁止修改 AstrBot 实例的任何状态。** 包括但不限于：配置档
   （`data/config/abconf_*.json`、`cmd_config.json`）、路由表 / 共享首选项
   （`data_v4.db` 的 `preferences`）、平台与会话配置、插件开关、白名单。
   即使诊断出「问题出在 AstrBot 配置」也只**报告**，由用户自己改。
   原由：2026-10-26 排查 onebot 指令失效时直接改了用户的配置档 `wake_prefix`，
   属于越界改动用户环境。E2E 需要临时改动实例状态时，必须**先取得用户明确同意**，
   且备份 → `finally` 还原 → 回读校验（参考 `.build/harness/quilltest/gates.py` 的
   `RouteFlip`），还原后确认与备份逐字节一致。

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
- `QuillConfig` **新增字段必须有五个落点，缺一即失效**：`_conf_schema.json`（默认值与分组）→
  `config.py`（`QuillConfig` 读取，`_safe_int/float/bool` 兜底）→ `props.py`（访问器，宿主侧
  一律 `self.props.x`）→ `web_routes.py::_ALLOWED_CONFIG_KEYS`（面板读写白名单，漏登记则面板
  改不动也读不到）→ 面板（`index.html` 控件 + `config.js` 的 `CFG_FIELDS` / `CFG_DEFAULTS`）。
  两点易踩的坑：**「`_LIVE_READ_FIELDS` 豁免」与「改名别名表」在现行代码里都不存在**
  （旧守卫已随 `tests/` 归档，见 §〇-6），不要按旧写法去找它们；`props` 全部实时读
  `self.config`，`save_plugin_configs` 重建 config 引用后即全量生效。真正需要手工同步的是
  **构造期被快照成普通属性的值**（如 `rag_retriever.top_k` / `enable_memory`、
  `rag_kb_source.enabled` / `top_k`），必须加进 `main.py::save_plugin_configs` 的热更新分支，
  否则面板上改了要重载插件才生效。
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

### M2 用量与临时上下文约束（2026-10-28 报告复核后补充）

- 宿主版本与包元数据只作诊断；临时上下文必须核对真实 runner、主/回退 provider 与已审阅的装配及保存契约，未知路径保守关闭。不得靠缺失配置键的默认值放行。
- 用量的数值与报告证据分别保存。宿主默认零不等于供应商报告零，未暴露的缓存创建、重试或费用不得猜测；采集来源与计价来源是两个独立维度。
- 明细分页不得影响全量筛选汇总。缓存覆盖说明适用分母与样本数，供应商金额与价格推算分开；未知金额用调用数表示，不能填零元。
- 新面板沿用现有页头、卡片、表格和分页组件；破坏性动作使用危险确认，分页无下一页时禁用。真实浏览器检查不得仅依靠空数据。
- 审查报告是证据材料，先与源码及原始数据交叉核对；修复后保留原结论适用范围与未验边界，不把复刻探针、失败响应或字符前缀当作真实模型账单验收。

### M3M7 审查与请求复制约束（2026-10-29）

- 请求装配幂等记录属于事件生命周期，不在会被宿主复制的 ProviderRequest 上挂布尔完成标记。必须分别验同对象重复进入与真实 handler 连续 yield 复制请求，装配和日志/轮次副作用分别拒重。
- sqlite3 连接上下文管理器只结束事务，恢复副本清理前必须显式关闭句柄。迁移校验失败保留恢复线索，不自动丢弃备份。
- 过期日志清理必须尊重启用消费者的水位；禁用消费者不能永久阻塞保留期清理。初始化失败不伪装为零统计，各存储组件独立降级。
- 恢复失败保留原阶段和目标、明确阻止相关会话继续；不得把半完成清理静默放行或当作已完成。取消不能掩盖实际存储失败。
- 会话组数字续接读取共享实际菜单；消费视图排除旧修订及其成对回复，审计原文保留。后台共享删除必须核对确认时的修订号与影响范围。

### 历史索引与用量图形约束（2026-10-29）

- 迁移校验必须覆盖旧版本实际留下的孤儿 FTS 空壳。限定历史名称、FTS5 声明与内容表缺失，先保存原始一致性快照，在恢复副本执行同一修复再做完整检查；不能泛化吞掉 vtable 或 integrity 错误。受控 schema 修复必须关闭 writable_schema，未知故障继续拒绝。
- 用量图表使用全量聚合；input_total 已含缓存，不能重复堆叠。字段覆盖不是共同样本，命中率/覆盖率直接使用服务端结果，金额严格区分币种与来源，零与未知区别展示。
- 用量局部重绘前释放图表/选择器资源；桥接没有取消契约时使用单请求与最新待办，防过期响应覆盖。真实菜单测试须等待滚动结束，避免测试脚本自身触发菜单滚动收起。

### 历史压缩与换算边界（2026-10-29）

- 长期记忆摘要、提示词预算裁剪、宿主压缩和插件历史压缩必须分别说明。压缩先保存原文检查点、核对scope/persona/revision/cid与来源前缀，不压系统/本轮正文、不拆工具/媒体前缀，失败保留原文；共享摘要不删除原始流或回灌其他入口物理历史。
- 费用原始事实不被换算覆盖；汇率须明确来源，未设置和其他币种不能猜成已换算。逻辑scope和物理origin分别筛选，组变更不追溯重写旧账单。
- 调研源码先固定提交并追实际分支，类/README/旧后端声明不当作当前运行行为；引入学习/生命周期前先有来源、权限、候选审查和可恢复归档。
