# 羽笔（quillplus）v5.3.0 渐进式重构规划

> 版本：v5.3.0-draft（规划基线：原版 v5.2.5 @ `astrbot_plugin_quillplus`，参考实现：`quill refactor test`）
> 日期：2026-09-25
> 决策记录：改造基座 = **原版渐进原位重构**；数据布局 = **保持三库不动**；前端 = **原版面板渐进维护**；测试 = **移植 pytest 体系 + CI**；版本号 = **5.3.0（次版本号升级，重构作为新版本主题发布）**

---

## 0. 背景与总原则

### 0.1 两版现状诊断（结论来自 2026-09 的全量代码审计）

**原版（v5.2.5，约 14,300 行 Python + 8,000 行前端）**——实战打磨出来的准生产系统：

- 持久化正确性罕见地高（原子写、WAL、在线备份、世代号根治竞态、zip-slip 三重防护）；
- 与真实 AstrBot 的兼容性经过市场审查与线上运行验证（`await request.json()` 等异步 web 约定、GreedyStr 默认值坑的 workaround）；
- FTS5 中文检索是实测驱动的正解（trigram + 短词 LIKE 双路径）。

但结构性债务集中：

| # | 债务 | 证据 |
|---|---|---|
| D1 | `main.py` 巨石（2,978 行，6 钩子 + 7 指令 + 700 行状态栏解析器） | `main.py` 全文；`on_llm_request` 单函数 265 行 |
| D2 | 配置投影反模式（17 个实例属性手工同步/回滚，需 AST 测试看守） | `main.py:1073-1194`、`tests/test_config_projection.py` |
| D3 | 四组 base64 双份 handler + 三份原子写 + 两份 `_spawn` | `web_routes.py:411-481`、`state.py:150` 等 |
| D4 | 数据层吞错误（20+ 处失败一律 warning + 返回空） | `memory_store.py:221,540,558,581` |
| D5 | 锁不可重入只靠注释维系 | `memory_store.py:135`、`vector_store.py:91` |
| D6 | 测试硬编码 `D:\Program\AstrBot\backend\app`，换机即失效 | `tests/test_memory_fts.py:30` |
| D7 | 文档漂移（面板"单文件内联"失实、PDF 支持失实、版本号三处不一） | README:388-394、README:55 vs `web_routes.py:425`、`main.py:372-378` |
| D8 | 热路径 O(历史长度) 正则清洗 | `main.py:2380-2412` |

**重构版（约 20,400 行，四层架构）**——工程上限更高，但有一个致命伤 + 若干高风险：

- 优点：分层是真的（domain 零框架 import、11 个 Protocol、190 用例脱离 AstrBot 可跑）；迁移器数据安全到位（`mode=ro` 只读源库 + SHA256 字节级守卫 + 未合并 WAL 回归测试）；修掉原版十个真实缺陷（FAISS 维度不再硬编码 512、`vector_state` 一致性状态机、路径安全 `resolve_safe()`、LIKE `ESCAPE`、可重入锁 + 死锁回归测试）。
- 致命：`routes.py:926-988` 把 `request.json/form/files` 当同步属性读，真实 AstrBot（v4.27.4+）中它们是 async 方法 → 真实面板上所有写操作恒拿空数据，三道测试防线全部漏过。
- 高危：迁移 INSERT 无 `ON CONFLICT`，中途崩溃重跑会重复插数据，违背自家幂等承诺；激活判定 fail-open（`plugin.py:397-405`），检测器坏掉时退化为每轮全量注入。

### 0.2 总原则

1. **线上不停机原则**：每一步重构后插件必须可加载、可聊天、可回滚。绝不做"一次性大爆炸替换"（重构版的教训：脱离真实框架验证太久，积累了系统性兼容缺陷）。
2. **行为保持原则**：重构期间用户可感知行为（指令、面板、注入逻辑、状态栏输出）保持等价。行为变更需单独开小节说明并进 CHANGELOG。
3. **摘果实原则**：从重构版摘取经过验证的具体实现（如错误体系、可重入锁、FAISS 维度自适应、路径安全），不搬它的 Web 层（有致命 bug）和迁移器（与"三库不动"决策冲突）。
4. **小步提交原则**：原仓库已有 git，每个里程碑独立分支 + 可验证的提交粒度，便于二分定位回归。
5. **测试先行于搬移**：任何模块在被搬动/拆分前，先为其在 pytest 体系下建立行为快照测试，搬移后跑同一组测试证明等价（"先立保护网再走钢丝"）。

