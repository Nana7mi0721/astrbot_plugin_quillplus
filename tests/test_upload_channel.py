# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""统一上传通道 & 路径安全测试（v5.3.0 M3.1）。

三层覆盖
--------
1. ``quill/core/paths.py``      —— ``sanitize_name`` / ``resolve_safe``
   （用户输入 → 磁盘路径的唯一安全通道，纯函数）；
2. ``interfaces/web/upload.py`` —— ``read_upload``（multipart 文件 /
   base64 表单 / base64 JSON 三通道消歧，原六组 *_base64 handler 的
   workaround 逻辑收敛点）；
3. ``web_routes.QuillRoutes``   —— 四组双份 handler 收敛后的**别名注册**
   （base64 路由与正身指向同一函数）与行为等价（响应 envelope 形态不变）。

双模式说明
----------
``read_upload(request, ...)`` 的 ``request`` 是参数，直接传
``tests/infra/framework_stubs`` 的假件代理——与环境里有无真实 astrbot 无关，
双模式（纯 stub / ASTRBOT_APP）行为一致。路由层用例经
:func:`bind_stub_request` fixture 把 ``web_routes`` 模块级 ``request``
monkeypatch 成 stub 代理：真实 ``PluginRequestProxy`` 需要 starlette
请求上下文（``bind_request_context``），测试里无法构造；而
``json/form/files/body`` 必须走 async 契约（BASELINE §1.1 / §1.2 教训），
stub 代理按真机形状建模，两模式通吃。
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from tests.infra import framework_stubs
from tests.infra.framework_stubs import RequestContext, upload as make_upload

# ── 被测对象（新模块；红相阶段 import 失败即红）──────────────────────

from astrbot_plugin_quillplus.quill.core.paths import (
    _WINDOWS_RESERVED,
    resolve_safe,
    sanitize_name,
)
from astrbot_plugin_quillplus.interfaces.web.upload import (
    BINARY_EXTS,
    DEFAULT_LIMIT,
    UploadError,
    UploadPayload,
    UploadTooLarge,
    read_upload,
)
import astrbot_plugin_quillplus.quill.core.paths as paths_module
import astrbot_plugin_quillplus.web_routes as web_routes_module
from astrbot_plugin_quillplus.web_routes import PLUGIN_NAME, QuillRoutes


# ── 测试假件 ──────────────────────────────────────────────────────


class _RouteRecorder:
    """register_web_api 假件：记录 (path, handler, methods)。"""

    def __init__(self) -> None:
        self.registered: list[tuple[str, object, tuple[str, ...]]] = []

    def register_web_api(self, path, handler, methods, description):
        self.registered.append((path, handler, tuple(methods)))


class _PersonaManagerStub:
    """persona_manager 假件：记录调用，返回可预测结果。"""

    def __init__(self) -> None:
        self.saved: tuple[str, bytes] | None = None
        self.parse_calls: list[tuple[bytes, bool]] = []
        self.clip_text: str | None = None

    async def save_avatar(self, filename, data):
        self.saved = (filename, data)
        return f"quill_avatars/{filename}"

    def parse_v2_card(self, data, is_image=False):
        self.parse_calls.append((data, is_image))
        return {"name": "Layla"}

    async def create_persona(self, data):
        return {"id": "p1", "name": data.get("name", ""), "message": "Persona created"}

    def parse_clipboard_text(self, text):
        self.clip_text = text
        return {"name": text.splitlines()[0][:10] if text else ""}

    async def read_avatar(self, filename):
        if filename == "me.png":
            return b"PNGDATA"
        return None


class _WbManagerStub:
    def __init__(self) -> None:
        self.import_calls: list[tuple[str, str]] = []

    def import_from_st(self, path, name):
        self.import_calls.append((path, name))
        return True


class _PluginStub:
    def __init__(self, paths: dict) -> None:
        self.paths = paths


@pytest.fixture
def bind_stub_request(monkeypatch):
    """把 web_routes 模块级 request 换成 stub 代理（双模式一致）。"""
    proxy = framework_stubs._RequestProxy()
    monkeypatch.setattr(web_routes_module, "request", proxy)
    return proxy


