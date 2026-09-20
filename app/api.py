"""闲鱼助手 - FastAPI 路由层（轻量管理接口）"""
import base64
import json
import os
import secrets
import time

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from . import config, db
from .browser import BrowserSession
from .deliverer import Deliverer
from .rules import ReplyEngine
from .sync import (auto_relist_pass, do_delete,
                   republish_product, save_publish_info, sync_products)
from .tasks import task_queue
from . import donation_assets
from . import screenshot_util
from . import selfcheck
from . import listener as listener_mod

app = FastAPI(title="闲鱼助手", version="1.0.0")

browser = BrowserSession()
reply_engine = ReplyEngine()


def _load_or_create_token():
    """令牌持久化：重启不变（否则浏览器缓存的令牌会失效）"""
    if config.WEB_PASS:
        return config.WEB_PASS
    with db.get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='web_token'").fetchone()
        if row:
            return row["value"]
        tok = secrets.token_urlsafe(16)
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('web_token', ?)", (tok,))
        return tok


_web_token = _load_or_create_token()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api"):
        # 免鉴权端点：令牌获取 + 前台/静默模式标记
        # （后者由页面 pagehide 用 sendBeacon 上报，无法携带 Authorization 头）
        if request.url.path in ("/api/auth/token", "/api/ui/mode"):
            return await call_next(request)
        if request.headers.get("authorization", "") != f"Bearer {_web_token}":
            # 注意：中间件内不能用 raise HTTPException（会变 500），必须返回 JSONResponse
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/api/auth/token")
def get_token():
    return {"token": _web_token, "user": config.WEB_USER}


@app.get("/api/status")
def status():
    return {
        "browser": browser.login_status(),
        "logged_in": browser.is_logged_in(),
        "ai_enabled": config.AI_ENABLED,
        "db": str(config.DB_PATH),
        # 身份标识（纯 ASCII）：托盘用于判断"该端口上的实例是否属于本账号"。
        # account_id 由托盘启动时通过 XY_ACCOUNT_ID 注入；db_b64 为数据库路径的 base64，
        # 避免中文路径经接口传输后编码不一致导致比对失败。
        "account_id": config.ACCOUNT_ID,
        "db_b64": base64.b64encode(str(config.DB_PATH).encode("utf-8")).decode("ascii"),
        "version": config.APP_VERSION,
        "agent_paused": db.get_setting("agent_paused", "0") == "1",
        # 声音开关（后端全局值，托盘/多页面共用；浏览器本地值随其后）
        "sound": {
            "sys": db.get_setting("sound_sys_enabled", "1") == "1",
            "msg": db.get_setting("sound_msg_enabled", "1") == "1",
        },
        # 自检摘要（页面用于展示最近结果 + 触发系统提示音）
        "selfcheck": {
            "running": db.get_setting("selfcheck_running", "") == "1",
            "ts": db.get_setting("last_selfcheck_ts", ""),
            "ok": db.get_setting("last_selfcheck_ok", "") == "1",
            "result": db.get_setting("last_selfcheck_result", ""),
        },
        "last_msg_id": db.latest_message_id(),
    }


@app.get("/api/tools/screenshot")
def tools_screenshot():
    """系统级截屏（虚拟桌面全屏 PNG → base64），供聊天窗口"📷 截图"使用，
    替代浏览器"屏幕共享选择器"（其流程难以使用）。
    截图在本机后端进程内完成，不经过浏览器捕获。"""
    try:
        png, w, h = screenshot_util.capture_screen_png()
    except Exception as e:
        raise HTTPException(500, f"系统截图失败：{e}")
    return {
        "ok": True,
        "width": w,
        "height": h,
        "png_b64": base64.b64encode(png).decode("ascii"),
    }


@app.post("/api/tools/snipping-tool")
def tools_snipping_tool():
    """启动 Windows 系统截图工具（SnippingTool / ms-screenclip 画布）。
    用户系统框选后按 Ctrl+C（自动入剪贴板），回页面在聊天输入框 Ctrl+V 粘贴发送。
    返回提示语供前端展示。"""
    import subprocess as _sp
    import os as _os
    hint = ""
    # 方式1：经典 SnippingTool（多数系统自带）
    try:
        _sp.Popen(["SnippingTool.exe"])
        hint = "已打开系统截图：框选区域后 Ctrl+C（或点复制）"
        return {"ok": True, "hint": hint}
    except Exception:
        pass
    # 方式2：Win10/11 截图画布（Win+Shift+S 同款）
    try:
        _os.startfile("ms-screenclip:")
        hint = "已打开系统截图：框选区域后会自动复制到剪贴板"
        return {"ok": True, "hint": hint}
    except Exception:
        pass
    raise HTTPException(500, "无法启动系统截图工具，请使用 Win+Shift+S 截图后 Ctrl+V 粘贴到输入框")


@app.post("/api/login/start")
def login_start():
    """启动扫码登录：推给属主线程执行（浏览器线程亲和）。
    登录页二维码将保存到 data/login_qr.png，页面轮询 /api/status 观察进度。"""
    def run():
        try:
            res = browser.start_login()
            db.log_op("info", "api", f"登录流程启动: {res}")
        except Exception as e:
            db.log_op("error", "api", f"登录流程异常: {type(e).__name__} {e}")
    task_queue.push(run)
    return {"accepted": True, "note": "登录二维码将写入 data/login_qr.png，请刷新页面查看"}


