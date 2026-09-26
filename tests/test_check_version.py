# -*- coding: utf-8 -*-
# Copyright (C) 2025 Nana7mi0721
# SPDX-License-Identifier: AGPL-3.0-or-later
"""版本号单源对拍器测试（v5.3.0 M4.1）。

tools/check_version.py 静态解析**仓库真实文件**，因此测试经根目录参数
传入 tmp_path 造出的样本树（quill/__init__.py + main.py + metadata.yaml
+ README.md），断言一致/不一致样本的返回值与报告内容。另有一条对拍
仓库自身的健全性用例（真源 = metadata = @register = README badge）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"

# tools/ 不是包（无 __init__.py），按文件路径加载，避免名字空间纠缠
_spec = importlib.util.spec_from_file_location(
    "quillplus_check_version", _TOOLS_DIR / "check_version.py"
)
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)

_VERSION = "5.3.0"
_DESC = "世界书+写作素材库+角色卡+文档RAG+动态记忆+状态栏 六合一沉浸式 RP 核心引擎 | 六级降级解析兜底 | 平行宇宙双轴隔离 | JSON 原子化状态机 | 无损断点续传"


# ── 样本树工厂 ────────────────────────────────────────────────────


def _make_sample(
    tmp_path: Path,
    *,
    quill_version: str = _VERSION,
    register_version: str = _VERSION,  # 传 "__import__" 表示单源导入形态
    register_desc: str = _DESC,
    metadata_version: str = _VERSION,
    metadata_desc: str = _DESC,
    readme_badge: str = _VERSION,
    main_py: str | None = None,
    quill_init: str | None = None,
) -> Path:
    """在 tmp_path 造一棵最小对拍样本树，返回仓库根目录。"""
    root = tmp_path / "repo"
    (root / "quill").mkdir(parents=True)

    if quill_init is None:
        quill_init = (
            '"""quill 包 docstring（样本）。"""\n\n'
            f'__version__ = "{quill_version}"\n'
        )
    (root / "quill" / "__init__.py").write_text(quill_init, encoding="utf-8")

    if main_py is None:
        if register_version == "__import__":
            version_arg = "__version__"
            import_line = "from .quill import __version__\n"
        else:
            version_arg = f'"{register_version}"'
            import_line = ""
        main_py = (
            f"{import_line}"
            "from astrbot.api.star import register\n\n"
            "@register(\n"
            '    "astrbot_plugin_quillplus",\n'
            '    "Author",\n'
            f'    "{register_desc}",\n'
            f"    {version_arg},\n"
            '    "https://example.com/repo",\n'
            ")\n"
            "class QuillPlugin:\n"
            '    """插件类（样本）。"""\n'
        )
    (root / "main.py").write_text(main_py, encoding="utf-8")

    (root / "metadata.yaml").write_text(
        "name: astrbot_plugin_quillplus\n"
        f'version: "{metadata_version}"\n'
        f'desc: "{metadata_desc}"\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        f"[![Version](https://img.shields.io/badge/version-{readme_badge}-green.svg)]()\n",
        encoding="utf-8",
    )
    return root


# ── 一致样本 ──────────────────────────────────────────────────────


def test_consistent_sample_import_form_passes(tmp_path):
    root = _make_sample(tmp_path, register_version="__import__")
    ok, lines = cv.check(root)
    assert ok, "\n".join(lines)
    text = "\n".join(lines)
    assert "唯一真源" in text
    assert "自 quill 单源导入" in text
    assert "desc 一致" in text
    assert cv.main([str(root)]) == 0


def test_consistent_sample_literal_form_passes(tmp_path):
    root = _make_sample(tmp_path, register_version=_VERSION)
    ok, lines = cv.check(root)
    assert ok, "\n".join(lines)
    assert any("字面量" in ln for ln in lines)


def test_readme_without_badge_is_skipped_not_failed(tmp_path):
    root = _make_sample(tmp_path)
    (root / "README.md").write_text("没有任何 badge 的 README\n", encoding="utf-8")
    ok, lines = cv.check(root)
    assert ok, "\n".join(lines)
    assert any("SKIP" in ln for ln in lines)


# ── 不一致样本 ────────────────────────────────────────────────────


def test_metadata_version_mismatch_fails(tmp_path):
    root = _make_sample(tmp_path, metadata_version="5.2.5")
    ok, lines = cv.check(root)
    assert not ok
    assert cv.main([str(root)]) == 1
    assert any("5.2.5" in ln and "5.3.0" in ln for ln in lines)


def test_register_literal_mismatch_fails(tmp_path):
    root = _make_sample(tmp_path, register_version="5.0.6")
    ok, lines = cv.check(root)
    assert not ok
    assert any("5.0.6" in ln for ln in lines)


def test_source_version_mismatch_fails(tmp_path):
    root = _make_sample(tmp_path, quill_version="5.4.0")
    ok, lines = cv.check(root)
    assert not ok
    # 真源 5.4.0 与其余三处（@register/metadata/README badge）均报不一致
    # （末行汇总也含 "≠ 真源" 字样，故排除汇总行后计数）
    fail_lines = [ln for ln in lines if ln.startswith("FAIL") and "版本对拍：" not in ln]
    assert sum("≠ 真源" in ln for ln in fail_lines) == 3
    assert any("README badge" in ln for ln in lines)


def test_readme_badge_mismatch_fails(tmp_path):
    root = _make_sample(tmp_path, readme_badge="5.2.5")
    ok, _ = cv.check(root)
    assert not ok


def test_register_desc_mismatch_fails(tmp_path):
    root = _make_sample(tmp_path, register_desc="羽笔 v5.0 — 五合一（漂移）")
    ok, lines = cv.check(root)
    assert not ok
    assert any("desc 不一致" in ln for ln in lines)


# ── 解析失败样本 ──────────────────────────────────────────────────


def test_missing_dunder_version_fails(tmp_path):
    root = _make_sample(
        tmp_path, quill_init='"""只有 docstring，没有版本。"""\n'
    )
    ok, lines = cv.check(root)
    assert not ok
    assert any("真源解析失败" in ln for ln in lines)


def test_unresolvable_register_version_fails(tmp_path):
    # version 参数是动态表达式（无法静态解析）→ 判失败，宁误报不放过
    root = _make_sample(
        tmp_path,
        main_py=(
            "import os\n"
            "from astrbot.api.star import register\n\n"
            "@register(\n"
            '    "astrbot_plugin_quillplus",\n'
            '    "Author",\n'
            f'    "{_DESC}",\n'
            '    os.environ.get("QUILL_VERSION", "5.3.0"),\n'
            '    "https://example.com/repo",\n'
            ")\n"
            "class QuillPlugin:\n"
            '    """样本。"""\n'
        ),
    )
    ok, lines = cv.check(root)
    assert not ok
    assert any("未取到 version 参数" in ln for ln in lines)


def test_name_without_single_source_import_fails(tmp_path):
    # 引用 __version__ 但没有 from .quill import —— 单源链路断裂
    root = _make_sample(
        tmp_path,
        main_py=(
            "from astrbot.api.star import register\n"
            "__version__ = \"9.9.9\"\n\n"  # 本地定义，并非单源导入
            "@register(\n"
            '    "astrbot_plugin_quillplus",\n'
            '    "Author",\n'
            f'    "{_DESC}",\n'
            "    __version__,\n"
            '    "https://example.com/repo",\n'
            ")\n"
            "class QuillPlugin:\n"
            '    """样本。"""\n'
        ),
    )
    ok, lines = cv.check(root)
    assert not ok
    assert any("不可静态解析的引用" in ln for ln in lines)


# ── 仓库自身健全性 ────────────────────────────────────────────────


def test_real_repo_is_consistent():
    repo_root = Path(__file__).resolve().parents[1]
    ok, lines = cv.check(repo_root)
    assert ok, "\n".join(lines)
