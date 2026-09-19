"""闲鱼助手 - SQLite 数据层（WAL，单机轻量）"""
import json
import os
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '闲鱼账号',
    status TEXT NOT NULL DEFAULT 'offline',      -- offline/online/expired
    enc_session TEXT,                            -- Fernet 加密的浏览器会话态
    last_active_at TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    sku TEXT,
    price REAL,
    auto_deliver INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'selling',      -- selling/sold/off（在售/已售/下架）
    delay_seconds INTEGER NOT NULL DEFAULT 0,     -- 发货延迟（秒）
    listed_at TEXT,
    sold_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS card_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    key_content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',    -- available/sold/disabled
    remark TEXT NOT NULL DEFAULT '',
    sold_order_no TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    sold_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_order_no TEXT UNIQUE NOT NULL,
    product_id INTEGER,
    buyer_name TEXT,
    amount REAL,
    status TEXT NOT NULL DEFAULT 'paid',         -- paid/delivered/out_of_stock/manual
    delivered_key_id INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id TEXT UNIQUE NOT NULL,
    buyer_name TEXT,
    last_msg_at TEXT,
    manual_flag INTEGER NOT NULL DEFAULT 0,
    last_auto_reply_at TEXT
);

CREATE TABLE IF NOT EXISTS reply_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    match_type TEXT NOT NULL DEFAULT 'contains', -- contains/regex/all
    keywords TEXT NOT NULL,                       -- 逗号分隔；match_type=all 用空
    reply_template TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    cooldown_sec INTEGER NOT NULL DEFAULT 10,
    product_id INTEGER NOT NULL DEFAULT 0        -- 0=通用规则；>0=关联某商品
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS op_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now','localtime')),
    level TEXT NOT NULL DEFAULT 'info',
    module TEXT,
    message TEXT,
    order_no TEXT
);

CREATE INDEX IF NOT EXISTS idx_card_keys_product ON card_keys(product_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS seen_messages (
    peer_id TEXT NOT NULL,
    preview TEXT NOT NULL,
    first_seen_ts TEXT DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (peer_id, preview)
);

-- 聊天历史（供 Web 聊天窗口：收发消息留痕 / 人工回复上下文）
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'in',    -- in=收到买家消息 / out=助手发出
    text TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer_id, id);