@app.get("/api/login/qr")
def login_qr():
    p = config.DATA_DIR / "login_qr.png"
    if not p.exists():
        raise HTTPException(404, "qr not ready")
    return FileResponse(str(p), media_type="image/png")


@app.get("/api/sounds/{kind}")
def sound_file(kind: str):
    """提示音文件：sys=系统提示音（自检异常时响）/ msg=消息提示音（收到买家消息时响）。
    文件位置见 config.SOUND_SYS_FILE / SOUND_MSG_FILE（默认本机路径，可用环境变量覆盖）。"""
    if kind == "sys":
        p = config.SOUND_SYS_FILE
    elif kind == "msg":
        p = config.SOUND_MSG_FILE
    else:
        raise HTTPException(404, "unknown sound kind")
    if not os.path.exists(p):
        raise HTTPException(404, f"sound file not found: {p}")
    return FileResponse(p, media_type="audio/ogg")


# ---------- 捐赠与支持：图片资源 ----------
@app.get("/api/support/qr")
def support_qr():
    """返回「捐赠与支持」弹窗用的图片资源（随程序内置；资源缺失/校验失败时 404）。"""
    data, mime = donation_assets.load_donation_qr()
    if not data:
        raise HTTPException(404, "资源缺失或校验失败")
    return Response(content=data, media_type=mime)


# ---------- 回复规则 ----------
def _rule_out(r: dict) -> dict:
    """规则行输出：附带解析后的 product_ids 数组（多商品兼容）"""
    out = dict(r)
    out["product_ids"] = db.rule_product_ids(r)
    return out


@app.get("/api/rules")
def list_rules(product_id: int = None):
    """规则列表；product_id 给定则只返回通用+适用该商品的规则"""
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM reply_rules ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if product_id is not None and not db.rule_matches_product(d, product_id):
            continue
        out.append(_rule_out(d))
    return out


def _clean_product_ids(payload) -> list:
    """从 payload 提取适用商品 id 列表（支持 product_ids 数组 / 旧 product_id 单值）"""
    if "product_ids" in payload:
        v = payload.get("product_ids") or []
        if isinstance(v, list):
            return [int(x) for x in v if str(x).isdigit()]
        return []
    if payload.get("product_id"):
        return [int(payload["product_id"])]
    return []


@app.post("/api/rules")
def create_rule(payload: dict):
    ids = _clean_product_ids(payload)
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reply_rules(name, match_type, keywords, reply_template, "
            "cooldown_sec, product_ids, product_id) VALUES (?,?,?,?,?,?,?)",
            (payload.get("name", ""), payload.get("match_type", "contains"),
             payload.get("keywords", ""), payload.get("reply_template", ""),
             payload.get("cooldown_sec", config.RULE_COOLDOWN_SEC),
             json.dumps(ids, ensure_ascii=False),
             (ids[0] if len(ids) == 1 else 0)),
        )
        return {"id": cur.lastrowid}


@app.put("/api/rules/{rid}")
def update_rule(rid: int, payload: dict):
    """更新规则：仅更新 payload 中存在的字段（product_ids 支持多商品数组）"""
    sets, args = [], []
    for f in ("name", "match_type", "keywords", "reply_template", "enabled", "cooldown_sec"):
        if f in payload:
            sets.append(f"{f}=?")
            args.append(payload[f])
    if "product_ids" in payload or "product_id" in payload:
        ids = _clean_product_ids(payload)
        sets.append("product_ids=?")
        args.append(json.dumps(ids, ensure_ascii=False))
        sets.append("product_id=?")
        args.append(ids[0] if len(ids) == 1 else 0)
    if not sets:
        return {"ok": True}
    args.append(rid)
    with db.get_conn() as conn:
        conn.execute(f"UPDATE reply_rules SET {', '.join(sets)} WHERE id=?", args)
    return {"ok": True}


