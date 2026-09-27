"""数据源适配层。

把「从哪取数据」与「怎么用数据」解耦。目前包含：

    eltdx_source —— 通达信 7709 协议（快，但许可证限非商业用途）

调用方应通过本包提供的统一入口取数，而不是直接 import 某个具体实现，
这样将来换源 / 摘除某个源时只需改这里。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from . import eltdx_source

# 总开关：设为 0 可强制关闭 eltdx，全部回落到原数据源。
# 这对「许可证合规排查」「怀疑 eltdx 导致数据异常」两种场景很有用。
_ENABLED = os.environ.get("TICK_ELTDX", "1") not in ("0", "false", "False", "")


def eltdx_enabled() -> bool:
    """eltdx 是否启用（总开关打开 且 依赖已安装）。"""
    return _ENABLED and eltdx_source.available()


__all__ = ["eltdx_source", "eltdx_enabled"]
