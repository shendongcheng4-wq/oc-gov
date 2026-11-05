"""身份口令校验：密码加盐哈希，不存明文。

第三批「真登录」的密码层。设计约束：

1. **不存明文**。用 `hashlib.pbkdf2_hmac` + 随机盐，落盘只存盐和哈希。
   标准库就能做，不引 bcrypt/argon2 —— 单机自托管，pbkdf2 已经足够，
   且避免给「标准库就能跑」的定位加一个原生依赖。
2. **默认口令可配置、必须改**。口令来源优先级：
   `OCGOV_PASSWORD` 环境变量（统一口令）> 默认口令 `ocgov2026`。
   单机场景没有 LDAP/SSO，口令本质是管理员自设的本地口令；代码里留默认值
   只是为了「开箱能进」，README 和登录界面都要提示改成自己的。
3. **按身份存**，不是全局一个口令。将来接真实账号时，这里就是「账号表」的
   雏形 —— 把 `people.PEOPLE` 的 id 换成登录名，校验逻辑不用动。

落盘：`data/credentials.json`，结构
  {"mayor": {"salt": "...", "hash": "...", "algo": "pbkdf2_sha256", "iter": 200000}, ...}

注意：这是「校验口令」的模块，不做 session（那是 auth.py 的事）、不做授权
（authz.py）。`verify(person, password)` 只回答「这个口令对不对」。
"""

from __future__ import annotations

import hashlib
import os
import secrets

import store

STORE_NAME = "credentials"

# 默认口令。单机自托管，管理员第一次进去后应改成自己的（通过 OCCOV_PASSWORD
# 环境变量或直接改 data/credentials.json）。写在这里是为了「开箱能进」，
# 不是安全措施。
DEFAULT_PASSWORD = "ocgov2026"

# 环境变量里配的统一口令，优先级高于 DEFAULT_PASSWORD。
_ENV_PASSWORD = os.environ.get("OCGOV_PASSWORD", "").strip()

_ALGO = "pbkdf2_sha256"
_ITER = 200_000


def _effective_password() -> str:
    """当前生效的口令。环境变量 > 默认。"""
    return _ENV_PASSWORD or DEFAULT_PASSWORD


def _hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITER)
    return dk.hex()


def _doc() -> dict:
    d = store.read(STORE_NAME, {}) or {}
    return d if isinstance(d, dict) else {}


def ensure(person: str) -> dict | None:
    """确保某人有口令记录，没有就用当前生效口令生成一份。返回记录 dict。"""
    doc = _doc()
    rec = doc.get(person)
    if isinstance(rec, dict) and rec.get("hash") and rec.get("salt"):
        return rec
    salt = secrets.token_hex(16)
    rec = {"salt": salt, "hash": _hash(_effective_password(), salt), "algo": _ALGO, "iter": _ITER}
    doc[person] = rec
    store.write(STORE_NAME, doc)
    return rec


def verify(person: str, password: str) -> bool:
    """校验某人的口令。没有记录时先用当前生效口令补一份再验。"""
    if not password:
        return False
    rec = ensure(person)
    salt = rec.get("salt") or ""
    if not salt:
        return False
    # 常量时间比较，避免时序侧信道（单机场景意义有限，但这是标准做法）
    expected = rec.get("hash") or ""
    actual = _hash(password, salt)
    return secrets.compare_digest(actual, expected)


def set_password(person: str, password: str) -> bool:
    """设置/重置某人的口令。空口令拒绝。"""
    if not password:
        return False
    salt = secrets.token_hex(16)
    doc = _doc()
    doc[person] = {"salt": salt, "hash": _hash(password, salt), "algo": _ALGO, "iter": _ITER}
    store.write(STORE_NAME, doc)
    return True