---

## 1. 目标与非目标

### 1.1 目标

- **G1 结构**：消灭 main.py 巨石与四组重复 handler，形成"接口层 → 服务层 → 存储层"三级内部结构（不上全六边形，见 2.1）。
- **G2 可测试**：pytest 用例覆盖核心纯逻辑（状态栏解析、检索打分、配置投影），CI 每次提交自动跑，摆脱本机路径依赖。
- **G3 健壮**：修掉 D4（吞错误）、D5（锁纪律）、激活判定 fail-open、FAISS 维度硬编码、路径安全五项实打实的正确性问题。
- **G4 一致**：文档/版本号/配置描述与实现全量对齐；建立防再漂移机制（版本号单源化、CI 文档一致性检查）。
- **G5 性能**：消灭热路径 O(历史长度) 正则清洗。
- **G6 体验**：沉淀错误体系（用户可见 message 与日志 detail 分离），为后续可观测性打基础。

### 1.2 非目标（明确不做）

- 不合并三库为单一 quill.db（用户决策：保持三库；迁移器风险 > 收益，留待 v6.0 评估）。
- 不更换前端面板（原版面板实战验证过，studio 只作为未来视觉参考，不在本期范围）。
- 重构版的 Web 层（`interfaces/web/routes.py`）不采用——其请求体读取与真实框架不兼容，且原版 Web 层久经考验，只做内部去重。
- 不引入 pydantic 等新重依赖（沿用 dataclass + 显式校验，重构版 ARCHITECTURE.md:15-22 的否决理由成立）。
- 不做数据布局迁移（三库不动 + 存储路径不变，天然无需迁移）。

### 1.5 各债务 → 目标映射（决策索引）

| 债务/问题 | 处置 | 落点 |
|---|---|---|
| D1 main.py 巨石 | 解决 | M2 |
| D2 配置投影 | 解决（属性访问器化） | M2.3 |
| D3 重复代码 | 解决（统一上传通道 + 公共设施） | M2.4/M3.1 |
| D4 吞错误 | 解决（结果对象 + 可观测机制） | M3.2 |
| D4b 激活 fail-open | 解决（改 fail-close） | M3.2 |
| D5 锁纪律 | 解决（可重入锁，摘自重构版） | M3.3 |
| D6 测试路径 | 解决（pytest 化 + stub + CI） | M1 |
| D7 文档漂移 | 解决（版本单源 + CI 检查） | M4 |
| D8 热路径 O(n) | 解决（时间戳游标） | M3.4 |
| F1 FAISS 维度 512 硬编码 | 解决（首批真实向量决定维度，摘自重构版） | M3.3 |
| F2 路径安全 | 解决（`resolve_safe` 移植） | M3.1 |
| kb 全表扫描上限 2000 条含 content | 解决（FTS 命中后按 id 回表） | M3.3.5 |
| 面板 studio 化 / 单库统一 + 迁移器 / TG Markdown 剥离 | 不做 | — |

---

## 2. 目标架构

### 2.1 包结构（目标态）

```
astrbot_plugin_quillplus/
├── main.py                    # 只剩插件注册 + 钩子接线，目标 <150 行
├── interfaces/
│   ├── astrbot_hooks.py       # 6 个事件钩子（薄适配，只做取参/调服务/还参/顶层降级）
│   ├── astrbot_commands.py    # 7 个指令入口（薄适配）
│   └── web/
│       ├── routes.py          # 路由注册 + base64 消歧（见 M3.1）
│       └── upload.py          # 统一上传通道（见 M3.1）
└── quill/                     # 与 AstrBot 解耦的核心包（可独立测试）
    ├── core/                  # 错误体系、路径安全、原子写、FTS 工具、锁
    │   ├── errors.py          # QuillError 体系（摘自重构版 core/errors.py）
    │   ├── paths.py           # 现有 _paths.py + resolve_safe()/sanitize_name()
    │   ├── atomic_io.py       # 三份原子写收敛
    │   ├── locks.py           # 可重入异步锁（摘自重构版）
    │   └── fts.py             # 现有 _fts_util.py
    ├── services/              # 服务层（原版业务逻辑按域拆分）
    │   ├── activation.py
    │   ├── character.py       # persona_manager 演进
    │   ├── worldbook.py
    │   ├── writing.py         # kb.py 演进（模块名与内容对齐）
    │   ├── memory.py          # quill_rag/memory_store 演进
    │   ├── rag.py             # vector_store/retrieval/embedding/reranker 归拢
    │   ├── prompt.py          # prompt_builder 演进
    │   ├── state.py
    │   └── statusbar/         # 状态栏子系统（原 main.py 700 行拆出）
    │       ├── parsers.py     # L1-L6 解析器注册表（含现有 fixture 回归）
    │       ├── render.py      # 平台模板渲染
    │       └── tokens.py      # [LOVE_DATA]/[STATUS] 等魔法字符串常量
    └── config.py              # 配置投影 → 属性访问器（见 M2.3）
```

