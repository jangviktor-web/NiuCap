#!/usr/bin/env python3
"""
启动脚本 —— 供发布平台调用

端口解析优先级：
  1. 命令行参数  --port N  /  N（位置参数）
  2. 环境变量    PORT
  3. 默认 8899（与 .cloudstudio 平台配置一致；勿改回 8000，
     否则平台按 8899 探活会连不上）

始终绑定 0.0.0.0，保证发布平台/网关可访问。
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)              # 确保工作目录在 server/，保证模块可导入
sys.path.insert(0, HERE)

DEFAULT_PORT = 8899


def resolve_port(argv):
    """命令行 > 环境变量 > 默认。"""
    args = list(argv)
    # --port N / -p N
    for i, a in enumerate(args):
        if a in ("--port", "-p") and i + 1 < len(args):
            return int(args[i + 1])
        if a.startswith("--port="):
            return int(a.split("=", 1)[1])
    # 裸位置参数（纯数字）
    for a in args:
        if a.isdigit():
            return int(a)
    return int(os.environ.get("PORT", str(DEFAULT_PORT)))


def main():
    import uvicorn
    from app import app

    # stdout 重定向到文件时 Python 默认按块缓冲（4KB 才落盘），
    # 排障时日志总是滞后一大截。改成按行刷新，print 立即可见。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    port = resolve_port(sys.argv[1:])
    sys.stderr.write(f"[run] starting on 0.0.0.0:{port}\n")
    sys.stderr.flush()
    uvicorn.run(app, host="0.0.0.0", port=port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.stderr.flush()
        raise
