"""问句归一：把口语、错字、别称落到已注册指标上。纯规则，不调模型。

解决的是「问句和指标名差一两个字，整条链路就落空」：
    「民生十事完成了几项」     → 民生实事
    「公共预算输入增长多少」   → 一般公共预算收入
这类问句在 `ask.match_metrics` 里精确匹配不到，接着去知识库、资料库，
最后拒答 —— 而用户问的其实就是字典里的指标。

两个出口，用途不同：
- `exact_hits()`   精确命中（别名逐字出现）。`ask.match_metrics` 的实现就调它，
                   「什么算命中」只在这一处定义；否则「优化提问」和「问数」
                   会对同一句话给出不同结论。
- `fuzzy_suggest()` 近似命中，**只产候选**，采不采用由调用方定：置信度够高
                   就直接当命中（并如实注明「按近似理解」），否则摆给用户点。

为什么不用拼音或编辑距离库：别名最短 4 字，difflib 在这个长度上判别力够用，
标准库零依赖。拼音反而会把「实事」和「实施」判成同一个词 —— 在政务口径里
这两个差了整整一个考核项。

不做什么：不改数字、不改期次（那是 polish 的活，且有数字守恒校验）；不做
语义相似（「老百姓喝水的水质」→ 空气质量，规则做不到，交给模型那条路）。
"""

from __future__ import annotations

import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

_METRICS_DIR = Path(__file__).resolve().parent / "metrics"
if str(_METRICS_DIR) not in sys.path:
    sys.path.insert(0, str(_METRICS_DIR))

import mockdata as md  # noqa: E402

# 少于 4 字的别名不做近似匹配：「工业」「招商」「固投」「GDP」这类两字词，
# 差一个字就是另一个意思，容忍 1 个字等于把误命中放进来。
_MIN_ALIAS = 4

# 单个别名最多容忍几个字不同。7 字以上给 2 个字，是因为「规模以上工业增加值」
# 这种长名用户记不全 —— 少两个字仍然只有它一个候选。
_ALIAS_INDEX: list[tuple[str, str]] | None = None


def _alias_index() -> list[tuple[str, str]]:
    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        _ALIAS_INDEX = sorted(
            ((alias, code) for code, meta in md.METRICS.items() for alias in meta["aliases"]),
            key=lambda pair: -len(pair[0]),
        )
    return _ALIAS_INDEX


def _allowed_edits(length: int) -> int:
    return 1 if length <= 6 else 2


def exact_hits(question: str) -> list[tuple[str, str]]:
    """按别名长度从长到短匹配，返回 [(code, 命中的别名)]，一句话可命中多个指标。"""
    q = str(question or "")
    out: list[tuple[str, str]] = []
    consumed: list[str] = []
    for alias, code in _alias_index():
        if alias in q and code not in {c for c, _ in out}:
            # 更长的别名已覆盖的片段不再重复计分
            if any(alias in c for c in consumed):
                continue
            out.append((code, alias))
            consumed.append(alias)
    return out


