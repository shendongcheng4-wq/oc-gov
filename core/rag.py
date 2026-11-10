"""通用文档 RAG：把上传的文件变成可检索的原文片段。

与 core/knowledge.py 的分工：
- knowledge.py 检索的是**系统内部已有的结构化数据**（指标口径、督办台账、会议记录），
  用 bigram + 术语加权，零依赖，返回的每条都带出处。
- 本模块检索的是**用户上传的任意文档**（政策文件、报告、汇报材料），落盘成
  分类库，切块建索引，问句来了按相关度召回原文片段。

界面上两者合并成一个「知识库」：清单页把系统记录和上传文档排在一起，靠
`kind == "doc"` 区分。合并发生在 app/main.py（`_kb_doc_entries`），不在这里 ——
两个模块谁也不该成为对方的下游。

两条链路都守同一条边界：**检索到的永远是真实存在的原文，模型不参与复述**。
数字进不进模型与"怎么检索"无关 —— 向量也好、bigram 也好，找到的都是原文，
原文带出处展示，模型没有机会把甲文件的话安到乙文件头上。

## 向量化是可选的

项目立身之本是「标准库即可跑」。向量化要 onnxruntime / fastembed 这类第三方
依赖，所以走**懒加载 + 静默降级**：

- 装了 fastembed（`pip install fastembed`）→ 真向量检索，能召回"用词不同但
  意思相关"的片段；
- 没装 → 退回 bigram 关键词检索（复用 knowledge.py 的思路），功能照常，只是
  召不回同义改写。

判定在 import 时做一次，之后每次调用只查一个布尔，不反复 try。

## 存储

- 控制面（库、文档的元数据）进 `data/rag.json`，复用 core/store.py 的 JSON 方案，
  跟项目其余部分一致，能进 git diff、坏了能手工修。
- 原文落盘 `data/rag_files/<kb_id>/<doc_id>.<ext>`，索引里只存切块和向量，
  不重复存全文。
- 每个库一个 SQLite 向量索引（`data/rag_idx/<kb_id>.sqlite`）。SQLite 是标准库
  自带，不破零依赖；embedding 用 struct.pack 存 BLOB，检索时进程内算余弦，
  几百上千块足够快 —— 这正是 octop 的 KnowledgeIndex 做法，规模再大才需要
  专用向量库。

## 依赖方向

本模块只依赖标准库 + 同项目 core 模块（store / docparse），不感知 HTTP。
将来包成 MCP 工具直接拿 create_base / upload_document / search 去用即可。
"""

from __future__ import annotations

import bisect
import hashlib
import math
import re
import secrets
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any

import embedding
import store

# ── 目录 ──────────────────────────────────────────────────────────────────
FILES_DIR = store.DATA_DIR / "rag_files"
INDEX_DIR = store.DATA_DIR / "rag_idx"
FILE = "rag"

# 切块参数（照 octop 的固定窗口 + 重叠）
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

# 检索默认参数（照 octop 的 retrieve.py）
DEFAULT_K = 8
DEFAULT_CHAR_BUDGET = 6000

# 库与文档的数量上限
MAX_BASES = 20
MAX_DOCS_PER_BASE = 100
MAX_DOC_BYTES = 9 * 1024 * 1024          # 单个文件解码后上限（与附件接口一致）
_MAX_PREVIEW_CHARS = 12000                # 单个文档参与切块的最大字数


# ── 向量化：懒加载 + 降级 ────────────────────────────────────────────────
#
# 复用 core/embedding.py 的共享向量化（fastembed + bge-small-zh-v1.5，
# 内网离线加载）。不可用就 _HAVE_EMBED = False，检索退回 bigram。
_HAVE_FASTEMBED = embedding.embedding_available()

_HAVE_EMBED = False


def _embed_model():
    """懒加载一个中文 embedding 模型。失败返回 None（调用方降级）。

    直接委托共享模块的实例，不再自己缓存一份。
    """
    global _HAVE_EMBED
    model = embedding._embed_model()
    _HAVE_EMBED = model is not None
    return model


def embedding_available() -> bool:
    return embedding.embedding_available()


def _embed(texts: list[str]) -> list[list[float]] | None:
    """把一串文本变成向量。不可用返回 None。"""
    return embedding.embed(texts)


# ── bigram 降级检索（与 knowledge.py 同一套思路） ─────────────────────────
#
# 中文没空格、标准库没分词器，把问句和片段都切成相邻二字组求覆盖率，是零依赖下
# 最稳的做法。这里只用于向量化不可用时的兜底，逻辑比 knowledge.py 精简：
# 片段是上传文档切出来的，没有"术语/单位/台账编号"那套加权。