"""


def connect(db_path=None):
    conn = sqlite3.connect(str(db_path or config.DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _column_exists(conn, table, col):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return col in cols


def _migrate(conn):
    """旧库自动补列（幂等）"""
    if not _column_exists(conn, "products", "status"):
        conn.execute("ALTER TABLE products ADD COLUMN status TEXT NOT NULL DEFAULT 'selling'")
    if not _column_exists(conn, "products", "item_id"):
        conn.execute("ALTER TABLE products ADD COLUMN item_id TEXT")
    if not _column_exists(conn, "products", "internal_sku"):
        conn.execute("ALTER TABLE products ADD COLUMN internal_sku TEXT DEFAULT ''")
    if not _column_exists(conn, "products", "publish_payload"):
        conn.execute("ALTER TABLE products ADD COLUMN publish_payload TEXT DEFAULT ''")
    if not _column_exists(conn, "products", "auto_relist"):
        conn.execute("ALTER TABLE products ADD COLUMN auto_relist INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "products", "relist_interval_sec"):
        conn.execute("ALTER TABLE products ADD COLUMN relist_interval_sec INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "products", "delay_seconds"):
        conn.execute("ALTER TABLE products ADD COLUMN delay_seconds INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "products", "listed_at"):
        conn.execute("ALTER TABLE products ADD COLUMN listed_at TEXT")
    if not _column_exists(conn, "products", "sold_at"):
        conn.execute("ALTER TABLE products ADD COLUMN sold_at TEXT")
    if not _column_exists(conn, "reply_rules", "product_id"):
        conn.execute("ALTER TABLE reply_rules ADD COLUMN product_id INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "reply_rules", "product_ids"):
        # 一条规则可应用多个商品：JSON 数组（如 [3,5]）；空=通用（兼容旧 product_id>0 单商品）
        conn.execute("ALTER TABLE reply_rules ADD COLUMN product_ids TEXT NOT NULL DEFAULT ''")
    if not _column_exists(conn, "card_keys", "remark"):
        conn.execute("ALTER TABLE card_keys ADD COLUMN remark TEXT NOT NULL DEFAULT ''")
    if not _column_exists(conn, "card_keys", "internal_sku"):
        conn.execute("ALTER TABLE card_keys ADD COLUMN internal_sku TEXT DEFAULT ''")
    if not _column_exists(conn, "card_keys", "recycle_enabled"):
        conn.execute("ALTER TABLE card_keys ADD COLUMN recycle_enabled INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "card_keys", "recycle_count"):
        conn.execute("ALTER TABLE card_keys ADD COLUMN recycle_count INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "card_keys", "recycle_limit"):
        conn.execute("ALTER TABLE card_keys ADD COLUMN recycle_limit INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "products", "delivery_mode"):
        # 重新发布时发货方式：''=默认包邮 / no_mail=无需邮寄 / post=按距离 / buyout=一口价
        conn.execute("ALTER TABLE products ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_product ON reply_rules(product_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_keys_product ON card_keys(product_id)")
    # 数据修复：有 item_id 但 sku 为空的行，补 sku=item_id（闲鱼SKU 即商品ID）
    conn.execute("UPDATE products SET sku=item_id "
                 "WHERE item_id IS NOT NULL AND item_id != '' AND (sku IS NULL OR sku='')")
    # 状态语义迁移：开启循环的卡密不应出现"已售"（循环卡=可用/停用），历史 sold+循环 → 转回可用
    conn.execute("UPDATE card_keys SET status='available' "
                 "WHERE recycle_enabled=1 AND status='sold'")


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    # 默认规则（可在 Web 页改）
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM reply_rules").fetchone()["c"]
        if n == 0:
            conn.execute(
                "INSERT INTO reply_rules(name, match_type, keywords, reply_template) VALUES (?,?,?,?)",
                ("问候", "contains", "在吗,在不在,你好,hello,hi", "亲，在的哦~ 请问有什么可以帮您？"),
            )
            conn.execute(
                "INSERT INTO reply_rules(name, match_type, keywords, reply_template) VALUES (?,?,?,?)",
                ("库存询问", "contains", "还有吗,有货吗,现货,库存", "亲，现货充足，拍下后自动发货哦~"),
            )
            conn.execute(
                "INSERT INTO reply_rules(name, match_type, keywords, reply_template) VALUES (?,?,?,?)",
                ("催发货", "contains", "多久发货,什么时候发,还没发货", "亲，虚拟商品拍下支付后自动发货，请留意聊天消息~"),
            )
    # 默认设置
    with get_conn() as conn:
        for k, v in (("account_name", ""), ("account_avatar", ""),
                     ("auto_sync_interval_sec", "600"),
                     # 管理员账号
                     ("admin_peer", ""),                 # 已绑定的管理员（闲鱼昵称/会话）
                     ("bind_phrase", ""),                # 绑定口令（任意账号发送即绑定为管理员）
                     ("admin_status_phrase", ""),        # 口令：索取运行状态
                     ("admin_sales_phrase", ""),         # 口令：索取运营汇报
                     ("status_report_time", ""),         # 定时状态检查时间 HH:MM
                     ("sales_report_time", ""),          # 定时运营汇报时间 HH:MM
                     ("last_status_report", ""), ("last_sales_report", ""),
                     # 自检任务
                     ("selfcheck_enabled", "0"),         # 定时自检总开关
                     ("selfcheck_interval_min", "60"),   # 自检间隔（分钟）
                     ("last_selfcheck_ts", ""),          # 上次自检时间
                     ("last_selfcheck_ok", ""),          # 上次自检是否全过 1/0
                     ("last_selfcheck_result", ""),      # 上次自检报告全文
                     # 账号/界面
                     ("account_unb", ""),                # 账号 unb（userId）
                     ("ui_mode", "silent"),              # 管理页前台/静默标记
                     ("sound_sys_enabled", "1"),         # 系统提示音开关（持久化，托盘/页面共用）
                     ("sound_msg_enabled", "1")):        # 消息提示音开关
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (k, v))


# ---------- 业务操作 ----------

def log_op(level, module, message, order_no=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO op_logs(level, module, message, order_no) VALUES (?,?,?,?)",
            (level, module, message, order_no),
        )


def record_seen(peer_id, preview, window_sec=120):
    """记录已处理消息；返回 True 表示应处理，False 表示刚处理过（窗口内重复）。
    窗口语义：同(peer,预览)在 window_sec 秒内视为重复（防 DOM 多轮/双通道重复）；
    超过窗口再次出现视为用户重发同文本（如再次发送测试口令/关键词），允许处理。
    预览取前 40 字符归一化为键（WS 全文与 DOM 列表截断预览共用同键，防双触发）。"""
    peer_id = (peer_id or "")[:120]
    key = ((preview or "").strip())[:40].rstrip("…... \t")
    if not key:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT first_seen_ts FROM seen_messages WHERE peer_id=? AND preview=?",
            (peer_id, key),
        ).fetchone()
        if row is None:
            conn.execute("INSERT INTO seen_messages(peer_id, preview) VALUES (?,?)",
                         (peer_id, key))
            return True
        # 已存在：窗口内视为重复；超窗口则更新记录并放行
        fresh = conn.execute(
            "SELECT (julianday('now','localtime') - julianday(?)) * 86400 < ? AS fresh",
            (row["first_seen_ts"], window_sec),
        ).fetchone()["fresh"]
        if fresh:
            return False
        conn.execute("UPDATE seen_messages SET first_seen_ts=datetime('now','localtime') "
                     "WHERE peer_id=? AND preview=?", (peer_id, key))
        return True


def take_card_key(product_id, order_no):
    """发货取卡（本商品）。返回 (key_id, key_content, kind, recycle_count) 或 None。
    状态语义（与需求一致）：
      - 普通卡（recycle_enabled=0）：available→sold（一次性消耗，已售即排除出发货列表）；
      - 循环卡（recycle_enabled=1）：状态始终 available（可再发=可用），每次发货 recycle_count+1，
        达到 recycle_limit 自动停用（disabled）。循环卡不会出现 sold 状态。"""
    with get_conn() as conn:
        # 1) 普通卡可用 → 一次性消耗（sold）
        row = conn.execute(
            "SELECT id, key_content FROM card_keys "
            "WHERE product_id=? AND status='available' AND recycle_enabled=0 ORDER BY id LIMIT 1",
            (product_id,)).fetchone()
        if row:
            cur = conn.execute(
                "UPDATE card_keys SET status='sold', sold_order_no=?, sold_at=datetime('now','localtime') "
                "WHERE id=? AND status='available'", (order_no, row["id"]))
            if cur.rowcount:
                return row["id"], row["key_content"], "normal", 0
        # 2) 循环卡可用 → 保持可用，次数+1；达上限自动停用
        row = conn.execute(
            "SELECT id, key_content, recycle_count, recycle_limit FROM card_keys "
            "WHERE product_id=? AND status='available' AND recycle_enabled=1 ORDER BY id LIMIT 1",
            (product_id,)).fetchone()
        if row:
            new_count = row["recycle_count"] + 1
            if row["recycle_limit"] and new_count >= row["recycle_limit"]:
                cur = conn.execute(
                    "UPDATE card_keys SET recycle_count=?, status='disabled', recycle_enabled=0, "
                    "sold_order_no=?, sold_at=datetime('now','localtime') "
                    "WHERE id=? AND status='available'",
                    (new_count, order_no, row["id"]))
            else:
                cur = conn.execute(
                    "UPDATE card_keys SET recycle_count=?, sold_order_no=?, "
                    "sold_at=datetime('now','localtime') WHERE id=? AND status='available'",
                    (new_count, order_no, row["id"]))
            if cur.rowcount:
                return row["id"], row["key_content"], "cycle", new_count
    return None


def mark_order_delivered(order_no, key_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status='delivered', delivered_key_id=?, delivered_at=datetime('now','localtime') "
            "WHERE platform_order_no=?",
            (key_id, order_no),
        )


def order_exists(order_no):
    with get_conn() as conn:
        r = conn.execute("SELECT 1 FROM orders WHERE platform_order_no=?", (order_no,)).fetchone()
        return r is not None


def upsert_order(order_no, product_id, buyer_name, amount):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO orders(platform_order_no, product_id, buyer_name, amount) VALUES (?,?,?,?)",
            (order_no, product_id, buyer_name, amount),
        )


def upsert_conversation(peer_id, buyer_name=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO conversations(peer_id, buyer_name, last_msg_at) VALUES (?,?,datetime('now','localtime')) "
            "ON CONFLICT(peer_id) DO UPDATE SET buyer_name=COALESCE(?, buyer_name), "
            "last_msg_at=datetime('now','localtime')",
            (peer_id, buyer_name, buyer_name),
        )


# ---------- 设置 ----------
def get_setting(key, default=""):
    with get_conn() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (key, str(value)))


# ---------- 商品 ----------
def add_product(title, sku="", price=0, auto_deliver=1, delay_seconds=0, status="selling"):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO products(title, sku, price, auto_deliver, delay_seconds, status, listed_at) "
            "VALUES (?,?,?,?,?,?, datetime('now','localtime'))",
            (title, sku, price, auto_deliver, delay_seconds, status),
        )
        return cur.lastrowid


def find_or_create_product_by_sku(sku):
    """按 SKU 查找商品；不存在则自动创建（用于 Excel 按 SKU 导入卡密）"""
    sku = (sku or "").strip()
    if not sku:
        return None
    with get_conn() as conn:
        r = conn.execute("SELECT id FROM products WHERE sku=?", (sku,)).fetchone()
        if r:
            return r["id"]
        cur = conn.execute(
            "INSERT INTO products(title, sku, price, auto_deliver, status) VALUES (?,?,0,0,'selling')",
            (sku, sku))
        return cur.lastrowid


def set_product_status(product_id, status):
    """status: selling/sold/off；标记已售时记录 sold_at，重新上架时更新 listed_at"""
    with get_conn() as conn:
        if status == "sold":
            conn.execute("UPDATE products SET status=?, sold_at=datetime('now','localtime') WHERE id=?",
                         (status, product_id))
        elif status == "selling":
            conn.execute("UPDATE products SET status=?, listed_at=datetime('now','localtime') WHERE id=?",
                         (status, product_id))
        else:
            conn.execute("UPDATE products SET status=? WHERE id=?", (status, product_id))


def sync_upsert_items(items):
    """mtop 同步：按 item_id 更新/新建商品（自动同步的商品 auto_deliver 默认关，用户自行开启）"""
    n_new = 0
    n_upd = 0
    with get_conn() as conn:
        for it in items:
            item_id = str(it.get("id") or "")
            title = it.get("title") or ""
            if not item_id or not title:
                continue
            price = 0.0
            try:
                price = float(it.get("price") or 0)
            except Exception:
                pass
            r = conn.execute("SELECT id, status FROM products WHERE item_id=?", (item_id,)).fetchone()
            if r:
                if title:
                    conn.execute(
                        "UPDATE products SET title=?, price=?, status='selling', "
                        "listed_at=COALESCE(listed_at, datetime('now','localtime')) WHERE item_id=?",
                        (title, price, item_id))
                else:
                    # 拉取到空标题时不覆盖已有标题（防止误清空）
                    conn.execute(
                        "UPDATE products SET price=?, status='selling', "
                        "listed_at=COALESCE(listed_at, datetime('now','localtime')) WHERE item_id=?",
                        (price, item_id))
                n_upd += 1
            else:
                conn.execute(
                    "INSERT INTO products(title, sku, price, auto_deliver, status, item_id, listed_at) "
                    "VALUES (?,?,?,0,'selling',?, datetime('now','localtime'))",
                    (title, item_id, price, item_id))
                n_new += 1
    return {"new": n_new, "updated": n_upd}


def mark_missing_sold(active_item_ids):
    """本次同步未出现在"在售"列表里的在售商品 → 标记已售。
    注意：开启了"重新发布"(auto_relist=1) 的商品**不在此自动标记**——
    它们只有在真实售出（订单通知→自动发货成功，deliverer 标记）后才进入
    重新发布计时；从在售列表消失可能是手动下架，不应触发自动重发。"""
    active = {str(x) for x in active_item_ids}
    n = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, item_id FROM products "
            "WHERE status='selling' AND item_id IS NOT NULL AND item_id != '' AND auto_relist=0"
        ).fetchall()
        for r in rows:
            if str(r["item_id"]) not in active:
                conn.execute(
                    "UPDATE products SET status='sold', sold_at=datetime('now','localtime') WHERE id=?",
                    (r["id"],))
                n += 1
    return n


def missing_selling_items(active_item_ids):
    """返回"本地标记在售、但本次同步不在在售列表"的商品（含 auto_relist=1）。
    用于调用方逐个核验真实状态（已删除/手动下架/已售出），避免僵尸在售。"""
    active = {str(x) for x in active_item_ids}
    out = []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, item_id, title FROM products "
            "WHERE status='selling' AND item_id IS NOT NULL AND item_id != ''"
        ).fetchall()
        for r in rows:
            if str(r["item_id"]) not in active:
                out.append(dict(r))
    return out


def list_products():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT p.*,
              (SELECT COUNT(*) FROM card_keys k WHERE k.product_id=p.id AND k.status='available') AS avail_keys,
              (SELECT COUNT(*) FROM card_keys k WHERE k.product_id=p.id) AS total_keys
            FROM products p ORDER BY p.id
        """).fetchall()
    return [dict(r) for r in rows]