> 注：`quill/` 包内不 import astrbot（与重构版 domain 零依赖同理由），但其内部仍持有 SQLite/FAISS 等实现——是"逻辑与框架解耦"，不是六边形"端口/适配器"。这是刻意的折中：在插件这个体量（1.4 万行、单作者维护）上，全六边形的人工成本（组合根、11 个 Protocol、接口隔离）会超过收益。重构版的实践也印证：它花钱维护了 11 个 Protocol，却在 Web 薄适配层栽了跟头——**接口数量不是正确性的来源，与真实框架的握手验证才是**。

### 2.2 依赖规则（可机械校验）

```
interfaces/ ──import──> quill/          （单向）
quill/core/ ←──import── quill/services/  （core 不依赖 services）
quill/ 内部禁止 import astrbot.*        （分层测试覆盖此断言）
```

校验手段：阶段 M1 的架构守护测试（`tests/arch/test_layering.py`）用 AST 扫描 import（含函数内延迟 import，扫描全函数体），违规则 CI 失败。这直接回应重构版的教训——它有 11 个 Protocol 但没有"core 不 import astrbot"的机械断言，导致 `core/paths.py:183` 延迟 import astrbot 违反自家规则而无人察觉。

### 2.25 重构版摘果实清单（已验证可移植项）

| 摘取物 | 来源 | 去处 | 验证状态 |
|---|---|---|---|
| `QuillError` 体系（9 子类 + message/detail 分离） | `core/errors.py` | `quill/core/errors.py` | 已复核，直接可用 |
| 可重入异步锁（任务级持有者/深度跟踪 + 死锁回归测试） | `infrastructure/db/database.py:51-99` | `quill/core/locks.py` | 已复核，直接可用 |
| FAISS 维度自适应（首批真实向量决定维度） | `vector/faiss_index.py:206-232` | `quill/services/rag/` | 已复核，需适配三库布局 |
| 路径安全 `resolve_safe`/`sanitize_name`（Windows 保留名、控制字符、`..`） | `core/paths.py:119-140` | `quill/core/paths.py` | 已复核，需适配原版路径层 |
| 状态栏测试用例设计（反误判样本、六级链可达性断言） | `domain/status.py` + `tests/test_status.py` | 状态栏 fixture 快照 | 只摘用例设计，实现以原版为准（重构版解析行为与原版逐 case 等价性未验证，搬实现风险高于重写） |
| `_conf_schema.json` 生成工具脚本 | `tools/gen_conf_schema.py` | 原仓库 `tools/` | 已复核，直接可用 |
| pytest 基建（framework_stubs + pytest.ini + asyncio_mode=auto） | `tests/framework_stubs.py` 等 | 原仓库 `tests/infra/` | 直接采用，**需关键修订：stub 的 request.json/form/files 必须建模为 async 方法，与真实 AstrBot v4.27.4+ 一致，堵住重构版翻车的那个坑** |

> 摘取边界（为何不全搬）：
> - Web 层不搬：致命 bug（同步读 async 属性）+ 面板决策（原版面板保留）；
> - 迁移器不搬：三库不动，无迁移需求；
> - AppContext/Protocol 体系不搬：与"不上六边形"决策一致；
> - studio 前端不搬：用户决策不采用。

### 2.3 风险预警表

| 风险 | 概率 | 影响 | 缓解 | 里程碑 |
|---|---|---|---|---|
| 原版测试缺 fixture 基线 → 搬移后无回归可证 | 高 | 高 | M1 先用原版 3 个手写脚本 + 状态栏样本建立 pytest fixture 快照 | M1 |
| 钩子重排引入行为漂移（尤其注入顺序/激活时序） | 高 | 高 | 每钩子重排单独 PR + 现有 1461 行状态栏 fixture 全量回归 + `docs/probe_*.py` 真机探针 | M2 |
| 钩子薄化后需穿透主类 state | 中 | 中 | 新增 `QuillContext` 数据类（session 键、激活状态等）作为唯一传参载体 | M2 |
| 吞错误改结果对象后日志噪音增大 | 中 | 低 | 分级：用户可见 message vs debug detail；面板 RAG 页已有 `/info` 可观测端点 | M3 |
| FAISS 维度自适应后旧索引不兼容 | 高 | 高 | 保留旧维度读取兼容分支 + 索引文件头版本号；带上线下旧索引重建测试 | M3 |
| 误动本地工具目录（.zcode/.backup/.build） | 低 | 低 | M0 清点列保护清单，M6 复查 | M0/M6 |

