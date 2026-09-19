"""闲鱼助手 - 商品同步与真实商品操作（mtop + 网页发布，需在浏览器属主线程执行）"""
import json
import os
import re
import time
import urllib.request

from . import config, db
from .mtop import (build_cookie_header, delete_item, fetch_item_detail,
                   fetch_item_list)

PUBLISH_URL = "https://www.goofish.com/publish"
PUBLISH_MIN_INTERVAL_SEC = 120   # 两次真实发布最小间隔（风控）


def _session(browser):
    """取浏览器会话：cookie 头 + unb 用户ID"""
    if browser._ctx is None:
        raise RuntimeError("浏览器未初始化")
    cookies = browser._ctx.cookies()
    cookie_str = build_cookie_header(cookies)
    user_id = next((c["value"] for c in cookies if c["name"] == "unb"), "")
    return cookie_str, user_id


def sync_products(browser, max_pages=3):
    """拉取在售商品 → 更新本地商品表（只读）。
    对"本地在售但拉取缺失"的商品（含 auto_relist=1）逐一核验真实状态：
    已删除/已下架→本地标 off（不再误报在售、不触发自动重发）；真实售出→标 sold。"""
    cookie_str, user_id = _session(browser)
    if not user_id:
        raise RuntimeError("未找到 unb Cookie（未登录或会话已失效）")
    items = []
    for page in range(1, max_pages + 1):
        page_items, _ = fetch_item_list(cookie_str, user_id, page_number=page, page_size=20)
        if not page_items:
            break
        items.extend(page_items)
    active_ids = [i["id"] for i in items]
    res = db.sync_upsert_items(items)
    marked_sold = db.mark_missing_sold(active_ids)
    verified_off = _verify_missing_items(cookie_str, active_ids)
    db.log_op("info", "sync", f"商品同步完成: 获取{len(items)}条, 新增{res['new']}, "
                              f"更新{res['updated']}, 标记已售{marked_sold}, 核验下架{verified_off}")
    return {"fetched": len(items), **res, "marked_sold": marked_sold,
            "verified_off": verified_off}


def _verify_missing_items(cookie_str, active_ids):
    """核验"本地在售但拉取缺失"的商品真实状态（含 auto_relist=1）：
    已删除/已下架→本地标 off（避免僵尸在售、不触发自动重发）；真实售出→标 sold。"""
    verified_off = 0
    for p in db.missing_selling_items(active_ids):
        try:
            detail = fetch_item_detail(cookie_str, p["item_id"])
            item = (detail or {}).get("itemDO") or {}
            status_str = str(item.get("itemStatusStr") or "")
            if "卖掉" in status_str or item.get("itemStatus") == 1:
                db.set_product_status(p["id"], "sold")
                db.log_op("info", "sync",
                          f"「{(p['title'] or '')[:16]}」真实已售出，标记 sold")
            else:
                db.set_product_status(p["id"], "off")
                verified_off += 1
                db.log_op("warn", "sync",
                          f"「{(p['title'] or '')[:16]}」已不在闲鱼（{status_str or '已下架'}），本地标记下架")
        except Exception as e:
            if any(k in str(e) for k in ("不存在", "已删除", "DEL_NOT_FOUND")):
                db.set_product_status(p["id"], "off")
                verified_off += 1
                db.log_op("warn", "sync",
                          f"「{(p['title'] or '')[:16]}」真实已删除，本地标记下架")
            else:
                db.log_op("warn", "sync",
                          f"核验「{(p['title'] or '')[:16]}」状态失败: {type(e).__name__}: {str(e)[:80]}")
    return verified_off


def _get_product(pid):
    with db.get_conn() as conn:
        r = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def do_delete(browser, pid):
    """真实删除商品（不可逆）"""
    p = _get_product(pid)
    if not p:
        raise RuntimeError("商品不存在")
    if not p.get("item_id"):
        # 本地手工分组无真实商品，直接清本地
        _local_delete(pid)
        db.log_op("info", "sync", f"本地分组已删除（无真实商品）: {p['title']}")
        return {"ok": True, "message": "本地分组已删除"}
    cookie_str, _ = _session(browser)
    res = delete_item(cookie_str, p["item_id"])
    if res.get("ok"):
        _local_delete(pid)
        db.log_op("info", "sync", f"已真实删除商品「{p['title']}」(item={p['item_id']})")
    else:
        msg = str(res.get("message") or "")
        if "不存在" in msg or "已删除" in msg or "DEL_NOT_FOUND" in msg:
            # 真实商品已不存在（可能 App 端已删/下架）：清理本地僵尸记录
            _local_delete(pid)
            db.log_op("warn", "sync",
                      f"真实商品已不存在（{msg[:40]}），已清理本地记录: {p['title']}")
            return {"ok": True, "message": "真实商品已不存在，本地已清理"}
        db.log_op("error", "sync", f"删除失败: {msg}")
    return res


