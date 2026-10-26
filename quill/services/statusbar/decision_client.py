# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""决策模型（Decision Models）结构化判定客户端 + 剧情走向推荐度辅助。

决策模型不生成文本，只对 state/input 做类型化判定（noul·predicate /
choice / score），返回概率分布与置信度。2026 年下半年起决策模型大量上线
（TypeSafe Jev、OpenAI GPT-6 Luna Decisions、Inception Mercury Decide、
Microsoft Decision-1、Cloudflare Clef、Perplexity pplx-decider、Upstage
Solar-Decide、Liquid D1、Hal-X THX-01、Baseten Elephant-1、云知声 U2 …），
但 wire format 分成两族：

* **System One**（TypeSafe 定义的基准，绝大多数服务采用）：
  ``POST /v1/systemone``，请求 ``{model, state, questions: {名: {type,
  instructions, criteria}}}``，响应 ``answers`` 为按问题名索引的对象。
* **OpenAI Decisions**（OpenAI 2026-09 DevDay 发布，api.openai.com 及
  LLM Gateway 等新式网关采用）：``POST /v1/decisions``，请求
  ``{model, input, questions: [{name, type, instructions, choices|levels}]}``
  ——``state``→``input``、questions 变数组、``noul``→``predicate``、
  choice 的 ``criteria``→``choices``、score 的 ``criteria``→``levels``；
  响应 ``answers`` 为按提问顺序的数组，每项回显 ``name``，概率分布为
  ``[{value, probability}]`` 数组，拒答项 ``type=="refusal"``。

本模块对两族格式做统一封装：发请求时按 ``api_format``（auto 按地址判
openai.com → openai，其余 → systemone）选 wire format，收响应时**双向归
一化**——无论对面是哪族，调用方拿到的都是 System One 形状的
``{问题名: {type, noul|choice, probabilities: {标签: 概率}, confidence}}``。

各服务差异的适配点（均按 base_url 的主机名自动处理，无需用户关心）：

* 鉴权：默认 ``Authorization: Bearer <key>``；Baseten（Elephant-1）用
  ``Api-Key`` 方案；Hal-X（THX-01）无需鉴权（key 留空即可）。
* 路径：OpenRouter 的 systemone 挂在 ``/api/v1`` 下，填
  ``https://openrouter.ai`` 或 ``https://openrouter.ai/api`` 都会归一到
  ``https://openrouter.ai/api/v1/systemone``；用户直接填完整端点
  （…/v1/systemone 或 …/v1/decisions）则原样使用（M14 教训：不能重复拼接）。
* 字段方言：THX-01 的问题描述字段是 ``question`` 而非 ``instructions``。

接入方式（v5.4 起）：不再借用 AstrBot 提供商——决策模型没有
chat/completions 端点，AstrBot 的 openai_compatible 提供商对它们不可用。
凭据（base_url / api_key / model / api_format）在插件配置里独立填写。

fail-open 契约：任何失败（凭据缺失/超时/非 200/解析失败/低置信）一律返回
None 或原样输出——判定服务挂了，状态栏与剧情走向不能挂。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit

from ...core.logbridge import logger

DECISION_TIMEOUT_S = 5.0
DEFAULT_CONFIDENCE_FLOOR = 0.6

# 基地址缺省值（配置 schema 与前端同款；OpenRouter 汇聚了 13+ 家决策模型）
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

_VALID_FORMATS = ("systemone", "openai")

# ── 格式与端点解析 ──────────────────────────────────────────────


def is_http_url(url: str) -> bool:
    """地址是否是「可外连的 HTTP(S) 端点」（scheme 与主机都要有）。

    Q1 修复：``urllib.request.urlopen`` 默认支持 ``file://``、``ftp://``、
    ``data:`` 等协议，而 ``base_url`` 是**用户/面板可填的自由文本**。不校验
    协议就意味着一次判定调用可以变成任意本地文件读取（``file:///etc/passwd``
    经 ``resolve_decision_endpoint`` 变成 ``file:///etc/passwd/v1/systemone``
    后仍会走 FileHandler），错误片段再经 ``detail`` 回传给调用方。

    只放行 http/https 且 netloc 非空——判定服务不存在别的协议形态，
    这条白名单不会误伤任何真实用法。
    """
    try:
        parts = urlsplit(str(url or "").strip())
    except Exception:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def resolve_format(base_url: str, api_format: str | None) -> str:
    """决定 wire format：显式 systemone/openai 优先，auto 按主机名判。

    auto 规则刻意保守：只有 ``openai.com`` 域名才走 OpenAI Decisions 格式
    （api.openai.com/v1/decisions），其余一切地址（typesafe.ai、
    openrouter.ai、jevstyle.com、hal-x.ai、baseten.co、maas-api.unisound.com、
    TrueFoundry/LLM Gateway 的 /v1/systemone 兼容层……）默认 System One——
    它是事实基准，且 OpenRouter 上全部 22 个决策模型都接受该格式。
    """
    fmt = str(api_format or "").strip().lower()
    if fmt in _VALID_FORMATS:
        return fmt
    host = (urlsplit(str(base_url or "")).netloc or "").lower()
    return "openai" if "openai.com" in host else "systemone"