### 2.4 防再漂移机制（G4 制度化）

| 漂移类型 | 预防机制 | CI 检查 |
|---|---|---|
| 版本号三处不一（`@register` / metadata.yaml / README badge） | 版本号单源化：唯一真源 `quill/__init__.py` 的 `__version__`，其余处由脚本对拍校验 | `tools/check_version.py`（CI 步骤） |
| `@register` 的 desc 与 metadata.yaml desc 不一致 | 同上单源化：desc 也从同文件常量派生 | 同上 |
| 面板 JS 内 API 路径与后端路由漂移 | `api.js` 的端点表生成校验脚本 | `tools/check_routes.py`（对拍后端 `register_web_api` 调用面） |
| README 声称与实现不符（如"支持 PDF"） | README 功能声明段落定点检查（版本/路由数）+ 人工季度审 | 折中：README 全量生成成本高且失真（自然语言无法从代码生成），用"单源化 + 定点校验" |

> 说明：docs/ 下探针脚本（probe_*.py）保留，作为真机验证手段。

---

## 3. 实施路线图

### 里程碑总览

| 里程碑 | 主题 | 预估规模 | 前置 |
|---|---|---|---|
| M0 | 验证基线 & 基础设施锁定 | 0.5 天 | — |
| M1 | 测试基建（pytest + stub + CI） | 2-3 天 | M0 |
| M2 | 结构拆分（main.py 拆解、handler 去重、配置投影消解） | 2-3 周 | M1 |
| M3 | 正确性修复（吞错误、锁、FAISS、路径、热路径） | 1-2 周 | M2 |
| M4 | 文档对齐 & 发布 v5.3.0 | 2-3 天 | M2、M3 均验收 |
| M5 | 观察期 & 回归窗口 | 2 周 | M4 发布 |
| M6 | 收尾归档 | 0.5 天 | M5 |

**总计：约 6-9 周**（不含观察期约 4-6 周；观察期与后续开发可并行）。

### M0 验证基线 & 基础设施锁定（0.5 天）

目的：在动第一行代码前，锁定"什么是正确行为"和"在哪里验证"。

- [ ] M0.1 记录当前真机环境快照：AstrBot 版本（README 声称 `>=4.26.0`，实际真机待确认）、插件版本、面板是否可写。
- [ ] M0.2 建立"手术灯清单"（手术式重构的保护清单）：列出所有不得被重构改动的行为面——6 钩子签名与调用顺序、`_route_core.py` 的 handler 签名、注入顺序（世界书→记忆→WR→RAG）、状态栏六级链行为、面板 60+ 路由的请求/响应形状、指令别名。**这些是后续所有"行为等价"验收的对照面。** 放入 `docs/v5.3/BASELINE.md`。
- [ ] M0.3 清点非源码资产（.backup/、.build/、.zcode/、.uploads/、tools/、docs/probe_*.py、knowledge/ 残留旧库），列入保护/搬移清单，防止重构中误动。
- M0 验收：BASELINE.md 存在且包含以上清单；真机快照已记录。

### M1 测试基建（2-3 天）

目的：建立"先立保护网再走钢丝"的保护网。**这是整个计划的安全带，M2/M3 的一切搬移都依赖它。**

- [ ] M1.1 搬入 pytest 基建：`pytest.ini`（asyncio_mode=auto）、`tests/infra/framework_stubs.py`（从重构版摘取，注入假 `astrbot.api.web` + `starlette.responses` 到 sys.modules；**关键修订：stub 的 request.json/form/files 必须建模为 async 方法，与真实 AstrBot v4.27.4+ 一致，堵住重构版翻车的那个坑**）、conftest.py（检测 ASTRBOT_APP 环境变量并 bootstrap，替代硬编码 `D:\Program\...`）。
- [ ] M1.2 现有 3 个手写脚本转 pytest 用例：`test_status_bar_parsers.py`（1461 行）的主体逻辑转为参数化 fixture；`test_memory_fts.py` 保留 async 测试，路径改环境变量；`test_config_projection.py` 的 AST 断言保留（它在 D2 消解前继续看守配置投影完整性）。
- [ ] M1.3 新增架构守护测试（防腐层）：`tests/arch/test_layering.py`，AST 扫描 import：
  - `quill/` 包内禁止 `import astrbot`（含延迟 import，扫描全函数体）；
  - `quill/core/` 禁止 import `quill/services/`；
  - 违规即失败——M2 结束时此测试必须通过（M2 前先以 skip 标记挂起，此时 quill/ 包尚不存在）。
