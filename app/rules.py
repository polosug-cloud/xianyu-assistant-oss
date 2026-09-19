"""闲鱼助手 - 关键词规则回复引擎（本地规则优先，AI 兜底）"""
import re
import time

from . import config
from . import db


def load_rules(product_id=None):
    """加载启用规则；product_id 给定则只加载 通用 + 包含该商品(多选) 的规则。
    未给定则返回全部启用规则。"""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reply_rules WHERE enabled=1 ORDER BY id").fetchall()
    rules = []
    for r in rows:
        d = dict(r)
        if product_id is not None and not db.rule_matches_product(d, product_id):
            continue
        rules.append(d)
    return rules


def match_rule(rules, message: str):
    """返回 (rule, matched_keyword) 或 (None, None)"""
    msg = (message or "").lower().strip()
    for r in rules:
        kw_list = [k.strip().lower() for k in (r["keywords"] or "").split(",") if k.strip()]
        if r["match_type"] == "all":
            if all(k in msg for k in kw_list):
                return r, None
        elif r["match_type"] == "regex":
            try:
                if any(re.search(k, msg) for k in kw_list):
                    return r, None
            except re.error:
                continue
        else:  # contains
            for k in kw_list:
                if k and k in msg:
                    return r, k
    return None, None


def render_template(template: str, ctx: dict):
    """替换模板变量：{卡密} {商品名} {买家} {订单号}"""
    out = template
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v or ""))
    return out


class ReplyEngine:
    def __init__(self, rule_cooldown_sec=None):
        self._last_reply = {}          # peer_id -> timestamp
        self._rule_cooldown = config.RULE_COOLDOWN_SEC if rule_cooldown_sec is None else rule_cooldown_sec

    def decide(self, peer_id: str, message: str, ctx: dict, product_id=None):
        """返回 (text, source)；source: rule/ai/none。
        product_id 给定：规则取 通用 + 含该商品(可多选) 的规则（商品专属优先）。"""
        now = time.time()
        last = self._last_reply.get(peer_id, 0)
        if now - last < self._rule_cooldown:
            return None, "cooldown"
        rules = load_rules(product_id)
        # 商品专属规则优先（非通用），其次通用
        rule, kw = match_rule([r for r in rules if db.rule_product_ids(r)], message)
        if rule is None:
            rule, kw = match_rule([r for r in rules if not db.rule_product_ids(r)], message)
        if rule:
            self._last_reply[peer_id] = now
            return render_template(rule["reply_template"], ctx), "rule"
        if config.AI_ENABLED:
            text = self._ai_reply(message, ctx)
            if text:
                self._last_reply[peer_id] = now
                return text, "ai"
        return None, "none"

    def _ai_reply(self, message: str, ctx: dict) -> str:
        """OpenAI 兼容接口（DeepSeek 等）。失败返回空串走兜底。"""
        try:
            import urllib.request
            import json as _json
            sys_prompt = config.AI_SYSTEM_PROMPT
            if ctx.get("商品名"):
                sys_prompt += f"\n当前商品：{ctx['商品名']}"
            body = _json.dumps({
                "model": config.AI_MODEL,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": message},
                ],
                "max_tokens": 300,
                "temperature": 0.6,
            }).encode("utf-8")
            req = urllib.request.Request(
                config.AI_BASE_URL.rstrip("/") + "/chat/completions",
                data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {config.AI_API_KEY}"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = _json.load(resp)
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            db.log_op("warn", "ai", f"AI 回复失败: {type(e).__name__} {e}")
            return ""