def resolve_decision_endpoint(base_url: str, fmt: str) -> str:
    """base_url → 完整判定端点。容忍带或不带 /v1、带或不带尾斜杠。

    M14 教训（沿袭自 JEV 客户端）：用户把**完整端点**填进 base_url 是完全
    自然的做法，不能在后面再拼一次得到 ``…/v1/systemone/v1/systemone``。
    已经指向 systemone/decisions 时原样返回。OpenRouter 特例：其端点挂在
    ``/api`` 前缀下（``https://openrouter.ai/api/v1/systemone``），用户只
    填裸域名时自动补 ``/api``。
    """
    base = (base_url or "").strip().rstrip("/")
    if fmt == "openai":
        if base.endswith("/decisions"):
            return base
        if base.endswith("/v1"):
            return base + "/decisions"
        return base + "/v1/decisions"
    if base.endswith("/systemone"):
        return base
    parts = urlsplit(base)
    if "openrouter.ai" in (parts.netloc or "").lower() and not (
        parts.path or ""
    ).startswith("/api"):
        base += "/api"
    if base.endswith("/v1"):
        return base + "/systemone"
    return base + "/v1/systemone"


def build_headers(api_key: str, url: str) -> dict:
    """按服务选鉴权头。Baseten 用 ``Api-Key`` 方案，Hal-X 免鉴权（不强制）。"""
    headers = {"Content-Type": "application/json"}
    key = str(api_key or "").strip()
    if not key:
        return headers
    host = (urlsplit(url).netloc or "").lower()
    if "baseten.co" in host:
        headers["Authorization"] = f"Api-Key {key}"
    else:
        headers["Authorization"] = f"Bearer {key}"
    return headers


# ── 请求体构造（两族 wire format）──────────────────────────────


def _systemone_body(model: str, state: Any, questions: dict) -> dict:
    payload: dict = {"state": state, "questions": questions}
    if str(model or "").strip():
        payload["model"] = str(model).strip()
    return payload


def _openai_body(model: str, state: Any, questions: dict) -> dict:
    oq = []
    for name, q in (questions or {}).items():
        qtype = str(q.get("type") or "")
        item: dict = {
            "name": name,
            "type": "predicate" if qtype == "noul" else qtype,
            "instructions": str(q.get("instructions") or ""),
        }
        crit = q.get("criteria")
        if qtype == "choice" and isinstance(crit, dict):
            # System One criteria {标签: 描述} → OpenAI choices [{value, description}]
            item["choices"] = [
                {"value": str(k), "description": str(v)} for k, v in crit.items()
            ]
        elif qtype == "score" and isinstance(crit, (list, tuple)):
            # System One criteria [等级, ...] → OpenAI levels [{label, description}]
            item["levels"] = [
                {"label": str(v)} if not isinstance(v, dict) else {
                    "label": str(v.get("label", "")),
                    **({"description": str(v["description"])} if v.get("description") else {}),
                }
                for v in crit
            ]
        oq.append(item)
    # OpenAI input 只收字符串（或消息数组）；结构化 state 序列化为紧凑 JSON
    if isinstance(state, str):
        text = state
    else:
        text = json.dumps(state, ensure_ascii=False)
    payload: dict = {"input": text, "questions": oq}
    if str(model or "").strip():
        payload["model"] = str(model).strip()
    return payload


def build_request_body(
    fmt: str, model: str, state: Any, questions: dict, url: str
) -> dict:
    """构造请求体；THX-01（hal-x.ai）方言：``instructions`` → ``question``。

    方言改名重建副本，绝不原地改动调用方传入的 questions——同一个 dict 会
    被多次求值复用（如测试对拍两族格式），原地改名会污染后续调用。
    """
    if fmt == "openai":
        return _openai_body(model, state, questions)
    qs = questions or {}
    if "hal-x.ai" in (urlsplit(url).netloc or "").lower():
        qs = {
            name: dict(
                (k, v) for k, v in q.items() if k != "instructions"
            ) | ({"question": q["instructions"]} if q.get("instructions") else {})
            for name, q in qs.items()
        }
    return _systemone_body(model, state, qs)


