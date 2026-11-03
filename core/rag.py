"""通用文档 RAG：把上传的文件变成可检索的原文片段。

与 core/knowledge.py 的分工：
- knowledge.py 检索的是**系统内部已有的结构化数据**（指标口径、督办台账、会议记录），
  用 bigram + 术语加权，零依赖，返回的每条都带出处。
- 本模块检索的是**用户上传的任意文档**（政策文件、报告、汇报材料），落盘成
  知识库，切块建索引，问句来了按相关度召回原文片段。

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

import hashlib
import math
import re
import secrets
import sqlite3
import struct
from pathlib import Path
from typing import Any

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
# 优先 fastembed（自带 ONNX 运行时 + 自动下载中文 embedding 模型），
# 次选裸 onnxruntime。都没有就 _HAVE_EMBED = False，检索退回 bigram。
try:
    from fastembed import TextEmbedding  # noqa: F401

    _HAVE_FASTEMBED = True
except Exception:  # noqa: BLE001
    _HAVE_FASTEMBED = False

_EMBED = None
_HAVE_EMBED = False


def _embed_model():
    """懒加载一个中文 embedding 模型。失败返回 None（调用方降级）。"""
    global _EMBED, _HAVE_EMBED
    if _HAVE_EMBED:
        return _EMBED
    if not _HAVE_FASTEMBED:
        return None
    try:
        from fastembed import TextEmbedding

        _EMBED = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5")
        _HAVE_EMBED = True
        return _EMBED
    except Exception:  # noqa: BLE001 —— 下载失败/网络不通都当「没有向量化」
        return None


def embedding_available() -> bool:
    return _HAVE_FASTEMBED


def _embed(texts: list[str]) -> list[list[float]] | None:
    """把一串文本变成向量。不可用返回 None。"""
    model = _embed_model()
    if model is None:
        return None
    try:
        vecs = list(model.embed(texts))
        return [list(v) for v in vecs]
    except Exception:  # noqa: BLE001
        return None


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
        raise ValueError("知识库名称不能为空")
    state = _state()
    if len(state["bases"]) >= MAX_BASES:
        raise ValueError(f"最多创建 {MAX_BASES} 个知识库")
    if any(b["name"] == name for b in state["bases"]):
        raise ValueError(f"已存在同名知识库「{name}」")
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
    """解析文件为纯文本，返回 (text, error)。"""
    if not data:
        return "", "文件是空的"
    # 纯文本后缀直接按文本读，其余交给 docparse（支持 xlsx/docx）
    low = (name or "").lower()
    if low.endswith((".md", ".txt", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".html", ".htm")):
        text = _read_text_bytes(data)
        if text.strip():
            return text, ""
        return "", "文件里没有文字"
    import docparse

    out = docparse.parse_book(data, name, limit=_MAX_PREVIEW_CHARS)
    if out.get("ok"):
        return out["text"], ""
    return "", out.get("error") or "解析失败"


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
        raise LookupError("知识库不存在")
    if len(base["documents"]) >= MAX_DOCS_PER_BASE:
        raise ValueError(f"一个知识库最多 {MAX_DOCS_PER_BASE} 个文档")
    if len(content) > MAX_DOC_BYTES:
        raise ValueError(f"文件超过 {MAX_DOC_BYTES // (1024 * 1024)}MB")

    name = str(filename or "未命名").strip()[:120] or "未命名"
    text, err = _parse_bytes(content, name)
    if err:
        raise ValueError(f"{name}：{err}")

    chunks = chunk_text(text)
    if not chunks:
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
    base["documents"].append(doc)
    _bump(base)
    return doc


def list_documents(kb_id: str) -> list[dict[str, Any]]:
    base = get_base(kb_id)
    if base is None:
        return []
    return list(base.get("documents") or [])


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
    return True


# ── 检索 ──────────────────────────────────────────────────────────────────


def search(
    kb_ids: list[str],
    query: str,
    *,
    k: int = DEFAULT_K,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """跨库检索，返回按相关度降序的原文片段，每条带出处。"""
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
            hits = _Index(kb_id).search(query_vec[0], k=k)
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
