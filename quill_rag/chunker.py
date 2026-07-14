# -*- coding: utf-8 -*-
"""文档分块：按段落优先、固定长度兜底的分块策略。"""

from __future__ import annotations

import re


# 句子结束符：中文句号、问号、感叹号、英文句号、问号、感叹号
_SENTENCE_END_RE = re.compile(r'[。！？.!?]\s*')


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """将文本分块。

    策略：
    1. 先按段落（空行）切分
    2. 超过 chunk_size 的段落再按固定长度切分
    3. 相邻块之间有 overlap 字符的重叠

    Args:
        text: 原始文本
        chunk_size: 每个块的字符数上限
        overlap: 相邻块的重叠字符数

    Returns:
        文本块列表
    """
    if not text:
        return []

    # 按段落切分
    paragraphs = re.split(r'\n\s*\n', text.strip())
    chunks = []
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # 如果当前块加上新段落不超过上限，则合并（+2 算上换行符）
        if len(current_chunk) + len(para) + 2 <= chunk_size:
            if current_chunk:
                current_chunk += "\n\n" + para
            else:
                current_chunk = para
        else:
            # 超过上限，先把当前累积的块存起来
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""

            # 处理这个新的段落
            if len(para) > chunk_size:
                # 单个段落本身就超长，执行句子边界优先的切分
                sentences = _SENTENCE_END_RE.split(para)
                current = ""
                for sent in sentences:
                    sent = sent.strip()
                    if not sent:
                        continue
                    if len(current) + len(sent) + 1 <= chunk_size:
                        current = (current + " " + sent) if current else sent
                    else:
                        if current:
                            chunks.append(current)
                        # 单句超长时退回固定长度切分
                        if len(sent) > chunk_size:
                            start = 0
                            while start < len(sent):
                                end = start + chunk_size
                                chunk = sent[start:end].strip()
                                if chunk:
                                    chunks.append(chunk)
                                start += chunk_size - overlap
                            current = ""
                        else:
                            current = sent
                if current:
                    chunks.append(current)
            else:
                # 单个段落没有超长，作为下一个块的开头
                current_chunk = para

    # 收尾
    if current_chunk:
        chunks.append(current_chunk)

    return chunks