def auto_relist_due_products():
    """自动重新发布到期的商品（auto_relist=1、有已保存发布信息、间隔到点）"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM products
            WHERE auto_relist=1 AND item_id IS NOT NULL AND item_id != ''
              AND title != '' AND title IS NOT NULL
              AND publish_payload != '' AND publish_payload IS NOT NULL
              AND status='sold' AND sold_at IS NOT NULL
              AND relist_interval_sec > 0
              AND (julianday('now','localtime') - julianday(sold_at)) * 86400 >= relist_interval_sec
            ORDER BY sold_at LIMIT 20
        """).fetchall()
    return [dict(r) for r in rows]


# ---------- 聊天消息（Web 聊天窗口历史/人工回复） ----------
def _norm_text(s):
    import re as _re
    return _re.sub(r"\s+", "", s or "")


def add_message(peer_id, direction, text):
    """记录一条聊天消息（in=收到 / out=发出）。失败静默，不影响主流程。
    去重：同会话同方向、归一化文本相同且 30 分钟内再次出现时，不新增行，
    仅刷新该消息时间（保证"相同消息不得连续出现"）。
    跨方向：若刚向该会话发出过同文本（自己消息被系统/对方回显成 in），
    该 in 直接丢弃不入库（避免"收发混乱/自己消息变成买家消息"）。"""
    try:
        text = (text or "").strip()
        if not text:
            return None
        peer = str(peer_id or "")[:120]
        d = "out" if direction == "out" else "in"
        n = _norm_text(text)[:200]
        with get_conn() as conn:
            # 近 30 分钟同会话同方向，归一化前缀相同的消息 → 视为重复（合并刷新）
            row = conn.execute(
                "SELECT id, text, direction FROM messages WHERE peer_id=? AND "
                "created_at >= datetime('now','localtime','-1800 seconds') "
                "ORDER BY id DESC LIMIT 12", (peer,)).fetchall()
            for r in row:
                m = _norm_text(r["text"])
                if not m:
                    continue
                same = (n == m or (len(n) >= 14 and n[:14] in m[:100]) or (len(m) >= 14 and m[:14] in n[:100]))
                if not same:
                    continue
                if r["direction"] == d:
                    # 同方向重复：合并刷新时间，不新增行
                    conn.execute("UPDATE messages SET created_at=datetime('now','localtime') WHERE id=?",
                                 (r["id"],))
                    return r["id"]
                elif d == "in":
                    # 我方刚发出的消息被回显为 in（系统回执/对方复读）→ 丢弃，不写买家消息
                    return r["id"]
                # 其它跨方向组合（out 紧随 in 同文本）：正常写入
                break
            cur = conn.execute(
                "INSERT INTO messages(peer_id, direction, text) VALUES (?,?,?)", (peer, d, text))
            return cur.lastrowid
    except Exception:
        return None


