"""闲鱼助手 - 消息监听器（M0#2 结论实现）

通道：/im 页 WebSocket `wss://wss-goofish.dingtalk.com/`，帧为 JSON（LWP 协议）
  - /reg 注册 -> reg-uid
  - /r/Conversation/listNewestPagination 拉会话
  - /s/sync 同步（消息/通知推送）：syncPushPackage.data[].data 为 base64 二进制，
    解码后含明文字段（reminderTitle 昵称 / reminderContent 文本 / senderUserId / itemId）
策略：
  - WS 实时解析推送帧并立即处理（主通道，秒级响应）
  - DOM 每 30s 兜底扫描会话列表（WS 失效/漏帧时的后备，防漏）
  - 文本按 messageId 去重（WS 主通道），DOM 用 record_seen 短窗口去重
"""
import base64
import json
import os
import re
import threading
import time

from . import config
from . import db
from . import selfcheck
from .sync import auto_relist_pass, sync_products
from .tasks import task_queue, note_own_sent, is_own_recent

WS_HOST_MARK = "wss-goofish.dingtalk.com"
IM_URL = "https://www.goofish.com/im"

# 模块级活跃实例（供 API 触发自检等操作）
_active_listener = None


def register_listener(inst):
    global _active_listener
    _active_listener = inst


def get_active_listener():
    return _active_listener