_STOP_PHRASES = (
    "请问", "帮我", "查一下", "查查", "看一下", "看看", "介绍一下", "讲讲", "说说",
    "是怎么", "怎么", "如何", "什么", "哪些", "哪个", "多少", "为什么", "为何",
    "是否", "有没有", "是不是", "能不能", "可以", "一下", "有关", "关于", "相关",
    "情况", "怎么样",
)
_STOP_CHARS = set("的了吗呢吧啊呀哦么是在有和与及或就都也还很太更最把被让给对从向往于着过")


def _bigrams(text: str) -> set[str]:
    s = str(text or "")
    for p in _STOP_PHRASES:
        s = s.replace(p, " ")
    s = "".join(c for c in s if c not in _STOP_CHARS)
    s = re.sub(r"[\s\W_]+", "", s)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


# ── 切块 ──────────────────────────────────────────────────────────────────


def chunk_text(text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """固定窗口 + 重叠切块。照 octop 的 chunk_text，纯字符窗口不做语义切分。"""
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be non-negative and smaller than size")
    content = (text or "").strip()
    if not content:
        return []
    step = size - overlap
    chunks: list[str] = []
    for start in range(0, len(content), step):
        chunks.append(content[start : start + size])
        if start + size >= len(content):
            break
    return chunks


# ── 控制面（JSON） ────────────────────────────────────────────────────────


def _empty() -> dict[str, Any]:
    return {"version": 1, "bases": []}


def _state() -> dict[str, Any]:
    raw = store.read(FILE)
    if not isinstance(raw, dict) or not isinstance(raw.get("bases"), list):
        raw = _empty()
        store.write(FILE, raw)
        return raw
    raw.setdefault("version", 1)
    raw.setdefault("bases", [])
    return raw


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _doc_path(kb_id: str, doc_id: str, filename: str) -> Path:
    return FILES_DIR / kb_id / f"{doc_id}{Path(filename).suffix.lower()}"


# ── 向量索引（每库一个 SQLite） ──────────────────────────────────────────


class _Index:
    """一个知识库的 SQLite 向量索引。embedding 存 BLOB，进程内算余弦。"""

    def __init__(self, kb_id: str):
        self._path = INDEX_DIR / kb_id / "index.sqlite"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    embedding BLOB,
                    meta_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def replace_doc_chunks(
        self,
        doc_id: str,
        texts: list[str],
        embeddings: list[list[float]] | None = None,
        *,
        metadata: list[dict[str, Any]] | None = None,
    ) -> None:
        if embeddings is not None and len(texts) != len(embeddings):
            raise ValueError("texts and embeddings must have the same length")
        rows: list[tuple[str, str, int, str, bytes | None, str]] = []
        for ordinal, text in enumerate(texts):
            vec = None
            if embeddings is not None:
                vec = struct.pack(f"<{len(embeddings[ordinal])}f", *[float(x) for x in embeddings[ordinal]])
            meta = metadata[ordinal] if metadata is not None else {}
            import json

            rows.append(
                (
                    f"{doc_id}:{ordinal}",
                    doc_id,
                    ordinal,
                    text,
                    vec,
                    json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
                )
            )
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                "INSERT INTO chunks(chunk_id, doc_id, ordinal, text, embedding, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def delete_doc(self, doc_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    def search(self, query_vec: list[float], k: int) -> list[dict[str, Any]]:
        """按余弦相似度返回 top-k 块（只对有向量的块算）。"""
        if k <= 0:
            return []
        query = [float(x) for x in query_vec]
        qnorm = math.sqrt(sum(x * x for x in query))
        if qnorm == 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT doc_id, ordinal, text, embedding FROM chunks WHERE embedding IS NOT NULL"
            ).fetchall()
        hits: list[dict[str, Any]] = []
        for doc_id, ordinal, text, blob in rows:
            vec = struct.unpack(f"<{len(blob) // 4}f", blob)
            if len(vec) != len(query):
                continue
            norm = math.sqrt(sum(x * x for x in vec))
            score = 0.0 if norm == 0 else sum(a * b for a, b in zip(query, vec)) / (qnorm * norm)
            hits.append({"doc_id": doc_id, "ordinal": ordinal, "text": text, "score": score})
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:k]


def _all_chunks(kb_id: str) -> list[dict[str, Any]]:
    """取一个库全部块（供 bigram 降级检索用）。"""
    path = INDEX_DIR / kb_id / "index.sqlite"
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT doc_id, ordinal, text FROM chunks").fetchall()
    return [{"doc_id": d, "ordinal": o, "text": t} for d, o, t in rows]


# ── 库与文档管理 ──────────────────────────────────────────────────────────


def list_bases() -> list[dict[str, Any]]:
    out = []
    for b in _state()["bases"]:
        docs = [d for d in b.get("documents") or []]
        out.append(
            {
                "id": b["id"],
                "name": b["name"],
                "description": b.get("description") or "",
                "created_at": b.get("created_at"),
                "doc_count": len(docs),
                "chunk_count": sum(int(d.get("chunk_count") or 0) for d in docs),
            }
        )
    return out


