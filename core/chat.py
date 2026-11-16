"""会话存储：thread + 消息流，供多轮对话使用。

结构照抄 octop 的骨架（一条 thread 头 + 消息按 `seq` 单调递增追加），但刻意简化：

- **没有 checkpoint / projection 双来源**。octop 那套是为了兼容老数据的迁移产物，
  新项目只有一份存储，不需要 cursor 和回填状态。
- **流式过程中不写库**，一轮结束后一次性 append（user + assistant 两条）。
  octop 也是这个策略：几百次小写入除了拖慢磁盘没别的好处。

## 回灌策略：完整原文（对齐 octop）

`context()` 把 user 和 assistant 的 `content` **原样**回灌，包括上一轮答案里的
数值。这与 octop 一致：多轮对话里模型看到的是自己上一轮真正说过的话，而不是
一句「我取值了，数值见界面」的模板复述 —— 后者信息密度低到几乎不产生指代价值。

代价必须写在明处：**历史里从此有数字了**。防线因此上移到输出侧：

- `experts` 的数字闸按「可溯源」放行：数值清单里的数、或写了算式且等式成立的推算值
  才留下，其余整段丢弃（`numgate.audit`）；
- planner 只允许输出 JSON 计划，没有正文出口，取数由程序执行 —— 模型决定查什么，
  不决定数是多少。

也就是说：数字可以出现在**新生成的结论**里，但每一个都要能追到接口返回的那份清单。

老数据、拒答、中止这类没有正文的消息才退回 `_summary()` 的模板（不含数值）——
它们本来就没有可回灌的原文。

同一问可以留几版答案（重答 / 重试，见「答案分支」一节），但**只有当前选中
的那一版回灌**：两版都进去，模型会看到自己对同一个问题说过两个数，下一轮
追问「刚才那个」时不知道该顺哪个 —— 这一点比省 token 重要得多。
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any

import store

FILE = "chats"
VERSION = 1
MAX_TITLE = 24
# 一个会话最多挂几份材料。一份材料上限 1.2 万字，八份已经能把 8k 窗口吃掉大半，
# 再多就不是「参考资料」而是「把文件塞给模型」了。
MAX_MATERIALS = 8

# ── token 估算 ────────────────────────────────────────────────────────
#
# 以前是 `len × 0.6`，一个系数包打天下。对纯中文勉强够用（BPE 里汉字约
# 1 字符 = 0.62 token），但用户粘一段英文材料、或问句里带几个指标 code 时，
# 按汉字价算英文会高估两倍多 —— 本来能带上的轮次被白扔掉，而界面上只显示
# 「前 N 轮已裁掉」，看不出是我们算贵了。
#
# 系数对齐 octop 的 context_usage.py（那边还多一档 JSON，这里用不上：
# 进上下文的是问句和回答原文，没有 tool schema 那种密集结构）。
_CJK = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\uac00-\ud7af]"
)
_CJK_CHARS_PER_TOKEN = 1.6     # 汉字 / 假名 / 谚文：约 1 字符 0.62 token
_LATIN_CHARS_PER_TOKEN = 4.0   # 拉丁字母与符号：约 4 字符 1 token

# 每条消息还有角色标记与分隔符，chat template 都会加。20 条消息就是 80 token ——
# 不记的话每一轮都在悄悄超支。
_MESSAGE_FRAMING_TOKENS = 4

# ── 上下文预算 ────────────────────────────────────────────────────────
#
# 以前写死 600。这个数是拍的，而且错得有点离谱：光 system prompt 加指标目录
# 本身就有 1100 token（`llm.system_prompt_text()` 实测），历史预算比系统提示
# 还小 —— 模型窗口九成以上空着，却在第 10 轮就开始丢上下文。
#
# 现在按「模型窗口 × 比例」算，与 octop 同构（那边也是窗口比例，只是把 85%
# 用作压缩触发线）。窗口来自 modelcfg 的 `context_window`，没填就用兜底值。
#
# 兜底值取 8192 而不是 128k：政务内网基本是本地部署的小模型，8k / 16k / 32k
# 常见，128k 罕见。而且这个方向上的错是不对称的 —— **高估会把 prompt 撑爆，
# 请求直接被服务端拒掉；低估只是少带几轮历史**。所以兜底宁小勿大，窗口真的
# 大就在模型配置里把 `context_window` 填上。
_FALLBACK_WINDOW = 8_192

# 历史最多占窗口的这个比例，剩下留给 system prompt、输出和估算误差。
# octop 在 85% 处触发压缩回收；这里还没有压缩，取一半更稳 ——
# 压缩落地后再把这条线抬到 0.85。
_HISTORY_RATIO = 0.5

# 兜底下限：再小的窗口也至少给这么多，否则一轮问答都放不下。
_MIN_BUDGET = 600

# 参考材料最多占窗口的这个比例。材料放在 user 消息里，和历史抢同一个窗口，
# 所以它不是一个独立额度，算历史预算时要先扣掉（见 `history_budget`）。
_MATERIAL_SHARE = 0.25

# 会话搜索最多给几条。多到几十条时用户也不会往下翻，不如让他改词。
_SEARCH_LIMIT = 40


def _est_tokens(text: str) -> int:
    """按字符类型分档粗估 token 数。

    不追求准（真值只有 tokenizer 知道），追求「分类之后不系统性偏高或偏低」：
    同一个系数套中英文，必然对其中一种错得离谱。
    """
    s = str(text or "")
    if not s:
        return 1
    cjk = len(_CJK.findall(s))
    other = len(s) - cjk
    return max(1, int(cjk / _CJK_CHARS_PER_TOKEN + other / _LATIN_CHARS_PER_TOKEN))


def model_window(window: int | None = None) -> int:
    """把调用方递进来的窗口规格化：没填、为 0 或非法，一律用兜底值。

    单独成函数是为了让「兜底是多少」只有一处定义 —— 附件裁剪和预算计算都要
    知道这个数，两边各写一个 8192，改的时候必漏一处。
    """
    win = int(window) if isinstance(window, (int, float)) and window and window > 0 else 0
    return win or _FALLBACK_WINDOW


def history_budget(window: int | None = None, reserved: int = 0) -> int:
    """把模型窗口换算成本轮可用的历史预算。

    策略集中在这一处，调用方只负责把 modelcfg 里的 `context_window` 递进来 ——
    否则每个调用点各写一个数字，又会退回「拍一个数」的老问题。

    `reserved` 是本轮已经被别的东西占掉的额度（目前只有参考材料）。材料放在
    user 消息里，和历史抢的是同一个窗口，不扣就会「历史算着没超、合起来超了」。
    """
    total = max(_MIN_BUDGET, int(model_window(window) * _HISTORY_RATIO))
    return max(_MIN_BUDGET, total - max(0, int(reserved)))


def clip_materials(
    items: list[dict[str, Any]], window: int | None = None
) -> tuple[list[dict[str, Any]], int]:
    """按模型窗口裁剪本轮参考材料，返回 (裁剪后的材料, 估算 token)。

    `app/main.py` 那层已经按「单份 1.2 万字、合计 3 万字」截过一遍，但那是
    **请求体安全线**，跟模型是谁无关。真正的约束是窗口：3 万字中文约 1.9 万
    token，在 8k 的内网小模型上，一份材料单独就能把窗口撑爆。

    撑爆的后果还特别隐蔽：请求失败会被当成普通模型故障，走「重试 → 换模型 →
    退回规则解析」，用户只看到答案忽然变粗糙，不知道是自己传的文件太大。
    所以这里必须按窗口截，而不是按字符数截。
    """
    if not items:
        return [], 0

    cap = max(400, int(model_window(window) * _MATERIAL_SHARE))
    kept: list[dict[str, Any]] = []
    used = 0
    for item in items:
        text = str(item.get("text") or "")
        cost = _est_tokens(text)
        if used + cost > cap:
            if kept:
                break
            # 单份就超额度：按比例截开头，而不是整份丢掉 —— 材料的前半段
            # 通常才是正文，后半段多是附表。
            keep = max(200, int(len(text) * cap / max(1, cost)))
            text = text[:keep]
            item = {**item, "text": text, "chars": len(text), "truncated": True}
            cost = _est_tokens(text)
        kept.append(item)
        used += cost
    return kept, used


def _now() -> int:
    return int(time.time())


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "threads": []}


def _cfg() -> dict[str, Any]:
    raw = store.read(FILE)
    if not isinstance(raw, dict) or not isinstance(raw.get("threads"), list):
        return _empty()
    raw.setdefault("version", VERSION)
    return raw


def _find(cfg: dict[str, Any], thread_id: str) -> dict[str, Any] | None:
    return next((t for t in cfg["threads"] if t["id"] == thread_id), None)


# ── 读 ──────────────────────────────────────────────────────────────────


def _snippet(msgs: list[dict[str, Any]], needle: str, width: int = 44) -> str | None:
    """命中位置前后各截一段。搜索要回答的是"为什么这条会出来"，
    只给标题的话，按内容命中的结果看起来像是乱入。"""
    for msg in msgs:
        text = str(msg.get("content") or "")
        pos = text.lower().find(needle)
        if pos < 0:
            continue
        start = max(0, pos - width // 2)
        end = min(len(text), pos + len(needle) + width // 2)
        return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")
    return None


def list_threads(query: str | None = None, owner: str | None = None) -> list[dict[str, Any]]:
    """列表只回头部信息，不把全部消息带上（一个长会话能到几十 KB）。

    带 `query` 时在标题和全部消息正文里找，命中返回一条 `snippet`。
    为什么不在前端过滤：前端只有 last_preview（最后一条消息前 60 字），
    搜"上次开会定了什么"这种按内容找的，必然搜不到。

    `owner` 给定时只回该身份的会话；传 None 表示不过滤（内部统计用）。
    **默认必须传**：以前不按身份过滤，切到副市长看到的还是市长那一屏会话。
    """
    needle = str(query or "").strip().lower()
    owner_key = None if owner is None else str(owner).strip()
    items = []
    for t in _cfg()["threads"]:
        if owner_key is not None and (t.get("owner") or "") != owner_key:
            continue
        msgs = t.get("messages") or []
        title = t.get("title") or "未命名对话"
        snippet = None
        if needle:
            if needle in title.lower():
                snippet = None
            else:
                snippet = _snippet(msgs, needle)
                if snippet is None:
                    continue
        # 预览和条数按「屏幕上能看到的」算，不按库存 —— 同一问多留了两版答案，
        # 侧栏条数就莫名多两条、预览还是那版没在显示的，用户会以为会话错了。
        vis = visible(t)
        last = vis[-1] if vis else None
        item = {
            "id": t["id"],
            "title": title,
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "model_ref": t.get("model_ref"),
            "message_count": len(vis) or len(msgs),
            "last_preview": (last.get("content") or "")[:60] if last else "",
            "last_role": last.get("role") if last else None,
        }
        if needle:
            item["snippet"] = snippet
        items.append(item)
    items.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return items[:_SEARCH_LIMIT] if needle else items


def get(thread_id: str, owner: str | None = None) -> dict[str, Any] | None:
    """按 id 取会话。给了 `owner` 时，不属于他的会话一律当「不存在」。

    返回 404 而不是 403：403 等于告诉对方「这个 id 是真的，只是不给你看」，
    拿 id 逐个试就能探出别人的会话数量。会话 id 是随机的，但没必要给这个口子。
    """
    found = _find(_cfg(), thread_id)
    if found is None:
        return None
    if owner is not None and (found.get("owner") or "") != str(owner).strip():
        return None
    return found


# ── 写 ──────────────────────────────────────────────────────────────────


def create(
    title: str | None = None,
    model_ref: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    thread = {
        "id": f"th_{secrets.token_hex(6)}",
        "title": (title or "").strip()[:MAX_TITLE],
        # 归属。空串只可能出现在历史数据里（见 own_orphans），新建一律带身份
        "owner": str(owner or "").strip(),
        "created_at": _now(),
        "updated_at": _now(),
        "model_ref": model_ref,
        "messages": [],
    }
    if thread["title"]:
        # 建会话时就带了标题（导入、外部调用）：当成用户起的，不被后续自动标题覆盖
        thread["title_src"] = "user"

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["threads"].append(thread)
        return cfg

    store.update(FILE, _do, _empty())
    return thread


def ensure(
    thread_id: str | None,
    model_ref: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """有就用，没有（或已被删、或不属于这个人）就新建。接口层直接用这个，省一次判空。

    「不属于自己」也走新建而不是报错：切换身份后前端可能还揣着上一个人的
    thread_id，报错只会让用户莫名其妙。但**不能**静默复用它 —— 那等于把
    别人的会话接着写下去。
    """
    if thread_id:
        found = get(thread_id, owner=owner)
        if found:
            return found
    return create(model_ref=model_ref, owner=owner)


def own_orphans(owner: str) -> int:
    """把还没有 `owner` 字段的老会话一次性划给某个身份，返回改动条数。

    为什么不"无主 = 所有人可见"：那等于隔离没做。为什么不直接删：用户的会话
    不是垃圾，删了没法解释。这批数据本来就是演示阶段大家共用的，归给默认身份
    是唯一不丢数据、又能让隔离真正生效的做法。

    只认「字段缺失」，不认空串：空串是显式写入的值，不该每启动一次就被重新认领。
    """
    changed = {"n": 0}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        for t in cfg["threads"]:
            if "owner" not in t:
                t["owner"] = str(owner or "").strip()
                changed["n"] += 1
        return cfg

    store.update(FILE, _do, _empty())
    return changed["n"]


# ── 会话标题 ──────────────────────────────────────────────────────────
#
# 标题有两层：发送那一刻先用「问句前 24 字」顶上（侧栏立刻有字，不用等），
# 首轮答完再换成一句真正的标题。第二层**不额外调模型** —— 答案里的
# `metric_names` / `period_label` 就是这一轮到底在说什么，用它拼比让模型
# 再读一遍问题更准，也少一次调用、少一份延迟。
#
# 为什么值得换：原话里一半是套话（「请帮我看看今年 9 月……是多少」），
# 十个会话在同一屏里全是「请帮我看看」，侧栏等于没有标题。
#
# 三种来源按优先级往上换，换上去就不往回退：
#   auto（问句截断） → derived（清掉套话的问句） → metric（指标 + 期次） → user
# `user` 是用户手改的，永远最高，任何自动逻辑都不许覆盖它。
_TITLE_RANK = {"auto": 0, "derived": 1, "metric": 2, "user": 9}

# 句首套话：只认完整词组，不删单字「请」——「请市政府协调……」删掉就成了另一个人
# 在说话。这类词在问句里只占字数、不带信息。
_SOFT_HEAD = (
    "请帮我看看", "请帮我", "麻烦帮我", "帮我看看", "帮我", "帮忙",
    "请问一下", "请问", "想问一下", "想问", "想了解", "看一下", "看看",
    "查一下", "查查", "给我看看", "给我", "告诉我", "能不能", "可以帮",
)
# 句尾：长的排前面，逐轮剥（「是多少呢」要先剥「是多少」再剥「呢」）
_SOFT_TAIL = (
    "是多少呢", "是多少", "多少", "是啥", "是什么", "怎么样", "如何",
    "什么情况", "吗", "呢", "？", "?", "。", "！", "!",
)


def _clean_question(text: str) -> str:
    """问句 → 标题：去掉句首套话、句尾问法，压掉多余空白。"""
    raw = str(text or "")
    s = re.sub(r"\s+", " ", raw).strip()
    changed = True
    while changed:
        changed = False
        for word in _SOFT_HEAD:
            if s.startswith(word) and len(s) > len(word) + 1:
                s = s[len(word):].lstrip("，,、 ")
                changed = True
                break
    for _ in range(3):
        hit = False
        for word in _SOFT_TAIL:
            if s.endswith(word) and len(s) > len(word) + 2:
                s = s[: -len(word)].rstrip("，,、 ")
                hit = True
                break
        if not hit:
            break
    s = s.strip()
    # 「今年 1-9 月」这种数字两边带空格的写法，标题里读着断：抹成「1-9月」
    s = re.sub(r"(?<=\d)\s+(?=[月年日季])", "", s)
    s = re.sub(r"(?<=[年月日季])\s+(?=\d)", "", s)
    # 剥到只剩两三个字就说明剥过头了（「你好」被剥成「你」），退回原话
    if len(s) < 3:
        s = re.sub(r"\s+", " ", raw).strip()
    return s[:MAX_TITLE]


# 指标名里人人都懂的缩写。只收「删掉不会改变含义」的几个 —— 「规模以上」写成
# 「规上」是这个系统自己在答案里一直在用的说法，不会让标题变难懂。
_NAME_ABBR = (("规模以上", "规上"),)


def _short_name(name: str) -> str:
    s = str(name or "").strip()
    for long, short in _NAME_ABBR:
        s = s.replace(long, short)
    return s


def _short_period(label: str) -> str:
    """期次写成标题里的样子：今年的省掉年份（「2026年9月」→「9月」）。"""
    s = str(label or "").strip()
    if not s:
        return ""
    s = re.sub(rf"^{time.localtime().tm_year}年", "", s)
    return s[:9]


def derive_title(
    question: str,
    meta: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """按这一轮实际查到的东西起标题，返回 (标题, 来源)。

    有指标就把「期次 + 指标名」放前面：十个会话都问同一个指标时，区分它们的
    只有期次。查不到指标（闲聊、知识库、拒答）才退回清理过的问句。
    """
    meta = meta or {}
    names = [str(n) for n in (meta.get("metric_names") or []) if str(n).strip()]
    intent = str(meta.get("intent") or "")
    # 问口径的那一轮不带期次，「规上工业增加值增速」这个标题会把「问的是口径」
    # 这层意思抹掉 —— 这种就退回清理过的问句（「规上工业增加值的口径」）。
    if names and intent != "definition":
        head = "、".join(_short_name(n) for n in names[:2])
        if len(names) > 2:
            head += f" 等 {len(names)} 项"
        period = _short_period(str(meta.get("period_label") or ""))
        text = f"{period} · {head}" if period else head
        return text[:MAX_TITLE], "metric"
    return _clean_question(question), "derived"


def _maybe_retitle(thread: dict[str, Any], msgs: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    """把标题往上升一级（auto → derived → metric），降不回去。"""
    first = next((m for m in msgs if m.get("role") == "user"), None)
    if first is None:
        return
    question = str(first.get("content") or "")
    cur = str(thread.get("title_src") or "")
    if not cur:
        # 老数据没有这个字段：只有当标题仍是首问截断时才认作自动生成，
        # 否则可能已经被用户改过名 —— 覆盖用户起的名字比标题难看严重得多。
        raw = re.sub(r"\s+", " ", question).strip()[:MAX_TITLE]
        cur = "auto" if thread.get("title") in (raw, _clean_question(question)) else "user"
    better, src = derive_title(question, meta)
    if better and _TITLE_RANK.get(src, 0) > _TITLE_RANK.get(cur, 0):
        thread["title"] = better
        thread["title_src"] = src


# ── 答案分支：同一问可以有几版答案 ─────────────────────────────────────
#
# 以前「重答」是删掉上一条（`drop_last`）。删掉是错的：那一轮花掉的模型调用、
# 试过的步骤、以及「用户按了停止」这件事本身都是记录，删了就没有对照可看，
# 而用户想比较的恰恰是「上一版和这一版哪个对」。
#
# 现在一条 assistant 消息记 `reply_to`（它回答的那条 user 消息 id），同一
# `reply_to` 下的若干条就是同一问的几版；`thread["picks"]` 记住当前显示哪一版。
# **只有被选中的那一版进模型上下文** —— 两版都进去，模型会看到自己对同一个
# 问题说了两个数，下一轮就不知道该顺着哪个说。
#
# 刻意不做的两件事：
#
# 1. **不做「从中间某轮分叉」**（改掉第 2 轮再往下答）。第 3 轮的回答是照着
#    第 2 轮写的，换掉第 2 轮之后第 3 轮就自相矛盾 —— 要么一起作废、要么逐轮
#    重算。所以只允许**最后一轮**重答或换版（`pick_answer` 里判死）。
# 2. **不把消息组织成树**。仍是 append-only 的扁平数组，`seq` 单调，导出、
#    搜索、审计、收藏夹都不必改。树能表达的东西这里用不上，而代价是每个读
#    消息的地方都要换成递归。
#
# 老数据没有 `reply_to`：按位置推断（挂到前面最近那条 user 上）。那正是老
# 代码的回灌顺序，所以升级前后上下文一字不差。
MAX_ANSWERS = 6


def _last_user_id(msgs: list[dict[str, Any]]) -> str | None:
    for msg in reversed(msgs):
        if msg.get("role") == "user":
            return str(msg.get("id") or "") or None
    return None


def _turns_of(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """把扁平消息折成轮次：`[{user, answers, pick}]`。

    只在内存里折，**不落库** —— 落一份「轮次表」就得和 messages 保持同步，
    迟早不一致，而不一致的表现是「点某一版没反应」，还查不出哪一步错的。

    没有 `reply_to` 的消息挂到最后一轮；连一轮都没有（凭空来的一条 assistant）
    就自成一轮 —— 那种消息没有「同一问」可言，也就没有分支。
    """
    picks = thread.get("picks")
    picks = picks if isinstance(picks, dict) else {}
    turns: list[dict[str, Any]] = []
    by_user: dict[str, dict[str, Any]] = {}
    for msg in thread.get("messages") or []:
        role = msg.get("role")
        if role == "user":
            turn = {"user": msg, "answers": [], "pick": None}
            turns.append(turn)
            by_user[str(msg.get("id") or "")] = turn
            continue
        if role != "assistant":
            continue
        key = str(msg.get("reply_to") or "")
        turn = by_user.get(key) if key else None
        if turn is None:
            if not turns or turns[-1].get("user") is None:
                turns.append({"user": None, "answers": [], "pick": None})
            turn = turns[-1]
        turn["answers"].append(msg)

    for turn in turns:
        user = turn.get("user")
        want = str(picks.get(str((user or {}).get("id") or "")) or "")
        hit = next(
            (a for a in turn["answers"] if str(a.get("id") or "") == want), None
        ) if want else None
        # picks 指向一条已经被删掉的答案（会话被手改过、或裁剪过）：退回最后
        # 一版，不报错也不清空 —— 宁可显示"某一版"，不能显示"这一轮没有答案"。
        turn["pick"] = hit or (turn["answers"][-1] if turn["answers"] else None)
    return turns


def _remember_pick(thread: dict[str, Any], msg: dict[str, Any]) -> None:
    """新答案默认显示。顺手把这一轮超过 `MAX_ANSWERS` 的最旧一版丢掉。

    为什么要封顶：界面只给前后切换，留到第八版之后，最早那几版已经不可能被
    翻到，而它们每一次都要跟着 `chats.json` 整份读写一遍 —— 多试几次答法，
    之后每一轮问答都变慢。丢的是最旧的、且一定不是刚选中的那一版。
    """
    rid = str(msg.get("reply_to") or "")
    picks = thread.get("picks")
    picks = picks if isinstance(picks, dict) else {}
    picks[rid] = str(msg.get("id"))
    thread["picks"] = picks

    same = [
        m for m in thread.get("messages") or [] if str(m.get("reply_to") or "") == rid
    ]
    if len(same) > MAX_ANSWERS:
        for old in same[: len(same) - MAX_ANSWERS]:
            thread["messages"] = [
                m for m in thread.get("messages") or [] if m is not old
            ]


def visible(thread: dict[str, Any] | None) -> list[dict[str, Any]]:
    """这一屏该显示的消息：每条问句 + 它当前选中的那一版答案。

    接口层用它回放 —— 把没选中的几版一起发给前端，界面要么重复画（同一问
    两条答案），要么自己再实现一遍选择逻辑（两份逻辑必然走偏）。

    多于两版时给那一条塞 `alts = {n, i, ids}`（共几版、这是第几版、各版 id），
    界面据此画「‹ 2/3 ›」。带 `ids` 是为了切换时不必再问一次"同问的另外几版
    是谁" —— 一个 `pick` 请求就够，少一次往返，也就少一处可能对不上的状态。
    **只在一版时不塞**：一版的题占着位置画个 1/1 是纯噪音。
    """
    if not thread:
        return []
    out: list[dict[str, Any]] = []
    for turn in _turns_of(thread):
        user = turn.get("user")
        if user:
            out.append(user)
        pick = turn.get("pick")
        if pick is None:
            continue
        answers = turn["answers"]
        if user and len(answers) > 1:
            idx = next((i for i, a in enumerate(answers) if a is pick), len(answers) - 1)
            pick = {
                **pick,
                "alts": {
                    "n": len(answers),
                    "i": idx + 1,
                    "ids": [str(a.get("id") or "") for a in answers],
                },
            }
        out.append(pick)
    return out


def last_turn(thread: dict[str, Any] | None) -> dict[str, Any] | None:
    """最后一轮（`{user, answers, pick}`）。重答/重试都只作用在它上面。"""
    turns = _turns_of(thread or {})
    return turns[-1] if turns else None


def pick_answer(
    thread_id: str,
    message_id: str,
    owner: str | None = None,
) -> dict[str, Any] | None:
    """把某一轮显示的那一版换成 `message_id`，返回换完之后的落点。

    换来换去只换「显示和上下文用哪一版」，一条消息都不删 —— 用户看过的那一版
    随时能切回去，这是他敢去点「换一版」的前提。

    不属于会话、不是答案、不在最后一轮，一律返回 None（接口层给 404）。中间轮
    不给换：换掉它，后面几轮就是照着另一版答案说的，整段历史自相矛盾。这条路
    正常走不到（界面只在最后一轮画切换钮），所以返回错误比返回 403 更合适 ——
    用户没做错什么。
    """
    if get(thread_id, owner=owner) is None:
        return None
    got: dict[str, Any] = {}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is None:
            return cfg
        turns = _turns_of(thread)
        if not turns:
            return cfg
        last = turns[-1]
        user = last.get("user")
        if user is None:
            return cfg
        hit = next(
            (a for a in last["answers"] if str(a.get("id") or "") == str(message_id)),
            None,
        )
        if hit is None:
            return cfg
        picks = thread.get("picks")
        picks = picks if isinstance(picks, dict) else {}
        picks[str(user.get("id") or "")] = str(hit.get("id"))
        thread["picks"] = picks
        got.update(
            ok=True,
            message_id=hit.get("id"),
            reply_to=user.get("id"),
            n=len(last["answers"]),
            i=last["answers"].index(hit) + 1,
        )
        return cfg

    store.update(FILE, _do, _empty())
    return got or None


def append(
    thread_id: str,
    role: str,
    content: str,
    meta: dict[str, Any] | None = None,
    reply_to: str | None = None,
) -> dict[str, Any] | None:
    """追加一条消息，seq 取 max+1。线程不存在返回 None。

    `reply_to` 只对 assistant 有意义：它回答的是哪一条问句。同一 `reply_to`
    下的多条就是同一问的几版答案（见下面的「答案分支」）。不给就取前面最近
    那条 user —— 老调用方（测试、脚本、别的入口）不必知道这个字段存在。
    """
    added: dict[str, Any] = {}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is None:
            return cfg
        msgs = thread.setdefault("messages", [])
        seq = max((m.get("seq") or 0 for m in msgs), default=0) + 1
        msg = {
            "id": f"m_{secrets.token_hex(6)}",
            "seq": seq,
            "role": role,
            "content": content,
            "ts": _now(),
        }
        if role == "assistant":
            rid = str(reply_to or _last_user_id(msgs) or "")
            if rid:
                msg["reply_to"] = rid
        if meta:
            msg["meta"] = meta
        msgs.append(msg)
        thread["updated_at"] = msg["ts"]
        if msg.get("reply_to"):
            _remember_pick(thread, msg)
        # 首条用户消息先顶一个标题，省一个「请命名」的步骤。这时候还没有答案，
        # 只能拿问句凑；首轮答完会换成真标题（见下面 derive_title）。
        if role == "user" and not thread.get("title"):
            thread["title"] = _clean_question(content)
            thread["title_src"] = "auto"
        # 首轮答完：用这一轮实际查到的指标和期次重起一个标题。不额外调模型 ——
        # 这些字段本来就在 meta 里（见 app/main.py 的 _persist_answer）。
        if role == "assistant" and meta:
            _maybe_retitle(thread, msgs, meta)
        added.update(msg)
        return cfg

    store.update(FILE, _do, _empty())
    return added or None


def rename(thread_id: str, title: str, owner: str | None = None) -> bool:
    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is not None:
            thread["title"] = title.strip()[:MAX_TITLE]
            # 用户亲手起的名字是终态：自动派生到此为止，后面轮次一律不许覆盖
            thread["title_src"] = "user"
        return cfg

    if get(thread_id, owner=owner) is None:
        return False
    store.update(FILE, _do, _empty())
    return True


def attach_materials(
    thread_id: str,
    add: list[str] | None = None,
    drop: list[str] | None = None,
) -> list[str]:
    """把材料 id 记到会话上（或从会话上摘掉），返回记完之后的清单。

    清单里只有 id，正文在 `core/materials.py` 里按 id 取 —— 一份一万多字的材料
    要是塞进 chats.json，每一轮上下文都会把它重复带一遍，几十轮之后光历史就能
    把窗口吃干净。

    封顶 `MAX_MATERIALS`，超了摘最旧的而不是拒绝新的：用户刚传的那份显然比
    上周那份更该在。摘掉只是不挂在会话上，材料本身还在库里（可能别的会话在用）。
    """
    gone = {str(x) for x in (drop or []) if x}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is None:
            return cfg
        cur = [str(x) for x in (thread.get("material_ids") or []) if x]
        cur = [x for x in cur if x not in gone]
        for mid in add or []:
            key = str(mid or "")
            if key and key not in cur:
                cur.append(key)
        cur = cur[-MAX_MATERIALS:]
        if cur != (thread.get("material_ids") or []):
            thread["material_ids"] = cur
        return cfg

    store.update(FILE, _do, _empty())
    thread = get(thread_id)
    return [str(x) for x in ((thread or {}).get("material_ids") or [])]


def delete(thread_id: str, owner: str | None = None) -> bool:
    if get(thread_id, owner=owner) is None:
        return False

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["threads"] = [t for t in cfg["threads"] if t["id"] != thread_id]
        return cfg

    store.update(FILE, _do, _empty())
    return get(thread_id) is None


def set_message_meta(thread_id: str, message_id: str, patch: dict[str, Any]) -> bool:
    """给已经落库的一条消息补字段（合并，不覆盖已有键）。

    干什么用的：领导在对话里说一句话，系统出一张拟办卡，人点「下发」之后才建出
    令。那一刻模型已经收工了，但**「这句话派生了哪条令」必须记在这条消息上** ——
    否则回放时只能拿原文去比字符串，同一句话说两遍就分不清哪张卡对应哪条令。
    所以下发成功后回写一个 `directive_id`，让消息和令之间是硬链接。
    """
    found = {"ok": False}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is None:
            return cfg
        for m in thread.get("messages") or []:
            if m.get("id") != message_id:
                continue
            meta = m.get("meta")
            if not isinstance(meta, dict):
                meta = {}
            meta.update(patch or {})
            m["meta"] = meta
            thread["updated_at"] = _now()
            found["ok"] = True
            break
        return cfg

    store.update(FILE, _do, _empty())
    return found["ok"]


def set_model_ref(thread_id: str, model_ref: str | None) -> None:
    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is not None:
            thread["model_ref"] = model_ref
        return cfg

    store.update(FILE, _do, _empty())


def clear_model_refs(prefix: str) -> int:
    """删除 provider 后，把会话里指向它的 model_ref 置空。返回改动条数。"""
    changed = {"n": 0}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        for t in cfg["threads"]:
            ref = t.get("model_ref")
            if ref and ref.startswith(prefix):
                t["model_ref"] = None
                changed["n"] += 1
        return cfg

    store.update(FILE, _do, _empty())
    return changed["n"]


# ── 给模型用的上下文 ────────────────────────────────────────────────────


def _summary(meta: dict[str, Any] | None) -> str:
    """把一条没有正文的回答压成一句不含数值的摘要。

    **这是兜底，不是主路径**。有 `content` 的消息一律回灌原文（见 `context`），
    只有老数据、空 headline 这类拿不到正文的消息才走这里。它仍然不带数值，
    因为唯一能保证「这些字没有数字」的办法是模板化 —— 而它现在只剩这一职责。
    """
    if not meta:
        return "（上一轮没有可用的摘要）"

    if meta.get("cancelled"):
        return "上一轮我按用户要求中止了，没有给出答案。"

    if meta.get("refused"):
        return "上一轮我拒答了：问题对应的指标未注册。"

    # 建会草稿不是一次问答：它没有数值也没有指标，硬套下面的模板会得到
    # 「我meet，但没有命中任何已注册指标」——一句在中文里根本不成立的话。
    if meta.get("meet_draft"):
        d = meta["meet_draft"]
        return f"上一轮我在约会议（{d.get('title') or '未定议题'}，{d.get('held_at') or '未定时间'}），只出了草稿，等用户确认。"

    names = meta.get("metric_names") or []
    intent = meta.get("intent") or "value"
    intent_cn = {
        "value": "取值",
        "trend": "看走势",
        "breakdown": "看分解",
        "compare": "做对比",
        "definition": "问口径",
    }.get(intent, intent)

    if not names:
        return f"上一轮我{intent_cn}，但没有命中任何已注册指标。"
    return f"上一轮我{intent_cn}，用的是这些已注册指标：{'、'.join(names)}。具体数值见界面，不在此重复。"


# ── 外部数据对照的回灌 ──────────────────────────────────────────────────

# 一段摘要最多列几个指标、每个指标最多列几行差异。再多就退化成把整张表塞回
# prompt —— 而回灌的目的只是让模型听懂「那份表」「第三期」指的是什么。
_DIGEST_METRICS = 4
_DIGEST_ROWS = 2
# 「问到的这一期」放宽到 6 个指标：用户是点名问的，漏掉他问的那一个比多带
# 两行摘要更糟。这是这一期唯一会展开的地方，行数还是被期次卡住的。
_ASKED_METRICS = 6


def _row_label(row: dict[str, Any]) -> str:
    return str(row.get("label") or row.get("period") or "—")


# 问句里的期次：「9 月」「2026年9月」「2026-09」。
# 会话层不引 metrics 的 parse_period —— 那会让 chat 反向依赖 metrics，而摘要的
# 全部字段都只取自落库的 meta.external，就是为了守住这条线。这里只用 rows 里
# 已有的 period 做字面比对，够用，也不新增依赖。
_PERIOD_RE = re.compile(r"^(\d{4})-(\d{1,2})$")
_ASKED_ISO = re.compile(r"(?<!\d)(20\d{2})\s*[-/.]\s*(\d{1,2})(?!\d)")
# 「3 个月」是时长不是期次，所以排除前面带「个」的
_ASKED_MONTH = re.compile(r"(?:(20\d{2})\s*年\s*)?(?<![个\d])(\d{1,2})\s*月")


def _asked_periods(query: str) -> list[tuple[int | None, int]]:
    """问句里被问到的期次，最多两个（「9 月比 8 月」就到顶了，再多是罗列）。"""
    if not query:
        return []
    got: list[tuple[int | None, int]] = []
    for m in _ASKED_ISO.finditer(query):
        got.append((int(m.group(1)), int(m.group(2))))
    for m in _ASKED_MONTH.finditer(query):
        month = int(m.group(2))
        if 1 <= month <= 12:
            got.append((int(m.group(1)) if m.group(1) else None, month))
    out: list[tuple[int | None, int]] = []
    for item in got:
        if item not in out:
            out.append(item)
    return out[:2]


def _month_text(year: int | None, month: int) -> str:
    return f"{year}年{month}月" if year else f"{month}月"


def _cell_of_period(rows: list[Any], year: int | None, month: int) -> dict[str, Any] | None:
    """在一个指标的对照行里找被问到的那一期。

    只说了「9 月」没说年份时取最近的一期 —— 跨年时 rows 里会同时有 2025-09
    和 2026-09，猜前面那个不是保守，是答错。
    """
    best: dict[str, Any] | None = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        m = _PERIOD_RE.match(str(r.get("period") or ""))
        if not m:
            continue
        py, pmo = int(m.group(1)), int(m.group(2))
        if pmo != month or (year is not None and py != year):
            continue
        if best is None or str(r.get("period")) > str(best.get("period")):
            best = r
    return best


def _asked_lines(metrics: list[dict[str, Any]], asked: list[tuple[int | None, int]]) -> list[str]:
    """把「用户问到的那一期」按指标列出来 —— 一致的那几期原本不在摘要里。

    摘要只列差异期，所以「表里 9 月写的是多少」这种问法，碰上一致的期次就没有
    落点。这里按期次补一遍，而且两个数都给（表 / 库）：让模型分得清「表里这么
    写」和「系统这么算」—— 这正是对照这件事本身的意义。
    """
    periods = [
        str(r.get("period"))
        for m in metrics
        for r in (m.get("rows") or [])
        if isinstance(r, dict) and _PERIOD_RE.match(str(r.get("period") or ""))
    ]
    if not periods:
        return []
    # 覆盖范围的上下界用中文期次说，和正文摘要同一个写法 —— 同一段话里
    # 一半「2026年7月」一半「2026-07」，读起来像两个来源拼的。
    labels = {
        str(r.get("period")): _row_label(r)
        for m in metrics
        for r in (m.get("rows") or [])
        if isinstance(r, dict)
    }
    lo, hi = min(periods), max(periods)
    span = f"{labels.get(lo, lo)}–{labels.get(hi, hi)}"
    out: list[str] = []
    for year, month in asked:
        bits: list[str] = []
        label = ""
        for m in metrics[:_ASKED_METRICS]:
            r = _cell_of_period(m.get("rows") or [], year, month)
            if not r:
                continue
            label = label or _row_label(r)
            name = str(m.get("name") or m.get("code") or "指标")
            unit = str(m.get("unit") or "")
            tag = {"same": "一致", "diff": "不一致", "check": "口径待确认"}.get(str(r.get("state")), "")
            bits.append(f"{name} 表 {r.get('external')}{unit} / 库 {r.get('internal')}{unit}（{tag}）")
        if bits:
            out.append(f"{label}：" + "；".join(bits))
        else:
            # 说清是「表里没有」还是「超出对照范围」：后者不是表的错，是只留了
            # 最近几期，用户据此知道要不要重新附一次材料。
            out.append(f"{_month_text(year, month)}：上一轮的对照里没有这一期（只对照了 {span}）")
    return out


def external_digest(ext: dict[str, Any] | None, query: str = "") -> str:
    """把一轮「外部数据 × 指标库」的对照结果压成可回灌的一段话。

    **为什么需要它**：B 档边界规定 headline / facts 里的数只能来自指标库，
    所以外部表的数值和「对上了几期」的结论只活在界面的对照块里，不进正文。
    后果是下一轮问「哪期差最多」「表里那期写的是多少」时，历史里既没有表内
    数字也没有对照结论，模型只能答「没有材料」—— 文件像是白传了。这段摘要
    补的就是这个落点。

    **为什么数字可以出现在这里**：它只喂 `llm.plan`（意图解析），正文由模板
    装配（见 core/metrics/ask.py 的 headline），模型没有路径把这些数字写进
    答案。摘要里的字段全部取自落库时已经定稿的 meta.external，这里不重算、
    不读指标库，所以会话层不会反向依赖 metrics。

    **`query` 是这一轮的追问句**：给了它，就把句子里问到的期次单独列一段，
    连**一致**的那几期一起给。不给也能用 —— 那就只列差异期，和以前一样。
    条件是「问到了才列」：把最近 8 期全量写进来是 1000 多 token，而摘要的
    全部价值就在于它便宜。
    """
    if not isinstance(ext, dict) or not ext:
        return ""

    files = [str(f) for f in (ext.get("files") or []) if f]
    metrics = [m for m in (ext.get("metrics") or []) if isinstance(m, dict)]
    lines = ["【上一轮的外部数据对照（系统计算，非模型陈述）】"]
    if files:
        lines.append("文件：" + "、".join(files[:3]))

    if not metrics:
        # 「没对上」也要回灌：否则用户追问时，模型不知道自己那轮其实什么都没对上，
        # 会把「没有对照」说成「没有这张表」。
        why = "；".join(str(n) for n in (ext.get("notes") or [])[:2] if n)
        tail = f"（{why}）" if why else ""
        lines.append("结果：上一轮附件里没有能和指标库逐期对上的数据。" + tail)
        lines.append("（上一轮确实没对上，不要据此编造对照。）")
        return "\n".join(lines)

    for m in metrics[:_DIGEST_METRICS]:
        rows = [r for r in (m.get("rows") or []) if isinstance(r, dict)]
        if not rows:
            continue
        name = str(m.get("name") or m.get("code") or "指标")
        unit = str(m.get("unit") or "")
        same = [r for r in rows if r.get("state") == "same"]
        diff = [r for r in rows if r.get("state") in ("diff", "check")]
        span = (
            f"{_row_label(rows[0])}–{_row_label(rows[-1])} "
            if len(rows) > 1
            else f"{_row_label(rows[0])} "
        )
        counts = f"{len(rows)} 期对照，{len(same)} 期一致"
        if diff:
            counts += f"、{len(diff)} 期不一致"
        bits = [f"{span}{counts}"]
        for r in diff[:_DIGEST_ROWS]:
            tag = "口径待确认" if r.get("state") == "check" else "不一致"
            bits.append(
                f"{_row_label(r)} {tag}（库 {r.get('internal')}{unit} / 表 {r.get('external')}{unit}）"
            )
        lines.append(f"- {name}：" + "；".join(bits))

    rest = len(metrics) - _DIGEST_METRICS
    if rest > 0:
        lines.append(f"- 另有 {rest} 个指标也做了对照，未在此列出。")

    # 问到哪一期就把那一期补上。只补被问到的，不铺全表 —— 6 个指标 × 8 期
    # 铺开是一千多 token，而这段摘要的全部价值就是它便宜。
    asked = _asked_periods(query)
    if asked:
        asked_lines = _asked_lines(metrics, asked)
        if asked_lines:
            lines.append("")
            lines.append("【问到的这一期】")
            lines.extend(asked_lines)

    lines.append("以上是上一轮已经算好的结论；取新的数仍然查指标库，不要从这段话里编数字。")
    return "\n".join(lines)


def context(
    thread_id: str | None,
    token_budget: int | None = None,
    max_turns: int = 200,
    out: dict[str, Any] | None = None,
    *,
    window: int | None = None,
    system_text: str = "",
    reserved: int = 0,
    drop_tail: int = 0,
) -> list[dict[str, str]]:
    """取历史对话，转成 OpenAI messages 格式，供意图解析参考指代。

    **回灌的是原文**：assistant 的 `content` 一字不改地进上下文，包括里面的
    数值。这是对齐 octop 的做法 —— 模型看到的是自己上一轮真正说过的话，
    指代（「那上个月呢」「再拆开看」）才有可解析的落点。模板摘要那套已经
    退成兜底，只用于没有正文的老数据。

    **预算按「模型窗口 × 比例」算**（`history_budget`），不按条数、也不再
    写死 600。`window` 由调用方从 modelcfg 的 `context_window` 递进来；
    不传就用兜底窗口。`max_turns` 默认放到 200 —— 真正起约束的是预算，
    轮数上限只是防死循环的保险丝（octop 那边根本不设轮数）。

    `out` 给定时回填本轮的分段统计（总轮数 / 用了多少 / 裁掉多少 / 系统段 /
    窗口）。界面要把这件事说出来：上下文被裁而用户不知道，表现上就是
    「它怎么把刚才说的忘了」，而用户唯一能做的判断是「系统不靠谱」。
    用出参而不是改返回值，是为了不动已有的调用方（同 degraded 的出参风格）。

    `drop_tail` 是从末尾摘掉几条（重答时用）：正在重答的那一轮，问句和上一版
    答案都不能进上下文 —— 模型看见自己上一版会把那套说法（含数字）复述一遍，
    而用户点「换一版」要的正是另一种答法。这几条在库里还在，只是这一轮不读。
    为什么放在这里而不是让调用方自己 pop：pop 之后 `out` 里的轮数/用量就对不上
    了，界面会显示「带入了 4 轮」而实际只有 3 轮。
    """
    def _report(**kw: Any) -> None:
        if out is not None:
            out.update(kw)

    win = model_window(window)
    # 系统段由调用方把本轮 system 文本传进来，这里用同一套分档估算 ——
    # 让调用方自己数 token，就等于把估算规则复制一份出去，迟早两边不一致。
    sys_used = _est_tokens(system_text) if system_text else 0
    # 材料占掉的额度不算在历史里，但确实占窗口，所以从预算里扣
    reserved = max(0, int(reserved or 0))
    # 空会话也要给全字段：界面按同一套键渲染，缺一个就是空白或 KeyError
    empty = dict(
        turns_total=0, turns_used=0, dropped=0, tokens=0,
        budget=history_budget(win, reserved), window=win, system=sys_used,
        reserved=reserved, total=sys_used + reserved,
    )

    if not thread_id:
        _report(**empty)
        return []
    thread = get(thread_id)
    if not thread:
        _report(**empty)
        return []

    entries: list[tuple[str, str]] = []
    # 带对照的回答落在哪一条上，按 entries 下标记。记成字典而不是就地记住
    # "最后一条"：重答要摘掉末尾那一轮（见下面的 drop_tail），摘完得能往回找。
    exts: dict[int, dict[str, Any]] = {}
    # **只回灌选中的那一版**（`_turns_of` 的 pick）。同一问的另外几版既不进
    # 上下文，也不参与 token 预算 —— 否则模型会看到自己答过两个不同的数，
    # 追问「刚才那个数」时不知道该顺哪个，而且一轮历史按两三轮的钱算。
    for turn in _turns_of(thread):
        user = turn.get("user")
        if user:
            entries.append(("user", (user.get("content") or "").strip()))
        answer = turn.get("pick")
        if answer is not None:
            # 原文回灌，数值照带。只有拿不到正文的消息（老数据、空 headline）
            # 才退回模板摘要 —— 那种消息本来就没有可回灌的内容。
            text = (answer.get("content") or "").strip()
            entries.append(("assistant", text or _summary(answer.get("meta"))))
            ext = (answer.get("meta") or {}).get("external")
            if isinstance(ext, dict):
                exts[len(entries) - 1] = ext

    # 重答时把末尾这一轮摘掉（见 `drop_tail` 的说明）。必须赶在对照摘要之前：
    # 摘要挂在"最后一条回答"上，而那条正是要被摘掉的那一轮。
    if drop_tail > 0 and entries:
        entries = entries[: max(0, len(entries) - int(drop_tail))]

    # **只认最后一条回答的对照**：更早的对照早被后面的问题顶掉了，每轮都往回
    # 灌只会让 prompt 里堆一串过期的表，还挤掉真正有用的历史。所以判的是
    # "最后一条 assistant 有没有对照"，不是"历史上有没有人对过表"——
    # 后一种写法会让新一轮明明没对表，却还挂着一张上上轮的表。
    last_at = next(
        (i for i in range(len(entries) - 1, -1, -1) if entries[i][0] == "assistant"),
        None,
    )
    last_ext = exts.get(last_at) if last_at is not None else None
    last_ext_at = last_at if last_ext else None

    # 对照结论补在上一轮回答的末尾。B 档边界把外部数字挡在正文外，历史里于是
    # 只剩「一张表对过」的残影；不补这一段，「哪期差最多」这类追问会掉进拒答。
    # 必须赶在下面切轮次之前加，让它的 token 也计入预算 —— 否则摘要会白占窗口。
    if last_ext:
        # 追问句 = 那条带对照的回答**之后**的第一条用户消息。摘要靠它把
        # 「问到的这一期」补进来 —— 「表里 9 月多少」本来就是冲着上一轮那张
        # 表问的，不带这句，一致的那几期永远没有落点。
        query = ""
        if last_ext_at is not None:
            for role, text in entries[last_ext_at + 1:]:
                if role == "user":
                    query = text
                    break
        digest = external_digest(last_ext, query)
        # 挂回**那条**带对照的回答本身，不挂"最后一条 assistant"：两者之间可能
        # 还隔着一轮（那一轮没有对照），挂错了就是给下一轮贴了上一轮的表。
        if digest and last_ext_at is not None and entries[last_ext_at][0] == "assistant":
            entries[last_ext_at] = (
                "assistant", entries[last_ext_at][1] + "\n\n" + digest
            )

    # 成对：assistant 紧跟 user 才算一轮；落单的（比如上一轮被停止）自己算一轮
    turns: list[list[tuple[str, str]]] = []
    i = len(entries) - 1
    while i >= 0:
        if entries[i][0] == "assistant" and i - 1 >= 0 and entries[i - 1][0] == "user":
            turns.append(entries[i - 1 : i + 1])
            i -= 2
        else:
            turns.append([entries[i]])
            i -= 1
    turns.reverse()

    budget = history_budget(win, reserved) if token_budget is None else int(token_budget)
    picked: list[list[tuple[str, str]]] = []
    used = 0
    for turn in reversed(turns):
        if len(picked) >= max_turns:
            break
        # framing 也要算：chat template 给每条消息加的那点开销不会因为
        # 内容短就消失，一轮两条就是 8 token。
        cost = sum(_est_tokens(text) + _MESSAGE_FRAMING_TOKENS for _, text in turn)
        # 至少带最近一轮：一句“那上个月呢”没有上一轮就完全没法解析
        if picked and used + cost > budget:
            break
        picked.append(turn)
        used += cost
    picked.reverse()

    _report(
        turns_total=len(turns),
        turns_used=len(picked),
        dropped=len(turns) - len(picked),
        tokens=used,
        budget=budget,
        window=win,
        system=sys_used,
        reserved=reserved,
        total=sys_used + reserved + used,
    )

    return [
        {"role": role, "content": text}
        for turn in picked
        for role, text in turn
    ]


def known_metrics(
    thread_id: str | None = None,
    owner: str | None = None,
    limit: int = 12,
) -> list[str]:
    """涉及过的指标名（按时间先后，去重），给 planner 的「常驻记忆」。

    历史窗口会被预算截断，但「这个指标」指谁这件事不该跟着一起被截掉。
    只取名字，不取数值 —— 与 context 的安全边界一致。

    两个来源，按优先级合并：
    1. 本会话（`thread_id` 给定时）：正在聊的上下文，最先带上；
    2. 本身份的历史会话（`owner` 给定时）：跨会话档案，解决「昨天那个工业
       增速」这种隔天指代。按 `updated_at` 从新到旧扫，新会话的指标排前面。

    `owner` 过滤天然防串味：别人的会话不会进别人的记忆。只传 thread_id 时
    行为与旧版完全一致（只回本会话），旧调用方不受影响。
    """
    def _names_of(t: dict[str, Any] | None) -> list[str]:
        out: list[str] = []
        if not t:
            return out
        for msg in reversed(t.get("messages") or []):
            meta = msg.get("meta") or {}
            for name in reversed(meta.get("metric_names") or []):
                name = str(name or "").strip()
                if name and name not in out:
                    out.append(name)
        out.reverse()
        return out

    names: list[str] = []

    if thread_id:
        names = _names_of(get(thread_id))

    if owner:
        owner_key = str(owner).strip()
        threads = sorted(
            (t for t in _cfg()["threads"] if (t.get("owner") or "") == owner_key),
            key=lambda t: t.get("updated_at") or 0,
            reverse=True,
        )
        for t in threads:
            if t.get("id") == thread_id:
                continue  # 本会话已经收过了，不重复扫
            for name in _names_of(t):
                if name not in names:
                    names.append(name)

    return names[-limit:] if limit else names


def usage_total(thread: dict[str, Any] | None) -> dict[str, int]:
    """本会话累计用量。老会话没有 meta.usage，返回全 0 而不是报错。"""
    total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for msg in (thread or {}).get("messages") or []:
        usage = (msg.get("meta") or {}).get("usage") or {}
        for key in total:
            total[key] += int(usage.get(key) or 0)
    return total


def stats(owner: str | None = None) -> dict[str, Any]:
    """会话与消息总数。带 owner 时只数这个人的 —— 界面上的「共 N 条」如果
    数的是别人的会话，切换身份后数字对不上，反而会让人以为数据丢了。"""
    cfg = _cfg()
    owner_key = None if owner is None else str(owner).strip()
    threads = [
        t for t in cfg["threads"] if owner_key is None or (t.get("owner") or "") == owner_key
    ]
    return {
        "threads": len(threads),
        "messages": sum(len(t.get("messages") or []) for t in threads),
    }
