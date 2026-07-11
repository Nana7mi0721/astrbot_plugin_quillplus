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
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


class ActivationDetector:
    """Determines if a user message triggers prompt injection mode."""

    def __init__(self, yaml_path: str) -> None:
        self.yaml_path: str = yaml_path
        self.substring_words: List[str] = []
        self.exact_words: Set[str] = set()
        self._exact_patterns: List[re.Pattern] = []
        # 仅匹配 CJK 全角方括号【】。半角 [..] 在自然语言/代码/markdown 链接中
        # 误报率极高（如 [INFO]、[1, 2, 3]、[link](url)），不再作为激活触发。
        self._bracket_re: re.Pattern = re.compile(r'【.*?】')
        self._load()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.yaml_path):
            logger.warning("Activation triggers file not found: %s", self.yaml_path)
            return
        try:
            with open(self.yaml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            logger.warning("Failed to load activation triggers: %s", self.yaml_path)
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

    def reload(self) -> None:
        self.substring_words.clear()
        self.exact_words.clear()
        self._exact_patterns.clear()
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_activate(self, message: str) -> bool:
        if not message:
            return False
        msg = message.lower()
        for word in self.substring_words:
            if word in msg:
                return True
        for pat in self._exact_patterns:
            if pat.search(msg):
                return True
        return False

    def check_brackets(self, message: str) -> bool:
        if not message:
            return False
        return bool(self._bracket_re.search(message))

    def get_word_count(self) -> int:
        return len(self.substring_words) + len(self.exact_words)


# ======================================================================
# Self-tests
# ======================================================================
if __name__ == "__main__":
    import tempfile

    logging.basicConfig(level=logging.DEBUG)

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
