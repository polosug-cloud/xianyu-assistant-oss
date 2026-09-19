"""闲鱼助手 - 内置图片资源加载

从 assets/donation/ 读取分片资源并在运行期还原出图片；
分片缺失或校验不通过时返回空（页面与托盘提示资源不可用，不影响其它功能）。
仅使用标准库，无第三方依赖。
"""
import hashlib
import hmac
import struct

from . import config

MAGIC = b"XYQ1"
_PART_TMPL = "qr.part{}.dat"
_SEED = b"xianyu-assistant-asset-v1"
_PBKDF2_ITER = 20000
_HDR = 4 + 1 + 16 + 32 + 16          # magic + index + iv + share + mac


def _keystream(seed: bytes, n: int) -> bytes:
    out = b""
    c = 0
    while len(out) < n:
        out += hashlib.sha256(seed + struct.pack(">I", c)).digest()
        c += 1
    return out[:n]


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def part_path(index: int):
    return config.DONATION_DIR / _PART_TMPL.format(index)


def load_donation_qr():
    """读取全部分片并还原图片 → (image_bytes, mime)；分片不全/校验失败 → (None, None)"""
    try:
        count = int(getattr(config, "DONATION_PART_COUNT", 5))
        raws = []
        for i in range(count):
            p = part_path(i)
            if not p.exists():
                return None, None
            raw = p.read_bytes()
            if len(raw) <= _HDR or raw[:4] != MAGIC:
                return None, None
            raws.append(raw)
        if sorted(r[4] for r in raws) != list(range(count)):
            return None, None

        # 1) 由各分片组装出还原所需信息
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
            sl = _xor(payload, _keystream(_SEED + bytes([idx]) + share, len(payload)))
            if idx != 0:
                exp = hmac.new(key, bytes([idx]) + sl, hashlib.sha256).digest()[:16]
                if not hmac.compare_digest(mac, exp):
                    return None, None
            slices.append((idx, sl, mac))

        slices.sort(key=lambda t: t[0])
        ct = b"".join(t[1] for t in slices)

        # 2) 整体完整性校验
        exp0 = hmac.new(key, b"\x00" + iv + ct, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(slices[0][2], exp0):
            return None, None

        # 3) 还原图片数据
        dk = hashlib.pbkdf2_hmac("sha256", key, iv, _PBKDF2_ITER, dklen=len(ct))
        plain = _xor(ct, dk)
        if plain[:3] == b"\xff\xd8\xff":
            return plain, "image/jpeg"
        if plain[:8] == b"\x89PNG\r\n\x1a\n":
            return plain, "image/png"
        if plain[:4] == b"RIFF":
            return plain, "image/webp"
        return None, None
    except Exception:
        return None, None
