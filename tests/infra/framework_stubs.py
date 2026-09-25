# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AstrBot 适配层假件（测试专用，v5.3.0 M1 基建）。

形状契约
--------
本 stub 的形状以**真机 AstrBot 4.28.0** 为唯一依据（见
``docs/v5.3/BASELINE.md`` §1.1，证据：
``astrbot/api/web.py`` PluginRequest L165-239）：

============================  ==========================
真机 ``astrbot.api.web``      本 stub
============================  ==========================
``request.json(d=None)``      ``async def json(d=None)``
``request.form()``           ``async def form()``
``request.files()``          ``async def files()``
``request.body()``           ``async def body()``
``request.query``            同步 property
``request.method`` 等         同步 property
``PluginUploadFile.read``    ``async def read``
============================  ==========================

**历史教训（重构版翻车坑）**：旧测试基建把 ``json/form/files`` 建成同步
属性，业务代码随之写成同步读取（``request.json()`` 不 await、甚至裸属性
``request.json``），三道测试防线全绿，真机上所有写操作恒拿空数据——
真实框架里它们是 ``async def``，同步读取拿到的是 coroutine/未执行结果。
因此本文件顶部带形状自检（``assert_shape_contract``），由
``test_framework_stub_shape.py`` 在 CI 中强制执行：json/form/files/body
**必须**是协程函数，query/method **必须**不是。

