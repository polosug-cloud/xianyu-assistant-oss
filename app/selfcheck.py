"""闲鱼助手 - 自检任务（新自检：优先准确判在线，再虚拟演练各业务链路，dry-run 不真实写）

设计原则：
  - 只读 + 离线模拟：不真实发送消息、不真实发货、不真实上架。
  - "虚拟"= 用真实代码路径（规则引擎/卡密预检/重发前置检查）跑一遍，设预期结果。
  - 在线判断用服务端会话验证（mtop loginuser.get），非仅本地 cookie。
运行位置：必须在浏览器属主线程（listener 主循环 / 经 task_queue 触发），因需 browser.verify_session_live()。
"""
import json
import time

from . import config, db

_ws_provider = None


def set_ws_provider(fn):
    """注册 WS/监听状态提供者（listener 调用），供自检读取通道活性。"""
    global _ws_provider
    _ws_provider = fn


def _ws_stats():
    try:
        if _ws_provider is None:
            return {}
        return _ws_provider() or {}
    except Exception:
        return {}


def _fmt_seconds(sec):
    if sec >= 86400:
        return f"{int(sec // 86400)}天"
    if sec >= 3600:
        return f"{int(sec // 3600)}小时{int((sec % 3600) // 60)}分"
    if sec >= 60:
        return f"{int(sec // 60)}分{int(sec % 60)}秒"
    return f"{int(sec)}秒"


