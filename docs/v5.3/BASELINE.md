# v5.3.0 重构基线（BASELINE）— 手术灯清单

> 基线版本：v5.2.5 @ main（commit 465da1a）｜采集日期：2026-09-25
> 配套规划：`docs/v5.3/PLAN.md`
> 本文是 M2/M3 一切"行为等价"验收的对照面。重构期间**不得改变**本文所列行为面；
> 发现实现与本文不符时，以本文为准修实现（先改 BASELINE 记录再动代码）。
> M6 收尾时验收结果追加在 §9。

---

## 1. 真机环境快照（M0.1）

| 项 | 值 | 证据 |
|---|---|---|
| AstrBot 真机版本 | **4.28.0** | `D:/Program/AstrBot/backend/app/astrbot/__init__.py:3`、`runtime-manifest.json` |
| 插件版本（@register） | **"5.0.6"**（漂移！） | `main.py:376`；docstring `main.py:4` 仍写 "v5.0" |
| 插件版本（metadata.yaml） | **"5.2.5"** | `metadata.yaml:4` |
| 插件版本（README badge） | **5.2.5** | `README.md:7` |
| 测试解释器 | AstrBot 捆绑 Python **3.12.12**（含 faiss-cpu 1.15.0、fastapi 0.138.1、starlette 1.6.0、numpy 2.5.2、pytest 9.1.1、pytest-asyncio 1.4.0） | `D:/Program/AstrBot/backend/python/python.exe`；系统 Python 3.10.11 无 faiss/pytest |
| 线上安装位置 | `D:/Program/AstrBot/AstrBotData/data/plugins/astrbot_plugin_quillplus/` | metadata.yaml=5.2.5 |
| 线上数据目录（三库所在） | `D:/Program/AstrBot/AstrBotData/data/plugin_data/astrbot_plugin_quillplus/`：`quill_state.json`(85,900B)、`knowledge/quill_memory.db`(438,272B)、`knowledge/quill_rag.db`(126,976B)、`knowledge/quill_wr.db`(1,097,728B)、`quill_personas/`×4、`worldbooks/`×2（witchi.json 34KB + 示例）、`imports/`、`quill_avatars/` | v5.2.4 起运行时数据在仓库外（`_paths.py` 头注释） |
| v6 试验残留 | `.../plugin_data/astrbot_plugin_quillplus_v6/quill.db`(1,847,296B, WAL) | 重构版试验产物，与 v5.3 无关，勿动 |
| 历史备份 | `D:/Program/AstrBot/AstrBotData/_plugin_backup/astrbot_plugin_quillplus_5.1.0_20260817` | — |

**版本号三处不一致实锤**（main.py 5.0.6 ≠ metadata.yaml=README 5.2.5）→ M4.1 版本单源化 `quill/__init__.py:__version__` 的直接动因。

### 1.1 框架契约关键事实（M1 stub 设计输入）

`D:/Program/AstrBot/backend/app/astrbot/api/web.py`（AstrBot 4.28.0 实测）：

- `PluginRequest`（L165 起）：**`json/form/files/body` 全部是 `async def` 方法**，不是属性：
  - `async def json(self, default=None)`（L200；解析异常返回 default）
  - `async def form(self) -> PluginMultiDict[str]`（L228；`_form_cache` 惰性缓存）
  - `async def files(self) -> PluginMultiDict[PluginUploadFile]`（L238）
  - `async def body(self) -> bytes`（L192）
  - 同步属性（构造时固化）：`method/path/headers/cookies/content_type/client_host/path_params/plugin_name/username/query`（L177-190）
- `PluginUploadFile`（L95）：`save/read/write/seek/close` 全 async（L112-159）
- `PluginMultiDict`（L20）：`get(key, default, type)/getlist/keys/values/items`
- `PluginRequestProxy`（L254 起，模块级单例 `request` L322）：同步侧 `@property`，`async def body/json/form/files`（L306-316）；`bind_request_context()`（L326-339）contextvars 绑定
- 分发链路：`dashboard/api/plugins.py:190-229 _call_plugin_extension`，路由参数语法 `<path:name>` 与 `<name>`（L157-160）；`register_web_api` 定义于 `core/star/context.py:705-727`

### 1.2 框架契约补充：钩子/指令注册机制（M2.2 阻塞性发现，真机 4.28.1 源码实证）