- [ ] M1.4 GitHub Actions workflow：`python -m pytest tests/ -x -q`（Python 3.12，windows-latest 与 ubuntu-latest 双矩阵；Windows 矩阵可捕获路径分隔符类 bug，原版在 Windows 上有第一手教训）。
- [ ] M1.5 CI 绿灯后打 tag `v5.2.5+testbase`（在主线外留一个测试基线锚点）。

M1 验收：CI 双平台全绿；手写脚本原有断言 100% 保留在 pytest 用例中。

### M2 结构拆分（2-3 周，最高风险里程碑）

目的：解决 D1/D2/D3。**每一步都是"建测试 → 搬移 → 等价验收 → 提交"的循环，严禁跳过验收。**

#### M2.0 拆分策略：按服务域垂直切分

main.py 里的内容按"域"归组（角色卡域、世界书域、记忆域、RAG 域、WR 域、状态栏域、配置域、备份域），每个域独立完成"搬移→验收"闭环，一次只搬一个域。顺序按依赖深度自底向上：先无依赖的（state/config/statusbar 纯函数），后有依赖的（memory/rag 有 LLM 调用），最后是钩子接线。搬移时旧位置保留 re-export（如 `from quill.services.statusbar import *`）以兼容 `probe_*.py` 探针脚本与用户自写脚本的 import 路径，全部搬完后统一摘除。

#### M2.1 状态栏域搬移（main.py 700 行 → `quill/services/statusbar/`）

1. 先把 main.py 中 L1-L6 解析器、`_StatusLevelResult/_StatusLevelContext`、HealthTracker 等纯函数部分识别出来（部分已在 `_route_core.py` 存在，需合并归拢）。
2. 搬移到 `parsers.py` + `render.py`，行为完全照搬，**不改任何一行解析逻辑**。
3. 魔法字符串（`[LOVE_DATA]`、`[STATUS]` 等）收敛为 `tokens.py` 常量，收敛时用 M1 的 fixture 全量回归证明无字符串遗漏。
4. 验收：1461 行状态栏 fixture 全量通过 + 真机 probe（`docs/probe_no_leak.py`）确认无裸 `[LOVE_DATA]` 泄漏。

#### M2.2 钩子薄化（main.py 钩子层 → interfaces/astrbot_hooks.py）

> **架构修订（2026-09-25，BASELINE §1.2）**：真机 4.28.1 源码实证，钩子/指令函数的 `__module__`
> 必须与插件注册路径 `data.plugins.<目录>.main` **精确相等**才能被绑定与分发（否则静默跳过）。
> 因此"钩子定义搬进 interfaces/"不可行。修订后形态：**注册桩留在 main.py 类体**（装饰器+签名+
> priority 不变，桩体一行委托），**实现函数住 interfaces/astrbot_hooks.py**，业务逻辑下沉 quill/services/。
> main.py 行数目标由 <150 行相应放宽（注册桩 + init + 接线约 200-300 行，M2 验收时按实际复核）。

1. 6 个钩子逐个搬移，每个钩子独立 PR：先在 pytest 中为该钩子建行为快照（输入：构造的 event/req 对象 + 桩 service；断言：对 req 的修改结果与旧实现一致），再搬移，再跑快照证明等价。
2. 钩子只做四件事：取参、调用服务、还参、顶层降级 try/except。业务逻辑全部下沉到服务层。
3. 关键等价面（M0 手术灯清单项）必须逐项对照：注入顺序（世界书→记忆→WR→RAG）、`_quill_activated` 闸门时序、断点续传垫回、SMT 工具改写与请求级还原。
4. 验收：6 钩子快照测试全绿；真机连续聊天 20 轮无异常、注入报告正常、状态栏正常渲染。

#### M2.3 配置投影消解（D2）

目标形态：从"17 个投影属性手工同步"改为**"属性访问器 + 唯一真源"**。

