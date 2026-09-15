# Changelog

## v5.2.3（排版收尾）— 控件宽度收敛 + 三处对齐缺陷 + 分段控件

上一轮桌面化重建后，配置页仍有「控件宽窄不一、排版零碎」与若干高度错位。
本轮逐项实测（Chrome CDP + AstrBot 桌面端实机）后修正，**未新增下拉菜单**。

**先说结论：下拉并未增多**
对比重写前后的配置页源码，控件类型与数量基本一致（`<select>` 旧 6 / 新 5，
`number` 各 11，`checkbox` 各 11），`_conf_schema.json` 的 `options` / `_special`
也一字未改。观感上「到处是下拉」源于**宽度失控**：同一张卡片里混着
211 / 260 / 303 / 1068px 四种宽度，长下拉在宽卡片里铺满整行，视觉权重被放大。

**控件宽度：三档令牌**
- 新增 `--w-num: 132px`（数字+单位）/ `--w-ctl: 220px`（普通输入、下拉、滑块）/
  `--w-wide: 420px`（多行文本、提供商下拉），全部左对齐到同一条竖线
- 根因是两类宽度互相打架：`.unit-wrap` 是 `inline-flex`，内部输入框被压到内容宽度（211px）；
  `.cfg-field .field--wide { max-width: none }` 又让提供商下拉铺满整行（1068px）。
  现改为 `.unit-wrap` 定宽 + 输入框 `width: 100%`，`.field--wide` 收敛到 420px
- 删除 `applySelWidth()` 里给 `.sel-wrap` 写内联 `maxWidth` 的分支——宽度已在 CSS 声明，
  JS 再写一份必然分叉

**配置页分栏**
- `.cfg-cols` 下限 `320px` → `min(100%, 460px)`：320px 在常见窗口宽度下排成 3 列，
  而「注入引擎」有 4 张卡片、必有孤行卡片单独落到下一行。改两列后卡片更舒展、无孤行

**三处对齐缺陷**
- `.page-head` 用 `align-items: flex-end` + 副标题 `padding-bottom: 4px`，
  标题与副标题底边差 **4.5px**（六个页面标题区全部可见）→ 改 `align-items: baseline`，去掉内边距
- `.cfg-row--stack` 是 `display: block`，把「LLM 智能提取」的开关甩到左下角，
  而同一张卡片其它开关都在右边缘（实测间隙 16px vs 0）→ 改回 flex 居中，
  实测同卡片所有开关 `rightGap` 一致为 16px
- `.wr-grid` 用 `align-items: start`，同行卡片高 110 / 158 / 132px 参差 → 改 `stretch`，
  底边对齐（`.wr-card-foot` 已是 `margin-top: auto`，开关自动落到统一基线）

**2 个选项的下拉改为分段控件**
- 新增 `.sel-seg` 与 `enhanceSelectAsSegmented()`：**仅**用于互斥、无禁用项、选项数 2–3 的枚举
  （HIG：分段控件要求全部选项可见；选项多或含禁用项时下拉更合适，此时自动回退到下拉）
- 首个应用：「提示词注入位置」原为 2 项下拉，现为分段控件，两个选项常显、一次点击即选中；
  同时补充了一段说明文案（原来只靠选项文字暗示哪个是推荐）
- 契约与下拉一致：原生 `<select>` 仍是唯一值存储，`Object.defineProperty` 遮蔽实例 `value`，
  点击写回后派发 `change`，因此 `applySettings()` / `CFG_FIELDS` 读写路径零改动
- 键盘：左右方向键移动、Home/End 到首尾，`role="radio"` + `aria-checked`

**其他**
- 记忆总览「向量索引」KPI 原显示 `NumPy (0)`——四个数字卡里混入英文词，字号还被拉到 28px
  （实测与「0」同款粗体）→ 只显示数字，`NumPy` 降级到标签行

**验证**（Chrome CDP，窗口需前台，否则 rAF 挂起会造成「菜单不显示」的假阴性）
- 6 个页面渲染正常、零 console 错误；配置页宽度实测收敛为 132 / 158 / 220 / 420 四档，
  每张卡片的控件左边缘唯一
- 分段控件：点击、方向键、`change` 事件、脏标记、保存往返（保存后已回滚测试值）全部实测通过
- 回归：自绘下拉（19 项）仍可开、配置搜索过滤、素材库分页 1→2 页、新建条目模态、
  卡片折叠持久化、深色主题（`#1C1C1E` 底 + 白字）
- AstrBot 桌面端内实机打开同一面板复核通过

## v5.2.3（桌面化修订）— 面板重建为 macOS 桌面应用形态

上一轮把面板「换皮」为 Apple 设计语言只能算试水。本轮按 macOS 应用的方式**重建**：
不只配色，而是信息架构、控件体系、材质、动效与操作方式一并对齐。**面向桌面端，移除移动端。**

**架构：边栏优先（Source-first）**
- 侧栏成为应用骨架：三个分组可折叠（状态持久化到 `/panel/ui_state`），六个页面接入实时计数，
  首屏即显示（新增 `syncSidebarBadges()`，`/info` + `/memories/stats` + `/rag/documents` 三源并行、各自独立降级）
- 内容区改为**通栏标题带**：页面标题与视图控件合并到一条通栏 sticky 材质带，
  正文独立为 `.page`。修掉了此前 sticky 工具栏材质带不横跨内容宽度、顶部留有视觉断口的问题
- 页面操作收进**主按钮 + `⋯` 溢出菜单**（菜单纯事件委托、点击外部/Esc 关闭、`aria-expanded` 同步）
- **删除 `.batchbar` 批量操作条**（它靠 `hidden` 弹出且无过渡，每次选中都把网格整体下推），
  改为「已选 N」胶囊 + `⋯` 菜单内的批量项