def is_recent_out(peer_id, text, window_sec=600):
    """近期（默认10分钟）是否向该会话发出过同文本消息（自己发出的消息回显判断）。
    用于把"卖家（本账号/其它设备）刚发出的消息"从 DOM 兜底中识别出来，避免误当买家消息。"""
    n = _norm_text(text)
    if not n:
        return False
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT text FROM messages WHERE peer_id=? AND direction='out' AND "
                "created_at >= datetime('now','localtime',?)",
                (str(peer_id or "")[:120], f"-{int(window_sec)} seconds")).fetchall()
    except Exception:
        return False
    for r in rows:
        m = _norm_text(r["text"])
        if not m:
            continue
        if m == n:
            return True
        if len(n) >= 14 and n[:14] in m[:100]:
            return True
        if len(m) >= 14 and m[:14] in n[:100]:
            return True
    return False


def recent_in_dup(peer_id, text, window_sec=300):
    """近 window_sec 秒内是否已入库过同会话的同文本 in 消息（归一化去空白，前缀容差）。
    用于拦截 DOM 兜底轮询对同一条买家消息的反复重放（record_seen 窗口过期后每 2 分钟重放），
    避免聊天窗口同一消息反复出现、消息音/自动回复重复触发。"""
    import re as _re

    def _norm(s):
        return _re.sub(r"\s+", "", s or "")

    n = _norm(text)
    if not n:
        return False
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT text FROM messages WHERE peer_id=? AND direction='in' AND "
                "created_at >= datetime('now','localtime',?)",
                (str(peer_id or "")[:120], f"-{int(window_sec)} seconds")).fetchall()
    except Exception:
        return False
    for r in rows:
        m = _norm(r["text"])
        if not m:
            continue
        if m == n:
            return True
        if len(n) >= 16 and n[:16] in m[:90]:
            return True
        if len(m) >= 16 and m[:16] in n[:90]:
            return True
    return False