1. 在 `quill/config.py` 中定义 `QuillConfigProperties` 类：直接持有 `config` 对象引用，17 个投影项改为 `@property` 实时读取（默认全部 property 实时读——读 dict 属性成本低；实测发现瓶颈再加缓存 + 显式 `refresh()`）。
2. main.py 的 17 个实例属性（`_PROJECTED_ATTRS`）删除，全部改为通过 `self.props.xxx` 访问。
3. `save_plugin_configs` 的 174 行手工同步/回滚逻辑删除——投影消失后无需同步，这 174 行是因投影存在才存在的代码。
4. `tests/test_config_projection.py` 的 AST 断言改造：从"看守投影登记完整性"改为"看守访问器覆盖完整性"（每个 QuillConfig 字段都有对应 property 或被运行期消费）——看守仍在，但看守的东西从反模式变成正模式。
5. 验收：面板改配置不重启生效（真机验证）；改造后 AST 测试通过。

#### M2.4 Web 层去重（D3 上半）

详见 M3.1 统一上传通道（M2/M3 交界处，随 M3.1 一并落地，避免同一文件动两次）。

M2 整体验收：架构守护测试转正通过（quill/ 零 astrbot import、core 不依赖 services）；main.py <150 行；真机全功能回归清单（M0 手术灯）逐项通过；6 钩子快照测试全绿。

> **验收口径补记（2026-09-26，M0-M4 验收后回写）**：main.py 行数口径经历两次修订——
> 本文多处 <150 行 → M2.2 修订为 200-300 行（框架契约实证，见 M2.2 引文）→
> BASELINE §9 最终按定性口径验收：注册桩（装饰器+签名+docstring，约 286 行）是
> 框架契约硬开销必须保留，逐字等价优先于行数目标。实测 5.3.0 main.py 1,829 行，
> 其中约 660 行（36%）是无框架耦合、可下沉而未下沉的业务逻辑（反思守护进程 121、
> `_llm_extract_status` 67、`_run_rag_retrieval` 69、`_check_activation` 58、
> 注入报告簇 78、`save_plugin_configs` 142、HealthTracker 58）——留在原处是
> 当时"等价优先"的取舍，其归位/下沉列入 M6 可选清理。本文保留 <150 行原文不改，
> 以本补记为准。

### M3 正确性修复（1-2 周）

目的：修实打实的正确性/安全/性能问题。顺序刻意安排在 M2 之后：先拆干净再修，避免在大文件上修 bug 引入二次回归。每项修复独立 PR + 回归测试。

#### M3.0 真机实测修复（2026-09-25 首测发现，BASELINE §8.2，用户决策排入 M3）

- **F1 SMT 回声重复回复**：H4（on_llm_response）对照框架 `_send_message_to_user_current_session_plain_texts` 已发记录，识别 completion 为工具消息回声（状态栏渲染/剥离变体归一后比对）时置空 result，让 respond.stage 去重/空链跳过。带真机复测。
- **F2 换卡不重置 quill_rounds**：`_ensure_persona_conversation` 新建/切换对话时 `reset_quill_rounds`，消除"新卡首轮跳 Layer 1 常驻"。
- F3（模型合规性）记录不修。

#### M3.1 统一上传通道 & 路径安全（D3 + F2）

四组 base64 双份 handler 收敛为一个统一上传通道：

```
interfaces/web/upload.py
  read_upload(request, *, keys=("file","upload","card","avatar"), limit=50MB) -> UploadPayload
  UploadPayload: {filename, data: bytes, content_type}
  # 内部自动处理: await request.files() / await request.form() / base64 字段双通道
  # 全部走 await 异步读取——与真实 AstrBot 一致，避免重构版翻车坑
```

四组双份 handler（rag_upload、persona_import、persona_import_text、backup_export）全部改为调用该通道，base64 消歧逻辑（沙箱 FormData 限制的 workaround）收敛为一处实现。二进制扩展名黑名单列表从两处抄写收敛为一处常量。

