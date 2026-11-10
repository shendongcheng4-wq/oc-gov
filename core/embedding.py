"""共享的文本向量化模块：把一段中文变成向量，供两条检索链路复用。

为什么单独抽一个模块：`rag.py`（上传文档检索）本来内置了一套 fastembed 加载，
`knowledge.py`（结构化数据检索）也要用向量来堵「通用词虚高」的问题。两处各写一份
`TextEmbedding(...)` 就变成两份要各自维护的依赖逻辑，所以提到这里共用。

## 内网离线是关键约束

fastembed 默认从 HuggingFace 下载模型，内网环境（不联网）会失败。这里做的：

- **优先复用本机已缓存的模型**：依次探测几个常见缓存目录（octop 的
  `embedding_models`、HuggingFace 默认 `~/.cache/huggingface` 等），找到
  `bge-small-zh-v1.5` 的 snapshot 就用 `specific_model_path + local_files_only`
  直接加载，**完全不联网**。
- **没有本地缓存就不加载**（返回 None，调用方降级到 bigram），**刻意不联网下载**。
  模型文件必须由人提前放进缓存目录——宁可退回 bigram，也不在内网静默外联 HuggingFace。
- **加载失败一律返回 None**，调用方降级到 bigram，绝不因此抛异常。

判定在首次调用时缓存（`_HAVE_EMBED`），之后只查布尔，不反复 try。

模型选 `BAAI/bge-small-zh-v1.5`：中文专用、约 90MB、快、小，与 octop 默认一致。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

# fastembed 是否可 import（决定这条路有没有机会走通）。
try:
    from fastembed import TextEmbedding  # noqa: F401

    _HAVE_FASTEMBED = True
except Exception:  # noqa: BLE001
    _HAVE_FASTEMBED = False

_EMBED: Any | None = None
_HAVE_EMBED = False


def embedding_available() -> bool:
    """fastembed 是否可用（注意：可用 ≠ 模型已就绪，模型可能还要下载）。"""
    return _HAVE_FASTEMBED


def _candidate_cache_dirs() -> list[Path]:
    """可能已经缓存了 embedding 模型的地方，按「最可能是本机已有」的顺序排。

    全部是只读探测，找不到就跳过，不创建目录。
    """
    dirs: list[Path] = []
    home = Path.home()

    # octop 用的目录（本机实测存在这份缓存）
    octop = Path(os.environ.get("OCTOP_HOME", "")) if os.environ.get("OCTOP_HOME") else None
    if octop and octop.is_dir():
        dirs.append(octop / "embedding_models")
    # 兜底：octop 默认 data 目录常见落点（D 盘、用户目录）
    for cand in (
        Path("D:/Octop/octop-data/embedding_models"),
        Path("E:/Octop/octop-data/embedding_models"),
        home / ".octop" / "embedding_models",
    ):
        if cand.is_dir():
            dirs.append(cand)

    # HuggingFace 默认缓存（fastembed/huggingface_hub 都会往这放）
    for cand in (
        home / ".cache" / "huggingface",
        Path(os.environ.get("HF_HOME", "")) if os.environ.get("HF_HOME") else None,
        Path(os.environ.get("HF_HUB_CACHE", "")) if os.environ.get("HF_HUB_CACHE") else None,
    ):
        if cand and cand.is_dir():
            dirs.append(cand)

    # 去重保序
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d.resolve()) if d else ""
        if key and key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _find_local_snapshot(cache_dir: Path) -> Path | None:
    """在某个缓存根目录里找 bge-small-zh-v1.5 的 snapshot（含 Qdrant 镜像别名）。"""
    # repo 目录可能直接放在 cache_dir 下（octop 的 embedding_models 就是这种），
    # 也可能在 cache_dir/hub 下 —— huggingface_hub 的标准布局是 HF_HOME/hub/repo，
    # 而 _candidate_cache_dirs() 给的是 HF_HOME 那一层。只探根目录会漏掉后者，
    # 表现就是「模型明明在 ~/.cache/huggingface 里，却一直退回 bigram」。
    for root in (cache_dir, cache_dir / "hub"):
        if not root.is_dir():
            continue
        # 模型 repo 目录有两种命名：官方 id（BAAI/bge-small-zh-v1.5）和 Qdrant 镜像
        # （Qdrant/bge-small-zh-v1.5）。fastembed 下载的是后者，octop 缓存里也是后者。
        for repo_name in ("models--Qdrant--bge-small-zh-v1.5", "models--BAAI--bge-small-zh-v1.5"):
            repo = root / repo_name
            snapshots = repo / "snapshots"
            if not snapshots.is_dir():
                continue
            # snapshot 里只要有 .onnx 权重 + tokenizer 就算可用
            for snap in snapshots.iterdir():
                if not snap.is_dir():
                    continue
                has_onnx = any(snap.rglob("*.onnx"))
                if has_onnx:
                    return snap
    return None


def _build_embedder() -> Any | None:
    """构造一个 TextEmbedding 实例，优先离线复用本地缓存。失败返回 None。"""
    if not _HAVE_FASTEMBED:
        return None
    try:
        from fastembed import TextEmbedding
    except Exception:  # noqa: BLE001
        return None

    # 先找本地缓存，命中就完全离线加载
    for cache_dir in _candidate_cache_dirs():
        snap = _find_local_snapshot(cache_dir)
        if snap is not None:
            try:
                return TextEmbedding(
                    model_name=_MODEL_NAME,
                    cache_dir=str(cache_dir),
                    specific_model_path=str(snap),
                    local_files_only=True,
                    threads=2,
                )
            except Exception:  # noqa: BLE001 —— 这份缓存坏了就试下一处
                continue

    # 没有本地缓存 → 返回 None，调用方降级到 bigram。
    # 刻意不走 fastembed 的默认下载路径：那会在内网环境静默外联 HuggingFace，
    # 违反「不联网也能跑」的约束。模型文件必须由人提前放进缓存目录。
    return None


def _embed_model() -> Any | None:
    """懒加载 embedding 模型。失败返回 None（调用方降级）。"""
    global _EMBED, _HAVE_EMBED
    if _HAVE_EMBED:
        return _EMBED
    if not _HAVE_FASTEMBED:
        return None
    _EMBED = _build_embedder()
    _HAVE_EMBED = _EMBED is not None
    return _EMBED


def embed(texts: list[str]) -> list[list[float]] | None:
    """把一串文本变成向量。不可用返回 None。"""
    if not texts:
        return []
    model = _embed_model()
    if model is None:
        return None
    try:
        vecs = list(model.embed(texts))
        return [list(v) for v in vecs]
    except Exception:  # noqa: BLE001
        return None
