"""闲鱼助手 - mtop API（仅用于网页版无法获取/操作的数据）

只读：卖家在售商品列表 mtop.idle.web.xyh.item.list
写入（均为用户手动触发，自带限流与令牌刷新重试）：
  - 下架  mtop.alibaba.idle.seller.pc.item.batch.offline
  - 上架  mtop.alibaba.idle.seller.pc.item.batch.online（与下架对称，未见于开源实现，失败仅报错无害）
  - 删除  mtop.alibaba.idle.seller.pc.item.delete

签名：md5(token & t & appKey & data)，token 取自 Cookie _m_h5_tk（下划线前部分）
"""
import hashlib
import json
import time
import urllib.parse
import urllib.request

APP_KEY = "34839810"
MTOP_HOST = "https://h5api.m.goofish.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 写操作最小间隔（秒）：风控护栏
WRITE_MIN_INTERVAL = 3.0
_last_write_ts = 0.0


def generate_sign(t: str, token: str, data: str) -> str:
    msg = f"{token}&{t}&{APP_KEY}&{data}"
    return hashlib.md5(msg.encode("utf-8")).hexdigest()


def build_cookie_header(cookies: list) -> str:
    pairs = [f"{c['name']}={c['value']}" for c in cookies
             if c["name"] not in ("_m_h5_tk_enc",) and c.get("value")]
    return "; ".join(pairs)


def _token_from_cookie_str(cookie_str: str) -> str:
    for part in cookie_str.split(";"):
        if part.strip().startswith("_m_h5_tk="):
            return part.split("=", 1)[1].split("_")[0]
    return ""


def _apply_set_cookie(cookie_str: str, headers) -> str:
    try:
        for raw in headers.get_all("Set-Cookie") or []:
            if "=" in raw:
                name, rest = raw.split("=", 1)
                value = rest.split(";")[0]
                parts = [p for p in cookie_str.split(";") if not p.strip().startswith(name + "=")]
                parts.append(f"{name}={value}")
                cookie_str = "; ".join(parts)
    except Exception:
        pass
    return cookie_str


