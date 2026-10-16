#!/usr/bin/env python3.11
"""Playwright 截图 → 落盘为 WebP（质量 80）。

Playwright 原生 screenshot 不支持 image/webp 输出，所以先截成临时 PNG，
再用 Pillow 压成 WebP。调用方式与 page.screenshot 几乎一致：

    from webp_shot import shoot
    shoot(page, "docs/shot-x.webp")                 # 直接给 .webp 路径
    shoot(page, "docs/shot-x.webp", full_page=True) # 透传其余参数
"""
import os
from PIL import Image


def shoot(page, path, quality=80, **kw):
    tmp = path[:-5] + ".png" if str(path).lower().endswith(".webp") else path
    page.screenshot(path=tmp, **kw)
    if tmp != path:
        im = Image.open(tmp)
        if im.mode == "P":
            im = im.convert("RGBA")
        im.save(path, "WEBP", quality=quality, method=6)
        os.remove(tmp)
    return path
