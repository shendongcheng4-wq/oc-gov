"""PDF：文字层 + 页面图。可选依赖 pypdfium2（Apache-2.0，PDFium 的绑定）。

为什么是 pypdfium2 而不是 pdfminer.six / PyMuPDF：

- **pdfminer.six（MIT）** 提文字质量好，但慢：313 页的书要 78 秒，实测取到的字
  还比 PDFium 少（少 5 万字，字体描述缺 BBox 时它直接跳过）。知识库上传是同步
  请求，这种耗时不能接受。
- **PyMuPDF（AGPL-3.0）** 最快也最强，但 AGPL 有传染性。这个项目要交付给别的
  单位，不能把整个系统拖进 AGPL。
- **pypdfium2（Apache-2.0）** 同一份书 0.68 秒、文字更多，而且它能把页面渲染成
  位图 —— 也就是知识库要的那张「预览图」，一个依赖同时解决两件事。

**没装就如实报错**，不静默降级：PDF 不像向量检索那样「降级了仍能用」，解不出文字
就是解不出，含糊过去只会让人以为文件坏了。
"""

from __future__ import annotations

import struct
import threading
import zlib
from typing import Any

try:  # 可选依赖：装在 requirements-optional.txt，不装也能跑其余功能
    import pypdfium2 as _pdfium

    _IMPORT_ERR = ""
except Exception as exc:  # pragma: no cover - 只在没装时走
    _pdfium = None
    _IMPORT_ERR = str(exc)

# PDFium 不是线程安全的：多个线程同时 open/document/page 会访问同一份全局状态。
# 实测 6 个线程一起渲染同一份文件时，进程不是抛异常而是直接消失（原生崩溃），
# 而服务是 ThreadingHTTPServer，一屏六张页面图正好同时打进来。所以这里加一把
# 全局锁把 PDFium 的所有调用串起来 —— 单页十几毫秒，串行的代价远小于崩一次。
#
# 锁管得住「我们调的」，管不住「析构函数调的」。pypdfium2 的 Document / Page /
# Bitmap 都带 finalizer，对象被 GC 回收时会在**回收那个线程**里调 PDFium ——
# 绕过了这把锁，也就是又回到「两个线程同时碰 PDFium」。所以下面每一处都显式
# close()，不把释放留给 __del__。
_LOCK = threading.Lock()

# 单次解析的闸门。PDFium 每页约 2ms，但一份 2000 页的汇编没有索引价值，
# 而且整段文字会被 _MAX_PREVIEW_CHARS 截掉，多解只是白烧 CPU。
MAX_PAGES = 400
# 一页里少于这么多字，就算「没有文字层」。空白页、纯图页都是这样。
_MIN_PAGE_CHARS = 8
# 判扫描件的两道口子：整份几乎没字，或者平均每页几乎没字。
# 不能只看总数 —— 一页 60 字的短通知不是扫描件；也不能只看比例 —— 两页里
# 一页有字一页空白，平均下来会把正常文件误判掉。
_SCANNED_TOTAL = 30
_SCANNED_PER_PAGE = 8


def available() -> bool:
    return _pdfium is not None


def unavailable_reason() -> str:
    """给用户看的缺依赖提示。不写「请 pip install」这种只有开发看得懂的话。"""
    if _pdfium is not None:
        return ""
    return (
        "这份文件是 PDF，但服务端没有装 PDF 解析组件（pypdfium2），"
        "暂时读不了它。装上之后 PDF 就能入库、能检索、能看页面预览。"
    )


def _open(data: bytes):
    return _pdfium.PdfDocument(data)


def page_count(data: bytes) -> int:
    if _pdfium is None:
        return 0
    try:
        with _LOCK:
            pdf = _open(data)
            try:
                return len(pdf)
            finally:
                pdf.close()
    except Exception:
        return 0


def extract(data: bytes, *, max_pages: int = MAX_PAGES) -> dict[str, Any]:
    """按页取出文字层。

    返回 `{ok, pages:[{no, text}], page_count, chars, capped, scanned, error}`。
    `page_count` 永远是文件真实页数（预览图要能翻到第 800 页），`pages` 只到
    `max_pages`；`capped=True` 说明后面没解。

    扫描件不报错 —— 它有页面图可看，只是搜不到。这个判断交给调用方决定怎么呈现，
    在这里一刀切掉会把「能翻页看」这件事也一起砍了。
    """
    if _pdfium is None:
        return {"ok": False, "error": unavailable_reason(), "scanned": False, "pages": []}
    if not data:
        return {"ok": False, "error": "文件是空的", "scanned": False, "pages": []}
    with _LOCK:
        return _extract(data, max_pages)


