"""对话里的寒暄：打招呼、问身份、问能力、道谢。

## 为什么需要这一层

问数流水线只有两条出口：命中指标 → 取数并带出处；命中知识库/资料库 → 原文片段。
两者都不中就走拒答（`ask.py` 的 `refuse` 分支）。但对话框里的第一句常常是
「你好」或「你能干什么」—— 按拒答处理，等于在门口把人拦下，还附一份他还没
准备好读的指标清单。那不是安全边界，是体验故障：系统答不上来的其实是
「我是谁」，而它明明知道自己是谁。

## 三条约束

1. **不调模型。** 寒暄是有限集合，规则足够。本地 27B（见 `run-llm.cmd`）一次
   推理 2-5 秒，为一句「你好」跑一遍，用户只会以为系统卡了；而且这句话里
   没有任何需要理解的东西。
2. **整句匹配，不做子串匹配。** 「你好，9 月工业增速多少」既有寒暄又有实质
   问题，必须整句交给问数链路。子串匹配会让「你好」把真问题抢走 —— 这和
   `meetplan` 里「上次开会定了什么」被误判成建会是同一类错，而且更隐蔽：
   用户会得到一段热情的能力介绍，而他问的数一个没答，界面上还一切正常。
3. **能力清单只写系统真有的。** 这张卡是用户建立预期的第一处。写一句做不到
   的事，后面每一次交互都在还这笔债。

## 与拒答的分工

这里只处理「整句就是寒暄」。凡是带了实质问题的（哪怕只多一句「顺便问下
9 月财政」），一律不认，交回原链路 —— 包括交回拒答。宁可让一句混合句走
完整流水线多花几秒，也不要把半句话的答案当成全部。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "core", ROOT / "core" / "metrics"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import mockdata as md  # noqa: E402

# 超过这个长度肯定不是单纯寒暄，直接不认 —— 省掉正则回溯，也防住
# 「把一大段材料开头加个『你好』」这种绕法。
_MAX_CHARS = 40

# 标点与空白不计入判定：「你好！」「你 好」「Hello!」都是同一个意思。
_PUNCT = re.compile(
    r"[\s，。！？、；：,.!?;:~～·…\-—_()（）\[\]【】\"'“”‘’《》<>]+"
)

_GREET = r"你好|您好|嗨|哈喽|哈啰|hello|hi|hey|早上好|上午好|中午好|下午好|晚上好|在吗|在不在|有人吗"
_SELF = (
    r"你是谁|你是哪位|您是哪位|你叫什么(名字)?|你是什么|你是干什么的"
    r"|你是(不是)?(机器人|ai|人工智能)"
    r"|你是什么模型|你用的什么模型|你背后是什么模型"
)
_CAP = (
    r"你(都)?(能|会)(做|干)(什么|啥)|你会什么|你能帮我(做|看|查|干)?(什么|啥)"
    r"|你能给我(做|干)?(什么|啥)|你有什么(功能|能力|用)|你可以(做|干)(什么|啥)"
    r"|(有什么|有哪些)(功能|能力)|可以(做|干)(什么|啥)"
    r"|怎么用|如何使用|使用说明|能查(什么|啥)|能问(什么|啥)|帮助|help"
)
_THANKS = r"谢谢(你|您)?|多谢|感谢|thanks?|thankyou|thx"
_BYE = r"再见|拜拜|bye|goodbye"
_OK = r"好的|好|嗯|哦"

# 整句结构：可有可无的礼貌前缀 + 一个寒暄核心 + 可有可无的尾缀。
# 关键是首尾都锚死（`^...$`），中间不许夹带别的字 —— 这就是「不抢问数」的实现。
#
# 前缀与尾缀拆成两个变量：它们的内容几乎一样，但要分开维护 —— 尾缀里的
# 「呀 / 啊 / 吗 / 你」这类语气词放在句首会变成灾难（「啊 9 月工业多少」）。
_POLITE = rf"请问|请问一下|问一下|我想问一下|那个|这里|这儿|都|{_OK}"
_TAIL = rf"{_SELF}|{_CAP}|{_GREET}|{_THANKS}|{_BYE}|请问|这里|这儿|都|{_OK}"
_ENDINGS = r"呀|啊|呢|吧|哦|哈|啦|嘛|哟|吗|你|您|的"

_CHITCHAT = re.compile(
    rf"^(?:{_POLITE})*(?:{_SELF}|{_CAP}|{_GREET}|{_THANKS}|{_BYE}|{_OK})"
    rf"(?:(?:{_TAIL})|(?:{_ENDINGS}))*$",
    re.IGNORECASE,
)

# 兜底防线：句子里只要出现任何一个已注册指标的别名，就不是寒暄。
# 整句匹配其实已经排除了这种句子，这条是给「以后有人放宽了上面的正则」准备的 ——
# 抢走问数的代价（用户以为系统答了）比漏判寒暄的代价（多花几秒）大得多。
_ALIASES = tuple(
    alias for meta in md.METRICS.values() for alias in meta["aliases"] if len(alias) >= 2
)

# 能力说明用的提问入口。挑三个不同领域的指标，让「不知道从哪开始」的人
# 有个具体落点；取不到就跳过，绝不写死一个可能已被删掉的 code。
_STARTER_CODES = ("GY_VA_GROWTH", "TZ_12345_CLOSURE", "FAI_PROGRESS")


def _norm(question: str) -> str:
    return _PUNCT.sub("", str(question or "")).strip()


def kind(question: str) -> str:
    """这句寒暄属于哪一类，返回 hello / self / cap / thanks / bye / ""。

    顺序即优先级：「你好，你能做什么」既有问候又有能力询问，要给能力清单 ——
    用户真正想知道的是后半句。同理「谢谢」排在最后，它最短也最容易被误吞。
    """
    s = _norm(question)
    if not s or len(s) > _MAX_CHARS:
        return ""
    if _CHITCHAT.fullmatch(s) is None:
        return ""
    if any(alias in s for alias in _ALIASES):
        return ""
    low = s.lower()
    for name, pattern in (
        ("cap", _CAP),
        ("self", _SELF),
        ("bye", _BYE),
        ("thanks", _THANKS),
        ("hello", _GREET),
    ):
        if re.search(pattern, low, re.IGNORECASE):
            return name
    # 只说了「好的 / 嗯」：整句能匹配上，但没有任何一类可以归 —— 当成一句
    # 轻量的确认，回一句「随时说」就够了，不必假装他问了什么。
    return "ok"


def wants(question: str) -> bool:
    """这句话是不是「整句就是寒暄」。"""
    return bool(kind(question))


# --------------------------------------------------------------------------
# 能力说明
# --------------------------------------------------------------------------
# 五类能力对应代码里真实存在的五条路。改这里之前先确认那条路还在：
#   ask.py 取数 / knowledge.py 知识库 / rag.py 资料库 / experts.py 解读 /
#   meetplan.py 建会草稿
_CAP_ROWS = (
    ("问数", "取值 / 走势 / 分解 / 对比 / 口径"),
    ("查记录", "督办、会议纪要与议定事项"),
    ("查材料", "你上传文件里的原文段落"),
    ("听解读", "参谋从各自角度点评，不报数"),
    ("约会议", "一句话出草稿，确认才落库"),
)

_HEADS = {
    "hello": "你好。问数、查记录、查材料、听解读、约会议，这五件事都能做。",
    "cap": "能做的事分五类，都在这张卡上。",
    "self": "我是这个工作台的问数助手。生成文字的是本机部署的模型，数值不经过它。",
    "thanks": "不客气。",
    "bye": "好，随时回来。",
    "ok": "嗯。有要查的随时说。",
}

_DETAILS = {
    "hello": "只答系统里有记录的东西 —— 对不上的会直说没有，不凭印象估算。",
    "cap": "只答系统里有记录的东西；对不上的会直说没有，并列出能问的指标。",
    "self": "模型只负责把你的话映射成「问哪个指标」，数字由指标接口返回，"
            "每条都带部门、报表和期次，所以能追到出处。",
    "thanks": "有要查的随时说。",
    "bye": "",
    "ok": "",
}


def _model_hint() -> str:
    """当前有没有可用模型。寒暄是唯一能便宜地说出这件事的地方 ——
    用户问「你能干什么」时，模型没连上也是「能干什么」的一部分。"""
    try:
        import modelcfg  # noqa: PLC0415 —— 延迟导入：只在这条分支上才需要

        return "已接（只用于理解意图）" if modelcfg.available() else "未接（问数不受影响）"
    except Exception:  # noqa: BLE001 —— 配置读坏了不该让寒暄跟着挂
        return "状态未知"


def _starters() -> list[dict[str, str]]:
    return [
        {"code": c, "name": md.METRICS[c]["name"], "department": md.METRICS[c]["department"]}
        for c in _STARTER_CODES
        if c in md.METRICS
    ]


def as_result(question: str) -> dict[str, Any]:
    """包成 `ask()` 的返回形状 —— 前端只认这一套字段。

    `refused` 必须是 False：这不是「答不上来」，是答案本身就是「我能做什么」。
    设成 True 会让上层去走 harness 兜底（`main.py:_harness_fallback` 只在
    refused 时触发），一句「你好」被转发给一个插件引擎，属于莫名其妙。
    """
    k = kind(question) or "ok"

    facts: list[dict[str, str]] = []
    if k in ("hello", "cap"):
        facts = [{"label": label, "value": value} for label, value in _CAP_ROWS]
        facts.append({"label": "本地模型", "value": _model_hint()})
    elif k == "self":
        facts = [
            {"label": "本地模型", "value": _model_hint()},
            {"label": "数字来源", "value": "指标接口，不由模型生成"},
            {"label": "答不了时", "value": "明说没有，并列出可问指标"},
        ]

    return {
        "refused": False,
        "question": question,
        "planner": "rules",
        "intent": "chitchat",
        # 给上层的标记：这轮没有指标，专家解读没有可解读的内容，不该调模型。
        "chitchat": True,
        "chitchat_kind": k,
        "headline": _HEADS.get(k, _HEADS["ok"]),
        "detail": _DETAILS.get(k, ""),
        "facts": facts,
        "sources": [],
        "notes": [],
        # 只在「不知道问什么」的那一刻给入口。回「谢谢」还推三个指标，
        # 像客服在硬转销售。
        "suggestions": _starters() if k in ("hello", "cap", "self") else [],
        "trace": [
            {
                "step": 1,
                "tool": "chitchat.match",
                "call": f"chitchat.match(question) → {k}",
                "status": "ok",
                "summary": "整句是寒暄或能力询问，直接回能力说明；没调模型，也没查指标",
            }
        ],
    }