def peer_messages(peer_id, limit=100, after_id=0):
    """某会话的消息历史（升序，截取最近 limit 条）。"""
    peer_id = (peer_id or "")[:120]
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE peer_id=? AND id>? "
            "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (peer_id, int(after_id), int(limit))).fetchall()
    return [dict(r) for r in rows]


def recent_messages_after(after_id=0, limit=200):
    """全局最新消息（供会话列表未读/新消息提示音轮询）"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE id>? ORDER BY id DESC LIMIT ?) "
            "ORDER BY id ASC", (int(after_id), int(limit))).fetchall()
    return [dict(r) for r in rows]


def latest_message_id():
    with get_conn() as conn:
        r = conn.execute("SELECT MAX(id) m FROM messages").fetchone()
        return r["m"] or 0


def conversation_list_with_preview(limit=100):
    """会话列表 + 各自最新一条消息（供聊天窗口左栏）"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT c.*, m.id AS last_msg_id, m.text AS last_text,
                   m.direction AS last_dir, m.created_at AS last_ts
            FROM conversations c
            LEFT JOIN messages m ON m.id = (
                SELECT MAX(id) FROM messages WHERE peer_id = c.peer_id)
            ORDER BY COALESCE(c.last_msg_at, '1970-01-01') DESC LIMIT ?
        """, (int(limit),)).fetchall()
    return [dict(r) for r in rows]


# ---------- 回复规则：多商品(product_ids JSON) ----------
def rule_product_ids(rule: dict) -> list:
    """规则适用的商品 id 列表；空列表 = 通用规则。
    兼容旧版 product_id>0 单商品字段（迁移期）。"""
    raw = rule.get("product_ids") or ""
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                return [int(x) for x in v if str(x).isdigit()]
        except Exception:
            pass
    pid = rule.get("product_id") or 0
    return [int(pid)] if int(pid) > 0 else []


def rule_matches_product(rule: dict, product_id) -> bool:
    ids = rule_product_ids(rule)
    return (not ids) or (int(product_id or 0) in ids)


def rules_for_product(product_id):
    """返回适用于某商品的启用规则（通用 + 含该商品的多选规则）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reply_rules WHERE enabled=1 ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if rule_matches_product(d, product_id):
            out.append(d)
    return out