# ── 响应归一化（两族 → System One 形状）────────────────────────


def _norm_probs(raw: Any) -> dict[str, float]:
    """概率分布归一化：``{标签: 概率}`` 或 ``[{value, probability}]`` → 前者。"""
    out: dict[str, float] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[str(k)] = float(v)
    elif isinstance(raw, list):
        for e in raw:
            if isinstance(e, dict) and isinstance(e.get("probability"), (int, float)) \
                    and not isinstance(e.get("probability"), bool):
                out[str(e.get("value"))] = float(e["probability"])
    return out


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def normalize_answers(resp: Any) -> dict | None:
    """两族响应 → 统一 ``{问题名: {type, noul|choice|score, probabilities, confidence}}``。

    - System One：``answers`` 是按问题名索引的对象，noul 值在 ``noul``；
    - OpenAI Decisions：``answers`` 是数组（按提问顺序，回显 ``name``），
      predicate 值在 ``probability``，choice 选中值在 ``choice``，概率分布
      是 ``[{value, probability}]``；
    - ``type == "refusal"``（模型拒答的个别问题）直接丢弃，调用方按缺答
      处理——一道坏问题不该拖垮整批（OpenAI 官方语义）。
    - 两种形状混存或字段缺失都不抛异常，缺什么按 0/空处理。
    """
    if not isinstance(resp, dict):
        return None
    answers = resp.get("answers")
    if isinstance(answers, dict):
        items = answers.items()
    elif isinstance(answers, list):
        items = (
            (a.get("name") or f"question_{i}", a)
            for i, a in enumerate(answers)
            if isinstance(a, dict)
        )
    else:
        return None
    out: dict = {}
    for name, a in items:
        if not isinstance(a, dict) or a.get("type") == "refusal":
            continue
        t = str(a.get("type") or "")
        if t in ("noul", "predicate"):
            out[name] = {
                "type": "noul",
                "noul": _safe_float(
                    a.get("noul", a.get("probability", 0.0))
                ),
            }
        elif t == "choice":
            out[name] = {
                "type": "choice",
                "choice": str(a.get("choice", a.get("value", "")) or ""),
                "probabilities": _norm_probs(a.get("probabilities")),
                "confidence": _safe_float(a.get("confidence")),
            }
        elif t == "score":
            out[name] = {
                "type": "score",
                "score": _safe_float(a.get("score")),
                "probabilities": _norm_probs(a.get("probabilities")),
                "confidence": _safe_float(a.get("confidence")),
            }
    return out or None


# ── 主入口：一次判定调用 ────────────────────────────────────────


def _post_json(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    """同步 POST（在线程池里跑）；429/529 单次短退避重试。

    Q1 修复：这里是**唯一**真正发出请求的出口，协议白名单卡在此处而不是只
    卡在上层——上游两个公开入口都有各自的早退分支，将来任一条新增调用路径
    漏写校验时，实际 I/O 仍然只可能落到 http/https 上。
    """
    if not is_http_url(url):
        raise ValueError(f"不允许的协议或地址：{str(url or '')[:120]}")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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


async def decision_evaluate(
    base_url: str,
    api_key: str,
    model: str,
    state: Any,
    questions: dict,
    api_format: str = "auto",
    timeout: float = DECISION_TIMEOUT_S,
    post_fn: Callable | None = None,
) -> dict | None:
    """一次判定调用，返回**归一化后**的 ``{问题名: 答案}``；任何失败返回 None。

    ``post_fn(url, headers, payload, timeout)`` 仅供测试注入替身；生产走
    ``_post_json``（asyncio.to_thread + wait_for 双层超时：内层管网络，
    外层兜住线程排队——线程池被占满时也不能拖住聊天管道）。

    base_url 缺失即视为未配置（静默返回 None 并留 debug 线索）；model 与
    api_key 允许留空（Hal-X 免鉴权、部分服务默认模型——缺什么由服务端
    4xx 报出来，warning 日志可检索）。
    """
    base = str(base_url or "").strip()
    if not base:
        logger.debug("[Decision] 未启用：decision_base_url 未配置")
        return None
    if not is_http_url(base):
        # Q1 修复：非 http(s) 地址一律当未配置处理。生产路径本来 fail-open，
        # 这里只是把「悄悄不做判定」的理由留在 debug 日志里。
        logger.debug("[Decision] 地址协议不支持（仅 http/https）：%s", base[:120])
        return None
    fmt = resolve_format(base, api_format)
    url = resolve_decision_endpoint(base, fmt)
    headers = build_headers(api_key, url)
    payload = build_request_body(fmt, model, state, questions, url)
    post = post_fn or _post_json
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(post, url, headers, payload, timeout),
            timeout=timeout + 2.0,
        )
    except Exception as e:
        # M14 教训：api_base 拼错导致永久 404 时，用户与开发者都拿不到任何
        # 线索。fail-open 语义不变，只是不再静默——具体原因（HTTP 码 / DNS /
        # 超时）由 _post_json 抛出的异常带上来。
        logger.warning(
            "[Decision] 判定调用失败（本次不标注走向百分比）: url=%s err=%s: %s",
            url, type(e).__name__, e,
        )
        return None
    return normalize_answers(raw)