def _mtop_post(api: str, version: str, data: dict, cookie_str: str, max_tries=2,
               extra_headers=None, extra_params=None):
    """通用 mtop 调用（POST，含令牌刷新重试）。返回响应 JSON。"""
    data_val = json.dumps(data, separators=(",", ":"))
    for attempt in range(max_tries):
        t = str(int(time.time() * 1000))
        token = _token_from_cookie_str(cookie_str)
        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": t,
            "sign": generate_sign(t, token, data_val),
            "v": version,
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": api,
            "sessionOption": "AutoLoginOnly",
        }
        if extra_params:
            params.update(extra_params)
        headers = {
            "User-Agent": UA,
            "Referer": "https://www.goofish.com/",
            "Origin": "https://www.goofish.com",
            "Cookie": cookie_str,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        url = f"{MTOP_HOST}/h5/{api}/{version}/" + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            data=("data=" + urllib.parse.quote(data_val)).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            res_json = json.loads(resp.read().decode("utf-8", "replace"))
            cookie_str = _apply_set_cookie(cookie_str, resp.headers)
        ret = res_json.get("ret") or []
        if ret and "SUCCESS" in ret[0]:
            return res_json
        if any("TOKEN" in r for r in ret) and attempt < max_tries - 1:
            continue
        raise RuntimeError(f"mtop {api} 失败: {ret} | {str(res_json)[:180]}")
    raise RuntimeError(f"mtop {api} 失败（重试后）")


# ---------- 只读：在售商品列表 / 商品详情 ----------
def check_session(cookie_str: str):
    """轻量探测会话是否有效（服务端真实验证，非仅本地 cookie）。
    判据：mtop.taobao.idlemessage.pc.loginuser.get（与页面/WS 会话同域）。
    页面级实证：cookie2 过期时 loginuser.get 返回 SESSION_EXPIRED，而 item.list
    网关校验宽松（旧 cookie 仍能拉取，造成"假在线"）；WS 心跳也可能为旧连接残留。
    故以 loginuser.get 为准。max_tries=2：首次遇令牌刷新(FAIL_SYS_TOKEN_EMPTY)重试，
    避免误报。返回 True=有效；False=已过期/未登录；None=探测失败（网络/风控，不确定）。"""
    try:
        _mtop_post("mtop.taobao.idlemessage.pc.loginuser.get", "1.0",
                   {}, cookie_str, max_tries=2)
        return True
    except RuntimeError as e:
        msg = str(e)
        if any(k in msg for k in ("SESSION_EXPIRED", "会话过期", "未登录", "NOT_LOGIN",
                                  "TOKEN_EMPTY", "令牌为空", "FAIL_SYS_USER_NOT_EXIST")):
            return False
        return None  # 其它错误（网络/风控）不确定
    except Exception:
        return None


def _https_ali(url: str) -> str:
    """http 的 alicdn 图床转 https（https 页面 Mixed Content 会拦截 http 图，导致头像不显示）"""
    if url and url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url or ""


def _deep_pick(obj, keys, max_depth=4):
    """在（可能嵌套的）字典/列表里按 key 名找第一个非空字符串值（兼容接口返回结构变化）"""
    if max_depth < 0 or obj is None:
        return ""
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (int, float)) and k.lower().endswith("id"):
                return str(v)
        for v in obj.values():
            r = _deep_pick(v, keys, max_depth - 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj[:10]:
            r = _deep_pick(v, keys, max_depth - 1)
            if r:
                return r
    return ""


def fetch_account_profile(cookie_str: str):
    """拉取账号资料（昵称/头像/uid）供页面显示。
    优先 loginuser.get（深度检索字段，兼容返回结构变化）；失败回退「在售商品详情 sellerDO」
    （实测字段 nick/uniqueName/portraitUrl）。失败返回 None（由调用方回退 DOM 抓取）。"""
    # 路线 A：loginuser.get
    try:
        res = _mtop_post("mtop.taobao.idlemessage.pc.loginuser.get", "1.0",
                         {}, cookie_str, max_tries=1)
        data = res.get("data") or {}
        if data:
            nick = _deep_pick(data, ("nickName", "nick", "userName", "loginNick",
                                     "displayName", "name", "showName"))
            avatar = _deep_pick(data, ("avatarUrl", "avatar", "iconUrl", "imgUrl",
                                       "headPic", "portraitUrl", "headImgUrl"))
            uid = _deep_pick(data, ("userId", "uid", "sellerId", "unb", "userIdStr"))
            if nick or avatar or uid:
                return {"nick": (nick or "").strip(), "avatar": _https_ali(avatar), "userId": uid}
    except Exception:
        pass
    # 路线 B：在售商品详情 sellerDO（服务端卖家资料，稳定）
    try:
        unb = ""
        for part in cookie_str.split(";"):
            if part.strip().startswith("unb="):
                unb = part.split("=", 1)[1].strip()
                break
        if not unb:
            return None
        items, _ = fetch_item_list(cookie_str, unb, page_number=1, page_size=1)
        if not items:
            return None
        detail = fetch_item_detail(cookie_str, items[0]["id"])
        seller = ((detail or {}).get("sellerDO")) or {}
        if not isinstance(seller, dict):
            return None
        nick = str(seller.get("nick") or seller.get("uniqueName") or "").strip()
        avatar = _https_ali(str(seller.get("portraitUrl") or ""))
        if not nick and not avatar:
            return None
        # 昵称防御（同旧规则，避免把占位文本当昵称）
        if nick and not (2 <= len(nick) <= 24 and not nick.isdigit()):
            nick = ""
        return {"nick": nick, "avatar": avatar, "userId": str(seller.get("sellerId") or "")}
    except Exception:
        return None


def fetch_item_detail(cookie_str: str, item_id: str):
    """拉取商品详情（mtop.taobao.idle.pc.detail），用于保存"重新发布"所需信息。"""
    res = _mtop_post("mtop.taobao.idle.pc.detail", "1.0",
                     {"itemId": str(item_id), "from": "pc_main"},
                     cookie_str, extra_params={"spm_cnt": "a21ybx.item.0.0"})
    return res.get("data") or {}


def fetch_item_list(cookie_str: str, user_id, page_number=1, page_size=20):
    data = {
        "needGroupInfo": False, "pageNumber": page_number, "pageSize": page_size,
        "groupName": "在售", "groupId": "58877261", "defaultGroup": True, "userId": user_id,
    }
    res = _mtop_post("mtop.idle.web.xyh.item.list", "1.0", data, cookie_str,
                     extra_params={"spm_cnt": "a21ybx.im.0.0"})
    items = []
    for card in (res.get("data") or {}).get("cardList") or []:
        cd = card.get("cardData") or {}
        if not cd:
            continue
        item_id = (cd.get("detailParams") or {}).get("itemId") or cd.get("id") or ""
        price_info = cd.get("priceInfo") or {}
        items.append({
            "id": item_id, "title": cd.get("title", ""),
            "price": price_info.get("price", ""),
            "item_status": cd.get("itemStatus", 0),
            "detail_url": cd.get("detailUrl", ""),
            "web_url": f"https://www.goofish.com/item?id={item_id}" if item_id else "",
        })
    return items, res


# ---------- 写操作（用户触发，限速） ----------
def _throttle_write():
    global _last_write_ts
    now = time.time()
    wait = WRITE_MIN_INTERVAL - (now - _last_write_ts)
    if wait > 0:
        time.sleep(wait)
    _last_write_ts = time.time()


def delete_item(cookie_str: str, item_id):
    """删除真实商品（不可逆，调用方需二次确认）。
    使用 goofish-cli 验证过的普通卖家接口 com.taobao.idle.item.delete v1.1
    （闲鱼普通卖家"下架/删除"即此操作）。"""
    _throttle_write()
    res = _mtop_post("com.taobao.idle.item.delete", "1.1",
                     {"itemId": str(item_id)}, cookie_str,
                     extra_params={"spm_cnt": "a21ybx.item.0.0"})
    ret = res.get("ret") or []
    ok = any("SUCCESS" in r for r in ret)
    return {"ok": ok, "message": " | ".join(ret)}