class MessageListener:
    def __init__(self, browser, reply_engine, deliverer):
        self.browser = browser
        self.reply_engine = reply_engine
        self.deliverer = deliverer
        self._stop = threading.Event()
        self._thread = None
        self._page = None
        self._ws_conns = 0
        self._ws_frames = 0
        self._frame_log = config.DATA_DIR / "ws_frames.log"
        self._seen_msgs = set()          # DOM 兜底去重：nick|preview|time
        self._seen_msg_ids = set()       # WS 消息ID去重（PNM）
        self._seen_order_msgs = set()    # 订单通知去重
        self._last_cached = "idle"
        self.running = False
        self._own_unb = None   # 本账号 unb（判断 WS 推送是否为自己发送）
        self._replied = {}     # peer -> (ts, norm_text) 最近自动回复登记（已回复消息不再重复触发）
        selfcheck.set_ws_provider(self.stats)  # 自检任务读取监听/WS 活性

    # ---------- 启动/停止 ----------
    def start(self):
        register_listener(self)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        try:
            # Playwright 线程亲和：浏览器必须在监听线程内创建/使用/关闭
            self.browser.ensure_browser()
            page = self.browser.page()
            self._page = page
            page.on("websocket", self._on_ws)
            page.goto(IM_URL, wait_until="domcontentloaded", timeout=60000)
            self.running = True
            db.log_op("info", "listener", f"监听已启动 {IM_URL}")
            # 常驻：WS 实时拦截 + DOM 兜底轮询检测新消息 + 执行跨线程任务
            last_dom_poll = 0.0
            last_heartbeat = time.time()
            last_qr_refresh = 0.0
            last_acct_extract = 0.0
            last_session_check = 0.0
            last_auto_sync = 0.0
            last_auto_relist = 0.0
            last_sched_report = 0.0
            last_selfcheck = 0.0
            try:
                auto_sync_sec = max(int(db.get_setting("auto_sync_interval_sec", "600")), 30)
            except Exception:
                auto_sync_sec = 600
            while not self._stop.is_set():
                time.sleep(config.MESSAGE_POLL_SEC)
                try:
                    if page.is_closed():
                        db.log_op("error", "listener", "页面已关闭，监听结束")
                        break
                    # 属主线程刷新浏览器状态缓存（供 API 线程读取）
                    st = self.browser.refresh_status()
                    # 弹出登录成功后：把新会话 Cookie 应用到主监听会话
                    if st != "online" and self.browser._pending_session_cookies:
                        if self.browser.apply_pending_session():
                            st = "online"
                            try:
                                page.goto(IM_URL, wait_until="domcontentloaded", timeout=60000)
                            except Exception:
                                pass
                    if st != self._last_cached:
                        db.log_op("info", "listener", f"浏览器状态: {st}")
                        if st == "online" and self._last_cached in ("logging_in", "offline"):
                            # 新登录成功：保存会话态并回到 /im
                            if self.browser.save_session_state():
                                db.log_op("info", "listener", "登录成功，会话态已保存")
                            try:
                                page.goto(IM_URL, wait_until="domcontentloaded", timeout=60000)
                            except Exception:
                                pass
                            # 立即拉取一次服务端账号昵称/头像（登录后马上显示正确资料）
                            try:
                                self._extract_account_profile()
                            except Exception:
                                pass
                        self._last_cached = st
                    # 登录中：每 10s 刷新二维码截图（供 Web 页显示）
                    if st == "logging_in" and time.time() - last_qr_refresh >= 10:
                        last_qr_refresh = time.time()
                        self.browser.save_login_qr()
                    # 在线：定期提取账号昵称/头像（服务端 loginuser.get，可靠；不再依赖 DOM 抓取）
                    # 昵称尚未取到时每 60s 重试（新账号常见：无在售商品导致商品详情回退路线失败），
                    # 已取到则 10 分钟刷新一次
                    _name_missing = not db.get_setting("account_name", "").strip()
                    _acct_wait = 60 if _name_missing else 600
                    if st == "online" and time.time() - last_acct_extract >= _acct_wait:
                        last_acct_extract = time.time()
                        self._extract_account_profile()
                    # 会话服务端真实验证（每 60s；仅当本地状态 online 时探测，
                    # 过期即降级 offline——修复"本地 cookie 在但会话已过期仍显示已登录"）
                    if st == "online" and time.time() - last_session_check >= 60:
                        last_session_check = time.time()
                        try:
                            live = self.browser.verify_session_live()
                            if live is False:
                                db.log_op("warn", "listener",
                                          "会话服务端已过期，状态已置 offline，等待重新扫码登录")
                        except Exception as e:
                            db.log_op("warn", "listener",
                                      f"会话验证异常: {type(e).__name__} {e}")
                    # 在线时：自动同步在售商品（间隔可配置，秒；助手暂停时不执行）
                    _paused = db.get_setting("agent_paused", "0") == "1"
                    if st == "online" and not _paused and time.time() - last_auto_sync >= auto_sync_sec:
                        last_auto_sync = time.time()
                        try:
                            sync_products(self.browser)
                        except Exception as e:
                            db.log_op("warn", "listener",
                                      f"自动同步失败: {type(e).__name__} {e}")
                    # 在线时：自动重新发布巡检（每 30s；暂停时不执行）
                    if st == "online" and not _paused and time.time() - last_auto_relist >= 30:
                        last_auto_relist = time.time()
                        try:
                            auto_relist_pass(self.browser)
                        except Exception as e:
                            db.log_op("warn", "listener",
                                      f"自动上架巡检异常: {type(e).__name__} {e}")
                    # 管理员定时报告（每 60s 检查一次时间点；暂停时不自动发）
                    if not _paused and time.time() - last_sched_report >= 60:
                        last_sched_report = time.time()
                        try:
                            self._maybe_send_scheduled_reports()
                        except Exception as e:
                            db.log_op("warn", "listener",
                                      f"定时报告异常: {type(e).__name__} {e}")
                    # 定时自检任务（每 60s 检查一次是否到点；监控性质，暂停期间仍自检但不打扰）
                    if time.time() - last_selfcheck >= 60:
                        last_selfcheck = time.time()
                        try:
                            self._maybe_run_scheduled_selfcheck()
                        except Exception as e:
                            db.log_op("warn", "listener",
                                      f"定时自检异常: {type(e).__name__} {e}")
                    # 心跳日志（每 5 分钟）
                    if time.time() - last_heartbeat >= 300:
                        last_heartbeat = time.time()
                        db.log_op("info", "listener",
                                  f"心跳: ws_conns={self._ws_conns} ws_frames={self._ws_frames} "
                                  f"tasks={task_queue.size()}")
                    # 执行 API 线程推送的任务（如手动发货/登录）
                    for task in task_queue.drain():
                        try:
                            task()
                        except Exception as e:
                            db.log_op("error", "listener", f"任务执行失败: {type(e).__name__} {e}")
                    # DOM 兜底：每 30s 扫描会话列表（WS 解析失败时的可靠后备）
                    if time.time() - last_dom_poll >= 30:
                        last_dom_poll = time.time()
                        self._poll_dom_messages(page)
                except Exception as e:
                    # 记录异常并继续（不静默退出；连续失败由心跳暴露）
                    db.log_op("error", "listener",
                              f"监听循环异常（继续）: {type(e).__name__} {e}")
                    time.sleep(2)
        except Exception as e:
            db.log_op("error", "listener", f"监听启动失败: {type(e).__name__} {e}")
        finally:
            self.running = False
            try:
                self.browser.close()
            except Exception:
                pass

    # ---------- DOM 兜底：扫描会话列表检测新消息 ----------
    def _poll_dom_messages(self, page):
        try:
            rows = page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('div').forEach((el) => {
                    if (el.querySelector('img') && el.children.length >= 2 && el.children[1].children &&
                        el.children[1].children.length >= 2) {
                        const img = el.querySelector('img');
                        const r = img.getBoundingClientRect();
                        if (r.width !== 44) return;
                        const parts = Array.from(el.children[1].children).map(c =>
                            (c.innerText || '').trim()).filter(Boolean);
                        if (parts.length >= 2) {
                            out.push({nick: parts[0], preview: parts[1], time: parts.slice(2).join(' ')});
                        }
                    }
                });
                return out;
            }""")
        except Exception as e:
            db.log_op("warn", "listener", f"DOM 扫描失败 {type(e).__name__}")
            return
        for row in rows:
            nick = (row.get("nick") or "").splitlines()[0].strip()
            preview = row.get("preview") or ""
            t = row.get("time") or ""
            if not nick or not preview:
                continue
            # 通知/系统类会话：走订单通知识别（低风险：默认仅记录+告警）
            if any(s in nick for s in ("通知", "闲小蜜", "交易", "闲鱼服务", "官方")):
                if any(m in t for m in ("刚刚", "分钟前", "秒前")):
                    self._poll_order_notification(nick, preview, t)
                continue
            if not any(m in t for m in ("刚刚", "分钟前", "秒前")):
                continue
            if is_own_recent(preview, window_sec=None):
                continue  # 自己发送的消息回显（会话列表预览长期存在），跳过
            if not db.record_seen(nick, preview):
                continue  # 历史已处理（重启不重复）
            db.log_op("info", "listener", f"[DOM] 新消息 {nick}: {preview[:60]}")
            self._dispatch_message(nick, preview)

    def _dispatch_message(self, peer, text, item_id=None, msg_id=None):
        """统一消息入口：管理员口令优先，其次按商品上下文走规则引擎。"""
        # WS 主通道：messageId 去重（同消息可能多帧推送），并登记 seen 供 DOM 跳过
        if msg_id:
            if msg_id in self._seen_msg_ids:
                return
            self._seen_msg_ids.add(msg_id)
            if len(self._seen_msg_ids) > 500:
                self._seen_msg_ids.clear()
            db.record_seen(peer, text)
        else:
            # DOM 兜底路径：无 messageId。record_seen 窗口过期后同一条消息仍会随会话列表
            # 反复重放（每 ~2 分钟一次），用近窗文本去重拦截，避免重复入库/重复回复
            if db.recent_in_dup(peer, text, window_sec=600):
                return
        # 自己（卖家）刚发出的消息回显：本进程发出由 own_sent/unb 过滤；
        # 其它设备（如手机 App 人工回复）发出后推回 → 按"近期 out 记录"识别为卖家消息，
        # 不再误判为买家消息（不入 in、不自动回复）。窗口 24 小时，覆盖平台延迟回显/重放
        if db.is_recent_out(peer, text, window_sec=86400):
            db.log_op("info", "listener", f"[卖家消息-忽略] {peer}: {text[:40]}")
            return
        # 追加防线：文本与我方任一"回复模板"完全相同 → 平台把我方自动回复回显成买家消息，
        # 一律按卖家消息处理（修复"收发倒置：自己的问候语出现在买家一侧"以及可能的问候语重复发送）
        if self._is_own_template_echo(text):
            db.log_op("info", "listener", f"[模板回显-忽略] {peer}: {text[:40]}")
            return
        # 交易/系统通知（买家拍下/付款/等待发货等）：先识别，不写入买家会话表
        # （避免"等待你发货"这类系统名污染最近买家，导致收货人取错），直接走自动发货
        if self._looks_like_order_notice(peer, text):
            if db.get_setting("agent_paused", "0") != "1":
                self._poll_order_notification(peer, text, "刚刚", item_id=item_id, msg_id=msg_id)
            else:
                db.log_op("info", "listener", f"助手已暂停，忽略订单通知: {text[:40]}")
            return
        # 系统回执/提示类消息（如"你已发货""买家确认收货，交易成功""快给ta一个评价吧～"）：
        # 非买家聊天，不入会话/历史、不自动回复（避免聊天窗口反复出现提示信息）
        if self._is_sys_receipt(text):
            db.log_op("info", "listener", f"[系统回执-忽略] {peer}: {text[:50]}")
            return
        db.upsert_conversation(peer, peer)
        db.add_message(peer, "in", text)  # 聊天历史留痕（聊天窗口/人工回复上下文）
        # 管理员口令优先（绑定/索取状态/索取汇报/自定义口令）——暂停期间仍响应，便于恢复
        if self._handle_admin_commands(peer, text):
            return
        # 助手暂停（自定义口令触发）：不自动回复买家
        if db.get_setting("agent_paused", "0") == "1":
            return
        # 商品上下文：itemId 匹配本地商品（sku=item_id），命中商品专属规则
        product_id = None
        if item_id:
            try:
                with db.get_conn() as conn:
                    r = conn.execute("SELECT id FROM products WHERE item_id=? OR sku=? LIMIT 1",
                                     (item_id, item_id)).fetchone()
                    if r:
                        product_id = r["id"]
            except Exception:
                pass
        ctx = {"买家": peer, "卡密": "", "商品名": ""}
        if product_id:
            try:
                with db.get_conn() as conn:
                    p = conn.execute("SELECT title FROM products WHERE id=?", (product_id,)).fetchone()
                    if p:
                        ctx["商品名"] = p["title"]
            except Exception:
                pass
        reply, source = self.reply_engine.decide(peer, text, ctx, product_id=product_id)
        if reply:
            # 已回复的消息（或与其高度相同）不重复触发自动回复（防重复推送/复读造成连环回复）
            if self._recently_replied(peer, text):
                db.log_op("info", "listener",
                          f"已回复过同类消息，跳过重复自动回复 {peer}: {text[:40]}")
            else:
                db.log_op("info", "listener", f"规则命中[{source}] 回复 {peer}: {reply[:50]}")
                self._send_reply(peer, reply)

    def _is_own_template_echo(self, text):
        """文本与我方任一自动回复模板相同（归一化）→ 平台把我方回复回显成买家消息。
        结果为"卖家消息"，不入 in、不自动回复（防收发倒置与问候语自问自答循环）。"""
        try:
            n = re.sub(r"\s+", "", (text or ""))
            if len(n) < 4:
                return False
            now = time.time()
            cache = getattr(self, "_tpl_cache", None)
            if not cache or now - cache[0] > 60:
                with db.get_conn() as conn:
                    rows = conn.execute("SELECT reply_template FROM reply_rules").fetchall()
                tpls = set()
                for r in rows:
                    t = re.sub(r"\s+", "", (r["reply_template"] or ""))
                    if len(t) >= 4:
                        tpls.add(t[:100])
                cache = (now, tpls)
                self._tpl_cache = cache
            return n[:100] in cache[1]
        except Exception:
            return False

    def _recently_replied(self, peer, text, window_sec=600):
        """近 window_sec 是否对该会话回复过同文本（归一化前缀容差）"""
        try:
            n = re.sub(r"\s+", "", text or "")[:80]
            if not n:
                return False
            rec = self._replied.get(peer)
            if not rec:
                return False
            ts, on = rec
            if time.time() - ts > window_sec:
                return False
            if not on:
                return False
            return n == on or n[:16] == on[:16]
        except Exception:
            return False

    def _send_reply(self, peer, text):
        """发送回复并登记 own_sent（防止 echo 被当作新消息再次触发）"""
        ok = self.deliverer.send_chat_message(peer, text)
        if ok:
            note_own_sent(text)
            try:
                self._replied[peer] = (time.time(), re.sub(r"\s+", "", text or "")[:80])
            except Exception:
                pass
        return ok

    # ---------- 管理员账号（绑定口令 / 口令索取状态汇报 / 定时报告） ----------
    def _handle_admin_commands(self, peer, text):
        try:
            bind = db.get_setting("bind_phrase", "").strip()
            admin = db.get_setting("admin_peer", "").strip()
            msg = (text or "").strip()
            # 1) 绑定口令：任意账号发送即绑定为管理员（仅网页端可解绑）
            if bind and msg == bind:
                db.set_setting("admin_peer", peer)
                db.log_op("info", "admin", f"已绑定管理员账号: {peer}")
                self._send_reply(peer, "✅ 已绑定为管理员账号（如需解绑请在网页端操作）")
                return True
            # 2) 管理员专用口令
            if admin and peer == admin:
                sp = db.get_setting("admin_status_phrase", "").strip()
                ap = db.get_setting("admin_sales_phrase", "").strip()
                if sp and msg == sp:
                    # 状态口令 → 执行新自检任务（含虚拟流程与预期结果），同步汇报
                    db.log_op("info", "admin", f"状态口令触发自检（{peer}）")
                    ok, report = self._do_selfcheck()
                    self._send_reply(peer, report)
                    return True
                if ap and msg == ap:
                    self._send_reply(peer, self._build_sales_report())
                    return True
                # 3) 自定义口令（网页可配置：暂停/恢复助手、暂停/恢复某商品自动发货）
                if self._handle_custom_phrase(peer, msg):
                    return True
        except Exception as e:
            db.log_op("error", "admin", f"管理员命令异常: {type(e).__name__} {e}")
        return False

    def _handle_custom_phrase(self, peer, msg):
        """自定义口令：{phrase, action, target?}
        action: pause_agent/resume_agent/pause_deliver/resume_deliver
        target: 内部SKU（商品自动发货暂停/恢复用）"""
        try:
            import json as _json
            raw = db.get_setting("custom_phrases", "[]")
            try:
                phrases = _json.loads(raw or "[]")
            except Exception:
                phrases = []
            for item in phrases:
                phrase = (item.get("phrase") or "").strip()
                if not phrase or msg != phrase:
                    continue
                action = item.get("action") or ""
                target = (item.get("target") or "").strip()
                if action == "pause_agent":
                    db.set_setting("agent_paused", "1")
                    db.log_op("info", "admin", f"自定义口令: 暂停助手（{peer}）")
                    self._send_reply(peer, "⏸ 助手已暂停：自动回复/自动发货/自动同步与重新发布已停止。发送恢复口令可继续。")
                elif action == "resume_agent":
                    db.set_setting("agent_paused", "0")
                    db.log_op("info", "admin", f"自定义口令: 恢复助手（{peer}）")
                    self._send_reply(peer, "▶️ 助手已恢复运行。")
                elif action in ("pause_deliver", "resume_deliver"):
                    want = 0 if action == "pause_deliver" else 1
                    if not target:
                        self._send_reply(peer, "该口令未配置目标商品（内部SKU），请先在网页端设置。")
                        return True
                    with db.get_conn() as conn:
                        p = conn.execute("SELECT id, title FROM products WHERE internal_sku=?", (target,)).fetchone()
                        if not p:
                            self._send_reply(peer, f"未找到内部SKU「{target}」对应的商品。")
                            return True
                        conn.execute("UPDATE products SET auto_deliver=? WHERE id=?", (want, p["id"]))
                    db.log_op("info", "admin",
                              f"自定义口令: {'暂停' if want == 0 else '恢复'}商品自动发货 "
                              f"「{(p['title'] or '')[:20]}」")
                    self._send_reply(peer,
                                     f"{'⏸ 已暂停' if want == 0 else '▶️ 已恢复'}商品「{(p['title'] or '')[:20]}」的自动发货。")
                else:
                    self._send_reply(peer, f"未知口令动作: {action}")
                return True
        except Exception as e:
            db.log_op("error", "admin", f"自定义口令异常: {type(e).__name__} {e}")
        return False

    def _maybe_send_scheduled_reports(self):
        """定时任务：到设定时间向管理员发送（每天一次）。
        【状态检查】= 完整重跑自检脚本并发送结果；【运营汇报】= 轻量营收汇总。"""
        admin = db.get_setting("admin_peer", "").strip()
        if not admin or not self.browser.is_logged_in():
            return
        now = time.strftime("%H:%M")
        today = time.strftime("%Y-%m-%d")
        # 定时状态检查：使用与"运行状态口令/立即自检"完全相同的自检脚本
        t = db.get_setting("status_report_time", "").strip()
        if t and now == t and db.get_setting("last_status_report", "") != today:
            try:
                db.log_op("info", "admin", "定时状态检查到点 → 重新运行完整自检脚本…")
                ok, report = self._do_selfcheck()
                self._send_reply(admin, report)
                db.set_setting("last_status_report", today)
                db.log_op("info", "admin", "定时状态检查（自检）已发送")
            except Exception as e:
                db.log_op("error", "admin", f"定时状态检查失败: {type(e).__name__} {e}")
        # 定时运营汇报
        t2 = db.get_setting("sales_report_time", "").strip()
        if t2 and now == t2 and db.get_setting("last_sales_report", "") != today:
            try:
                self._send_reply(admin, self._build_sales_report())
                db.set_setting("last_sales_report", today)
                db.log_op("info", "admin", "定时运营汇报已发送")
            except Exception as e:
                db.log_op("error", "admin", f"定时运营汇报失败: {type(e).__name__} {e}")

    # ---------- 自检任务（项5/6：状态口令 / 定时触发，见 app/selfcheck.py） ----------
    def _do_selfcheck(self):
        """同步执行一次完整自检（须在属主线程）。返回 (ok, report)。"""
        try:
            return selfcheck.run_selfcheck(self.browser, self.reply_engine)
        finally:
            db.set_setting("selfcheck_running", "0")  # 异常路径也复位运行中标记

    def _do_selfcheck_and_notify(self):
        """执行自检并向管理员账号发送结果（定时任务用）。"""
        ok, report = self._do_selfcheck()
        admin = db.get_setting("admin_peer", "").strip()
        try:
            if admin and self.browser.is_logged_in():
                self._send_reply(admin, report)
                db.log_op("info", "selfcheck", f"自检结果已发送管理员: {admin}")
        except Exception as e:
            db.log_op("warn", "selfcheck", f"自检结果发送失败: {type(e).__name__} {e}")
        return ok, report

    def _maybe_run_scheduled_selfcheck(self):
        """定时自检：开关开启且到间隔分钟 → 执行并通知管理员。"""
        if db.get_setting("selfcheck_enabled", "0") != "1":
            return
        try:
            interval_min = max(int(db.get_setting("selfcheck_interval_min", "60")), 1)
        except Exception:
            interval_min = 60
        last = db.get_setting("last_selfcheck_ts", "")
        if last:
            import datetime as _dt
            try:
                last_dt = _dt.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                if (_dt.datetime.now() - last_dt).total_seconds() < interval_min * 60:
                    return
            except Exception:
                pass
        db.log_op("info", "selfcheck", f"定时自检触发（间隔 {interval_min} 分钟）")
        self._do_selfcheck_and_notify()

    # ---------- 账号信息（昵称/头像，服务端 loginuser.get 优先） ----------
    def _dom_account_profile(self):
        """从 /im 页面 DOM 兜底抓取昵称/头像（服务端接口对新账号可能拿不到，
        页面已登录时这里通常能拿到；失败返回空 dict）。"""
        try:
            page = self._page
            if page is None:
                return {}
            return page.evaluate("""() => {
                const bad = new Set(['闲鱼','消息','登录','我的','首页','搜索','发布','客服','设置',
                                     '鱼塘','关注','粉丝','宝贝','动态','通知','交易','评价','管理','更多']);
                let avatar = '';
                for (const img of document.querySelectorAll('img')) {
                    const src = img.src || '';
                    if (src.indexOf('mtopupload') >= 0 || src.indexOf('avatar') >= 0 || src.indexOf('portrait') >= 0) {
                        avatar = src; break;
                    }
                }
                const pick = [];
                const scan = (sel, maxLen) => {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = (el.innerText || '').trim();
                        if (!t || t.length < 2 || t.length > maxLen) continue;
                        if (bad.has(t)) continue;
                        if (/^[\\d\\s.\\-+]+$/.test(t)) continue;
                        const r = el.getBoundingClientRect();
                        pick.push({ t: t, top: r.top, left: r.left, len: t.length });
                    }
                };
                scan('[class*=nick],[class*=Nick],[class*=userName],[class*=user-name],[class*=userInfo]', 24);
                if (!pick.length) scan('span,div,a', 16);
                pick.sort((a, b) => (a.top - b.top) || (b.left - a.left));
                return { nick: pick.length ? pick[0].t : '', avatar: avatar };
            }""") or {}
        except Exception:
            return {}

    def _extract_account_profile(self):
        """从服务端拉取账号昵称/头像（修复 DOM 抓取到 hello/头像加载不出的问题）；
        服务端拿不到（如新账号无在售商品）时回退 DOM 抓取；始终兜底写入 cookie 里的 unb。"""
        prof = {}
        try:
            from .mtop import build_cookie_header, fetch_account_profile
            if self.browser._ctx is None:
                return
            cookie_str = build_cookie_header(self.browser._ctx.cookies())
            prof = fetch_account_profile(cookie_str) or {}
        except Exception as e:
            prof = {}
            db.log_op("warn", "listener", f"账号资料(服务端)获取异常: {type(e).__name__} {e}")
        nick = (prof.get("nick") or "").strip()
        avatar = (prof.get("avatar") or "").strip()
        uid = (prof.get("userId") or "").strip()
        if not nick or not avatar:
            dom = self._dom_account_profile() or {}
            if not nick:
                nick = (dom.get("nick") or "").strip()
            if not avatar:
                avatar = (dom.get("avatar") or "").strip()
            if nick or avatar:
                db.log_op("info", "listener", "账号资料取自页面(DOM 兜底)")
        changed = []
        if nick:
            db.set_setting("account_name", nick)
            changed.append(f"昵称={nick}")
        if avatar:
            db.set_setting("account_avatar", avatar)
            changed.append("头像已更新")
        if not uid:
            uid = self._own_unb_id() or ""   # 兜底：直接用 cookie unb
        if uid:
            db.set_setting("account_unb", uid)
        if changed:
            db.log_op("info", "listener", "账号信息已更新（服务端）: " + "，".join(changed))
        elif not nick:
            db.log_op("warn", "listener",
                      "账号昵称仍未取到（服务端与页面均失败）：若刚登录请稍候，60 秒后自动重试")

    def _build_sales_report(self):
        today = time.strftime("%Y-%m-%d")
        rev = db.revenue_summary(start=today, end=today)
        lines = [f"【闲鱼助手 · 运营汇报 {today}】",
                 f"今日营收: ¥{rev['total_amount']} ｜ 今日订单: {rev['total_orders']}"]
        for p in rev["by_product"]:
            lines.append(f"  - {p['title']}: {p['count']}单 ¥{p['amount']}")
        if not rev["by_product"]:
            lines.append("（今日暂无销售）")
        return "\n".join(lines)

    # ---------- 订单通知识别（自动发货由各商品自身的"自动发货"开关控制，无全局总开关） ----------
    ORDER_KEYWORDS = ("付款", "支付", "拍下", "下单", "已售出", "订单", "发货")
    ORDER_STRONG = ("已付款", "已支付", "付款成功", "支付成功", "拍下成功", "新订单", "售出")
    PAID_HINTS = ("已付款", "已支付", "付款成功", "支付成功", "等待你发货", "待发货")
    # 系统会话名特征（WS 推送 reminderTitle 或 DOM 昵称）：命中即视为交易通知而非买家闲聊
    SYS_CONV_HINTS = ("拍下", "待付款", "待发货", "等待你发货", "已付款", "订单", "交易",
                      "服务", "官方", "闲小蜜", "通知", "售出", "退款", "评价")
    # 系统回执文本特征（无需动作，仅提示）：不入会话历史、不自动回复
    # 覆盖旧版出现的："你已发货/确认收货/交易成功/快给ta一个评价/我完成了评价/已拍下/待付款"等
    SYS_RECEIPT_HINTS = ("你已发货", "您已发货", "已发货", "买家确认收货", "确认收货",
                         "交易成功", "快给ta一个评价", "评价一下吧", "等待评价",
                         "我完成了评价", "完成了评价", "已拍下", "待付款", "已退款", "退款成功")

    def _is_sys_receipt(self, text):
        try:
            t = text or ""
            return any(h in t for h in self.SYS_RECEIPT_HINTS)
        except Exception:
            return False

    def _looks_like_order_notice(self, nick, text):
        """判断一条消息是否为交易/系统通知（区别于买家普通聊天）。
        发货前提：买家已付款（PAID_HINTS），"拍下待付款"类只记录不发货。"""
        if not any(k in text for k in self.ORDER_KEYWORDS):
            return False
        if any(k in (nick or "") for k in self.SYS_CONV_HINTS):
            return True
        # 普通买家昵称+已付款强信号（买家转述/系统会话 nick 未识别时兜底）
        if any(k in text for k in self.PAID_HINTS):
            return True
        return False

    def _poll_order_notification(self, nick, preview, t, item_id=None, msg_id=None):
        if not any(k in preview for k in self.ORDER_KEYWORDS):
            return
        if not any(k in preview for k in self.PAID_HINTS):
            db.log_op("info", "listener", f"[订单事件-待付款] {nick}: {preview[:100]}（未付款，不发货）")
            return
        key = f"ORDER|{msg_id or nick}|{preview[:120]}|{t}"
        if key in self._seen_order_msgs:
            return
        self._seen_order_msgs.add(key)
        db.log_op("info", "listener", f"[订单通知] {nick}: {preview[:150]}")
        try:
            # 匹配商品：优先 itemId（推送自带，最准），其次标题包含
            matched = None
            with db.get_conn() as conn:
                if item_id:
                    rows = conn.execute(
                        "SELECT * FROM products "
                        "WHERE (item_id=? OR sku=?) AND auto_deliver=1 AND status='selling'",
                        (item_id, item_id)).fetchall()
                    for p in rows:
                        if p["auto_deliver"] and p["status"] == "selling":
                            matched = p
                            break
                if matched is None:
                    rows = conn.execute(
                        "SELECT * FROM products WHERE auto_deliver=1 AND status='selling'").fetchall()
                    for p in rows:
                        if p["title"] and p["title"] in preview:
                            matched = p
                            break
            if matched is None:
                db.log_op("info", "listener",
                          "订单通知已记录（未匹配到开启了自动发货的在售商品，不自动发货）")
                return
            # 取最近的真实买家会话作为收货人（排除系统/交易通知会话名）
            buyer = None
            with db.get_conn() as conn:
                rows = conn.execute(
                    "SELECT peer_id, buyer_name FROM conversations WHERE manual_flag=0 "
                    "ORDER BY last_msg_at DESC LIMIT 10").fetchall()
                for r in rows:
                    cand = (r["buyer_name"] or r["peer_id"] or "").strip()
                    if not cand:
                        continue
                    if any(k in cand for k in self.SYS_CONV_HINTS):
                        continue  # 系统通知会话（等待你发货/买家已拍下…）不是收货人
                    buyer = cand
                    break
            if not buyer:
                db.log_op("warn", "listener", "订单通知已匹配商品，但未找到买家会话")
                return
            order_no = "XY-" + time.strftime("%Y%m%d%H%M%S")
            db.log_op("info", "listener",
                      f"[自动发货] 订单 {order_no} -> 商品「{matched['title']}」买家 {buyer}")
            res = self.deliverer.deliver(order_no, matched["id"], buyer, matched["price"], buyer)
            db.log_op("info", "listener", f"[自动发货] 结果: {res}", order_no)
        except Exception as e:
            db.log_op("error", "listener", f"订单自动发货异常: {type(e).__name__} {e}")

    # ---------- WebSocket 拦截 ----------
    def _on_ws(self, ws):
        url = ws.url or ""
        if WS_HOST_MARK not in url:
            return
        self._ws_conns += 1
        db.log_op("info", "listener", f"WS 已连接: {url[:100]}")
        ws.on("framereceived", lambda pl: self._on_frame("recv", str(pl)))
        ws.on("framesent", lambda pl: self._on_frame("send", str(pl)))

    _PUSH_KEYS = ("reminderTitle", "reminderContent", "senderUserId",
                  "detailNotice", "reminderUrl", "extJson")

    def _on_frame(self, direction, payload):
        self._ws_frames += 1
        try:
            self._log_frame(direction, payload)
        except Exception:
            pass
        try:
            data = json.loads(payload)
        except Exception:
            # 二进制帧：可能是 base64 打包的推送，尝试解析
            self._try_decode_binary(payload)
            return
        lwp = data.get("lwp") or ""
        headers = data.get("headers") or {}
        body = data.get("body")

        # 注册响应：记录 uid
        if "reg" in lwp and headers.get("reg-uid"):
            db.log_op("info", "listener", f"WS 注册成功 uid={headers['reg-uid']}")

        # 会话列表
        if "Conversation/listNewestPagination" in lwp and isinstance(body, list):
            self._handle_conversations(body)

        # 同步推送 /s/sync：syncPushPackage.data[].data 为 base64 二进制
        if "/s/sync" in lwp and isinstance(body, dict):
            pkg = body.get("syncPushPackage") or {}
            items = pkg.get("data") or []
            for it in items:
                b64 = None
                if isinstance(it, dict):
                    b64 = it.get("data") or it.get("payload")
                elif isinstance(it, str):
                    b64 = it
                if isinstance(b64, str) and b64:
                    self._try_decode_binary(b64)
            return

        # 其他事件（消息相关 JSON 帧）
        if "sync" in lwp.lower() or "message" in lwp.lower() or "msg" in lwp.lower():
            self._try_extract_event(lwp, body, headers)

    # ---------- 解析 ----------
    def _handle_conversations(self, body):
        try:
            convs = body[1] if isinstance(body, list) and len(body) > 1 and isinstance(body[1], list) else body
            for c in convs if isinstance(convs, list) else []:
                if not isinstance(c, dict):
                    continue
                peer = self._pick(c, "nickName", "name", "peerName", "title")
                cid = self._pick(c, "cid", "conversationId", "id")
                if peer or cid:
                    db.upsert_conversation(str(cid or peer or ""), str(peer or ""))
        except Exception:
            pass

    _KV_AHEAD = ("reminderUrl", "senderUserId", "reminderTitle", "reminderContent",
                 "detailNotice", "extJson", "reminderNotice", "senderUserType",
                 "clientIp", "port", "umid", "umidToken", "utdid", "needPush",
                 "sessionType", "bizTag", "_appVersion", "_platform")

    def _kv_extract(self, s, key):
        """宽松提取 TLV 键值：键名后 0-4 个非字母数字标记字节，值到下一已知键或串尾"""
        try:
            ahead = '|'.join(re.escape(k) for k in self._KV_AHEAD if k != key)
            pat = re.escape(key) + r'[^A-Za-z0-9\u4e00-\u9fff]{0,4}(.*?)(?=' + ahead + r'|$)'
            m = re.search(pat, s)
            if not m:
                return ""
            val = re.sub(r'^[\x00-\x1f\ufffd]+|[\x00-\x1f\ufffd]+$', '', m.group(1))
            return val
        except Exception:
            return ""

    def _try_decode_binary(self, raw):
        """解码 syncPushPackage.data 的 base64 二进制（含 LWP 打包的明文消息字段）。
        实测结构含 reminderTitle/reminderContent/senderUserId/itemId/xxx.PNM，
        文本与中文均以 UTF-8 明文内嵌，可稳定提取。"""
        if not raw:
            return
        try:
            dec = base64.b64decode(raw)
        except Exception:
            return
        if not dec:
            return
        try:
            s = dec.decode("utf-8", errors="replace")
        except Exception:
            return
        if not any(k in s for k in self._PUSH_KEYS):
            return  # 非消息帧（心跳等）
        # 消息ID（去重用）：xxx.PNM
        msg_id = ""
        m = re.search(r'([0-9]{8,}\.PNM)', s)
        if m:
            msg_id = m.group(1)
        if not msg_id:
            m = re.search(r'"messageId"\s*:\s*"([^"]+)"', s)
            if m:
                msg_id = m.group(1)
        # 文本：reminderContent 优先，其次 JSON text，再 detailNotice
        text = self._kv_extract(s, "reminderContent")
        if not text:
            m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}\s*\}', s)
            if m:
                try:
                    text = json.loads('"' + m.group(1) + '"')
                except Exception:
                    text = m.group(1)
        if not text:
            text = self._kv_extract(s, "detailNotice")
        # 发送方昵称/ID
        nick = self._kv_extract(s, "reminderTitle")
        uid = ""
        m = re.search(r'senderUserId[^0-9]{0,8}(\d+)', s)
        if m:
            uid = m.group(1)
        # 关联商品 itemId（reminderUrl 或 extension 中）
        item_id = ""
        m = re.search(r'itemId=(\d+)', s)
        if not m:
            m = re.search(r'"itemId"\s*:\s*"?(\d+)', s)
        if m:
            item_id = m.group(1)
        text = (text or "").strip()
        if not text:
            return
        # 自己发送的消息（网页端发出后也会被推送 echo），跳过以免重复入库/误触发：
        # ① 发送方 userId == 本账号 unb（最准）；② 文本与最近自己发送内容匹配（容忍换行差异）
        if uid:
            own = self._own_unb_id()
            if own and uid == own:
                return
        if is_own_recent(text):
            return
        peer = nick or uid or "unknown"
        db.log_op("info", "listener", f"[WS推送] {peer}: {text[:60]} msg={msg_id[:30] or '-'}")
        self._dispatch_message(peer, text, item_id=item_id, msg_id=msg_id)

    def _own_unb_id(self):
        """取本账号 unb（cookie），用于判断 WS 推送是否为自己发送。"""
        try:
            if self._own_unb is None and self.browser._ctx is not None:
                for c in self.browser._ctx.cookies():
                    if c["name"] == "unb":
                        self._own_unb = c["value"]
                        break
        except Exception:
            pass
        return self._own_unb

    def _try_extract_event(self, lwp, body, headers):
        """记录消息类 JSON 帧摘要（学习用）。
        注意：不在此 dispatch —— listUserMessages 等拉取响应含历史消息，
        若 dispatch 会批量误回复；新消息由 /s/sync 二进制推送实时处理，DOM 兜底防漏。"""
        try:
            db.log_op("info", "listener", f"事件帧 lwp={lwp} body摘要={str(body)[:300]}")
        except Exception:
            pass

    # ---------- 工具 ----------
    @staticmethod
    def _pick(o, *keys):
        if not isinstance(o, dict):
            return None
        for k in keys:
            v = o.get(k)
            if v not in (None, ""):
                return v
        return None

    def _log_frame(self, direction, payload):
        with open(self._frame_log, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {direction} {payload[:4000]}\n")

    def stats(self):
        return {"running": self.running, "ws_conns": self._ws_conns,
                "ws_frames": self._ws_frames, "frame_log": str(self._frame_log)}