def create_base(name: str, description: str = "") -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("分类库名称不能为空")
    state = _state()
    if len(state["bases"]) >= MAX_BASES:
        raise ValueError(f"最多创建 {MAX_BASES} 个分类库")
    if any(b["name"] == name for b in state["bases"]):
        raise ValueError(f"已存在同名分类库「{name}」")
    import time

    base = {
        "id": _new_id("kb"),
        "name": name,
        "description": str(description or "").strip(),
        "created_at": int(time.time()),
        "documents": [],
    }
    state["bases"].append(base)
    store.write(FILE, state)
    _Index(base["id"])  # 提前建好索引目录
    return base


def get_base(kb_id: str) -> dict[str, Any] | None:
    for b in _state()["bases"]:
        if b["id"] == kb_id:
            return b
    return None


def delete_base(kb_id: str) -> bool:
    state = _state()
    keep = [b for b in state["bases"] if b["id"] != kb_id]
    if len(keep) == len(state["bases"]):
        return False
    state["bases"] = keep
    store.write(FILE, state)
    _remove_tree(FILES_DIR / kb_id)
    _remove_tree(INDEX_DIR / kb_id)
    return True


def _remove_tree(path: Path) -> None:
    import shutil

    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _bump(updated: dict[str, Any]) -> dict[str, Any]:
    state = _state()
    for i, b in enumerate(state["bases"]):
        if b["id"] == updated["id"]:
            state["bases"][i] = updated
            break
    store.write(FILE, state)
    return updated


# ── 解析：复用 docparse，未支持的格式明说 ────────────────────────────────


def _parse_bytes(data: bytes, name: str) -> tuple[str, str]:
    """解析文件为纯文本，返回 (text, error)。

    只给「不要页码」的调用方用；上传走 `_parse_document`，那份还要页码映射。
    """
    text, _meta, err = _parse_document(data, name)
    return text, err


def _is_pdf(name: str, data: bytes) -> bool:
    """后缀和文件头任一说是 PDF 就算。

    PDF 后缀被改错的（比如 .pdf 存成了 txt）很少见，但反过来 —— 从网盘下来
    后缀丢了 —— 很常见，所以文件头也要认一眼。
    """
    return (name or "").lower().endswith(".pdf") or data[:4] == b"%PDF"


def _parse_document(data: bytes, name: str) -> tuple[str, dict[str, Any], str]:
    """解析成 (文本, 页码元信息, 错误)。

    页码元信息只对 PDF 有：`{count, starts, indexed, scanned, capped}`。
    `starts[i]` 是第 i 页正文在全文里的起始偏移，前端拿它把「命中位置」换成
    「第几页」—— 阅读视图显示的本来就是那一页的图。
    """
    if not data:
        return "", {}, "文件是空的"
    low = (name or "").lower()
    if _is_pdf(low, data):
        return _parse_pdf(data)
    if low.endswith((".md", ".txt", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".html", ".htm")):
        text = _read_text_bytes(data)
        if text.strip():
            return text, {}, ""
        return "", {}, "文件里没有文字"
    import docparse

    out = docparse.parse_book(data, name, limit=_MAX_PREVIEW_CHARS)
    if out.get("ok"):
        return out["text"], {}, ""
    return "", {}, out.get("error") or "解析失败"


def _parse_pdf(data: bytes) -> tuple[str, dict[str, Any], str]:
    """PDF：按页取文字层，顺手记下每页的起点。

    页码是 PDF 阅读视图的骨架 —— 没有它，"命中在第 5 页"就只能退回"命中在
    第 3400 个字"，而用户看的是页面图。所以这里拼全文时手工算偏移，不靠
    `"\\n".join` 之后再去数。
    """
    import pdfdoc

    got = pdfdoc.extract(data)
    if not got.get("ok"):
        return "", {}, str(got.get("error") or "PDF 解析失败")

    starts: list[int] = []
    parts: list[str] = []
    pos = 0
    for page in got["pages"]:
        body = str(page.get("text") or "").strip()
        starts.append(pos)
        parts.append(body)
        pos += len(body) + 1  # +1 是 join 时那个换行

    text = "\n".join(parts)
    # 和文本文件同一个上限：超了只索引前面这部分，后面页码照样能翻（预览图
    # 是从原文件渲染的，不依赖索引），但要如实说"只索引了前 N 页"。
    if len(text) > _MAX_PREVIEW_CHARS:
        text = text[:_MAX_PREVIEW_CHARS]
    indexed = sum(1 for s in starts if s < len(text))
    meta = {
        "count": int(got.get("page_count") or len(starts)),
        "starts": starts[:indexed],
        "indexed": indexed,
        "scanned": bool(got.get("scanned")),
        "capped": indexed < int(got.get("page_count") or len(starts)),
    }
    return text, meta, ""


def _read_text_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


# ── 文档上传 / 删除 ───────────────────────────────────────────────────────


