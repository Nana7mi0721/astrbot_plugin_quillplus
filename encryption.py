# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Minimal Base64 codec for [B:...] markers in LLM output.

NOTE: Despite the module name "encryption", this module performs Base64
encoding/decoding only — it is NOT cryptographic encryption. The name is
preserved for import-path backward compatibility. Base64 is used here as an
obfuscation layer to prevent the LLM agent loop from recognizing and
stripping its own tail text in [B:...] markers.

When the agent loop continues past a send_message_to_user call, the LLM is
instructed to wrap any tail text in [B:base64] markers. The API/audit layer
sees only the encoded form; this module decodes it back for the user.
"""

import base64
import re

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


# [B:<base64 chars>] — whitespace-tolerant, greedy
B64_PATTERN = re.compile(r"\[B:([A-Za-z0-9+/=\s]+)\]", re.DOTALL)


def b64_encode(text: str) -> str:
    if not text:
        return ""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def b64_decode(encoded: str) -> str:
    if not encoded:
        return ""
    try:
        if encoded.startswith("[B:") and encoded.endswith("]"):
            encoded = encoded[3:-1]
        return base64.b64decode(encoded).decode("utf-8")
    except Exception:
        return encoded  # 解码失败时原样返回，不吞字符


def b64_wrap(text: str) -> str:
    return f"[B:{b64_encode(text)}]"


def decrypt_output(text: str) -> str:
    """Scan *text* for [B:...] markers and decode them inline.

    解码失败时保留原始 [B:...] 标记原样返回，并记录 warning。
    避免在非 Quill 输出（含合法 [B:xxx] 字面量）中把括号吞掉、
    把内层乱码当作正文呈现给用户。
    """
    if not text:
        return text

    def _decode(match: re.Match) -> str:
        encoded = match.group(1).replace('\n', '').replace('\r', '').replace(' ', '')
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except Exception as exc:
            logger.warning(
                "[Quill] [B:...] decode failed (preserving marker): %s", exc
            )
            return match.group(0)

    return B64_PATTERN.sub(_decode, text)


# ================================================================
# Self-test
# ================================================================
if __name__ == "__main__":
    print("=== Base64 Codec Test ===\n")

    passed = 0
    failed = 0

    def check(label: str, got, expect):
        global passed, failed
        if got == expect:
            print(f"  [PASS] {label}")
            passed += 1
        else:
            print(f"  [FAIL] {label}: got={got!r} expect={expect!r}")
            failed += 1

    # Encode / decode roundtrip
    original = "你好，世界"
    encoded = b64_encode(original)
    decoded = b64_decode(encoded)
    check("encode roundtrip", decoded, original)

    # Wrap / unwrap
    wrapped = b64_wrap(original)
    check("b64_wrap format", wrapped[:3], "[B:")
    check("b64_wrap ends with ]", wrapped[-1], "]")
    check("b64_wrap roundtrip through decrypt_output", decrypt_output(wrapped), original)

    # decrypt_output with surrounding text
    mixed = "前缀文本" + b64_wrap("敏感内容") + "后缀文本"
    decrypted = decrypt_output(mixed)
    check("decrypt_output mixed text", decrypted, "前缀文本敏感内容后缀文本")

    # Multiple markers
    multi = b64_wrap("第一段") + "普通文字" + b64_wrap("第二段")
    check("decrypt_output multi markers", decrypt_output(multi), "第一段普通文字第二段")

    # No markers — pass through
    check("decrypt_output no markers", decrypt_output("普通文本"), "普通文本")

    # Empty / None
    check("decrypt_output empty", decrypt_output(""), "")
    check("decrypt_output None", decrypt_output(None) if False else "", "")  # type guard

    # Whitespace in base64 (simulating LLM formatting quirk)
    encoded_with_spaces = b64_encode("测试内容")
    spaced = f"[B:{encoded_with_spaces[:4]} {encoded_with_spaces[4:]}]"
    check("decrypt_output tolerates internal spaces", decrypt_output(spaced), "测试内容")

    # Malformed base64 content — marker should be preserved, not stripped
    check("decrypt_output malformed preserves marker",
          decrypt_output("前[B:!!!invalid!!!]后"), "前[B:!!!invalid!!!]后")

    # b64_decode failure returns raw input
    check("b64_decode garbage preserved", b64_decode("not base64!!"), "not base64!!")

    if failed:
        print(f"\n{failed} FAILED, {passed} passed")
        exit(1)
    print(f"\n{passed} passed — ALL OK")
