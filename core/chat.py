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

- `experts` 的数字闸仍丢弃任何含数值的解读（`_has_forbidden_number`）；
- planner 只允许输出 JSON 计划，没有正文出口，数值由 `mockdata` 返回后由程序
  拼装，模型碰不到。

也就是说：数字出现在**上下文**里是接受的，出现在**新生成的结论**里不行。

老数据、拒答、中止这类没有正文的消息才退回 `_summary()` 的模板（不含数值）——
它们本来就没有可回灌的原文。
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
        last = msgs[-1] if msgs else None
        item = {
            "id": t["id"],
            "title": title,
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "model_ref": t.get("model_ref"),
            "message_count": len(msgs),
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


def append(
    thread_id: str,
    role: str,
    content: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """追加一条消息，seq 取 max+1。线程不存在返回 None。"""
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
        if meta:
            msg["meta"] = meta
        msgs.append(msg)
        thread["updated_at"] = msg["ts"]
        # 首条用户消息自动当标题，省一个「请命名」的步骤
        if role == "user" and not thread.get("title"):
            thread["title"] = content.strip().replace("\n", " ")[:MAX_TITLE]
        added.update(msg)
        return cfg

    store.update(FILE, _do, _empty())
    return added or None


def rename(thread_id: str, title: str, owner: str | None = None) -> bool:
    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is not None:
            thread["title"] = title.strip()[:MAX_TITLE]
        return cfg

    if get(thread_id, owner=owner) is None:
        return False
    store.update(FILE, _do, _empty())
    return True


def delete(thread_id: str, owner: str | None = None) -> bool:
    if get(thread_id, owner=owner) is None:
        return False

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["threads"] = [t for t in cfg["threads"] if t["id"] != thread_id]
        return cfg

    store.update(FILE, _do, _empty())
    return get(thread_id) is None


def drop_last(thread_id: str) -> dict[str, Any] | None:
    """删掉最后一条消息，返回被删的那条。

    只给"重试"用：上一轮被用户停掉时落了一句「已停止生成」占位，重新问同一句时
    这条占位应当被真正的答案顶掉 —— 留着它，会话里就会出现
    问 → 已停止 → 问 → 答案，同一句话连着问了两遍。
    """
    removed: dict[str, Any] = {}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is None:
            return cfg
        msgs = thread.get("messages") or []
        if msgs:
            removed.update(msgs.pop())
            thread["updated_at"] = _now()
        return cfg

    store.update(FILE, _do, _empty())
    return removed or None


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


def _row_label(row: dict[str, Any]) -> str:
    return str(row.get("label") or row.get("period") or "—")


def external_digest(ext: dict[str, Any] | None) -> str:
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
    # 只认**最后一条**带对照的回答：更早的对照早被后面的问题顶掉了，每轮都
    # 往回灌只会让 prompt 里堆一串过期的表，还挤掉真正有用的历史。
    last_ext: dict[str, Any] | None = None
    for msg in thread.get("messages") or []:
        role = msg.get("role")
        if role == "user":
            entries.append(("user", (msg.get("content") or "").strip()))
        elif role == "assistant":
            # 原文回灌，数值照带。只有拿不到正文的消息（老数据、空 headline）
            # 才退回模板摘要 —— 那种消息本来就没有可回灌的内容。
            text = (msg.get("content") or "").strip()
            entries.append(("assistant", text or _summary(msg.get("meta"))))
            ext = (msg.get("meta") or {}).get("external")
            last_ext = ext if isinstance(ext, dict) else None

    # 对照结论补在上一轮回答的末尾。B 档边界把外部数字挡在正文外，历史里于是
    # 只剩「一张表对过」的残影；不补这一段，「哪期差最多」这类追问会掉进拒答。
    # 必须赶在下面切轮次之前加，让它的 token 也计入预算 —— 否则摘要会白占窗口。
    # 挂到「最后一条 assistant」而不是 entries[-1]：调用方有可能已经先把本轮
    # 问句落库了（重试、脚本、以后的别的入口），那时末尾是 user，判 entries[-1]
    # 就会静默不回灌 —— 这种错在界面上完全看不出来。
    if last_ext:
        digest = external_digest(last_ext)
        if digest:
            for i in range(len(entries) - 1, -1, -1):
                if entries[i][0] == "assistant":
                    entries[i] = ("assistant", entries[i][1] + "\n\n" + digest)
                    break

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