def _extract(data: bytes, max_pages: int) -> dict[str, Any]:
    try:
        pdf = _open(data)
    except Exception:
        # 加密的 PDF 也走这里：PDFium 打不开它，且不同版本异常类型不一样
        return {
            "ok": False,
            "error": "这份 PDF 打不开：可能设了打开密码，或者文件本身损坏",
            "scanned": False,
            "pages": [],
        }

    try:
        total = len(pdf)
        if total == 0:
            return {"ok": False, "error": "这份 PDF 里没有页面", "scanned": False, "pages": []}
        pages: list[dict[str, Any]] = []
        for i in range(min(total, max_pages)):
            page = pdf[i]
            tp = page.get_textpage()
            try:
                text = tp.get_text_range()
            finally:
                tp.close()
                page.close()
            # PDFium 给的是 \r\n，全库其它地方都按 \n 存，这里统一掉
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            pages.append({"no": i + 1, "text": text})
    except Exception as exc:  # PDFium 在坏页上会直接抛，不能让它把上传打成 500
        return {"ok": False, "error": f"PDF 解析到一半失败：{exc}", "scanned": False, "pages": []}
    finally:
        # 显式关掉，不交给 __del__：析构函数是在 GC 的线程里跑的，那把 _LOCK
        # 管不到它 —— 也就是「在别的线程里调 PDFium」，正是会静默干掉进程的那件事。
        pdf.close()

    chars = sum(len(p["text"].strip()) for p in pages)
    per_page = chars / max(1, len(pages))
    return {
        "ok": True,
        "error": "",
        "pages": pages,
        "page_count": total,
        "chars": chars,
        "capped": total > len(pages),
        "scanned": chars < _SCANNED_TOTAL or per_page < _SCANNED_PER_PAGE,
        "empty_pages": sum(1 for p in pages if len(p["text"].strip()) < _MIN_PAGE_CHARS),
    }


def render(data: bytes, page_no: int, *, width: int = 900) -> bytes:
    """把第 page_no 页（1 起）渲染成 PNG 字节。

    位图自己编码成 PNG（zlib + struct，二十来行），不引 Pillow/numpy：
    pypdfium2 的 `to_pil()` 要 Pillow，而 Pillow 只是为了存一个字节串就把整个
    依赖树拖进来，不值。
    """
    if _pdfium is None:
        raise RuntimeError(unavailable_reason())
    with _LOCK:
        return _render(data, page_no, width)


def _render(data: bytes, page_no: int, width: int) -> bytes:
    pdf = _open(data)
    try:
        total = len(pdf)
        if not 1 <= page_no <= total:
            raise IndexError(f"页码超出范围：{page_no} / 共 {total} 页")

        page = pdf[page_no - 1]
        try:
            # 按目标宽度反算缩放：PDF 的点单位是 72dpi，scale=1 在屏幕上偏小且发虚。
            pt_width = page.get_width() or 595.0
            scale = max(0.4, min(3.0, float(width) / float(pt_width)))
            bitmap = page.render(scale=scale)
            try:
                return _png(bitmap.buffer, bitmap.width, bitmap.height, bitmap.n_channels, bitmap.stride)
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        pdf.close()


def _png(buf, width: int, height: int, channels: int, stride: int) -> bytes:
    """BGR/BGRA → RGB PNG。PDFium 给的是 BGR（format=2），不是 RGB。

    逐像素在 Python 里换通道太慢（110 万像素要一两秒），所以整行用带步长的切片
    一次性取出来再按位置回填 —— 这些都是 C 层的内存搬运。
    """
    raw = bytes(buf)
    out = bytearray()
    for y in range(height):
        row = raw[y * stride : y * stride + width * channels]
        line = bytearray(width * 3 + 1)  # 首字节是 filter=0，其余按 RGB 填
        if channels >= 3:
            line[1::3] = row[2::channels]  # R（源里是 BGR，第 0 字节是蓝）
            line[2::3] = row[1::channels]  # G
            line[3::3] = row[0::channels]  # B
        else:  # 灰度：三通道同值
            v = bytes(row[0::channels])
            line[1::3] = v
            line[2::3] = v
            line[3::3] = v
        out += line
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(out), 6))
        + _chunk(b"IEND", b"")
    )


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )
