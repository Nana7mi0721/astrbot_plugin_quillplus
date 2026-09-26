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
| 2 | persona_import L1070-1114 | persona_import_base64 L1117-1163 | **扩展名白名单不一致**：multipart 侧 `.png/.jpg/.jpeg/.json`+`.card.png` 特判（L1085-1088），base64 侧多 `.webp`（L1136）——**M3.1 已收敛：并集 {.png,.jpg,.jpeg,.webp,.json}，两通道同一 handler**（决策记录见 §8.1） |
| 3 | persona_import_text L1240-1255 | persona_import_text_base64 L1258-1278 | b64_text→UTF-8 L1268 |
| 4 | backup_export L1340-1354 | backup_export_base64 L1357-1375 | 出向 b64 L1368-1372 |
| 5 | backup_restore L1378-1397（裸 body） | backup_restore_base64 L1400-1418 | **两种 restore 均无大小上限** |
| 6 | upload_avatar L1013-1037 | upload_avatar_base64 L1039-1062 | 5MB；另有 persona_export（GET 二进制）↔ persona_export_base64（POST JSON）一对 |

**消歧机制**：路由级双端点 + **前端选择**——`pages/panel/js/api.js:47`（bridge 通道拒绝 FormData/Blob；isFormData/isBlob 才走 fetch）、api.js:13-45（bridge 探测+负缓存）；面板实际只调 base64/JSON 变体（rag.js:74、persona.js:450/462/487、backup.js:19/43、cropper.js:156；worldbook.js:238 注释确认"读为文本走 JSON 绕开沙箱 FormData 限制"）。**M3.1 统一上传通道不得改变此传输契约。**

**M3.1 收敛状态（2026-09-26）**：组 1/2/3/6 已收敛为单一 handler + base64 路由别名注册（复用组 P3-3 delete_book 先例），消歧逻辑唯一实现于 `interfaces/web/upload.py:read_upload`（multipart 文件 → 表单 b64 → JSON b64 三通道按序探测）；wb_import_st（multipart 单通道）文件读取同批接入。组 4（backup_export，出向 zip）与组 5（backup_restore，裸 body / JSON-b64）不属文件上传通道，维持双端点现状。memory_import / wr_import / wb_import_json 为 JSON 文本通道，不属文件上传，未动。

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
- [x] **persona_import 扩展名白名单归一方向**（§6.2 组2 不一致）：**M3.1 已决——统一取并集** `{.png, .jpg, .jpeg, .webp, .json}`（即 base64 侧更宽集合）。理由：①M3.1 收敛后两通道共用同一 handler，白名单物理上只剩一份，必须取单值；②并集兼容面最宽、纯放宽——multipart 侧用户此前传 `.webp` 会 400，现转为可用，无任何既有客户端受损；③`.webp` 本就经 PIL 解析（parse_v2_card 图像分支 + save_avatar 白名单含 .webp），语义自洽。实现于 `interfaces/web/upload.py` 的 `CARD_IMPORT_EXTS`/`CARD_IMAGE_EXTS`，错误文案统一为「支持 PNG/JPG/WebP/JSON」。2026-09-26。
- [x] **backup_restore 无大小上限**：维持现状（restore 是管理员操作，已有 `_maintenance_lock` 互斥 + zip 校验 + SQLite 头校验 + 白名单落点三重防护）；`read_upload` 不套用于 restore 两端点（裸 body / JSON-b64 均不属 multipart 文件通道）。2026-09-26。

### 8.2 真机实测发现（2026-09-25 19:04-19:32 部署后首测，经真机日志 + 框架源码确证）

均为 **v5.2.5 预存行为**（M2 前后 H2/H4/SMT 流程与 state 逻辑逐字相同），非重构回归；但用户可感知，纳入 M3 修复清单优先处理：