用法
----
conftest.py 在收集前调用 :func:`install`，把假 ``astrbot.api.web`` /
``starlette.responses`` 注入 ``sys.modules``（仅当缺失时；环境里已有
真实 AstrBot 则绝不覆盖，但 CI 明确要求 stub 环境——见 ASTRBOT_TEST_STRICT）。
之后 ``from astrbot.api.web import request`` 与真机语义一致。
"""

from __future__ import annotations

import contextvars
import inspect
import json as json_module
import sys
import types
from typing import Any, Mapping

#: 当前请求上下文。用 contextvars 而不是全局变量：与真机
#: PluginRequestProxy 的绑定语义一致（bind_request_context），并发不串味。
_CURRENT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "quillplus_test_request", default=None
)


class PluginMultiDict(dict):
    """多值字典，兼容真机 astrbot.api.web.PluginMultiDict 接口。

    真机：get(key, default, type) / getlist / keys / values / items。
    """

    def getlist(self, key: str) -> list[Any]:
        value = self.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


class PluginUploadFile:
    """上传文件假件。真机 read/save/write/seek/close 均为 async。"""

    def __init__(self, filename: str, data: bytes, content_type: str = "") -> None:
        self.name = filename
        self.filename = filename
        self.content_type = content_type or "application/octet-stream"
        self._data = data
        self._read = False

    async def read(self) -> bytes:
        self._read = True
        return self._data

    async def save(self, target: Any) -> None:  # pragma: no cover - 兼容用
        from pathlib import Path

        Path(target).write_bytes(self._data)


class _RequestProxy:
    """复刻真机 astrbot.api.web.request（PluginRequestProxy）。

    同步侧（method/query/…）是构造时固化的 property；异步侧
    （json/form/files/body）是 async 方法——与真机一一对应。
    """

    # ── 同步侧（property）─────────────────────────────────────

    @property
    def query(self) -> PluginMultiDict:
        state = self._state()
        return PluginMultiDict(state.get("query") or {})

    @property
    def method(self) -> str:
        return str(self._state().get("method") or "POST")

    @property
    def path(self) -> str:
        return str(self._state().get("path") or "")

    @property
    def headers(self) -> dict:
        return dict(self._state().get("headers") or {})

    @property
    def content_type(self) -> str:
        return str(self._state().get("content_type") or "")

    # ── 异步侧（async def，真机契约）──────────────────────────

    async def json(self, default: Any = None) -> Any:
        state = self._state()
        if "json" not in state:
            return {} if default is None else default
        value = state["json"]
        # 复刻真机：json 字段是已解析对象或原始 bytes/str
        if isinstance(value, (bytes, bytearray)):
            try:
                return json_module.loads(value.decode("utf-8"))
            except Exception:
                return {} if default is None else default
        if isinstance(value, str):
            try:
                return json_module.loads(value)
            except Exception:
                return {} if default is None else default
        return value

    async def form(self) -> PluginMultiDict:
        return PluginMultiDict(self._state().get("form") or {})

    async def files(self) -> PluginMultiDict:
        return PluginMultiDict(self._state().get("files") or {})

    async def body(self) -> bytes:
        raw = self._state().get("body")
        if raw is None:
            payload = self._state().get("json")
            if payload is None:
                return b""
            return json_module.dumps(payload, ensure_ascii=False).encode("utf-8")
        return raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8")

    # ── 测试侧控制 ────────────────────────────────────────────

    def set(self, **kwargs: Any) -> contextvars.Token:
        """直接绑定请求上下文（供非 with 用法的测试使用）。"""
        return _CURRENT.set(kwargs)

    def reset(self, token: contextvars.Token) -> None:
        _CURRENT.reset(token)

    def _state(self) -> dict[str, Any]:
        state = _CURRENT.get()
        return state if state is not None else {}


class RequestContext:
    """``with RequestContext(json={...}):`` —— 绑定一次请求上下文。

    kwargs 可含：``json``（dict/str/bytes）、``form``、``files``、``body``、
    ``query``、``method``、``path``、``headers``、``content_type``。
    """

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "RequestContext":
        self._token = _CURRENT.set(self._kwargs)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)


class Response:
    """假 starlette Response，让二进制下载分支走真实路径。"""

    def __init__(
        self,
        content: bytes = b"",
        media_type: str = "",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.body = content
        self.media_type = media_type
        self.headers = dict(headers or {})


def install() -> None:
    """把假 astrbot 模块树装进 sys.modules（仅装缺失的）。"""
    if "astrbot" in sys.modules and hasattr(sys.modules["astrbot"], "api"):
        # 环境里已有真实 astrbot（ASTRBOT_APP 指向真机）——绝不用 stub 覆盖
        return

    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))

    # astrbot.api.logger：插件全量经 astrbot.api 导入 logger（上架合规硬约束）
    import logging

    api.logger = logging.getLogger("quillplus-test")

    # astrbot.api.web —— json/form/files/body 全部 async（见模块 docstring）
    web = types.ModuleType("astrbot.api.web")
    web.request = _RequestProxy()  # type: ignore[attr-defined]
    web.PluginUploadFile = PluginUploadFile  # type: ignore[attr-defined]
    web.PluginMultiDict = PluginMultiDict  # type: ignore[attr-defined]
    web.json_response = lambda data=None, **kw: {"json": data}  # type: ignore[attr-defined]
    web.error_response = lambda msg="", status_code=400, **kw: {  # type: ignore[attr-defined]
        "status": "error",
        "message": msg,
        "status_code": status_code,
    }
    sys.modules["astrbot.api.web"] = web
    api.web = web  # type: ignore[attr-defined]

    # astrbot.api.star —— @register / Star / Context
    star = types.ModuleType("astrbot.api.star")
    star.Context = object  # type: ignore[attr-defined]
    star.Star = object  # type: ignore[attr-defined]
    star.register = lambda *a, **k: (lambda cls: cls)  # type: ignore[attr-defined]
    sys.modules["astrbot.api.star"] = star
    api.star = star  # type: ignore[attr-defined]

    # astrbot.api.event —— filter 命名空间 + AstrMessageEvent
    event = types.ModuleType("astrbot.api.event")
    event.filter = types.SimpleNamespace(  # type: ignore[attr-defined]
        **{
            name: (lambda *a, **k: (lambda fn: fn))
            for name in (
                "command",
                "command_alias",
                "event_message_type",
                "permission_type",
                "on_waiting_llm_request",
                "on_llm_request",
                "on_llm_response",
                "on_llm_tool_respond",
                "on_using_llm_tool",
                "on_decorating_result",
                "on_astrbot_loaded",
            )
        }
    )
    event.AstrMessageEvent = object  # type: ignore[attr-defined]
    sys.modules["astrbot.api.event"] = event
    api.event = event  # type: ignore[attr-defined]

    # astrbot.api.provider
    provider = types.ModuleType("astrbot.api.provider")
    provider.ProviderRequest = object  # type: ignore[attr-defined]
    provider.LLMResponse = object  # type: ignore[attr-defined]
    sys.modules["astrbot.api.provider"] = provider
    api.provider = provider  # type: ignore[attr-defined]

    # astrbot.core.* —— FunctionTool / register_command / GreedyStr
    core = sys.modules.setdefault("astrbot.core", types.ModuleType("astrbot.core"))
    agent = types.ModuleType("astrbot.core.agent")
    tool_mod = types.ModuleType("astrbot.core.agent.tool")
    tool_mod.FunctionTool = object  # type: ignore[attr-defined]
    core_star = types.ModuleType("astrbot.core.star")
    reg = types.ModuleType("astrbot.core.star.register")
    reg.register_command = lambda *a, **k: (lambda fn: fn)  # type: ignore[attr-defined]
    filt = types.ModuleType("astrbot.core.star.filter")
    cmd = types.ModuleType("astrbot.core.star.filter.command")
    cmd.GreedyStr = str  # type: ignore[attr-defined]

    # astrbot.core.message / astrbot.core.platform —— commands.py 导入面。
    # 真机形状（4.28.0 实测）：MessageEventResult 是流式链 API——
    # .message(str) 追加 Plain 到 chain 并返回 self，.use_t2i(bool) 返回 self，
    # .get_plain_text() 拼接 chain 内 Plain 文本；components.Plain 持 .text。
    message = types.ModuleType("astrbot.core.message")
    mer = types.ModuleType("astrbot.core.message.message_event_result")
    components = types.ModuleType("astrbot.core.message.components")

    class _StubPlain:
        def __init__(self, text: str = "", **kwargs: Any) -> None:
            self.text = text

    class _StubMessageChain(list):
        pass

    class _StubMessageEventResult:
        """流式链桩：复刻 .message/.use_t2i/.get_plain_text/.chain。"""

        def __init__(self, chain: list | None = None, **kwargs: Any) -> None:
            self.chain = list(chain) if chain is not None else []
            self.use_t2i_flag: bool | None = None

        def message(self, text: str) -> "_StubMessageEventResult":
            self.chain.append(_StubPlain(text))
            return self

        def use_t2i(self, flag: bool) -> "_StubMessageEventResult":
            self.use_t2i_flag = flag
            return self

        def get_plain_text(self) -> str:
            return "".join(getattr(c, "text", "") for c in self.chain)

    mer.MessageEventResult = _StubMessageEventResult  # type: ignore[attr-defined]
    components.Plain = _StubPlain  # type: ignore[attr-defined]
    platform = types.ModuleType("astrbot.core.platform")
    mt_core = types.ModuleType("astrbot.core.platform.message_type")
    mt_core.MessageType = types.SimpleNamespace(  # type: ignore[attr-defined]
        FRIEND_MESSAGE="friend", GROUP_MESSAGE="group", OTHER_MESSAGE="other"
    )

    astrbot.core = core  # type: ignore[attr-defined]
    astrbot.api = api  # type: ignore[attr-defined]
    core.agent = agent
    agent.tool = tool_mod
    core.star = core_star
    core_star.register = reg
    core_star.filter = filt
    filt.command = cmd
    core.message = message
    message.message_event_result = mer
    message.components = components
    core.platform = platform
    platform.message_type = mt_core
    for name, mod in (
        ("astrbot.core", core),
        ("astrbot.core.agent", agent),
        ("astrbot.core.agent.tool", tool_mod),
        ("astrbot.core.star", core_star),
        ("astrbot.core.star.register", reg),
        ("astrbot.core.star.filter", filt),
        ("astrbot.core.star.filter.command", cmd),
        ("astrbot.core.message", message),
        ("astrbot.core.message.message_event_result", mer),
        ("astrbot.core.message.components", components),
        ("astrbot.core.platform", platform),
        ("astrbot.core.platform.message_type", mt_core),
    ):
        sys.modules[name] = mod

    # starlette.responses —— 仅在真包缺失时提供假件
    if "starlette.responses" not in sys.modules:
        try:
            import starlette.responses  # noqa: F401
        except Exception:
            starlette = sys.modules.setdefault(
                "starlette", types.ModuleType("starlette")
            )
            responses = types.ModuleType("starlette.responses")
            responses.Response = Response  # type: ignore[attr-defined]
            sys.modules["starlette.responses"] = responses
            starlette.responses = responses  # type: ignore[attr-defined]


def upload(filename: str, data: bytes, content_type: str = "") -> PluginUploadFile:
    """构造上传文件假件。"""
    return PluginUploadFile(filename, data, content_type)


# ── 形状自检（test_framework_stub_shape.py 消费）────────────────


#: 必须是协程函数的成员（真机为 async def）
ASYNC_MEMBERS = ("json", "form", "files", "body")

#: 必须是普通 property 的成员（真机构造时固化）
SYNC_PROPERTIES = ("query", "method", "path", "headers", "content_type")


def assert_shape_contract() -> None:
    """断言 stub 形状与真机 AstrBot 4.28.0 一致（防 stub 再漂移）。

    真机依据：astrbot/api/web.py PluginRequest/PluginRequestProxy
    （BASELINE.md §1.1）。任何人在 stub 里把 json/form/files 改成同步
    属性都会在这里失败——那正是重构版全面板写操作失效的根因。
    """
    install()
    from astrbot.api import web  # noqa: F401  (确保已安装)

    req = sys.modules["astrbot.api.web"].request
    for name in ASYNC_MEMBERS:
        attr = getattr(req, name, None)
        assert attr is not None, f"stub request.{name} 缺失"
        assert inspect.iscoroutinefunction(attr), (
            f"stub request.{name} 必须是 async def（真机契约），"
            f"当前是 {type(attr)}——同步形状正是重构版翻车的根因"
        )
        if name == "json":
            sig = inspect.signature(attr)
            assert "default" in sig.parameters, (
                "stub request.json 必须带 default kwarg（真机签名 json(default=None)）"
            )
    for name in SYNC_PROPERTIES:
        attr = getattr(type(req), name, None)
        assert isinstance(attr, property), (
            f"stub request.{name} 必须是同步 property（真机契约），当前是 {type(attr)}"
        )
    # 上传文件 read 也必须是 async
    assert inspect.iscoroutinefunction(PluginUploadFile.read), (
        "PluginUploadFile.read 必须是 async def（真机契约）"
    )


__all__ = [
    "RequestContext",
    "Response",
    "PluginMultiDict",
    "PluginUploadFile",
    "assert_shape_contract",
    "install",
    "upload",
]
