# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 调用边界观测；只包自己的请求，不改宿主 Provider 或全局钩子。"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import time
from contextlib import contextmanager
from uuid import uuid4
from urllib.parse import urlsplit

from astrbot.api import logger

from .scopes import event_turn
from .prompt_cache import selected_provider
from ..quill.services.scope import ScopeResolver, bind_turn
from ..quill.services.usage import field, normalize_usage, parse_prices, price_usage, reported_cost

_identity = contextvars.ContextVar("quill_usage_identity", default=None)


class UsageRecorder:
    def __init__(self, context, store, settings, session_resolver=None):
        self.context = context
        self.session_resolver = session_resolver or ScopeResolver().from_session_key
        self.store = store
        self.settings = settings
        self._ready = False
        self._queue = asyncio.Queue(maxsize=256)
        self._writer = None
        self.dropped_records = 0
        self._saved_gaps = 0
        self._gap_lock = asyncio.Lock()
        self.error = None

    @property
    def ready(self):
        return self._ready and self.store.available and self._writer is not None and not self._writer.done()

    def _enabled(self):
        return bool(self.ready and self.settings().usage_enabled)

    async def initialize(self):
        try:
            await self.store.initialize()
            diagnostics = await self.store.diagnostics()
            self.dropped_records = diagnostics['dropped_records']
            self._saved_gaps = self.dropped_records
            self._ready = True
            self.error = None
            if self._writer is None or self._writer.done():
                self._writer = asyncio.create_task(self._write_loop(), name="quill-usage-writer")
        except Exception as exc:
            self.error = type(exc).__name__ + ":" + str(exc)[:100]
            logger.warning("[Quill Usage] 台账不可用（%s），对话继续", self.error)

    async def identity_for(self, event, persona_id=None, *, new_request=False):
        existing = event.get_extra("_quill_usage_identity")
        if isinstance(existing, dict) and not new_request:
            return existing
        umo = str(event.unified_msg_origin or "")
        identity = {"turn_id": uuid4().hex, "turn_kind": "user", "source_umo": umo, "scope_id": umo,
                    "scope_revision": 0, "persona_id": str(persona_id or ""),
                    "started_at": time.perf_counter(), "_call_started_at": time.time(), "provider_id": "", "model": "",
                    "_prices": parse_prices(self.settings().usage_prices_json),
                    "_price_snapshot_at": time.time(), "_logical_request_id": uuid4().hex}
        turn = event_turn(event)
        if turn is not None:
            identity.update(turn.to_dict())
            identity["source_umo"] = turn.origin_umo
            identity["_turn_context"] = turn
        event.set_extra("_quill_usage_identity", identity)
        return identity

    @contextmanager
    def bind(self, identity):
        token = _identity.set(dict(identity))
        try:
            with bind_turn(identity.get("_turn_context")):
                yield
        finally:
            _identity.reset(token)

    @contextmanager
    def bind_memory(self, session_id):
        current = _identity.get()
        turn = self.session_resolver(session_id)
        if current and current.get("scope_id") == turn.scope_id and current.get("persona_id", "") == turn.persona_id:
            yield
            return
        identity = {**turn.to_dict(), "turn_kind": "background", "source_umo": turn.origin_umo,
                    "_turn_context": turn}
        with self.bind(identity):
            yield

    async def note_provider(self, identity, req, event=None):
        try:
            config = self.context.get_config(event.unified_msg_origin) if event is not None else {}
            runner_config = config.get('agent_runner') or {}
            model_config = (runner_config.get("config") or {}).get("model")
            fallback_ids = model_config.get('fallback_provider_ids') if isinstance(model_config,dict) else None
            identity["_fallback_possible"] = runner_config.get('runner_type') != 'local' or not isinstance(fallback_ids,list) or bool(fallback_ids)
            provider = await selected_provider(self.context, event) if event is not None else None
            if provider is not None:
                meta = provider.meta()
                identity["provider_id"] = str(getattr(meta, "id", "") or "")[:200]
                identity["_provider_type"] = str(getattr(meta, "type", "") or "")
                identity["model"] = str(getattr(req, "model", None) or provider.get_model() or "")[:200]
                identity["_reported_currency"] = self._currency_hint(provider)
        except Exception as exc:
            logger.debug("[Quill Usage] provider 元数据不可用（%s）", type(exc).__name__)

    @staticmethod
    def endpoint_identity(base_url):
        # 决策服务不一定是宿主 Provider；用端点摘要区分，不存地址或密钥。
        try:
            url = urlsplit(str(base_url or ""))
            canonical = f"{url.scheme.lower()}://{url.hostname or ''}:{url.port or ''}{url.path.rstrip('/')}"
        except ValueError:
            return "endpoint:invalid"
        return "endpoint:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]

    async def _write_loop(self):
        while True:
            record, days, maximum = await self._queue.get()
            try:
                self._inflight = True
                await self.store.put(record, days, maximum)
                self.error = None
            except Exception as exc:
                self.error = type(exc).__name__
                self.dropped_records += 1
                logger.warning("[Quill Usage] 台账写入失败（%s），对话继续", self.error)
            finally:
                self._inflight = False
                self._queue.task_done()

    async def _save_gaps(self):
        async with self._gap_lock:
            count = self.dropped_records - self._saved_gaps
            if count > 0:
                await self.store.note_gaps(count)
                self._saved_gaps += count

    async def close(self):
        self._ready = False
        if self._writer is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("[Quill Usage] 台账收尾超时，仍有 %s 条待写记录", self._queue.qsize())
                self.dropped_records += self._queue.qsize() + int(getattr(self,'_inflight',False))
            self._writer.cancel()
            await asyncio.gather(self._writer, return_exceptions=True)
            self._writer = None
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()
        try:
            await self._save_gaps()
        finally:
            await self.store.close()


    @staticmethod
    def _currency_hint(provider):
        # OpenRouter 官方 usage.cost 以 USD 计；不从模型名或任意兼容站猜币种。
        config = getattr(provider, "provider_config", {}) or {}
        url = urlsplit(str(config.get("api_base", "")))
        return "USD" if url.scheme == "https" and url.hostname == "openrouter.ai" and url.port in (None, 443) else None

    async def record(self, response, **kwargs):
        try:
            await self._record(response, **kwargs)
        except Exception as exc:
            self.error = type(exc).__name__
            self.dropped_records += 1
            logger.warning("[Quill Usage] 采集失败（%s），对话继续", self.error)

    async def _record(self, response, *, purpose, provider_id="", model="", identity=None,
                     call_id=None, started=None, status="completed", coverage="plugin_call_boundary",
                     error=None, usage_supported=True):
        if not self._enabled():
            return
        origin = dict(identity or _identity.get() or {})
        origin.setdefault("turn_id", uuid4().hex)
        origin.setdefault("source_umo", "")
        origin.setdefault("scope_id", origin["source_umo"])
        origin.setdefault("persona_id", "")
        raw = field(response, "raw_completion")
        served_raw = raw if raw is not None else response
        served = field(served_raw, "model") or field(served_raw, "model_version")
        producer = origin.get('_provider_type','')
        if purpose == 'main' and origin.get('_fallback_possible',True):
            producer = ''  # 请求选择不证明实际原生提取器，不能替未知回退报告零命中。
        usage = normalize_usage(response, purpose=purpose, provider_type=producer) if usage_supported else normalize_usage(None, purpose=purpose)
        fallback_unknown = purpose == 'main' and origin.get('_fallback_possible',True)
        price = reported_cost(response, currency_hint=None if fallback_unknown else origin.get("_reported_currency")) if usage_supported else None
        if price is None:
            # M5：有回退但宿主未证明实际 provider 时，不套请求 provider 的专属单价。
            price_provider = "" if purpose == "main" and origin.get("_fallback_possible", True) else provider_id
            if fallback_unknown and not served:
                price = {'cost_amount':None,'currency':None,'cost_source':'unknown','price_snapshot':None,
                         'cost_note':'宿主存在未知回退且未报告实际模型，未套用请求模型价格'}
            else:
                price = price_usage(usage, origin["_prices"] if "_prices" in origin else parse_prices(self.settings().usage_prices_json),
                                    price_provider, str(served or model or ""))
        resolved_call_id = call_id or uuid4().hex
        record = {
            "call_id": resolved_call_id, "turn_id": origin["turn_id"],
            "logical_request_id": origin.get("_logical_request_id") or uuid4().hex, "attempt_id": None,
            "call_source": "host_final_response" if purpose == "main" else "plugin_invocation",
            "turn_kind": origin.get("turn_kind", "unattributed"),
            "actor_id": origin.get("actor_id"), "platform_instance_id": origin.get("platform_instance_id", ""),
            "message_type": origin.get("message_type", ""),
            "started_at": origin.get("_call_started_at"), "ended_at": time.time(),
            "created_at": origin.get("_call_started_at", time.time()), "source_umo": origin["source_umo"],
            "attempt_coverage": "plugin_call_boundary_only", "ttft_ms": None,
            "scope_id": origin["scope_id"], "scope_revision": origin.get("scope_revision", 0),
            "persona_id": origin["persona_id"], "provider_id": str(provider_id or "")[:200],
            "model": str(served or model or "unknown")[:200],
            "requested_model": str(model or "")[:200], "served_model": str(served or "")[:200],
            "provider_identity_source": "request_selection_unverified" if purpose == "main" else
                ("endpoint_hash" if str(provider_id).startswith("endpoint:") else "explicit_call"),
            "purpose": purpose, "status": status, "billing_coverage": coverage,
            "duration_ms": max(0, int((time.perf_counter() - started) * 1000)) if started else None,
            "usage": usage, "error_class": error, **price,
        }
        record["price_snapshot_at"] = origin.get("_price_snapshot_at", record["created_at"]) if price.get("price_snapshot") else None
        cfg = self.settings()
        try:
            # M8：回复不等待 SQLite fsync；有界队列满时显式报覆盖缺口。
            self._queue.put_nowait((record, cfg.usage_retention_days, cfg.usage_max_records))
        except asyncio.QueueFull:
            self.dropped_records += 1
            self.error = "usage_queue_full"
            logger.warning("[Quill Usage] 台账队列已满，本次未记录；对话继续")

    async def main_response(self, event, response):
        if getattr(response, "is_chunk", False):
            return  # 主钩子只采最终回调，不能把累积 chunk 求和。
        identity = event.get_extra("_quill_usage_identity")
        if not isinstance(identity, dict):
            return
        raw = field(response, "raw_completion")
        upstream_id = field(response, "id") or field(raw, "id") or "final"
        call_id = hashlib.sha256(
            (identity["_logical_request_id"] + ":main:" + str(upstream_id)).encode()
        ).hexdigest()
        await self.record(response, purpose="main", identity=identity, call_id=call_id,
                          provider_id=identity.get("provider_id", ""), model=identity.get("model", ""),
                          started=identity.get("started_at"),
                          status="error" if field(response, "role") == "err" else "completed",
                          coverage="final_response_only")
        event.set_extra("_quill_usage_call_id", call_id)

    async def generate(self, *, provider_id, purpose, timeout=None, session_id=None, **kwargs):
        provider = self.context.get_provider_by_id(provider_id)
        try:
            model = provider.get_model() if provider is not None else ""
        except Exception:
            model = ""
        if session_id is not None:
            with self.bind_memory(session_id):
                return await self.generate(provider_id=provider_id, purpose=purpose, timeout=timeout, **kwargs)
        generator = getattr(self.context, "llm_generate", None)
        # 老宿主缺公开生成助手时保留原来的 provider 调用，不因记量破坏辅助功能。
        return await self.invoke(
            lambda: generator(chat_provider_id=provider_id, **kwargs) if callable(generator)
                    else provider.text_chat(**kwargs),
            purpose=purpose, provider_id=provider_id, model=model, timeout=timeout,
        )

    async def invoke(self, operation, *, purpose, provider_id="", model="", timeout=None,
                     usage_supported=True, session_id=None, base_url=None):
        if session_id is not None:
            with self.bind_memory(session_id):
                return await self.invoke(operation, purpose=purpose, provider_id=provider_id,
                                         model=model, timeout=timeout, usage_supported=usage_supported, base_url=base_url)
        started, call_id = time.perf_counter(), uuid4().hex
        identity = dict(_identity.get() or {})
        identity["_call_started_at"] = time.time()
        identity["_prices"] = parse_prices(self.settings().usage_prices_json)
        identity["_price_snapshot_at"] = time.time()
        identity["_logical_request_id"] = uuid4().hex
        if base_url is not None:
            provider_id = self.endpoint_identity(base_url)
        try:
            provider = self.context.get_provider_by_id(provider_id) if provider_id and base_url is None else None
            identity["_reported_currency"] = self._currency_hint(provider) if provider else None
            if base_url is not None:
                url = urlsplit(str(base_url))
                identity["_reported_currency"] = "USD" if url.scheme == "https" and url.hostname == "openrouter.ai" and url.port in (None, 443) else None
            if provider is not None:
                identity["_provider_type"] = str(getattr(provider.meta(), "type", ""))
            if provider and not model:
                model = str(provider.get_model() or "")
        except Exception:
            identity["_reported_currency"] = None
        try:
            value = operation()
            if inspect.isawaitable(value):
                result = await asyncio.wait_for(value, timeout=timeout)
            else:
                result = value
        except asyncio.CancelledError:
            await self.record(None, purpose=purpose, provider_id=provider_id, model=model,
                              identity=identity, call_id=call_id, started=started,
                              status="cancelled", error="CancelledError")
            raise
        except Exception as exc:
            await self.record(None, purpose=purpose, provider_id=provider_id, model=model,
                              identity=identity, call_id=call_id, started=started,
                              status="error", error=type(exc).__name__)
            raise
        await self.record(result, purpose=purpose, provider_id=provider_id, model=model,
                          identity=identity, call_id=call_id, started=started,
                          usage_supported=usage_supported)
        return result

    async def clear(self):
        await asyncio.wait_for(self._queue.join(), timeout=5)
        await self.store.clear()
        self.error = None

    async def query(self, **filters):
        cfg = self.settings()
        await asyncio.wait_for(self._queue.join(), timeout=5)
        await self._save_gaps()
        for key in ('scope_id', 'source_umo'):
            if filters.get(key) and not (len(filters[key]) == 16 and all(c in '0123456789abcdef' for c in filters[key])):
                filters[key] = hashlib.sha256(filters[key].encode()).hexdigest()[:16]
        result = await self.store.query(**filters, retention_days=cfg.usage_retention_days,
                                        max_records=cfg.usage_max_records)
        from ..quill.services.usage import convert_usage_view
        result = convert_usage_view(result, cfg.usage_display_currency, cfg.usage_usd_cny_rate)
        diagnostics = await self.store.diagnostics()
        return {**result, "enabled": cfg.usage_enabled, "available": self.ready,
                'quarantined_records': diagnostics['quarantined_records'],
                "error_class": self.error, "dropped_records": self.dropped_records,
                "coverage_note": "主对话仅记录宿主最终响应；工具循环中间调用及供应商内部重试未逐次暴露，不代表完整账单。"}
