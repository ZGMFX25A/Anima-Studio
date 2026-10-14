"""前端服务入口：启动页面服务、ComfyUI 引擎与绘画模块；按 Enter 关闭全部。"""
import logging
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import config, procs

URL = f"http://{config.HOST}:{config.PORT}"


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(config.LOGS_DIR / "frontend.log", encoding="utf-8")])
    for noisy in ("httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("frontend")

    if _port_busy(config.PORT):
        log.info("前端已在运行：%s（已打开浏览器）", URL)
        webbrowser.open(URL)
        return

    from .app import app  # noqa: E402  导入时会初始化数据库

    procs.ADOPT_INTO_JOB = True
    from . import gallery  # noqa: E402
    n = gallery.cleanup_refs(min_age=7 * 86400)   # 上传后 7 天仍未被任何记录引用的参考图
    if n:
        log.info("已清理 %d 张未使用的参考图", n)
    from . import personas  # noqa: E402
    n = personas.migrate_legacy_covers()          # 旧版 webp 封面副本 → 原图无损 PNG（只处理一次）
    if n:
        log.info("已用原图重建 %d 个人设的无损封面", n)

    server = uvicorn.Server(uvicorn.Config(app, host=config.HOST, port=config.PORT, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.1)
    log.info("前端页面：%s", URL)

    procs.start_all()
    if not os.environ.get("ANIMA_NO_BROWSER"):
        webbrowser.open(URL)

    def report():
        if procs.COMFY.wait_ready(procs.COMFY.start_timeout) and procs.PAINTER.wait_ready(60):
            log.info("全部就绪。按 Enter 关闭前端、绘画模块与 ComfyUI。")

    threading.Thread(target=report, daemon=True).start()
    print("\n  按 Enter 关闭全部服务（直接关闭本窗口也会一并结束）\n", flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    log.info("正在关闭……")
    procs.stop_all()
    server.should_exit = True
    t.join(timeout=10)
    log.info("已全部关闭。")


if __name__ == "__main__":
    main()