def upload_document(
    kb_id: str,
    filename: str,
    content: bytes,
    *,
    content_type: str = "",
) -> dict[str, Any]:
    base = get_base(kb_id)
    if base is None:
        raise LookupError("分类库不存在")
    if len(base["documents"]) >= MAX_DOCS_PER_BASE:
        raise ValueError(f"一个分类库最多 {MAX_DOCS_PER_BASE} 个文档")
    if len(content) > MAX_DOC_BYTES:
        raise ValueError(f"文件超过 {MAX_DOC_BYTES // (1024 * 1024)}MB")

    # 只留文件名：有的客户端会把整条本地路径当 name 传上来（浏览器不会，脚本
    # 会），存进清单就变成「C:/Users/xxx/Desktop/…」这种一屏放不下的标题。
    # 顺带挡掉 `../` 这种不像文件名的东西 —— 名字只用于显示和取后缀。
    raw_name = str(filename or "").strip()
    name = Path(raw_name.replace("\\", "/")).name.strip() or "未命名"
    name = name[:120] or "未命名"
    text, pages, err = _parse_document(content, name)
    if err:
        raise ValueError(f"{name}：{err}")

    chunks = chunk_text(text)
    # 扫描件没有文字层，索引为空 —— 但它有页面图，能翻能看，只是搜不到。
    # 一律拒收会连带砍掉"能看"这件事，而红头文件扫描件恰恰是最常见的来文。
    scanned = bool(pages.get("scanned"))
    if not chunks and not scanned:
        raise ValueError(f"{name}：没有可索引的文字")

    # 向量化（可能降级为 None）
    vecs = _embed(chunks)

    doc_id = _new_id("doc")
    import time

    # 落盘原文
    path = _doc_path(kb_id, doc_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    # 建索引
    idx = _Index(kb_id)
    idx.replace_doc_chunks(doc_id, chunks, vecs)

    doc = {
        "id": doc_id,
        "name": name,
        "content_type": content_type or "",
        "byte_size": len(content),
        "chars": len(text),
        "chunk_count": len(chunks),
        "embedded": vecs is not None,
        "created_at": int(time.time()),
    }
    if pages:
        # 页码表进控制面：阅读视图靠它把「命中位置」换算成「第几页」。
        # 12000 字上限下最多几十页，几百个整数，不值得另开一个文件。
        doc["pages"] = pages
    base["documents"].append(doc)
    _bump(base)
    return doc


def list_documents(kb_id: str) -> list[dict[str, Any]]:
    base = get_base(kb_id)
    if base is None:
        return []
    return list(base.get("documents") or [])


def browse_documents() -> list[dict[str, Any]]:
    """跨库列出全部文档，新→旧。

    和 `list_documents` 是两个用途：那个要指定库、给单库的管理面板用；
    这个供知识库页面当条目浏览，所以要带上库名，否则「XX政策.pdf」看不出
    它属于哪个库、为什么会被召回。
    """
    out: list[dict[str, Any]] = []
    for b in _state()["bases"]:
        for d in b.get("documents") or []:
            out.append({**d, "kb_id": b["id"], "kb_name": b["name"]})
    out.sort(key=lambda d: int(d.get("created_at") or 0), reverse=True)
    return out


def preview_chunks(kb_id: str, doc_id: str, limit: int = 3) -> list[str]:
    """取一篇文档开头的若干块，给清单页做预览。

    不重新解析原文件：索引里存的就是切好的原文，再解一遍只为得到同样的
    结果，还多一次格式兼容的风险。
    """
    path = INDEX_DIR / kb_id / "index.sqlite"
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE doc_id = ? ORDER BY ordinal LIMIT ?",
            (doc_id, max(1, int(limit))),
        ).fetchall()
    return [str(r[0]) for r in rows]


# ── 阅读视图：全文还原、章节切分、命中定位 ──────────────────────────────
#
# 清单页原先只给「前 3 块 + 四格元信息」，读起来像一张表。文件不是表格，
# 它是章节结构；检索命中的也不是"某个 800 字窗口"，而是"某份文件的某一节"。
# 这里把两件事补齐：还原全文、认出章节，再把命中块映射到章节上。


def document_chunks(kb_id: str, doc_id: str) -> list[dict[str, Any]]:
    """按顺序取一篇文档的全部块（原文，不含向量）。"""
    path = INDEX_DIR / kb_id / "index.sqlite"
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT ordinal, text FROM chunks WHERE doc_id = ? ORDER BY ordinal",
            (doc_id,),
        ).fetchall()
    return [{"ordinal": int(o), "text": str(t)} for o, t in rows]


def document_text(kb_id: str, doc_id: str) -> str:
    """从索引还原全文。

    不重新解析原文件：索引里的块就是当初解析出来的那一份，重解只会多一次
    格式兼容的风险。切块是固定步长（size - overlap），拼回去时每块去掉与
    已拼文本尾部重叠的那 `CHUNK_OVERLAP` 个字即可。
    """
    chunks = document_chunks(kb_id, doc_id)
    if not chunks:
        return ""
    acc = chunks[0]["text"]
    for c in chunks[1:]:
        cut = min(CHUNK_OVERLAP, len(c["text"]))
        acc += c["text"][cut:]
    return acc


