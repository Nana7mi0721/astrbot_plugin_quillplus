#!/usr/bin/env python3
"""Quill Desktop — 独立写作素材库/世界书管理面板

用法:
    python quill_desktop.py
    python quill_desktop.py --port 8080
    python quill_desktop.py --kb-path /path/to/quill_kb.db

无需 AstrBot 运行，零配置，自动打开浏览器。
"""

import argparse
import asyncio
import json
import os
import sys
import webbrowser
from functools import wraps
from pathlib import Path
from typing import Optional

# ── 处理依赖路径 ──────────────────────────────────────────────
# 当系统 Python 缺少 aiosqlite/quart/yaml 时，尝试复用 AstrBot 自带的 site-packages。
# 路径优先级：
#   1. 环境变量 QUILL_ASTRBOT_SITE_PACKAGES（推荐，跨机器共享时设置它）
#   2. 用户级 Python 安装 ~/AppData/Roaming/Python/Python3*/site-packages
#   3. 从当前插件路径向上回溯 AstrBot/backend/python/Lib/site-packages
#   4. 原作者本机的默认路径（AstrBot 和插件分别装在不同盘时用得上）
_ASTRBOT_SITE_PK = None
_candidates = []

_env_path = os.environ.get("QUILL_ASTRBOT_SITE_PACKAGES")
if _env_path:
    _candidates.append(_env_path)

# 用户级 Python
import glob as _glob
for _d in _glob.glob(os.path.expanduser(r"~\AppData\Roaming\Python\Python3*\site-packages")):
    _candidates.append(_d)

# 从当前插件路径向上回溯寻找 AstrBot/backend/python/Lib/site-packages
_here = os.path.dirname(os.path.abspath(__file__))
for _up in range(1, 7):
    _probe = os.path.normpath(os.path.join(_here, *([".."] * _up), "backend", "python", "Lib", "site-packages"))
    if os.path.isdir(_probe):
        _candidates.append(_probe)
        break

# 手动路径（如果 AstrBot 装在非标准位置，取消注释并填入实际路径）
# _candidates.append(r"X:\YourAstrBotDir\backend\python\Lib\site-packages")

for _p in _candidates:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
        if _ASTRBOT_SITE_PK is None:
            _ASTRBOT_SITE_PK = _p

# ── 导入依赖 ──────────────────────────────────────────────────
try:
    import aiosqlite
    import yaml
    from quart import Quart, request, jsonify, Response as QResponse
except ImportError as e:
    print(f"[Quill Desktop] missing dependency: {e}")
    print("  pip install aiosqlite pyyaml quart")
    print("  or set env QUILL_ASTRBOT_SITE_PACKAGES to a site-packages with those libs")
    print("  candidates probed:")
    for _p in _candidates:
        _mark = "OK " if os.path.isdir(_p) else "-- "
        print(f"    [{_mark}] {_p}")
    sys.exit(1)

# ── 导入本插件模块 ────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from kb import KnowledgeBaseManager
from worldbook import WorldbookManager
from _route_core import (
    ok,
    handle_kb_list,
    handle_kb_get,
    handle_kb_create,
    handle_kb_update,
    handle_kb_delete,
    handle_kb_toggle,
    handle_kb_export,
    handle_kb_import,
    handle_kb_test,
    handle_kb_categories,
    handle_wb_list,
    handle_wb_get,
    handle_wb_create,
    handle_wb_delete,
    handle_wb_entry_create,
    handle_wb_entry_update,
    handle_wb_entry_delete,
    handle_wb_import_st,
    handle_wb_export_st,
    handle_wb_bindings,
    handle_wb_bind,
    handle_wb_unbind,
    handle_info,
)

# ── 简单响应类（替代 astrbot.dashboard.routes.route.Response）──
class SimpleResponse:
    def __init__(self):
        self.status = None
        self.message = None
        self.data = None

    def ok(self, data=None, message=None):
        self.status = "ok"
        self.data = data
        self.message = message
        return self

    def error(self, message):
        self.status = "error"
        self.message = message
        return self


def _api(handler):
    @wraps(handler)
    async def wrapper(*args, **kwargs):
        try:
            return await handler(*args, **kwargs)
        except Exception as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)
    return wrapper


def _wrap(result: dict):
    """Convert _route_core result dict to SimpleResponse."""
    if result.get("status") == "ok":
        resp = SimpleResponse().ok(result.get("data"), **{k: v for k, v in result.items() if k not in ("status", "data")})
        return jsonify(resp.__dict__)
    return jsonify(SimpleResponse().error(result.get("message", "Unknown error")).__dict__)