# ── 连通性测试（面板「测试」按钮）────────────────────────────────


def _probe_questions() -> dict:
    """测试用两问：一道 noul + 一道 choice。

    刻意两族都问：只发 noul 就测不出 choice 的 ``criteria``→``choices``
    映射，而剧情走向恰恰依赖 choice；只发 choice 又漏掉 predicate 分支。
    选项数取 3（与 plot_paths 默认等长），不依赖任何会话状态。
    """
    return {
        "responding": {
            "type": "noul",
            "instructions": (
                "Is the user's message a greeting? Answer true for a greeting "
                "such as '你好'."
            ),
            "criteria": {
                "true": "The message greets someone.",
                "false": "The message does not greet anyone.",
            },
        },
        "branch": {
            "type": "choice",
            "instructions": "Which of the options best matches the message?",
            "criteria": {
                "1": "继续当前话题",
                "2": "转换场景",
                "3": "结束互动",
            },
        },
    }


async def decision_probe(
    base_url: str,
    api_key: str,
    model: str,
    api_format: str = "auto",
    timeout: float = DECISION_TIMEOUT_S,
    post_fn: Callable | None = None,
) -> dict:
    """用一次真实判定调用测连通性，返回可直接渲染进面板的诊断结果。

    与 :func:`decision_evaluate` 的分工：那个是生产路径，任何失败都吞成
    None（fail-open，判定挂了不能拖住聊天）；这个是给人看的，**必须把失败
    原因原样带回来** —— 「连不上」和「连上了但回的不是决策格式」是两种完全
    不同的错，用户要据此改地址还是改模型 ID。

    返回值恒为 dict（不抛异常）::

        {"ok": bool, "stage": "config|network|http|format|parse|ok",
         "url": str, "format": str, "elapsed_ms": int,
         "message": str, "detail": str, "answers": {...}|None}

    ``stage`` 是给前端做文案分派的稳定标识，``message`` 是给用户看的中文
    结论，``detail`` 是原始异常/响应片段（截断，仅作排障线索）。
    """
    started = time.time()
    base = str(base_url or "").strip()
    model = str(model or "").strip()
    result: dict = {
        "ok": False,
        "stage": "config",
        "url": "",
        "format": "",
        "elapsed_ms": 0,
        "message": "",
        "detail": "",
        "answers": None,
    }

    if not base:
        result["message"] = "API 地址为空，请先填写服务地址（如 https://openrouter.ai/api/v1）"
        return result
    if not is_http_url(base):
        # Q1 修复：阶段仍是 config——这是「配置不对」，不是网络问题，
        # 前端按 stage 分派文案时要把用户引到地址输入框上。
        result["message"] = "API 地址必须以 http:// 或 https:// 开头，并带有主机名"
        result["detail"] = str(base)[:300]
        return result
    if not model:
        # 部分服务有默认模型，但决策模型基本都按 model 路由；空着大概率 400，
        # 与其发出去等 4xx，不如在本地就说清楚。
        result["message"] = "模型 ID 为空，请填写要测试的决策模型（如 inception/mercury-decide:free）"
        return result

    fmt = resolve_format(base, api_format)
    url = resolve_decision_endpoint(base, fmt)
    result["url"] = url
    result["format"] = fmt

    headers = build_headers(api_key, url)
    payload = build_request_body(
        fmt, model, {"probe": "connectivity test"}, _probe_questions(), url
    )
    post = post_fn or _post_json

    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(post, url, headers, payload, timeout),
            timeout=timeout + 2.0,
        )
    except asyncio.TimeoutError:
        result["stage"] = "network"
        result["message"] = f"连接超时（{timeout:.0f} 秒内无响应）"
        result["detail"] = "检查网络/代理，或该地址是否需要走内网"
    except urllib.error.HTTPError as e:
        result["stage"] = "http"
        result["message"] = f"服务返回 HTTP {e.code}"
        result["detail"] = _read_error_body(e)
    except urllib.error.URLError as e:
        result["stage"] = "network"
        result["message"] = "无法连接到该地址"
        result["detail"] = str(getattr(e, "reason", e))[:300]
    except Exception as e:  # noqa: BLE001 — 任何异常都要变成可读结论
        result["stage"] = "network"
        result["message"] = f"请求失败：{type(e).__name__}"
        result["detail"] = str(e)[:300]
    else:
        answers = normalize_answers(raw)
        if answers is None:
            result["stage"] = "format"
            result["message"] = "服务有响应，但回的不是决策模型格式"
            result["detail"] = _truncate_json(raw)
        else:
            result["ok"] = True
            result["stage"] = "ok"
            result["message"] = "连接正常，判定模型已正确响应"
            result["answers"] = _summarize_probe_answers(answers)

    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def _read_error_body(e: urllib.error.HTTPError) -> str:
    """把 HTTPError 的响应体读成一行文本（服务端的错误说明通常比状态码有用）。"""
    try:
        raw = e.read()
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    except Exception:
        return str(getattr(e, "reason", e))[:300]
    return text.strip()[:300]