def run_selfcheck(browser, reply_engine=None):
    """执行一次完整自检。返回 (ok: bool, report: str)。
    每次都重新完整运行：在线验证实时打 mtop；虚拟收发走真实规则引擎；
    虚拟发货逐商品模拟"将扣哪张卡"；虚拟重发验证到期商品发布信息并真实探测首图可达。"""
    # 标记运行中（供页面显示"⏳ 自检中…"，避免误读上次缓存结果）
    db.set_setting("selfcheck_running", "1")
    db.log_op("info", "selfcheck", "自检开始：重新运行全量自检脚本…")
    st = browser.login_status()
    checks = []
    paused = db.get_setting("agent_paused", "0") == "1"

    # ---------- ① 账号在线（首要：服务端真实验证） ----------
    if st["status"] != "online" or not browser.is_logged_in():
        checks.append(("账号在线(服务端验证)", False,
                       f"本地状态={st['status']} 未登录/会话缺失 → 需重新扫码登录"))
    else:
        live = None
        try:
            live = browser.verify_session_live()
        except Exception as e:
            live = None
        if live is True:
            checks.append(("账号在线(服务端验证)", True,
                           "mtop 服务端会话验证通过（真实在线）"))
        elif live is False:
            checks.append(("账号在线(服务端验证)", False,
                           "服务端会话已过期（本地 cookie 仍在但服务端失效）→ 需重新扫码"))
        else:
            checks.append(("账号在线(服务端验证)", False,
                           "会话探测无结果（网络/接口异常），无法确认真实在线"))

    # ---------- ② 消息监听 / WS 通道活性 ----------
    ws = _ws_stats()
    running = bool(ws.get("running"))
    conns = int(ws.get("ws_conns") or 0)
    if running and conns >= 1:
        checks.append(("消息监听/WS 通道", True,
                       f"监听运行中，WS 连接 {conns} 条（收消息通道就绪）"))
    else:
        checks.append(("消息监听/WS 通道", False,
                       f"监听={'运行中' if running else '已停止'} WS连接={conns} → 消息通道不可用"))

    # ---------- ③ 虚拟收发消息 + 虚拟自动回复（离线模拟引擎链路，不真实发送） ----------
    try:
        with db.get_conn() as conn:
            rules = [dict(r) for r in conn.execute(
                "SELECT * FROM reply_rules WHERE enabled=1 ORDER BY id LIMIT 60").fetchall()]
        if not rules:
            checks.append(("虚拟自动回复", False,
                           "无启用规则：虚拟买家消息无回复可命中（请先添加通用规则）"))
        else:
            from .rules import match_rule, render_template
            probe = "在吗 有货吗 怎么发货"
            hit, kw = match_rule(rules, probe)
            if hit:
                tpl = render_template(hit["reply_template"], {"买家": "自检", "商品名": "", "卡密": ""})
                checks.append(("虚拟收发/自动回复", True,
                               f"虚拟买家消息命中规则「{(hit.get('name') or '')[:12]}」(kw={kw})，"
                               f"将回复: {tpl[:36]}… （dry-run 未真实发送）"))
            else:
                checks.append(("虚拟收发/自动回复", False,
                               f"有 {len(rules)} 条启用规则但探针消息均未命中（检查关键词配置）"))
    except Exception as e:
        checks.append(("虚拟收发/自动回复", False, f"引擎异常: {type(e).__name__}: {str(e)[:60]}"))

    # ---------- ④ 虚拟拍下→自动发货（逐商品模拟"将扣哪张卡"，dry-run 不真扣/不真发） ----------
    try:
        with db.get_conn() as conn:
            deliver_rows = [dict(r) for r in conn.execute(
                "SELECT id, title, internal_sku FROM products "
                "WHERE status='selling' AND auto_deliver=1 ORDER BY id LIMIT 8").fetchall()]
        if not deliver_rows:
            checks.append(("虚拟自动发货", True,
                           "无开启自动发货的在售商品（跳过；新增商品开启后可复检）"))
        else:
            details, bad = [], []
            for p in deliver_rows:
                ok_stock, _note = db.has_stock_for_product(p["id"])
                src, kid = db.peek_deliver_key(p["id"], p.get("internal_sku") or "")
                if not ok_stock or kid is None:
                    bad.append(f"「{(p['title'] or '')[:14]}」无可用卡密")
                    details.append(f"「{(p['title'] or '')[:10]}」→ 缺卡")
                else:
                    details.append(f"「{(p['title'] or '')[:10]}」→ 模拟扣卡 {src}#{kid}")
            checks.append(("虚拟自动发货", not bad,
                           (f"{len(deliver_rows)} 个在售自动发货商品：{'；'.join(details[:4])}"
                            f"{'…' if len(details) > 4 else ''}"
                            if not bad else f"缺卡密: {'; '.join(bad[:3])}") + "（dry-run 未真扣）"))
    except Exception as e:
        checks.append(("虚拟自动发货", False, f"预检异常: {type(e).__name__}: {str(e)[:60]}"))

    # ---------- ⑤ 虚拟已售→重新发布（到期商品模拟重发：校验发布信息并真实探测首图可达） ----------
    try:
        due = db.auto_relist_due_products()[:3]
        with db.get_conn() as conn:
            relist_rows = [dict(r) for r in conn.execute(
                "SELECT id, title, publish_payload FROM products "
                "WHERE auto_relist=1 ORDER BY id LIMIT 8").fetchall()]
        if not relist_rows:
            checks.append(("虚拟已售重新发布", True,
                           "无开启【重新发布】的商品（跳过；开启后可复检）"))
        elif due:
            import urllib.request as _ur
            details, ok_all = [], True
            for p in due:
                try:
                    pl = json.loads(p.get("publish_payload") or "")
                    imgs = pl.get("imgs") or []
                    url = imgs[0] if imgs else ""
                    if not (pl.get("desc") or "").strip() or not url:
                        ok_all = False
                        details.append(f"「{(p['title'] or '')[:12]}」发布信息不完整")
                        continue
                    # 真实探测重发将用的首图是否可达（读前 8KB 验证，非仅查库）
                    req = _ur.Request(url, headers={
                        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
                        "Referer": "https://www.goofish.com/"})
                    with _ur.urlopen(req, timeout=8) as resp:
                        got = len(resp.read(8192))
                    details.append(f"「{(p['title'] or '')[:12]}」已到期将重发，首图可达({got}B)")
                except Exception as e:
                    ok_all = False
                    details.append(f"「{(p['title'] or '')[:12]}」首图探测失败 {type(e).__name__}")
            checks.append(("虚拟已售重新发布", ok_all,
                           "；".join(details) + "（模拟重发流程，dry-run 未真实上架）"))
        else:
            bad = []
            for p in relist_rows:
                try:
                    pl = json.loads(p.get("publish_payload") or "")
                    if not (pl.get("imgs") or []):
                        bad.append(f"「{(p['title'] or '')[:14]}」发布信息无图片")
                    if not (pl.get("desc") or "").strip():
                        bad.append(f"「{(p['title'] or '')[:14]}」发布信息无描述")
                except Exception:
                    bad.append(f"「{(p['title'] or '')[:14]}」发布信息损坏")
            checks.append(("虚拟已售重新发布", not bad,
                           (f"{len(relist_rows)} 个商品重发配置完整，暂未到期"
                            f"（dry-run 未真实上架）" if not bad else f"配置不完整: {'; '.join(bad[:3])}")))
    except Exception as e:
        checks.append(("虚拟已售重新发布", False, f"预检异常: {type(e).__name__}: {str(e)[:60]}"))

    # ---------- ⑥ 数据完整性（附加） ----------
    try:
        with db.get_conn() as conn:
            avail_keys = conn.execute(
                "SELECT COUNT(*) c FROM card_keys WHERE status='available'").fetchone()["c"]
            errs = conn.execute("SELECT COUNT(*) c FROM op_logs WHERE level='error' "
                                "AND ts >= datetime('now','localtime','-1 hour')").fetchone()["c"]
        checks.append(("数据完整性", True,
                       f"可用卡密 {avail_keys} 张 ｜ 近1小时错误日志 {errs} 条"))
    except Exception as e:
        checks.append(("数据完整性", False, f"读取失败: {type(e).__name__}"))

    # ---------- 汇总 ----------
    fails = [c for c in checks if not c[1]]
    lines = [f"【闲鱼助手 · 自检报告 {time.strftime('%Y-%m-%d %H:%M')}】"]
    for name, ok_flag, note in checks:
        lines.append(f"  {'✅' if ok_flag else '❌'} {name}: {note}")
    lines.append(f"暂停状态: {'⏸ 已暂停' if paused else '▶️ 运行中'}")
    lines.append(f"结论: {'全部正常 ✅' if not fails else f'{len(fails)} 项异常，请查看日志/修复后重检'}")
    ok = not fails
    report = "\n".join(lines)

    # 结果落库（供日志区/页面/系统提示音）
    db.set_setting("last_selfcheck_ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    db.set_setting("last_selfcheck_ok", "1" if ok else "0")
    db.set_setting("last_selfcheck_result", report)
    db.set_setting("selfcheck_running", "0")
    db.log_op("info", "selfcheck",
              f"自检完成: {'全部正常' if ok else str(len(fails)) + ' 项异常'} "
              f"（{', '.join(c[0] for c in fails) if fails else 'OK'}）")
    return ok, report
