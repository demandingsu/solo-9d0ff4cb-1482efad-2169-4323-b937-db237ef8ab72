"""外字号清单 / 对照表 CSV 解析。

排版组交《外字号清单》：每行一个外字号（表头“外字号”，兼容无表头）。
校勘组交《对照表》：每行外字号 + 对应单字（表头“外字号,字”，必须有表头）。
同一编号在不同稿里可能代表不同的字，因此两份表均按 (册, 稿次) 分别收。
"""
from __future__ import annotations

import csv
import io

from .errors import DomainError

LIST_ALIASES = ("外字号", "外字编号", "编号", "code", "matrix_no", "no")
MAP_CODE_ALIASES = ("外字号", "外字编号", "编号", "code", "matrix_no", "no")
MAP_CHAR_ALIASES = ("字", "单字", "用字", "字符", "glyph", "char", "character")


def decode_bytes(data: bytes) -> str:
    """优先 UTF-8（含 BOM），回退 GB18030；全空按空文件处理。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DomainError("文件编码无法识别，请使用 UTF-8 或 GBK 编码的 CSV")


def _normalize(header: str) -> str:
    return (header or "").strip().lstrip("﻿").lower()


def _find_column(fieldnames: list[str], aliases: tuple[str, ...]) -> int | None:
    normalized = [_normalize(h) for h in fieldnames]
    for name in aliases:
        needle = name.lower()
        if needle in normalized:
            return normalized.index(needle)
    return None


def _read_rows(text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text))
    return [row for row in reader if row and any(c.strip() for c in row)]


def parse_list_csv(data: bytes) -> list[str]:
    """外字号清单 → 按文件顺序去重后的外字号列表。

    有“外字号”表头时按列读取；没有表头时把每行首格当作一个外字号。
    """
    text = decode_bytes(data)
    rows = _read_rows(text)
    if not rows:
        raise DomainError("外字号清单为空")

    first = rows[0]
    idx = _find_column(first, LIST_ALIASES)
    codes: list[str] = []
    data_rows = rows[1:] if idx is not None else rows
    if idx is None:
        idx = 0
    for line_no, raw in enumerate(data_rows, start=2 if idx is not None else 1):
        if idx >= len(raw):
            raise DomainError(f"外字号清单第 {line_no} 行列数不足")
        code = raw[idx].strip()
        if not code:
            raise DomainError(f"外字号清单第 {line_no} 行存在空外字号")
        if code not in codes:
            codes.append(code)
    if not codes:
        raise DomainError("外字号清单没有任何数据行")
    return codes


def parse_mapping_csv(data: bytes) -> list[tuple[str, str]]:
    """对照表 → (外字号, 单字) 行列表，保留文件顺序；编号与单字均不允许重复。"""
    text = decode_bytes(data)
    rows = _read_rows(text)
    if not rows:
        raise DomainError("对照表为空")

    header = rows[0]
    code_idx = _find_column(header, MAP_CODE_ALIASES)
    char_idx = _find_column(header, MAP_CHAR_ALIASES)
    if code_idx is None or char_idx is None:
        raise DomainError("对照表缺少必需列，表头需含 外字号,字")

    pairs: list[tuple[str, str]] = []
    seen_codes: set[str] = set()
    seen_chars: set[str] = set()
    for line_no, raw in enumerate(rows[1:], start=2):
        if code_idx >= len(raw) or char_idx >= len(raw):
            raise DomainError(f"对照表第 {line_no} 行列数不足")
        code = raw[code_idx].strip()
        char = raw[char_idx].strip().replace(" ", "")
        if not code or not char:
            raise DomainError(f"对照表第 {line_no} 行存在空值")
        if len(char) != 1:
            raise DomainError(f"对照表第 {line_no} 行的“字”必须是单个字符: {char!r}")
        if code in seen_codes:
            raise DomainError(f"对照表第 {line_no} 行外字号重复: {code}")
        if char in seen_chars:
            raise DomainError(f"对照表第 {line_no} 行单字重复: {char}（一个字只能对应一个外字号）")
        seen_codes.add(code)
        seen_chars.add(char)
        pairs.append((code, char))
    if not pairs:
        raise DomainError("对照表没有任何数据行")
    return pairs


def parse_legacy_csv(data: bytes) -> tuple[str, int]:
    """原用字表：兼容有/无表头，按单元格收字（一格可多字），返回 (全文, 字数)。"""
    text = decode_bytes(data)
    rows = _read_rows(text)
    if not rows:
        raise DomainError("原用字表为空")
    start = 1 if _find_column(rows[0], MAP_CHAR_ALIASES) is not None else 0
    chars: list[str] = []
    for raw in rows[start:]:
        for cell in raw:
            chars.extend(list(cell.strip().replace(" ", "")))
    if not chars:
        raise DomainError("原用字表没有任何字")
    return "".join(chars), len(chars)
