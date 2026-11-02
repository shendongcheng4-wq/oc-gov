"""会议纪要 → 议定事项：模型抽取 + 服务端校验。

这件事的风险和问数不一样。问数是「模型只出查询计划，数值由接口给」，模型没有机会
编数字；纪要抽取是**模型直接从自然语言生成结构化字段**，它有充分的机会编出
责任单位、时限、验收标准。政务场景里一条编出来的"11 月 20 日前完成"会直接变成督办
时限，后果比答错一句话严重得多。

所以这里设三道闸：

1. **引文闸**：每条事项必须给 `quote`（原文逐字连续片段）。服务端做归一化子串匹配，
   对不上就整条标 `needs_review`。模型没法靠"理解大意"绕过 —— 它必须真的指着原文说话。
2. **数字闸**：事项标题/验收标准/时限里出现的阿拉伯数字，必须能在原文里找到。
   原文只说"年底前"，模型就别想给出 12-31。
3. **白名单闸**：责任单位只能取自实际存在过的单位（督办台账 + 指标字典的责任部门）。
   不在名单里的标 `needs_review`，不自动落库。

三道闸全过的条目才允许写进督办台账，且仍要人工勾选确认 —— `verify` 只是过滤器，
不是审批。这条边界不要为了让流程顺一点而拆掉。

本模块只依赖标准库和同项目的 core 模块，不感知 HTTP。将来要包成 MCP 工具，直接
`extract()` / `verify_item()` 拿去用即可。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import llmclient
import mockdata as md
import oversight as ov

# 正文最多送这么多字。再长模型也记不住，且纪要有用信息通常在最前面。
MAX_CHARS = 12000

CONFIDENCES = {"high", "low"}

_PUNCT = re.compile(
    # 全部用 \uXXXX 写，避免中文全角引号在源码里被当成 ASCII 引号截断字符串。
    # 覆盖：空白 / 全角空格 / ，。、；：/ “”‘’ / （）《》〈〉【】/ ！？～－—…·
    # 以及半角 , . ; : ! ? ~ - " ' ` ( ) [ ] { }
    r"[\s\u3000\uff0c\u3002\u3001\uff1b\uff1a\u201c\u201d\u2018\u2019"
    r"\uff08\uff09\u300a\u300b\u3008\u3009\u3010\u3011\uff01\uff1f\uff5e\uff0d"
    r"\u2014\u2026\u00b7,.;:!?~\"'`\-\[\](){}]+"
)
_DIGITS = re.compile(r"\d+(?:\.\d+)?")


def normalize(text: str) -> str:
    """去掉所有空白和标点，只留字。用于「这段引文是不是真在原文里」的判断。

    这样做会放过"标点不同"的情况（模型把「，」抄成「、」），但不会放过改写：
    只要用词变了，归一化后的串就对不上。业务上这是对的 —— 我们要防的是编造，
    不是防标点。
    """
    return _PUNCT.sub("", text or "")


def known_units() -> list[str]:
    """可交办单位白名单：督办台账里实际出现过的 + 指标字典的责任部门。

    不从"常见政府部门清单"里补 —— 补了就等于允许模型往一个我们没有任何台账的
    单位头上派活，出问题时连该找谁都不知道。名单短一点，宁可标待核实。

    注意：直接在种子文件里硬编码单位名是另一条路，但那样台账加了新单位这里就
    失效了。所以每次从当前台账现算，顺序稳定（sorted）以便 prompt 可复现。
    """
    units: set[str] = set()
    for item in ov.list_items():
        units.add(str(item.get("assignee") or ""))
        for co in item.get("co_assignees") or []:
            units.add(str(co))
    for meta in md.METRICS.values():
        units.add(str(meta.get("department") or ""))
    return sorted(u for u in units if u)


SYSTEM_PROMPT = """你是政务会议纪要的结构化抽取器，只做一件事：从会议记录原文里，
把「议定事项」抽成结构化条目。

严格要求：
1. 只抽原文明确写了的事项。原文没写的不要补，不要推测，不要"合理推断"。
   读者看完你的输出，应该能逐条在原文里指出来，指不出来就是错的。
2. 每条必须给 quote：从原文**逐字连续照抄**的一段话，8—80 字。不能改写、
   不能拼接两处、不能加省略号、不能调整语序。服务端会校验 quote 是否真实存在于
   原文，对不上整条作废。
3. title：动宾短语概括这条事项，不超过 30 字。不得引入原文没有的数字。
4. assignee：只能从【可交办单位】里原样选一个。原文没写责任单位、或写的单位
   不在列表里，填 null —— 不要用相近的单位替代。
