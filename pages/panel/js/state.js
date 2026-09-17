/* state.js — 全局可变状态（由 index.html 拆分生成）
 *
 * 为什么需要这个文件：ES module 的 import 绑定是**只读**的，
 * 而原脚本里这些顶层变量会被反复赋值（`_dirty = true` 之类）。
 * 放进 S 对象后，各模块 `S._dirty = true` 即可跨模块共享同一份状态。
 */

export const S = {
  _applyingSettings: false,
  _bridgeAbsent: false,
  _crop: null,
  _ctxHost: null,
  _ctxItems: [],
  _ctxOpen: null,
  _dirty: false,
  _lastCfgTab: "wr",
  _lastFocused: null,
  _memPage: 1,
  _memPages: 1,
  _modalMode: null,
  _modalState: null,
  _originalAvatarDataUrl: null,
  _personaEditing: null,
  _personaMode: null,
  _personaQuery: "",
  _personaShowCount: 20,
  _personaView: "grid",
  _personas: [],
  _ragConfig: null,
  _ragConfigAt: 0,
  _rawConfig: {},
  _scrollLockTop: 0,
  _selOpen: null,
  _wbEntryTagInput: null,
  _wbOnlyConst: false,
  _wbOpenItem: null,
  _wrTagInput: null,
  wbAll: [],
  wbCache: {},
};
