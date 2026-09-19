"""闲鱼助手 - 入口"""
import os
import sys
import threading
import time
import logging

from . import config, db
from .browser import BrowserSession
from .rules import ReplyEngine
from .listener import MessageListener
from .deliverer import Deliverer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.FileHandler(config.LOG_DIR / "app.log", encoding="utf-8")]
    + ([logging.StreamHandler()] if sys.stderr is not None else []),
)
log = logging.getLogger("xianyu")


def _install_console_close_handler():
    """Windows 控制台关闭处理：关闭 CMD 窗口（CTRL_CLOSE_EVENT）时，
    先优雅停止监听线程并关闭浏览器（Playwright 清理 chromium/driver 子进程）再退出，
    避免弹出"Windows 无法结束此程序"并反复出现（残留子进程持有控制台句柄）。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        def _handler(ctrl_type):
            try:
                from .listener import get_active_listener
                inst = get_active_listener()
                if inst is not None:
                    inst.stop()          # 监听循环退出（其 finally 会关闭浏览器）
                    time.sleep(3)        # 给 Playwright 清理 chromium/driver 的时间
            except Exception:
                pass
            try:
                os._exit(0)
            except Exception:
                pass
            return 1

        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler, True)
        log.info("已注册控制台关闭处理（关闭窗口将优雅退出，避免残留进程弹窗）")
    except Exception as e:
        log.warning(f"控制台关闭处理注册失败: {type(e).__name__} {e}")


def main():
    _install_console_close_handler()
    db.init_db()
    from .api import app, browser, reply_engine
    import uvicorn

    def start_listener_guard():
        # listener 依赖 M0#2 结论；未实现时仅告警，不阻塞 API
        try:
            from .api import browser as b, reply_engine as re
            d = Deliverer(b)
            l = MessageListener(b, re, d)
            l.start()
        except NotImplementedError as e:
            db.log_op("warn", "main", f"{e}")

    threading.Thread(target=start_listener_guard, daemon=True).start()

    # log_config=None：不再让 uvicorn 重建日志（其在无控制台/pythonw 下会崩），
    # 使用应用自身 logging（FileHandler + 可选 StreamHandler）配置
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info", log_config=None)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback
        try:
            with open(config.LOG_DIR / "crash.log", "a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception:
            pass
        raise