5. deadline：只有原文明确说了完成时间才填，格式 YYYY-MM-DD（今天是 {today}，
   年份据此推算）。原文说「年底前」「尽快」「近期」「下一步」这类不明确表述，
   一律填 null，不要自己定一个日子。
6. standard：这条事项「办成什么样算办结」，一句话，必须能在 quote 里找到依据。
   原文没给验收标准的，把原文要求原样压缩，不要新编指标、数字或比例。
7. 没有议定事项就返回 {"items":[]}，不要硬凑条数。

只输出 JSON，不要 markdown 代码块：
{"items":[{"title":"","assignee":null,"deadline":null,"standard":"","quote":"","confidence":"high"}]}

confidence 只能填 high 或 low：原文表述明确填 high，有歧义填 low。
"""


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def verify_item(
    item: dict[str, Any],
    source_text: str,
    units: list[str] | None = None,
    today: date | None = None,
    manual: set[str] | None = None,
) -> dict[str, Any]:
    """校验一条抽取结果，返回 {status, issues, confirmed_by_human}。纯函数，不碰存储。

    issues 里每一项是 {"code": 机器可读, "field": 字段名, "detail": 给人看的话}。
    前端按 code 决定怎么标红，按 detail 提示用户改哪里。

    `manual` 是**人工改过的字段名集合**。这里要分清两类问题：

    - **硬问题**：引文对不上、字段为空、时限已过期、单位不在名单 —— 客观错误，
      人改了也得重新满足，不能豁免。
    - **软问题**：`deadline_not_grounded`、`number_not_in_source` —— 本质是
      「疑似模型编的」。判定依据是"原文没有"和"数据里没有"。

    软问题必须允许人工认领，否则死锁：原文写"年底前"，人补成 12-20，系统永远
    说"原文没有 12 月"，这条就永远落不了库。而人在这个界面上改字段，本身就是
    在承担这个责任 —— 他是市长办的人，比任何字符串匹配更有资格定这个日子。

    所以：人改过的字段，软问题豁免，但在 `confirmed_by_human` 里留痕。审计要的
    不是"拦住人"，而是"谁定的、什么时候定的"留得下来。
    """
    units = units if units is not None else known_units()
    today = today or date.today()
    manual = manual or set()
    src = normalize(source_text)
    quote = _as_text(item.get("quote"))
    norm_quote = normalize(quote)
    issues: list[dict[str, str]] = []
    confirmed: list[str] = []

    # —— 闸 1：引文 ——
    if not quote:
        issues.append({"code": "quote_missing", "field": "quote", "detail": "没有给原文引文，无法核对。"})
    elif len(quote) > 200:
        issues.append({"code": "quote_too_long", "field": "quote", "detail": f"引文 {len(quote)} 字，过长。"})
    elif norm_quote not in src:
        issues.append(
            {
                "code": "quote_not_found",
                "field": "quote",
                "detail": "引文在原文里找不到，可能被改写或拼接了。",
            }
        )

    # —— 字段完整性 ——
    title = _as_text(item.get("title"))
    if not title:
        issues.append({"code": "title_empty", "field": "title", "detail": "事项标题为空。"})
    elif len(title) > 60:
        issues.append({"code": "title_too_long", "field": "title", "detail": f"标题 {len(title)} 字，超过 60。"})

    standard = _as_text(item.get("standard"))
    if not standard:
        issues.append(
            {"code": "standard_empty", "field": "standard", "detail": "没有办结标准，督办就没有终点。"}
        )

    # —— 闸 3：责任单位白名单 ——
    assignee = _as_text(item.get("assignee"))
    if not assignee:
        issues.append(
            {"code": "assignee_missing", "field": "assignee", "detail": "没有责任单位，无法交办。"}
        )
    elif assignee not in units:
        issues.append(
            {
                "code": "unknown_unit",
                "field": "assignee",
                "detail": f"「{assignee}」不在已知单位里，需要人工确认全称。",
            }
        )

    # —— 时限 ——
    deadline = _as_text(item.get("deadline"))
    if not deadline:
        issues.append(
            {"code": "deadline_missing", "field": "deadline", "detail": "没有时限，无法派生逾期风险。"}
        )
    else:
        try:
            dl = date.fromisoformat(deadline)
        except ValueError:
            issues.append(
                {"code": "bad_deadline", "field": "deadline", "detail": f"时限格式应为 YYYY-MM-DD，收到「{deadline}」。"}
            )
            dl = None
        if dl is not None:
            if dl < today:
                issues.append(
                    {"code": "deadline_past", "field": "deadline", "detail": f"时限 {deadline} 早于今天。"}
                )
            # 数字闸（时限）：月和日必须在原文里出现过，否则就是模型自己定的日子
            # 人改过 deadline 就豁免 —— 见函数说明里的"软问题"。
            if "deadline" in manual:
                confirmed.append("deadline")
            else:
                for part, label in ((dl.month, "月"), (dl.day, "日")):
                    if str(part) not in src:
                        issues.append(
                            {
                                "code": "deadline_not_grounded",
                                "field": "deadline",
                                "detail": f"原文里没有出现 {part} {label}，时限可能是模型推测的。",
                            }
                        )
                        break

    # —— 闸 2：数字 ——
    for field, value in (("title", title), ("standard", standard)):
        if field in manual:
            confirmed.append(field)
            continue
        missing = sorted({d for d in _DIGITS.findall(value) if d not in src})
        if missing:
            issues.append(
                {
                    "code": "number_not_in_source",
                    "field": field,
                    "detail": f"「{'、'.join(missing)}」在原文里找不到，可能是编的。",
                }
            )

    return {
        "status": "ok" if not issues else "needs_review",
        "issues": issues,
        "quote_found": bool(norm_quote) and norm_quote in src,
        "confirmed_by_human": sorted(set(confirmed)),
    }


def _clean_items(raw: Any) -> list[dict[str, Any]]:
    """把模型返回的 items 洗成固定形状。形状不对的整条丢弃，不做"尽力修复"。"""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        title = _as_text(entry.get("title"))
        if not title:
            continue
        conf = _as_text(entry.get("confidence")).lower()
        out.append(
            {
                "title": title[:120],
                "assignee": _as_text(entry.get("assignee")) or None,
                "deadline": _as_text(entry.get("deadline")) or None,
                "standard": _as_text(entry.get("standard")),
                "quote": _as_text(entry.get("quote"))[:300],
                "confidence": conf if conf in CONFIDENCES else "low",
                "raw_assignee": _as_text(entry.get("assignee")),
            }
        )
    return out


def extract(
    text: str,
    *,
    title: str = "",
    held_at: str = "",
    attendees: list[str] | None = None,
    model_ref: str | None = None,
    today: date | None = None,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """抽取 + 校验。返回 {items, warnings, model_ref, model, usage, truncated}。

    模型不可用时抛 ModelUnavailable（不返回空列表假装成功）—— 「没抽到」和
    「没法抽」必须让用户分得清，否则他会以为这份纪要真的没有议定事项。
    """
    today = today or date.today()
    body = (text or "").strip()
    if not body:
        raise ValueError("empty_text")

    truncated = len(body) > MAX_CHARS
    body = body[:MAX_CHARS]

    units = known_units()
    context = "\n".join(
        [
            f"会议标题：{title or '（未填）'}",
            f"会议时间：{held_at or '（未填）'}",
            f"参会单位：{'、'.join(attendees) if attendees else '（未填）'}",
            "",
            "可交办单位（assignee 只能从这里选）：",
            "、".join(units),
            "",
            "会议记录原文：",
            body,
        ]
    )
    if truncated:
        context += f"\n（原文超过 {MAX_CHARS} 字，已截断）"

    out = llmclient.chat_json(
        SYSTEM_PROMPT.replace("{today}", today.isoformat()),
        context,
        model_ref=model_ref,
        temperature=0,
        timeout=timeout,
    )

    items = _clean_items(out["data"].get("items"))
    warnings: list[str] = []
    if truncated:
        warnings.append(f"原文超过 {MAX_CHARS} 字已截断，后半段未参与抽取。")

    verified = []
    for idx, item in enumerate(items, 1):
        check = verify_item(item, body, units=units, today=today)
        verified.append(
            {
                "seq": idx,
                **{k: v for k, v in item.items() if k != "raw_assignee"},
                "verify": check,
            }
        )

    ok_count = sum(1 for i in verified if i["verify"]["status"] == "ok")
    if not verified:
        warnings.append("模型认为这份记录里没有议定事项。请人工确认后手工补录。")
    elif ok_count < len(verified):
        warnings.append(f"{len(verified) - ok_count} 条未通过校验，需人工核实后才能落督办。")

    return {
        "items": verified,
        "warnings": warnings,
        "units": units,
        "model_ref": out["model_ref"],
        "model": out["model"],
        "usage": out["usage"],
        "truncated": truncated,
    }