def _local_delete(pid):
    """删除本地商品记录（含引用清理）。先删子表（卡密/规则/订单关联）再删商品，避免外键约束失败。"""
    with db.get_conn() as conn:
        conn.execute("DELETE FROM card_keys WHERE product_id=?", (pid,))
        conn.execute("DELETE FROM reply_rules WHERE product_id=?", (pid,))
        # 订单保留记录（营收审计），仅解除 product_id 引用
        conn.execute("UPDATE orders SET product_id=NULL WHERE product_id=?", (pid,))
        conn.execute("DELETE FROM products WHERE id=?", (pid,))


def _extract_imgs(detail):
    """从商品详情提取真实商品图片 URL 清单（过滤 logo/水印/缩略图，保留 xy_item/mtopupload/主图）"""
    urls = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, str) and v.startswith('http') and \
                        ('.jpg' in v or '.jpeg' in v or '.png' in v or '.webp' in v):
                    urls.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(detail)
    seen, out = set(), []
    for u in urls:
        u2 = re.sub(r'_\d+x\d+.*?(\.(?:jpg|jpeg|png|webp))', r'\1', u)
        u2 = u2.split('?')[0]
        # 过滤明显非商品图（尺寸小 logo / 非 alicdn 大图）
        if any(x in u2 for x in ('tps-', 'tb-', '-tps', 'logo')):
            continue
        if 'mtopupload' not in u2 and 'xy_item' not in u2 and '-xy-' not in u2:
            continue
        # 过滤"系统/头像"图：mtopupload 上传图无卖家目录段（如
        # uploaded/i4/O1CN…_!!4611686018427384785-0-mtopupload.jpg，sellerId 为 2^62+1 保留值）
        # 正常商品图路径带卖家目录段 uploaded/iX/<卖家ID>/O1CN…
        if 'mtopupload' in u2 and not re.search(r'/uploaded/i\d+/\d+/', u2):
            continue
        if u2 not in seen:
            seen.add(u2)
            out.append(u2)
    # 至少保底 1 张（主图）
    if not out:
        for u in urls:
            u2 = u.split('?')[0]
            if u2 not in seen:
                seen.add(u2)
                out.append(u2)
    return out


def save_publish_info(browser, pid):
    """保存商品"重新发布"所需信息（拉取详情存入 publish_payload：标题/描述/图片/属性/价格）"""
    p = _get_product(pid)
    if not p or not p.get("item_id"):
        raise RuntimeError("商品无 item_id，无法拉取发布信息")
    cookie_str, _ = _session(browser)
    detail = fetch_item_detail(cookie_str, p["item_id"])
    if not detail:
        raise RuntimeError("详情为空（商品可能已下架/删除）")
    item = detail.get("itemDO") or {}
    # 图片清单 + 描述文本（发布回填）
    payload = {
        "item_id": p["item_id"],
        "title": p.get("title", "") or item.get("title", ""),
        "desc": (item.get("desc") or "").strip(),
        "imgs": _extract_imgs(detail),
        "price": p.get("price") or item.get("soldPrice") or 0,
        "category": (item.get("categoryId") or ""),
        "detail": detail,
    }
    with db.get_conn() as conn:
        conn.execute("UPDATE products SET publish_payload=? WHERE id=?",
                     (json.dumps(payload, ensure_ascii=False), pid))
    db.log_op("info", "sync",
              f"已保存商品发布信息: id={pid} title={payload['title'][:30]} "
              f"imgs={len(payload['imgs'])} desc={len(payload['desc'])}")
    return {"ok": True, "payload_len": len(json.dumps(payload))}


