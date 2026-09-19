"""闲鱼助手 - 浏览器会话管理（Playwright，登录态持久化）

M0 已确认：
  - 登录流程：goofish.com 首页"登录" -> 模态框 iframe passport.goofish.com/mini_login.htm
    -> 闲鱼 App 直接扫码（无需跳转淘宝）
  - 反爬：headless 必须带真实 Chrome UA + 指纹伪装，否则被"非法访问"拦截

线程模型（重要）：
  Playwright 线程亲和 —— 浏览器只能在"属主线程"内创建与访问。
  其他线程（如 FastAPI API 线程）只能读取缓存状态 cached_status，禁止直接调用 ctx。
"""
import os
import threading
import time

from . import config
from . import db

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

LOGIN_COOKIE_NEEDED = {"unb", "cookie2", "tracknick"}


class BrowserSession:
    """单例：一个 Playwright 浏览器上下文，负责登录态与页面操作。"""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._login_qr_path = config.DATA_DIR / "login_qr.png"
        self._cached_status = "idle"   # idle/logging_in/online/offline/error（仅属主线程写）
        self._owner_tid = None
        self.last_error = ""
        self._offline_until = 0.0      # 服务端验证判过期后的维持窗口（防被本地 cookie 检查闪回 online）
        self._qr_refresh_stop = threading.Event()
        self._pending_session_cookies = None   # 弹出登录成功后待属主线程应用的 Cookie

    # ---------- 生命周期（必须由属主线程调用） ----------
    def ensure_browser(self):
        if self._ctx is not None:
            return
        from playwright.sync_api import sync_playwright
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.BROWSERS_DIR))
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        state_path = str(config.SESSION_STATE_FILE) if config.SESSION_STATE_FILE.exists() else None
        self._ctx = self._browser.new_context(
            viewport={"width": 1280, "height": 900}, user_agent=UA,
            storage_state=state_path,  # 加载已保存会话态（登录态复用）
        )
        self._ctx.add_init_script(STEALTH)
        self._page = self._ctx.new_page()
        self._owner_tid = threading.get_ident()
        self.refresh_status()

    def _is_owner(self):
        return self._owner_tid is None or threading.get_ident() == self._owner_tid

    def refresh_status(self):
        """属主线程调用：从 Cookie 刷新缓存状态。其他线程调用不生效（读缓存）。
        登录流程中(logging_in)：只允许升级为 online，不被降级（否则扫码界面会闪没）。
        服务端验证判过期后（_offline_until 窗口内）维持 offline：本地 cookie 仍在
        但服务端已过期时，避免状态被闪回 online（"假在线"）。"""
        if not self._is_owner() or self._ctx is None:
            return self._cached_status
        try:
            names = {c["name"] for c in self._ctx.cookies()}
            ok = LOGIN_COOKIE_NEEDED.issubset(names)
        except Exception:
            return self._cached_status
        if self._cached_status == "logging_in":
            if ok:
                self._cached_status = "online"
            return self._cached_status
        if ok:
            if self._cached_status == "online":
                return "online"
            if time.time() < self._offline_until:
                return "offline"  # 服务端已判过期，等下一次验证/重新登录成功
            self._cached_status = "online"
        else:
            self._cached_status = "offline"
        return self._cached_status

    def verify_session_live(self):
        """属主线程调用：服务端真实验证会话（mtop 探测）。
        过期 → 状态置 offline/error 并返回 False；有效 → 保持/置 online 返回 True。
        探测失败（网络等）不改状态，返回 None。"""
        if not self._is_owner() or self._ctx is None or self._cached_status != "online":
            return None
        from .mtop import check_session
        try:
            cookies = self._ctx.cookies()
            from .mtop import build_cookie_header
            cookie_str = build_cookie_header(cookies)
        except Exception:
            return None
        try:
            ok = check_session(cookie_str)
        except Exception:
            return None
        if ok is False:
            self._cached_status = "offline"
            self._offline_until = time.time() + 75   # 维持离线提示，直到下次验证/重新登录
            self.last_error = "会话已过期，请重新扫码登录"
            db.log_op("warn", "browser", "会话服务端已过期（本地 cookie 仍在），状态置为 offline")
            return False
        if ok is True:
            self.last_error = ""
            if self._cached_status == "online":
                return True
            self._cached_status = "online"
            return True
        return None

    def is_logged_in(self):
        return self._cached_status == "online"

    def close(self):
        self._qr_refresh_stop.set()
        if not self._is_owner():
            return  # 非属主线程只请求停止，不碰浏览器对象
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._browser = self._ctx = self._page = None
        self._pw = None
        self._cached_status = "idle"

    # ---------- 登录（属主线程调用） ----------
    def start_login(self):
        """弹出「闲鱼登录窗口」扫码登录（有头浏览器，全新会话，必定显示登录页）。

        流程：新开有头 Chromium 窗口（无旧会话）→ goofish 登录框 → 用户在窗口内
        用闲鱼 App 扫码 → 成功后把新会话 Cookie 同步进主监听会话并保存会话态。
        必须在属主线程调用（由监听线程经任务队列执行）；窗口弹出期间会阻塞监听循环，
        直到登录成功或超时（可接受，登录是低频人工操作）。"""
        if not self._is_owner():
            return {"ok": False, "error": "登录接口需在属主线程调用（通过 Web 页状态查看）"}
        self.ensure_browser()
        if self.is_logged_in():
            return {"ok": True, "already": True}
        self._cached_status = "logging_in"
        db.log_op("info", "browser", "正在弹出闲鱼登录窗口（请在弹出窗口内用闲鱼 App 扫码）")
        threading.Thread(target=self._popup_login_worker, daemon=True).start()
        return {"ok": True, "note": "已弹出登录窗口，请在窗口中扫码（Web 页会轮询状态）"}

    def _popup_login_worker(self):
        """独立线程运行有头登录窗口（自带 playwright 实例，避免与监听线程实例冲突）。
        登录成功：保存会话态文件，并把新 Cookie 交给属主线程应用（pending）。"""
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            bro = pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = bro.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
            ctx.add_init_script(STEALTH)
            page = ctx.new_page()
            page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(6)
            # 点击页头登录，打开闲鱼原生登录框
            try:
                loc = page.locator("a.item--m9jSTUup").first
                if loc.count() > 0:
                    loc.click(force=True, timeout=8000)
            except Exception:
                page.evaluate('''() => {
                    const as = Array.from(document.querySelectorAll('a'));
                    for (const a of as) {
                        if ((a.innerText||'').trim() === '登录') { a.click(); return true; }
                    }
                    return false;
                }''')
            deadline = time.time() + 30
            while time.time() < deadline:
                if any("passport.goofish.com" in f.url and "mini_login" in f.url
                       for f in page.frames):
                    break
                time.sleep(2)
            # 等待扫码（默认最长 30 分钟，可用 XY_LOGIN_WAIT 调整）；
            # 期间若登录框被收起/二维码过期，自动重新唤起登录框，避免"来不及扫码就被收起"
            wait_sec = int(getattr(config, "LOGIN_WAIT_SEC", 1800) or 1800)
            deadline = time.time() + wait_sec
            logged_in = False
            last_log = 0.0
            last_repair = 0.0
            db.log_op("info", "browser", f"等待扫码（最长 {wait_sec // 60} 分钟）：请用闲鱼 App 扫码")
            while time.time() < deadline:
                time.sleep(3)
                names = {c["name"] for c in ctx.cookies()}
                if LOGIN_COOKIE_NEEDED.issubset(names):
                    logged_in = True
                    break
                now = time.time()
                if now - last_log >= 30:
                    last_log = now
                    db.log_op("info", "browser",
                              f"等待扫码中…剩余 {int(deadline - now)} 秒（登录窗口保持打开）")
                has_box = any("passport.goofish.com" in f.url and "mini_login" in f.url
                              for f in page.frames)
                if not has_box and now - last_repair >= 10:
                    last_repair = now
                    try:
                        reopened = page.evaluate('''() => {
                            const els = Array.from(document.querySelectorAll('a,button,div,span'));
                            for (const el of els) {
                                const t = (el.innerText || '').trim();
                                if (t === '登录' || t === '亲，请登录' || t === '请登录') { el.click(); return true; }
                            }
                            return false;
                        }''')
                        if reopened:
                            db.log_op("info", "browser", "登录框已收起，已自动重新唤起（可继续扫码）")
                    except Exception:
                        pass
            if logged_in:
                try:
                    ctx.storage_state(path=str(config.SESSION_STATE_FILE))
                    db.log_op("info", "browser", "弹出登录成功：会话态已保存，等待主会话应用")
                except Exception as e:
                    db.log_op("warn", "browser", f"保存会话态失败: {e}")
                self._pending_session_cookies = ctx.cookies()
            else:
                self._cached_status = "offline"
                db.log_op("warn", "browser", "弹出登录超时，未完成扫码")
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self._cached_status = "error"
            db.log_op("error", "browser", f"弹出登录异常: {self.last_error}")
        finally:
            try:
                bro.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass

    def apply_pending_session(self):
        """属主线程调用：把弹出登录成功的新 Cookie 应用到主监听会话。"""
        cookies = self._pending_session_cookies
        if not cookies:
            return False
        self._pending_session_cookies = None
        try:
            self._ctx.add_cookies([
                {"name": c["name"], "value": c["value"], "domain": c["domain"],
                 "path": c.get("path", "/")} for c in cookies
            ])
            self._cached_status = "online"
            self._offline_until = 0.0
            self.last_error = ""
            self.save_session_state()
            db.log_op("info", "browser", "主会话已应用新登录态")
            return True
        except Exception as e:
            db.log_op("error", "browser", f"应用新会话失败: {type(e).__name__} {e}")
            return False

    def save_login_qr(self):
        """保存当前登录页截图作为二维码（供 Web 页显示）。属主线程调用。"""
        try:
            page = self._page
            # 优先 iframe 内 canvas 元素截图，失败则整页截图（二维码在模态框内可见）
            for f in page.frames:
                if "passport.goofish.com" in f.url and "mini_login" in f.url:
                    try:
                        el = f.query_selector("canvas") or f.query_selector("img")
                        if el:
                            el.screenshot(path=str(self._login_qr_path))
                            return True
                    except Exception:
                        pass
            page.screenshot(path=str(self._login_qr_path))
            return True
        except Exception:
            return False

    def save_session_state(self):
        """保存会话态（属主线程：登录成功后调用）"""
        try:
            self._ctx.storage_state(path=str(config.SESSION_STATE_FILE))
            return True
        except Exception as e:
            db.log_op("warn", "browser", f"保存会话态失败: {e}")
            return False

    def login_status(self):
        return {"status": self._cached_status, "qr": str(self._login_qr_path),
                "last_error": self.last_error}

    # ---------- 页面操作（仅属主线程：listener/deliverer） ----------
    def page(self):
        self.ensure_browser()
        return self._page
