"""提问优化：把口语、错字的问句改写成指标字典认得的问法。

和 octop 的 `POST /agents/{id}/chat/polish` 定位一致（改写用户草稿、不写会话、
不碰历史），但目标不同：那边是通用润色「让另一个 AI 听懂」，这里只有一个
目标 —— 让 `ask.match_metrics` 命中。所以 system 里直接带指标目录，
第一条硬规则就是「指标名只能从目录里挑，不确定就保留原话」。

为什么改写结果必须再校验一遍（`_accept`）：
模型改写问句时最容易犯的两类错，恰好都是这个项目的红线 ——
① 把「1—9 月」改写成「前三季度」（数字被换掉，答案的期次就错了）；
② 用户没说期次，它好心补一个「2026 年 9 月」（凭空造事实）。
这两种输出在文本上都很漂亮，肉眼很难发现。所以不靠提示词自觉，代码里逐条核：
原文出现的每个数字都必须还在，原文没有的数字一个都不许多出来。
校验不过就丢弃模型输出、退回规则整理，并把原因带给界面。

另有一条构造上的边界：改写结果只进输入框，**由用户决定发不发**。这一层不
写会话、不调 ask、不产生任何数值 —— 它能出错，但错不到答案里。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import llmclient
import modelcfg
import mockdata as md
import queryfix

# 输入上限。输入框本身是 3 行的短问句，超过这个长度基本是误粘贴整篇文章，
# 拿去让模型改写只会更慢更贵，而且改出来一定不像一句话。
MAX_INPUT = 300
MAX_OUTPUT = 240

_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?\s*[%％]?")
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_PREFIX = re.compile(r"^(改写后|优化后|整理后|问题|问句|回答|答)\s*[:：]\s*")


def _system_prompt() -> str:
    lines = []
    for code, meta in md.METRICS.items():
        lines.append(f"- 名称={meta['name']} ｜ 别名={'、'.join(meta['aliases'])}")
    return (
        "你是政务问数系统的「提问整理器」，不是回答者。\n"
        "用户给你一句口语化的提问，你的唯一任务：把它改写成系统指标字典认得的问法。\n\n"
        "硬规则：\n"
        "1. 指标名只能从下面的目录里挑。目录里没有的，保留用户原话，不替换、不编造。\n"
        "2. 不回答这个问题，不解释，不给建议，不补充任何事实。\n"
        "3. 原文里的数字和期次必须原样保留（「1—9 月」不能改成「前三季度」）。\n"
        "   原文没写期次，绝对不要补年份或月份。\n"
        "4. 只改三件事：指标名的错别字、口语说法（如「咋样」「多少来着」）、\n"
        "   和字典不一致的简称（如「固投」改成「固定资产投资」）。\n"
        "5. 输出一句话，中文，不超过 60 字，只输出这句话本身：\n"
        "   不要引号、不要「改写后：」这类前缀、不要 Markdown、不要换行。\n\n"
        "已注册指标（名称 ｜ 别名）：\n" + "\n".join(lines)
    )


def _tokens(text: str) -> Counter[str]:
    """数字 token 的多重集。千分位、全角百分号先归一，再计数。

    用多重集而不是集合：「从 3% 到 5%」改成「从 3% 到 3%」是集合察觉不到的
    篡改，而且恰恰是最容易被当成「改写成功」的那种输出。
    """
    out: Counter[str] = Counter()
    for match in _NUM_TOKEN.finditer(str(text or "").replace("％", "%")):
        out[match.group().replace(",", "").replace(" ", "")] += 1
    return out


def _clean(raw: str) -> str:
    """把模型输出收成一句话：去围栏、去前缀、只留第一行、去首尾引号。"""
    text = _FENCE.sub("", str(raw or "").strip())
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    text = _PREFIX.sub("", lines[0])
    return text.strip().strip("\"'“”「」『』 ").strip()


def _accept(original: str, candidate: str) -> tuple[bool, str]:
    """改写结果能不能用。返回 (是否通过, 不通过的原因)。"""
    if not candidate:
        return False, "模型没有输出内容"
    if len(candidate) > MAX_OUTPUT:
        return False, f"输出 {len(candidate)} 字，超过 {MAX_OUTPUT} 字上限"
    if "\n" in candidate:
        return False, "输出不止一行"
    if candidate.count("？") + candidate.count("?") > 1:
        # 一句问句一个问号；两个以上说明它在自问自答或列了好几种问法
        return False, "输出包含多个问句"

    before, after = _tokens(original), _tokens(candidate)
    lost = before - after
    if lost:
        return False, "丢了原文的数字：" + "、".join(sorted(lost))
    added = after - before
    if added:
        return False, "多出原文没有的数字：" + "、".join(sorted(added))
    return True, ""


def _short(exc: BaseException) -> str:
    text = str(exc).strip().replace("\n", " ")
    return (type(exc).__name__ + (": " + text if text else ""))[:160]


def _attempts(model_ref: str | None, limit: int = 2) -> list[str]:
    """先指定的（或全局默认），再其它已配置模型。

    这里和问数用同一套降级顺序。只用首选一个的话会出现很别扭的一幕：
    本地模型没起来时，问数还能靠备用 provider 答出来，而「整理提问」
    直接甩一句「模型不可用」—— 同一个界面里两个结论互相打脸。
    """
    primary = model_ref or modelcfg.active_ref()
    refs = [primary] if primary else []
    for item in modelcfg.resolved():
        ref = item.get("ref")
        if ref and ref not in refs:
            refs.append(ref)
    return refs[:limit]


# --------------------------------------------------------------------------
# 自动理解：命不中时自己改写一次，不用用户点按钮
# --------------------------------------------------------------------------

# 「像不像在问数据」。判断刻意放松，因为两种误判的代价不对称：
# 放行一句不该放行的，代价是知识库里那种问题多等两三秒；
# 拦掉一句该放行的，用户就少了这次优化 —— 而这次优化本来就是他抱怨的那件事。
_DATA_HINT = re.compile(
    r"\d|[%％]|多少|几[个项条件]|率|增速|增长|下降|完成|进度|怎么样|咋样|"
    r"是多少|达到|达标|占比|比重|排名|位次|同比|环比|数据|指标|规模|收入|支出|产值|面积"
)
# 反过来，这些词说明用户要的是「解释」而不是「数值」。知识库能直接回答，
# 让模型改写只会把问题往指标上拽，答出一段口径说明或者一个不相干的数。
_DOC_HINT = re.compile(r"口径|定义|怎么算|怎么统计|谁负责|谁在管|会议|纪要|上次|定了|督办|文件|依据")


def looks_like_data_question(question: str) -> bool:
    text = str(question or "")
    if not text:
        return False
    return bool(_DATA_HINT.search(text)) and not _DOC_HINT.search(text)


def autofix(question: str, model_ref: str | None = None, timeout: float = 12.0) -> dict[str, Any] | None:
    """命不中指标时自动改写一次，**命中才返回**，没帮上忙就返回 None。

    这个函数的存在理由：错字、口语、简称这三类问句，规则层救不全，而让
    用户先点一个「整理」按钮再发，等于把系统该做的事派给了用户 —— 点击率
    低、还多一步。所以放到问句未命中的那条路上自动做。

    只在「本来要拒答」时才被调用，正常情况下一次模型调用都不会多花。
    None 的三种原因（没模型 / 改坏了 / 改完还是没命中）在这里故意不区分：
    对调用方它们是同一件事 —— 这句话没救回来，按原流程走知识库和拒答。

    返回：
      text     改写后的问句（已过数字守恒校验）
      codes    命中的指标 code（可能多个）
      aliases  对应的命中片段，用来在答案里注明「按哪几个字理解的」
      via      llm / llm+fuzzy
      model    实际使用的模型
    """
    raw = str(question or "").strip()
    if not raw or len(raw) > MAX_INPUT:
        return None
    if not modelcfg.available():
        # 没模型时规则层已经在 ask 里跑过了（精确 + 近似），这里再跑一遍是白跑
        return None

    out = polish(raw, model_ref=model_ref, timeout=timeout)
    text = str(out.get("text") or "")
    if not text or text == raw:
        return None

    hits = queryfix.exact_hits(text)
    via = "llm"
    if not hits:
        # 改写之后仍然没直接命中，只接受「高度确信」的近似。
        # 这里比 ask 的正常路径更严：改写本身已经是一次猜测，
        # 再叠一层弱近似，就是两个不确定凑成一个看上去确定的答案。
        cands = queryfix.fuzzy_suggest(text)
        if not cands or not queryfix.is_strong(cands[0], cands[1:2]):
            return None
        hits = [(str(cands[0]["code"]), str(cands[0]["alias"]))]
        via = "llm+fuzzy"

    return {
        "text": text,
        "codes": [code for code, _ in hits],
        "names": [md.METRICS[code]["name"] for code, _ in hits],
        "aliases": [alias for _, alias in hits],
        "via": via,
        "model": out.get("model"),
    }


def polish(question: str, model_ref: str | None = None, timeout: float = 30.0) -> dict[str, Any]:
    """改写一句问句。返回可直接给界面用的结构。

    返回字段：
      text       改写后的问句（校验不过时就是原文 / 规则整理后的文本）
      changed    和原文相比是否真的变了
      method     "llm" | "rules"
      applied    规则层做过的替换（「民生十事」→「民生实事」）
      candidates 近似指标候选，界面可提示「我理解成 X」
      degraded   非空表示这次不是首选路径给的：{kind, reason}
      model      实际使用的模型标识（规则路径为 None）
    """
    raw_question = str(question or "").strip()
    if not raw_question:
        raise ValueError("empty_question")
    if len(raw_question) > MAX_INPUT:
        raise ValueError("too_long")

    # 规则整理先做一遍：它既是无模型时的兜底，也是模型那条路失败后的退路。
    rule_text, applied = queryfix.canonicalize(raw_question)

    degraded: dict[str, Any] | None = None
    text, method, model = rule_text, "rules", None

    if not modelcfg.available():
        degraded = {
            "kind": "rules",
            "reason": "当前没有配置模型，只按指标别名做了错字/简称整理，"
            "口语说法没有归一。接模型见使用说明第三节。",
        }
    else:
        errors: list[str] = []
        for ref in _attempts(model_ref):
            try:
                out = llmclient.chat_text(
                    _system_prompt(),
                    "只改写下面这句问句，不要回答它：\n<<<\n" + raw_question + "\n>>>",
                    model_ref=ref,
                    timeout=timeout,
                    max_tokens=200,
                )
                candidate = _clean(str(out.get("text") or ""))
                ok, why = _accept(raw_question, candidate)
                if ok:
                    text, method, model = candidate, "llm", out.get("model")
                    if errors:
                        # 换过模型才成：得说出来，用户看到的是「改好了」，
                        # 但质量已经不是首选模型给的
                        degraded = {
                            "kind": "fallback",
                            "reason": "首选模型不可用，已改用备用模型整理。",
                            "errors": errors[:4],
                        }
                    break
                # 校验不过不是「模型连不上」，换一个模型它很可能照样补期次，
                # 再试只是多等几秒。直接退回规则整理。
                degraded = {
                    "kind": "rejected",
                    "reason": f"模型改写未通过校验（{why}），已退回规则整理。",
                    "errors": [f"{ref}: {why}"],
                }
                break
            except llmclient.ModelUnavailable:
                degraded = {"kind": "rules", "reason": "模型服务不可用，已退回规则整理。"}
                break
            except Exception as exc:  # noqa: BLE001 网络/额度/超时都不该让这个按钮不可用
                errors.append(f"{ref}: {_short(exc)}")
                degraded = {
                    "kind": "fallback",
                    "reason": "模型改写失败，已退回规则整理。",
                    "errors": errors[:4],
                }
                continue

    return {
        "text": text,
        "changed": text != raw_question,
        "method": method,
        "applied": applied,
        "candidates": queryfix.fuzzy_suggest(raw_question, limit=3),
        "matched": [md.METRICS[code]["name"] for code, _ in queryfix.exact_hits(text)],
        "degraded": degraded,
        "model": model,
    }