- 插件导入路径 = `data.plugins.<目录名>.main`（`star_manager.py:1114`，`module_str="main"` L302）；star_map 键 = 该路径（`star_manager.py:1369`）。
- **钩子/指令以函数 `__module__` 精确匹配**两处：
  1. 装饰器即时注册（`register/star_handler.py:47-60`，`handler_module_path=handler.__module__`）；
  2. 加载时实例绑定 `get_handlers_by_module_name(metadata.module_path)`（`star_handler.py:191-199` 为 `==` 精确比较；`star_manager.py:1258-1268` functools.partial 绑定）；
  3. 分发时 `star_map.get(handler.handler_module_path)`，查不到**静默跳过**（`process_stage/method/star_request.py:40-47`）。
- **结论：钩子/指令函数定义搬进任何子模块都会使 `__module__` 不等于插件注册路径 → 六钩子/七指令全部静默失效。** PLAN §2.1"钩子住 interfaces/"不可行，修订为：**注册桩（装饰器+签名+priority）留在 main.py 类体，桩体一行委托 interfaces/astrbot_hooks.py 的实现函数；业务逻辑下沉 quill/services/**。
- 该发现与 PLAN §0.1 重构版教训同源："接口数量不是正确性的来源，与真实框架的握手验证才是"。
- **M1 stub 纪律：request 的 json/form/files/body 必须建模为 `async def`（可带 `default` kwarg），method/path/query 等为属性。**（重构版翻车教训：stub 把它们建成同步属性，三道测试防线全部漏过。）

---

## 2. 事件钩子（6 个，main.py）

| # | 钩子 | 装饰器 | 签名 | 体量 | 顶层降级 |
|---|---|---|---|---|---|
| H1 | `on_waiting_llm_request` | `@filter.on_waiting_llm_request(priority=100)` L1217 | `(self, event: AstrMessageEvent)` | L1219-1257 | **无**（内层小块 L1224-1228 `except: return`；`state_manager.get_state` L1242 抛出会上抛框架） |
| H2 | `on_using_llm_tool` | `@filter.on_using_llm_tool(priority=200)` L2087 | `(self, event, tool: FunctionTool, tool_args: dict\|None)` | L2092-2197 | 吞掉放行（不修改 tool_args）L2196-2197 |
| H3 | `on_llm_request` | `@filter.on_llm_request(priority=100)` L2349 | `(self, event, req: ProviderRequest)` | L2351-2614（264 行，最大） | 吞掉放行 + 脱敏摘要 L2598-2614；`emergency`/`extra_info` try 前预初始化 L2363-2364 |
| H4 | `on_llm_response` | `@filter.on_llm_response(priority=10)` L2616 | `(self, event, resp: LLMResponse)` | L2618-2722 | 吞掉放行 L2721-2722 |
| H5 | `on_llm_tool_respond` | `@filter.on_llm_tool_respond(priority=10)` L2724 | `(self, event, tool, tool_args, tool_result)` | L2729-2831 | **无顶层 try（唯一例外）**；gate 间异常上抛框架；内层两块 warning 吞掉（L2830/2827） |
| H6 | `on_decorating_result` | `@filter.on_decorating_result(priority=100)` L2837 | `(self, event)` | L2839-2906 | 吞掉放行 L2904-2906（发送绝不中断） |

**H1 行为要点**：先 `await self._ensure_persona_conversation(event)`（L1222，必须早于框架 `_get_session_conv()`，自带全量 try L2035-2085）；拦截字面 `/reinject`/`/重新注入`（L1231-1239，**用 sender_id 非 target_id**，重置 quill_rounds 后 set_result return）；按 `state.stream_mode` off/on/auto 设置 `enable_streaming` extra（L1244-1257；auto 且激活词/括号时关流式）。

**H6 行为要点**：**无 `_quill_activated` gate，始终执行**。两档强度：状态栏开 → 只擦原始标记 `_strip_raw_markers`（L2892-2893）；关 → 全套 `_strip_status_artifacts`（L2894-2895）。只减法不补栏。

### 2.1 两个不对称点（重构时必须保留的怪癖）

1. **H5 无顶层 try**是刻意的还是历史意外未考，但 M2.2 快照测试必须复刻"gate 间异常上抛"行为。
2. **`_quill_activated` gate 不覆盖**：H4 的状态栏处理段（L2641-2688，在 L2690 gate 之前）与 H6 全部——这两个清理路径始终执行，与激活无关。另外 H5 依赖 `_quill_memorized` 专用标记去重（L2758-2760），**不把 `_quill_activated` 置 False**（历史 bug，注释 L2746-2757）。

### 2.2 `_quill_activated` 闸门时序（激活异常语义 = 现状 fail-open 边界）

- 设点**仅 L2581**（H3 尾部，全部注入成功之后）。读取点：L2097（H2）、L2690（H4）、L2741（H5）。
- 未激活 → L2475-2477 `reset_quill_rounds` + return，不注入，下游三钩子跳过。
- **激活检测自身异常**（`activation.py:75-90` 无 try 纯正则）：冒泡到 H3 顶层 except L2598 → 吞掉 → **不注入、gate 不置位**。
- 注入中途异常（L2581 前）：同上，注入不完整且 gate 不置位；L2581 后异常：注入已完成 gate 已置位。
- **M3.2 D4b 变更点**：现状激活检测异常时整体走顶层降级 = 不注入（fail-close 形态），但 PLAN 要求把"检测器**加载失败**"也改 fail-close（重构版 `plugin.py:397-405` 的 fail-open 指加载期 fail-open——检测器加载失败时退化为每轮全量注入）。改造时区分"单轮检测异常"（现状已是不注入）与"检测器加载失败"（现状 fail-open，需改 fail-close）。

---

## 3. 指令（7 个）

| 指令 | 别名 | 注册方式 | 参数 | 权限模型 |
|---|---|---|---|---|
| `/wb` | 无 | `@filter.command("wb")` L2912 | `args: GreedyStr`（必带，无默认值；L51-59 注释解释为何不能用 `GreedyStr=""`） | 写子命令群聊 admin |
| `/char` | 无 | L2918 | `args: GreedyStr` | 同上 |
| `/quill` | 无 | L2923 | `args: GreedyStr`；空参→`quill_status`；分派 help/reset/debug/statusbar/test | `quill_debug`/`reset` 群聊 admin |
| `/memory` | 无 | L2958 | `args: GreedyStr` | 写操作 admin |
| `/doc` | 无 | L2964 | `args: GreedyStr` | 写操作 admin |
| `/stream` | 无 | L2970 | `arg: str = ""`（唯一非 GreedyStr） | 写操作 admin |
| `/reinject` | `重新注入` | `@register_command("reinject", alias={"重新注入"})` L2975（**非** filter.command，import L33） | 无参 | admin（`commands.py:1617-1619`） |

权限实现：`commands.py:29-59 _check_group_permission` — 私聊直接放行（L44-45）；群聊仅 `admin_users` 白名单（L53-58）；**admin_users 为空时 fail-close 拒绝**（L47-52）。参数切分统一 `_split2`（main.py L62-69）。

**等价验收补充**：指令文本前缀（wake prefix）由框架处理，不在本插件职责内；`/reinject` 在 H1 有字面前置拦截（L1231-1239），指令 handler 与前置拦截**并存**，M2 重排时不得丢其一。

---

## 4. 注入顺序（H3 `on_llm_request` L2361-2597，顺序不得变）

1. L2365 `_restore_smt_tool(req)` — 无条件，最先
2. L2370-2392 **Context Restoration 垫回**：`req.contexts` 空/≤1 条 且 `rag_enable_chat_logging`（默认 True）且 retriever.memory_store 存在 → `get_recent_chat_logs(mem_session_id, limit=8)` 前插（L2391）；session 维度 `target_id::persona_id`（L2008-2009）；写入侧：用户 L2416-2422、助手 H4 L2693-2707 / H5 L2788-2795（`_quill_assistant_logged` 防双写）
3. L2401-2412 状态栏关闭时清洗历史 contexts 中已渲染状态栏
4. L2414 `_inject_persona_and_first_message`：`req.conversation.persona_id = "[%None]"`（L2204，切断原生人格）+ 空上下文插开场白（L2216-2222）
5. L2416-2422 用户消息落 chat_logs（仅绑卡、非 `/` 指令）
6. L2428-2446 自然语言核心记忆（`@记住：` 前缀，正则 L112-115；改写 req.prompt L2440 + 后台写库）
7. L2448-2452 最近 12 条存 `_quill_recent_msgs` extra
8. L2454-2464 拼多轮 context_text（末 4 条）
9. L2466 `_check_activation`（激活词/【】括号/WR 关键词，L2225-2256；WR 匹配异常内部吞 L2253-2254）
10. L2470-2473 `worldbook_always_activate` 强制 `activated=True, wr_activated=False`
11. L2475-2477 **未激活 → reset_quill_rounds + return**
12. L2479-2482 `increment_quill_rounds`；`quill_rounds>1` → `skip_constants`（跳过 Layer1 常驻）
13. L2486 `_rewrite_smt_tool_description`（SMT 改写，见 §4.1）
14. L2500-2515 emergency 检查 + extra_info（含 session_vars）
15. L2521 `_prompt_builder_for_request(_sb_effective)`（浅拷贝对齐状态栏开关，L1411-1427）
16. L2534-2540 `build_system_prompt(wr_manager, _wb_for_request, ...)` — **世界书+WR 注入点**（`worldbook_enabled=False` 传 None 整段跳过）
17. L2543-2545 `_run_rag_retrieval`（L2258-2326）— **RAG 注入点**：doc 检索 L2280 → memory 检索 L2285 → 核心记忆无条件注入 L2289 → `format_for_prompt` 追加到 dynamic_prompt
18. L2547-2556 世界书触发日志注入（worldbook_show_log）
19. L2560-2563 `inject_prompt` 合并：默认 `stable → original → dynamic`；`user_prefix` 时 `dynamic → stable → original`（prompt_builder.py L302-329）
20. L2565-2579 tail message 追加到 req.prompt（开：`build_status_reminder`；关：禁止状态栏文案）
21. **L2581 `event.set_extra("_quill_activated", True)`**（闸门唯一点位，在全部注入成功之后）
22. L2583-2584 `update_activity` / `clear_refusal`

`build_system_prompt` 内部段序（prompt_builder.py L164-300）：emergency(L186) → anti_refusal(L194) → send_message_guide(L199) → status_bar_guide(L206，门控) → session_state(L213-226，门控) → persona_card(L228-249) → Layer1 WR 常驻+随机池(L377-413) → Layer1 WB 常驻(L415-426) → Layer2 WR 匹配+fallback(L430-473) → Layer2 WB 匹配+token 上限(L475-502) → safety_wrapper(L282-287，进 dynamic)。

> PLAN 总原则：注入顺序"世界书→记忆→WR→RAG"是宏观描述，实际实现如上（WR 与 WB 同在 build_system_prompt，记忆检索在 RAG 段）。**以本清单为准。**

### 4.1 SMT（send_message_to_user）改写与请求级还原

- 常量：`_QUILL_SMT_DESC_MARKER = "MUST use this tool to send ALL reply text"`（L1980）；`_QUILL_ORIG_DESC_KEY = "_quill_orig_desc_saved"`（L1981）。
- 改写 `_rewrite_smt_tool_description`（L2328-2347）：无角色卡时跳过（L2334-2336，防 Agent 死循环）；原始描述存 **req 对象属性**（L2341，非共享 tool，防并发覆盖）；仅激活路径调用（L2486）。
- 还原 `_restore_smt_tool`（L1983-2001）：按 req 上的 `_quill_orig_desc_saved` 恢复并删属性（L1995-2001）；识别条件 = 描述含 marker（L1993）；**调用点仅 H3 开头 L2365**（每请求先还原再按需改写；H4/H6 不还原）。

---

## 5. 状态栏六级链（M2.1 搬移对象，行为完全照搬）

- 注册表 `_SB_LEVELS`（main.py L1565-1572，顺序即优先级，注释"不要随意调整" L1506）：
  - **L1 code block** `_sb_l1_code_block`（L1574-1595）：`**状态栏** ```…``` 块内替换，保留外围 Markdown
  - **L2 LOVE_DATA inline** `_sb_l2_love_data`（L1597-1613）：`[LOVE_DATA] a|b|c` 单行套模板渲染（实测主力路径）
  - **L3 STATUS legacy** `_sb_l3_legacy`（L1615-1630）：`[STATUS]…[/STATUS]`，标签在即认领（不校验空）
  - **L4 raw key:value** `_sb_l4_raw`（L1632-1695）：≥2 字段重建整栏（terminal）；单命中仅剥离该行继续下降（non-terminal，L1687-1694）
  - **L5 lenient** `_sb_l5_lenient`（L1697-1721）：宽松解析 + 历史值融合（`_lenient_parse_status` L1357-1378，内部要求 ≥2）
  - **L6 LLM 智能提取** `_sb_l6_llm_extract`（L1723-1742）：`status_bar_llm_extract` 默认关；`_llm_extract_status` L1891-1957（3s 超时、白名单、≥2 关键词启发）
- 驱动循环 `_handle_status_bar`（L1458-1561）：level loop L1515-1533（单级异常仅降该级继续 L1518-1523；`terminal=False` 不停链 L1531-1533；全败日志 L1539-1541）；统一持久化 L1552-1559（归一化后写 session_vars，标注不落库）。
- 兜底栏 `_build_default_love_data`（L1870-1889，调用点 H4 L2664-2671）。
- **HealthTracker**（main.py L260-317，滑动窗口 20，仅内存；实例化 L409）：写入点 L1530/L1544/L2320/L2324；消费方 commands.py L792-794/L987-989、_route_core.py L439/L469（handle_info）、web_routes.py L294。**归 statusbar 域所有，memory 域经注入引用**（PLAN §5.1 倾向）。

---

## 6. Web API 基线（68 条路由）

- 注册：`web_routes.py:177`（`_r = self.context.register_web_api`），全部在 `register_all()` L175-276；`main.py:679-682` 实例化，`main.py:844` 备份恢复后重建路由引用。前缀 `/astrbot_plugin_quillplus/`（L120）。
- **鉴权：无插件级权限门**，委托 AstrBot 面板（注释 L301）；唯一白名单 = 配置写白名单 `_ALLOWED_CONFIG_KEYS`（L13-45，config_save/save_batch/config_all 三处共用）。
- 业务层 `_route_core.py`（816 行，零 HTTP 依赖，42 个 `handle_*` L78-740）。
- **响应 envelope**：`{"status": "ok", "data": ...}` / `{"status": "error", "message": "..."}`（_route_core.py 模块 docstring 约定）；异常兜底 `_api_handler`（web_routes.py L135-148）→ 500 + 通用消息（不泄堆栈）；`_json_body()`（L123-132）：`await request.json(default={})` 且**非 dict 收敛为 {}**（后续必填校验出 400）。

### 6.1 路由清单（68 条，按域）

**WR 写作素材库（12）** L180-191：GET /wr/list（分页 L703-707）、POST /wr/get、/wr/create*、/wr/update*、/wr/delete*、/wr/toggle*、GET /wr/export、POST /wr/import*、/wr/test、GET /wr/categories、POST /wr/batch_delete*、/wr/batch_toggle*（* = 写操作）

**WB 世界书（12）** L194-206：GET /wb/list、POST /wb/get、/wb/create*、/wb/delete*、/wb/delete_book*（**同一 handler 历史别名**，L198 注释）、/wb/reload*（重读盘 to_thread L920-929）、/wb/entry/create*、/wb/entry/update*、/wb/entry/delete*（entry_id/id 双兼容 L844）、/wb/import_st*（multipart L853-885，路径校验 L866-869）、/wb/import_json*（tempfile L899-908）、GET /wb/export_st

**Persona 角色卡（14）** L209-222：GET /persona/list（剥离 avatar_url L942）、GET /persona/avatar（懒加载 L945-967）、POST /persona/create*、/persona/update*、/persona/delete*、/upload_avatar*（multipart 5MB L1029）、/upload_avatar_base64*（L1048）、/persona/import*（V2 卡 multipart 5MB L1090）、/persona/import_base64*（L1125）、GET /persona/export（二进制下载 L1165-1209）、POST /persona/export_base64（L1285→b64 L1305）、/persona/import_text*（L1244）、/persona/import_text_base64*（b64_text L1263）、GET /avatar/\<path:filename\>（静态头像，路径穿越防护 L1218、MIME 表 L1227-1231）

**Info/Config（4）**：GET /info（L225）、POST /config/save*（L228）、/config/save_batch*（≤64 项 L330）、GET /config/all（白名单过滤下发 L371）

**RAG（6）** L233-238：POST /rag/upload*（multipart 50MB L420）、/rag/upload_base64*（50MB L459）、GET /rag/documents、POST /rag/delete*、/rag/search、GET /rag/config

**Memory（8）** L241-248：GET /memory/list、/memory/list_all（上限 200 L549）、/memory/sessions、/memory/stats、/memory/get；POST /memory/delete*、/memory/pin*（is_core L649-665）、/memory/vector_search

**Provider/导入导出/Chatlog/Stream（8）**：GET /provider/list（L251）、GET /memory/export（L254）、POST /memory/import*（向量重建 L255）、/memory/prune*（L256）、GET /chatlog/list（limit≤1000 L676）、/chatlog/export（L260）、GET /stream/stats（L269）、POST /stream/all*（mode 枚举 L396）

**Backup（4）** L273-276：GET /backup/export（zip）、/backup/export_base64、POST /backup/restore*（**裸 body** `await request.body()` L1391）、/backup/restore_base64*（L1408）。四者共享 `_maintenance_lock`（L171；互斥检查 L1342-1445-1415-1417）；恢复核心 `_do_restore_bytes`（L1420-1549）：zip 校验 → 白名单落点 `resolve_archive_dest` → .db SQLite 头校验（L1449-1452）→ sidecar 清理（L1487-1488）→ `_prepare_for_restore`/`_reload_after_restore` 顺序约束（L1497-1531）。

**前端未调用的 11 条端点仍须保留**（外部 API 面）：/wb/delete_book、/wb/import_st、/upload_avatar、/persona/import、/persona/export、/persona/import_text、/config/save、/rag/upload、/backup/export、/backup/restore（其余 57 条面板在用）。

### 6.2 双份 handler 与 base64 消歧（六组，M3.1 收敛对象）

| 组 | multipart 侧 | base64 侧 | 备注 |
|---|---|---|---|
| 1 | rag_upload L412-444 | rag_upload_base64 L447-481 | base64 解码 L456；**50MB 限制两处抄写** L420/L459；**二进制黑名单两处抄写** L425/L462（同字面集合 `{.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.zip,.rar,.7z,.png,.jpg,.jpeg,.gif,.bmp}`；配套 `_TEXT_EXTS` 仅 L424 且实际未参与判断） |
| 2 | persona_import L1070-1114 | persona_import_base64 L1117-1163 | **扩展名白名单不一致**：multipart 侧 `.png/.jpg/.jpeg/.json`+`.card.png` 特判（L1085-1088），base64 侧多 `.webp`（L1136）——收敛时需决定归一方向（见 §7 开放问题） |
| 3 | persona_import_text L1240-1255 | persona_import_text_base64 L1258-1278 | b64_text→UTF-8 L1268 |
| 4 | backup_export L1340-1354 | backup_export_base64 L1357-1375 | 出向 b64 L1368-1372 |
| 5 | backup_restore L1378-1397（裸 body） | backup_restore_base64 L1400-1418 | **两种 restore 均无大小上限** |
| 6 | upload_avatar L1013-1037 | upload_avatar_base64 L1039-1062 | 5MB；另有 persona_export（GET 二进制）↔ persona_export_base64（POST JSON）一对 |

**消歧机制**：路由级双端点 + **前端选择**——`pages/panel/js/api.js:47`（bridge 通道拒绝 FormData/Blob；isFormData/isBlob 才走 fetch）、api.js:13-45（bridge 探测+负缓存）；面板实际只调 base64/JSON 变体（rag.js:74、persona.js:450/462/487、backup.js:19/43、cropper.js:156；worldbook.js:238 注释确认"读为文本走 JSON 绕开沙箱 FormData 限制"）。**M3.1 统一上传通道不得改变此传输契约。**

### 6.3 请求体读取方式（M3.1 收敛的等价面）

- `await _json_body()`（46 处）、`await request.files()`（L415/856/1020/1076）、`await request.form()`（L429/861）、`await request.body()`（L1391）、query 参数（L539/548/549/575/675/676/685/686/703-707/913/950/1171）、b64 字段（`b64_data` L452/1048/1125/1408、`b64_text` L1263）。

### 6.4 原子写（5 份拷贝，M3.1 `quill/core/atomic_io.py` 收敛对象）

1. `worldbook.py:22-36` `_save_json_atomic`（mkstemp+fsync+os.replace；调用 L282/312/327/344/439）
2. `persona_manager.py:62-73` `_sync_write_bytes`
3. `persona_manager.py:135-146` `_sync_write_file`
4. `state.py:150-160` `_atomic_write`（调用 L136/222）
5. `quill_rag/vector_store.py:151-156`（faiss 索引：固定 `.tmp` + os.replace，无 mkstemp/fsync，风格不同）

**勿与上述合并**的 tempfile 非原子写：web_routes.py:900、_route_core.py:424（写临时→解析→删除）；_backup_util.py:113/150；main.py:630-640（一次性迁移裸 os.replace）。

### 6.5 前端面板结构（M4 文档对齐依据）

- `pages/` 共 **32 文件**：index.html（1123 行）、12 CSS、18 JS（4336 行）。JS 非内联——单 ES module 入口 `<script type="module" src="js/app.js">`（L1121）。**README L352-394"单文件内联"声明失实**（M4.2 修正）；README:392"角色卡页内联所有头像"亦过时（persona_list 已剥离 avatar_url，头像懒加载）。
- `api.js` 是传输层（bridge vs fetch、envelope 解包、候选 URL L84-88、Bearer L77-78），**不是端点注册表**——端点字符串分散在 js/pages/*.js 与 components/。PLAN §2.4 的 `tools/check_routes.py` 设计需按此事实调整（扫全部 js 文件而非仅 api.js）。
- 面板静态服务由 AstrBot Plugin Pages 承担（main.py:688-694 仅检查 index.html 存在）。

---

## 7. 非源码资产保护清单（M0.3）

| 资产 | 状态 | 重构期处置 |
|---|---|---|
| 线上三库 + quill_state.json | 在仓库外（`<AstrBotData>/plugin_data/astrbot_plugin_quillplus/`） | **最高保护**。任何重构不触其路径布局（三库不动） |
| `knowledge/`（quill_memory/quill_rag/quill_wr.db + 旧名 quill_kb.db） | v5 旧布局残留副本；-wal/-shm mtime 2026-09-21 仍被打开过 | 保护至 M6：确认无引用且无进程占用后移 `.backup/`（PLAN M6） |
| `.backup/`（2.2MB，含 2026-09-16 全量源码快照） | 历史回退手段 | **原样保留**，不入重构改动范围 |
| `.build/`（48MB harness） | 前端构建中间产物 | 同上，不动 |
| `.zcode/` | 会话规划文档 | 不动 |
| `.uploads/` | 空目录 | 不动 |
| `data/`（仓库内） | 无数据库，仅旧布局残留（quill_state.json 旧版、空 quill_personas、t2i 模板） | gitignore 已覆盖；不动 |
| `worldbooks/witchi.json` | 用户数据，已 gitignore | 不动；`示例世界书.json` 已跟踪，保留 |
| `docs/probe_*.py` ×6 | 真机探针（config_effects/live_chat/no_leak/platform_template/session_on_off/session_override），均有 `__main__` | **保留**（M2 验收手段） |
| `tests/` ×3 | test_status_bar_parsers.py(1461 行)/test_memory_fts.py(241)/test_config_projection.py(278)；前两个硬编码路径可经 `ASTRBOT_APP` 环境变量覆盖（status_bar L33、fts L30）；config_projection 无路径依赖 | M1.2 转 pytest 用例 |
| `tools/` ×3 | relink_panel/split_panel/verify_modules（一次性拆分脚本，已跟踪） | 保留 |
| `.github/ISSUE_TEMPLATE/` | 已跟踪 | 不动 |
| `__pycache__/`（根/docs/tests） | 生成物 | 可随时清 |
| 根散件 | `_commit_msg.txt`(已 ignore)、`requirements.txt`(Pillow/faiss-cpu/numpy/aiosqlite)、`requirements-local.txt`(可选本地嵌入模型)、`encryption.py`（实为 Base64 标记编解码，防 LLM 剥尾文本）、`_astrbot_bootstrap.py`、`_backup_util.py`（WAL 快照助手）、`_fts_util.py`、`_paths.py`、`_route_core.py`、`logo.png` | 按各自归位处理 |

**gitignore 已验证覆盖**：data/、*.db 及 sidecar、knowledge/*（白名单 2 个 md）、worldbooks/*.json（白名单示例）、.backup/、.build/、_commit_msg.txt、__pycache__、.zcode/。`git status` 当前仅 `?? docs/v5.3/`。

---

## 8. 基线验收面（重构期间每个 PR 对照）

1. **指令等价**：7 指令名称/别名/参数/权限行为不变（§3）。
2. **钩子等价**：6 钩子降级行为、gate 时序、两个不对称点（§2.1）逐项对照。
3. **注入顺序**：H3 内 22 步顺序 + build_system_prompt 内部段序不变（§4）。
4. **状态栏**：六级链顺序、terminal/non-terminal 语义、持久化行为不变（§5）；1461 行 fixture 全绿；probe_no_leak 无裸 `[LOVE_DATA]` 泄漏。
5. **Web API**：68 路由方法/路径/请求读取方式/envelope 形状不变（§6.1-6.3）；黑盒对拍请求/响应。
6. **传输契约**：bridge 通道 FormData 拒绝、base64 变体选择（§6.2）——面板零改动下后端重构后面板全功能可用。
7. **数据布局**：三库路径、命名、WAL 模式、quill_state.json 结构不变。
8. **性能等价**：重构不引入新的热路径退化（D8 优化是唯一允许的改进方向）。

### 8.1 开放问题（实施中需拍板，随答案更新本节）

- [x] **v5.2.5 基线点 git tag**：已打在 M1 结束点 `a60ed3e`（tag `v5.2.5+testbase`，含测试保护网），2026-09-25。
- [ ] **persona_import 扩展名白名单归一方向**（§6.2 组2 不一致）：倾向 base64 侧集合（更宽，含 .webp）——确认后同步 BASELINE §6.2 并在 M3.1 实施时消歧。
- [ ] **backup_restore 无大小上限**：M3.1 统一上传通道是否为 restore 加上限（会改变行为）→ 倾向维持现状（restore 是管理员操作且已有 zip 校验），BASELINE 记录即可。

### 8.2 真机实测发现（2026-09-25 19:04-19:32 部署后首测，经真机日志 + 框架源码确证）

均为 **v5.2.5 预存行为**（M2 前后 H2/H4/SMT 流程与 state 逻辑逐字相同），非重构回归；但用户可感知，纳入 M3 修复清单优先处理：

- [ ] **F1 SMT 回声重复回复**：AstrBot 4.28.x 的 `send_message_to_user` 为直接发送（`message_tools.py:349` context.send_message），并把已发文本记入 `_send_message_to_user_current_session_plain_texts`；respond.stage 以**精确文本匹配**去重回声（`respond/stage.py:189-207`）。羽笔流程天然打破匹配：H2 把状态栏**渲染后**随工具文本发送，H4 把 completion 回声里的状态栏**剥离**——两个变体不等 → 去重失效 → 用户收到两条（实测：19:31:22 工具直发带渲染状态栏 + 19:31:47 respond 再发剥离版，间隔 25s）。修复方向：H4 检测 completion 为已发送文本的回声（对照 extra 记录 + 状态栏变体归一）时置空 result，让 respond.stage 走空链跳过。
- [ ] **F2 quill_rounds 不随换卡重置**：`_get_target_id` 返回 UMO（聊天会话级，main.py `_get_target_id`），计数跨角色卡连续（实测三张卡连计到第 9 轮）；换卡建新隔离对话后第一轮即 `skip_constants=True`，而新对话无历史可承载被跳过的 Layer 1 常驻 → 新卡首轮缺失 WR/WB 常驻内容。修复方向：`_ensure_persona_conversation` 新建/切换对话时 `reset_quill_rounds`（记忆会话键 `UMO::persona` 已隔离，仅注入计数键需要跟上）。
- [ ] **F3（记录，暂不修）**：SMT 强制描述下模型仍可能纯文本直出（实测 Layla 两轮），属模型合规性，框架侧行为，与插件无关。

---

## 9. 验收记录（M2/M3/M6 完成后追加）

- **2026-09-25 19:04 部署 v5.3/m2-structure（M0+M1+M2.1+M2.3 @ aea4733）至真机**：`D:/Program/AstrBot/AstrBotData/data/plugins/astrbot_plugin_quillplus/`，v5.2.5 旧版备份于 `_plugin_backup/astrbot_plugin_quillplus_5.2.5_preM2_20260925_190257`（回滚=拷回）。导入冒烟通过。
- **2026-09-25 M2.2 六钩子薄化完成**（958722e→9d3117d 六轮，每轮独立提交+快照先写先跑）：
  - H6→strip.py、H1→character.py、H2→response.py、H5→memory.py、H3→prompt.py（services 落位）；H4 实现整体留 interfaces（框架胶水，下沉决策记录 docstring）
  - 注册桩全部保持框架契约（BASELINE §1.2）；降级层位/无顶层 try 等怪癖逐项钉住；legacy 源码窗口断言（t25/t26）以行为契约 docstring 方式保绿，legacy 文件零改动
  - main.py 2978（v5.2.5）→ **1753 行**；interfaces/astrbot_hooks.py 869 行；快照套件 163 passed（双模式）
  - **M2 验收面"main.py <150 行"评估**：该目标制定于发现框架注册契约（§1.2）之前。当前 main.py 剩余内容 = 注册桩×6 + 指令桩×7 + `__init__` 组件接线 + `save_plugin_configs` + H3 经 plugin.* 调用的辅助方法（激活检测/RAG 编排/SMT 改写还原等）+ re-export 兼容层。进一步压行需把 init 接线与配置保存也服务化——超出 M2.2 范围且行为风险/收益比不佳。**处置：M2 验收按修订口径执行（钩子/指令全桩化 + 业务逻辑零残留于钩子层 + 快照全绿 + 架构守卫通过 = 达标）；main.py 进一步瘦身转为 M6 前的可选清理项，随真机观察期评估。**
  - 待真机复测（M2.2 后重新部署）：PLAN §M2.2.4 20 轮聊天 + 注入报告 + 状态栏。
- **19:16-19:32 真机首测（M2.1 状态栏 + M2.3 配置投影验收）**：
  - 状态栏 L2（`statusbar.parsers:419` LOVE_DATA inline）与 L4（`:491` raw key:value）均从搬移后新模块执行 ✓；无裸标记泄漏 ✓；H4 剥离兜底正常 ✓
  - `/quill statusbar` 无参四行报告 / `on` / `off` 会话级切换 ✓（与 legacy t22 基线一致）
  - 换卡（3 张）→ 独立对话隔离 + 开场白注入 + SMT 重写 + 连续激活跳 Layer1 + 记忆检索/自动摘要 全链路 ✓
  - 面板配置保存 ×3（含 status_bar.llm_provider_id / performance.max_output_length / worldbook.injection_position）全部成功、injection_position 变更即时反映到 prompt ✓
  - 全程插件日志零 ERROR、零 Prompt 装配降级 ✓
  - 发现 F1/F2/F3（见 §8.2），均为预存行为，转入 M3 修复清单。
