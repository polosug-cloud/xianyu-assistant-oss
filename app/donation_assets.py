"""闲鱼助手 - 内置图片资源加载

从内置资源目录读取并在运行期还原出图片；资源缺失或校验不通过时返回空
（页面与托盘提示资源不可用，不影响其它功能）。仅使用标准库，无第三方依赖。
"""
import hashlib
import hmac
import struct

from . import config

MAGIC = b"XYQ2"          # 当前格式
MAGIC_V1 = b"XYQ1"       # 旧格式（解码较慢）
_PART_TMPL = "qr.part{}.dat"
_SEED = b"xianyu-assistant-asset-v1"
_PBKDF2_ITER = 20000
_KEY_LEN = 32
_HDR = 4 + 1 + 16 + 32 + 16

_cache = {}              # {资源状态: (bytes, mime)}


def _sha_ks(seed: bytes, n: int) -> bytes:
    out = bytearray()
    c = 0
    while len(out) < n:
        out += hashlib.sha256(seed + struct.pack(">I", c)).digest()
        c += 1
    return bytes(out[:n])


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def part_path(index: int):
    return config.DONATION_DIR / _PART_TMPL.format(index)


def _mime_of(plain: bytes) -> str:
    if plain[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if plain[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if plain[:4] == b"RIFF":
        return "image/webp"
    return ""


def _read_parts(count):
    raws = []
    stamp = []
    for i in range(count):
        p = part_path(i)
        if not p.exists():
            return None, None
        raw = p.read_bytes()
        if len(raw) <= _HDR:
            return None, None
        raws.append(raw)
        stamp.append(f"{p.stat().st_mtime_ns}:{len(raw)}")
    if sorted(r[4] for r in raws) != list(range(count)):
        return None, None
    return raws, "|".join(stamp)


def _decode_v2(raws):

    key = b"\x00" * 32
    for r in raws:
        key = _xor(key, r[21:53])
    iv = raws[0][5:21]
    slices = []
    for r in raws:
        idx = r[4]
        share = r[21:53]
        mac = r[53:69]
        payload = r[69:]
        sl = _xor(payload, _sha_ks(_SEED + bytes([idx]) + share, len(payload)))
        if idx != 0:
            exp = hmac.new(key, bytes([idx]) + sl, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(mac, exp):
                return None
        slices.append((idx, sl, mac))
    slices.sort(key=lambda t: t[0])
    ct = b"".join(t[1] for t in slices)
    exp0 = hmac.new(key, iv + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(slices[0][2], exp0):
        return None
    dk = hashlib.pbkdf2_hmac("sha256", key, iv, _PBKDF2_ITER, dklen=_KEY_LEN)
    return _xor(ct, _sha_ks(dk + b"\x01", len(ct)))


def _decode_v1(raws):

    key = b"\x00" * 32
    for r in raws:
        key = _xor(key, r[21:53])
    iv = raws[0][5:21]
    slices = []
    for r in raws:
        idx = r[4]
        share = r[21:53]
        mac = r[53:69]
        payload = r[69:]
        sl = _xor(payload, _sha_ks(_SEED + bytes([idx]) + share, len(payload)))
        if idx != 0:
            exp = hmac.new(key, bytes([idx]) + sl, hashlib.sha256).digest()[:16]
            if not hmac.compare_digest(mac, exp):
                return None
        slices.append((idx, sl, mac))
    slices.sort(key=lambda t: t[0])
    ct = b"".join(t[1] for t in slices)
    exp0 = hmac.new(key, b"\x00" + iv + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(slices[0][2], exp0):
        return None
    dk = hashlib.pbkdf2_hmac("sha256", key, iv, _PBKDF2_ITER, dklen=len(ct))
    return _xor(ct, dk)


def load_donation_qr():
    """读取内置资源并还原图片 → (bytes, mime)；缺失/校验失败 → (None, None)"""
    try:
        count = int(getattr(config, "DONATION_PART_COUNT", 5))
        raws, ck = _read_parts(count)
        if not raws:
            return None, None
        if ck in _cache:
            return _cache[ck]
        magic = raws[0][:4]
        if magic == MAGIC:
            plain = _decode_v2(raws)
        elif magic == MAGIC_V1:
            plain = _decode_v1(raws)
        else:
            return None, None
        if not plain:
            return None, None
        mime = _mime_of(plain)
        if not mime:
            return None, None
        _cache.clear()
        _cache[ck] = (plain, mime)
        return plain, mime
    except Exception:
        return None, None
