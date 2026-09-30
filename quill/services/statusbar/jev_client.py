# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Jev（TypeSafe System One）结构化判定客户端 + 剧情走向推荐度辅助。

Jev 不生成文本，只对 state 做类型化判定（noul/choice/score），返回概率
分布与置信度（docs.typesafe.ai/api）。本模块只依赖标准库——市场插件不新增
pip 依赖，不用官方 typesafe_sdk；HTTP 走 urllib + asyncio.to_thread。

接入方式（v5.3 调研定稿）：复用 AstrBot 自带模型提供商。用户在 AstrBot
面板建一个指向 api.typesafe.ai 的 openai_compatible 提供商（模型填
jev-latest），本模块从 ``provider.provider_config`` 提取 api_base/key，
发原生 ``POST /v1/systemone``。Jev 没有 chat/completions 端点，
``provider.text_chat()`` 走不通，必须直连。

fail-open 契约：任何失败（无凭据/超时/非 200/解析失败/低置信）一律返回
None 或原样输出——Jev 挂了，状态栏与剧情走向不能挂。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

JEV_DEFAULT_MODEL = "jev-latest"
JEV_TIMEOUT_S = 5.0
DEFAULT_CONFIDENCE_FLOOR = 0.6

# ── 凭据与端点（从 AstrBot provider_config 提取）─────────────────


def extract_jev_creds(provider_config: Any) -> tuple[str, str, str] | None:
    """从 provider_config 提取 (api_key, api_base, model)；缺关键项返回 None。

    AstrBot 的 ``key`` 允许配置成列表（多 key 轮换），取第一个非空值；
    model 缺省回退 jev-latest（provider 的 model 字段本就应填 Jev 模型名）。
    """
    if not isinstance(provider_config, dict):
        return None
    raw_key = provider_config.get("key", "")
    if isinstance(raw_key, (list, tuple)):
        api_key = next((str(k).strip() for k in raw_key if str(k).strip()), "")
    else:
        api_key = str(raw_key or "").strip()
    api_base = str(
        provider_config.get("api_base") or provider_config.get("base_url") or ""
    ).strip().rstrip("/")
    model = str(provider_config.get("model") or "").strip() or JEV_DEFAULT_MODEL
    if not api_key or not api_base:
        return None
    return api_key, api_base, model


def resolve_jev_endpoint(api_base: str) -> str:
    """api_base → /v1/systemone。容忍带或不带 /v1、带或不带尾斜杠。"""
    base = (api_base or "").strip().rstrip("/")
    if base.endswith("/v1"):
        return base + "/systemone"
    return base + "/v1/systemone"