# ── 路径检测 ──────────────────────────────────────────────────
def _find_plugin_dir() -> str:
    return _SCRIPT_DIR


def _load_config(plugin_dir: str) -> dict:
    config_path = os.path.join(plugin_dir, "config.yaml")
    if os.path.isfile(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _resolve_kb_path(plugin_dir: str, config: dict, cli_arg: Optional[str]) -> str:
    if cli_arg:
        return cli_arg
    kb_cfg = config.get("knowledge_base", {})
    rel = kb_cfg.get("kb_path", "knowledge/quill_kb.db")
    path = os.path.join(plugin_dir, rel)
    if not os.path.isabs(rel):
        return path
    return rel


def _resolve_wb_dir(plugin_dir: str, _config: dict, cli_arg: Optional[str]) -> str:
    if cli_arg:
        return cli_arg
    return os.path.join(plugin_dir, "worldbooks")


def _resolve_db_path(plugin_dir: str, cli_arg: Optional[str]) -> Optional[str]:
    """自动发现 AstrBot data_v4.db（从插件目录向上推断）。"""
    if cli_arg:
        return cli_arg
    candidates = [
        os.path.abspath(os.path.join(plugin_dir, "..", "..", "data_v4.db")),
        os.path.abspath(os.path.join(plugin_dir, "..", "..", "..", "data_v4.db")),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


# ── Persona 管理器（直读 AstrBot data_v4.db）───────────────
class PersonaDB:
    """直接通过 aiosqlite 读写 AstrBot data_v4.db 的 personas 表。"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def list_all(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, persona_id, system_prompt, begin_dialogs, tools, skills, "
                "custom_error_message, folder_id, sort_order, created_at, updated_at "
                "FROM personas ORDER BY sort_order, id"
            ) as cur:
                return [self._row_to_dict(r) for r in await cur.fetchall()]

    async def get_by_id(self, persona_id: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, persona_id, system_prompt, begin_dialogs, tools, skills, "
                "custom_error_message, folder_id, sort_order, created_at, updated_at "
                "FROM personas WHERE persona_id = ?", (persona_id,)
            ) as cur:
                r = await cur.fetchone()
                return self._row_to_dict(r) if r else None

    async def create(self, persona_id: str, system_prompt: str,
                     begin_dialogs: list[str] | None = None,
                     tools=None, skills=None,
                     custom_error_message: str | None = None,
                     folder_id: str | None = None,
                     sort_order: int = 0) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT 1 FROM personas WHERE persona_id = ?", (persona_id,)) as cur:
                if await cur.fetchone():
                    raise ValueError(f"Persona '{persona_id}' 已存在")
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            await db.execute(
                "INSERT INTO personas (persona_id, system_prompt, begin_dialogs, tools, skills, "
                "custom_error_message, folder_id, sort_order, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (persona_id, system_prompt,
                 json.dumps(begin_dialogs) if begin_dialogs else None,
                 json.dumps(tools) if tools is not None else None,
                 json.dumps(skills) if skills is not None else None,
                 custom_error_message, folder_id, sort_order, now, now))
            await db.commit()
        return await self.get_by_id(persona_id)

    async def update(self, persona_id: str, **kwargs) -> Optional[dict]:
        allowed = {"system_prompt", "begin_dialogs", "tools", "skills",
                   "custom_error_message", "folder_id", "sort_order"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return await self.get_by_id(persona_id)
        for k in ("begin_dialogs", "tools", "skills"):
            if k in updates and isinstance(updates[k], (list, dict)):
                updates[k] = json.dumps(updates[k])
        import datetime
        updates["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [persona_id]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE personas SET {set_clause} WHERE persona_id = ?", values)
            await db.commit()
        return await self.get_by_id(persona_id)

    async def delete(self, persona_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM personas WHERE persona_id = ?", (persona_id,))
            await db.commit()
            return True

    @staticmethod
    def _row_to_dict(r) -> dict:
        d = dict(r)
        for k in ("begin_dialogs", "tools", "skills"):
            if d.get(k) and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d


# ── 创建 Quart App ──────────────────────────────────────────
def create_app(
    kb_manager,
    wb_manager,
    persona_db: PersonaDB | None = None,
    *,
    port: int = 18425,
) -> Quart:
    app = Quart(__name__)
    plugin_dir = _SCRIPT_DIR

    # ── KB 路由 ────────────────────────────────────────────
    @app.route("/api/kb/list")
    @_api
    async def kb_list():
        category = request.args.get("category")
        search = request.args.get("search")
        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 100)
        return _wrap(await handle_kb_list(kb_manager, category, search, page, per_page))

    @app.route("/api/kb/get", methods=["POST"])
    @_api
    async def kb_get():
        data = await request.json
        return _wrap(await handle_kb_get(kb_manager, (data or {}).get("entry_id")))

    @app.route("/api/kb/create", methods=["POST"])
    @_api
    async def kb_create():
        return _wrap(await handle_kb_create(kb_manager, await request.json))

    @app.route("/api/kb/update", methods=["POST"])
    @_api
    async def kb_update():
        return _wrap(await handle_kb_update(kb_manager, await request.json))

    @app.route("/api/kb/delete", methods=["POST"])
    @_api
    async def kb_delete():
        data = await request.json
        return _wrap(await handle_kb_delete(kb_manager, (data or {}).get("entry_id")))

    @app.route("/api/kb/toggle", methods=["POST"])
    @_api
    async def kb_toggle():
        data = await request.json
        entry_id = (data or {}).get("entry_id")
        enabled = (data or {}).get("enabled", True)
        return _wrap(await handle_kb_toggle(kb_manager, entry_id, enabled))

    @app.route("/api/kb/export")
    @_api
    async def kb_export():
        return _wrap(await handle_kb_export(kb_manager))

    @app.route("/api/kb/import", methods=["POST"])
    @_api
    async def kb_import():
        data = await request.json
        entries = (data or {}).get("entries", [])
        return _wrap(await handle_kb_import(kb_manager, entries))

    @app.route("/api/kb/test", methods=["POST"])
    @_api
    async def kb_test():
        data = await request.json
        text = (data or {}).get("text")
        return _wrap(await handle_kb_test(kb_manager, text))

    @app.route("/api/kb/categories")
    @_api
    async def kb_categories():
        return _wrap(await handle_kb_categories(kb_manager))

    # ── WB 路由 ────────────────────────────────────────────
    @app.route("/api/wb/list")
    @_api
    async def wb_list():
        return _wrap(await handle_wb_list(wb_manager))

    @app.route("/api/wb/reload", methods=["POST"])
    @_api
    async def wb_reload():
        """Force reload all worldbooks from disk."""
        if wb_manager:
            wb_manager._load_all()
        return _wrap(ok({"reloaded": True}))

    @app.route("/api/wb/get", methods=["POST"])
    @_api
    async def wb_get():
        data = await request.json
        return _wrap(await handle_wb_get(wb_manager, (data or {}).get("name")))

    @app.route("/api/wb/create", methods=["POST"])
    @_api
    async def wb_create():
        data = await request.json
        name = (data or {}).get("name")
        description = (data or {}).get("description", "")
        return _wrap(await handle_wb_create(wb_manager, name, description))

    @app.route("/api/wb/delete", methods=["POST"])
    @_api
    async def wb_delete():
        data = await request.json
        return _wrap(await handle_wb_delete(wb_manager, (data or {}).get("name")))

    @app.route("/api/wb/entry/create", methods=["POST"])
    @_api
    async def wb_entry_create():
        data = await request.json
        name = (data or {}).get("name")
        entry = (data or {}).get("entry")
        return _wrap(await handle_wb_entry_create(wb_manager, name, entry))

    @app.route("/api/wb/entry/update", methods=["POST"])
    @_api
    async def wb_entry_update():
        data = await request.json
        name = (data or {}).get("name")
        entry_id = (data or {}).get("entry_id")
        entry = (data or {}).get("entry")
        return _wrap(await handle_wb_entry_update(wb_manager, name, entry_id, entry))

    @app.route("/api/wb/entry/delete", methods=["POST"])
    @_api
    async def wb_entry_delete():
        data = await request.json
        name = (data or {}).get("name")
        entry_id = (data or {}).get("entry_id")
        return _wrap(await handle_wb_entry_delete(wb_manager, name, entry_id))

    @app.route("/api/wb/export_st")
    @_api
    async def wb_export_st():
        name = request.args.get("name")
        return _wrap(await handle_wb_export_st(wb_manager, name))

    @app.route("/api/wb/import_st", methods=["POST"])
    @_api
    async def wb_import_st():
        files = await request.files
        upload = files.get("file")
        if not upload:
            return jsonify(SimpleResponse().error("No file uploaded").__dict__)
        form = await request.form
        name = form.get("name")
        if not name:
            return jsonify(SimpleResponse().error("Worldbook name required").__dict__)
        return _wrap(await handle_wb_import_st(wb_manager, name, await upload.read()))

    @app.route("/api/wb/bindings")
    @_api
    async def wb_bindings():
        return _wrap(await handle_wb_bindings(wb_manager))

    @app.route("/api/wb/bind", methods=["POST"])
    @_api
    async def wb_bind():
        data = await request.json
        bind_type = (data or {}).get("type", "user")
        target_id = (data or {}).get("target_id")
        wb_name = (data or {}).get("worldbook_name")
        return _wrap(await handle_wb_bind(wb_manager, bind_type, target_id, wb_name))

    @app.route("/api/wb/unbind", methods=["POST"])
    @_api
    async def wb_unbind():
        data = await request.json
        bind_type = (data or {}).get("type", "user")
        target_id = (data or {}).get("target_id")
        wb_name = (data or {}).get("worldbook_name")
        return _wrap(await handle_wb_unbind(wb_manager, bind_type, target_id, wb_name))

    # ── Persona 路由 ──────────────────────────────────────────
    @app.route("/api/persona/list")
    @_api
    async def persona_list():
        if not persona_db:
            return jsonify(SimpleResponse().error("AstrBot 数据库未配置").__dict__)
        return jsonify(SimpleResponse().ok({"personas": await persona_db.list_all()}).__dict__)

    @app.route("/api/persona/get", methods=["POST"])
    @_api
    async def persona_get():
        if not persona_db:
            return jsonify(SimpleResponse().error("AstrBot 数据库未配置").__dict__)
        data = await request.json
        pid = (data or {}).get("persona_id")
        if not pid:
            return jsonify(SimpleResponse().error("persona_id is required").__dict__)
        p = await persona_db.get_by_id(pid)
        if not p:
            return jsonify(SimpleResponse().error("Persona not found").__dict__)
        return jsonify(SimpleResponse().ok(p).__dict__)

    @app.route("/api/persona/create", methods=["POST"])
    @_api
    async def persona_create():
        if not persona_db:
            return jsonify(SimpleResponse().error("AstrBot 数据库未配置").__dict__)
        data = await request.json
        if not data or "persona_id" not in data or "system_prompt" not in data:
            return jsonify(SimpleResponse().error("persona_id and system_prompt are required").__dict__)
        p = await persona_db.create(
            persona_id=data["persona_id"],
            system_prompt=data["system_prompt"],
            begin_dialogs=data.get("begin_dialogs"),
            tools=data.get("tools"),
            skills=data.get("skills"),
            custom_error_message=data.get("custom_error_message"),
            folder_id=data.get("folder_id"),
            sort_order=data.get("sort_order", 0),
        )
        return jsonify(SimpleResponse().ok(p, message="Persona created").__dict__)

    @app.route("/api/persona/update", methods=["POST"])
    @_api
    async def persona_update():
        if not persona_db:
            return jsonify(SimpleResponse().error("AstrBot 数据库未配置").__dict__)
        data = await request.json
        if not data or "persona_id" not in data:
            return jsonify(SimpleResponse().error("persona_id is required").__dict__)
        pid = data["persona_id"]
        kwargs = {k: data[k] for k in (
            "system_prompt", "begin_dialogs", "tools", "skills",
            "custom_error_message", "folder_id", "sort_order",
        ) if k in data}
        p = await persona_db.update(pid, **kwargs)
        if not p:
            return jsonify(SimpleResponse().error("Persona not found").__dict__)
        return jsonify(SimpleResponse().ok(p, message="Persona updated").__dict__)

    @app.route("/api/persona/delete", methods=["POST"])
    @_api
    async def persona_delete():
        if not persona_db:
            return jsonify(SimpleResponse().error("AstrBot 数据库未配置").__dict__)
        data = await request.json
        pid = (data or {}).get("persona_id")
        if not pid:
            return jsonify(SimpleResponse().error("persona_id is required").__dict__)
        await persona_db.delete(pid)
        return jsonify(SimpleResponse().ok({"persona_id": pid}, message="Persona deleted").__dict__)

    # ── 信息路由 ────────────────────────────────────────────
    @app.route("/api/info")
    @_api
    async def info():
        persona_count = len(await persona_db.list_all()) if persona_db else 0
        return _wrap(await handle_info(kb_manager, wb_manager, persona_count))

    # ── 前端页面 ────────────────────────────────────────────
    # serve 新面板 pages/panel/index.html；quill_desktop 独立后端不走 AstrBot
    # register_web_api，而是把路由直挂在 Quart 的 /api/...（无插件 prefix）。
    # 注入 window.__QUILL_CONFIG__ 告知前端：当前是独立服务端（origin + desktopServer）。
    _panel_html_path = Path(plugin_dir) / "pages" / "panel" / "index.html"
    _fallback_html_path = Path(plugin_dir) / "web_panel" / "static" / "index.html"

    def _read_panel_html() -> str:
        """读取面板 HTML，并把 __QUILL_QUILL_CONFIG__ 占位替换为运行时 origin。"""
        path = _panel_html_path if _panel_html_path.is_file() else _fallback_html_path
        if not path.is_file():
            return ""
        html = path.read_text(encoding="utf-8")
        # 用运行时构造的绝对 origin 替换占位；script 在面板自身 <script> 前注入，
        # 这样后续 fetch fallback 就能把请求打到本 Quart 服务。
        config_json = json.dumps(
            {
                "astrbotOrigin": "__QUILL_ORIGIN__",
                "desktopServer": True,
            },
            ensure_ascii=False,
        )
        injection = (
            '<script>window.__QUILL_CONFIG__='
            + config_json
            + ";</script>"
        )
        if "</head>" in html:
            html = html.replace("</head>", injection + "</head>", 1)
        else:
            html = injection + html
        return html

    @app.route("/")
    async def index():
        html = _read_panel_html()
        if not html:
            return "<h1>Quill Desktop</h1><p>Frontend not found.</p>", 501
        # 把占位 origin 替换为 Quartet 的真实监听地址（带端口）。
        origin = f"http://127.0.0.1:{port}"
        html = html.replace("__QUILL_ORIGIN__", origin)
        resp = QResponse(html, content_type="text/html; charset=utf-8")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    return app


# ── 主入口 ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Quill Desktop — 写作素材库/世界书/角色卡管理面板")
    parser.add_argument("--port", type=int, default=18425, help="监听端口（默认 18425）")
    parser.add_argument("--kb-path", help="写作素材库数据库路径")
    parser.add_argument("--wb-path", help="世界书目录路径")
    parser.add_argument("--db-path", help="AstrBot data_v4.db 路径（角色卡数据）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    plugin_dir = _find_plugin_dir()
    config = _load_config(plugin_dir)

    kb_path = _resolve_kb_path(plugin_dir, config, args.kb_path)
    wb_dir = _resolve_wb_dir(plugin_dir, config, args.wb_path)
    db_path = _resolve_db_path(plugin_dir, args.db_path)

    print(f"[Quill Desktop] 插件目录: {plugin_dir}")
    print(f"[Quill Desktop] 写作素材库: {kb_path}")
    print(f"[Quill Desktop] 世界书: {wb_dir}")
    if db_path:
        print(f"[Quill Desktop] AstrBot 数据库: {db_path}")
    else:
        print("[Quill Desktop] AstrBot 数据库: 未找到（角色卡功能不可用）")

    async def _start():
        kb = None
        kb_cfg = config.get("knowledge_base", {})
        if kb_cfg.get("enabled", True):
            try:
                kb = KnowledgeBaseManager(kb_path)
                await kb.initialize()
                stats = await kb.get_stats()
                print(f"[Quill Desktop] 写作素材库加载: {stats['total_entries']} 条 (启用 {stats['enabled_entries']} 条)")
            except Exception as e:
                print(f"[Quill Desktop] 写作素材库加载失败: {e}")
                kb = None
        else:
            print("[Quill Desktop] 写作素材库已禁用")

        wb = None
        try:
            wb = WorldbookManager(wb_dir)
            names = wb.list_worldbooks()
            print(f"[Quill Desktop] 世界书加载: {len(names)} 个 - {names}")
        except Exception as e:
            print(f"[Quill Desktop] 世界书加载失败: {e}")

        pd = PersonaDB(db_path) if db_path else None
        if pd:
            try:
                count = len(await pd.list_all())
                print(f"[Quill Desktop] 角色卡加载: {count} 个")
            except Exception as e:
                print(f"[Quill Desktop] 角色卡加载失败: {e}")
                pd = None

        app = create_app(kb, wb, persona_db=pd, port=args.port)
        url = f"http://127.0.0.1:{args.port}"

        if not args.no_browser:
            print(f"[Quill Desktop] 正在打开浏览器: {url}")
            webbrowser.open(url)
        else:
            print(f"[Quill Desktop] 面板地址: {url}")

        print("[Quill Desktop] 按 Ctrl+C 停止服务器")
        await app.run_task(host="127.0.0.1", port=args.port)

    try:
        asyncio.run(_start())
    except KeyboardInterrupt:
        print("\n[Quill Desktop] 已停止")


if __name__ == "__main__":
    main()
