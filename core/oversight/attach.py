"""佐证材料原件仓库：一份文件上一次，之后按 (事项编号, id) 取。

## 为什么要有这个模块

在这之前，佐证材料在系统里是一串**手打的文件名**：前端一个输入框「逗号分隔」，
后端只判 `if not evidence`，事件流里存下 `["销号台账.xlsx"]`，详情页显示
「佐证 2 份」四个字。于是：

- 领导确认办结时想核对，点开来什么都没有 —— 佐证成了一句自我声明；
- 同名的两份不同版本无法区分，改了数再报一次，看起来一模一样；
- 台账可以声称「附了现场照片」，而系统里没有任何一张照片。

督办的价值在于「可验证」。所以这里把原件真正收下来。

## 为什么不复用现成的两处

- **共享资料库（rag）**：那是有向量索引的知识库，写材料时会被检索并作为
  政策依据摆到纸面旁边。佐证是现场照片、销号台账、回执扫描件，进库就会在
  「依据」里冒出来，把真正的政策原文挤掉。而且它按库分，不按事项分。
- **会话材料（materials）**：只存纯文本（`*_txt`），二进制读不回来；
  且按 owner 隔离，换个人审核就取不到。

所以单开一个仓库：按事项分目录，只存原件 + 一份清单，**不解析、不建索引**。
审核的人要的是「这张照片拍的是什么」，机器抽出来的文字反而多一层要信任的中间物。

## 边界

- **只收常见办公与现场材料格式**：白名单在 `ALLOWED_EXT`。可执行文件一律拒收 ——
  这些文件之后会被浏览器下载回本机，没必要给台账留一条可执行的投递通道。
- **同一事项内按 sha1 去重**：同一张照片传两遍只留一份，事件引用同一个 id。
- **删了就真没了**：删除只允许针对「还没被任何事件引用」的文件，判定在
  `oversight.remove_evidence()`。
- **不留历史版本**：这是一份报送材料，不是文档管理系统。要换材料就删了重传。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("OCGOV_DATA") or (Path(__file__).resolve().parents[2] / "data"))
BLOB_ROOT = DATA_DIR / "evidence"
INDEX_NAME = "_index.json"

# 单个文件上限。和 app/main.py 的 `_MAX_UPLOAD_FILE` 对齐：那边是请求体解码前的
# 判断，这里是解码后的第二道。两处数字不一致会出现「前端说超了、后端说没超」。
MAX_BYTES = 9 * 1024 * 1024
# 一条事项最多收多少份。不是技术限制，是为了让「佐证」保持可读：
# 一次办结附 200 个文件，审核的人一个都不会点。
MAX_PER_ITEM = 40

# 白名单。分四类：图片（现场照）、公文类（台账/回执/报告）、压缩包（批量扫描件）、
# 影音（现场核查录像、录音）。不在表里的一律拒，报错时把表列出来 ——
# 「不支持的类型」不告诉人支持什么，等于让人试。
ALLOWED_EXT: dict[str, str] = {
    # 图片：现场核查照片是最常见的佐证
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    ".heic": "image/heic", ".heif": "image/heif",
    # 公文与台账
    ".pdf": "application/pdf", ".ofd": "application/ofd",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".wps": "application/kswps", ".et": "application/kset", ".dps": "application/ksdps",
    ".txt": "text/plain", ".csv": "text/csv", ".md": "text/markdown",
    # 打包的扫描件
    ".zip": "application/zip", ".rar": "application/vnd.rar", ".7z": "application/x-7z-compressed",
    # 影音
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".wmv": "video/x-ms-wmv", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
}

# 每个事项目录一把锁。清单是「读-改-写」，多线程同时传两个文件会丢一个。
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _lock_for(item_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(item_id)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[item_id] = lock
        return lock


def _dir_of(item_id: str) -> Path:
    """事项目录。item_id 来自台账（DB2026-0030），但这里是文件系统边界，
    仍然要挡一次路径穿越：`../` 不该因为调用方换了就变成可写目录。"""
    if not _SAFE_ID.match(str(item_id or "")):
        raise ValueError(f"事项编号不合法：{item_id!r}")
    return BLOB_ROOT / item_id


def clean_name(raw: Any) -> str:
    """只留文件名。浏览器不会传整条路径，脚本会（`C:/Users/...`）——
    存进清单就变成一屏放不下的标题，而且在「另存为」时会把用户带到一个怪路径。"""
    s = str(raw or "").replace("\\", "/")
    s = s.rsplit("/", 1)[-1]
    s = _BAD_CHARS.sub("_", s).strip().strip(".")
    return (s[:120] or "未命名")


def _suffix(name: str) -> str:
    return Path(name).suffix.lower()


def _load_index(item_id: str) -> list[dict[str, Any]]:
    path = _dir_of(item_id) / INDEX_NAME
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "[]")
    except (json.JSONDecodeError, OSError):
        # 清单坏了不要连带把已有的文件判死：目录里还在的文件仍然能按 id 取，
        # 只是列表里看不到。重建清单的成本远高于「少显示一条」。
        return []
    return [r for r in raw if isinstance(r, dict) and r.get("id")]


def _save_index(item_id: str, rows: list[dict[str, Any]]) -> None:
    d = _dir_of(item_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / INDEX_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _file_of(item_id: str, meta: dict[str, Any]) -> Path:
    return _dir_of(item_id) / f"{meta['id']}{_suffix(str(meta.get('name') or ''))}"


def put(
    item_id: str,
    filename: str,
    content: bytes,
    *,
    by: str = "",
    at: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """收一份原件。成功返回 (清单项, "")，失败返回 (None, 原因)。

    去重按 (事项, sha1)：同一份文件重复上传只留一条，返回已有的那条并带上
    `dedup=True`。前端据此提示「这份已经在里面了」，而不是默默多出一条一样的。
    """
    name = clean_name(filename)
    suffix = _suffix(name)
    if suffix not in ALLOWED_EXT:
        kinds = "图片 / PDF·OFD / Word·Excel·PPT / 压缩包 / 音视频"
        return None, f"{name}：不支持的类型（{suffix or '无扩展名'}）。可以传：{kinds}。"
    if not content:
        return None, f"{name}：文件是空的。"
    if len(content) > MAX_BYTES:
        return None, f"{name}：超过 {MAX_BYTES // (1024 * 1024)}MB。"

    digest = hashlib.sha1(content).hexdigest()
    d = _dir_of(item_id)
    with _lock_for(item_id):
        rows = _load_index(item_id)
        for old in rows:
            if old.get("sha1") == digest:
                return {**old, "dedup": True}, ""
        if len(rows) >= MAX_PER_ITEM:
            return None, f"这条事项已有 {len(rows)} 份佐证，超过上限 {MAX_PER_ITEM} 份。"

        eid = "ev_" + secrets.token_hex(6)
        meta: dict[str, Any] = {
            "id": eid,
            "name": name,
            "size": len(content),
            "sha1": digest,
            "type": ALLOWED_EXT[suffix],
            "at": at,
            "by": by,
        }
        d.mkdir(parents=True, exist_ok=True)
        _file_of(item_id, meta).write_bytes(content)
        rows.append(meta)
        _save_index(item_id, rows)
    return meta, ""


def list_of(item_id: str) -> list[dict[str, Any]]:
    """这条事项收过的全部原件，新→旧。"""
    try:
        rows = _load_index(item_id)
    except ValueError:
        return []
    return sorted(rows, key=lambda r: str(r.get("at") or ""), reverse=True)


def get(item_id: str, eid: str) -> tuple[dict[str, Any], Path] | None:
    """按 id 取 (清单项, 文件)。文件被外部删掉时返回 None ——
    清单里还挂着、盘上没有，这种「看着有其实没有」必须当场暴露，
    不能返回一条空记录让下载变成 0 字节。"""
    try:
        rows = _load_index(item_id)
    except ValueError:
        return None
    for row in rows:
        if row.get("id") == eid:
            path = _file_of(item_id, row)
            if not path.exists():
                return None
            return row, path
    return None


def exists(item_id: str, eid: str) -> bool:
    return get(item_id, eid) is not None


def remove(item_id: str, eid: str) -> bool:
    """删一份原件。调用方必须先确认它没有被事件引用过（`oversight.remove_evidence`）。"""
    with _lock_for(item_id):
        try:
            rows = _load_index(item_id)
        except ValueError:
            return False
        keep = [r for r in rows if r.get("id") != eid]
        if len(keep) == len(rows):
            return False
        meta = next(r for r in rows if r.get("id") == eid)
        try:
            _file_of(item_id, meta).unlink(missing_ok=True)
        except OSError:
            pass  # 文件已经不在了，清单条目照删 —— 结果是「本来就没有」
        _save_index(item_id, keep)
    return True


def describe(item_id: str, entries: list[Any] | None) -> list[dict[str, Any]]:
    """事件里存的佐证条目 → 前端可渲染的列表。

    事件流里可能混着三种东西：
    - 上传过的：`{"id": "ev_x", "name": "...", "size": ...}`；
    - 老数据 / 手填的：字符串，只有名字（`note_only`）—— 之前那个输入框的遗留；
    - 上传过但后来被删的：标注 `missing`，让人知道「这里原本有份文件」。
      静默丢掉会让旧事件看起来像没附佐证，那是改写历史。

    清单只读一次：一条事项可能挂着几十份文件，每条事件各读一遍 index.json
    会让「打开详情」变成几十次磁盘读 —— 而它已经是最常走的路径之一。
    """
    try:
        index = {str(r.get("id") or ""): r for r in _load_index(item_id)}
    except ValueError:
        index = {}
    out: list[dict[str, Any]] = []
    for raw in entries or []:
        if isinstance(raw, str):
            name = clean_name(raw)
            if name:
                out.append({"id": "", "name": name, "size": 0, "note_only": True, "missing": False})
            continue
        if not isinstance(raw, dict):
            continue
        eid = str(raw.get("id") or "")
        if not eid:
            out.append(
                {
                    "id": "",
                    "name": clean_name(raw.get("name")),
                    "size": int(raw.get("size") or 0),
                    "note_only": True,
                    "missing": False,
                }
            )
            continue
        meta = index.get(eid)
        # 清单里有还不够：盘上文件可能被外部删了。返回「有 id 但 missing」
        # 比返回一条空记录更诚实 —— 后者会让下载变成 0 字节。
        if meta is None or not _file_of(item_id, meta).exists():
            out.append(
                {
                    "id": eid,
                    "name": clean_name(raw.get("name") or (meta or {}).get("name")),
                    "size": int(raw.get("size") or (meta or {}).get("size") or 0),
                    "note_only": False,
                    "missing": True,
                }
            )
            continue
        out.append(
            {
                "id": eid,
                "name": meta["name"],
                "size": int(meta.get("size") or 0),
                "type": meta.get("type") or "",
                "note_only": False,
                "missing": False,
            }
        )
    return out