def _make_routes(persona_manager=None, wb_manager=None, plugin=None, rag=None):
    ctx = _RouteRecorder()
    routes = QuillRoutes(
        wr_manager=None,
        wb_manager=wb_manager,
        context=ctx,
        config=None,
        rag_components=rag or {},
        plugin=plugin,
        persona_manager=persona_manager,
    )
    return routes, ctx


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unwrap(resp):
    """双模式取 (负载, 状态码)。

    stub 环境：``json_response`` → ``{"json": data}`` dict，
    ``error_response`` → ``{"status","message","status_code"}`` dict；
    真机环境（ASTRBOT_APP）：两者都返回 Starlette ``JSONResponse``
    （body 为 JSON 字节，状态码在 ``status_code`` 属性；error 信封为
    ``{"status":"error","message":...}``）。
    """
    if isinstance(resp, dict):
        if set(resp) == {"json"}:
            return resp["json"], 200
        return resp, resp.get("status_code", 400)
    return json.loads(resp.body), getattr(resp, "status_code", 200)


# ── 1. read_upload：三通道消歧 ────────────────────────────────────


async def test_read_upload_multipart_file_channel():
    proxy = framework_stubs._RequestProxy()
    with RequestContext(files={"file": make_upload("a.txt", b"hello", "text/plain")}):
        payload = await read_upload(proxy)
    assert isinstance(payload, UploadPayload)
    assert payload.data == b"hello"
    assert payload.filename == "a.txt"
    assert payload.content_type == "text/plain"


async def test_read_upload_multipart_key_candidates():
    """默认 keys 按 file→upload→card→avatar 逐个探测。"""
    proxy = framework_stubs._RequestProxy()
    with RequestContext(files={"card": make_upload("card.png", b"PNG")}):
        payload = await read_upload(proxy)
    assert payload is not None
    assert payload.filename == "card.png"

    proxy = framework_stubs._RequestProxy()
    with RequestContext(files={"avatar": make_upload("me.png", b"PNG")}):
        payload = await read_upload(proxy)
    assert payload is not None
    assert payload.filename == "me.png"


async def test_read_upload_base64_json_channel():
    """面板 bridge 通道：JSON body 携带 b64_data（BASELINE §6.2 传输契约）。"""
    proxy = framework_stubs._RequestProxy()
    with RequestContext(json={"filename": "x.png", "b64_data": _b64(b"JSONDATA")}):
        payload = await read_upload(proxy, default_filename="card.png")
    assert payload is not None
    assert payload.data == b"JSONDATA"
    assert payload.filename == "x.png"


async def test_read_upload_base64_form_channel():
    """沙箱 FormData workaround：base64 塞在表单文本字段里。"""
    proxy = framework_stubs._RequestProxy()
    with RequestContext(form={"filename": "f.bin", "b64_data": _b64(b"FORMDATA")}):
        payload = await read_upload(proxy)
    assert payload is not None
    assert payload.data == b"FORMDATA"
    assert payload.filename == "f.bin"


async def test_read_upload_file_channel_wins_over_base64():
    """双通道同时存在时文件通道优先。"""
    proxy = framework_stubs._RequestProxy()
    with RequestContext(
        files={"file": make_upload("real.txt", b"FILE")},
        json={"b64_data": _b64(b"BASE64")},
    ):
        payload = await read_upload(proxy)
    assert payload is not None
    assert payload.data == b"FILE"


async def test_read_upload_no_upload_returns_none():
    proxy = framework_stubs._RequestProxy()
    with RequestContext(json={"other": "字段"}):
        assert await read_upload(proxy) is None
    with RequestContext():
        assert await read_upload(proxy) is None


async def test_read_upload_over_limit_raises():
    proxy = framework_stubs._RequestProxy()
    big = b"x" * (DEFAULT_LIMIT + 1)
    with RequestContext(files={"file": make_upload("big.bin", big)}):
        with pytest.raises(UploadTooLarge):
            await read_upload(proxy)


async def test_read_upload_limit_none_unlimited():
    proxy = framework_stubs._RequestProxy()
    big = b"x" * (DEFAULT_LIMIT + 1)
    with RequestContext(files={"file": make_upload("big.bin", big)}):
        payload = await read_upload(proxy, limit=None)
    assert payload is not None
    assert len(payload.data) == DEFAULT_LIMIT + 1


