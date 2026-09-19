"""闲鱼助手 - 自动发货执行器（M0#3 结论实现）

约束：网页版无"我卖出的"页/发货按钮（待上线），虚拟商品交付 = 在买家会话中发卡密。
流程：支付事件 -> 幂等 -> 原子扣卡 -> upsert 订单 -> 聊天发送卡密 -> 标记已发货
"""
import time

from . import config
from . import db
from .tasks import note_own_sent

IM_URL = "https://www.goofish.com/im"


class Deliverer:
    def __init__(self, browser):
        self.browser = browser

    # ---------- 业务主流程 ----------
    def deliver(self, order_no, product_id, buyer_name, amount, peer_id=None):
        if db.order_exists(order_no):
            db.log_op("info", "deliverer", f"订单已处理，跳过: {order_no}", order_no)
            return {"ok": False, "reason": "duplicate"}
        # 商品信息（含内部SKU，用于同SKU换行重上架后的跨行兜底取卡）
        internal_sku = ""
        try:
            with db.get_conn() as conn:
                r = conn.execute("SELECT internal_sku FROM products WHERE id=?", (product_id,)).fetchone()
                if r:
                    internal_sku = r["internal_sku"] or ""
        except Exception:
            pass
        # 取卡：先本商品（普通→循环），无则按内部SKU跨行兜底（普通→循环）
        # 状态语义：普通卡取后→已售(一次性)；循环卡保持"可用"、循环次数+1、达上限自动停用
        res = db.take_card_key(product_id, order_no)
        via = "本商品"
        if res is None and internal_sku:
            r2 = db.take_card_key_by_sku(internal_sku, order_no)
            if r2:
                res = r2
                via = f"内部SKU[{internal_sku}]跨行"
                db.log_op("info", "deliverer",
                          f"订单 {order_no} 按内部SKU「{internal_sku}」跨行取到卡密", order_no)
        if res is None:
            db.log_op("error", "deliverer", f"无库存，需人工处理: {order_no}", order_no)
            db.upsert_order(order_no, product_id, buyer_name, amount)
            return {"ok": False, "reason": "out_of_stock"}
        key_id, key_content, key_kind, recycle_count = res
        db.upsert_order(order_no, product_id, buyer_name, amount)

        # 商品级发货延迟（秒，上限 120s，避免长时间阻塞监听线程）
        delay = 0
        try:
            with db.get_conn() as conn:
                r = conn.execute("SELECT delay_seconds FROM products WHERE id=?", (product_id,)).fetchone()
                if r:
                    delay = min(int(r["delay_seconds"] or 0), 120)
        except Exception:
            pass
        if delay > 0:
            db.log_op("info", "deliverer", f"发货延迟 {delay}s（商品配置）", order_no)
            time.sleep(delay)

        # 发送卡密（peer 未知时用买家名在 /im 查找会话）
        # 按需：消息仅含卡密正文，不额外附加"您好，您购买的商品已发货…"等文案
        peer = peer_id or buyer_name
        text = self.card_message(key_content)
        sent = False
        if peer:
            sent = self.send_chat_message(peer, text)
            if sent:
                note_own_sent(text)   # out 历史由 send_chat_message 内部记录
        if not sent:
            db.log_op("error", "deliverer", f"卡密发送失败，订单置人工处理: {order_no}", order_no)
            return {"ok": False, "reason": "send_failed", "key_id": key_id}
        db.mark_order_delivered(order_no, key_id)
        kind_desc = (f"{via}·循环卡" + (f"(第{recycle_count}次)" if key_kind == "cycle" else "")
                     if key_kind == "cycle" else f"{via}·一次性卡")
        db.log_op("info", "deliverer",
                  f"已发货 {order_no} key_id={key_id}（{kind_desc}）via chat", order_no)
        # 若商品开启了"重新发布"：发货成功即标记商品已售，进入重新发布计时
        # （商品行 internal_sku 保留，重新上架后自动沿用，无需额外处理）
        try:
            with db.get_conn() as conn:
                pr = conn.execute("SELECT auto_relist FROM products WHERE id=?",
                                  (product_id,)).fetchone()
                if pr and pr["auto_relist"]:
                    db.set_product_status(product_id, "sold")
                    db.log_op("info", "deliverer",
                              f"商品 {product_id} 已标记售出，进入重新发布计时", order_no)
        except Exception:
            pass
        return {"ok": True, "key_id": key_id}

    # ---------- 聊天发送（DOM，条件等待版：无固定长 sleep，秒级完成） ----------
    @staticmethod
    def card_message(key_content):
        """发货消息：仅卡密正文（不加任何模板前后缀文案）"""
        return (key_content or "").strip()
    def _wait_for(self, fn, timeout_s, interval_s=0.4):
        """轮询等待 fn() 返回真值，超时返回 None（替代固定 time.sleep）"""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                v = fn()
                if v:
                    return v
            except Exception:
                pass
            time.sleep(interval_s)
        return None

    def send_chat_message(self, peer, text):
        """在 /im 页：点击左侧会话 -> 输入框填卡密 -> 回车发送。
        全部用条件等待：会话出现→输入框出现→输入框清空(发送成功)，避免固定 sleep。"""
        t0 = time.time()
        try:
            page = self.browser.page()
            if "im" not in page.url:
                page.goto(IM_URL, wait_until="domcontentloaded", timeout=30000)

            # 1) 点击左侧会话（按昵称/文字匹配；等待其出现，最长 10s）
            def _click_conv():
                loc = page.get_by_text(peer, exact=True)
                n = loc.count()
                for i in range(min(n, 10)):
                    el = loc.nth(i)
                    if el.is_visible():
                        el.click(force=True, timeout=3000)
                        return True
                return None

            clicked = self._wait_for(_click_conv, timeout_s=10, interval_s=0.5)
            if not clicked:
                db.log_op("warn", "deliverer", f"未找到买家会话: {peer}")
                return False

            # 2) 等待输入框出现（最长 8s；点击会话后聊天区渲染）
            def _find_box():
                for sel in ["textarea[placeholder*='请输入消息']", "textarea",
                            "[contenteditable='true']", "div[role=textbox]"]:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            return el
                    except Exception:
                        continue
                return None

            box = self._wait_for(_find_box, timeout_s=8, interval_s=0.4)
            if box is None:
                db.log_op("warn", "deliverer", "未找到聊天输入框")
                return False

            # 3) 输入并发送：Enter 优先，输入框被清空即视为发送成功
            box.click()
            box.fill(text)
            try:
                box.press("Enter")
            except Exception:
                pass

            def _cleared():
                try:
                    return (box.input_value() or "").strip() == ""
                except Exception:
                    return None

            sent = self._wait_for(_cleared, timeout_s=6, interval_s=0.4) is not None

            # 4) 兜底：Enter 未清空则点"发 送"按钮后再确认
            if not sent:
                try:
                    sent_btn = page.query_selector("button:has-text('发 送')") or \
                               page.query_selector("button:has-text('发送')")
                    if sent_btn and sent_btn.is_visible():
                        sent_btn.click()
                        sent = self._wait_for(_cleared, timeout_s=5, interval_s=0.4) is not None
                except Exception:
                    pass

            # 5) 最后兜底：文本出现在聊天区
            if not sent:
                lines = text.splitlines()
                key_part = next((ln.strip() for ln in lines if ln.strip()), "")[:30]

                def _in_body():
                    try:
                        body = page.inner_text("body")
                        return key_part in body.replace("\n", " ")
                    except Exception:
                        return None

                sent = self._wait_for(_in_body, timeout_s=5, interval_s=0.5) is not None

            if not sent:
                # 输入框未清空且聊天区未匹配到——但发送动作已执行（WS 帧会证明），
                # 按"已发送"处理并告警，避免重复发货
                db.log_op("warn", "deliverer", "发送验证未确认，按已发送处理（请人工核对）")
                sent = True
            if sent:
                db.add_message(peer, "out", text)  # 聊天历史留痕（含人工回复）
            db.log_op("info", "deliverer",
                      f"发送完成 peer={peer} ok={sent} 耗时{time.time()-t0:.1f}s")
            return sent
        except Exception as e:
            db.log_op("error", "deliverer", f"发送异常 {type(e).__name__}: {e}")
            return False

    # ---------- 图片发送（在 /im 会话发送本地图片；网页版若无可发送入口则返回 False） ----------
    def send_image(self, peer, file_path):
        try:
            import os as _os
            if not _os.path.exists(file_path):
                db.log_op("warn", "deliverer", f"图片文件不存在: {file_path}")
                return False
            t0 = time.time()
            page = self.browser.page()
            if "im" not in page.url:
                page.goto(IM_URL, wait_until="domcontentloaded", timeout=30000)

            def _click_conv():
                loc = page.get_by_text(peer, exact=True)
                n = loc.count()
                for i in range(min(n, 10)):
                    el = loc.nth(i)
                    if el.is_visible():
                        el.click(force=True, timeout=3000)
                        return True
                return None

            if not self._wait_for(_click_conv, timeout_s=10, interval_s=0.5):
                db.log_op("warn", "deliverer", f"未找到买家会话: {peer}")
                return False
            time.sleep(1.0)   # 会话/聊天区渲染

            sent = False
            # 方式A：点击"图片/相册"入口并捕获文件选择框（Playwright filechooser）
            try:
                with page.expect_file_chooser(timeout=6000) as fc:
                    self._click_image_trigger(page)
                fc.value.set_files(file_path)
                sent = True
            except Exception as e:
                db.log_op("info", "deliverer", f"filechooser 方式不可用: {type(e).__name__}")
                # 方式B：直接对可见 file input 赋值
                try:
                    for sel in ("input[type=file]", "input[type='file']"):
                        el = page.query_selector(sel)
                        if el is not None:
                            try:
                                el.set_input_files(file_path)
                                sent = True
                                break
                            except Exception:
                                continue
                except Exception:
                    pass
            if not sent:
                db.log_op("warn", "deliverer",
                          "图片发送：未找到图片上传入口（网页版 /im 可能不支持图片消息）")
                return False
            # 发送确认：上传后一般自动发出，必要时补一次回车/发送键
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            time.sleep(3.0)
            db.log_op("info", "deliverer",
                      f"图片发送完成(尽力) peer={peer} 耗时{time.time()-t0:.1f}s")
            return True
        except Exception as e:
            db.log_op("error", "deliverer", f"图片发送异常 {type(e).__name__}: {e}")
            return False

    def _click_image_trigger(self, page):
        """尽力点击 /im 聊天栏的图片上传按钮（多候选启发式）"""
        try:
            ok = page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('button,[role=button],span,i'));
                for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (!(r.width > 8 && r.width < 80 && r.height > 8)) continue;
                    const info = ((el.getAttribute('title')||'') + ' ' + (el.getAttribute('aria-label')||'') + ' ' + (el.className||''));
                    if (/(图片|相册|照片|image|photo|album)/i.test(info)) { el.click(); return true; }
                }
                return false;
            }""")
            if ok:
                return
            # 候选2：输入框上方一带含 icon 特征的可点击元素（近似图片按钮）
            page.evaluate("""() => {
                const box = document.querySelector('textarea') || document.querySelector('[contenteditable=true]');
                if (!box) return;
                const br = box.getBoundingClientRect();
                const els = Array.from(document.querySelectorAll('button,[role=button],i,span,div'));
                for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.width > 60 || r.height < 10 || r.height > 60) continue;
                    if (r.bottom > br.top - 6 && r.bottom <= br.top + 40) {
                        const c = (el.className||'') + ' ' + (el.getAttribute('title')||'');
                        if (/icon|upload|add|img|pic/i.test(c)) { el.click(); return; }
                    }
                }
            }""")
        except Exception:
            pass
