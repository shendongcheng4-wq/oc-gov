"""运行时数据的 JSON 存储。

oc-gov 刻意不用 SQLite：单机单进程、数据量在几百 KB 量级，JSON 能直接用编辑器
打开、能进 git diff、坏了能手工修。代价是并发写要自己兜住，做法是：

- **全量替换 + tmp 后 rename**：单文件写入是原子的，读到的永远是完整 JSON；
- **每个文件一把可重入锁**：`ThreadingHTTPServer` 是多线程的，读-改-写必须串行，
  否则两个请求同时「读 8 条 → 加 1 条 → 写 9 条」会丢掉一条。

不做跨文件事务。会话和模型配置互不依赖，真需要一致性的地方（比如删除模型时
清理会话里的 model_ref）用单次 update 在同一把锁内完成就够了。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("OCGOV_DATA") or (ROOT / "data"))

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def path_of(name: str) -> Path:
    return DATA_DIR / (name if name.endswith(".json") else f"{name}.json")


def _lock_for(name: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(name)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[name] = lock
        return lock


def read(name: str, default: Any = None) -> Any:
    """读整个文件。文件不存在或内容损坏都返回 default，不抛异常。

    损坏时静默返回 default 是有意的：演示数据坏了不该让服务起不来。但真实业务
    数据不能这样兜——那时候要显式报错，别学这里。
    """
    p = path_of(name)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8") or "null")
    except (json.JSONDecodeError, OSError):
        return default


def write(name: str, data: Any) -> None:
    with _lock_for(name):
        _write_unlocked(name, data)


def _write_unlocked(name: str, data: Any) -> None:
    p = path_of(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)  # POSIX/Windows 同盘 rename 都是原子的


def update(name: str, fn: Callable[[Any], Any], default: Any = None) -> Any:
    """读-改-写，整个过程在同一把锁内。fn 的返回值会被写回。"""
    with _lock_for(name):
        current = read(name, default)
        result = fn(current)
        _write_unlocked(name, result)
        return result