async def test_read_upload_base64_over_limit_raises():
    proxy = framework_stubs._RequestProxy()
    big = b"x" * (5 * 1024 * 1024 + 1)
    with RequestContext(json={"b64_data": _b64(big)}):
        with pytest.raises(UploadTooLarge):
            await read_upload(proxy, limit=5 * 1024 * 1024)


async def test_read_upload_base64_decode_failure_raises():
    proxy = framework_stubs._RequestProxy()
    with RequestContext(json={"b64_data": "!!not-base64!!"}):
        with pytest.raises(UploadError):
            await read_upload(proxy)


async def test_read_upload_filename_field_resolution():
    """rag 通道的文件名字段是 source（历史契约）。"""
    proxy = framework_stubs._RequestProxy()
    with RequestContext(json={"source": "docs.txt", "b64_data": _b64(b"data")}):
        payload = await read_upload(proxy, filename_field="source", default_filename="unknown")
    assert payload is not None
    assert payload.filename == "docs.txt"

    # 字段缺失 → default_filename 兜底
    with RequestContext(json={"b64_data": _b64(b"data")}):
        payload = await read_upload(proxy, filename_field="source", default_filename="unknown")
    assert payload is not None
    assert payload.filename == "unknown"


async def test_read_upload_collects_text_fields():
    """multipart 通道的附加文本字段（如 rag 的 source）随 payload 带出。"""
    proxy = framework_stubs._RequestProxy()
    with RequestContext(
        files={"file": make_upload("a.txt", b"data")},
        form={"source": "我的文档.txt"},
    ):
        payload = await read_upload(proxy)
    assert payload is not None
    assert payload.fields.get("source") == "我的文档.txt"


async def test_read_upload_default_filename_fallback():
    proxy = framework_stubs._RequestProxy()
    with RequestContext(files={"file": make_upload("", b"data")}):
        payload = await read_upload(proxy, default_filename="avatar.png")
    assert payload is not None
    assert payload.filename == "avatar.png"


# ── 2. sanitize_name ─────────────────────────────────────────────


def test_sanitize_name_passthrough_normal():
    assert sanitize_name("avatar_1737.png") == "avatar_1737.png"
    assert sanitize_name("我的角色卡 v2.png") == "我的角色卡 v2.png"


def test_sanitize_name_windows_reserved_bare():
    for name in ("CON", "NUL", "AUX", "PRN", "COM1", "com5", "LPT9"):
        assert sanitize_name(name) == f"_{name}"


def test_sanitize_name_windows_reserved_with_ext():
    assert sanitize_name("con.txt") == "_con.txt"
    assert sanitize_name("NUL.png") == "_NUL.png"
    assert _WINDOWS_RESERVED >= {"CON", "PRN", "AUX", "NUL", "COM1", "LPT9"}


def test_sanitize_name_control_chars():
    # 首尾的 \x1c-\x1f 属于 str.strip() 的空白集合，先行剥离；\x00/\x01 在串中替换为 _
    assert sanitize_name("head\x00tail\x1f") == "head_tail"
    assert sanitize_name("a\x01b") == "a_b"


