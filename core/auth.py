"""会话认证：后端签发的 session，可即时吊销。

借鉴 Octop `infra/auth/tokens.py` 的载荷结构（sub / uname / role），但不引
pyjwt —— oc-gov 是标准库单机服务，用 `secrets` + `hmac` 自己做等价物。

用户已拍板选「session 方案」而非无状态 token：政务要的是「说停就停」——
session id 存在 `data/sessions.json`，删掉（或调 `revoke`）当场失效，
不用等 token 自己过期。这是和 Octop 的关键差别：Octop 的 JWT 签出去收不回，
政务场景要能立刻把离岗/调岗的人踢出去。

session 载荷存什么：
  sid      随机会话 id（发出去的凭据）
  person   身份 id（people.PEOPLE 里的 mayor / vmayor / bureau）
  role     授权角色（leader / dept-leader / bureau，由 directory 映射）
  units    分管单位（进载荷，授权判定就不必每请求查 people）
  created/expires  时间戳

发出去的凭据只有 sid，person/role/units 都是服务端算好、按 sid 查回的，
前端永远改不了自己的身份。这点和现在「X-Person-Id 头随便传」有本质区别。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import directory
import store

STORE_NAME = "sessions"

# 会话有效期（秒）。政务偏短：默认 8 小时，一个工作日。
DEFAULT_TTL = 8 * 3600

_LOCK = threading.Lock()


def _doc() -> dict[str, Any]:
    d = store.read(STORE_NAME, {}) or {}
    return d if isinstance(d, dict) else {}


def _save(doc: dict[str, Any]) -> None:
    store.write(STORE_NAME, doc)


def _now() -> int:
    return int(time.time())


def login(person: str, password: str = "", ttl_seconds: int = DEFAULT_TTL) -> dict[str, Any] | None:
    """给某人签发一个 session。person 必须是白名单身份、口令必须正确，否则 None。

    `password` 校验走 credentials.verify：空口令（没传）一律拒绝 —— 登录必须
    凭口令，不能再「选个身份就进」。这是第三批从「演示身份」到「口令登录」的分界。

    返回 {sid, person, role, units, expires}。sid 是发出去的凭据。
    """
    import people  # noqa: PLC0415  # 延迟导入，避免 auth ↔ people 成环
    import credentials  # noqa: PLC0415

    if people.find(person) is None:
        return None
    if not credentials.verify(person, password):
        return None

    sid = secrets.token_urlsafe(32)
    scope = directory.person_scope(person)
    session = {
        "sid": sid,
        "person": person,
        "role": scope["role"],
        "units": list(scope["units"]),
        "created": _now(),
        "expires": _now() + ttl_seconds,
    }
    with _LOCK:
        doc = _doc()
        sessions = doc.get("sessions") if isinstance(doc.get("sessions"), dict) else {}
        sessions[sid] = session
        # 顺手清掉过期会话，避免文件无限膨胀
        sessions = {k: v for k, v in sessions.items() if _session_alive(v)}
        doc["sessions"] = sessions
        _save(doc)
    return session


def _session_alive(s: Any) -> bool:
    return isinstance(s, dict) and int(s.get("expires", 0)) > _now()


def resolve(sid: str | None) -> dict[str, Any] | None:
    """按 sid 查回会话。过期/不存在/被吊销都返回 None。

    返回的是 session dict（含 person/role/units），调用方拿它喂
    directory / authz。不返回任何「未知身份回落」的假身份——没会话就是没会话。
    """
    if not sid:
        return None
    doc = _doc()
    sessions = doc.get("sessions") if isinstance(doc.get("sessions"), dict) else {}
    s = sessions.get(sid)
    if s is None or not _session_alive(s):
        return None
    return dict(s)


def revoke(sid: str | None) -> bool:
    """吊销一个会话（踢人）。不存在返回 False。"""
    if not sid:
        return False
    with _LOCK:
        doc = _doc()
        sessions = doc.get("sessions") if isinstance(doc.get("sessions"), dict) else {}
        if sid not in sessions:
            return False
        del sessions[sid]
        doc["sessions"] = sessions
        _save(doc)
    return True


def revoke_person(person: str) -> int:
    """吊销某人的全部会话（调岗/离岗时踢掉所有登录）。返回吊销数量。"""
    with _LOCK:
        doc = _doc()
        sessions = doc.get("sessions") if isinstance(doc.get("sessions"), dict) else {}
        doomed = [k for k, v in sessions.items() if v.get("person") == person]
        for k in doomed:
            del sessions[k]
        doc["sessions"] = sessions
        _save(doc)
        return len(doomed)
