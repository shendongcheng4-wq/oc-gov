"""数值闸：让数字过模型，但不让数字失控。

原来的规矩是「参谋一个数字都不许说」。代价很明显 —— 它看不见 8% 还是 2%，
只能判断方向，于是只能说「偏高、偏低、要盯住」，说了三句等于没说。

把数值放给它是一步，但放开之后必须换一道更硬的锁：**每个数字都要能追到来源**。

三种来源，只有前两种放行：

1. 接口给的数（现值、同比、环比、目标）—— 直接放行；
2. 由这些数算出来的数 —— 必须把算式写进正文（`1024.8 - 882.6 = 142.2`），
   代码验等式成立、操作数也都在放行集合里，才算数；
3. 其他 —— 判为编造，整段不可用，调用方退回模板 / 逐句剔除。

**截止日不算报数**：`12月31日前完成` 是对将来的时限，不是对已发生事实的引用，
追不到出处也放行；`8月11日披露` 这种「已发生」的日期照样要溯源。

**为什么要求写算式**：模型算错时最难查，因为结果看起来像模像样。让它把推导
过程交出来，等式不成立立刻就能看出来，用户在界面上也能照着核。这比事后抽查
可靠得多，也是「数值过模型」还能保住可核性的唯一办法。

**期次里的数字不拆开放行**：`2026年1-7月` 当作一个字面量整段抹掉再检查，
而不是把 2026、1、7 放进白名单。否则「1 个百分点」这种编造就借着期次里的
那个 1 混过去了。同理，模型要是把「1-7月」写成「7月」，剩下的 7 无可追溯，
会被判坏 —— 这恰好是我们要拦的那类错。

容差留 0.05 或千分之二：模型常做四舍五入（142.19 / 142.2），卡太死会把
正确结果判死，等于逼它不要算。
"""

from __future__ import annotations

import re
from typing import Any