- **行内操作悬停显现**（Finder 范式）：`opacity 0→1`，同时挂 `:hover` 与 `:focus-within` 保证键盘等价；
  开关等状态控件保持常显

**控件体系：令牌化（对齐问题的根因）**
- 新增控件高度令牌 `--h-control` / `--h-control-sm` / `--h-icon-btn` / `--h-icon-btn-sm` / `--h-row`，
  与 macOS 字阶、行高令牌。此前高度全是散落字面量（32/26/30/34/20/22/44/50），任何两个控件并排都会错位
- `.toolbar-actions` **此前没有任何 CSS 规则**（唯一规则在已删除的移动端媒体查询里），
  导致其中按钮按**基线**对齐：「导出」「导出 ST」「文本粘贴导入」比带图标的兄弟低 3–4px，
  间距是源码空白宽度。补上 flex 基础规则后三处工具栏全部对齐
- `.card-head` 内含 `.btn--sm` 时高 50px、其余 44px → 统一 44px
- `.segmented` 30px vs 同行 32px，且内部按钮未设 `line-height` → 对齐 32px 并补 `line-height: 1`
- `.stats` 骨架→真实内容跳 5.5px（每次加载下推整块内容）→ `min-height: 20px`
- `.tagfield` 34px 夹在两个 32px `.field` 之间 → 32px
- `.persona-actions` 三处定义互相覆盖、列表视图堆叠把行撑到 86–110px 使头像居中飘浮 → 合并为一条并顶部对齐
- 表格操作列贴顶（比行视觉中心高约 10px）→ `vertical-align: middle`
- 骨架屏尺寸对齐真实组件；`.doc-row` 收窄；`.cfg-section` 锚点滚动统一走 `scrollIntoView`

**macOS 桌面交互**
- **弹层从窗口顶部垂落**（sheet 语义，`transform-origin: top center` + `translateY`），
  替换原 iOS 居中缩放；`.alert` 保持视口居中（`#alertScrim` 单独覆盖）
- **右键上下文菜单**（全文件此前 0 处 `contextmenu`）：素材库卡片 / 世界书条目 / 角色卡 / 记忆行，
  菜单项复用既有 `ACTIONS` 分发，带快捷键提示、危险项红色、贴边自动翻转、键盘可达
- **自绘下拉 listbox** 替换全部 11 处原生 `<select>`：材质浮层、当前项打勾、
  ↑↓/Home/End/Enter/Esc/首字母跳转、视口边缘翻转、`role="combobox"/"listbox"/"option"`。
  **原生 `<select>` 保留为值存储与数据源**（遮蔽实例 `value` + `MutationObserver` 重建 + 写回后派发 `change`），
  因此 `fillProviderSelect` / `modeSelect` / `applySettings` / `markDirty` 等既有代码**零改动**；
  仅在增强成功后才隐藏原生控件，JS 出错时自动回退
- **方向键列表导航**（此前 `Arrow`/`Home`/`End` 零处处理）：roving tabindex + 事件委托，
  网格按实际列数跳行，`scrollIntoView({block:"nearest"})` 不打断滚动手感
- **双击编辑**：卡片/行双击直接打开编辑器；用「单击延迟 220ms + 快照回滚」双保险解决与
  素材库卡片展开的单击冲突

**视觉**
- 分类色从 **360° 任意色相哈希**改为**固定 8 色调色板**（复用面板语义色令牌）。
  原实现白字对比度在大部分色相只有 1.78:1–2.20:1（HIG 要求 4.5:1），且 `progression` 会落到正红、
  与「危险/删除」语义撞车。同时删除卡顶 3px 全宽色带，徽章改淡底深字
- 素材库网格加 `align-items: start`：此前 grid 默认拉伸使无关键词的 113px 卡片被拉到 161px，
  底部出现 **48px 空洞**
- 滚动条改叠加式自动隐藏；spinner 从 iOS 圆环改为 macOS `NSProgressIndicator` 的 8 片花瓣离散步进

**修复既有缺陷（11 项，均经真实浏览器验证）**
1. `#wrCat` 分类筛选**从未接上 `change` 处理器**——下拉一直在但筛选无效（补处理器，
   并限定 `SELECT` 以避开新建条目弹窗里的同名文本输入框）
2. `Alt+1..6` 切页快捷键位于 `typing` 早返回**之后**——面板里总有输入框持有焦点，
   导致该快捷键实际不可用（提前到早返回之前）
3. `#pf-avatar-zone` 键盘监听在元素存在前绑定，**永远没绑上**——有 `role="button" tabindex="0"` 却敲不动（改 document 委托）
4. `.banner-progress` 缺定位祖先，所有进度条**堆在容器底部互相覆盖**（补 `position: relative`）
5. 确认框内**回车无反应**（`#promptInput` / `#dangerInput` 无分支；补上并保留 `isComposing` 中文输入法保护、
   危险框校验确认词后才放行）
6. 四个 scrim 共用 `z-index: 100`，叠放**纯靠标记顺序**（改为显式令牌分层）
7. 裁剪弹窗不能点遮罩关闭（另外三个都可以）
8. 弹窗**无焦点陷阱、无焦点归还、无滚动锁**，Tab 会走到遮罩后面
9. `saveModal()` 在无模态状态时**兜底误调 `saveWREntry()`**
10. `withBusy` 对容器替换 `innerHTML`，使批量条从 42px 塌到 31px、页面跳动（非表单控件改 `aria-busy` + `.is-busy`）
11. `showToastWithAction` 撤销后定时器泄漏；折叠卡片缺 `aria-expanded`

**移除移动端**：底部标签栏、全部 `max-width` 断点、`pointer: coarse` 触控放大（在触屏笔记本上会误触发）、
iOS 上滑 sheet 拖拽（`initSheetDrag` / `springTo`）、`env(safe-area-inset-*)`；
同时清理约 50 行零引用死 CSS（`.sheet*`、`.list-row*`、`.scrim--center/--sheet`、`.modal--lg`、
`.foot-spacer`、`.btn--lg`、`.badge--pill/--solid`、`.topper` 等）