def set_rule_product_ids(rid, product_ids: list):
    """保存规则适用商品列表（空=通用）。product_ids 兼容旧字段同步。"""
    clean = [int(x) for x in (product_ids or []) if str(x).isdigit()]
    with get_conn() as conn:
        conn.execute(
            "UPDATE reply_rules SET product_ids=?, product_id=? WHERE id=?",
            (json.dumps(clean, ensure_ascii=False), (clean[0] if len(clean) == 1 else 0), rid))


def delete_rules_for_product(pid):
    """删除适用于某商品的全部规则（pid=0 指通用规则组）。"""
    pid = int(pid)
    with get_conn() as conn:
        if pid == 0:
            cur = conn.execute("DELETE FROM reply_rules WHERE product_ids IN ('', '[]') "
                               "OR (product_ids='' AND product_id=0)")
        else:
            # 含该 pid 的规则（新格式 product_ids JSON 精确数字匹配；旧格式 product_id 单商品兼容）
            cur = conn.execute(
                "DELETE FROM reply_rules WHERE "
                "(product_ids != '' AND product_ids LIKE ?) OR "
                "(product_ids='' AND product_id=?)",
                (f"%\"{pid}\"%", int(pid)))
        return cur.rowcount


# ---------- 内部SKU：卡密兜底取用 / 关联迁移 ----------
def take_card_key_by_sku(internal_sku, order_no):
    """按内部SKU跨商品行兜底取卡（同SKU换行重上架后仍可发货），状态语义同 take_card_key：
    先普通卡(可用→已售)，再循环卡(保持可用、次数+1、达上限停用)。"""
    sku = (internal_sku or "").strip()
    if not sku:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT k.id, k.key_content FROM card_keys k "
            "JOIN products p ON p.id = k.product_id "
            "WHERE p.internal_sku=? AND k.status='available' AND k.recycle_enabled=0 "
            "ORDER BY k.id LIMIT 1", (sku,)).fetchone()
        if row:
            cur = conn.execute(
                "UPDATE card_keys SET status='sold', sold_order_no=?, sold_at=datetime('now','localtime') "
                "WHERE id=? AND status='available'", (order_no, row["id"]))
            if cur.rowcount:
                return row["id"], row["key_content"], "normal", 0
        row = conn.execute(
            "SELECT k.id, k.key_content, k.recycle_count, k.recycle_limit FROM card_keys k "
            "JOIN products p ON p.id = k.product_id "
            "WHERE p.internal_sku=? AND k.status='available' AND k.recycle_enabled=1 "
            "ORDER BY k.id LIMIT 1", (sku,)).fetchone()
        if row:
            new_count = row["recycle_count"] + 1
            if row["recycle_limit"] and new_count >= row["recycle_limit"]:
                cur = conn.execute(
                    "UPDATE card_keys SET recycle_count=?, status='disabled', recycle_enabled=0, "
                    "sold_order_no=?, sold_at=datetime('now','localtime') "
                    "WHERE id=? AND status='available'",
                    (new_count, order_no, row["id"]))
            else:
                cur = conn.execute(
                    "UPDATE card_keys SET recycle_count=?, sold_order_no=?, "
                    "sold_at=datetime('now','localtime') WHERE id=? AND status='available'",
                    (new_count, order_no, row["id"]))
            if cur.rowcount:
                return row["id"], row["key_content"], "cycle", new_count
    return None