# 数字片段：支持千分位（接口格式化后是「1,024.8 亿元」），符号不参与溯源
# 全角逗号也要认：模型复制「1，024.8」很常见，拆成 1 和 024.8 就会误判。
_NUM = re.compile(r"\d{1,3}(?:[，,]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
# 算式：a - b = c，容忍全角等号、乘除号、两侧空格
_EQ = re.compile(
    r"(-?\d[\d,，]*(?:\.\d+)?\s*[%％]?)\s*([-+*/×÷])\s*"
    r"(-?\d[\d,，]*(?:\.\d+)?\s*[%％]?)\s*[=＝]\s*(-?\d[\d,，]*(?:\.\d+)?)"
)
# 一个字符串算不算「纯数值」：8.9 / +5.4% / 1,024.8 亿元 / 45 项 / 6.5%
_PURE = re.compile(
    r"^\s*[+-]?[\d,，]+(?:\.\d+)?\s*(?:%|％|pct|个百分点|亿元|万元|亿|万|个|项|人|天|家|件)?\s*$"
)


def _to_float(text: str) -> float:
    # 去掉千分位、百分号和空格：算式里的操作数常写成「8% - 6.5%」
    return float(re.sub(r"[,，%％\s]", "", text))


def _apply(a: float, op: str, b: float) -> float | None:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op in ("*", "×"):
        return a * b
    if op in ("/", "÷"):
        return None if b == 0 else a / b
    return None


def _close(x: float, y: float) -> bool:
    return abs(x - y) <= max(0.05, abs(y) * 0.002)


def _walk(obj: Any, numbers: set[float], literals: list[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        numbers.add(float(obj))
        return
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return
        if _PURE.match(text):
            for m in _NUM.finditer(text):
                numbers.add(float(m.group(0)))
        elif len(text) >= 2:
            # 含中文/字母的字符串（期次、指标名、部门、状态）整体当字面量，
            # 不拆数字。拆了就等于把 1、7、2026 全放进白名单。
            literals.append(text)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            # sources 要算进来：披露日期（「8月10日披露」）是引擎给的出处信息，
            # 模型引用它是在交代来源，不是报数。把它排除掉会逼模型不敢说出处。
            if key in ("trace", "raw"):
                continue
            if key in ("value", "target_note", "period_note") and isinstance(value, str):
                # value / target_note 是引擎算好的数值文本（「1,024.8 亿元」
                # 「高于年度目标 1.5 pct」）。它们天然含中文单位和修饰，不能按
                # 字面量处理，否则模型只说「1.5 个百分点」就成了无出处 ——
                # 而那正是引擎算给它的数。
                for match in _NUM.finditer(value):
                    numbers.add(_to_float(match.group(0)))
                literals.append(value.strip())
                continue
            if key == "notes" and isinstance(value, (list, tuple)):
                # 口径说明里的数字（「年主营业务收入 2000 万元及以上」）是官方口径的
                # 组成部分，模型转述它是在说清口径，不是报数。
                for item in value:
                    if isinstance(item, str):
                        for match in _NUM.finditer(item):
                            numbers.add(_to_float(match.group(0)))
                        literals.append(item.strip())
                continue
            _walk(value, numbers, literals, depth + 1)
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _walk(item, numbers, literals, depth + 1)


def facts_of(payload: Any) -> tuple[set[float], list[str]]:
    """从一个结果 payload 里抽出（可放行的数，需整段抹掉的字面量）。"""
    numbers: set[float] = set()
    literals: list[str] = []
    _walk(payload, numbers, literals)
    return numbers, literals


# 期次片段：2026年 / 2026年7月 / 7月。用户问句里的月份要能放行 ——
# 他问「7月份」，参谋回「7月当月」是在接话，不是编期次。
_PERIOD_PIECE = re.compile(r"\d{4}年(?:\d{1,2}月)?|\d{1,2}月")
# 期次区间：1-7月 / 1—7月 / 1至7月。口径标签写成「2026年1—7月累计」时，
# 模型通常只说「1—7月」，不带上年份和「累计」，得单独认出来整段抹掉。
_PERIOD_RANGE = re.compile(r"\d{1,2}\s*[－—–~～至\u2010\u2011\u2212-]\s*\d{1,2}\s*月")
# 模型还常把「1-7月」写成「1-7」（省略「月」），例如「与1-7的口径不同」。
# 不带月份的区间也要认，否则残留的 1 会把整段正确解读判死。
_PERIOD_BARE = re.compile(r"\d{1,2}\s*[－—–~～至\u2010\u2011\u2212-]\s*\d{1,2}(?!\d)")
# 截止日：12月31日前 / 本月底前。这是对**将来**的时限，不是报一个已发生的事实，
# 追不到出处也不该拦 —— 建议里「什么时候之前办完」正是最该留下的部分。
# 只认带「前/截止」的写法：「8月11日披露」这种对已发生事实的引用照样要溯源。
_DEADLINE = re.compile(
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]\s*(?:之前|以前|前|截止)"
    r"|\d{1,2}\s*月底\s*(?:之前|以前|前|截止)"
    r"|(?:今年|本年|年底|年内)\s*(?:之前|以前|前|底前)"
)


def _norm_period(text: str) -> str:
    """期次写法归一：连接符统一、数字与「月/年/季」之间的空格去掉。

    模型经常写「1—7 月」「1 - 7月」，和标签里的「1—7月」字面不同，不归一就
    永远匹配不上，1 和 7 会被当成裸露数字，整段正确解读被误杀。
    """
    text = re.sub(r"[－—–~～至\u2010\u2011\u2212-]", "-", text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"(?<=\d)\s+(?=[月年日季])", "", text)
    return text


def _variants(lit: str) -> list[str]:
    """一个字面量的合法写法。

    `2026年1-7月` 在实际文本里常被写成 `1-7月`（年份省略）、破折号也可能被换成
    全角或「至」。这几种都是同一个期次，抹掉整段才不至于把 1、7 当成裸露数字；
    而单写 `7月` 不是任何一个变体，仍会被判坏 —— 那正是「把累计说成单月」要拦的。

    发布日期同理：出处里存的是 `2026-08-10`，模型转述时写「8月10日披露」。
    不给这个变体，模型一交代出处就会被判编数 —— 罚的是它做了对的事。
    """
    out = {lit, _norm_period(lit)}
    iso = re.fullmatch(r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})", lit.strip())
    if iso:
        month, day = int(iso.group(2)), int(iso.group(3))
        out.update(
            {
                f"{month}月{day}日",
                f"{month:02d}月{day:02d}日",
                f"{month}月{day}号",
                f"{iso.group(1)}年{month}月{day}日",
            }
        )
    # 只有年月的 ISO 期次（源数据里就是 `2026-08`）：模型转述时也照抄这个写法，
    # 不给变体就会被拆成 2026、08 两个裸数字，一句「2026-08 是 95.4%」直接判死。
    ym = re.fullmatch(r"(\d{4})\s*-\s*(\d{1,2})", lit.strip())
    if ym:
        month = int(ym.group(2))
        out.update(
            {
                f"{ym.group(1)}年{month}月",
                f"{ym.group(1)}年{month:02d}月",
                f"{month}月",
                f"{month:02d}月",
            }
        )
    for item in list(out):
        m = re.match(r"^\d{4}年(.+)$", item)
        if m:
            out.add(m.group(1))
    return [x for x in out if len(x) >= 2]


def _period_digits(literals: list[str]) -> set[int]:
    """期次里出现过的月/年数字。用来判断「1—6月」这种区间是不是拼出来的。"""
    out: set[int] = set()
    for lit in literals:
        for piece in _PERIOD_PIECE.findall(lit) + _PERIOD_RANGE.findall(lit):
            for match in _NUM.finditer(piece):
                out.add(int(_to_float(match.group(0))))
    return out


def _drop_known_ranges(text: str, digits: set[int]) -> str:
    """抹掉「两端都是已知期次数字」的区间，如 1—6月（6 来自 6 月累计）。

    只认 1-2 位的月序，不会碰到总量（1024.8）和年份（2026）。
    两端有一个不认识，就留给后面的数字校验去拦。
    """
    def repl(match: re.Match[str]) -> str:
        a, b = int(match.group(1)), int(match.group(2))
        if a in digits and b in digits:
            return " "
        return match.group(0)

    return re.sub(r"(\d{1,2})\s*-\s*(\d{1,2})\s*月?", repl, text)


def _strip(text: str, literals: list[str]) -> str:
    probe = _norm_period(text)
    # **长的先抹**。`2026年7月` 会同时产出 `7月` 这个变体，若先抹短的，
    # 原串就被拆成「2026年 」，反过来把 2026 判成无出处 —— 正确的话被误杀。
    variants: list[str] = []
    for lit in literals:
        variants.extend(_variants(lit))
    for variant in sorted(set(variants), key=len, reverse=True):
        if variant and variant in probe:
            probe = probe.replace(variant, " ")
    return probe


def audit(
    text: str,
    payload: Any,
    extra: set[float] | None = None,
    extra_literals: list[str] | None = None,
) -> dict[str, Any]:
    """核一段模型文本里的数字。

    返回 `{"ok": bool, "bad": [无出处的数字], "computed": [已验证的算式]}`。
    `ok=False` 时调用方不能原样使用这段文字：要么整段丢弃，要么按句剔除违规句
    （core/agent.py 的 `_salvage`）。不能只删掉那几个数字 —— 算式可能连环，
    删数字只会把一句话改成病句，还看不出错在哪。

    `extra_literals` 给「点名」用：记录标题里的 `第 14 次常务会`、`12345`
    是专有名词，参谋提到它们是在指出处，不是报数。只放标题，不放正文。
    """
    allowed, literals = facts_of(payload)
    if extra:
        allowed |= {float(x) for x in extra}
    if extra_literals:
        titles = [str(x) for x in extra_literals]
        literals = literals + titles
        # 专有名词里的数字是**标识号**（12345、第 14 次常务会），参谋要点名就得
        # 说出来。只放这一份清单里的数字，正文摘录不在这里 —— 正文里的数照样拦。
        for title in titles:
            for match in _NUM.finditer(title):
                allowed.add(_to_float(match.group(0)))

    # 问句里点到的月份（「7月份」）要单独放行，否则参谋连「7月当月」都说不出口。
    # 只抽 年/月 这种期次片段，不碰其他数字。
    for lit in list(literals):
        for piece in _PERIOD_PIECE.findall(lit):
            if len(piece) >= 2:
                literals.append(piece)
        for piece in _PERIOD_RANGE.findall(lit):
            literals.append(re.sub(r"[－—–~～至\u2010\u2011\u2212-]", "-", piece))
        for piece in _PERIOD_BARE.findall(lit):
            literals.append(re.sub(r"[－—–~～至\u2010\u2011\u2212-]", "-", piece))

    bad: list[str] = []
    computed: list[str] = []

    # 先验算式，并把结果加入放行集合：一句话里可能先算再引用
    for match in _EQ.finditer(text):
        a, op, b, c = match.group(1), match.group(2), match.group(3), match.group(4)
        try:
            fa, fb, fc = _to_float(a), _to_float(b), _to_float(c)
        except ValueError:  # pragma: no cover - 正则已保证是数字
            bad.append(match.group(0))
            continue
        got = _apply(fa, op, fb)
        if (
            got is not None
            and _close(got, fc)
            and any(_close(fa, x) for x in allowed)
            and any(_close(fb, x) for x in allowed)
        ):
            allowed.add(fc)
            computed.append(match.group(0))
        else:
            bad.append(match.group(0))

    # 顺序有讲究：先抹截止日（12月31日前），再抹区间（1—6月），最后抹单个月份（6月）。
    # 反过来的话「6月」先被抹掉，剩下「1- 」既不成区间也认不出来，1 就裸露了。
    rest = _norm_period(_EQ.sub(" ", text))
    rest = _DEADLINE.sub(" ", rest)
    rest = _drop_known_ranges(rest, _period_digits(literals))
    rest = _strip(rest, literals)
    for match in _NUM.finditer(rest):
        value = _to_float(match.group(0))
        if not any(_close(value, x) for x in allowed):
            bad.append(match.group(0))

    return {"ok": not bad, "bad": bad, "computed": computed}