@app.delete("/api/rules/{rid}")
def delete_rule(rid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM reply_rules WHERE id=?", (rid,))
    return {"ok": True}


@app.delete("/api/rules/group/{product_id}")
def delete_rule_group(product_id: int):
    """删除适用于某商品(或通用组=0)的全部规则"""
    deleted = db.delete_rules_for_product(product_id)
    return {"ok": True, "deleted": deleted}


# ---------- 规则导入/导出（CSV） ----------
_RULE_CSV_HEADER = ["名称", "匹配类型", "关键词", "回复模板", "适用内部SKU", "启用", "冷却秒"]


def _rule_sku_text(d: dict) -> str:
    """规则适用商品的内部SKU列表（逗号分隔，多个用 | 分隔）"""
    ids = db.rule_product_ids(d)
    if not ids:
        return ""
    skus = []
    with db.get_conn() as conn:
        for pid in ids:
            row = conn.execute("SELECT internal_sku, title FROM products WHERE id=?", (pid,)).fetchone()
            if row:
                skus.append((row["internal_sku"] or "").strip() or (row["title"] or f"#{pid}"))
    return "|".join([s for s in skus if s])


@app.get("/api/rules/export")
def export_rules_csv():
    """导出全部自动回复规则为 CSV（UTF-8 BOM，Excel 可直接打开）"""
    import csv as _csv
    import io as _io
    with db.get_conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM reply_rules ORDER BY id").fetchall()]
    buf = _io.StringIO()
    w = _csv.writer(buf, lineterminator="\r\n")
    w.writerow(_RULE_CSV_HEADER)
    for d in rows:
        w.writerow([
            d.get("name", ""),
            d.get("match_type", "contains"),
            d.get("keywords", ""),
            d.get("reply_template", ""),
            _rule_sku_text(d),
            "是" if d.get("enabled") else "否",
            d.get("cooldown_sec", config.RULE_COOLDOWN_SEC),
        ])
    ts = time.strftime("%Y%m%d_%H%M%S")
    fname = f"reply_rules_{ts}.csv"
    return Response(content=buf.getvalue().encode("utf-8-sig"),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/rules/import")
async def import_rules_csv(file: UploadFile = File(...)):
    """从 CSV 导入规则：按「名称」匹配——同名更新，新名新增；
    「适用内部SKU」为空=通用规则；多个 SKU 用 | 或 , 分隔（找不到的 SKU 记入 skipped）。"""
    import csv as _csv
    import io as _io
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "空文件")
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        raise HTTPException(400, "无法识别文件编码（请用 UTF-8 或 GBK 保存的 CSV）")
    try:
        rows = list(_csv.reader(_io.StringIO(text)))
    except Exception as e:
        raise HTTPException(400, f"CSV 解析失败：{e}")
    if not rows:
        raise HTTPException(400, "CSV 内容为空")

    # 表头映射（缺列时按默认顺序兜底）
    header = [h.strip().lstrip("\ufeff") for h in rows[0]]
    idx = {}
    for i, h in enumerate(header):
        idx[h] = i

    def cell(r, key, default=""):
        i = idx.get(key)
        if i is None or i >= len(r):
            return default
        return (r[i] or "").strip()

    if not idx:
        raise HTTPException(400, "缺少表头（需包含：名称,匹配类型,关键词,回复模板,适用内部SKU,启用）")

    created = updated = skipped_empty = 0
    sku_missing = []
    with db.get_conn() as conn:
        for r in rows[1:]:
            if not any((c or "").strip() for c in r):
                continue
            name = cell(r, "名称")
            keywords = cell(r, "关键词")
            template = cell(r, "回复模板")
            if not keywords or not template:
                skipped_empty += 1
                continue
            mtype = cell(r, "匹配类型") or "contains"
            if mtype not in ("contains", "regex", "all"):
                # 允许中文写法
                mtype = {"包含": "contains", "正则": "regex", "全含": "all"}.get(mtype, "contains")
            enabled = 1 if cell(r, "启用", "是") in ("是", "1", "true", "True", "启用", "") else 0
            try:
                cd = int(float(cell(r, "冷却秒", "") or config.RULE_COOLDOWN_SEC))
            except Exception:
                cd = config.RULE_COOLDOWN_SEC
            sku_text = cell(r, "适用内部SKU")
            pids = []
            if sku_text:
                for sku in [s.strip() for s in sku_text.replace("，", ",").replace("|", ",").split(",") if s.strip()]:
                    row = conn.execute(
                        "SELECT id FROM products WHERE internal_sku=? OR title=? LIMIT 1", (sku, sku)).fetchone()
                    if row:
                        pids.append(int(row["id"]))
                    else:
                        sku_missing.append(sku)
            if not name:
                name = (keywords[:12] or "导入规则")
            exist = conn.execute("SELECT id FROM reply_rules WHERE name=? LIMIT 1", (name,)).fetchone()
            if exist:
                conn.execute(
                    "UPDATE reply_rules SET match_type=?, keywords=?, reply_template=?, enabled=?, "
                    "cooldown_sec=?, product_ids=?, product_id=? WHERE id=?",
                    (mtype, keywords, template, enabled, cd, json.dumps(pids, ensure_ascii=False),
                     (pids[0] if len(pids) == 1 else 0), exist["id"]))
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO reply_rules(name, match_type, keywords, reply_template, enabled, "
                    "cooldown_sec, product_ids, product_id) VALUES (?,?,?,?,?,?,?,?)",
                    (name, mtype, keywords, template, enabled, cd,
                     json.dumps(pids, ensure_ascii=False), (pids[0] if len(pids) == 1 else 0)))
                created += 1
    db.log_op("info", "api", f"规则CSV导入：新增 {created}，更新 {updated}，跳过(缺关键词/模板) {skipped_empty}")
    return {"ok": True, "created": created, "updated": updated,
            "skipped_empty": skipped_empty, "sku_missing": sorted(set(sku_missing))}


# ---------- 卡密库存 ----------
@app.get("/api/card-keys")
def list_keys(product_id: int = None, status: str = None):
    sql = "SELECT * FROM card_keys WHERE 1=1"
    args = []
    if product_id:
        sql += " AND product_id=?"
        args.append(product_id)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT 500"
    with db.get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/card-keys/import")
def import_keys(payload: dict):
    """批量导入卡密：{product_id, keys: "一行一个"}"""
    product_id = payload.get("product_id")
    keys = [k.strip() for k in (payload.get("keys") or "").splitlines() if k.strip()]
    if not product_id or not keys:
        raise HTTPException(400, "need product_id and keys")
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO card_keys(product_id, key_content) VALUES (?,?)",
            [(product_id, k) for k in keys],
        )
    return {"imported": len(keys)}


@app.post("/api/card-keys")
def add_key(payload: dict):
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO card_keys(product_id, key_content, remark) VALUES (?,?,?)",
            (payload.get("product_id"), payload.get("key_content"), payload.get("remark", "")),
        )
        return {"id": cur.lastrowid}


