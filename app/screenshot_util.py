"""系统级截屏工具（聊天窗口"📷 截图"用，替代浏览器共享选择器）。

实现：GDI 抓取 Windows 虚拟桌面（全部显示器）为 32bpp 位图，
再用标准库 struct/zlib 手工编码为 PNG（无需 Pillow 等第三方库）。
注意：需在交互式桌面会话中运行（普通双击/托盘启动即可）；
在无窗口站/服务会话中 GetDC 会失败并抛出清晰错误。
"""
import ctypes
import struct
import zlib

_SRCCOPY = 0x00CC0020
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
                ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
                ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_int32),
                ('biYPelsPerMeter', ctypes.c_int32), ('biClrUsed', ctypes.c_uint32),
                ('biClrImportant', ctypes.c_uint32)]


def capture_screen_png():
    """抓取虚拟桌面全屏 → (png_bytes, width, height)"""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    x = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        raise RuntimeError(f"无法获取屏幕尺寸 ({w}x{h})，可能不在交互桌面会话中")

    hdc = user32.GetDC(0)
    if not hdc:
        raise RuntimeError("GetDC 失败：当前进程无桌面访问权限")
    try:
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        if not mem or not bmp:
            raise RuntimeError("创建位图失败")
        try:
            old = gdi32.SelectObject(mem, bmp)
            if not gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, _SRCCOPY):
                raise RuntimeError("BitBlt 抓屏失败")
            gdi32.SelectObject(mem, old)

            bih = _BITMAPINFOHEADER()
            bih.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bih.biWidth = w
            bih.biHeight = -h          # top-down
            bih.biPlanes = 1
            bih.biBitCount = 32
            bih.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), 0):
                raise RuntimeError("GetDIBits 失败")
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
    finally:
        user32.ReleaseDC(0, hdc)

    # BGRA(每像素4字节, A常为0) → RGB
    raw = bytearray(w * h * 3)
    src = buf.raw
    o = 0
    for i in range(0, len(src), 4):
        raw[o] = src[i + 2]
        raw[o + 1] = src[i + 1]
        raw[o + 2] = src[i]
        o += 3

    def _chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)   # 8bit RGB
    rows = bytearray()
    stride = w * 3
    for row0 in range(h):
        rows.append(0)
        start = row0 * stride
        rows += raw[start:start + stride]
    png = (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr) +
           _chunk(b'IDAT', zlib.compress(bytes(rows), 6)) + _chunk(b'IEND', b''))
    return png, w, h
