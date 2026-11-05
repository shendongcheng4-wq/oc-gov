"""审计日志：append-only JSONL。

借鉴 Octop `db/repos/audit.py` 的 `audit_log(ts, actor, action, target, payload)`
五字段结构，但 oc-gov 无数据库，落成 `data/audit.jsonl`（每行一条 JSON，追加写）。

记录原则（用户已拍板）：
- **写操作全记**，包括被拒绝的（拒绝也要记，否则看不到越权尝试）。
- **正常读不记**（不记每次罗盘刷新）。
- **越权尝试必记**（action 用 `access.denied`）。

actor 用 Octop 的约定：`_system`（定时任务）、`_admin`（超管/系统操作）、
登录用户名（这里就是 person id）。字段：
  ts      ISO 时间
  actor   谁（person id / _system / _admin）
  action  点分层动作词表（auth.login / oversight.sign / access.denied ...）
  target  资源标识（事项 id、会议 id、指标 code 等），可为空
  payload 附加上下文（拒绝原因、替谁签收等），JSON 字符串，可为空

append 用单文件追加 + 一把锁，不追求跨进程原子（单机单进程场景足够）。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("OCGOV_DATA") or (ROOT / "data"))
AUDIT_FILE = DATA_DIR / "audit.jsonl"

_LOCK = threading.Lock()

ACTOR_SYSTEM = "_system"
ACTOR_ADMIN = "_admin"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record(
    actor: str | None,
    action: str,
    target: str | None = None,
    payload: Any = None,
) -> None:
    """追加一条审计。任何异常都不该打断业务（审计失败不能挡住写操作本身）。"""
    row = {
        "ts": _now(),
        "actor": actor or "",
        "action": action,
        "target": target or "",
        "payload": _payload_text(payload),
    }
    try:
        with _LOCK:
            AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        # 审计是旁路，不能因为它写不进盘就把一次合法的写操作搞失败。
        pass


def _payload_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)


# ── 便捷封装（对齐 Octop 的 system_event / admin_event / user_event）──
def system_event(action: str, target: str | None = None, payload: Any = None) -> None:
    record(ACTOR_SYSTEM, action, target, payload)


def admin_event(action: str, target: str | None = None, payload: Any = None) -> None:
    record(ACTOR_ADMIN, action, target, payload)


def user_event(actor: str, action: str, target: str | None = None, payload: Any = None) -> None:
    record(actor, action, target, payload)


def tail(limit: int = 100, *, actor: str | None = None, action: str | None = None) -> list[dict[str, Any]]:
    """读最近 N 条，可按 actor / action 过滤。给审计面板用。

    JSONL 无 SQL，按行扫、取满足条件的最后 limit 条。审计文件通常不大，
    全量扫没问题。
    """
    if not AUDIT_FILE.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with AUDIT_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if actor and row.get("actor") != actor:
                    continue
                if action and row.get("action") != action:
                    continue
                out.append(row)
    except OSError:
        return []
    return out[-limit:]