@app.put("/api/card-keys/{kid}")
def update_key(kid: int, payload: dict):
    """更新单条卡密：remark / disabled 停用启用 / internal_sku / recycle 循环(含约束与次数)
    循环约束：开启循环要求 卡密已关联内部SKU 且 该内部SKU商品的"重新发布"开关已开启；
    停用卡密开启循环并设次数后自动转为可用。"""
    with db.get_conn() as conn:
        if "remark" in payload:
            conn.execute("UPDATE card_keys SET remark=? WHERE id=?", (payload["remark"], kid))
        if "disabled" in payload:
            want_disabled = int(payload["disabled"])
            if want_disabled:
                # 停用：available/sold → disabled。
                # 已售卡密停用 = 退出循环池（关闭 recycle_enabled，不再被循环复用）；
                # 重新启用后再开循环即可恢复。
                cur = conn.execute("SELECT status FROM card_keys WHERE id=?", (kid,)).fetchone()
                if cur and cur["status"] in ("available", "sold"):
                    conn.execute("UPDATE card_keys SET status='disabled', recycle_enabled=0 "
                                 "WHERE id=?", (kid,))
            else:
                # 启用：disabled → available（普通可用；如需循环再开循环开关）
                conn.execute("UPDATE card_keys SET status='available' WHERE id=? "
                             "AND status='disabled'", (kid,))
        if "internal_sku" in payload:
            conn.execute("UPDATE card_keys SET internal_sku=? WHERE id=?", (payload["internal_sku"], kid))
        if "recycle_limit" in payload:
            conn.execute("UPDATE card_keys SET recycle_limit=? WHERE id=?",
                         (int(payload["recycle_limit"]) or 0, kid))
        if "recycle_enabled" in payload:
            enable = int(payload["recycle_enabled"])
            if enable:
                row = conn.execute("SELECT * FROM card_keys WHERE id=?", (kid,)).fetchone()
                if not row:
                    raise HTTPException(404, "卡密不存在")
                internal = (row["internal_sku"] or "").strip()
                if not internal:
                    raise HTTPException(400, "请先为该卡密关联内部SKU（商品同步栏设置）")
                # 循环使用不要求商品开启"重新发布"——只要卡密关联了内部SKU且设了次数即可
                # （发货无可用卡密时会自动从同内部SKU循环池复用）
                limit = int(row["recycle_limit"] or 0)
                if limit <= 0:
                    raise HTTPException(400, "请先设置循环次数（>0）")
                # 状态语义：开启循环后卡密只处于 可用/停用——停用或已售卡开启循环即转可用
                conn.execute(
                    "UPDATE card_keys SET recycle_enabled=1, "
                    "status=CASE WHEN status IN ('disabled','sold') THEN 'available' ELSE status END "
                    "WHERE id=?", (kid,))
            else:
                conn.execute("UPDATE card_keys SET recycle_enabled=0 WHERE id=?", (kid,))
    return {"ok": True}


@app.post("/api/card-keys/{kid}/cycle")
def cycle_key(kid: int):
    """手动循环一次：已售/停用的可循环卡密 → 重新置为可用，循环次数+1"""
    with db.get_conn() as conn:
        r = conn.execute("SELECT * FROM card_keys WHERE id=?", (kid,)).fetchone()
        if not r:
            raise HTTPException(404, "卡密不存在")
        if not r["recycle_enabled"]:
            raise HTTPException(400, "该卡密未开启循环使用")
        conn.execute(
            "UPDATE card_keys SET status='available', sold_order_no=NULL, sold_at=NULL, "
            "recycle_count=recycle_count+1 WHERE id=? AND recycle_enabled=1",
            (kid,))
    db.log_op("info", "api", f"卡密 {kid} 循环一次")
    return {"ok": True}


@app.delete("/api/card-keys/{kid}")
def delete_key(kid: int):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM card_keys WHERE id=?", (kid,))
    return {"ok": True}


@app.delete("/api/card-keys/group/{product_id}")
def delete_key_group(product_id: int):
    """删除某商品的全部卡密"""
    with db.get_conn() as conn:
        cur = conn.execute("DELETE FROM card_keys WHERE product_id=?", (product_id,))
    return {"ok": True, "deleted": cur.rowcount}


