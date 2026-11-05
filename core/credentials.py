"""身份口令校验：密码加盐哈希，不存明文。

第三批「真登录」的密码层。设计约束：

1. **不存明文**。用 `hashlib.pbkdf2_hmac` + 随机盐，落盘只存盐和哈希。
   标准库就能做，不引 bcrypt/argon2 —— 单机自托管，pbkdf2 已经足够，
   且避免给「标准库就能跑」的定位加一个原生依赖。
2. **代码里没有内置口令**。这个仓库是公开的，写死一个默认口令等于没有口令：
   任何拿到代码的人都能登进市长身份。口令来源只有三条，按优先级：
     1) `OCGOV_PASSWORD` 环境变量 —— 部署时的统一口令。**改了会在下次启动时
        重新派生全部记录**，所以它始终是权威值。
     2) `data/credentials.json` 里已有的记录 —— 首次启动生成、或
        `set_password` 写入的。
     3) 全新安装（前两者都没有）—— 启动时随机生成一次性口令，只在服务端
        控制台打印这一次，落盘仍只存哈希。忘了就删 `data/credentials.json`
        重新生成，或改用环境变量。
3. **按身份存**，不是全局一个口令。将来接真实账号时，这里就是「账号表」的
   雏形 —— 把 `people.PEOPLE` 的 id 换成登录名，校验逻辑不用动。

落盘：`data/credentials.json`，结构
  {"_meta": {"env_fp": "..."},
   "mayor": {"salt": "...", "hash": "...", "algo": "pbkdf2_sha256", "iter": 200000}, ...}

`env_fp` 是环境变量口令的指纹（不存口令本身），用来判断「换了部署口令要重新派生」。

注意：这是「校验口令」的模块，不做 session（那是 auth.py 的事）、不做授权
（authz.py）。`verify(person, password)` 只回答「这个口令对不对」。
"""

from __future__ import annotations

import hashlib
import os
import secrets

import store

STORE_NAME = "credentials"

_ALGO = "pbkdf2_sha256"
_ITER = 200_000

# _meta 是保留键，不参与身份遍历。身份 id 不会以下划线开头。
_META = "_meta"


def _env_password() -> str:
    """环境变量里的统一口令。空串表示没配。"""
    return os.environ.get("OCGOV_PASSWORD", "").strip()


def _fingerprint(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


def _hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITER)
    return dk.hex()


def _record(password: str) -> dict:
    salt = secrets.token_hex(16)
    return {"salt": salt, "hash": _hash(password, salt), "algo": _ALGO, "iter": _ITER}


def _doc() -> dict:
    d = store.read(STORE_NAME, {}) or {}
    return d if isinstance(d, dict) else {}


def _records(doc: dict) -> dict:
    """只取身份记录，滤掉 _meta 这类非记录键。"""
    return {k: v for k, v in doc.items() if isinstance(v, dict) and v.get("salt") and v.get("hash")}


def bootstrap(ids: list[str]) -> str | None:
    """启动时把口令记录对齐到当前配置。返回需要打印的一次性口令（仅全新安装）。

    - 配了 OCGOV_PASSWORD：口令变了就重新派生全部记录（环境变量是权威值）。
    - 没配但有记录：什么都不做，尊重已有的。
    - 都没有：随机生成一个，写哈希，把明文返回给调用方打印一次。
    """
    doc = _doc()
    env = _env_password()

    if env:
        fp = _fingerprint(env)
        meta = doc.get(_META) if isinstance(doc.get(_META), dict) else {}
        if meta.get("env_fp") != fp:
            fresh: dict = {pid: _record(env) for pid in ids}
            fresh[_META] = {"env_fp": fp}
            store.write(STORE_NAME, fresh)
        return None

    if _records(doc):
        return None

    generated = secrets.token_urlsafe(9)
    fresh = {pid: _record(generated) for pid in ids}
    fresh[_META] = {"env_fp": ""}
    store.write(STORE_NAME, fresh)
    return generated


def verify(person: str, password: str) -> bool:
    """校验某人的口令。

    没有记录就是「管理员还没初始化」，一律拒绝 —— 绝不回落到某个内置口令。
    """
    if not password:
        return False
    rec = _records(_doc()).get(person)
    if not rec:
        return False
    salt = rec.get("salt") or ""
    if not salt:
        return False
    # 常量时间比较，避免时序侧信道（单机场景意义有限，但这是标准做法）
    return secrets.compare_digest(_hash(password, salt), rec.get("hash") or "")


def set_password(person: str, password: str) -> bool:
    """设置/重置某人的口令。空口令拒绝。"""
    if not password:
        return False
    doc = _doc()
    doc[person] = _record(password)
    store.write(STORE_NAME, doc)
    return True


def has_records() -> bool:
    """是否已经初始化过口令（服务端据此决定要不要打印一次性口令提示）。"""
    return bool(_records(_doc()))