- [x] **F1 SMT 回声重复回复（M3.0 已修，09-25 23:53 会话与 09-26 14:44 复测通过）**：AstrBot 4.28.x 的 `send_message_to_user` 为直接发送（`message_tools.py:349` context.send_message），并把已发文本记入 `_send_message_to_user_current_session_plain_texts`；respond.stage 以**精确文本匹配**去重回声（`respond/stage.py:189-207`）。羽笔流程天然打破匹配：H2 把状态栏**渲染后**随工具文本发送，H4 把 completion 回声里的状态栏**剥离**——两个变体不等 → 去重失效 → 用户收到两条（实测：19:31:22 工具直发带渲染状态栏 + 19:31:47 respond 再发剥离版，间隔 25s）。修复方向：H4 检测 completion 为已发送文本的回声（对照 extra 记录 + 状态栏变体归一）时置空 result，让 respond.stage 走空链跳过。
- [x] **F2 quill_rounds 不随换卡重置（M3.0 已修，09-26 14:44 复测通过：换卡即见「对话已变更，quill_rounds 已重置」+「连续第 1 轮」计数）**：`_get_target_id` 返回 UMO（聊天会话级，main.py `_get_target_id`），计数跨角色卡连续（实测三张卡连计到第 9 轮）；换卡建新隔离对话后第一轮即 `skip_constants=True`，而新对话无历史可承载被跳过的 Layer 1 常驻 → 新卡首轮缺失 WR/WB 常驻内容。修复方向：`_ensure_persona_conversation` 新建/切换对话时 `reset_quill_rounds`（记忆会话键 `UMO::persona` 已隔离，仅注入计数键需要跟上）。
- [ ] **F3（记录，暂不修）**：SMT 强制描述下模型仍可能纯文本直出（实测 Layla 两轮），属模型合规性，框架侧行为，与插件无关。
- [x] **F4 同回合 send_message_to_user 循环调用（M3.0b 已修）**：现象——同一回合内模型反复调用 SMT（00:06:36 发正文+状态栏 → 00:06:49 又单独发一遍状态栏 → 00:07:22/00:07:32 再发两遍变体正文），插件照单全发，用户被迫手动停止 agent。根因——H2 对 `tool_args.messages` 照单处理，无同回合去重/预算语义。处置（M3.0b）——H2 JSON 解析后新增守卫段，三规则首个命中即把 messages 置 []（框架 `message_tools.py` 对空 messages 直接返回 error 且**不发送任何内容** → 用户侧零副作用）：①已发正文精确/子串重复；②整段状态栏痕迹（归一正文为空）且 `_quill_status_handled` 已置位（未置位放行 = 历史「正文一段、状态栏单独一段」合法分割）；③发送预算 `_SMT_MAX_SENDS_PER_TURN = 2`（合法分割两条、循环失败实测 3-4 条）。含媒体段放行；守卫异常只 debug 放行（宁漏勿误）；放行路径末尾登记已发正文（与 F1 回声比对共用 `_normalized_reply_body` 归一，两侧对称）与次数（恒 +1，含媒体调用）；SMT 改写描述末尾追加单次调用契约（源头减压）。
- [x] **F6 SMT 之后的直出 completion 送达（M3.0c 已修）**：现象——模型调用 send_message_to_user 发完正文后，又在 content 字段输出元叙述（实测昨日 19:17:26 "消息已发送。用户当前收到了……等待用户选择下一步剧情。"、19:19:47 同类），框架照发 → 用户看到莫名其妙的旁白。根因——改写后的 SMT 工具描述承诺 "Output text DIRECTLY in your response will be DISCARDED" 但框架并不丢弃，F1 回声去重只拦归一全等的回声。处置（M3.0c）——H4 在 F1 段之后新增 F6 段：`_quill_smt_send_count` ≥1（本轮已用工具发过）且 completion 非空 → 置空，把工具描述的承诺变成真行为；垃圾不进 chat_logs/记忆库。count 只在 H2 activated 放行路径登记——直接文本流路径（count 缺失/0）不受影响。**已知边界**：webchat 强制流式下 content 逐块直出，流式 chunk 无法拦截（框架 result_decorate 阶段对流式本就不可靠），F6 只覆盖 completion_text 路径。
- [x] **F5 剧情分支标记渲染割裂（M3.0b 输出侧归一 + M3.0c 源头换格式）**：现象——`>>> 剧情走向 <<<` 在 webchat 等 Markdown 渲染器里行首 `>>>` 被解析为嵌套引用块渲染成三条竖线，`<<<` 无此语义保持字面，观感割裂。处置（M3.0b）——输出侧归一为全角（＞＞＞/＜＜＜，`response.normalize_plot_markers`，≥3 连续箭头整组映射）：H2 对每个 plain 段无条件归一；解析器加全角容忍；H6 对非渲染栏段兜底归一（**有意收窄**：H6 对剥离后仍含渲染签名 `**状态栏**`+``` 的段不归一——legacy 兜底钩子节把「已渲染状态栏原样保留」连同 ASCII 箭头逐字钉死；该形态的 ASCII 箭头均在代码围栏内字面显示，无割裂面）。**M3.0c 修订（用户复测 webchat 仍见 >>> → 用户指令：直接换格式）**——流式直出路径不可拦截，从源头消除：剧情标记换【剧情走向】/【请选择】（prompt_builder 契约新增 `plot_block_v2` 为实际注入键、render.py 兜底栏、parsers.py plot_str 三处），`_PLOT_PATH_RE` 改双形式（【】新形态 + 旧 ASCII/全角形态向后兼容）、`_lenient_parse_status` 前瞻与 `_parse_status_block` 跳行加【】标记；输出侧全角归一保留作旧习惯兜底。契约 `plot_block` 键保留 ASCII 形态仅因 legacy t18 逐字钉住（勿消费）；**legacy 修订例外**：t18 L420「tail 提醒含完整选项块」原钉死 tail 必须含 ASCII plot_block——tail 是每轮注入的强引导位，保留它等于让模型继续学 >>>，与用户指令直接冲突，故该一行断言改为 `plot_block_v2`（tests/legacy 首次例外，带内联注释 + 本记录）。

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
- **2026-09-25 M3.0b（F4/F5 真机实测缺陷修复）**：F4 同回合 SMT 循环调用拦截（H2 守卫三规则 + 放行记录段 + SMT 描述单次契约）与 F5 剧情分支箭头输出侧全角归一（H2 plain / H6 非渲染栏段 + 解析器三处全角容忍；H6 渲染栏段豁免见 §8.2 F5 有意收窄）。主审补强：F4 规则 1 子串判重加最小长度 `_SMT_SUBSTR_MIN_LEN = 20`（短正文偶然含于已发长文属合法新消息，宁漏勿误），配套 `test_f4_short_body_substring_of_sent_passes`。快照新增 16 条（F4×10 / F5×6，红相验证 14 failed/1 passed）；双模测试 **191 passed**（基线 175 + 新增 16，纯桩模式与 ASTRBOT_APP 真实模式一致）；tests/legacy 零改动。待部署真机复测：循环调用拦截日志（`[Quill] F4 已拦截第 N 次…`）与 webchat 箭头观感。
- **2026-09-26 M3.0c（F5 源头换格式 + F6 直出丢弃）**：日志复诊确认 13:09 重载后新版已生效（main:1552 / interfaces:745 行号吻合、微信侧箭头已全角）；用户 webchat 仍见 `>>>` 根因 = 强制流式 content 逐块直出、发送前钩子不可达 → 按用户指令从源头换标记【剧情走向】/【请选择】（prompt_builder `plot_block_v2` 实际注入键 + guide/tail/wrapper 消费点切换、render.py 兜底栏、parsers.py plot_str；`_PLOT_PATH_RE` 双形式兼容旧输出）；F6 = H4 置空 SMT 之后的直出 completion（工具描述契约落地，消除元叙述旁白）。快照新增 8 条（M3.0c×4 + F6×4）；**tests/legacy 首次修订例外 1 行**（t18 L420 tail 选项块断言 plot_block → plot_block_v2，内联注释 + §8.2 F5 记录，理由：tail 教 ASCII 与用户指令直接冲突）；契约 `plot_block` 键保留 ASCII 仅作 legacy 钉住、勿消费。双模测试 **199 passed**。待部署真机复测：webchat 剧情分支观感 + F4/F1/F2 回归。
- **2026-09-26 M3.1（统一上传通道 & 路径安全，D3+F2）**：`interfaces/web/upload.py` 落地 `read_upload`（multipart 文件 → 表单 b64 → JSON b64 三通道按序消歧，json/form/files 全 await 契约）+ `UploadPayload`/`UploadError`/`UploadTooLarge`；二进制扩展名黑名单 `BINARY_EXTS` 与角色卡白名单 `CARD_IMPORT_EXTS`（并集决策见 §8.1）单源化。`quill/core/paths.py` 自参考版移植 `sanitize_name`/`_WINDOWS_RESERVED`/`resolve_safe`（模块级纯函数；移植修正：参考版 `_UNSAFE_CHARS` 为普通字符串，`\x00-\x1f` 区间写法实际只含 NUL/减号/单元分隔符三字符——既漏其余控制字符又误伤连字符，改为正则字符类让区间语义成立，连字符系合法文件名字符、persona 头像文件名真实存在）。web_routes 四组双份 handler（rag_upload / upload_avatar / persona_import / persona_import_text）收敛为单一函数体 + base64 路由别名注册，`*_base64` 重复函数删除（1549→1467 行）；wb_import_st 文件读取接入 read_upload（limit=None 保持无上限）；serve_avatar 手工 `'..'/'/'/'\\'` 三连检查收敛为 sanitize_name 规整比对（400 形态不变；合法头像名 `{safe}_{ts}{ext}` 全数透传，分隔符/保留名/首尾点空白比原检查拒得更严——纯收紧）。**等价面微移（均为错误边缘，envelope 形态不变）**：①persona_import「超限且扩展名同时非法」从 400 转 413（limit 在通道内先于扩展名检查，与 rag 原序一致）；②缺参文案统一（base64 侧「未收到文件数据」→「未收到文件」、「缺少 b64_text 参数」→「缺少 text 参数」）；③真机附带修复：原 multipart handler `getattr(upload, 'name', ...)` 在真机 PluginUploadFile（暴露 `.filename`，无 `.name`）上恒取默认值，扩展名黑名单/文件名形同虚设——read_upload 改读 `.filename`（回退 `.name` 兼容 stub）后恢复设计意图。快照新增 49 条（read_upload×13 / sanitize_name×7 / resolve_safe×7 / 路由层×22，tests/test_upload_channel.py，红相验证 import 失败 + 4 断言红）；双模测试 **248 passed**（基线 199 + 新增 49）；tests/legacy 零改动。待部署真机复测：面板头像裁剪上传、RAG 文档上传、角色卡导入（含 .webp）、世界书 ST 导入。
- **2026-09-26 M3.2（错误处理整改 D4 + D4b）**：
  - **D4 六类高频路径 StorageError 化**：`quill/core/errors.py`（自重构参考版移植 QuillError 单基类族：ValidationError/NotFoundError/ConflictError/PermissionDeniedError/DependencyMissingError/StorageError/MigrationError/ProviderError/UnsupportedFormatError；message 用户可见 / detail 进日志分离，`__str__` 携带 detail 使既有 `error_text` 信封无需改动即保留原始异常信息）+ `quill/core/storage_stats.py`（六类失败计数，threading.Lock，进程内）落位。底层 except 块从「warning+返回空」改 `note_storage_error(category) + raise StorageError(...) from exc`：memory_store（add: add/log_message；search: search_all/关键词回表；prune: prune_memories/cleanup_chat_logs；delete: delete_memory/delete_session_memories/delete_all_session_memories/delete_session_chat_logs/delete_all_session_chat_logs/delete_chat_logs_by_ids）、vector_store（add: SQLite 阶段+FAISS 阶段两处；search: 外层 FAISS 检索；delete: delete_by_source SQLite 阶段）、kb（add: add_entry（IntegrityError 重复仍 False）；delete: delete_entry；search: match 兜底扫描）、web_routes（backup: _build_backup_zip；restore: 三处失败点计数）。未改造路径日志统一带方法名 + `exc_info=True`（不改返回语义）。调用方降级逐处保留原形态：retrieval（_rag_failed / 吞掉）、main 启动清理/修剪（warning 后照常启动）、quill/services/memory.py 反思调度（spawn 任务失败进日志回调）、_route_core 面板信封（wr_create/wr_delete 新增 StorageError 分支，信封形状不变）。
  - **面板可观测**：`/info` 响应体新增 `storage_errors` 字段（`{"add":0,"search":0,"prune":0,"delete":0,"backup":0,"restore":0}`，只增字段向后兼容；结构文档见 storage_stats.py 模块 docstring）。
  - **D4b 有意行为变更（唯一）——激活判定 fail-close**：见 §8.2 后专门记录。
  - 快照新增 32 条（tests/test_storage_errors.py：QuillError 族×4 / storage_stats×4 / 六类路径×13 / /info×1 / D4b×6 / kb 直测×4），D4b 红相验证（stash main.py 后 `test_d4b_check_activation_exception_returns_false` 以 RuntimeError 失败）；双模测试 **280 passed**（基线 248 + 新增 32）；tests/legacy 零改动。待部署真机复测：面板 /info storage_errors 展示、备份导出失败信封。
  - **失败路径文案微移清单（信封形状均不变）**：①`/memory/delete` 等面板删除失败消息中的异常类型名由原始类型（如 sqlite3.OperationalError）变为 `StorageError`（error_text 只透出类型名，原始异常在服务端日志）；②`wr/create` 存储失败消息由「创建条目失败（ID 可能已存在）」（改前存储失败与重复 ID 混同的误导文案）变为「创建条目失败（StorageError）」；③`/memory learn` 存储失败从「已学习」假成功变为「学习失败： …」真实回执（store_memory_direct 本就 re-raise，add 吞错误曾使其谎报成功）；④`rag/delete` SQLite 删除失败从 `ok(deleted:0)` 静默变为 err 信封（调用方既有 except 分支本为此设计）。控制流/降级形态零变化。
- **D4b 有意行为变更记录（M3.2，v5.3.0 唯一）——激活判定 fail-open → fail-close**：
  - **改前**：检测器加载失败（YAML 缺失/损坏）→ 词表为空、激活词通道恒不命中（`load_ok` 不可见，仅 init 日志一条「激活词: 0 个」）；检测过程异常（理论不可达，纯正则）→ 上抛 H3 顶层 except 吞掉 → 不注入。两处均为「碰巧 fail-close」——无契约、无专门日志、无测试保护；而重构参考版 `interfaces/astrbot/plugin.py` `_should_activate` 是显式 fail-open（`detector is None → return True` / `except → return True`，坏检测器退化为每轮全量注入），M3.3+ 服务层整合若照搬会把 fail-open 带回来。
  - **改后**：①`activation.py`：`should_activate`/`check_brackets` 检测异常 → 捕获、warning 带异常链、返回 False（该轮不注入）；`_load` 失败 → `load_ok=False` + warning 带异常链（此前异常细节被整体吞掉）。②`main.py._check_activation`：检测调用包 try → 异常返回 `(False, False)`，与 WR 匹配既有降级（`wr_activated=False`）语义对齐。**正常判定逻辑一行未动**（快照 H3 节钉住）；`worldbook_always_activate` 强制激活是用户显式配置，不属故障路径，保持不变。
  - **权衡（PLAN §M3.2 原文理由）**：fail-open 故障形态 = 每轮注入全部设定（最贵行为、用户可感为刷屏注入报告）；fail-close 故障形态 = 该注入时没注入（下一轮检测恢复即自愈，用户可感为偶发设定丢失）——后者更安全且自愈。括号【】通道不依赖触发词文件，加载失败时仍正常工作（fail-close 只关激活词通道，不误伤括号激活）。
- **2026-09-26 M3.3 + M3.3.5（存储层修复 D5 + F1 + 缺陷自查 + kb 回表）**：
  - **D5 可重入锁**：`quill/core/locks.py` 落地 `ReentrantLock`（自重构参考版 `infrastructure/db/database.py:51-99` 移植，任务级持有者 + 重入深度）。`memory_store` / `vector_store` / `kb` 三处 `asyncio.Lock` 替换，锁语义等价（仍串行化写）；三处"此处已在 _lock 内不能再调用 _exec_*"注释警告删除，**调用结构保持不变**（不为利用可重入重构调用链），仅消除同任务自死锁风险。回归测试用 `asyncio.wait_for(timeout=5.0)` 守卫死锁场景（嵌套 acquire 同任务不阻塞、跨任务排队、非持有者释放 RuntimeError、超时守卫下的嵌套业务调用、真件 MemoryStore 持锁调 _exec_*）。**实测教训（测试写法）**：`wait_for(coro, t)` 在 Python 3.10 会把 coro 包成新任务、3.12 则在当前任务内直接 await（stub 模式 3.10.11 / 真机模式 3.12.12 行为分裂）——守卫必须包完整场景，"另一任务"必须 `ensure_future` 显式创建，否则任务身份错位把重入判定测歪。
  - **F1 FAISS 维度自适应**（真机 SiliconFlow/bge-m3 实测 1024 维 vs 硬编码 512）：`embedding.get_dim()` 去除 `return 512` 硬编码，未知返回 **0**（未知≠猜一个值）；`FaissVectorStore` 维度改由**事实**决定——磁盘已有索引以索引为准；无索引不预建（旧版在此按推断值建错维度索引），延迟到 `add()` 用**首批真实向量**的维度建；`add()` 批内异维度仍拒（调用方缺陷），provider 报告维度与实际向量不一致仅告警、以向量为准。**索引文件头**：魔数 `QPVI` + 版本号(LE uint32) + 维度冗余位 + faiss 序列化负载，单文件原子替换（`_save_index` 改 serialize+头+负载）；旧版无头文件按旧规则兼容读取（faiss fourcc ≠ QPVI 区分），首次保存自动升级；未知版本/截断文件优雅降级（移除坏文件等首批向量重建）。**维度不匹配重建**：加载期 provider 维度已知且不一致、或 add() 时维度变化 → 丢旧索引文件、按新维度建空索引、SQLite 孤儿 faiss_id 复位 -1，文本全留 chunks 表随上传回填，**不自动重烧 API 配额**（PLAN 决策保留）。
  - **VERIFICATION.md 11 类缺陷存储相关项自查结论**（详见下方清单）：修 1（锁自死锁=F1 同类）、修 3（LIKE 转义三处）、核对不适用 7。PLAN 点名项：session 键处理（_fts_scores 双路径 session 过滤 ✓ 已有，新增回归测试钉住）；LIKE 转义（**发现并修复**：memory 短词兜底 `summary/chat_summary LIKE`、`delete_all_session_memories/chat_logs` 的 `target_id::%` 前缀、`kb.search`——用户输入/会话键含 `%`/`_` 时前者多召回噪声、后者**误删其他会话数据**；统一走 `_fts_util.escape_like` + `ESCAPE '\'`）。
  - **M3.3.5 kb 全表扫描**：`kb.py` 兜底扫描（`SELECT wr.* … LIMIT 2000`）与 `keyword_match`（`SELECT * … LIMIT 2000`）改两段式——瘦身列扫描（打分只需 keywords/aliases/secondary_keywords/name/priority，LIMIT 2000 扫描窗口语义不变）→ 命中后按 id 回表（`SELECT * WHERE id IN`，500/批防变量上限）。命中集合/打分/顺序与改前一致（含 FTS 正常路径命中即返回、FTS 部分命中宁缺勿滥、FTS 故障降级路径行为原样）；O(全库) 的 content 全文搬运消失。SQL 层回归测试经连接代理记录实际执行语句断言（`wr.*`/`SELECT *` 全表捞取不得再现）+ 2101 条目钉 LIMIT 2000 边界语义 + FTS JOIN rank 契约。
  - **等价面微移（有意行为变更，均为缺陷修复方向）**：①`FaissVectorStore.search` 查询维度与索引不一致时返回 `[]`（WARNING + `dim_mismatch` 面板标记）而非 FAISS 断言炸成 StorageError——配置状态≠存储故障，与重构参考版一致；改前该形态计为 search 失败。②`FaissVectorStore.add` 维度不匹配从 ValueError 拒绝上传改为重建空索引后照常写入（文本保留、随上传回填）——改前 provider 维度错时所有上传永久失败。③`get_stats()` 的 `dim` 语义从「启动期推断值」变为「索引真实维度（0=未建）」，新增 `dim_mismatch`/`last_rebuild` 字段；`/rag/documents` 响应新增 `index_status`（只增字段）。④LIKE 转义使命中/删除**严格字面化**（含 `%`/`_` 的查询此前召回更宽、`target_id` 含 `_` 时前缀删除误伤他库——现与缓存失效的 `startswith` 语义完全对齐）。⑤`expected_dim`/`dim` 形参默认 768→0（仅提示位，不再参与建索引）。
  - **新增测试 35 条**（双模：stub 305 passed+10 skipped（faiss 用例环境无 faiss 自动 skip）；真机 315 passed）：`tests/test_locks.py`×7、`tests/test_vector_store_dim.py`×13（真 faiss×10 + 无需 faiss×3）、`tests/test_kb_fetchback.py`×8、`tests/test_like_escape.py`×7；`tests/test_storage_errors.py` 两条假 FAISS 注入用例补 `store.dim = 8`（F1 dim 门新契约，非 legacy 文件）；tests/legacy 零改动。`python kb.py` 自检脚本全过。
  - **缺陷自查逐类结论（VERIFICATION.md 11 类，仅存储层）**：
    | # | 类别 | 结论 |
    |---|---|---|
    | 1 | 数据库事务自死锁 | **同类形态存在，本轮修复**（三处不可重入锁 + 注释纪律 → ReentrantLock + 死锁回归测试） |
    | 2 | plugin_dir 差两层 | 不适用（非存储；本仓库激活路径经真机多轮验证正常，M0-M2 未现该形态） |
    | 3 | 桥 upload() 不接受额外字段 | 不适用（前端/桥类，任务范围外；本仓库 M3.1 统一上传通道已覆盖双通道语义） |
    | 4 | app.js 默认导入页面模块 | 不适用（前端类） |
    | 5 | 日志通道不合规 | 已核对：本仓库 v5.2.5（d203163）已修复——存储三模块均 `from astrbot.api import logger`（kb 顶层回退仅 standalone 自检用），无帧代理依赖 |
    | 6 | 前端 svg() 重复 6 份 | 不适用（前端类） |
    | 7 | 配置枚举项没有可选项 | 不适用（配置 schema/前端类） |
    | 8 | 验证脚本自身缺陷 | 不适用（工具类）；教训已采纳：本仓库 pytest.ini 无 `-q` 叠加问题、汇总判断走退出码 |
    | 9 | mode=ro 数据安全缺口 | 不适用（迁移器类；本仓库三库不动、无迁移路径，备份为文件级 zip、不触 SQLite 连接） |
    | 10 | 插件类放错模块 | 不适用（加载契约类；M2.2 修订已按 BASELINE §1.2 注册桩留 main.py，真机加载验证通过） |
    | 11 | 两代插件注册重名 | 不适用（部署冲突类，非存储） |
    附：PLAN §M3.3 点名的存储类专项——session 键处理 ✓（双路径过滤已有 + 新增测试钉住）；LIKE 转义 **存在→已修**（三处，见上）；N+1/批量回表 ✓（memory 关键词回表 500/批、vector 回表 IN 批量、WR 计数/日志批处理，均已有）；`search_all` LIMIT 2000 载入向量 BLOB 为检索必需载荷（向量运算输入），形态不同、非缺陷。