def test_sanitize_name_separators_and_unsafe():
    assert sanitize_name("a/b\\c") == "a_b_c"
    assert sanitize_name('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"
    assert sanitize_name("my-card_01.png") == "my-card_01.png"  # 连字符合法
    assert sanitize_name("head\x01tail") == "head_tail"  # 控制字符整段替换
    # ../etc/passwd → 分隔符被替换、首部悬挂的点被剥离，落成单段文件名，
    # 天然免疫目录穿越（strip(" .") 行为与参考版一致）
    assert sanitize_name("../etc/passwd") == "_etc_passwd"
    assert sanitize_name("..\\etc\\passwd") == "_etc_passwd"
    assert "/" not in sanitize_name("../etc/passwd")
    assert "\\" not in sanitize_name("..\\etc\\passwd")


def test_sanitize_name_dot_dot_alone_falls_back():
    assert sanitize_name("..") == "unnamed"
    assert sanitize_name(".") == "unnamed"
    assert sanitize_name("..", fallback="card") == "card"


def test_sanitize_name_truncation():
    assert sanitize_name("x" * 200) == "x" * 120
    # 截断后尾部悬挂的 . / 空格再剥离
    assert sanitize_name("a" * 119 + "." * 10) == "a" * 119


def test_sanitize_name_fallback():
    assert sanitize_name("") == "unnamed"
    assert sanitize_name(None) == "unnamed"
    assert sanitize_name("   ") == "unnamed"
    assert sanitize_name("", fallback="card.png") == "card.png"


# ── 3. resolve_safe ──────────────────────────────────────────────


def test_resolve_safe_normal_name(tmp_path):
    assert resolve_safe(tmp_path, "a.txt") == (tmp_path / "a.txt").resolve()


def test_resolve_safe_appends_suffix(tmp_path):
    assert resolve_safe(tmp_path, "report", suffix=".json") == (tmp_path / "report.json").resolve()


def test_resolve_safe_suffix_already_present(tmp_path):
    # 后缀已存在（大小写不敏感）时不重复追加
    assert resolve_safe(tmp_path, "report.JSON", suffix=".json") == (tmp_path / "report.JSON").resolve()


def test_resolve_safe_separators_become_single_segment(tmp_path):
    # 分隔符被 sanitize_name 替换后必然落在 base 之内（免疫穿越；
    # 首部悬挂的点被 strip 剥离，见 sanitize_name 用例）
    assert resolve_safe(tmp_path, "sub/child.txt") == (tmp_path / "sub_child.txt").resolve()
    assert resolve_safe(tmp_path, "..\\evil") == (tmp_path / "_evil").resolve()


def test_resolve_safe_reserved_name_prefixed(tmp_path):
    assert resolve_safe(tmp_path, "CON.png", suffix=".png") == (tmp_path / "_CON.png").resolve()


def test_resolve_safe_escape_guard_fires(tmp_path, monkeypatch):
    """纵深防御：即便 sanitize_name 被改坏（返回 ..），relative_to 断言仍拦截。"""
    monkeypatch.setattr(paths_module, "sanitize_name", lambda name, **kw: "..")
    with pytest.raises(ValueError):
        resolve_safe(tmp_path, "anything")


def test_resolve_safe_escape_guard_absolute(tmp_path, monkeypatch):
    """纵深防御：盘符绝对路径替换 base 的 join 语义也被断言拦截。"""
    monkeypatch.setattr(
        paths_module, "sanitize_name", lambda name, **kw: "C:/Windows/evil"
    )
    with pytest.raises(ValueError):
        resolve_safe(tmp_path, "anything")


# ── 4. 路由层：别名注册与行为等价 ─────────────────────────────────

#: M3.1 收敛的四组双份 handler（base64 路由别名 → 正身路由）。
_ALIAS_PAIRS = [
    "/rag/upload",
    "/upload_avatar",
    "/persona/import",
    "/persona/import_text",
]


def test_register_all_aliases_share_handler():
    routes, ctx = _make_routes()
    routes.register_all()
    by_path = {path: handler for path, handler, _ in ctx.registered}
    for route in _ALIAS_PAIRS:
        main = by_path[f"/{PLUGIN_NAME}{route}"]
        alias = by_path[f"/{PLUGIN_NAME}{route}_base64"]
        # 绑定方法每次属性访问都是新对象，必须比 __func__
        assert main.__func__ is alias.__func__, f"{route}_base64 未与正身共享 handler"
    # 既有历史别名（P3-3）保持共享
    assert (
        by_path[f"/{PLUGIN_NAME}/wb/delete"].__func__
        is by_path[f"/{PLUGIN_NAME}/wb/delete_book"].__func__
    )


async def test_upload_avatar_multipart_and_base64_equivalent(bind_stub_request):
    """两通道走同一 handler，成功响应形态一致。"""
    pm = _PersonaManagerStub()
    routes, _ = _make_routes(persona_manager=pm)

    with RequestContext(files={"file": make_upload("me.png", b"PNGDATA", "image/png")}):
        resp_file = await routes.upload_avatar()
    body, status = _unwrap(resp_file)
    assert status == 200
    assert body["message"] == "Avatar uploaded"
    assert body["url"] == f"/{PLUGIN_NAME}/avatar/me.png"
    assert pm.saved == ("me.png", b"PNGDATA")

    pm.saved = None
    with RequestContext(json={"filename": "me.jpg", "b64_data": _b64(b"JPGDATA")}):
        resp_b64 = await routes.upload_avatar()
    body, status = _unwrap(resp_b64)
    assert status == 200
    assert body["message"] == "Avatar uploaded"
    assert pm.saved == ("me.jpg", b"JPGDATA")


async def test_upload_avatar_size_limit(bind_stub_request):
    routes, _ = _make_routes(persona_manager=_PersonaManagerStub())
    big = b"x" * (5 * 1024 * 1024 + 1)
    with RequestContext(files={"file": make_upload("big.png", big)}):
        resp = await routes.upload_avatar()
    body, status = _unwrap(resp)
    assert status == 413
    assert "5MB" in body["message"]


async def test_persona_import_accepts_webp_union_whitelist(bind_stub_request):
    """白名单统一取并集（BASELINE §8.1 决策）：multipart 侧历史上拒绝的
    .webp 现在两通道一致接受。"""
    pm = _PersonaManagerStub()
    routes, _ = _make_routes(persona_manager=pm)
    with RequestContext(json={"filename": "card.webp", "b64_data": _b64(b"WEBP!")}):
        resp = await routes.persona_import()
    body, status = _unwrap(resp)
    assert status == 200
    assert body["message"] == "Persona created"
    assert pm.parse_calls == [(b"WEBP!", True)]
    assert pm.saved == ("Layla.webp", b"WEBP!")


async def test_persona_import_rejects_unknown_ext(bind_stub_request):
    routes, _ = _make_routes(persona_manager=_PersonaManagerStub())
    with RequestContext(json={"filename": "card.exe", "b64_data": _b64(b"MZ")}):
        resp = await routes.persona_import()
    body, status = _unwrap(resp)
    assert status == 400
    assert "不支持的文件格式" in body["message"]


async def test_persona_import_multipart_json_card(bind_stub_request):
    pm = _PersonaManagerStub()
    routes, _ = _make_routes(persona_manager=pm)
    with RequestContext(files={"file": make_upload("card.json", b'{"spec": "chara_card_v2"}')}):
        resp = await routes.persona_import()
    body, status = _unwrap(resp)
    assert status == 200
    assert pm.parse_calls == [(b'{"spec": "chara_card_v2"}', False)]
    assert pm.saved is None  # JSON 卡不存头像


async def test_persona_import_base64_decode_failure(bind_stub_request):
    routes, _ = _make_routes(persona_manager=_PersonaManagerStub())
    with RequestContext(json={"filename": "card.png", "b64_data": "!!bad!!"}):
        resp = await routes.persona_import()
    body, status = _unwrap(resp)
    assert status == 400
    assert "Base64 解码失败" in body["message"]


async def test_rag_upload_rejects_binary_ext(bind_stub_request):
    routes, _ = _make_routes()
    with RequestContext(files={"file": make_upload("doc.pdf", b"%PDF-1.4")}):
        resp = await routes.rag_upload()
    body, status = _unwrap(resp)
    assert status == 400
    assert "不支持的文件格式" in body["message"]
    assert ".pdf" in body["message"]


async def test_rag_upload_no_file(bind_stub_request):
    routes, _ = _make_routes()
    with RequestContext():
        resp = await routes.rag_upload()
    body, status = _unwrap(resp)
    assert status == 400


async def test_rag_upload_over_limit_413(bind_stub_request):
    routes, _ = _make_routes()
    big = b"x" * (50 * 1024 * 1024 + 1)
    with RequestContext(files={"file": make_upload("doc.txt", big)}):
        resp = await routes.rag_upload()
    body, status = _unwrap(resp)
    assert status == 413
    assert "50MB" in body["message"]


async def test_rag_upload_text_reaches_rag_init_gate(bind_stub_request):
    """合法扩展名通过黑名单检查后走到 RAG 初始化闸门（此桩里未初始化 → 500）。"""
    routes, _ = _make_routes()
    with RequestContext(
        files={"file": make_upload("doc.txt", "正文".encode("utf-8"))},
        form={"source": "我的文档"},
    ):
        resp = await routes.rag_upload()
    body, status = _unwrap(resp)
    assert status == 500
    assert body["message"] == "RAG 未初始化"


async def test_persona_import_text_channel(bind_stub_request):
    pm = _PersonaManagerStub()
    routes, _ = _make_routes(persona_manager=pm)
    with RequestContext(json={"text": "名字：Layla\n描述：测试"}):
        resp = await routes.persona_import_text()
    body, status = _unwrap(resp)
    assert status == 200
    assert pm.clip_text == "名字：Layla\n描述：测试"


async def test_persona_import_text_b64_channel(bind_stub_request):
    """b64_text 绕沙箱通道收敛进同一 handler。"""
    pm = _PersonaManagerStub()
    routes, _ = _make_routes(persona_manager=pm)
    raw = "名字：Layla\n描述：测试".encode("utf-8")
    with RequestContext(json={"b64_text": _b64(raw)}):
        resp = await routes.persona_import_text()
    body, status = _unwrap(resp)
    assert status == 200
    assert pm.clip_text == "名字：Layla\n描述：测试"


async def test_persona_import_text_b64_invalid(bind_stub_request):
    routes, _ = _make_routes(persona_manager=_PersonaManagerStub())
    with RequestContext(json={"b64_text": "!!bad!!"}):
        resp = await routes.persona_import_text()
    body, status = _unwrap(resp)
    assert status == 400
    assert "Base64 解码失败" in body["message"]


async def test_persona_import_text_missing(bind_stub_request):
    routes, _ = _make_routes(persona_manager=_PersonaManagerStub())
    with RequestContext(json={}):
        resp = await routes.persona_import_text()
    body, status = _unwrap(resp)
    assert status == 400


async def test_serve_avatar_rejects_traversal(bind_stub_request):
    routes, _ = _make_routes(persona_manager=_PersonaManagerStub())
    for bad in ("../evil.png", "a/b.png", "a\\b.png", "..", "con.png", ""):
        resp = await routes.serve_avatar(bad)
        body, status = _unwrap(resp)
        assert status == 400, f"{bad!r} 应被拒绝"
        assert body["message"] == "无效的文件名"


async def test_serve_avatar_serves_existing_file(bind_stub_request):
    routes, _ = _make_routes(persona_manager=_PersonaManagerStub())
    resp = await routes.serve_avatar("me.png")
    assert getattr(resp, "body", None) == b"PNGDATA"
    assert resp.media_type == "image/png"


async def test_serve_avatar_missing_404(bind_stub_request):
    routes, _ = _make_routes(persona_manager=_PersonaManagerStub())
    resp = await routes.serve_avatar("ghost.png")
    body, status = _unwrap(resp)
    assert status == 404


async def test_wb_import_st_uses_unified_channel(bind_stub_request, tmp_path):
    """单通道 multipart 导入同样走 read_upload（imports 暂存 + 导入不变）。"""
    wb = _WbManagerStub()
    plugin = _PluginStub({"imports_dir": str(tmp_path)})
    routes, _ = _make_routes(wb_manager=wb, plugin=plugin)
    with RequestContext(
        files={"file": make_upload("lore.json", b'{"entries": []}')},
        form={"name": "测试书"},
    ):
        resp = await routes.wb_import_st()
    body, status = _unwrap(resp)
    assert status == 200
    assert wb.import_calls and wb.import_calls[0][1] == "测试书"
    saved = tmp_path / "测试书.json"
    assert saved.is_file()
    assert saved.read_bytes() == b'{"entries": []}'


async def test_wb_import_st_missing_name(bind_stub_request, tmp_path):
    plugin = _PluginStub({"imports_dir": str(tmp_path)})
    routes, _ = _make_routes(wb_manager=_WbManagerStub(), plugin=plugin)
    with RequestContext(files={"file": make_upload("lore.json", b"{}")}, form={}):
        resp = await routes.wb_import_st()
    body, status = _unwrap(resp)
    assert status == 400
    assert "缺少世界书名称" in body["message"]


def test_binary_ext_blacklist_single_source():
    """黑名单收敛为一处常量：upload.py 定义一次，web_routes 不再抄写字面集合。"""
    assert BINARY_EXTS >= {".pdf", ".doc", ".zip", ".png", ".jpg"}
    wr_source = Path(web_routes_module.__file__).read_text(encoding="utf-8")
    assert ".pptx" not in wr_source, "web_routes 不应再抄写二进制黑名单字面集合"
    upload_source = Path(
        web_routes_module.__file__
    ).parent.joinpath("interfaces", "web", "upload.py").read_text(encoding="utf-8")
    assert upload_source.count(".pptx") == 1