def relink_card_keys_by_internal_sku(pid, internal_sku):
    """把其它持有同一内部SKU的商品行的卡密迁移到本行（pid），并清空它们的内部SKU。
    返回移动的卡密数（用于日志提示是否关联成功）。"""
    sku = (internal_sku or "").strip()
    if not sku:
        return 0
    moved = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM products WHERE internal_sku=? AND id!=?",
            (sku, int(pid))).fetchall()
        for r in rows:
            cur = conn.execute("UPDATE card_keys SET product_id=? WHERE product_id=?",
                               (int(pid), r["id"]))
            moved += cur.rowcount
            # 清空旧行内部SKU：同SKU只保留一个活跃归属行，避免重复配置歧义
            conn.execute("UPDATE products SET internal_sku='' WHERE id=? AND id!=?",
                         (r["id"], int(pid)))
    return moved


def find_or_create_product_by_internal_sku(sku):
    """按内部SKU查找商品；不存在则自动创建（Excel 按内部SKU导入卡密用）。
    创建的行 auto_deliver=0（用户自行开启自动发货）。"""
    sku = (sku or "").strip()
    if not sku:
        return None
    with get_conn() as conn:
        r = conn.execute("SELECT id FROM products WHERE internal_sku=?", (sku,)).fetchone()
        if r:
            return r["id"]
        cur = conn.execute(
            "INSERT INTO products(title, internal_sku, price, auto_deliver, status) "
            "VALUES (?,?,0,0,'selling')", (sku, sku))
        return cur.lastrowid


def count_available_for_product(pid, with_recycle=False):
    """商品可用卡密数（含可选：循环池内可复用卡密）。"""
    with get_conn() as conn:
        if not with_recycle:
            n = conn.execute(
                "SELECT COUNT(*) c FROM card_keys WHERE product_id=? AND status='available'",
                (int(pid),)).fetchone()["c"]
            return n
        return conn.execute(
            "SELECT COUNT(*) c FROM card_keys WHERE product_id=? AND "
            "(status='available' OR (recycle_enabled=1 AND "
            "(recycle_limit=0 OR recycle_count<recycle_limit)))",
            (int(pid),)).fetchone()["c"]


