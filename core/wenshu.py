"""开文种：请示、通知的成稿与排版。

材料助手（`briefdoc`）解决的是「把已经发生的事排成一份汇报」，这里是另一件事：
**还没有的事，要写成一份能签发的文**。

请示和通知之所以能自动化，不是因为模型会写公文，而是因为这两类文的事实来源是
确定的、结构是法定的：

- 结构法定。请示就是「缘由—事项—结语」，通知就是「依据—事项—要求—结语」，
  没有第二种写法。所以骨架由代码固定，谁也不能增删条款。
- 事实来自登记过的地方。请示要办的事在督办台账里，通知要讲的指标在口径表里，
  依据的会议在会议台账里。用户勾一个编号，正文里那句话就有了出处。
- 模型只写「缘由段」。这一段是唯一允许自由措辞的地方，因为它是背景交代，
  不承担数字责任。而且写出来之后仍然要过数字闸、单位闸。

**系统不编文号。** 发文字号由发文登记簿管理，序号取决于本年度已经发到第几号，
系统里没有这个登记簿，编出来的号一定是错的。所以默认留空，纸面上显示
「溧政〔2026〕__号」，并在体例检查里提示去登记簿取号。同理，成文日期默认取今天，
可以改——签发日是领导定的，不是系统定的。

**不带示例事实进正文。** 勾选的台账事项如果什么都没填，会自动变成事项条款，
这是为了少打几遍字；但每一句都带着台账编号，能点回去看原文。宁可丑，
不能出现「材料里有件事，台账里查不到」。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "core", ROOT / "core" / "metrics", ROOT / "core" / "oversight", ROOT / "core" / "meeting"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ask as ask_mod  # noqa: E402
import docxgen as dg  # noqa: E402
import llmclient  # noqa: E402
import mockdata as md  # noqa: E402
import oversight as ov  # noqa: E402
from ledger import get as meeting_get  # noqa: E402

CITY = "溧阳市"

# 一、二、三……（通知事项用），（一）（二）……（请示事项用）
_CN = ("一", "二", "三", "四", "五", "六", "七", "八", "九")

MAX_ITEMS = 6
MAX_ANCHORS = 6
REASON_MIN = 30
REASON_MAX = 240

KINDS: dict[str, dict[str, Any]] = {
    "qingshi": {
        "id": "qingshi",
        "label": "请示",
        "hint": "上行文：一文一事，结尾请上级批复",
        "word": "请示",
        "org": "溧阳市人民政府",
        "no_prefix": "溧政",
        "to": "常州市人民政府",
        "tail": "妥否，请批示。",
        "join": "现将有关事项请示如下：",
        "item_no": "paren",
        "need_contact": True,
        "cc_label": "",
        "sections": ["请示缘由", "请示事项", "结语"],
    },
    "tongzhi": {
        "id": "tongzhi",
        "label": "通知",
        "hint": "下行文：讲清依据、事项和时限",
        "word": "通知",
        "org": "溧阳市人民政府办公室",
        "no_prefix": "溧政办发",
        "to": "各镇（区）人民政府，市各委办局、市各直属单位",
        "tail": "特此通知。",
        "join": "现就有关事项通知如下：",
        "item_no": "dun",
        "need_contact": False,
        "cc_label": "抄送",
        "sections": ["通知依据", "通知事项", "工作要求", "结语"],
    },
}
KIND_IDS = tuple(KINDS)

# 公文里不用的套话：没有动作、没有对象、删掉不影响信息量。
# 这一条和审校里的 sty-commit 是同一套判断标准，放在这里是因为文种稿
# 一出厂就要过一遍，不能等用户点「审校」才发现满篇「高度重视」。
CLICHES = (
    "高度重视", "狠抓落实", "进一步加强", "切实抓好", "全面提升",
    "扎实推进", "全力推进", "大力推动", "切实增强",
)

_UNIT_RE = re.compile(r"市[\u4e00-\u9fa5]{2,10}?(?:局|委|办|中心|院|校|公司|集团)")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _nums(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def _cn_date(text: str) -> str:
    """把 2026-10-28 写成 2026年10月28日。写不出来就原样返回。"""
    raw = str(text or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if not m:
        return raw
    return f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日"


def _period_cn(period: str) -> str:
    return f"{period[:4]}年{int(period[5:7])}月" if len(period) >= 7 else period


# --------------------------------------------------------------------------
# 事实锚点：勾一个编号，换一句话，带一处出处
# --------------------------------------------------------------------------
def _ledger_fact(item_id: str) -> dict[str, Any] | None:
    # 走 list_items 而不是 ov.get：前者返回的是对外简报（含派生风险），
    # 后者是内部 dataclass，字段名不一样，混用会在「风险」这种派生字段上翻车
    it = next((x for x in ov.list_items() if x["id"] == item_id), None)
    if not it:
        return None
    risk = it.get("risk") or ""
    deadline = _cn_date(it.get("deadline") or "")
    assignee = it.get("assignee") or "未明确"
    bits = [f"{it['title']}由{assignee}承办"]
    if deadline:
        bits.append(f"办结时限{deadline}")
    if it.get("status"):
        bits.append(f"目前{it['status']}")
    if risk and risk != "正常":
        bits.append(f"已{risk}")
    # 事项条款要的是「谁在什么时候做完」，不是一句描述——把同一件事写成两种句式，
    # 缘由段和事项段才不会看起来像同一句抄了两遍
    clause = it["title"] + "。"
    clause += (f"{assignee}于{deadline}前完成。" if deadline else f"{assignee}负责落实。")
    return {
        "type": "ledger",
        "ref": it["id"],
        "label": it["title"],
        "text": "，".join(bits) + "。",
        "clause": clause,
        "short": it["title"],
        "source": it.get("source_ref") or "",
        "nums": _nums(" ".join([deadline, str(it.get("progress") or "")])),
    }


def _metric_fact(code: str, today: date) -> dict[str, Any] | None:
    if code not in md.METRICS:
        return None
    r = md.query_metric(code, None, 1, today)
    if not r.get("ok") or r.get("value") is None:
        return None
    unit = r.get("unit") or "%"
    value = ask_mod.fmt_value(r.get("value"), unit)
    # 中文里数字和月份之间不加空格：「2026年9月为 8.9%」，加了空格读起来像表格派生
    bits = [f"{r['name']}{r.get('period_label') or ''}为{value}"]
    if r.get("target") is not None:
        bits.append(f"年度目标{ask_mod.fmt_value(r['target'], unit)}")
    src = r.get("source") or {}
    report = str(src.get("report") or r["name"])
    # report 一般自带书名号（《本市统计月报》2026年9月），再加一层就成了《《…》》
    shown = report if "《" in report else f"《{report}》"
    return {
        "type": "metric",
        "ref": code,
        "label": r["name"],
        "text": "，".join(bits) + "。",
        "short": r["name"],
        "source": f"{src.get('department') or ''}{shown}".strip(),
        "nums": _nums(" ".join([value, str(r.get("target") or "")])),
    }


def _meeting_fact(mid: str) -> dict[str, Any] | None:
    m = meeting_get(mid)
    if not m:
        return None
    held = _cn_date(m.get("held_at") or "")
    bits = [f"{m['title']}"]
    if held:
        bits.append(f"于 {held} 召开")
    if m.get("host"):
        bits.append(f"由{m['host']}主持")
    return {
        "type": "meeting",
        "ref": m["id"],
        "label": m["title"],
        "text": "，".join(bits) + "。",
        "short": m["title"],
        "source": m["id"],
        "nums": _nums(held),
    }


def _facts(anchors: Any, today: date) -> tuple[list[dict[str, Any]], list[str]]:
    """把前端传来的 {type, ref} 换成事实句。查不到的如实报告，不静默丢。"""
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not isinstance(anchors, list):
        return out, warnings
    for a in anchors[:MAX_ANCHORS]:
        if not isinstance(a, dict):
            continue
        typ = str(a.get("type") or "").strip()
        ref = str(a.get("ref") or a.get("id") or a.get("code") or "").strip()
        if not ref:
            continue
        fact = None
        if typ == "ledger":
            fact = _ledger_fact(ref)
        elif typ == "metric":
            fact = _metric_fact(ref, today)
        elif typ == "meeting":
            fact = _meeting_fact(ref)
        if fact is None:
            warnings.append(f"锚点 {typ}:{ref} 在系统里查不到，已跳过。")
            continue
        if any(f["type"] == fact["type"] and f["ref"] == fact["ref"] for f in out):
            continue
        out.append(fact)
    return out, warnings


# --------------------------------------------------------------------------
# 缘由段：唯一允许模型措辞的地方
# --------------------------------------------------------------------------
_SYS = """你是市政府办公室的文书。下面给出了一份「{word}」的事实清单（JSON），你只写开头那段「缘由」。