@app.post("/api/card-keys/import-excel")
async def import_keys_excel(file: UploadFile = File(...)):
    """Excel 导入卡密（按【内部SKU】自动关联分组；闲鱼SKU 列不再参与校验/必填）。
    模板列：序号(忽略) | 闲鱼SKU(可留空) | 卡密(必填) | 销售状态(可用/停用，留空默认可用) |
            备注 | 内部SKU(关联依据) | 循环使用(是/否)
    兼容旧 3 列表头：卡密 | 备注 | SKU
    """
    data = await file.read()
    try:
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        ws = wb.active
    except Exception as e:
        raise HTTPException(400, f"Excel 解析失败: {type(e).__name__}: {e}")
    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        raise HTTPException(400, "Excel 为空")
    header = [str(c).strip() if c is not None else "" for c in (all_rows[0] or [])]

    def find_col(*names):
        for i, h in enumerate(header):
            if h in names:
                return i
        return -1

    idx_key = find_col("卡密")
    if idx_key >= 0:
        idx_internal = find_col("内部SKU", "internal_sku")
        idx_status = find_col("销售状态", "状态", "状态(status)")
        idx_remark = find_col("备注", "remark")
        idx_recycle = find_col("循环使用", "循环", "recycle")
    else:
        # 无表头：按新布局位置 序号|闲鱼SKU|卡密|销售状态|备注|内部SKU|循环使用
        idx_no, _idx_sku, idx_key, idx_status, idx_remark, idx_internal, idx_recycle = 0, 1, 2, 3, 4, 5, 6

    def norm_status(v):
        s = str(v or "").strip()
        if not s:
            return "available"
        # "已售/售出"类：已消耗，导入时跳过（防误当可用再发）
        if "已售" in s or "售出" in s or s.startswith("售"):
            return "skip"
        return "disabled" if ("停" in s or "禁" in s) else "available"

    def norm_bool(v):
        s = str(v or "").strip().lower()
        return 1 if s in ("是", "1", "y", "yes", "true", "循环", "可循环") else 0

    rows = []   # (key, status, remark, internal_sku, recycle) —— 只关心内部SKU
    sold_skipped = 0
    for row in all_rows[1:]:
        if not row or len(row) <= idx_key or row[idx_key] in (None, ""):
            continue
        key = str(row[idx_key]).strip()
        if not key:
            continue
        status = norm_status(row[idx_status] if idx_status >= 0 and len(row) > idx_status else "")
        if status == "skip":
            sold_skipped += 1
            continue
        remark = str(row[idx_remark]).strip() if idx_remark >= 0 and len(row) > idx_remark and row[idx_remark] is not None else ""
        internal = str(row[idx_internal]).strip() if idx_internal >= 0 and len(row) > idx_internal and row[idx_internal] not in (None, "") else ""
        recycle = norm_bool(row[idx_recycle] if idx_recycle >= 0 and len(row) > idx_recycle else "")
        rows.append((key, status, remark, internal, recycle))
    if not rows:
        raise HTTPException(400, "Excel 无有效数据（表头：序号 | 闲鱼SKU(可留空) | 卡密 | 销售状态 | 备注 | 内部SKU | 循环使用）")
    by_internal = {}
    no_internal = 0
    for key, status, remark, internal, recycle in rows:
        if internal:
            by_internal.setdefault(internal, []).append((key, status, remark, recycle))
        else:
            no_internal += 1  # 无内部SKU 的行无法归属商品，跳过并提示
    result = {}
    total = 0
    for internal, items in by_internal.items():
        pid = db.find_or_create_product_by_internal_sku(internal)
        if pid is None:
            continue
        with db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO card_keys(product_id, key_content, status, remark, internal_sku, recycle_enabled) "
                "VALUES (?,?,?,?,?,?)",
                [(pid, k, st, r, internal, rc) for k, st, r, rc in items])
        result[internal] = len(items)
        total += len(items)
    if no_internal:
        result["__no_internal_skipped"] = no_internal
    db.log_op("info", "api",
              f"Excel 导入卡密 {total} 张（按内部SKU分组 {len(result)} 组；"
              f"{no_internal} 行缺内部SKU已跳过，{sold_skipped} 行已售已跳过）")
    return {"imported": total, "by_internal": result, "no_internal_skipped": no_internal,
            "sold_skipped": sold_skipped}