**路径安全同步落地**：移植重构版 `resolve_safe()`/`sanitize_name()`（Windows 保留名、控制字符、`..` 处理）到 `quill/core/paths.py`，替换现有分散的 `..`/`/`/`\` 手工检查（web_routes.py:1218 等）。原版 zip-slip 三重防护已很强，`resolve_safe` 补的是日常文件名处理的盲区。

#### M3.2 错误处理整改（D4 + D4b）

**原则先行：吞错误（D4）与 fail-open（D4b）是两个不同的问题，分开处理：**

- **D4 吞错误**（memory_store 20+ 处 `logger.warning` + 返回空）：
  - 底层（memory_store/vector_store/kb）改用摘自重构版的 `QuillError` 体系：存储层抛 `StorageError`（携带 detail），调用方决定降级策略。**不做全量改造**——只改造"失败不可见"最伤的 6 条高频路径：add/search/prune/delete/backup/restore。其余保持 warning+返回空，但日志中带上方法名与异常链（`logger.warning("... %s", exc_info=True)`）。
  - 面板可观测：失败计数通过现有 `/info` 端点暴露（`_note_fts_failure` 模式推广）。
- **D4b 激活判定 fail-open → fail-close**：
  - 检测器加载失败或检测异常时返回 False（不注入），而非 True（全量注入）。理由：fail-open 的故障形态是"每轮注入全部设定"（最贵行为、用户可感为刷屏注入报告），fail-close 的故障形态是"该注入时没注入"（下一轮恢复，用户可感为偶发设定丢失）——后者更安全且自愈。
  - 这是本版本**唯一一处有意行为变更**，进 CHANGELOG 首条并在 BASELINE.md 记录。

#### M3.3 存储层修复（D5 + F1）

- **可重入异步锁**：摘取重构版 `_ReentrantLock`（任务级持有者/深度跟踪，含死锁回归测试模式），替换 memory_store/vector_store/kb 三处"不可重入锁 + 注释纪律"。注释里的"此处已在 _lock 内不能再调用 _exec_*"警告全部可以删除——锁本身安全了。
- **FAISS 维度自适应**：摘取重构版方案——索引维度由首批真实向量决定，不再硬编码 512（原版 `quill_rag/embedding.py` 有 1 处硬编码）。带上线下旧 512 维索引的兼容读取分支 + 索引文件头版本号；旧索引首次加载时若维度不匹配则触发一次重建（面板提示），不自动烧 API 配额。
- **双重检查**：对照重构版 VERIFICATION.md 记录的原版十个真实缺陷清单，存储相关项（session 键处理、LIKE 转义等）逐项自查修掉。

#### M3.3.5 数据层修复的特别说明

三库不动 ≠ 存储层不修。三库布局不变（用户零感知），但每库内部修复照做：memory_store 的吞错误、vector_store 的 FAISS 维度、kb 的全表扫描。kb 全表扫描 2000 条含 content 全文进 Python 的问题（kb.py:774、861），改为 FTS 命中后按 id 回表，避免降级路径 O(全库)。

#### M3.4 热路径优化（D8）

`on_llm_request` 每轮对全部历史 contexts 做注入报告 scrub + 状态栏 strip（main.py:2380-2412）。改为：

- 维护每个会话的"已清洗游标"（最后处理到的消息时间戳/序号），只对增量消息执行正则。
- 涉及注入报告的结构变化时（如世界书格式调整），通过世代号使游标失效，触发全量重洗。
- 验收：100 轮长对话下，`on_llm_request` 钩子耗时曲线从线性增长变为平稳；注入报告内容与旧实现逐字节一致。

### M4 文档对齐 & 发布 v5.3.0（2-3 天）

- [ ] M4.1 版本号单源化落地：唯一真源 `quill/__init__.py:__version__`，`@register` 与 metadata.yaml 由 `tools/check_version.py` 对拍校验（不匹配 CI 失败）。
- [ ] M4.2 README 重写漂移段：面板"单文件内联"→实际 30 文件结构说明；PDF 支持声明默认删除（实现 PDF 解析需引入 pypdf 可选依赖 + 双通道实现，收益/成本比低；若未来需求强烈可单开小版本）。
- [ ] M4.3 CHANGELOG v5.3.0 条目撰写：结构重构主题 + 唯一行为变更（激活 fail-close）+ 正确性修复清单。
- [ ] M4.4 LLM Guard 合规复查：logger 只从 `astrbot.api` 导入（v5.2.5 曾因内置 logging 被市场拒审，这是上架硬约束）；确认新 `quill/` 包内 logger 用法合规。
- [ ] M4.5 发 `v5.3.0` tag + 市场提交。

### M5 观察期（2 周）

- 真机跑 v5.3.0，重点观察：状态栏解析行为、激活时序、面板写操作、长对话性能（D8 优化效果）、FAISS 旧索引重建提示是否如预期。
- 预留快速回滚点：git tag + 市场版本回退路径；任何回归按"定位→M2/M3 对应 PR→cherry-pick 修复"处理。
- 收集用户反馈，评估是否需要 v5.3.1 补丁窗口。

### M6 收尾归档（0.5 天）

- [ ] 删除所有过渡期 re-export 兼容层（M2.0 约定）。
- [ ] 确认 `.zcode/`、`.build/`、`.backup/` 等本地工具目录未被误动。
- [ ] `knowledge/` 残留旧库确认无引用后移动到 `.backup/`（v5 时代 workspace 残留）；README 同步说明数据目录位置。
- [ ] 本规划文档归档（已在 `docs/v5.3/PLAN.md`），验收结果追加到 `docs/v5.3/BASELINE.md`。

### 里程碑依赖图

```
M0 ──> M1 ──> M2 ──> M3 ──> M4 ──> M5 ──> M6
              │        │
              │        └──(M3.3 依赖 M2 的存储服务拆分完成)
              └──(M4 需 M2、M3 均验收通过)
