# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Activation Detector — boolean gate for prompt injection mode.
Ported from intimate_send, standalone (no astrbot imports).
"""

import os
import re
from typing import List, Set

import yaml

try:
    from astrbot.api import logger
except ModuleNotFoundError:  # 直接运行本文件做自测：先把 AstrBot 加入可导入路径
    # 包内加载走相对导入；`python <file>` 时无父包，退回顶层导入
    try:
        from ._astrbot_bootstrap import ensure_astrbot_importable
    except ImportError:
        from _astrbot_bootstrap import ensure_astrbot_importable
    ensure_astrbot_importable()
    from astrbot.api import logger


class ActivationDetector:
    """Determines if a user message triggers prompt injection mode.

    D4b（M3.2）激活判定 **fail-close** 契约：

    * **加载失败**（触发词文件缺失 / 解析失败 / 格式非法）只意味着「激活词
      通道降级为永不命中」，``should_activate`` / ``check_brackets`` 照常
      返回 False——即**不注入**，绝不允许退化为「每轮全量注入」的 fail-open
      （重构参考版 ``plugin.py`` 曾把 ``detector is None`` 顶成 True，移植
      时不得带回）。加载状态经 :attr:`load_ok` 暴露，供诊断与测试。
    * **检测过程异常**：捕获后按未激活处理（返回 False），warning 带异常链。
      理由（PLAN §M3.2）：fail-open 故障形态 = 每轮注入全部设定（最贵、
      用户可感刷屏注入报告）；fail-close = 该注入时没注入（下一轮恢复，
      偶发设定丢失）——后者更安全且自愈。
    """

    def __init__(self, yaml_path: str) -> None:
        self.yaml_path: str = yaml_path
        self.substring_words: List[str] = []
        self.exact_words: Set[str] = set()
        self._exact_patterns: List[re.Pattern] = []
        # 仅匹配 CJK 全角方括号【】。半角 [..] 在自然语言/代码/markdown 链接中
        # 误报率极高（如 [INFO]、[1, 2, 3]、[link](url)），不再作为激活触发。
        self._bracket_re: re.Pattern = re.compile(r'【.*?】')
        # 加载状态：True=触发词已就位；False=文件缺失/解析失败（激活词通道
        # 降级为不命中，见类 docstring 的 fail-close 契约）。
        self.load_ok: bool = False
        self._load()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.yaml_path):
            logger.warning("Activation triggers file not found: %s", self.yaml_path)
            self.load_ok = False
            return
        try:
            with open(self.yaml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            # 此前异常细节被整体吞掉（只记了文件名）；fail-close 的前提是
            # 故障可诊断——补上异常链。
            logger.warning("Failed to load activation triggers: %s (%s)", self.yaml_path, e, exc_info=True)
            self.load_ok = False
            return

        words = data.get('activation_words', [])
        if isinstance(words, list):
            self.substring_words = [str(w).strip().lower() for w in words if w and str(w).strip()]

        exact = data.get('exact_match_words', [])
        if isinstance(exact, list):
            self.exact_words = {str(w).strip().lower() for w in exact if w and str(w).strip()}

        self._exact_patterns = [
            re.compile(r'\b' + re.escape(w) + r'\b') for w in self.exact_words
        ]
        self.load_ok = True

    def reload(self) -> None:
        self.substring_words.clear()
        self.exact_words.clear()
        self._exact_patterns.clear()
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_activate(self, message: str) -> bool:
        """激活词/边界词判定；检测异常时按未激活处理（D4b fail-close）。"""
        if not message:
            return False
        try:
            msg = message.lower()
            for word in self.substring_words:
                if word in msg:
                    return True
            for pat in self._exact_patterns:
                if pat.search(msg):
                    return True
        except Exception as e:
            # 检测过程异常（输入形态异常等）→ 不激活。fail-close：该注入时
            # 没注入（下一轮自愈）好过每轮全量注入（fail-open，PLAN §M3.2）。
            logger.warning(
                "Activation detection error; treating as NOT activated (fail-close): %s",
                e, exc_info=True,
            )
            return False
        return False

    def check_brackets(self, message: str) -> bool:
        """【…】括号判定；检测异常时按未命中处理（D4b fail-close）。"""
        if not message:
            return False
        try:
            return bool(self._bracket_re.search(message))
        except Exception as e:
            logger.warning(
                "Bracket detection error; treating as NOT matched (fail-close): %s",
                e, exc_info=True,
            )
            return False

    def get_word_count(self) -> int:
        return len(self.substring_words) + len(self.exact_words)


# ======================================================================
# Self-tests
# ======================================================================
if __name__ == "__main__":
    import tempfile

    # 此处原本配置日志等级；插件统一用 astrbot.api 的 logger，无需再配置。

    SAMPLE_YAML = """\
activation_words:
  - 插入
  - 测试
exact_match_words:
  - love
  - romance
"""

    passed = 0
    failed = 0

    def check(label: str, got: bool, expect: bool) -> None:
        global passed, failed
        if got is expect:
            print(f"  PASS  {label}")
            passed += 1
        else:
            print(f"  FAIL  {label}  got={got} expect={expect}")
            failed += 1

    # Create temp YAML
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False, encoding='utf-8')
    tmp.write(SAMPLE_YAML)
    tmp.close()

    det = ActivationDetector(tmp.name)

    # 1. Substring match (Chinese)
    check("substring: '插入' in msg", det.should_activate("用插入的方式"), True)
    check("substring: no match", det.should_activate("你好世界"), False)

    # 2. Word-boundary (English) — "love" should NOT match "lovely"
    check("boundary: 'love' NOT in 'lovely'", det.should_activate("lovely day"), False)
    check("boundary: 'love' in 'I love you'", det.should_activate("I love you"), True)

    # 3. Brackets — only CJK 【】 activates; ASCII [..] is intentionally ignored
    check("brackets: 【测试】", det.check_brackets("【测试】"), True)
    check("brackets: ASCII [test] does NOT activate", det.check_brackets("[test]"), False)
    check("brackets: markdown link [a](b)", det.check_brackets("see [docs](http://x)"), False)
    check("brackets: log line [INFO]", det.check_brackets("[INFO] booting"), False)
    check("brackets: array [1, 2, 3]", det.check_brackets("x = [1, 2, 3]"), False)
    check("brackets: none", det.check_brackets("nothing here"), False)

    # 4. Empty / None
    check("empty string", det.should_activate(""), False)
    check("None input", det.should_activate(None) if det.should_activate.__code__.co_argcount == 1 else False, False)  # type: ignore[arg-type]
    try:
        det.should_activate(None)  # type: ignore[arg-type]
        check("None no crash", True, True)
    except Exception:
        check("None no crash", False, True)

    # 5. Word count
    check("word count", det.get_word_count(), 4)

    # 6. Reload
    det.reload()
    check("reload keeps words", det.get_word_count(), 4)

    # Cleanup
    os.unlink(tmp.name)

    print(f"\n{passed} passed, {failed} failed")
    exit(1 if failed else 0)
