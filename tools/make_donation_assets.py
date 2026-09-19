"""生成本地图片资源文件（与 app/donation_assets.py 配套）

用法：
    python tools/make_donation_assets.py <图片文件> [输出目录] [分片数]

默认：输出目录 assets/donation，分片数 5（需与 app/config.py 的 DONATION_PART_COUNT 一致）。
仅使用标准库，无第三方依赖。
"""
import hashlib
import hmac
import os
import struct
import sys

MAGIC = b"XYQ1"
SEED = b"xianyu-assistant-asset-v1"
ITER = 20000


def keystream(seed: bytes, n: int) -> bytes:
    out = b""
    c = 0
    while len(out) < n:
        out += hashlib.sha256(seed + struct.pack(">I", c)).digest()
        c += 1
    return out[:n]


def xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join("assets", "donation")
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    plain = open(src, "rb").read()
    if not plain:
        print("图片为空")
        return 1
    print(f"明文图片: {len(plain)} bytes, 分片数: {n}")

    key = os.urandom(32)
    iv = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", key, iv, ITER, dklen=len(plain))
    ct = xor(plain, dk)

    shares = [os.urandom(32) for _ in range(n - 1)]
    acc = key
    for s in shares:
        acc = xor(acc, s)
    shares.append(acc)

    size = (len(ct) + n - 1) // n
    os.makedirs(out_dir, exist_ok=True)
    for i in range(n):
        sl = ct[i * size:(i + 1) * size]
        share = shares[i]
        payload = xor(sl, keystream(SEED + bytes([i]) + share, len(sl)))
        if i == 0:
            mac = hmac.new(key, b"\x00" + iv + ct, hashlib.sha256).digest()[:16]
            iv_field = iv
        else:
            mac = hmac.new(key, bytes([i]) + sl, hashlib.sha256).digest()[:16]
            iv_field = b"\x00" * 16
        blob = MAGIC + bytes([i]) + iv_field + share + mac + payload
        path = os.path.join(out_dir, f"qr.part{i}.dat")
        with open(path, "wb") as f:
            f.write(blob)
        print(f"  写出 {path}  {len(blob)} bytes")
    print("完成。请把全部分片一起提交，并确保 config.DONATION_PART_COUNT 与分片数一致。")
    return 0

if __name__ == "__main__":
    sys.exit(main())