def _post_json(url: str, api_key: str, payload: dict, timeout: float) -> dict:
    """同步 POST（在线程池里跑）；429/529 单次短退避重试。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    for attempt in (1, 2):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt == 1:
                time.sleep(0.6)
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


async def jev_evaluate(
    provider_config: Any,
    state: Any,
    questions: dict,
    timeout: float = JEV_TIMEOUT_S,
    post_fn: Callable | None = None,
) -> dict | None:
    """一次 System One 评估，返回原始响应 dict；任何失败返回 None。

    ``post_fn(url, api_key, payload, timeout)`` 仅供测试注入替身；生产走
    ``_post_json``（asyncio.to_thread + wait_for 双层超时：内层管网络，
    外层兜住线程排队——线程池被占满时也不能拖住聊天管道）。
    """
    creds = extract_jev_creds(provider_config)
    if creds is None:
        return None
    api_key, api_base, model = creds
    payload = {"state": state, "model": model, "questions": questions}
    post = post_fn or _post_json
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(post, resolve_jev_endpoint(api_base), api_key, payload, timeout),
            timeout=timeout + 2.0,
        )
    except Exception:
        return None


# ── 剧情走向：问题与 state 构造 ─────────────────────────────────


def build_plot_questions(options: list[str]) -> dict:
    """剧情走向两问（官方推荐的原子化设计）。

    - ``responding``（Noul）：用户是否在响应分支提示？< 0.5 时整体跳过——
      用户无视选项自由 RP 时不误路由、不标误导性百分比；
    - ``branch``（Choice）：选了哪个分支，criteria 键即选项序号，
      值为选项原文（中文选项名直接做判据；指令用英文写——Jev 主训练语言
      是英语，CJK 内容保留在 state 与选项原文里）。
    """
    criteria = {str(i + 1): opt for i, opt in enumerate(options)}
    return {
        "responding": {
            "type": "noul",
            "instructions": (
                "Is the user's latest message responding to the plot-branch "
                "options shown earlier (picking one by number or wording, or "
                "clearly steering the story along one of them)?"
            ),
            "criteria": {
                "true": "The user is choosing or clearly following one of the plot options.",
                "false": "The message ignores the options and just continues free roleplay.",
            },
        },
        "branch": {
            "type": "choice",
            "instructions": (
                "Which plot branch does the user's latest message pick or "
                "follow? Answer with the option number."
            ),
            "criteria": criteria,
        },
    }


def build_route_state(user_msg: str, recent: list[str], options: list[str]) -> dict:
    """构造 state：选项表 + 最近对话（截断）+ 本条消息（截断）。"""
    return {
        "plot_options": [f"{i + 1}. {o}" for i, o in enumerate(options)],
        "recent_dialogue": [str(x)[:400] for x in (recent or [])][-6:],
        "latest_user_message": str(user_msg or "")[:800],
    }


# ── 用户显式数字选择（不走 Jev）───────────────────────────────────

_NUMERIC_PICK_RE = re.compile(r"^([1-9])(?:[.、)）]|\s*$)")


def parse_numeric_pick(text: str, n_options: int) -> int | None:
    """显式数字选择（"1"、"2."、"3、"）→ 选项序号（1 基）；否则 None。

    显式数字零成本零风险，直接精确匹配；只有自由文本/模糊输入才交给 Jev。
    超出选项数的数字（如只有 2 个选项时回 "3"）不算选择。
    """
    t = (text or "").strip()
    if not t:
        return None
    m = _NUMERIC_PICK_RE.match(t)
    if not m:
        return None
    idx = int(m.group(1))
    return idx if 1 <= idx <= int(n_options) else None


# ── 推荐选择度标注（第二档：百分比进正文块）───────────────────────

_PLOT_LINE_RE = re.compile(r"^(\s*)([1-9])[.、)）]?\s*(.+)$")


def annotate_plot_probs(
    plot_content: str,
    probs: dict[str, float],
    confidence: float,
    floor: float = DEFAULT_CONFIDENCE_FLOOR,
) -> str:
    """给【剧情走向】块逐项追加「▸ N%」推荐选择度（第二档）。

    - 按行首序号匹配分布键，模型改写选项文字不影响匹配；
    - ``confidence < floor`` 时原样返回——分布平坦（如 34/33/33）时百分比
      没有信息量，硬标反而误导（Jev "I don't know is a signal" 的用法）；
    - 命中行数 < 2（块结构对不上/只标中一项）→ 原样返回，避免半吊子标注。
    语义提醒：probabilities 是「当前剧情态势下各分支的合理度」，不是
    「用户会选什么」——UI 文案用「剧情倾向」，不要写「推荐你选」。
    """
    if not probs or confidence < floor:
        return plot_content
    lines = plot_content.splitlines()
    out: list[str] = []
    hit = 0
    for line in lines:
        m = _PLOT_LINE_RE.match(line)
        if m:
            p = probs.get(m.group(2))
            if isinstance(p, (int, float)) and 0.0 <= float(p) <= 1.0:
                out.append(f"{line} ▸ {round(float(p) * 100)}%")
                hit += 1
                continue
        out.append(line)
    if hit < 2:
        return plot_content
    return "\n".join(out)


def round_cache_entry(
    probs: dict[str, float],
    pick: int | None,
    confidence: float,
    options: list[str],
) -> dict:
    """构造轮次缓存条目（统一字段，消费方按需取用）。"""
    return {
        "probs": probs,
        "pick": pick,
        "confidence": confidence,
        "options": list(options),
        "ts": time.time(),
    }


def prune_round_cache(cache: dict, max_age_s: float = 300.0) -> None:
    """清理过期轮次缓存（阈值取 5 分钟 > 任何一轮的生命周期）。"""
    now = time.time()
    stale = [k for k, v in cache.items()
             if not isinstance(v, dict) or now - float(v.get("ts", 0)) > max_age_s]
    for k in stale:
        cache.pop(k, None)