@app.get("/api/card-keys/export")
def export_keys():
    """导出卡密表格（格式与导入模板一致：序号|闲鱼SKU|卡密|销售状态|备注|内部SKU|循环使用）。
    导出全部卡密（可用/停用/已售），已售行标记"已售"供人工核对与循环备份；
    重新导入时"已售"行会被跳过（不会误当可用）。文件名携带导出日期时间。"""
    import io
    import openpyxl
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT k.key_content, k.status, k.remark, k.internal_sku, k.recycle_enabled, p.sku "
            "FROM card_keys k LEFT JOIN products p ON p.id=k.product_id "
            "ORDER BY p.id, k.id"
        ).fetchall()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "卡密"
    ws.append(["序号", "SKU", "卡密", "销售状态", "备注", "内部SKU", "循环使用"])
    for i, r in enumerate(rows, 1):
        st = {"available": "可用", "disabled": "停用", "sold": "已售"}.get(r["status"], r["status"])
        rc = "是" if r["recycle_enabled"] else "否"
        ws.append([i, r["sku"] or "", r["key_content"], st, r["remark"] or "",
                   r["internal_sku"] or "", rc])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = "card_keys_export_" + time.strftime("%Y%m%d_%H%M%S") + ".xlsx"
    return Response(content=buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.get("/api/card-keys/template")
def key_template():
    """下载卡密导入模板（xlsx）：序号 | SKU | 卡密 | 销售状态 | 备注 | 内部SKU | 循环使用"""
    import io
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "卡密"
    ws.append(["序号", "SKU", "卡密", "销售状态", "备注", "内部SKU", "循环使用"])
    ws.append([1, "SKU-001", "CARD-EXAMPLE-001", "可用", "示例备注（可留空）", "", "否"])
    ws.append([2, "SKU-001", "CARD-EXAMPLE-002", "停用", "停用示例", "", "否"])
    ws.append([3, "SKU-002", "CARD-EXAMPLE-003", "", "销售状态留空=默认可用", "SKU-002", "是"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=card_keys_template.xlsx"})


@app.get("/api/card-keys/stats")
def key_stats():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT product_id, status, COUNT(*) c FROM card_keys GROUP BY product_id, status"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- 商品 / 订单 / 会话 / 日志 ----------
@app.get("/api/products")
def list_products():
    return db.list_products()


@app.post("/api/products")
def add_product(payload: dict):
    pid = db.add_product(
        title=payload.get("title", "未命名"),
        sku=payload.get("sku", ""),
        price=payload.get("price", 0),
        auto_deliver=int(payload.get("auto_deliver", 1)),
        delay_seconds=int(payload.get("delay_seconds", 0)),
        status=payload.get("status", "selling"),
    )
    return {"id": pid}


@app.put("/api/products/{pid}")
def update_product(pid: int, payload: dict):
    """更新商品（仅更新 payload 中存在的字段，避免部分更新清空其他列）
    内部SKU 变更时自动执行"卡密关联迁移"：把其它同 SKU 商品行的卡密挂到本行，
    使同内部SKU售出后在APP重新上架的新行可直接继续使用该 SKU 的自动发货卡密。"""
    fields = {"title": str, "sku": str, "price": float, "auto_deliver": int,
              "delay_seconds": int, "auto_relist": int, "relist_interval_sec": int,
              "internal_sku": str, "delivery_mode": str}
    sets, args = [], []
    for k, cast in fields.items():
        if k in payload:
            sets.append(f"{k}=?")
            args.append(cast(payload[k]))
    if not sets:
        return {"ok": True}
    args.append(pid)
    with db.get_conn() as conn:
        conn.execute(f"UPDATE products SET {', '.join(sets)} WHERE id=?", args)
    # 内部SKU 变更 → 自动关联同 SKU 卡密并提示（无论是否有关联卡密均写日志）
    if "internal_sku" in payload:
        sku = str(payload["internal_sku"] or "").strip()
        if sku:
            moved = db.relink_card_keys_by_internal_sku(pid, sku)
            if moved:
                db.log_op("info", "api",
                          f"商品{pid} 内部SKU→{sku}：已将其他商品行的 {moved} 张卡密"
                          f"自动关联到本行，自动发货可用（卡密库存栏可见）")
            else:
                db.log_op("info", "api",
                          f"商品{pid} 内部SKU→{sku}：无其他行的卡密可迁移；"
                          f"发货时将按内部SKU「{sku}」跨行兜底取用可用卡密")
    return {"ok": True}


@app.get("/api/products/internal-skus")
def internal_skus():
    """全部商品的内部SKU列表（供卡密内部SKU下拉）"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, internal_sku FROM products ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/products/{pid}/status")
def product_status(pid: int, payload: dict):
    """本地状态标记（供无真实商品的手工分组使用）"""
    status = payload.get("status")
    if status not in ("selling", "sold", "off"):
        raise HTTPException(400, "invalid status")
    db.set_product_status(pid, status)
    db.log_op("info", "api", f"商品状态变更: id={pid} -> {status}")
    return {"ok": True}


@app.post("/api/products/{pid}/auto-relist")
def product_auto_relist(pid: int, payload: dict):
    """重新发布开关 + 发布间隔（秒）（原"自动上架"语义扩展）"""
    with db.get_conn() as conn:
        conn.execute("UPDATE products SET auto_relist=?, relist_interval_sec=? WHERE id=?",
                     (int(payload.get("enabled", 0)), int(payload.get("interval_sec", 0)), pid))
    db.log_op("info", "api", f"重新发布设置: id={pid} enabled={payload.get('enabled')} "
                             f"interval={payload.get('interval_sec')}s")
    return {"ok": True}


@app.post("/api/products/{pid}/save-publish-info")
def product_save_publish(pid: int):
    """拉取并保存该商品的发布信息（供重新发布复用；任务推给属主线程）"""
    def run():
        try:
            res = save_publish_info(browser, pid)
            db.log_op("info", "api", f"保存发布信息结果: {res}")
        except Exception as e:
            db.log_op("error", "api", f"保存发布信息失败: {type(e).__name__} {e}")
    task_queue.push(run)
    return {"accepted": True}


@app.post("/api/products/{pid}/republish")
def product_republish(pid: int):
    """立即网页真实重新发布该商品（用已保存的发布信息在 /publish 自动上架新链接；
    真实写操作，任务推给属主线程执行）。"""
    def run():
        try:
            res = republish_product(browser, pid)
            db.log_op("info", "api", f"重新发布结果: {res}")
        except Exception as e:
            db.log_op("error", "api", f"重新发布异常: {type(e).__name__} {e}")
    task_queue.push(run)
    return {"accepted": True, "note": "重新发布任务已提交（真实上架，请留意日志确认新链接）"}


@app.delete("/api/products/{pid}")
def delete_product(pid: int, payload: dict = None):
    """真实删除商品（mtop 写操作，不可逆；无真实商品的手工分组直接清本地）。
    ⚠️ 安全护栏：必须携带 {"confirm_delete": true}，仅商品同步栏【删除】按钮会发送；
    其它任何页面操作（如卡密库存栏的删除）都不会触发真实商品删除。"""
    if not payload or payload.get("confirm_delete") is not True:
        raise HTTPException(400, "删除商品需显式确认（请通过商品同步栏的【删除】操作）")
    def run():
        try:
            res = do_delete(browser, pid)
            db.log_op("info", "api", f"删除结果: {res}")
        except Exception as e:
            db.log_op("error", "api", f"删除异常: {type(e).__name__} {e}")
    task_queue.push(run)
    return {"accepted": True, "note": "删除任务已提交（真实商品将调用 mtop 删除）"}


@app.post("/api/products/sync")
def products_sync():
    """从闲鱼拉取在售商品（mtop API，网页版无此功能）。任务推给属主线程执行。"""
    def run():
        try:
            res = sync_products(browser)
            db.log_op("info", "api", f"商品同步结果: {res}")
        except Exception as e:
            db.log_op("error", "api", f"商品同步失败: {type(e).__name__} {e}")
    task_queue.push(run)
    return {"accepted": True, "note": "同步任务已提交，请稍后刷新页面查看"}


# ---------- 营收 ----------
@app.get("/api/revenue")
def revenue(start: str = None, end: str = None, product_id: int = None):
    return db.revenue_summary(start, end, product_id)


# ---------- 设置 / 账号 ----------
@app.get("/api/settings/{key}")
def get_setting_api(key: str):
    return {"key": key, "value": db.get_setting(key)}


@app.put("/api/settings/{key}")
def put_setting_api(key: str, payload: dict):
    db.set_setting(key, payload.get("value", ""))
    return {"ok": True}


@app.get("/api/admin/phrases")
def admin_phrases():
    """自定义口令列表 [{phrase, action, target?, note?}]"""
    try:
        return json.loads(db.get_setting("custom_phrases", "[]") or "[]")
    except Exception:
        return []


@app.put("/api/admin/phrases")
def admin_phrases_put(payload: dict):
    """整体保存自定义口令列表（前端提交完整数组）。
    action: pause_agent/resume_agent/pause_deliver/resume_deliver
    target: 商品内部SKU（pause/resume_deliver 必填）"""
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise HTTPException(400, "items 必须是数组")
    cleaned = []
    for it in items:
        phrase = str(it.get("phrase", "")).strip()
        action = str(it.get("action", "")).strip()
        if not phrase or action not in (
                "pause_agent", "resume_agent", "pause_deliver", "resume_deliver"):
            continue
        target = str(it.get("target", "")).strip()
        if action in ("pause_deliver", "resume_deliver") and not target:
            raise HTTPException(400, f"口令「{phrase}」需配置目标商品内部SKU")
        cleaned.append({"phrase": phrase, "action": action,
                        "target": target, "note": str(it.get("note", ""))[:100]})
    db.set_setting("custom_phrases", json.dumps(cleaned, ensure_ascii=False))
    return {"ok": True, "count": len(cleaned)}


@app.get("/api/account")
def account():
    return {
        "name": db.get_setting("account_name", ""),
        "avatar": db.get_setting("account_avatar", ""),
        "userId": db.get_setting("account_unb", ""),
        "logged_in": browser.is_logged_in(),
    }


@app.post("/api/account/logout")
def account_logout():
    """退出当前账号（模拟人工登出）：清 Cookie + 删除本地会话态，可重新扫码切换账号"""
    def run():
        try:
            if browser._ctx is not None:
                try:
                    browser._ctx.clear_cookies()
                except Exception:
                    pass
            state = config.SESSION_STATE_FILE
            if state.exists():
                state.unlink()
            browser.refresh_status()
            db.log_op("info", "api", "账号已登出：Cookie 已清除，会话态已删除")
        except Exception as e:
            db.log_op("error", "api", f"登出异常: {type(e).__name__} {e}")
    task_queue.push(run)
    return {"accepted": True, "note": "登出任务已提交（需数秒生效）"}


@app.get("/api/orders")
def list_orders(status: str = None, limit: int = 100):
    sql = "SELECT * FROM orders WHERE 1=1"
    args = []
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db.get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/conversations")
def list_conversations():
    """会话列表（含各自最新一条消息，供聊天窗口左栏与未读判断）"""
    return db.conversation_list_with_preview(limit=100)


@app.get("/api/messages")
def list_messages(peer: str = "", limit: int = 100, after_id: int = 0):
    """某会话历史消息（升序；after_id 仅取更新的，供轮询增量）"""
    peer = (peer or "").strip()
    if not peer:
        return []
    return db.peer_messages(peer, limit=limit, after_id=after_id)


@app.get("/api/messages/newest")
def newest_messages(after_id: int = 0, limit: int = 100):
    """全局最新消息（收发均含；供聊天窗口/新消息提示音轮询）"""
    return {"max_id": db.latest_message_id(),
            "messages": db.recent_messages_after(after_id=after_id, limit=limit)}


@app.post("/api/messages/send")
def send_message(payload: dict):
    """人工回复：向某会话发送一条消息（模拟人工在 /im 会话输入，任务推属主线程执行）"""
    peer = (payload.get("peer") or "").strip()
    text = (payload.get("text") or "").strip()
    if not peer or not text:
        raise HTTPException(400, "need peer and text")
    if len(text) > 5000:
        raise HTTPException(400, "消息过长（≤5000字）")

    def run():
        try:
            d = Deliverer(browser)
            ok = d.send_chat_message(peer, text)
            if ok:
                # out 历史由 send_chat_message 内部记录；登记 own_sent 防 echo 被当新消息
                from .tasks import note_own_sent
                note_own_sent(text)
                db.log_op("info", "api", f"人工回复已发送 → {peer}: {text[:40]}")
            else:
                db.log_op("warn", "api", f"人工回复发送失败（未找到会话/输入框）: {peer}")
        except Exception as e:
            db.log_op("error", "api", f"人工回复异常: {type(e).__name__} {e}")
    task_queue.push(run)
    return {"accepted": True, "note": "消息已进入发送队列（约数秒内发出，见聊天窗口/日志）"}


@app.post("/api/messages/send-image")
async def send_image(peer: str = Form(...), file: UploadFile = File(...)):
    """发送图片（聊天窗口"图片/截图"入口）：图片暂存后由浏览器线程在 /im 会话发送。
    网页版 /im 若无图片入口将失败并在日志提示。"""
    peer = (peer or "").strip()
    if not peer:
        raise HTTPException(400, "need peer")
    ct = (file.content_type or "").lower()
    if not ct.startswith("image/"):
        raise HTTPException(400, "仅支持图片文件")
    data = await file.read()
    if not data or len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, "图片为空或过大（≤8MB）")
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(ct, "png")
    tmp_dir = config.DATA_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"send_{int(time.time() * 1000)}_{secrets.token_hex(3)}.{ext}"
    tmp.write_bytes(data)

    def run():
        try:
            d = Deliverer(browser)
            ok = d.send_image(peer, str(tmp))
            db.log_op("info", "api",
                      f"图片发送{'成功' if ok else '失败'} → {peer}（{len(data)}B, {ct}）")
        except Exception as e:
            db.log_op("error", "api", f"图片发送异常: {type(e).__name__} {e}")
        finally:
            try:
                tmp.unlink()
            except Exception:
                pass
    task_queue.push(run)
    return {"accepted": True, "note": "图片已进入发送队列（见日志确认）"}


@app.post("/api/selfcheck")
def selfcheck_now():
    """立即触发一次完整自检（任务推属主线程执行，结果写入日志区/设置并可发管理员）"""
    def run():
        try:
            inst = listener_mod.get_active_listener()
            if inst is not None:
                ok, report = inst._do_selfcheck_and_notify()
            else:
                ok, report = selfcheck.run_selfcheck(browser)
            db.log_op("info", "selfcheck", f"网页触发自检完成: {'正常' if ok else '有异常'}")
        except Exception as e:
            db.log_op("error", "selfcheck", f"网页触发自检异常: {type(e).__name__} {e}")
    task_queue.push(run)
    return {"accepted": True, "note": "自检任务已提交，结果见操作日志与下方自检结果区"}


@app.get("/api/logs")
def list_logs(limit: int = 200):
    with db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM op_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/actions/pause")
def pause(payload: dict):
    """暂停/恢复助手自动化（托盘/口令共用）：payload {pause: 1|0}
    暂停后自动回复/自动发货/自动同步/自动重发/定时报告停止；管理员口令仍可恢复。"""
    if "pause" in payload:
        v = "1" if int(payload["pause"]) else "0"
        db.set_setting("agent_paused", v)
        db.log_op("info", "api", f"助手{'已暂停' if v == '1' else '已恢复'}")
        return {"ok": True, "paused": v == "1"}
    db.log_op("info", "api", f"自动化暂停开关: {payload}")
    return {"ok": True}


@app.post("/api/app/exit")
def app_exit():
    """完全关闭助手进程（使用声明"不同意" / 托盘"关闭助手"）。
    不依赖任务队列：直接停监听线程并延迟数秒后 os._exit，确保进程与浏览器全部退出。"""
    db.log_op("warn", "api", "收到退出指令，助手即将完全关闭…")
    try:
        inst = listener_mod.get_active_listener()
        if inst is not None:
            try:
                inst.stop()
            except Exception:
                pass
    except Exception:
        pass
    import threading as _t

    def _bye():
        import time as _time
        _time.sleep(1.8)   # 等监听线程退出并关闭浏览器（Playwright 清理子进程）
        try:
            os._exit(0)
        except Exception:
            pass
    _t.Thread(target=_bye, daemon=True).start()
    return {"accepted": True, "note": "助手正在完全关闭…"}


@app.post("/api/ui/mode")
def ui_mode(payload: dict):
    """管理页前台/静默模式标记（托盘菜单√ 联动）。
    mode: front=管理页在前台打开 / silent=仅后台运行（无管理页）。
    页面加载置 front、页面关闭(pagehide)置 silent；托盘点击对应菜单亦设置。"""
    mode = (payload or {}).get("mode", "")
    if mode not in ("front", "silent"):
        raise HTTPException(400, "mode 需为 front 或 silent")
    db.set_setting("ui_mode", mode)
    return {"ok": True, "mode": mode}


@app.post("/api/actions/deliver")
def manual_deliver(payload: dict):
    """手动触发发货（实弹测试/人工兜底）。任务推给属主线程执行。
    payload: {order_no, product_id, buyer_name?, amount?, peer_id?}
    """
    order_no = payload.get("order_no")
    product_id = payload.get("product_id")
    if not order_no or not product_id:
        raise HTTPException(400, "need order_no and product_id")
    def run():
        try:
            d = Deliverer(browser)
            res = d.deliver(
                order_no, product_id,
                payload.get("buyer_name", ""),
                payload.get("amount", 0),
                payload.get("peer_id"),
            )
            db.log_op("info", "api", f"手动发货结果: {res}", order_no)
        except Exception as e:
            db.log_op("error", "api", f"手动发货异常: {type(e).__name__} {e}", order_no)
    task_queue.push(run)
    return {"accepted": True, "order_no": order_no}


@app.get("/")
def index():
    """管理页（no-cache：避免浏览器缓存旧版页面导致看不到新功能）"""
    resp = FileResponse(str(config.BASE_DIR / "web" / "index.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp
