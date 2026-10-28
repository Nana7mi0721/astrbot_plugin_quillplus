# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 用量与定价的纯数据适配：缺失不填零，缓存不重复计费。"""

from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any


def field(value: Any, key: str, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def count(value: Any) -> int | None:
    # bool、负数、小数和非有限数均不能成为供应商的真实 token 计数。
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        if math.isfinite(number) and 0 <= number <= 1000000000000 and number.is_integer():
            return int(number)
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def _first(*values):
    return next((v for v in values if v is not None), None)


def normalize_usage(response: Any, *, purpose="main", provider_type="") -> dict:
    """计数与报告证据分开；宿主默认零不冒充供应商真实零。"""
    raw = field(response, "raw_completion")
    if raw is None:
        raw = response
    usage, meta = field(raw, "usage"), field(raw, "usage_metadata")
    chat = purpose not in ("embedding", "rerank", "decision", "decision_probe")
    result = dict(input_other=None, input_cached=None, cache_write=None,
                  cache_write_1h=None, output=None, reasoning=None, input_total=None,
                  usage_source="not_reported" if chat else "not_applicable",
                  cache_reported=False, cache_unknown=True, cache_applicable=chat,
                  cache_write_reported=False, cache_write_billable=None,
                  input_other_semantics="unknown", usage_all_zero=False)
    prompt = count(field(usage, "prompt_tokens"))
    direct_input = count(field(usage, "input_tokens"))
    if prompt is not None or direct_input is not None:
        anthropic = prompt is None and (field(raw, "type") == "message"
            or field(usage, "cache_read_input_tokens") is not None
            or field(usage, "cache_creation_input_tokens") is not None)
        if anthropic:
            other = direct_input
            cached = count(field(usage, "cache_read_input_tokens"))
            write = count(field(usage, "cache_creation_input_tokens"))
            ttl = field(usage, "cache_creation")
            one_hour = count(field(ttl, "ephemeral_1h_input_tokens"))
            five_min = count(field(ttl, "ephemeral_5m_input_tokens"))
            if write is None and five_min is not None and one_hour is not None:
                write = five_min + one_hour
            # 未报告 TTL 拆分时不能把有写入的调用猜成 5m。
            if write == 0:
                one_hour = 0
            result.update(input_other=other, input_cached=cached, cache_write=write,
                          cache_write_1h=one_hour, output=count(field(usage, "output_tokens")),
                          cache_write_reported=write is not None, cache_write_billable=True,
                          input_total=sum((other, cached, write))
                          if all(v is not None for v in (other, cached, write)) else None)
        else:
            total = prompt if prompt is not None else direct_input
            details = field(usage, "prompt_tokens_details" if prompt is not None else "input_tokens_details")
            cached = _first(count(field(details, "cached_tokens")),
                            count(field(usage, "prompt_cache_hit_tokens")),
                            count(field(usage, "cached_tokens")),
                            count(field(usage, "cache_read_input_tokens")))
            miss = count(field(usage, "prompt_cache_miss_tokens"))
            if cached is None and miss is not None and miss <= total:
                cached = total - miss
            if cached is not None and cached > total:
                cached = None
            write_raw = _first(field(details, "cache_write_tokens"),
                               field(usage, "cache_creation_input_tokens"))
            write_reported = write_raw is not None
            # 数值零与报告证据分开；缺写入字段的兼容站只能提供有边界的估算。
            write = count(write_raw) if write_reported else 0
            if cached is not None and write is not None and cached + write > total:
                cached, write = None, None
            result.update(input_total=total, input_cached=cached, cache_write=write,
                          cache_write_reported=write_reported,
                          cache_write_billable=True if write_reported else None, cache_write_1h=0 if write == 0 else None,
                          input_other=total - cached - write
                          if cached is not None and write is not None else None,
                          output=_first(count(field(usage, "completion_tokens")), count(field(usage, "output_tokens"))),
                          reasoning=_first(count(field(field(usage, "completion_tokens_details"), "reasoning_tokens")),
                                           count(field(field(usage, "output_tokens_details"), "reasoning_tokens"))))
            # 仅给 Anthropic 扩展字段的 relay，prompt 是否含缓存没有统一约定。
            # 保留原始报告量与缓存分项，不能猜 whole 后计算输入/金额。
            relay_only = (field(usage, "cache_read_input_tokens") is not None
                or field(usage, "cache_creation_input_tokens") is not None) and all(
                    field(obj, key) is None for obj, key in ((details, "cached_tokens"),
                    (usage, "prompt_cache_hit_tokens"), (usage, "cached_tokens")))
            if relay_only:
                result.update(input_other=None, input_total=None,
                              reported_input_tokens=total, input_inclusion="relay_cache_inclusion_unknown")
        result.update(usage_source="provider", cache_applicable=purpose not in ("embedding", "rerank"),
                      cache_reported=result["input_cached"] is not None,
                      cache_unknown=result["input_cached"] is None,
                      input_other_semantics="unknown" if result.get("input_inclusion") == "relay_cache_inclusion_unknown" else "excludes_cache_read_and_write")
        # embeddings 的原始 usage 若宿主未来公开，可直接采输入；无输出不是零输出报告。
        if purpose in ("embedding", "rerank"):
            result.update(cache_applicable=False, cache_reported=False, cache_unknown=True)
        result["usage_all_zero"] = result["input_total"] == 0 and result["output"] == 0
        return result
    if meta is not None:
        total = count(field(meta, "prompt_token_count"))
        cached = count(field(meta, "cached_content_token_count"))
        if total is not None:
            if cached is not None and cached > total:
                cached = None
            candidates = count(field(meta, "candidates_token_count"))
            thoughts = count(field(meta, "thoughts_token_count"))
            result.update(input_total=total, input_other=total - cached if cached is not None else None,
                          input_cached=cached, output=candidates + (thoughts or 0)
                          if candidates is not None else None,
                          cache_write=0, cache_write_1h=0, cache_write_billable=False,
                          cache_write_source="gemini_token_protocol_excludes_storage_fees",
                          reasoning=thoughts, usage_source="provider", cache_reported=cached is not None,
                          cache_unknown=cached is None, input_other_semantics="excludes_cache_read")
            return result
    native = field(response, "usage")
    other, cached, output = (count(field(native, name)) for name in ("input_other", "input_cached", "output"))
    if any(v is not None and v > 0 for v in (other, cached, output)):
        # 只对已审阅的宿主提取器认定真实零；第三方默认字段仍不冒充供应商证据。
        cache_known = cached is not None and (cached > 0 or provider_type in ('anthropic_chat_completion', 'openai_chat_completion', 'openai_responses', 'gemini'))
        result.update(input_other=other, input_cached=cached, output=output,
                      input_total=other + cached if other is not None and cached is not None else None,
                      usage_source="native", cache_reported=cache_known,
                      cache_unknown=not cache_known,
                      input_other_semantics="host_may_include_cache_write",
                      cache_write_source="host_not_exposed")
    elif native is not None and any(v is not None for v in (other, cached, output)):
        result["usage_source"] = "host_default_zero_unverified"
        result["usage_all_zero"] = all(v == 0 for v in (other,cached,output))
    elif purpose in ("embedding", "rerank"):
        # 宿主接口只返回向量/排名，供应商 raw usage 没有传出；不绕过宿主再请求一遍。
        result["usage_source"] = "host_not_exposed"
    return result


def parse_prices(text: str) -> dict:
    """只接受用户审阅的静态单价；不联网拉价、不解析任意计价表达式。"""
    try:
        data = json.loads(text or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict) or len(data) > 200:
        return {}
    clean = {}
    for name, row in data.items():
        if not isinstance(name, str) or len(name) > 240 or not isinstance(row, dict):
            continue
        currency = str(row.get("currency", "")).upper()
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            continue
        rates = {}
        for key in ("input", "output", "cache_read", "cache_write", "cache_write_1h"):
            try:
                value = Decimal(str(row[key]))
                if value.is_finite() and 0 <= value <= 1000000000:
                    rates[key] = str(value)
            except (KeyError, InvalidOperation, TypeError, ValueError):
                continue
        clean[name] = {"currency": currency, "per_million": rates,
                       "version": str(row.get("version", "manual"))[:80]}
    return clean


def price_usage(usage: dict, prices: dict, provider: str, model: str) -> dict:
    snapshot = prices.get(f"{provider}/{model}") or prices.get(model)
    result = {"cost_amount": None, "currency": None,
              "cost_source": "unknown", "price_snapshot": None, "cost_note": None}
    if not snapshot:
        return result
    result["price_snapshot"] = snapshot
    result["currency"] = snapshot["currency"]
    rates = snapshot["per_million"]
    other, cached, output = (usage.get(k) for k in ("input_other", "input_cached", "output"))
    if other is None or cached is None or output is None:
        return result
    # 无缓存写报告的协议不能凭直觉按免费写缓存算账。
    write, one_hour = usage.get("cache_write"), usage.get("cache_write_1h")
    if write is None:
        result['cost_note'] = '宿主未暴露缓存创建分项，无法可靠计价'
        return result
    if one_hour is None:
        if write:
            return result
        one_hour = 0
    if one_hour > write:
        return result
    components = {"input": other, "output": output, "cache_read": cached,
                  "cache_write": write - one_hour, "cache_write_1h": one_hour}
    amount = Decimal(0)
    for key, tokens in components.items():
        if tokens:
            if key not in rates:
                return result
            amount += Decimal(tokens) * Decimal(rates[key]) / Decimal(1000000)
    write_unknown = not usage.get('cache_write_reported') and usage.get('cache_write_billable') is not False
    result.update(cost_amount=format(amount, "f"), cost_source="price_snapshot_partial" if write_unknown else "price_snapshot")
    if write_unknown:
        result['cost_note'] = '未报告缓存写入费用，仅估算已知输入、缓存读取和输出；可能低估'
    elif usage.get('cache_write_source') == 'gemini_token_protocol_excludes_storage_fees':
        result['cost_note'] = '仅 token 费用估算，不含显式缓存存储时长费用'
    return result


def reported_cost(response: Any, *, currency_hint=None) -> dict | None:
    """金额必须同时带币种；兼容接口只给 cost 数字时不擅自假定美元。"""
    raw = field(response, "raw_completion") or response
    usage = field(raw, "usage")
    currency = field(usage, "currency") or field(raw, "currency") or currency_hint
    amount = field(usage, "cost")
    if amount is None:
        amount = field(raw, "cost")
    if isinstance(amount, dict):
        currency = amount.get("currency")
        amount = amount.get("amount")
    try:
        value = Decimal(str(amount))
        currency = str(currency or "").upper()
        if value.is_finite() and value >= 0 and len(currency) == 3 and currency.isascii() and currency.isalpha():
            return {"cost_amount": format(value, "f"), "currency": currency,
                    "cost_source": "reported", "price_snapshot": None}
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None


def validate_prices(text) -> str | None:
    if not isinstance(text, str) or len(text) > 100000:
        return "价格表必须为不超过 100000 字符的 JSON 文本"
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return "价格表不是有效 JSON"
    if not isinstance(data, dict) or len(data) > 200:
        return "价格表必须为对象，最多 200 个模型"
    parsed = parse_prices(text)
    if set(parsed) != set(data):
        return "价格条目需要模型名和三位币种，例如 USD"
    for name, row in data.items():
        keys = {"input", "output", "cache_read", "cache_write", "cache_write_1h"}
        supplied = keys.intersection(row)
        if not supplied or supplied != set(parsed[name]["per_million"]):
            return "单价必须为有限非负数，单位为每百万 token"
        if set(row) - keys - {"currency", "version"}:
            return "价格表包含不支持的字段"
    return None
