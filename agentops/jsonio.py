# -*- coding: utf-8 -*-
"""结果 JSON 统一写出：浮点统一保留 4 位小数。

原因：全精度浮点会产出 `0.19293885830658355` 这类长数字串，
既不利于对比，也会在敏感信息扫描里被误判为手机号/长标识。
"""
from __future__ import annotations

import json
from pathlib import Path


def round_deep(obj, ndigits: int = 4):
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: round_deep(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [round_deep(v, ndigits) for v in obj]
    return obj


def dump(path: str | Path, obj, ndigits: int = 4) -> None:
    """写出取整后的 JSON（UTF-8、缩进 2）。"""
    Path(path).write_text(
        json.dumps(round_deep(obj, ndigits), ensure_ascii=False, indent=2),
        encoding="utf-8")