```

---

## 4. 验收标准（发布 v5.3.0 的门槛）

1. **结构**：main.py < 150 行；`quill/` 包内零 astrbot import（架构测试守护）；`quill/core` 不依赖 `quill/services`。
2. **等价性**：状态栏 fixture（1461 行）全绿；6 钩子快照测试全绿；面板 60+ 路由请求/响应形状不变（黑盒对拍）；真机全功能回归清单（M0 手术灯）逐项通过。
3. **正确性**：激活判定 fail-close；存储层 6 条高频路径失败可观测；FAISS 维度自适应 + 旧索引兼容；路径安全 resolve_safe 覆盖所有文件名入口；可重入锁落地并带回归测试。
4. **测试**：CI（Windows + ubuntu 双平台）全绿；用例总数 ≥ 重构版 190 用例的对应范围（重点：状态栏/检索/配置）；现有 3 个手写脚本断言 100% 保留。
5. **市场合规**：logger 全部走 `astrbot.api`（LLM Guard 硬约束）；无 `data/plugins/` 残留备份目录类问题。
6. **性能**：100 轮长对话下钩子耗时平稳（D8 达成）；kb 全表扫描降级路径消除。
7. **文档**：版本号单源化 + CI 对拍；README 漂移段全部修正；CHANGELOG v5.3.0 完整覆盖结构变更/行为变更/修复清单。

---

## 5. 开放问题（实施前需确认）

1. **M2.0 拆分粒度**：HealthTracker 被状态栏与记忆两域引用。倾向：归 statusbar 域所有，memory 域通过注入引用；实施时如发现双向依赖，把 HealthTracker 上提到 `quill/core/`。
2. **M2.3 缓存策略**：默认全部 property 实时读，实测发现瓶颈再加缓存。
3. **M3.3 FAISS 旧索引处理**：维度不匹配时"触发重建并面板提示" vs "直接静默重 embed"，倾向前者（尊重 API 配额，用户知情）；需确认原版是否已有索引维度记录文件可直接读取。
4. **M2.2 钩子快照的桩设计**：构造 event/req 桩对象时，`get_extra/set_extra` 的行为需要精确复刻（重构版 main.py:5-43 对框架行为有细致刻画，可作桩设计输入）。
5. **M1.4 CI 矩阵**：Windows runner 分钟数消耗约为 Linux 的 2 倍，若仓库 Actions 配额受限，可降级为 ubuntu-only + 本地 Windows 手跑。

---

## 6. 与重构版的关系声明

本规划刻意选择**原版渐进原位重构**而非以重构版为基座，核心理由：

1. 重构版存在一个会让面板所有写操作失效的致命 Web 层 bug（同步读 async 属性），且其测试体系三道防线全部漏过——基座本身未经真实框架验证。
2. 原版在持久化正确性、框架兼容性上的实战经验（异步 web 读取、WAL/在线备份、zip-slip、GreedyStr 坑）是重构版不具备的资产。
3. 用户决策已确认：三库不动、原版面板保留——重构版两大结构资产（单库+迁移器、studio 面板）恰好都在不采用范围内。

重构版的价值通过 **§2.25 摘果实清单** 落地：错误体系、可重入锁、FAISS 维度检测、路径安全、pytest 基建、测试用例设计。它同时也是重要的反面教材库——其验证体系的教训（**stub 的形状必须与真实框架一致，验证强度取决于复刻真实调用路径的程度**）直接体现在 M1.1 的 stub 修订要求里。