def auto_relist_pass(browser):
    """重新发布巡检：到期（开启重新发布+间隔到点）的已售商品 → 网页真实重新发布。
    注意：一次巡检至多发布 1 个；发布失败/未确认后进入冷却期，防止重复发布。"""
    # 冷却防护：最近一次发布未确认或失败后，短时间内不再自动重发
    try:
        blocked_until = db.get_setting("relist_block_until", "")
        if blocked_until:
            import datetime as _dt
            bd = _dt.datetime.strptime(blocked_until, "%Y-%m-%d %H:%M:%S")
            if _dt.datetime.now() < bd:
                return {"due": -1, "done": 0, "blocked_until": blocked_until}
    except Exception:
        pass
    due = db.auto_relist_due_products()
    if not due:
        return {"due": 0, "done": 0}
    p = due[0]
    done = 0
    try:
        res = republish_product(browser, p["id"])
        if res.get("ok") and not res.get("pending"):
            done = 1
        elif res.get("pending"):
            # 发布已执行但未能确认新链接：进入冷却，避免再次自动重发
            _block_relist(30 * 60)
    except Exception as e:
        db.log_op("error", "sync", f"重新发布失败 {p['title']}: {type(e).__name__} {e}")
        _block_relist(30 * 60)
    db.log_op("info", "sync", f"重新发布巡检: 到期{len(due)}个，本次处理1个，成功{done}个")
    return {"due": len(due), "done": done}


def _block_relist(seconds=1800):
    """设置自动重新发布冷却（发布失败/未确认后防止重复真实上架）。参数为秒。"""
    import datetime as _dt
    until = (_dt.datetime.now() + _dt.timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    db.set_setting("relist_block_until", until)
    db.log_op("warn", "sync",
              f"自动重新发布已冷却 {int(seconds // 60)} 分钟（至 {until}），避免重复上架")


# ---------- 真实重新发布（网页 /publish 发闲置，模拟人工填表） ----------
def _download_to_file(url, dest):
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Referer": "https://www.goofish.com/",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def republish_product(browser, pid):
    """用已保存的发布信息在网页 /publish 重新发布商品（真实上架，高风险写操作）。
    流程：下载原图到本地 → /publish 上传图片 → 填描述 → 等属性识别 → 填价格
    → 发货方式 → 点发布 → 成功后解析新 item_id 并回写本地。
    返回 {"ok": True, "item_id": 新链接} 或抛异常。"""
    p = _get_product(pid)
    if not p:
        raise RuntimeError("商品不存在")
    if not p.get("publish_payload"):
        raise RuntimeError("尚未保存发布信息，请先在商品行点『存发布信息』")
    try:
        payload = json.loads(p["publish_payload"])
    except Exception:
        raise RuntimeError("发布信息已损坏，请重新保存")
    imgs = payload.get("imgs") or []
    if not imgs:
        raise RuntimeError("发布信息缺少图片，请重新保存")
    title = (payload.get("title") or p.get("title") or "").strip()
    desc = (payload.get("desc") or "").strip()
    price = p.get("price") or payload.get("price") or 0
    delivery = (p.get("delivery_mode") or "").strip()  # ''=包邮 / no_mail=无需邮寄 / buyout=一口价
    if not desc:
        raise RuntimeError("发布信息缺少描述，请重新保存")
    # 发布时间窗口（风控：真实发布是写操作，错开高峰+最低间隔）
    min_interval = float(db.get_setting("relist_min_interval_sec", str(PUBLISH_MIN_INTERVAL_SEC)))
    last_pub = db.get_setting("last_relist_ts", "")
    if last_pub:
        import datetime
        try:
            last_dt = datetime.datetime.strptime(last_pub, "%Y-%m-%d %H:%M:%S")
            gap = (datetime.datetime.now() - last_dt).total_seconds()
            if gap < min_interval:
                wait = min_interval - gap
                db.log_op("info", "sync", f"距上次发布不足{min_interval}s，等待{int(wait)}s后发布")
                time.sleep(wait)
        except Exception:
            pass

    page = browser.page()
    if "publish" not in page.url:
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(8)

    # 下载图片到本地临时文件后填表发布
    try:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="xy_relist_") as td:
            local_files = []
            for i, url in enumerate(imgs[:6]):  # 最多 6 张
                dest = os.path.join(td, f"img_{i}.jpg")
                try:
                    _download_to_file(url, dest)
                    if os.path.getsize(dest) > 1024:
                        local_files.append(dest)
                except Exception as e:
                    db.log_op("warn", "sync", f"图片下载失败 {url[:60]}: {e}")
            if not local_files:
                raise RuntimeError("全部图片下载失败，无法发布")
            return _republish_fill(page, browser, local_files, desc, price, delivery, pid, title,
                                   old_item_id=p.get("item_id"))
    except Exception as e:
        db.log_op("error", "sync", f"重新发布异常: {type(e).__name__} {e}")
        raise


