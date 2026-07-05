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
from persona_manager import QuillPersonaManager
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


# ── 创建 Quart App ──────────────────────────────────────────
def create_app(
    kb_manager,
    wb_manager,
    persona_manager=None,
    *,
    port: int = 18425,
) -> Quart:
    app = Quart(__name__)
    plugin_dir = _SCRIPT_DIR
    persona_db = persona_manager  # 别名，与路由内部变量名一致

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

    # ── Persona 角色卡 (独立 Quill 管理) ──────────────────────

    @app.route("/api/persona/list")
    @_api
    async def persona_list():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        personas = await persona_db.load_all()
        # 为每个有头像的角色卡嵌入 base64 data URL
        for p in personas:
            avatar_path = p.get("avatar_path", "")
            if avatar_path and avatar_path.startswith("quill_avatars/"):
                try:
                    fname = avatar_path[len("quill_avatars/"):]
                    data = await persona_db.read_avatar(fname)
                    if data:
                        import base64, os
                        ext = os.path.splitext(fname)[1].lower()
                        mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}
                        mime = mime_map.get(ext, 'image/png')
                        p["avatar_url"] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                    else:
                        p["avatar_url"] = ""
                except Exception:
                    p["avatar_url"] = ""
            else:
                p["avatar_url"] = ""
        return jsonify(SimpleResponse().ok(personas).__dict__)

    @app.route("/api/persona/create", methods=["POST"])
    @_api
    async def persona_create():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        data = await request.json
        try:
            result = await persona_db.create_persona(data)
            return jsonify(SimpleResponse().ok(result).__dict__)
        except ValueError as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)

    @app.route("/api/persona/update", methods=["POST"])
    @_api
    async def persona_update():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        data = await request.json
        pid = (data.get("id") or "").strip()
        if not pid:
            return jsonify(SimpleResponse().error("id is required").__dict__)
        try:
            result = await persona_db.update_persona(pid, data)
            return jsonify(SimpleResponse().ok(result).__dict__)
        except ValueError as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)

    @app.route("/api/persona/delete", methods=["POST"])
    @_api
    async def persona_delete():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        data = await request.json
        pid = (data.get("id") or "").strip()
        if not pid:
            return jsonify(SimpleResponse().error("id is required").__dict__)
        try:
            await persona_db.delete_persona(pid)
            return jsonify(SimpleResponse().ok({"id": pid}).__dict__)
        except ValueError as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)

    @app.route("/api/upload_avatar", methods=["POST"])
    @_api
    async def upload_avatar():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        files = await request.files
        upload = files.get("file")
        if not upload:
            return jsonify(SimpleResponse().error("未收到文件").__dict__)
        data = await upload.read()
        filename = getattr(upload, 'name', 'avatar.png')
        if len(data) > 5 * 1024 * 1024:
            return jsonify(SimpleResponse().error("图片文件过大（最大 5MB）").__dict__)
        try:
            rel_path = await persona_db.save_avatar(filename, data)
            url = f"/api/avatar/{os.path.basename(rel_path)}"
            return jsonify(SimpleResponse().ok({"url": url, "path": rel_path}).__dict__)
        except Exception as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)

    @app.route("/api/upload_avatar_base64", methods=["POST"])
    @_api
    async def upload_avatar_base64():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        import base64
        data = await request.json
        filename = (data.get("filename") or "avatar.png").strip()
        b64_data = (data.get("b64_data") or "").strip()
        if not b64_data:
            return jsonify(SimpleResponse().error("未收到文件数据").__dict__)
        try:
            file_bytes = base64.b64decode(b64_data)
        except Exception as e:
            return jsonify(SimpleResponse().error(f"Base64 解码失败: {e}").__dict__)
        if len(file_bytes) > 10 * 1024 * 1024:
            return jsonify(SimpleResponse().error("图片文件过大（最大 10MB）").__dict__)
        try:
            rel_path = await persona_db.save_avatar(filename, file_bytes)
            url = f"/api/avatar/{os.path.basename(rel_path)}"
            return jsonify(SimpleResponse().ok({"url": url, "path": rel_path}).__dict__)
        except Exception as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)

    @app.route("/api/avatar/<path:filename>")
    @_api
    async def serve_avatar(filename):
        if not persona_db:
            return jsonify(SimpleResponse().error("Not available").__dict__)
        if '..' in filename or '/' in filename or '\\' in filename:
            return jsonify(SimpleResponse().error("Invalid filename").__dict__)
        data = await persona_db.read_avatar(filename)
        if not data:
            return jsonify(SimpleResponse().error("File not found").__dict__)
        ext = os.path.splitext(filename)[1].lower()
        mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif'}
        mime = mime_map.get(ext, 'application/octet-stream')
        return QResponse(data, content_type=mime)

    @app.route("/api/persona/import", methods=["POST"])
    @_api
    async def persona_import():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        files = await request.files
        upload = files.get("file")
        if not upload:
            return jsonify(SimpleResponse().error("未收到文件").__dict__)
        file_data = await upload.read()
        filename = getattr(upload, 'name', 'card.png').lower()
        ext = os.path.splitext(filename)[1].lower()
        # 处理 .card.png 等特殊扩展名
        if filename.lower().endswith('.card.png'):
            ext = '.png'
        if ext not in ('.png', '.jpg', '.jpeg', '.json'):
            return jsonify(SimpleResponse().error("不支持的文件格式").__dict__)
        try:
            is_image = ext in ('.png', '.jpg', '.jpeg')
            persona_data = persona_db.parse_v2_card(file_data, is_image)
            if is_image:
                avatar_filename = f"{persona_data['name']}{ext}"
                avatar_path = await persona_db.save_avatar(avatar_filename, file_data)
                persona_data["avatar_path"] = avatar_path
            result = await persona_db.create_persona(persona_data)
            return jsonify(SimpleResponse().ok(result).__dict__)
        except ImportError as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)
        except ValueError as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)
        except Exception as e:
            return jsonify(SimpleResponse().error(f"导入失败: {e}").__dict__)

    @app.route("/api/persona/import_base64", methods=["POST"])
    @_api
    async def persona_import_base64():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        import base64
        data = await request.json
        filename = (data.get("filename") or "card.png").strip().lower()
        b64_data = (data.get("b64_data") or "").strip()
        if not b64_data:
            return jsonify(SimpleResponse().error("未收到文件数据").__dict__)
        try:
            file_bytes = base64.b64decode(b64_data)
        except Exception as e:
            return jsonify(SimpleResponse().error(f"Base64 解码失败: {e}").__dict__)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ('.png', '.jpg', '.jpeg', '.json'):
            return jsonify(SimpleResponse().error("不支持的文件格式").__dict__)
        try:
            is_image = ext in ('.png', '.jpg', '.jpeg')
            persona_data = persona_db.parse_v2_card(file_bytes, is_image)
            if is_image:
                avatar_filename = f"{persona_data['name']}{ext}"
                avatar_path = await persona_db.save_avatar(avatar_filename, file_bytes)
                persona_data["avatar_path"] = avatar_path
            result = await persona_db.create_persona(persona_data)
            return jsonify(SimpleResponse().ok(result).__dict__)
        except ImportError as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)
        except ValueError as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)
        except Exception as e:
            return jsonify(SimpleResponse().error(f"导入失败: {e}").__dict__)

    @app.route("/api/persona/export")
    @_api
    async def persona_export():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        pid = request.args.get("id", "").strip()
        if not pid:
            return jsonify(SimpleResponse().error("缺少角色 ID").__dict__)
        persona = await persona_db.get_persona(pid)
        if not persona:
            return jsonify(SimpleResponse().error("角色卡不存在").__dict__)
        try:
            avatar_data = None
            avatar_path = persona.get("avatar_path", "")
            if avatar_path and avatar_path.startswith("quill_avatars/"):
                avatar_data = await persona_db.read_avatar(os.path.basename(avatar_path))
            export_data = persona_db.export_v2_card(persona, avatar_data)
            if avatar_data:
                return QResponse(export_data, content_type="image/png", headers={
                    "Content-Disposition": f'attachment; filename="{persona["name"]}_v2.png"'
                })
            else:
                return QResponse(export_data, content_type="application/json", headers={
                    "Content-Disposition": f'attachment; filename="{persona["name"]}_v2.json"'
                })
        except ImportError as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)
        except Exception as e:
            return jsonify(SimpleResponse().error(f"导出失败: {e}").__dict__)

    @app.route("/api/persona/export_base64", methods=["POST"])
    @_api
    async def persona_export_base64():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        data = await request.json
        persona_id = (data.get("id") or "").strip()
        if not persona_id:
            return jsonify(SimpleResponse().error("缺少角色 ID").__dict__)
        persona = await persona_db.get_persona(persona_id)
        if not persona:
            return jsonify(SimpleResponse().error("角色卡不存在").__dict__)
        try:
            import os
            import base64
            avatar_data = None
            avatar_path = persona.get("avatar_path", "")
            if avatar_path and avatar_path.startswith("quill_avatars/"):
                avatar_filename = os.path.basename(avatar_path)
                avatar_data = await persona_db.read_avatar(avatar_filename)
            export_data = persona_db.export_v2_card(persona, avatar_data)
            filename = f"{persona['name']}_v2.png" if avatar_data else f"{persona['name']}_v2.json"
            b64_str = base64.b64encode(export_data).decode('ascii')
            return jsonify(SimpleResponse().ok({"filename": filename, "b64_data": b64_str}).__dict__)
        except Exception as e:
            return jsonify(SimpleResponse().error(f"导出失败: {e}").__dict__)

    @app.route("/api/persona/import_text", methods=["POST"])
    @_api
    async def persona_import_text():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        data = await request.json
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify(SimpleResponse().error("text is required").__dict__)
        try:
            persona_data = persona_db.parse_clipboard_text(text)
            result = await persona_db.create_persona(persona_data)
            return jsonify(SimpleResponse().ok(result).__dict__)
        except ValueError as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)
        except Exception as e:
            return jsonify(SimpleResponse().error(f"解析失败: {e}").__dict__)

    @app.route("/api/persona/import_text_base64", methods=["POST"])
    @_api
    async def persona_import_text_base64():
        if not persona_db:
            return jsonify(SimpleResponse().error("Persona manager not available").__dict__)
        import base64
        data = await request.json
        b64_text = (data.get("b64_text") or "").strip()
        if not b64_text:
            return jsonify(SimpleResponse().error("b64_text is required").__dict__)
        try:
            text = base64.b64decode(b64_text).decode('utf-8')
        except Exception as e:
            return jsonify(SimpleResponse().error(f"Base64 解码失败: {e}").__dict__)
        try:
            persona_data = persona_db.parse_clipboard_text(text)
            result = await persona_db.create_persona(persona_data)
            return jsonify(SimpleResponse().ok(result).__dict__)
        except ValueError as e:
            return jsonify(SimpleResponse().error(str(e)).__dict__)
        except Exception as e:
            return jsonify(SimpleResponse().error(f"解析失败: {e}").__dict__)

    # ── 信息路由 ────────────────────────────────────────────
    @app.route("/api/info")
    @_api
    async def info():
        return _wrap(await handle_info(kb_manager, wb_manager))

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
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    plugin_dir = _find_plugin_dir()
    config = _load_config(plugin_dir)

    kb_path = _resolve_kb_path(plugin_dir, config, args.kb_path)
    wb_dir = _resolve_wb_dir(plugin_dir, config, args.wb_path)

    print(f"[Quill Desktop] 插件目录: {plugin_dir}")
    print(f"[Quill Desktop] 写作素材库: {kb_path}")
    print(f"[Quill Desktop] 世界书: {wb_dir}")

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

        pm = QuillPersonaManager(os.path.join(plugin_dir, "data", "quill_personas"))
        try:
            count = await pm.get_persona_count()
            print(f"[Quill Desktop] 角色卡加载: {count} 个")
        except Exception as e:
            print(f"[Quill Desktop] 角色卡加载失败: {e}")

        app = create_app(kb, wb, persona_manager=pm, port=args.port)
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