def peek_deliver_key(pid, internal_sku=""):
    """模拟取卡（自检用，只读不扣）：按真实发货顺序预演将扣哪张卡。
    返回 (来源描述, key_id 或 None)。顺序：本商品普通可用→本商品循环可用→同SKU跨行普通→同SKU跨行循环。"""
    pid = int(pid)
    sku = (internal_sku or "").strip()
    with get_conn() as conn:
        def _find(base_sql, args):
            return conn.execute(base_sql + " ORDER BY k.id LIMIT 1", args).fetchone()

        r = _find("SELECT k.id FROM card_keys k WHERE k.product_id=? AND k.status='available' "
                  "AND k.recycle_enabled=0", (pid,))
        if r:
            return "本商品普通卡", r["id"]
        r = _find("SELECT k.id FROM card_keys k WHERE k.product_id=? AND k.status='available' "
                  "AND k.recycle_enabled=1", (pid,))
        if r:
            return "本商品循环卡", r["id"]
        if sku:
            r = _find("SELECT k.id FROM card_keys k JOIN products p2 ON p2.id=k.product_id "
                      "WHERE p2.internal_sku=? AND k.status='available' AND k.recycle_enabled=0", (sku,))
            if r:
                return f"内部SKU[{sku}]跨行普通卡", r["id"]
            r = _find("SELECT k.id FROM card_keys k JOIN products p2 ON p2.id=k.product_id "
                      "WHERE p2.internal_sku=? AND k.status='available' AND k.recycle_enabled=1", (sku,))
            if r:
                return f"内部SKU[{sku}]跨行循环卡", r["id"]
        return "无可用卡密", None


def has_stock_for_product(pid):
    """发货就绪预检（自检用，不扣卡）：本商品有可用卡密，
    或本商品内部SKU 可在全库（跨行）取到 可用/可循环 卡密。"""
    pid = int(pid)
    with get_conn() as conn:
        p = conn.execute("SELECT internal_sku FROM products WHERE id=?", (pid,)).fetchone()
        sku = (p["internal_sku"] or "").strip() if p else ""
        # 本商品可用或循环可用
        n = conn.execute(
            "SELECT COUNT(*) c FROM card_keys WHERE product_id=? AND "
            "(status='available' OR (recycle_enabled=1 AND "
            "(recycle_limit=0 OR recycle_count<recycle_limit)))",
            (pid,)).fetchone()["c"]
        if n:
            return True, "本商品卡密可用"
        if sku:
            m = conn.execute(
                "SELECT COUNT(*) c FROM card_keys k JOIN products p2 ON p2.id=k.product_id "
                "WHERE p2.internal_sku=? AND (k.status='available' OR "
                "(k.recycle_enabled=1 AND (k.recycle_limit=0 OR k.recycle_count<k.recycle_limit)))",
                (sku,)).fetchone()["c"]
            if m:
                return True, f"内部SKU「{sku}」跨行卡密可用"
        return False, (f"无可用/可循环卡密（内部SKU{'「' + sku + '」' if sku else '为空'}）")


# ---------- 营收统计 ----------
def revenue_summary(start=None, end=None, product_id=None):
    """按时间范围/商品聚合订单营收（paid+delivered 计入）
    日期比较用 date() 函数（避免 delivered_at 含时分秒导致"结束日期当天"被排除）"""
    def _date_cond(prefix, args_list):
        cond = ""
        if start:
            cond += f" AND date({prefix}) >= ?"
            args_list.append(start)
        if end:
            cond += f" AND date({prefix}) <= ?"
            args_list.append(end)
        return cond

    sql = "SELECT product_id, COUNT(*) cnt, SUM(amount) total FROM orders WHERE status IN ('paid','delivered')"
    args = []
    sql += _date_cond("delivered_at", args)
    if product_id:
        sql += " AND product_id=?"
        args.append(product_id)
    sql += " GROUP BY product_id"
    sql2 = ("SELECT date(delivered_at) d, COUNT(*) cnt, SUM(amount) total FROM orders "
            "WHERE status IN ('paid','delivered') AND delivered_at IS NOT NULL")
    args2 = []
    sql2 += _date_cond("delivered_at", args2)
    if product_id:
        sql2 += " AND product_id=?"
        args2.append(product_id)
    sql2 += " GROUP BY d ORDER BY d"
    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
        days = conn.execute(sql2, args2).fetchall()
        by_product = []
        for r in rows:
            pid = r["product_id"]
            title = "未关联商品"
            if pid:
                p = conn.execute("SELECT title, price FROM products WHERE id=?", (pid,)).fetchone()
                title = p["title"] if p else f"已删除商品#{pid}"
            by_product.append({
                "product_id": pid,
                "title": title,
                "count": r["cnt"],
                "amount": round(r["total"] or 0, 2),
            })
    total = sum((r["total"] or 0) for r in rows)
    cnt = sum(r["cnt"] for r in rows)
    by_day = [{"date": r["d"], "count": r["cnt"], "amount": round(r["total"] or 0, 2)} for r in days]
    return {"total_amount": round(total, 2), "total_orders": cnt,
            "by_product": by_product, "by_day": by_day}