def _republish_fill(page, browser, local_files, desc, price, delivery, pid, title, old_item_id=None):
    """填表并发布（必须在浏览器属主线程调用）"""
    # 上传图片（一次多选全部图片，保持原商品图集顺序）
    try:
        file_input = page.locator("input[type=file]").first
        file_input.set_input_files(local_files)
        db.log_op("info", "sync", f"发布图片已上传({len(local_files)}张)")
        time.sleep(10)
    except Exception as e:
        db.log_op("warn", "sync", f"图片上传失败: {type(e).__name__} {e}")

    # 描述：逐行输入以保留原换行/空行格式（fill 会把空行翻倍导致段落错乱）
    try:
        ed = page.locator("[contenteditable=true]").first
        ed.click()
        time.sleep(1)
        lines = (desc or "").split("\n")
        for line in lines:
            if line:
                # 单段文本用 insertText 注入（避免 keyboard 逐字慢）
                page.evaluate(
                    "(t) => document.execCommand('insertText', false, t)", line)
            page.keyboard.press("Enter")  # 每行（含空行）后回车，保留段落结构
        db.log_op("info", "sync", f"发布描述已填({len(lines)}段)")
        time.sleep(6)  # 等属性智能识别
    except Exception as e:
        raise RuntimeError(f"描述填写失败: {e}")

    # 可选属性兜底：成色选"几乎全新"（若可选项出现且未选中；非必填跳过即可）
    try:
        sel_texts = page.evaluate("() => Array.from(document.querySelectorAll('.ant-select-selection-item')).map(e => e.innerText)")
        if not any("全新" in s for s in sel_texts):
            r = page.evaluate("""() => {
                const items = Array.from(document.querySelectorAll('.ant-form-item'));
                for (const it of items) {
                    const t = (it.innerText || '');
                    if (t.includes('成色') && t.includes('请选择')) {
                        const sel = it.querySelector('.ant-select');
                        if (sel) { const r = sel.getBoundingClientRect();
                            return {x: r.x + r.width / 2, y: r.y + r.height / 2}; }
                    }
                }
                return null;
            }""")
            if r:
                page.mouse.click(r["x"], r["y"])
                time.sleep(2)
                try:
                    page.locator('.ant-select-item-option:has-text("几乎全新")').first.click()
                    db.log_op("info", "sync", "成色已选: 几乎全新")
                    time.sleep(1)
                except Exception:
                    pass  # 非必填，选不上不强求
    except Exception:
        pass

    # 价格
    try:
        price_box = page.locator("input.ant-input:visible").first
        price_box.click()
        time.sleep(0.5)
        price_box.fill(str(float(price)))
        db.log_op("info", "sync", f"发布价格已填: {price}")
    except Exception as e:
        raise RuntimeError(f"价格填写失败: {e}")

    # 发货方式
    try:
        dm = {"": "包邮", "no_mail": "无需邮寄", "buyout": "一口价", "post": "按距离计费"}.get(delivery, "包邮")
        page.locator(f'.ant-radio-wrapper:has-text("{dm}")').first.click()
        db.log_op("info", "sync", f"发货方式: {dm}")
    except Exception as e:
        db.log_op("warn", "sync", f"发货方式选择失败(默认包邮): {e}")

    time.sleep(2)
    # 发布前核对：无必填错误
    try:
        errs = page.evaluate(
            "() => Array.from(document.querySelectorAll('.ant-form-item-explain-error')).map(e => (e.innerText||'').trim()).filter(Boolean)",
            timeout=5000)
    except Exception:
        errs = []
    if errs:
        raise RuntimeError(f"发布表单校验未通过: {errs[:3]}")

    # 点击发布（真实上架！）——所有调用带显式超时，防止卡死监听主循环
    db.log_op("warn", "sync", f"即将真实发布商品: {title[:30]} (pid={pid})")
    try:
        page.locator("button:has-text('发布')").last.click(timeout=8000)
    except Exception:
        try:
            r = page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button'));
                for (const b of btns) {
                    if ((b.innerText||'').trim() === '发布') {
                        const r = b.getBoundingClientRect();
                        return {x: r.x + r.width/2, y: r.y + r.height/2, disabled: b.disabled};
                    }
                }
                return null;
            }""", timeout=5000)
            if not r:
                raise RuntimeError("未找到发布按钮")
            if r.get("disabled"):
                raise RuntimeError("发布按钮不可用（仍有必填项未完成）")
            page.mouse.click(r["x"], r["y"], timeout=8000)
        except Exception as e2:
            raise RuntimeError(f"点击发布失败: {type(e2).__name__} {e2}")
    db.log_op("info", "sync", "已点击发布按钮，等待发布结果...")
    time.sleep(8)

    # 等待发布结果（最多 ~30s；不无限轮询——监听主循环不能被阻塞）
    new_item_id = None
    for _ in range(12):
        time.sleep(2)
        try:
            url = page.url
            body = page.evaluate("() => (document.body.innerText || '').slice(0, 800)", timeout=4000)
            m = re.search(r'item(?:Id|_id)?[=/](\d{8,20})', url) or \
                re.search(r'itemId=(\d{8,20})', body)
            if m:
                new_item_id = m.group(1)
            if new_item_id or "发布成功" in body or "上架成功" in body:
                break
        except Exception:
            continue  # 页面跳转/暂时不可用，继续等
    # 保存现场截图（供人工核对）
    try:
        page.screenshot(path=str(config.DATA_DIR / f"relist_result_{pid}_{int(time.time())}.png"),
                        timeout=6000)
    except Exception:
        pass

    if not new_item_id:
        # 页面解析失败：用 mtop 回查在售列表（发布后列表同步有延迟，多轮重试）
        for attempt in range(3):
            try:
                new_item_id = _find_new_item_after_publish(browser, title, old_item_id)
                if new_item_id:
                    break
            except Exception as e:
                db.log_op("warn", "sync", f"发布后回查新链接失败(第{attempt+1}次): {e}")
            time.sleep(8)
    if new_item_id and new_item_id != old_item_id:
        # 回写本地：同一商品行沿用（新 item_id = 新链接）
        with db.get_conn() as conn:
            conn.execute("UPDATE products SET item_id=?, sku=?, status='selling', "
                         "listed_at=datetime('now','localtime'), sold_at=NULL WHERE id=?",
                         (new_item_id, new_item_id, pid))
        # 清理 sync 可能误建的同标题新本地行（保留真实链接不删，仅去重本地行）
        _dedupe_relist_rows(pid, new_item_id, title)
        db.set_setting("last_relist_ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        db.log_op("info", "sync", f"重新发布成功: pid={pid} 新链接={new_item_id}")
        return {"ok": True, "item_id": new_item_id}

    # 未能确认新链接：置为在售待确认，避免重复发布；人工可核对截图后处理
    with db.get_conn() as conn:
        conn.execute("UPDATE products SET sold_at=NULL, status=? WHERE id=?",
                     ("selling", pid))
    db.log_op("warn", "sync",
              f"发布已点击但未能确认新链接 pid={pid}，已置为在售待确认，请核对网页/截图")
    return {"ok": True, "pending": True, "item_id": None}


def _find_new_item_after_publish(browser, title, old_item_id):
    """发布后回查在售列表：找标题一致、item_id 非原链接的新商品（发布成功标志）"""
    cookie_str, user_id = _session(browser)
    if not user_id:
        return None
    title = (title or "").strip()
    for page in range(1, 3):
        items, _ = fetch_item_list(cookie_str, user_id, page_number=page, page_size=20)
        if not items:
            break
        for it in items:
            it_id = str(it.get("id") or it.get("itemId") or "")
            it_title = str(it.get("title") or "")
            if it_id and it_id != str(old_item_id) and it_title.strip() == title:
                return it_id
    return None


def _dedupe_relist_rows(pid, new_item_id, title):
    """清理重发后 sync 可能新建的同标题本地行（仅本地删除，不动真实链接）"""
    try:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id FROM products WHERE id != ? AND title=? AND item_id=?",
                (pid, title, new_item_id)).fetchall()
            for r in rows:
                conn.execute("DELETE FROM products WHERE id=?", (r["id"],))
                conn.execute("DELETE FROM card_keys WHERE product_id=?", (r["id"],))
                conn.execute("DELETE FROM reply_rules WHERE product_id=?", (r["id"],))
    except Exception:
        pass