def normalize_text(text: str) -> str:
    """输入侧归一：全角转半角、压空白、去首尾标点。

    中文输入法下「１—９月」「９月」很常见，而字典里写的是半角；这一步
    在近似匹配之前做，否则「１」和「1」会被当成两个不同的字，平白多一次
    编辑距离。只动字符形态，不改任何语义。
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = " ".join(s.split())
    return s.strip(" \t\r\n。？?！!，,、；;")


def _windows(text: str, size: int) -> list[str]:
    if size <= 0 or len(text) < size:
        return []
    return [text[i : i + size] for i in range(len(text) - size + 1)]


def _best_window(alias: str, question: str) -> tuple[float, str]:
    """在问句里找和别名最像的一段。窗口取 len±1，容忍多一个字或少一个字。

    分数不是裸的 SequenceMatcher：等长窗口错一个字（0.75）会输给「少一个字的
    前缀全中」（0.857），于是「民生项木」被截成「民生项」替换掉，剩下一个
    「木」字 —— 改完比不改更难看。所以乘一个长度系数 min/max，让窗口长度
    和被替换的片段长度也参与打分。
    """
    best_score = 0.0
    best_text = ""
    for size in (len(alias) - 1, len(alias), len(alias) + 1):
        for window in _windows(question, size):
            ratio = SequenceMatcher(None, alias, window).ratio()
            score = ratio * (min(len(alias), size) / max(len(alias), size))
            # 同分时偏好等长窗口：多一个字算「像」，等长才算「写错了一个字」
            if score > best_score + 1e-9 or (
                abs(score - best_score) <= 1e-9 and size == len(alias) and window
            ):
                best_score, best_text = score, window
    return best_score, best_text


def fuzzy_suggest(question: str, limit: int = 3) -> list[dict[str, object]]:
    """近似命中的候选，按置信度降序。

    返回项：{code, name, alias, matched, score}。`matched` 是问句里那段原文，
    用来在界面上说「你说的『民生十事』我理解成『民生实事』」—— 只给结论
    不给依据的话，用户没法判断该不该点。
    """
    q = normalize_text(question)
    if len(q) < _MIN_ALIAS - 1:
        return []
    exact_codes = {code for code, _ in exact_hits(q)}
    best_by_code: dict[str, dict[str, object]] = {}
    for alias, code in _alias_index():
        if len(alias) < _MIN_ALIAS or alias in q or code in exact_codes:
            continue
        ratio, window = _best_window(alias, q)
        if not window or ratio < 0.70:
            continue
        # ratio 换算成「大概改了几个字」：等长时 1-ratio 就是错字占比
        if (1.0 - ratio) * len(alias) > _allowed_edits(len(alias)) + 1e-9:
            continue
        current = best_by_code.get(code)
        if current is None or ratio > float(current["score"]):
            best_by_code[code] = {
                "code": code,
                "name": md.METRICS[code]["name"],
                "alias": alias,
                "matched": window,
                "score": round(ratio, 3),
            }
    items = sorted(
        best_by_code.values(),
        key=lambda item: (-float(item["score"]), -len(str(item["alias"]))),
    )
    return items[:limit]


def is_strong(item: dict[str, object] | None, rivals: list[dict[str, object]] = ()) -> bool:
    """够不够直接当命中用（而不是只摆候选）。

    两条同时满足才认：① 至少 75% 的字对得上；② 比第二名高出 0.08 以上。
    第二条防的是「一句话同时像两个指标」—— 猜一个错的比说没听懂糟得多，
    那种情况应该把候选摆出来让用户点。

    阈值定 0.75 而不是 0.8，是因为 4 字别名错一个字就是 0.75（「民生十事」
    →「民生实事」）。这类别名占了字典的一半，卡在 0.8 等于把它们全部排除，
    而它们恰恰是最容易被写错的（四个字里用户记错一个很正常）。
    """
    if not item:
        return False
    score = float(item.get("score") or 0)
    if score < 0.75:
        return False
    if rivals and float(rivals[0].get("score") or 0) > score - 0.08:
        return False
    return True


def canonicalize(question: str, limit: int = 2) -> tuple[str, list[dict[str, str]]]:
    """把问句里写错的指标名换成字典里的写法，返回 (新问句, 改动清单)。

    只换**高置信度**那一条，换完重算一次再换下一条（最多 `limit` 条）。
    低置信度的不动 —— 规则改写没有「撤销」按钮，改错了用户只会觉得
    「这系统连我的话都听不懂还乱改」。
    """
    q = normalize_text(question)
    changes: list[dict[str, str]] = []
    for _ in range(max(0, limit)):
        candidates = fuzzy_suggest(q, limit=2)
        if not candidates or not is_strong(candidates[0], candidates[1:]):
            break
        best = candidates[0]
        matched = str(best["matched"])
        if not matched or matched not in q:
            break
        q = q.replace(matched, str(best["alias"]), 1)
        changes.append(
            {
                "from": matched,
                "to": str(best["alias"]),
                "code": str(best["code"]),
                "name": str(best["name"]),
            }
        )
    return q, changes