- **2026-09-26 M3.4（热路径优化 D8：H3 历史 contexts 清洗 O(历史长度) → O(新增)）**：
  - **机制**：`quill/services/history_scrub.py` 新增按会话（target_id）的「已清洗游标」，对 H3 步 2-3 的两遍逐条清洗（注入报告行抹除 / 状态栏关闭时的历史剥离）做**逐条记忆化**。游标键 = 消息 content 的 SHA-256 全长摘要（**刻意不用列表下标**：框架滑动窗口截头、上下文恢复前插、换卡整体更换使下标不稳定；两个清洗函数是纯文本变换、与 role/位置无关，指纹只需覆盖 content）。每会话两份通道游标（scrub / strip），与 H3 的"报告行洗在垫回前、剥离洗在垫回后"结构一一对应——恢复来的 chat_logs 不经报告行抹除的既有语义由钩子块结构保真，游标不感知。每通道持 `cache`（已清洗指纹集合：digest→输出，命中不跑正则）与 `last_seq`（最近一次清洗的指纹序列，用于每轮 GC 到活跃窗口）。
  - **世代号（失效条件）**：`_inputs_fingerprint` = (SCHEMA_GEN 手动版本位, 注入报告行正则 pattern——经 main.py 新增类属性 re-export 读取，剥离正则集 (pattern, replacement) 摘要, tuple(love_fields))，每次清洗前比对，任一变化 → 游标整体重置当轮全量重洗。love_fields 是唯一运行期可变成分（面板保存整体替换 config 后 props 实时读到新表）；字段表变化使两通道同时失效——对报告行通道是有意的过度失效（该函数不读字段表），换 rare 事件的少量重洗换机制简单可靠。游标状态被外力改坏（cache 非 dict / last_seq 非 list）同样重置回退全量（宁慢勿错）。
  - **为何逐字节一致**：增量路径与全量路径调**同一个**清洗函数（经 `plugin._scrub_inject_report` / `_strip_status_artifacts` 动态分发，与改前 self._* 同一路径，无"增量版"正则）；两函数纯文本变换，指纹命中 ⇒ 输入全文 SHA-256 相同 ⇒ 同一纯函数同一输出。逐条替换语义逐字保持：报告行通道 eligible 项一律重建 dict、剥离通道仅在文本变化时换新 dict 否则原 dict 透传（未清洗消息返回原文）；异常原样上抛（H3 桩顶层 try 降级不变），req.contexts 赋值发生在函数返回之后。
  - **内存护栏**：会话游标 LRU 上限 256（OrderedDict 超限淘汰最旧，淘汰/淘汰后重访只影响性能不影响正确性）；单通道缓存条目上限 512 + 每轮 GC 到当前窗口（空窗口不清——垫回撑起的轮次下一轮大概率复用）。
  - **不动面**：22 步编排顺序与语义零变化（`contexts_is_fresh` 判定、垫回闸门、`_sb_effective` 求值位置、开关方向、步 5-22 全部原样）；H2/H4/H6 的回复侧清洗（逐条、无历史增长）不在本轮范围。
  - **测试**：新增 `tests/test_history_scrub.py` 16 条——等价性（100 轮滑动窗口+首轮垫回场景逐轮比对增量 vs 改前 oracle vs 全新游标全量路径，三方逐字节一致 + 开启方向 30 轮）、性能代理（计数 monkeypatch 经动态分发 patch 点包住两清洗函数：持续历史每轮恒 2 次/总量 120 vs 全量 3660、fresh+垫回每轮恒 1 次、滑动窗口每轮恒 2 次——不做真实时间断言避免 CI 抖动）、失效（love_fields 变化全量重洗且输出按新表计算、报告行正则变化重洗且输出随新正则、游标改坏回退全量、开关间隙缓存存活）、护栏（LRU 淘汰重访重洗、缓存上限溢出尾部重洗）、逐条语义（非 eligible 透传同一性、未变化原 dict 透传、同内容共享一份清洗）、钩子级两轮集成（真实注册桩连跑两轮：第二轮只洗新增 2 条且结果与全量路径逐字节一致）。红相验证：stash 掉钩子接线后钩子级用例以 `assert 4 == 2` 失败（全量重洗形态被精确计数捕获）。
  - **双模测试 321 passed+10 skipped（stub，基线 305+10）/ 331 passed（真机，基线 315）**；tests/legacy 零改动。待部署真机复测：100 轮长对话下 `[Quill] 触发:` 日志时间戳间隔平稳、注入报告/状态栏行为与改前无可感差异。