# 章节标题识别。政策文件的层级就这几种写法，一份文件里常混着用：
#   # 一级 / ## （一）…   （markdown）
#   第一章 / 第一部分
#   一、总体目标
#   （一）主要任务
#   1. 具体措施
# 只认「整行就是标题」，且不超过 _MAX_HEADING_CHARS 字 —— 正文里
# 「一、二、三产业融合」这种句子不该把文档切成两半。
_MAX_HEADING_CHARS = 40
# 纯数字编号（1. / 2、）在公文里既可能是小标题，也可能是条款。条款的特征是
# 带冒号/句号、句子长 —— 「1. 本单元业主表决通过：专有部分面积占……」当标题
# 认会把正文切得七零八落，所以这一路额外收紧。
_MAX_DIGIT_HEADING_CHARS = 24
_HEADING_RE = re.compile(
    r"^(?:(?P<md>#{1,6})\s*(?P<md_t>.+?)"
    r"|(?P<cn>第[一二三四五六七八九十百零〇\d]+[章节篇部分])\s*(?P<cn_t>.*)"
    r"|(?P<num>[一二三四五六七八九十百零〇]{1,4})、\s*(?P<num_t>.+)"
    r"|(?P<paren>[（(][一二三四五六七八九十百零〇\d]{1,4}[）)])\s*(?P<paren_t>.+)"
    r"|(?P<digit>\d{1,2}(?:\.\d{1,2})*)[、.．]\s*(?P<digit_t>\S.*)"
    r")$"
)