def _truncate_json(raw: Any) -> str:
    """把非决策格式的响应压成一行，供用户判断对面到底是什么服务。"""
    try:
        return json.dumps(raw, ensure_ascii=False)[:300]
    except Exception:
        return str(raw)[:300]


def _summarize_probe_answers(answers: dict) -> dict:
    """把归一化后的答案压成「人能一眼看懂」的摘要。

    只保留结论量（noul 概率 / 选中项 / 置信度），不打全量概率分布——
    测试按钮要回答的是「通不通」，不是「分布长什么样」。
    """
    out: dict = {}
    resp = answers.get("responding")
    if isinstance(resp, dict):
        out["greeting_probability"] = round(_safe_float(resp.get("noul")), 4)
    branch = answers.get("branch")
    if isinstance(branch, dict):
        probs = branch.get("probabilities")
        if isinstance(probs, dict):
            out["probabilities"] = {
                str(k): round(_safe_float(v), 4) for k, v in probs.items()
            }
        pick = branch.get("choice")
        if pick is not None:
            out["choice"] = pick
        out["confidence"] = round(_safe_float(branch.get("confidence")), 4)
    return out


# ── 剧情走向：问题与 state 构造 ─────────────────────────────────


def build_plot_questions(options: list[str]) -> dict:
    """剧情走向两问（官方推荐的原子化设计）。

    - ``responding``（Noul/predicate）：用户是否在响应分支提示？< 0.5 时整体
      跳过——用户无视选项自由 RP 时不误路由、不标误导性百分比；
    - ``branch``（Choice）：选了哪个分支，criteria 键即选项序号，
      值为选项原文（中文选项名直接做判据；指令用英文写——决策模型主训练
      语言是英语，CJK 内容保留在 state 与选项原文里）。
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


# ── 用户显式数字选择（不走判定模型）─────────────────────────────

_NUMERIC_PICK_RE = re.compile(r"^([1-9])(?:[.、)）]|\s*$)")


def parse_numeric_pick(text: str, n_options: int) -> int | None:
    """显式数字选择（"1"、"2."、"3、"）→ 选项序号（1 基）；否则 None。

    显式数字零成本零风险，直接精确匹配；只有自由文本/模糊输入才交给判定模型。
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


# ── 推荐选择度标注（第二档：百分比进正文块）──────────────────────

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
      没有信息量，硬标反而误导（"I don't know is a signal" 的用法）；
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
    """清理过期轮次缓存（阈值取 5 分钟 > 任何一轮的生命周期）。

    L19 修复：``float(v.get("ts", 0))`` 对 ts 是字符串/None 的脏条目直接抛
    TypeError，而唯一调用点（LLM 请求钩子）没有保护——一个脏条目就能让整轮
    请求钩子报错。这里对类型与数值都做守卫，不可解析的按「过期」处理。
    """
    now = time.time()
    stale = []
    for k, v in cache.items():
        if not isinstance(v, dict):
            stale.append(k)
            continue
        try:
            age = now - float(v.get("ts", 0))
        except (TypeError, ValueError):
            stale.append(k)
            continue
        if age > max_age_s:
            stale.append(k)
    for k in stale:
        cache.pop(k, None)