**设计参考**：逐条对照 [emilkowalski/skills](https://github.com/emilkowalski/skills) 的
`apple-design`（WWDC 流体界面）、`emil-design-eng`、`review-animations/STANDARDS`（UI 动效 <300ms、
入场 ease-out、禁用 ease-in、仅动 transform/opacity、键盘触发的动作不做动效）执行。

## v5.2.3 — 面板全面重写为 Apple 设计语言 + 字体方案调整

管理面板（`pages/panel/index.html`）从 Material Design 3 整体重写为 Apple HIG 设计语言，
含信息架构、组件体系、动效与字体方案；同时修复 3 个在真实沙箱 iframe 中才会暴露的既有缺陷。

**设计系统（全部取自 Apple HIG 现行发布值）：**
- 颜色：改用 Apple 系统色（accent `#0088FF`/`#0091FF`、success `#34C759`、danger `#FF383C`、
  warning `#FF8D28`、系统灰 1–6），并建立 label 四级透明度层级与四级 systemFill。
  文字级强调色另设 `--accent-text`（浅 `#0071E3`/深 `#0A84FF`）——`#0088FF` 作白底文字仅
  3.52:1，低于 HIG 要求的 4.5:1，故文字场景改用加深变体
- 字阶：macOS 基线（页面标题 26 / 组标题 22 / 卡片标题 17 / 正文 14 / 说明 13 / 辅助 12），
  小于 13px 的档位为中文可读性抬升至 13 起，行高按 CJK 放宽；数字启用 `tabular-nums`
- 圆角：按 HIG「同心圆角」规则（卡片 12 / 控件 6，子级 = 父级 − 内边距）
- 分隔线：0.5px 发丝线（`transform: scaleY(.5)`）；分组列表用内缩分隔线，替代整宽边框
- 材质：工具栏/底栏/弹层改用 `backdrop-filter` 毛玻璃 + 半透明底 + 顶部高光描边，
  并配 `@supports not` 与 `prefers-reduced-transparency` 回退
- 动效：入场 `cubic-bezier(.22,1,.36,1)` 260ms、出场加速曲线 180ms；按下即反馈（100ms）。
  移动端弹层用自写 rAF 弹簧（临界阻尼），支持从当前屏幕值起步、速度接管、中途抓回反转
- 无障碍：响应 `prefers-reduced-motion` / `-reduced-transparency` / `-contrast` 三条媒体查询；
  `pointer: coarse` 下点击目标放大至 HIG 建议尺寸；状态用「颜色 + 形状」双编码

**结构重写：**
- 外壳改为「源列表侧栏（桌面）/ 底部标签栏（移动）」+ 半透明材质工具栏
- FAB 与配置页粘性底栏合并为唯一悬浮操作条（仅在存在未保存更改时出现）
- 内部用 `data-action` 事件委托替代 82 处内联 `onclick`（根除模板字符串转义隐患）
- 33 处重复内联 SVG（同一箭头重复 10 次）收敛为 40 个 `<symbol>` 图标符号表
- 配置页 5 个分区改用 HIG 内缩分组列表 + 分段式分区导航；173 处内联样式收敛为类

**字体：**
- 移除 Google Fonts（DM Sans / JetBrains Mono）外链；等宽改用系统 `ui-monospace` 栈
- 新增 HarmonyOS Sans SC（华为鸿蒙字体）：macOS 走系统 SF Pro + 苹方（零下载），
  其他平台异步加载鸿蒙字体（unicode-range 分块，仅下载实际用到的字块），
  CDN 不可达时静默回退本地字体栈，不影响功能
- 依华为字体许可要求，在面板页脚与 README 致谢区标注字体来源

**修复（均在真实沙箱 iframe 中复现并验证）：**
- **未保存配置会把用户锁死在配置页**：面板 iframe 为
  `sandbox="allow-scripts allow-forms allow-downloads"`（无 `allow-same-origin`、无
  `allow-modals`），原生 `confirm()` 恒返回 false，导致离开配置页的守卫永远拦截。改用自绘对话框
- **顶层 localStorage 异常中断整段脚本**：沙箱内 `localStorage` 抛 `SecurityError`，
  原实现有两处未捕获调用且其中一处位于顶层 IIFE，会连带废掉其后全部初始化
  （键盘快捷键、脏数据守卫、折叠恢复）。改为 `store` 封装（try + 内存兜底）
- **折叠状态等偏好跨刷新丢失**：沙箱不透明源下 localStorage 无法持久化，
  改为新后端接口 `GET/POST /panel/ui_state` 存于插件配置（含字段白名单与体积上限）
- `switchTab` 脏态守卫判断写反（进入配置页时提示、离开时反而不提示），已修正为
  离开时提示，且确认后回滚表单值而非只清标记
- `[hidden]` 被组件 `display` 声明覆盖，导致批量操作条未选中条目时也显示（补 `!important`）
- 独立打开面板时首个请求固定延迟数秒（`getBridge()` 已判空后又走满 `waitForBridge` 超时）
- 工具栏按钮组空间不足时被拆散、尾部按钮孤立换行（改为整组换行）
- 后端 `save_plugin_config` 重建 `QuillConfig` 后未同步路由持有的引用，导致
  Web 面板「写入成功但读不回来」（同时修复既有 `/panel/theme` 的同类问题）

**后端：**
- `web_routes.py`：新增 `/panel/ui_state` 读写路由（含键长/条目数/体积校验）
- `config.py` + `_conf_schema.json`：新增 `debug.panel_ui_state`；
  `debug.panel_theme` 标记为弃用（面板主题现由 AstrBot 面板控制，插件面板自动跟随）

## v5.2.2 — 配置页 MD3 重排版 + Roadmap 重写

**配置页重排版（MD3 设置页模式）：**
- 信息架构重组：4 节 → 5 节（注入引擎 / 状态栏 / RAG 与记忆 / 会话与输出 / 系统与安全），
  「流式输出控制」从基础配置迁出为独立"会话与输出"节（会话级操作语义），
  「应急反拒绝协议」归入注入引擎
- 新增 `.setting-item` 设置行组件（MD3 模式：标题/说明在左、Switch 在右），
  收敛 12 处内联样式重复写法
- 状态栏卡补「缺省占位符」输入框（default_placeholder 后端早已支持，本次补上面板入口）
- 全部配置卡可折叠 + localStorage 持久化；无记录时每节首卡展开、其余折叠，收敛首屏长度
- 保存操作条升级：干净态为静态提示，改动后变为「● 未保存更改 | 放弃 | 保存」
  （放弃 = 重拉服务端配置覆盖本地编辑）
- 顶栏合并：锚点导航与配置搜索框合为一行；数字输入补单位后缀（token/条/字符）；
  权限白名单卡与备份区加警示色调；去除配置卡片强制等高

**前端体验：**
- WR 删除（单条/批量）toast 支持 5 秒「撤销」（暂存条目数据，撤销即重建）
- 全局键盘增强：Esc 关闭最上层弹窗；`/` 聚焦当前页搜索框

**文档：**
- README 下阶段规划重写为 v5.3/v5.4/远期分层路线，并补前端单文件拆分的约束说明

## v5.2.1 — 独立代码审查修复（P0×2 / P1×6 / P2×8 / P3 若干）

对 v5.2.0 执行计划书全部变更（13 文件 +1046 行）进行两路独立子代理审查并逐项复核后，
修复全部确认属实的问题：

**P0（崩溃/数据损坏）：**
- `memory_store.py`：修复 `search()` 中维度校验行的缩进错误（SyntaxError）——该错误使
  `import MemoryStore` 失败且被 `_init_rag` 吞掉，动态记忆/Doc RAG/反思**全部静默瘫痪**
- `web_routes.py` + `main.py`：备份恢复全面安全化——恢复前先停 autoflush（不 flush，
  防旧内存态反向覆盖恢复的 quill_state.json）+ 关闭旧 aiosqlite/FAISS/WR 句柄（防
  Windows 覆盖运行中 DB 读到错乱页）；解压改"目录白名单（data/knowledge/worldbooks）
  + normpath 边界校验 + 以校验后 dest 手写落盘"三位一体（防 zip slip 与覆盖插件源码），
  逐文件容错；恢复后全量重建 State/WR/Persona/WB/RAG 并**同步刷新 Web 路由持有的组件
  引用**（此前 QuillRoutes.rag 是一次性快照，面板 API 会一直操作旧连接）
- `commands.py`：修复 `/memory pin` 与 `/memory core` 是不可达死代码的问题——两分支
  误嵌在 `search` 块无条件 return 之后，从未生效

**P1（功能错误）：**
- `main.py`：`_changed` 状态栏变更标记不再写入 session_vars——此前会持久化进
  quill_state.json 并被 prompt_builder 无白名单遍历注入 system prompt（dict repr
  污染模型输入且残留累积），现仅记 debug 日志
- `main.py`：`@记住：` 自然语言注入三重修复——切片统一以 stripped 文本为基准（此前
  前导空白时 prompt 残留尾部字符）；剥离后为空则保留原文（防空 prompt 发给 LLM）；
  群聊写入增加 admin 权限校验（与 `/memory core` 对齐，防任意群成员写核心记忆）
- `memory_store.py`：`update_core_memory` 只更新系统核心行（`length(vector)=0`），
  不再静默覆盖用户通过 `/memory pin` 钉住的记忆
- `pages/panel/index.html`：备份恢复改走 Base64 + `apiPost`（bridge 兼容）——面板
  iframe 是无 allow-same-origin 的受限沙箱，直连 fetch 无法携带 Dashboard 认证
- `pages/panel/index.html`：独立面板模式 bridge 负缓存——此前 `getBridge()` 恒 null
  但每次 API 调用仍空转 5s 才降级 fetch
- `pages/panel/index.html`：匹配测试弹窗重置确认按钮 disabled 残留

**P2（健壮性）：**
- 前端：WR 多选集合在重新拉取列表（翻页/过滤/增删）时失效，批量操作不再命中不可见
  条目；批量结果透出失败计数；恢复成功后刷新全部模块数据；移动端 toast/FAB 抬升
  避让底部导航遮挡；记忆搜索框改为准确的"按 Session ID 筛选"语义并与会话选择互斥
- 后端：对话日志查询改 `ORDER BY id DESC LIMIT + reverse`（"最近 200 条"此前实际取
  最早 200 条）；备份 zip 条目名统一 `/` 分隔符（跨平台恢复兼容）；Embedding 切换
  重初始化改用 `_spawn`（持任务引用防 GC，弃用 `get_event_loop`）并刷新路由引用；
  `/quill debug` 世界书统计修复（`list_worldbooks()` 返回 `List[str]`，此前按 dict
  取值必然报"查询失败"）
- 文档：README pip 依赖加引号（`>` 曾被 shell 当重定向）、Roadmap 移除已实现项、
  补回 FAQ 误删的问题行、命令表补 `/memory core`/`@记住：`/`/quill debug`；修正
  CHANGELOG 夸大措辞（"自动重嵌入"实为触发组件重初始化，旧文档向量仍需重新上传；
  "键盘导航"实为 ARIA 角色标注）

## v5.2.0 — 面板功能补全 + 三轮代码审查修复

**新功能（面板）：**
- 对话日志查看器：动态记忆页新增「对话日志」子页，按会话浏览/导出 RP 对话记录（Markdown/TXT），后端 API 早已就绪，本次补上前端入口
- 全量备份/恢复：配置页新增一键备份下载（zip 打包素材库/世界书/角色卡/记忆/文档索引）与恢复上传（自动解压到插件目录，防 zip slip 攻击）
- 删除/启停操作统一忙碌态：按钮禁用 + spinner，防止误以为无响应而重复点击
- 面板版本徽章改为动态读取 metadata.yaml（同时修复 `_get_plugin_version` 路径多跳一级导致永远显示 v5.0.4 的 bug）
- WR 匹配测试台：写作素材库标签页新增「匹配测试」按钮，弹窗输入文本即可测试关键词命中与匹配度
- ST 世界书导出：世界书标签页新增「导出 ST」按钮，支持导出为 SillyTavern 兼容格式
- WR 批量操作：卡片新增多选复选框 + 批量操作工具栏，支持批量启用/禁用/删除
- 记忆会话选择器：记忆浏览子页新增会话下拉框，替代纯文本搜索，按会话快速筛选
- 移动端底部导航栏：<768px 隐藏侧边栏，显示固定底部导航栏，符合 MD3 Bottom Navigation 规范
- 无障碍改进：ARIA 标签/角色/tablist 标注，装饰性 SVG 添加 aria-hidden

**P2/P3 功能增强（后端/前端/无障碍）：**
- 备份恢复 REST API：新增 `POST /backup/restore` 端点，接受 zip 上传并解压，自动重初始化 RAG 组件
- WR 批量操作：新增 `POST /wr/batch_delete` 和 `POST /wr/batch_toggle` 后端端点，前端卡片多选 + 批量工具栏
- 记忆会话列表 API：新增 `MemoryStore.list_sessions()` 方法 + `GET /memory/sessions` 路由，返回按活跃时间倒序的会话列表
- `/quill debug` 注入组成查看：显示当前会话的 Target/Session/Persona/状态栏/WR/WB/记忆/健康度/Session Vars 等调试信息
- 配置缓存失效策略：`MemoryStore` 新增 `clear_cache()` 公共方法，允许手动清空 LRU 缓存
- 前端结构拆分：按功能域添加 7 个模块级注释分隔线（工具函数/UI交互/API通信/WR/WB/角色卡/RAG/记忆/初始化）
- 阅读障碍改进：`index.html` 添加 ARIA role/label/tablist 属性，装饰性 SVG 添加 `aria-hidden`，标签切换时同步 `aria-selected`
- `/memory pin <序号> [on|off]`：聊天指令钉住/取消核心记忆，对应面板已有功能
- `/memory core <内容>`：直接写入核心记忆（不参与遗忘），绕过 LLM 反思流程
- `@记住：` / `核心记忆：` 自然语言前缀：在对话中直接写入核心记忆，自动剥离注入指令
- `/quill status` 健康度：指令输出新增 RAG 检索成功率与状态栏解析成功率（来自 HealthTracker）
- `admin_users` 报错区分：未配置时提示"请填写你的用户 ID"；不在白名单时提示"请联系群主添加"
- 状态栏占位符可配置：新增 `default_placeholder` 配置项，默认"未设置"，可自定义占位文本
- Embedding 切换联动：配置面板保存嵌入提供商后自动触发 RAG 组件重初始化（旧文档向量仍需重新上传）；MemoryStore 增加维度不匹配保护
- 状态栏变化高亮：后端记录字段变更历史（`_changed` 标记），为后续前端高亮提供数据基础

**代码审查修复（v5.1.0 → 本版本累计三轮）：**

*状态栏 L4 正则重写（叙事保护）：*
- `main.py`：L4 raw 状态栏解析的检测与删除改为**共用同一正则**——此前删除侧单独构造无锚定模式（`{字段名}[：:=→].*`），会从叙事句中间的同名字段删到行尾（如"想读懂她的心情：那份悸动"被拦腰截断）；现整行对称移除（含列表/Markdown 符号前缀，不留空行残留）
- 值加 `{1,30}` 上限：行首"字段：长句"更可能是叙事，宁可漏检交给 L5 宽松解析，不误删正文
- 顺带支持 `**心情**：羞涩` Markdown 粗体字段名；删除死代码 `_RAW_STATUS_RE`（已被动态版取代）

*功能缺陷（P0/P1）：*
- `web_routes.py`：修复备份导出 `backup_export` 双 `dirname` 路径错误（dir 指到 `plugins/` 目录，永远 404）；同时补全 `knowledge/` 和 `worldbooks/` 两个数据目录（此前只打包 `data/`）并跳过 `-wal`/`-shm` 临时文件
- `_route_core.py`：修复记忆导入用 `asyncio.to_thread` 包装 **async 函数**导致协程从未执行——Web 面板导入显示成功但一条都没写库，改为直接 `await`
- `memory_store.py`：修复 `update_core_memory` 写入的空向量核心记忆行（`vector=b'', dim=0`）与正常维度向量 `np.stack` 形状不兼容——闲时反思一旦建立核心记忆，该会话的记忆检索永久失效；现跳过空向量行
- `memory_store.py`：补齐 LRU 缓存失效——`/memory clear`、`/quill reset`、`set_core`、`prune_memories` 此前不失效缓存，已删记忆仍会被召回
- `commands.py`：`/memory search` 分数字段修正（读取 `rrf_score`/`vec_score`，此前恒显示 0.00）；`/memory list` 改用真实 COUNT 总数（此前"共 N 条"随页码增长）
- `memory_store.py`：新增 `count_session_memories`

*健壮性（P2）：*
- `memory_store.py`：`mark_memories_used` 置 `is_active=1`——被召回的记忆进入活跃态，匹配缓存查询与 prune 分支语义（此前 `is_active` 从未被置位，60 天清理分支是死代码）
- `kb.py`：FTS5 查询改 OR 连接 + 过滤 <3 字符 token（trigram 下限），中文长句不再必然 miss 全表扫描；补 `asyncio.Lock` 串行化写路径（与 memory/vector store 的 F4 修复对齐）
- `retrieval.py`：`_spawn` 镜像 main.py 的异常日志修复

*P3：*
- `memory_store.py`：`utcnow()` 弃用替换（保持 naive UTC 比较语义）、裸 `except:` 收敛；`get_recent_chat_logs` 改 `ORDER BY id`（同秒消息顺序稳定）
- `main.py`：清理过时的"prune_memories 是同步方法"注释
- `pages/panel/index.html`：移除独立页调试用的 QDBG 标题栏污染（`document.title`），该行完成定位使命后已被清理

**第一/二轮修复：**

*P0 崩溃修复：*
- `memory_store.py`：修复 `_init_db()` 中 `_exec_fetchall` 重入 `asyncio.Lock` 导致的自死锁——现在 FTS5 回填直接使用 `self._conn.execute` 而非锁辅助方法（`#110`）
- `memory_store.py:605-610`：修复 `get_stats()` 中 `await coro(...)[0]` 语法错误（`'coroutine' object is not subscriptable`），统计不再恒为 0

*P1 功能错误修复：*
- `main.py:286-292`：反思守护进程补上空闲过滤——按 `last_active` 解析时间戳，仅对空闲超过 1 小时的会话执行反思与日志清理，不再误删活跃会话日志
- `vector_store.py:89-125`：`_load_index()` 中 `faiss.read_index` 等同步操作移入 `asyncio.to_thread` 避免阻塞事件循环；`load_index()` 修复漏 `await`（协程从未执行，/doc reload 实际无效）且不再持锁（避免与非重入锁死锁）
- `embedding.py:58`：本地模型首次加载 `_load_local_model()` 移入 `asyncio.to_thread`
- `main.py:521-526`：`_spawn` 后台任务 done 回调增加异常检查，异常不再静默吞没

*P2 健壮性与安全修复：*
- `web_routes.py`：`config_all` 经 `_ALLOWED_CONFIG_KEYS` 白名单过滤（不复下发敏感字段）；补齐 `enable_autonomous_reflection` 白名单键（此前面板无法保存该开关）
- `state.py`：autoflush 连续失败改为指数退避（上限 60s）而非 3 次后永久停止；Windows 下 `os.replace` 因文件占用短暂失败时能自动恢复
- `memory_store.py:114`：`except: pass` 改为 `logger.warning`，FTS5 回填失败可见
- `kb.py:698`：回退全表扫描上限从 500 放宽到 2000
- `llm_summarizer.py:114`：JSON 提取改用 `json.JSONDecoder.raw_decode` 逐层解析，消除贪婪正则在多 JSON 块输出时的解析失败
- `worldbook.py:180`：`get_active_worldbooks()` 返回深拷贝（`get_worldbook` 已修，此处漏修）
- `web_routes.py:1114,1224`：`export_v2_card` 中 PIL 图片操作移入 `asyncio.to_thread`
- `prompt_builder.py:225`：加注释说明 `max_prompt_length` 语义为字符（与 `_smart_truncate` 的 `len()` 度量一致），防止未来误改

## v5.1.0 — 全自动自迭代记忆 (Phase 4) 与检索架构演进

本版本正式引入了“全自动自迭代记忆 (Autonomous Memory Management)”，同时对搜索模块引入混合检索与 LRU 缓存，是记忆架构的重大演进。

**核心功能升级 (Phase 4)：**
- 引入**闲时检测 (Idle Detection) 与 反思守护进程 (Reflection Loop)**：在系统空闲时自动分析处理过去未被提纯的记忆，总结“new_core_traits”、“crucial_facts”和“trivial_summaries”。
- **长期核心记忆更新**：每次反思均可迭代角色的深层设定和不可逆客观事件。
- **混合检索 (Hybrid Search)**：结合 FTS5 (BM25) 与 Vector 检索，使用 Reciprocal Rank Fusion (RRF) 技术结合频次与时间衰减 (Ebbinghaus) 过滤记忆。
- **LRU Session Cache**：构建基于 `OrderedDict` 的 LRU 会话向量缓存，加速同 Session 多次检索，减少磁盘与序列化开销。
- **前端支持**：配置面板新增“全自动闲时反思”开关，可自由控制守护进程的启停。

## v5.0.6 — 数据库并发架构升级与 RAG 性能优化

本版本完成数据库与并发架构升级，以及 RAG 性能优化，大幅度提升了系统的并发承载能力和稳定性。

**数据库与并发架构升级：**
- 动态对话记忆库 (`MemoryStore`) 与 文档切片元数据 (`FaissVectorStore`) 全面升级采用 `aiosqlite` 异步驱动。
- 将初始化时易发生阻塞的同步建表操作抽离，实现了完全非阻塞的 `initialize()`。

**RAG 性能优化：**
- 针对向量库的高强度运算，将 FAISS 相关检索和插入（如 `index.add_with_ids` 和 `index.search`）卸载至后台线程 (`asyncio.to_thread`)。
- 使用了更安全的 `asyncio.Lock()` 维持多并发请求下的读写安全。
- 检索请求直接 `await` 异步数据层，大幅减少了线程切换带来的事件循环阻塞（Event Loop Freezing）问题，全面增强高并发访问时的稳定性。

## v5.0.5 — 前端 MD3 重构 + 命名统一 + 代码审查修复

本版本完成前端面板 Material Design 3 重构、写作素材库命名统一（kb→wr）、以及 10 项代码审查修复。无破坏性变更。

**前端 MD3 重构：**
- 前端面板 `pages/panel/index.html` 按 Google Material Design 3 标准全面重构
- 采用 MD3 配色体系、圆角卡片、涟漪按钮、状态层交互等视觉规范
- 响应式布局适配移动端，网格/列表视图切换
- 表单控件统一为 MD3 Filled/Outlined 风格

**命名统一（kb → wr）：**
- 写作素材库类名 `KnowledgeBaseManager` → `WritingResourceManager`（`kb.py`）
- API 路由 `/kb/*` → `/wr/*`（10 条路由，`web_routes.py` + `_route_core.py`）
- 配置节名 `knowledge_base` → `writing_resource`（`_conf_schema.json` + `config.py`）
- SQL 表名 `knowledge_base` → `writing_resource`（含 FTS 虚拟表与触发器）
- DB 文件名自动迁移：`quill_kb.db` → `quill_wr.db`（`main.py` 启动时 `os.rename`）
- 前端 DOM id/函数名/变量名 `kb*` → `wr*`（`index.html`）
- 角色卡扩展字段 `bound_knowledge_base` → `bound_writing_resource`
- 删除所有 kb→wr 向后兼容回退代码（无外部用户，无需兼容）
- 修复表名迁移遗漏：新增 `_migrate_legacy_tables` 自动迁移旧 SQLite 表名

**10 项代码审查修复：**
- P1: `vector_store.py` `add()` 增加 embedding 维度校验，不匹配时抛 `ValueError`
- P2: `state.py` autoflush 增加连续失败上限（3 次），超过后停止后台重试
- P2: `main.py` Prompt 装配失败日志脱敏，只记录 `persona_id` 和字段长度
- P3: `main.py` `req.contexts` 增加 `isinstance(list)` 防御性类型守卫
- P3: `vector_store.py` `delete_by_source` 改为先删 FAISS 再删 SQLite，避免幽灵向量
- P3: `state.py` `_atomic_write` 改用 `tempfile.mkstemp` 生成安全临时文件名
- P3: 清理 `_commit_msg.txt`，`.gitignore` 新增 `.opencode/`、`.trae/`、`_commit_msg.txt`
- P4: `kb.py` `match_count` 更新统一为 `_increment_match_counts` 批量方法
- P4: `kb.py`/`worldbook.py` 异常类型细化（`sqlite3.Error`、`OSError`、`json.JSONDecodeError`）
- P4: `persona_manager.py` 三态 mode 增加白名单校验（`_normalize_mode`），非法值归一化为 `disabled`

**文档更新：**
- 新增 `Introduction.md`：全插件工作流程与配置项说明（6 章节架构文档）
- `README.md` 更新：写作素材库英文名、架构说明、版本号、MD3 重构说明

## v5.0.4 — 核心记忆锚定 + 模型路由 + 多项 Bug 修复

本版本新增两项核心功能（P0 级升级）并修复多个影响体验的 Bug，无破坏性变更。

**P0-1 核心记忆锚定（Core Memory Anchoring）：**
- 新增 `is_core` 字段（SQLite 热迁移，兼容老数据库），支持将关键记忆钉住为核心锚定记忆
- 核心记忆**不参与 Top-K 竞争**，无条件注入 `<core_memory>` XML 标签，类似人设基石
- `prune_memories` 遗忘清理跳过核心记忆（`is_core=1` 永不清理）
- Web 面板记忆列表新增钉住/取消钉住按钮 + 核心锚定标签（黄色高亮整行）
- 记忆详情弹窗显示核心锚定状态
- 新增 API：`POST /memory/pin`（`{memory_id, is_core}`）

**P0-2 模型路由（Model Routing）：**
- 状态栏 LLM 智能提取支持独立配置 provider（`status_bar.llm_provider_id`）
- 留空时自动回退到 RAG 摘要 LLM（`rag.llm_provider_id`），向后兼容
- 建议配置轻量/便宜的模型（如 GPT-4o-mini）做 JSON 提取，降低成本
- 前端状态栏配置卡片新增 LLM 模型下拉框

**Bug 修复：**
- **私聊管理员权限误判**：`_check_group_permission` 用字符串 `"PrivateMessage"` 判断私聊，但 AstrBot 统一使用 `MessageType.FRIEND_MESSAGE` 枚举，导致私聊用户被误判为无权限；改用枚举判断
- **`/quill reset` 后旧记忆残留**：reset 时 `persona_id` 为空只清理了 `target_id` 的日志，未清理 `target_id::persona_id` 的旧日志；新增 `delete_all_session_memories` 和 `delete_all_session_chat_logs`，用 SQL `LIKE target_id::%` 批量清理
- **流式控制按钮报错**：后端返回扁平结构 `{"status":"ok","message":...}` 但前端 `api()` 期望 `result.data` 字段；统一为 `{"status":"ok","data":{...}}` 格式
- **头像裁剪 `request.args` 报错**：`PluginRequest` 对象没有 `args` 属性；`persona_avatar` 和 `persona_export` 端点改用 `request.query.get()`
- **头像重新裁剪只能基于小图**：裁剪后的 300×300 小图覆盖了 `avatar_path`，重新裁剪时拿到的是已裁剪的小图；新增 `_originalAvatarDataUrl` 缓存，上传时缓存原图，重新裁剪时优先用缓存
- **删除确认按钮无法点击**：角色名含特殊字符（如 `│`）时全名匹配失败；改为首词匹配（输入第一个空格前的单词即可确认）
- **管理员白名单重载后丢失**：前端发送 `permission.admin_users` 但后端 schema 定义为 `permissions.admin_users`，键名不匹配；统一为 `permissions`

**新功能：**
- **Web 面板流式输出控制**：基础配置区域新增流式模式批量控制卡片，支持一键设置所有会话的流式模式（自动/开启/关闭），含统计信息
- **头像自定义裁剪**：Web 面板角色卡编辑弹窗内置裁剪器，支持拖拽移动 + 滚轮缩放，Canvas 生成 300×300 方形 PNG 上传
- **指令回复文本全面修正**：全量核查 3 个文件（commands.py / main.py / _route_core.py）共 15 处不一致，修复 9 处

## v5.0.3 — UI 优化 + 代码冗余清理

本版本聚焦于前端管理面板的 UI/UX 打磨与全代码库的冗余清理，无破坏性变更。

**UI 界面优化：**
- **P0-P3 全量优化**：实施 qwen3.7-plus 版本对比报告中的全部 P0-P3 优化建议（图标可以有但不滥用 emoji）
- **统一骨架屏**：KB 列表（skeleton-card）、WB/角色卡列表（skeleton-item）、角色卡导入/RAG 检索状态统一使用骨架屏占位
- **统计数字语义色**：健康状态统计应用 `stat-value--success/--warning/--danger` 语义色类，替代内联样式
- **卡片折叠**：状态栏配置卡 + RAG/动态记忆卡支持折叠，状态持久化到 localStorage
- **修复删除二次确认按钮 Bug**：`sandboxConfirm` 函数中 `btn.disabled` 状态管理缺陷 — 危险操作后所有后续确认按钮保持 disabled 不可点击；修复为函数入口重置 + input 事件监听启用

**HTML 体积优化（178KB → 170KB，节省 2.4%）：**
- 内联 style 批量抽取为 CSS 工具 class（`.text-muted-12`、`.hint-block`、`.danger-box` 等）
- JS helper 函数抽取（`apiPost()`、`downloadJSON()`、`renderWBEntryCard()`）
- 模板抽取（WB 条目卡片渲染）
- 删除 CSS 重复定义、未使用 class、未使用 JS 函数/变量

**Python 代码冗余清理：**
- **P0 修复**：`commands.py:709` 键名不匹配 — 读取 `total_documents` 但 `vector_store.py` 返回 `total_docs`，导致 `/quill status` 的 Doc RAG 计数永远为 0
- **P1 补全**：`reranker.py` 的 `fallback_llm_id` 属性此前被存储但 LLM 降级重排逻辑未实现；新增 `_rerank_with_llm` 方法，在 rerank provider 不可用时通过 LLM 对候选内容打分排序
- **删除 15 处未使用 import**（涉及 10 个文件）
- **清理 3 处局部 re-import 遮蔽**（`web_routes.py` 的 `tempfile`/`os` 和 `file_response`）
- **删除 9 项死代码**：
  - `config.py` — `_DEFAULTS` 字典（46 行，从未被读取）
  - `prompt_builder.py` — `status_bar_prompt_template` 属性
  - `kb.py` — `@conn.setter`、`get_entry_by_id` 方法
  - `web_routes.py` — `serve_panel` 路由（引用不存在的 `web_panel/` 目录）
  - `memory_store.py` — `list_sessions` 方法
  - `state.py` — `is_first_message_injected` 方法
  - `retrieval.py` — `store_memory` 方法（已被 `store_memory_direct` 取代）
  - `persona_manager.py` — `invalidate_cache` 方法
- **异常静默吞噬修复**：4 处 `except Exception: pass` 改为 `logger.debug(..., exc_info=True)`，符合项目硬约束
- **删除不可达分支**：`web_routes.py` 的 `wb_reload` 中 `hasattr` 永远为 True 的 `_load_all` fallback 分支

## v5.0.2 — P1 体验打磨 + 状态栏智能纠正

针对 longcat2.0 评估报告中的 P1 级问题进行打磨优化，并新增状态栏智能纠正管线：

**P1 体验打磨：**
- **P1-1 状态栏 fallback 调试增强**：所有降级解析均失败时记录原始文本前 200 字到 debug 日志
- **P1-2 导入错误详情展示**：新增 `showErrorDetail` 函数，长错误信息用 modal 显示完整内容
- **P1-3 长文本 modal 可读性**：角色卡编辑 textarea 行数增大，line-height 1.6，readonly 用等宽字体
- **P1-4 RAG 异常感知面板**：配置页新增「系统健康度」卡片，滑动窗口记录最近 20 次 RAG 检索/状态栏解析成功率，三色阈值显示
- **P1-5 角色卡网格懒加载**：`<img loading="lazy">` + IntersectionObserver 无限滚动

**状态栏智能纠正（A+B+C+D 方案）：**
- **方案A 动态字段白名单**：L4 正则改为动态构建，支持用户自定义字段名；分隔符扩展 `[：:=→]` 覆盖非标准格式
- **方案B 部分提取+历史值融合**：L5 阈值从 ≥2 降为 ≥1，即使只匹配到 1 个字段也保留 LLM 值，其余用历史值补全
- **方案C LLM 智能提取（实验性）**：L1-L5 全失败时调用轻量 LLM 做结构化提取，3s 超时保护，配置开关控制（默认关闭）
- **方案D Prompt 增强**：状态栏协议加入格式强制强调 + 负例展示 + 正确示例，从源头减少格式错误

## v5.0.1 — UX 优化与安全加固

针对 longcat2.0 全链路产品体验评估报告中的 P0 级问题进行专项优化，无破坏性变更：

- **P0-1 会话 ID 友好化展示**：前端将记忆列表/详情中暴露的 `target_id::persona_id` 复合键渲染为「群号/私聊 - 角色名」可读格式，hover 仍可见原始键
- **P0-2 危险操作二次确认**：删除角色卡、清空记忆、解绑世界书等高危操作要求手动输入确认词（角色名/特定词）才执行，红色警示 UI
- **P0-3 `/quill help` 降维速查**：新增折叠式指令速查，20+ 指令按五大系统分组，纯文本输出（`use_t2i(False)`），QQ/Discord 聊天窗口内不刷屏

## v5.0.0 — Initial Public Release (重构首发版)

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