def _split_head(text: str) -> tuple[str, str]:
    """把「一、总体目标：到 2026 年底……」拆成标题和同行正文。

    公文里标题和第一句经常写在同一行（尤其是「一、总体目标：……」），整行当
    标题会把这段内容吞掉 —— 阅读视图上只剩一个标题、正文空着。有冒号就拆，
    没有就是纯标题。
    """
    m = re.match(r"^(.{1,30}?)[：:]\s*(.+)$", (text or "").strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (text or "").strip(), ""


def _heading_of(line: str) -> tuple[str, int, str] | None:
    """一行如果像标题，返回 (标题文本, 层级, 同行正文)，否则 None。"""
    stripped = (line or "").strip()
    if not stripped or len(stripped) > _MAX_HEADING_CHARS:
        return None
    m = _HEADING_RE.match(stripped)
    if not m:
        return None
    if m.group("md"):
        title, rest = _split_head(m.group("md_t"))
        return title, len(m.group("md")), rest
    if m.group("cn"):
        title, rest = _split_head(stripped)
        return title, 1, rest
    if m.group("num"):
        title, rest = _split_head(stripped)
        return title, 1, rest
    if m.group("paren"):
        title, rest = _split_head(stripped)
        return title, 2, rest
    if len(stripped) > _MAX_DIGIT_HEADING_CHARS or re.search(r"[：:。；;]", stripped):
        return None
    return stripped, 3, ""


def split_chapters(text: str) -> list[dict[str, Any]]:
    """按标题行把正文切成章节。

    返回 [{title, level, start, end, text}]，按原文顺序。第一个标题之前的内容
    单独成一段（title 为空、level 0）—— 政策文件开头常有发文单位、成文日期，
    丢掉它们会让预览从半句话开始。整篇没有标题时，全文就是一段。
    """
    content = str(text or "")
    if not content.strip():
        return []

    marks: list[tuple[int, str, int, str]] = []
    pos = 0
    for line in content.split("\n"):
        hit = _heading_of(line)
        if hit:
            # 行首可能有缩进，偏移要算到标题真正开始的位置
            offset = len(line) - len(line.lstrip())
            marks.append((pos + offset, hit[0], hit[1], hit[2]))
        pos += len(line) + 1

    if not marks:
        return [{"title": "", "level": 0, "start": 0, "end": len(content), "text": content.strip("\n")}]

    out: list[dict[str, Any]] = []
    if marks[0][0] > 0:
        head = content[: marks[0][0]]
        if head.strip():
            out.append({"title": "", "level": 0, "start": 0, "end": marks[0][0], "text": head.strip("\n")})
    for i, (start, title, level, rest) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(content)
        segment = content[start:end]
        # 标题单独给，正文从标题那一行剩下的部分（冒号后的内容）+ 后续行开始，
        # 前端才不会把标题显示两遍，也不会丢掉写在标题行里的第一句。
        after = segment.split("\n", 1)[1].lstrip("\n") if "\n" in segment else ""
        body = f"{rest}\n{after}".strip("\n") if rest else after
        out.append({"title": title, "level": level, "start": start, "end": end, "text": body})
    return out


def _chapter_index(chapters: list[dict[str, Any]], pos: int) -> int:
    """字符偏移落在第几章。章节按 start 升序，线性扫足够（一章几千字以内）。"""
    if not chapters:
        return 0
    for i, c in enumerate(chapters):
        if int(c["start"]) <= pos < int(c["end"]):
            return i
    return len(chapters) - 1


def _page_of(starts: list[int], pos: int) -> int:
    """字符偏移落在第几页（1 起）。starts 是每页正文起点，升序。

    用二分而不是线性扫：一份 300 页的文件每章都要算一次，线性扫是 O(页数×章节数)。
    """
    if not starts:
        return 0
    return max(1, bisect.bisect_right(starts, max(0, int(pos))))


def doc_hits(kb_id: str, doc_id: str, query: str, k: int = 6) -> list[dict[str, Any]]:
    """一篇文档内部的命中块（带 ordinal，供映射到章节）。

    复用 `search` 而不是再写一套打分：同一个问句对同一份文件，问答复用的
    召回结果和这里看到的应该是同一批块，否则「页面说命中第 3 章、问答却引
    第 5 段」只是时间问题。
    """
    q = str(query or "").strip()
    if not q:
        return []
    hits = search([kb_id], q, k=max(24, k * 6), char_budget=10**9)
    mine = [h for h in hits if h["doc_id"] == doc_id]
    return mine[:k]


def doc_view(
    kb_id: str,
    doc_id: str,
    query: str = "",
    *,
    max_chars: int = 40000,
) -> dict[str, Any] | None:
    """一篇文档的阅读视图：元信息 + 章节 + 命中章节。

    `max_chars` 是上限不是目标：正常政策文件几千到两万字，全给。只有病态的
    超大文本才截断，并在返回值里如实标 `truncated`，前端不能假装它是全文。
    """
    base = get_base(kb_id)
    if base is None:
        return None
    doc = next((d for d in base["documents"] if d["id"] == doc_id), None)
    if doc is None:
        return None

    text = document_text(kb_id, doc_id)
    truncated = False
    if max_chars and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    chapters = split_chapters(text)

    # PDF 的页码表：有了它，前端才能把章节/命中换成「第几页」并加载那一页的图
    pages_meta = doc.get("pages") or {}
    starts = list(pages_meta.get("starts") or [])
    if starts:
        for c in chapters:
            c["page"] = _page_of(starts, int(c.get("start") or 0))

    hits: list[dict[str, Any]] = []
    q = str(query or "").strip()
    if q:
        step = CHUNK_SIZE - CHUNK_OVERLAP
        for h in doc_hits(kb_id, doc_id, q):
            chunk_text_ = str(h["text"])
            # 章节定位用「块起点 + 词在块内的位置」，不是块起点：一个块常横跨
            # 两三节，只按起点算会把它整段记到上一节头上。字面找不到（换了说法、
            # 只靠语义命中）时退回块中点，比起点更不容易指错。
            idx = chunk_text_.find(q)
            pos = int(h["ordinal"]) * step + (idx if idx >= 0 else len(chunk_text_) // 2)
            ch = _chapter_index(chapters, pos)
            # 命中片段：语义命中时正文里根本没有原词（换说法也能召回），只高亮
            # 正文等于什么都没标。给一小段命中原文，让人看见"凭什么是这一节"。
            anchor = idx if idx >= 0 else 0
            snip_start = max(0, anchor - 100)
            snippet = chunk_text_[snip_start : snip_start + 320].strip()
            if snip_start > 0:
                snippet = "…" + snippet
            if snip_start + 320 < len(chunk_text_):
                snippet += "…"
            hits.append(
                {
                    "chapter": ch,
                    "chapter_title": chapters[ch]["title"] if chapters else "",
                    "page": _page_of(starts, pos) if starts else 0,
                    "ordinal": h["ordinal"],
                    "score": round(float(h.get("score") or 0), 3),
                    "count": chunk_text_.count(q) if q else 0,
                    "snippet": snippet,
                }
            )

    return {
        "ok": True,
        "doc": {
            "id": doc["id"],
            "name": doc["name"],
            "kb_id": kb_id,
            "kb_name": base["name"],
            "byte_size": int(doc.get("byte_size") or 0),
            "chars": len(text),
            "chunk_count": int(doc.get("chunk_count") or 0),
            "embedded": bool(doc.get("embedded")),
            "created_at": int(doc.get("created_at") or 0),
            "chapters": len(chapters),
        },
        # PDF 才有：总页数 / 已索引页数 / 是不是扫描件。前端据此决定显示页面图
        # 还是章节文字，并如实说明"这份没有文字层，只能看图"。
        "pages": {
            "count": int(pages_meta.get("count") or 0),
            "indexed": int(pages_meta.get("indexed") or 0),
            "scanned": bool(pages_meta.get("scanned")),
            "capped": bool(pages_meta.get("capped")),
        }
        if pages_meta
        else None,
        "chapters": chapters,
        "truncated": truncated,
        "hits": hits,
        "query": q,
    }


def delete_document(kb_id: str, doc_id: str) -> bool:
    base = get_base(kb_id)
    if base is None:
        return False
    doc = next((d for d in base["documents"] if d["id"] == doc_id), None)
    if doc is None:
        return False
    base["documents"] = [d for d in base["documents"] if d["id"] != doc_id]
    _bump(base)
    _Index(kb_id).delete_doc(doc_id)
    p = _doc_path(kb_id, doc_id, doc["name"])
    if p.exists():
        p.unlink(missing_ok=True)
    with _PAGE_LOCK:
        _PAGE_CACHE.clear()  # 文件没了，缓存里的图也不能再被取到
    return True


# ── 页面图（PDF 的"预览图"） ──────────────────────────────────────────────
#
# 阅读视图展示的是"这份文件的这一页"，所以图必须在服务端渲染：把原始 PDF 交给
# 浏览器自带的阅读器也行不通 —— 一要浏览器支持内联 PDF，二来那样就绕过了我们
# 自己的命中定位，用户看到的和检索命中的对不上。
#
# 缓存 24 页：翻页是来回的，单页渲染 10~30ms 不算贵，但连续翻十页会卡；
# 24 页 × 约 40KB 完全可以忽略。

_PAGE_CACHE: dict[tuple[str, int, int], bytes] = {}
_PAGE_CACHE_MAX = 24
# 服务是 ThreadingHTTPServer，一屏里六张页面图会同时打进来。没有这把锁的话
# "边遍历边 evict" 会撞出 `RuntimeError: dictionary changed size during
# iteration` —— 表现为随机几张图 503，而单张手动打开又都是好的。
_PAGE_LOCK = threading.Lock()


def page_image(kb_id: str, doc_id: str, page_no: int, *, width: int = 900) -> bytes | None:
    """渲染 PDF 的第 page_no 页为 PNG。文档不存在或无页码表时返回 None。

    `pages.count` 是文件真实页数，不是索引页数 —— 没索引到的页也能翻，
    只是里面搜不到东西。
    """
    base = get_base(kb_id)
    if base is None:
        return None
    doc = next((d for d in base["documents"] if d["id"] == doc_id), None)
    if doc is None or not (doc.get("pages") or {}).get("count"):
        return None
    path = _doc_path(kb_id, doc_id, doc["name"])
    if not path.exists():
        return None

    key = (doc_id, int(page_no), int(width))
    with _PAGE_LOCK:
        hit = _PAGE_CACHE.get(key)
    if hit is not None:
        return hit

    # 渲染放在锁外：一页十几毫秒，串行起来六张图就得排队，而同一个 key 重复
    # 渲染一次的代价比排队小。
    import pdfdoc

    png = pdfdoc.render(path.read_bytes(), int(page_no), width=width)
    with _PAGE_LOCK:
        while len(_PAGE_CACHE) >= _PAGE_CACHE_MAX:
            _PAGE_CACHE.pop(next(iter(_PAGE_CACHE)), None)
        _PAGE_CACHE[key] = png
    return png


# ── 检索 ──────────────────────────────────────────────────────────────────

# 向量余弦相似度召回下限。与 knowledge.py 同一套判断：bge-small-zh 对
# 「短查询 vs 长片段」有个 0.3~0.5 的背景相似度，语义不沾边也落在里面
# （实测「银河系有多少颗恒星」与「海绵城市导则」余弦 0.378，误召回导致
# 问数把「拒答」误判成「命中资料库」）。真命中都在 0.6 以上。定 0.55，
# 低于它不召回，宁可说没有也不硬凑。
#
# 但**字面命中不看这个分**（见 `_literal_hit`）。这里的片段长 800 字，一个三四字
# 的词落在里面会被稀释：实测「双随机」对确实写了这三个字的那一块只有 0.296，
# 「消防通道」0.337，「预付卡」0.408。照 0.55 一刀切，用户搜一个文件里原样写着
# 的词，得到的是「没有相关文件」——这是最难解释的一种错。字面对上就是字面上对，
# 和 knowledge.py 里「编号命中不看阈值」同一条道理：确定的证据比阈值可靠。
MIN_VECTOR_SIM = 0.55

# 字面命中也要有下界：查询太短会把整个库翻出来（搜「水」应该走语义，不该把写了
# 「水」字的每块都列出来），所以 2 个字以上才启用。
# 候选池放宽到 60：字面命中的块排名可能很靠后（那条 0.296 的排第 5），只取 top-k
# 会先被截掉；`_Index.search` 本来就要算遍全库再切片，放宽不增加成本。
_MIN_LITERAL_CHARS = 2
_LITERAL_POOL = 60


def _plain(text: str) -> str:
    """只留字：去掉空白、标点和 markdown 记号。

    切块是纯字符窗口，片段里常带「## 三、分类整治要求」这种标题记号，用户搜
    「分类整治要求」时字面是对的，但原样比对会被「三、」和空格卡住。两边都先
    去干净再比，口径才一致。
    """
    return re.sub(r"[\s\W_]+", "", str(text or "").lower())


def _literal_hit(query: str, text: str) -> bool:
    """问句是否原样落在这段里（去标点空白后逐字比对）。

    刻意不做同义、不做词干：这一路只证明一件事——他说的字，这段里真有。语义
    相关但换了说法的交给向量分，两路各管一段，不互相替。
    """
    q = _plain(query)
    if len(q) < _MIN_LITERAL_CHARS:
        return False
    return q in _plain(text)


def name_hit(filename: str, query: str) -> bool:
    """问句是否落在文件名里。

    单列一路是因为文件名比正文短得多：搜「加装电梯办事指南」，正文里这个词
    会被 800 字窗口稀释到 0.3 上下，而文件名本来就是那个名字。用户打全名时
    要找的就是这份文件，不该让它排在一堆正文命中后面。

    中文文件名常被中间几个字隔开（「加装电梯办事指南」vs 搜「加装电梯指南」），
    逐字 in 会漏，所以整串没对上时再看二字组覆盖率。
    """
    q = _plain(query)
    if len(q) < _MIN_LITERAL_CHARS:
        return False
    name = _plain(filename)
    if q in name:
        return True
    parts = _bigrams(q)
    if not parts:
        return False
    return sum(1 for p in parts if p in name) / len(parts) >= 0.7


def search(
    kb_ids: list[str],
    query: str,
    *,
    k: int = DEFAULT_K,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """跨库检索，返回按相关度降序的原文片段，每条带出处。

    两个信号取并集：向量分够高（语义相关，允许换说法），或者字面命中（他说的
    字这段里原样有）。只留向量分一条路的话，800 字片段会把短查询稀释到 0.3 上下，
    字面上明摆着写着也检不出来。
    """
    q = str(query or "").strip()
    if not q or not kb_ids or k <= 0 or char_budget <= 0:
        return []

    query_vec = _embed([q])
    ranked: list[dict[str, Any]] = []

    for kb_id in kb_ids:
        base = get_base(kb_id)
        if base is None:
            continue
        doc_by_id = {d["id"]: d for d in base["documents"]}
        if query_vec is not None:
            # 候选池放宽：字面命中可能排名很靠后，先取 top-k 会把它们截掉
            hits = _Index(kb_id).search(query_vec[0], k=max(k, _LITERAL_POOL))
            # 向量路设下限，挡掉语义不沾边的背景相似度；字面命中的例外见
            # `_literal_hit` —— 那一路不看分。
            hits = [h for h in hits if h["score"] >= MIN_VECTOR_SIM or _literal_hit(q, h["text"])]
        else:
            hits = _bigram_search(kb_id, q, k=k)
        for h in hits:
            doc = doc_by_id.get(h["doc_id"])
            if doc is None:
                continue
            ranked.append(
                {
                    "kb_id": kb_id,
                    "kb_name": base["name"],
                    "doc_id": h["doc_id"],
                    "doc_name": doc["name"],
                    "ordinal": h["ordinal"],
                    "text": h["text"],
                    "score": h["score"],
                    "method": "vector" if query_vec is not None else "bigram",
                }
            )

    ranked.sort(key=lambda h: h["score"], reverse=True)
    return _trim_to_budget(ranked[:k], char_budget)


def _bigram_search(kb_id: str, query: str, k: int) -> list[dict[str, Any]]:
    q = _bigrams(query)
    if not q:
        return []
    scored = []
    for c in _all_chunks(kb_id):
        doc_bg = _bigrams(c["text"])
        if not doc_bg:
            continue
        inter = q & doc_bg
        if not inter:
            continue
        score = len(inter) / len(q)
        if score > 0:
            scored.append(
                {
                    "doc_id": c["doc_id"],
                    "ordinal": c["ordinal"],
                    "text": c["text"],
                    "score": score,
                }
            )
    scored.sort(key=lambda h: h["score"], reverse=True)
    return scored[:k]


def _trim_to_budget(hits: list[dict[str, Any]], char_budget: int) -> list[dict[str, Any]]:
    out = []
    remaining = char_budget
    for h in hits:
        text = h["text"].strip()[:remaining]
        if not text:
            continue
        out.append({**h, "text": text})
        remaining -= len(text)
        if remaining <= 0:
            break
    return out


def stats() -> dict[str, Any]:
    """给 /api/health 或界面显示的状态。"""
    state = _state()
    bases = state["bases"]
    docs = sum(len(b.get("documents") or []) for b in bases)
    chunks = sum(int(d.get("chunk_count") or 0) for b in bases for d in b.get("documents") or [])
    return {
        "bases": len(bases),
        "documents": docs,
        "chunks": chunks,
        "embedding": embedding_available(),
        "vector_active": _HAVE_EMBED or _embed_model() is not None,
    }