硬规则（违反任何一条，这段会被整段丢弃，退回规则文本）：
- 120 到 200 字，2 到 4 句，句号收尾属于上一段，不要自己加「请示如下」「通知如下」。
- 只能使用事实清单里出现过的数字，不许自己算比例、算差额、算天数、算排名。
- 不许新增事实清单里没有的事项、单位、指标、会议。
- 不写「高度重视」「狠抓落实」「进一步加强」这类没有动作的套话。
- 不评价工作好坏，只交代「为什么现在要办这件事」。
- 只输出 JSON：{{"reason": "..."}}"""


def _clauses(fields: dict[str, Any], facts: list[dict[str, Any]]) -> list[str]:
    """把用户填的要素拆成可清点的短句，模型和规则文本共用这一份。"""
    out: list[str] = []
    subject = str(fields.get("subject") or "").strip()
    if subject:
        out.append(f"事由：{subject}")
    for f in facts:
        out.append(f"事实：{f['text']}")
    return out


def _rule_reason(
    kind: str,
    fields: dict[str, Any],
    facts: list[dict[str, Any]],
    skip: set[str] | None = None,
) -> str:
    """规则版缘由段。不评价、不引申，只用用户给的要素和勾选的事实。

    事由和事实标题高度重合时不重复写：勾了「高铁东站周边路网配套工程」这条台账，
    事由又写着同一件事，两句话开头一模一样，读起来像系统在复读。
    已经被事项条款用掉的台账（skip）也不再引——一件事在纸上说两遍，
    读的人会以为是两件事。
    """
    skip = skip or set()
    facts = [f for f in facts if f["ref"] not in skip]
    subject = str(fields.get("subject") or "").strip()
    basis = str(fields.get("basis") or "").strip()
    parts: list[str] = []

    dup = bool(facts) and (facts[0]["short"] in subject or subject in facts[0]["short"])
    if subject and not dup:
        parts.append(f"{'为' if kind == 'tongzhi' else '关于'}{subject}，")
    if basis:
        parts.append(f"根据{basis}，")
    for f in facts[:2]:
        parts.append(f["text"])
    if not parts:
        # 什么都没勾、事由也空着：宁可给一句空话，也不能编一个背景出来
        return ""
    return "".join(parts).rstrip("，。") + "。" + KINDS[kind]["join"]


def _model_reason(
    kind: str,
    fields: dict[str, Any],
    facts: list[dict[str, Any]],
    *,
    allowed_nums: set[str],
    allowed_units: set[str],
) -> dict[str, Any]:
    if not llmclient.available():
        return {"source": "rule", "note": "未配置模型，缘由段按要素与锚点事实生成", "degraded": True}

    payload = {
        "文种": KINDS[kind]["word"],
        "主送": str(fields.get("to") or "").strip(),
        "发文机关": str(fields.get("org") or "").strip(),
        "要素": _clauses(fields, facts),
    }
    try:
        res = llmclient.chat_json(
            _SYS.format(word=KINDS[kind]["word"]),
            json.dumps(payload, ensure_ascii=False),
            timeout=45,
            max_tokens=500,
        )
    except (llmclient.ModelUnavailable, llmclient.ModelError) as exc:
        return {"source": "rule", "note": f"模型调用失败（{type(exc).__name__}），缘由段退回规则文本", "degraded": True}

    text = str((res.get("data") or {}).get("reason") or "").strip()
    if not text:
        return {"source": "rule", "note": "模型未给出内容，缘由段退回规则文本", "degraded": True}
    if len(text) > REASON_MAX:
        return {"source": "rule", "note": "模型输出的缘由段过长，已丢弃", "degraded": True}
    extra = _nums(text) - allowed_nums
    if extra:
        return {"source": "rule",
                "note": f"模型输出含事实清单外的数字（{'、'.join(sorted(extra)[:3])}），已丢弃",
                "degraded": True}
    stray = {u for u in _UNIT_RE.findall(text) if u not in allowed_units}
    if stray:
        return {"source": "rule",
                "note": f"模型输出提到了材料里没有的单位（{'、'.join(sorted(stray)[:2])}），已丢弃",
                "degraded": True}
    for c in CLICHES:
        if c in text:
            return {"source": "rule", "note": f"模型输出用了套话「{c}」，已丢弃", "degraded": True}
    return {"source": "model", "text": text, "note": "缘由段由模型根据勾选事实撰写，数字均取自事实清单",
            "degraded": False}


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------
def _item_text(kind: str, idx: int, text: str) -> str:
    body = str(text).strip().rstrip("。；;")
    if not body:
        return ""
    no = KINDS[kind]["item_no"]
    head = f"（{_CN[idx]}）" if no == "paren" else f"{_CN[idx]}、"
    return f"{head}{body}。"


def _auto_items(fields: dict[str, Any], facts: list[dict[str, Any]], kind: str) -> list[dict[str, str]]:
    """用户没写事项时，把勾选的台账事项直接变成条款。

    只对台账成立：指标是背景、会议是依据，都不能当「要办的事」。
    """
    items = fields.get("items")
    if isinstance(items, list) and any(str((x or {}).get("text") or "").strip() if isinstance(x, dict) else str(x or "").strip() for x in items):
        return []
    out: list[dict[str, str]] = []
    for f in facts:
        if f["type"] != "ledger":
            continue
        out.append({"text": f.get("clause") or f["text"], "ref": f["ref"]})
    return out[:MAX_ITEMS]


def build(
    kind: str = "qingshi",
    fields: dict[str, Any] | None = None,
    *,
    use_model: bool = True,
    today: date | None = None,
) -> dict[str, Any]:
    """装配一份文种稿。不落盘、不编号、不改台账。"""
    today = today or date.today()
    fields = dict(fields or {})
    kind = kind if kind in KINDS else "qingshi"
    K = KINDS[kind]
    warnings: list[str] = []

    org = str(fields.get("org") or K["org"]).strip()
    to = str(fields.get("to") or K["to"]).strip()
    subject = str(fields.get("subject") or "").strip()
    if not subject:
        warnings.append("还没写事由。标题和缘由段都要用它，先补一句「关于什么」。")

    year = today.year
    no = str(fields.get("no") or "").strip()
    if not no:
        # 不编号：序号取决于发文登记簿，系统里没有这本簿子
        no = f"{K['no_prefix']}〔{year}〕___号"
        warnings.append("文号留空：请按发文登记簿的序号补填（系统不代为编号）。")

    date_text = str(fields.get("date_text") or "").strip() or f"{today.year}年{today.month}月{today.day}日"

    facts, fw = _facts(fields.get("anchors"), today)
    warnings.extend(fw)

    allowed_nums = _nums(date_text) | _nums(subject) | _nums(to)
    allowed_units = {u for u in _UNIT_RE.findall(to + org + subject)}
    for f in facts:
        allowed_nums |= f["nums"]
        allowed_units |= {u for u in _UNIT_RE.findall(f["text"])}
    for it in fields.get("items") or []:
        if isinstance(it, dict):
            allowed_nums |= _nums(str(it.get("text") or "") + str(it.get("due") or ""))
            allowed_units |= {u for u in _UNIT_RE.findall(str(it.get("text") or ""))}

    # 事项：用户写的优先，没写就用勾选的台账事项补
    raw_items = fields.get("items")
    items: list[dict[str, str]] = []
    if isinstance(raw_items, list):
        for x in raw_items[:MAX_ITEMS]:
            if isinstance(x, dict):
                txt = str(x.get("text") or "").strip()
                due = str(x.get("due") or "").strip()
                unit = str(x.get("unit") or "").strip()
            else:
                txt, due, unit = str(x or "").strip(), "", ""
            if due:
                due_cn = _cn_date(due)
                txt = f"{txt}（{due_cn}前完成）" if txt else f"{due_cn}前完成"
            # 事项里已经写了责任单位就不再补一句「由X负责」，
            # 否则会出现「由市住建局牵头…，由市住建局负责」这种自己重复自己的句子
            if unit and unit not in txt:
                txt = f"{txt}，由{unit}负责"
            if txt:
                items.append({"text": txt, "ref": str((x or {}).get("ref") or "") if isinstance(x, dict) else ""})
    if not items:
        auto = _auto_items(fields, facts, kind)
        if auto:
            items = auto
            warnings.append(f"事项未填，已用勾选的 {len(auto)} 项台账事项代入，请核对表述。")
    if not items:
        warnings.append("还没有具体事项：请示要写清「要批什么」，通知要写清「要做什么」。")
    if kind == "qingshi" and len(items) > 3:
        warnings.append("请示应当一文一事：事项超过 3 条，建议拆成两份文。")

    # 缘由段。事项先定下来，缘由才知道哪些事实「已经说过了」——
    # 自动代入的事项已经把那句话写在正文里，缘由再引一遍就成了同一件事说两遍。
    used_refs = {it.get("ref") for it in items if it.get("ref")}
    rule_reason = _rule_reason(kind, fields, facts, skip=used_refs)
    reason = str(fields.get("reason") or "").strip()
    model = {"source": "keep", "note": "缘由段沿用你写的文字", "degraded": False}
    if not reason:
        reason = rule_reason
        model = {"source": "rule", "note": "缘由段按要素与锚点事实生成", "degraded": True}
        if use_model:
            bg = [f for f in facts if f["ref"] not in used_refs]
            got = _model_reason(kind, fields, bg, allowed_nums=allowed_nums, allowed_units=allowed_units)
            model = got
            if got.get("source") == "model" and got.get("text"):
                reason = got["text"]
                # 模型经常把「现就有关事项通知如下。」当结尾自己写上，而且用句号；
                # 还会写成「现就…作出安排。」这种同义变体。只判 endswith(join) 两种
                # 都漏：纸上会连着出现两句「现就…」，读起来像机器写的。
                # 所以先比「已经说过」，再说就把那半句摘掉、统一收口成文种自己的 join。
                stem = K["join"].rstrip("：:")
                if reason.rstrip("。；：: ").endswith(stem):
                    reason = reason.rstrip("。；：: ") + "："
                else:
                    body = reason.rstrip("。；")
                    sents = re.split(r"(?<=[。；])", body)
                    if sents and re.match(r"^(现就|现将)", sents[-1].strip()):
                        sents = sents[:-1]
                    head = "".join(sents).rstrip("。；")
                    reason = (head + "。" if head else "") + K["join"]
    elif facts and not any(f["short"] in reason for f in facts):
        warnings.append("缘由段里没有出现勾选的事实，核对一下是不是漏引了。")

    paras: list[dict[str, str]] = []
    if reason:
        paras.append({"id": "reason", "role": "reason", "text": reason})
    for i, it in enumerate(items):
        t = _item_text(kind, i, it["text"])
        if t:
            paras.append({"id": f"item-{i + 1}", "role": "item", "text": t, "ref": it.get("ref") or ""})
    require = str(fields.get("require") or "").strip()
    if kind == "tongzhi" and require:
        paras.append({"id": "require", "role": "require", "text": require.rstrip("。") + "。"})

    doc = {
        "kind": kind,
        "kind_label": K["label"],
        "org": org,
        "no": no,
        "title": f"关于{subject}的{K['word']}" if subject else f"关于〔事由待填〕的{K['word']}",
        "to": to,
        "paras": paras,
        "tail": str(fields.get("tail") or K["tail"]).strip(),
        "attach": str(fields.get("attach") or "").strip(),
        "sign": str(fields.get("sign") or org).strip(),
        "date": date_text,
        "note": (f"（联系人：{str(fields.get('contact') or '').strip() or '___'}，"
                 f"电话：{str(fields.get('tel') or '').strip() or '___'}）") if K["need_contact"] else "",
        "cc": (f"{K['cc_label']}：{str(fields.get('cc') or '').strip()}" if str(fields.get('cc') or '').strip() else ""),
        "facts": [{"type": f["type"], "ref": f["ref"], "label": f["label"], "source": f["source"]} for f in facts],
    }

    meta = {
        "kind": kind,
        "kind_label": K["label"],
        "title": doc["title"],
        "org": org,
        "no": no,
        "to": to,
        "date_text": date_text,
        "generated_at": _now().strftime("%Y-%m-%d %H:%M"),
        "anchors": doc["facts"],
        "sections": K["sections"],
        "mock": True,
    }

    verdict = review(meta, doc, today=today)
    return {
        "ok": True,
        "meta": meta,
        "doc": doc,
        "warnings": warnings,
        "model": model,
        "check": verdict,
        "markdown": markdown(meta, doc),
    }


# --------------------------------------------------------------------------
# 体例检查：出厂一遍，不等用户点审校
# --------------------------------------------------------------------------
ERROR, WARN, INFO = "error", "warn", "info"
_WEIGHT = {ERROR: 12, WARN: 5, INFO: 1}


def _issue(level: str, rule: str, where: str, msg: str, *, block: str = "", fix: str = "") -> dict[str, str]:
    # 字段名跟 proofread 对齐（detail / fix / block / where）：
    # 前端是同一块侧栏在用，两个模块给两套字段名，面板就会显示出一片空白
    return {"level": level, "rule": rule, "where": where, "detail": msg, "block": block, "fix": fix}


def review(meta: dict[str, Any], doc: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    """对一份文种稿做体例检查。只读，结论可复现。"""
    today = today or date.today()
    kind = str(doc.get("kind") or "qingshi")
    K = KINDS.get(kind) or KINDS["qingshi"]
    issues: list[dict[str, str]] = []
    paras = doc.get("paras") or []
    by_role = {p.get("role"): p for p in paras}

    # 文号
    no = str(doc.get("no") or "")
    if not re.match(r"^\S+〔\d{4}〕\d+号$", no):
        issues.append(_issue(INFO, "ws-no", "文头", f"文号「{no}」还没填序号，或格式不是「机关代字〔年份〕序号号」。",
                             fix="按发文登记簿补填；系统不代为编号。"))

    # 标题
    title = str(doc.get("title") or "")
    if "事由待填" in title:
        issues.append(_issue(ERROR, "ws-subject", "标题", "标题里的事由还是空的。",
                             fix="写清「关于什么」，标题和缘由段都要用它。"))
    if not title.endswith(K["word"]):
        issues.append(_issue(WARN, "ws-title", "标题", f"标题没有以文种「{K['word']}」结尾。",
                             fix="公文标题规范写法：关于××的" + K["word"] + "。"))

    # 主送机关
    if not str(doc.get("to") or "").strip():
        issues.append(_issue(ERROR, "ws-to", "主送机关", "没有主送机关，这份文不知道发给谁。",
                             fix="上行文写上级机关全称，下行文写受文单位范围。"))

    # 缘由段
    reason = by_role.get("reason", {}).get("text") or ""
    if not reason:
        issues.append(_issue(ERROR, "ws-reason", "缘由", "没有缘由段：上级看不出为什么要批这件事。", block="reason",
                             fix="交代背景、依据和现状，落到「现将有关事项请示如下」。"))
    elif len(reason) < REASON_MIN:
        issues.append(_issue(WARN, "ws-reason", "缘由", f"缘由段只有 {len(reason)} 字，太短，背景交代不清。",
                             block="reason", fix="补上依据（某会议、某文件）和现状（指标或台账事实）。"))
    elif len(reason) > REASON_MAX:
        issues.append(_issue(WARN, "ws-reason", "缘由", f"缘由段 {len(reason)} 字，超过一段的容量。",
                             block="reason", fix="把过程性内容压到事项里，缘由只留「为什么」。"))

    # 事项
    items = [p for p in paras if p.get("role") == "item"]
    if not items:
        issues.append(_issue(ERROR, "ws-items", "正文", "没有具体事项：请示要写清批什么，通知要写清做什么。",
                             fix="一条一句，逐条写清动作、责任单位和时限。"))
    if kind == "qingshi" and len(items) > 3:
        issues.append(_issue(WARN, "ws-many", "正文", f"请示事项 {len(items)} 条，违反「一文一事」。",
                             fix="拆成多份请示，或把同一件事的几层内容合并成一条。"))

    # 结语
    tail = str(doc.get("tail") or "")
    if not tail:
        issues.append(_issue(ERROR, "ws-tail", "结语", "没有结语，公文不成立。", fix=f"补上「{K['tail']}」。"))
    elif kind == "qingshi" and "请" not in tail:
        issues.append(_issue(WARN, "ws-tail", "结语", f"请示的结语要提出请求，现在是「{tail}」。",
                             fix="改为「妥否，请批示。」或「以上请示，请予批复。」"))

    # 请示必须留联系人
    if K["need_contact"]:
        note = str(doc.get("note") or "")
        if "___" in note or not note:
            issues.append(_issue(WARN, "ws-contact", "附注", "请示要在成文日期下方注明联系人和电话。",
                                 fix="填上承办人姓名和座机，上级批复前会先电话核实。"))

    # 日期
    date_text = str(doc.get("date") or "")
    if not re.match(r"^\d{4}年\d{1,2}月\d{1,2}日$", date_text):
        issues.append(_issue(WARN, "ws-date", "成文日期", f"成文日期「{date_text}」不是「2026年11月7日」的写法。",
                             fix="用汉字年月日，不写 2026-11-07。"))
    else:
        m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$", date_text)
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d > today:
                issues.append(_issue(WARN, "ws-date", "成文日期", f"成文日期 {date_text} 在今天之后。",
                                     fix="签发日不能早于起草日，也不能写到未来。"))
        except ValueError:
            issues.append(_issue(ERROR, "ws-date", "成文日期", f"成文日期「{date_text}」不是一个存在的日期。"))

    # 数字与单位：能溯源吗
    fact_nums: set[str] = set()
    for p in paras:
        fact_nums |= _nums(str(p.get("text") or ""))
    fact_nums |= _nums(date_text) | _nums(no)
    allowed_units = {u for u in _UNIT_RE.findall(str(doc.get("org") or "") + str(doc.get("to") or "")
                                                 + str(doc.get("sign") or ""))}
    for p in paras:
        allowed_units |= {u for u in _UNIT_RE.findall(str(p.get("text") or ""))}

    for p in paras:
        text = str(p.get("text") or "")
        if not text:
            continue
        stray = [u for u in _UNIT_RE.findall(text) if u not in allowed_units]
        # 只报一次，同一个单位不重复刷
        if stray:
            issues.append(_issue(WARN, "ws-unit", "正文", f"提到了未经核对单位名称「{stray[0]}」。",
                                 block=str(p.get("id") or ""), fix="核对单位全称，或从台账里选对应事项。"))
            allowed_units |= set(stray)
        for c in CLICHES:
            if c in text:
                issues.append(_issue(WARN, "ws-cliche", "正文", f"用了套话「{c}」，删掉不影响任何信息。",
                                     block=str(p.get("id") or ""), fix="改成谁在什么时间做什么。"))
                break

    # 抄送：请示一般不抄送下级
    if kind == "qingshi" and doc.get("cc"):
        issues.append(_issue(INFO, "ws-cc", "抄送", "请示一般只报主送机关，抄送范围放宽容易引起口径不一。"))
    if kind == "tongzhi" and not doc.get("cc"):
        issues.append(_issue(INFO, "ws-cc", "抄送", "通知没有抄送。涉及其他部门的，按惯例抄送。"))
    if kind == "tongzhi" and not any(p.get("role") == "require" for p in paras):
        issues.append(_issue(INFO, "ws-require", "正文",
                             "通知没有单列「工作要求」：分头落实的事，一般要写明报送对象和时限。",
                             fix="补一段，写清谁在什么时间把什么报给谁。"))

    tally = {ERROR: 0, WARN: 0, INFO: 0}
    for it in issues:
        tally[it["level"]] += 1
    penalty = sum(_WEIGHT.get(it["level"], 0) for it in issues)
    verdict = "block" if tally[ERROR] else ("review" if tally[WARN] else "pass")
    return {
        "ok": True,
        "verdict": verdict,
        "verdict_text": {"block": "有必须改的问题，暂不能出手",
                         "review": "体例上没有硬错，但有需要斟酌的表述",
                         "pass": "未发现问题，可以出手"}[verdict],
        "score": max(0, 100 - penalty),
        "summary": {"total": len(issues), "error": tally[ERROR], "warn": tally[WARN], "info": tally[INFO],
                    "numbers": len(fact_nums), "untraced": 0, "rows": 0, "rows_traced": 0},
        "issues": issues,
        "checked": {"kind": kind, "paras": len(paras), "at": today.isoformat()},
    }


# --------------------------------------------------------------------------
# 出口：Markdown / docx
# --------------------------------------------------------------------------
def markdown(meta: dict[str, Any], doc: dict[str, Any]) -> str:
    """导出用。公文不套 Markdown 标题层级——它是一张纸，不是一篇文章。"""
    L: list[str] = [f"# {doc['title']}", ""]
    L.append(f"**发文机关**：{doc['org']}　　**文号**：{doc['no']}　　**生成时间**：{meta['generated_at']}")
    if doc.get("facts"):
        # 只写出处，不写「ledger:DB2026-0027」这种内部编号——
        # 导出的稿子要能直接给领导看，机器用的 id 留在接口里
        L.append("**依据**：" + "；".join(
            f["label"] + (f"（{f['source']}）" if f.get("source") else "") for f in doc["facts"]))
    L.append("")
    L.append(f"**{doc['to']}：**")
    L.append("")
    for p in doc.get("paras") or []:
        L.append(p["text"])
        L.append("")
    L.append(doc.get("tail") or "")
    L.append("")
    if doc.get("attach"):
        L.extend([doc["attach"], ""])
    L.append(doc.get("sign") or "")
    L.append(doc.get("date") or "")
    if doc.get("note"):
        L.extend(["", doc["note"]])
    if doc.get("cc"):
        L.extend(["", doc["cc"]])
    L.append("")
    if meta.get("mock"):
        L.append("> 演示数据：本稿的事实取自内置样例库，未接入真实数据源，不可直接签发。")
    return "\n".join(L).rstrip() + "\n"


def filename(meta: dict[str, Any]) -> str:
    day = str(meta.get("generated_at") or "").replace("-", "")[:8] or "draft"
    name = re.sub(r"[\\/:*?\"<>|]", "", str(meta.get("title") or "公文"))
    return f"{name}-{day}.docx"


def to_docx(meta: dict[str, Any], doc: dict[str, Any]) -> bytes:
    """按 GB/T 9704 的红头格式排版。

    和纸面共用同一份 doc 数据：文号、事项、结语都直接取，不重新装配——
    重新装配会让纸面和文件的措辞对不上，而这份稿子是要打印出来送签的。
    """
    d = dg.Doc(title=str(doc.get("title") or ""), author=str(doc.get("org") or ""))
    d.red_head(str(doc.get("org") or ""))
    d.para(str(doc.get("no") or ""), align="center", indent=False, after=180)
    d.red_line()
    d.title(str(doc.get("title") or ""))
    d.para(f"{doc.get('to') or ''}：", indent=False)
    for p in doc.get("paras") or []:
        d.para(str(p.get("text") or ""))
    if doc.get("tail"):
        d.para(str(doc["tail"]))
    if doc.get("attach"):
        for i, line in enumerate(str(doc["attach"]).splitlines()):
            d.para(line.strip(), indent=False, before=(120 if i == 0 else 0))
    d.blank()
    d.right(str(doc.get("sign") or ""))
    d.right(str(doc.get("date") or ""))
    if doc.get("note"):
        # 附注在成文日期下方、左空二字，用括号包起来——这是请示的必备项
        d.para(str(doc["note"]), before=120)
    if doc.get("cc"):
        d.para(str(doc["cc"]), indent=False, before=240)
    return d.build()


def options() -> dict[str, Any]:
    """给前端填表用：文种、默认值、可勾选的锚点。"""
    items = ov.list_items()
    return {
        "ok": True,
        "city": CITY,
        "kinds": [
            {
                "id": K["id"], "label": K["label"], "hint": K["hint"],
                "org": K["org"], "to": K["to"], "no_prefix": K["no_prefix"],
                "tail": K["tail"], "need_contact": K["need_contact"],
                "sections": list(K["sections"]),
            }
            for K in KINDS.values()
        ],
        "anchors": {
            "ledger": [
                {"ref": it["id"], "label": it["title"], "sub": f"{it.get('assignee') or ''}　{it.get('risk') or ''}"}
                for it in items
            ],
            "metrics": [{"ref": m["code"], "label": m["name"], "sub": m.get("department") or ""}
                        for m in md.list_metrics()],
        },
        "max_items": MAX_ITEMS,
        "max_anchors": MAX_ANCHORS,
        "mock": True,
    }
