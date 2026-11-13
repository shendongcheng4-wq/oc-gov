"""可召唤的专家团：角色卡 + 一段「怎么看」的解读。

**专家不参与取数。** 数值、口径、出处仍然由 `core/metrics/ask.py` 走指标接口
拼出来；专家只在这之后再补一段「怎么看」。这是刻意划的线：如果让专家自己去查数，
同一场会里经济参谋说 8.9%、民生参谋说 9.1%，谁也说不清该信谁 —— 而数字只能有
一个来源，就是指标接口。

所以这里有一道数字闸（`numgate.audit`）：**解读里的每个数都要能追到本轮清单** ——
接口返回的现值 / 同比 / 环比 / 目标直接放行；由它们算出来的数必须把算式写进正文，
等式成立才留下。追不到就整段丢弃并注明原因（不是「修正」）。指标名和记录名里本来
就带的数字（「12345 群众诉求按期办结率」）先剔掉再校验，免得冤枉。

召唤多个专家 = 同一个问题、同一组数字，各自给一段角度不同的看法。**不做合并总结**：
合并必须再调一次模型，而那次调用不会产生任何新信息，只多一次编数字的机会。

**专家名单是可配置的。** 每位专家是一份 Markdown 人设卡，放在项目根 `experts/<id>/expert.md`
（frontmatter 存身份与域，正文写「看问题的方式」），可入库、可分发、业务方自己写。
内置默认是这七位的落地文件；`data/experts.json` 是**旧格式**，仅当 `experts/` 目录
不存在时回退读取，读到后自动迁成 MD。可在专家管理面板里增删改。用户自建的专家没有
`domain` 字段也能正常用 —— 只是拿不到域内依据，退回「只看方向信号」。

**专家读知识库，但只读自己域内的那几条。** 域写在每位专家的 `domain` 里
（指标代码 + 责任单位 + 分类库名），由 `core/knowledge.index(person, domain)`
收窄；分类库那一段由 `ask._expert_refs` 换成 `rag.search` 的库 id 后单独查。
读进来的是原文摘录，参谋可以据此判断好坏，但**仍然不许复述里面的数字** ——
数字只有一个来源，这条线没动。知识库补的是「判断的素材」，不是「报数的权限」。
域内一条都没有时如实写「本域内无相关记录」，不拿别域的记录凑数：那等于让经济参谋
去评价河道巡查，说得越像越糟。

模型没配好时不让整条问答失败：专家段标注「未配置模型」，数值答案照常返回。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import llmclient  # noqa: E402
import numgate  # noqa: E402

# 一次会诊最多召唤几位。这是**并发成本上限**（一位 = 一次模型调用、一份 token），
# 与 EXPERTS 里有几位专家无关 —— 专家名单可以扩到十几个，但一次别全叫上。
# 接口的 max 和后端的截断都引用这一个常量，别在别处再写死数字。
MAX_PER_CALL = 3

# 总参谋的固定 id。它不绑域，职责是「收口」：等分管参谋说完，再归类、挑重点。
# 单独选中它时由 `expand_for_chief` 展开成全部分管参谋 + 它自己（排最后）。
CHIEF_ID = "chief"


def cap_for(ids: list[str], cap: int | None = None) -> int:
    """这一轮最多叫几位参谋。

    默认仍是 MAX_PER_CALL —— 一次会诊不该变成刷模型。但总参谋在场时放开：
    它本身就是被 `expand_for_chief` 展开出来的，再按 3 砍掉，收口就只剩半张桌子，
    总参谋会对着三位没说过话的人做「综合」。其余情况（含团模式）保持原上限。

    调用方显式给 `cap` 时以调用方为准，便于测试与将来的名单扩容。
    """
    if cap is not None:
        return cap
    return len(ids) if CHIEF_ID in ids else MAX_PER_CALL


# 专家名单的**主存储**是项目根 experts/<id>/expert.md（可入库、可分发）。
# data/experts.json 是旧格式：仅在 experts/ 目录不存在时回退读取，读到后自动迁成 MD。
# 内置默认（_DEFAULT_EXPERTS）是兜底中的兜底，正常情况由上面的 MD 落地文件承担。
EXPERTS_DIR = ROOT / "experts"

# 旧格式持久化文件（遗留回退 + 迁移来源）。
EXPERTS_FILE = ROOT / "data" / "experts.json"

# 判定 frontmatter 起始：第一行是 `---` 且后面能找到闭合的 `---` 才算有 frontmatter。
# 没有 frontmatter 的 md 读进来是空身份，会被下面当「坏专家」跳过，不至于把正文当名字。
_FM_DELIM = "---"

# 每位专家最多带几条知识库记录进 prompt。
# 定 4 是按 token 算的：一条正文截 180 字，4 条 ≈ 720 字，加上指标信号和角色说明
# 仍在 1200 字以内。带再多，会议原文那种几千字的记录会把指标信号挤到无关紧要的位置。
REF_LIMIT = 4

# 每条记录的正文截多长。会议原文动辄数千字，整段塞进去只会淹没方向信号。
# 引句（quote）优先于正文（text）：议定事项的 quote 是纪要原句，督办条目的 quote
# 是最后一笔办理经过 —— 都比「拼装出来的摘要」更有信息量，也更难被误读。
REF_CHARS = 180

# 内置默认名单：总参谋统筹不绑域，六位板块参谋各管自己那摊。
# `domain.codes` 是这位参谋管的指标代码（一个指标只给一个参谋，否则口径会被
# 两个域各引一半）；`domain.units` 是责任单位全称（事项/会议按它归属，
# 简称由 knowledge.unit_variants 派生，两边都手写迟早分叉）；`domain.bases`
# 是他挂的上传分类库（存库名，查询时换 id）。总参谋**不绑域**：不绑域在
# `knowledge.in_domain` 里就是「不限」，所以他读全域的台账与会议 —— 他是
# 「先定看什么」的角色，手上没有全域记录就只能空谈。指标口径仍按代码归
# 各位分管参谋，他不另占一份。
_DEFAULT_EXPERTS: tuple[dict[str, Any], ...] = (
    {
        "id": "chief",
        "name": "总参谋",
        "avatar": "总",
        "brief": "统筹五大板块，先定看什么",
        "focus": "定板块、定主次、定风险点",
        "angle": (
            "先把问题归到经济运行、招商引资、重大项目、社会治理、民生保障、生态环境里的某一块，"
            "判断这是总量问题还是结构问题；再从分管参谋给的依据里挑一两个最该盯的风险点，"
            "说清先看什么、下一步找谁。不逐个罗列，也不替分管参谋下结论。"
        ),
    },
    {
        "id": "economy",
        "name": "经济运行参谋",
        "avatar": "经",
        "brief": "精通全市经济数值与局势",
        "focus": "工业、投资、财政、市场主体",
        "angle": (
            "先看总量和增速跟年度节奏合不合拍，再看结构里有没有拖后腿的单项：工业靠什么撑、"
            "投资靠谁拉、财政收入有没有靠一次性因素、市场主体是在增还是在减。"
            "总量达标但结构失衡时，先点结构，不急着说好。"
        ),
        "domain": {
            "codes": ["GY_VA_GROWTH", "GDP_TOTAL", "BUDGET_REV", "SS_ENTERPRISE"],
            "units": ["市统计局", "市财政局", "市市场监督管理局"],
            # 营商环境类政策（市场主体、审批服务）由经济运行参谋借来看，
            # 它管的 SS_ENTERPRISE 就在这个库里；库粒度拆不开，先整库挂。
            "bases": ["政务热线与营商环境"],
        },
    },
    {
        "id": "investment",
        "name": "招商引资参谋",
        "avatar": "商",
        "brief": "熟悉在谈项目与签约落地",
        "focus": "在谈项目、签约落地、开工转化",
        "angle": (
            "盯签约到落地、落地到开工这两段转化：签约多不等于落地多，落地慢往往卡在用地、审批、资金。"
            "看项目体量和产业方向配不配，警惕「签约热闹、开工冷清」，也警惕一个大项目撑住全年。"
        ),
        "domain": {
            "codes": ["INVEST_SIGNED"],
            "units": ["市投资促进局"],
        },
    },
    {
        "id": "project",
        "name": "重大项目参谋",
        "avatar": "项",
        "brief": "掌握项目进度与要素保障",
        "focus": "重点项目进度、投资完成、要素保障",
        "angle": (
            "把投资完成率和时序进度放在一起看。进度慢的先分清是钱没到位，还是地没供上、审批没跑完；"
            "再看到底是普遍偏慢，还是个别项目拖了后腿。要素保障说不出具体卡在哪一步，就等于没说。"
        ),
        "domain": {
            "codes": ["FAI_PROGRESS", "FAI_GROWTH"],
            "units": ["市发展和改革委员会"],
            # 城市更新（老旧小区改造、加装电梯）在这套板块里没有独立参谋，
            # 归项目口：它盯的就是「项目进度 + 要素保障」。
            "bases": ["城市更新政策"],
        },
    },
    {
        "id": "society",
        "name": "社会治理参谋",
        "avatar": "治",
        "brief": "紧盯12345诉求与公共安全",
        "focus": "12345 诉求、矛盾纠纷、公共安全",
        "angle": (
            "先看诉求办理的质量，不只看完结率：同一类事反复投诉、办结后回访不满意，"
            "都比数字难看更要紧。再看矛盾纠纷有没有从个体转向群体、安全类问题有没有抬头。"
            "最后落到哪一类事、找哪个单位、先办哪一批。"
        ),
        "domain": {
            "codes": ["TZ_12345_CLOSURE"],
            # 城管局：12345 高频诉求（停车难）、城市运行与供暖预案；
            # 大数据局：城市运行管理服务平台的数据接入。两者都不归任何
            # 板块参谋时，台账里这几件事只有总参谋看得到。
            "units": [
                "市政务服务和数据管理局（12345 热线中心）",
                "市城管局",
                "市大数据局",
            ],
            "bases": ["政务热线与营商环境"],
        },
    },
    {
        "id": "livelihood",
        "name": "民生保障参谋",
        "avatar": "民",
        "brief": "关注就业教育与民生实事",
        "focus": "就业、教育、医疗、民生实事",
        "angle": (
            "先看民生实事是真完成还是纸面完成——验收了没有、群众体感如何；"
            "再看就业、教育、医疗里哪一块的压力在往上走。"
            "项数完成得漂亮但投诉在涨，就先查兑现质量，不拿完成率交差。"
        ),
        "domain": {
            "codes": ["UR_RATE", "LIVELIHOOD_DONE"],
            "units": ["国家统计局本市调查队", "市政府办公室（民生实事专班）"],
        },
    },
    {
        "id": "eco",
        "name": "生态环境参谋",
        "avatar": "环",
        "brief": "把环境质量与经济增速放一起看",
        "focus": "空气质量、工业排放相关指标",
        "angle": (
            "把空气质量和工业增速放在一起看：两者同向要问代价，反向要问是不是靠天帮忙、"
            "靠临时停产换来的。再看整改是一阵风还是有长效机制，点出最该盯的那一类污染源，"
            "不泛泛说「加强监管」。"
        ),
        "domain": {
            "codes": ["AQI_GOOD_DAYS"],
            "units": ["市生态环境局"],
            "bases": ["生态环境政策"],
        },
    },
)

_SYSTEM = """你是{name}（{brief}），在市政府常务会上旁听，被市长点名说两句。
你关注：{focus}。你的看问题方式：{angle}

硬性要求：
1. 只能用「可用数值」里列出的数，或由这些数算出来的数。**算出来的必须把算式原样写进正文**
   （例如「1024.8 - 882.6 = 142.2」）；两个数相减、相除得来的差值也算「算出来的」，
   同样要写出算式。只给结果不给算式，那句话会被删掉。
2. 除此之外不许出现任何数字。**单独的年份也不行**：要写年份就照抄带月份的完整期次
   （年月都要带上），不要只写「2026年」。年份和期次不许改写成「7月」「前三季度」这类
   别的说法，不得引用没列出的指标。
3. 可以点名记录（如「河道巡查那件事」），但不能把记录里的话改写成新的数字或新的时间。
4. 四句话以内，按这个顺序说：结论（好/坏/偏离目标）→ 依据（哪个数、跟什么比）→
   一条能落地的动作（找哪个单位、看什么、什么时候之前）。**动作是这段话的目的：
   宁可少写一句依据，也不要省掉动作。** 不要客套、不要复述问题、不要格式标记。
   清单里已经给了「上月水平（推算）」「去年同期水平（推算）」的，直接用那个数，
   不要自己再加减 —— 方向算反过一次（环比回落被算成更低），这种错看不出来。
5. 「可用数值」不足以判断时，直说缺哪一类口径，不要用「信息不足」四个字敷衍，也不要猜。
"""


# 哪些字段属于「专家身份」，持久化和对外输出都只保留这些（字符串字段）。
# brief 参与提示词但也可空；angle 是看问题方式，编辑面板的核心。
# domain 不是身份字段，是「这位参谋读哪块知识库」的能力配置，
# 单独走 _clean_domain 校验，面板编辑不带 domain 时保留原值（见 save_experts）。
_EXPERT_KEYS = ("id", "name", "avatar", "brief", "focus", "angle")


def _clean_domain(raw: Any) -> dict[str, list[str]] | None:
    """校验并规范化 domain；不合法返回 None（视为没绑域）。

    三个字段各管一段：`codes` 指标代码（读哪些口径）、`units` 责任单位
    （读哪些督办/会议）、`bases` 分类库（读哪些上传的政策原文）。三者至少
    有一个才算绑了域。

    `bases` 存**库名**不存库 id：id 只在本机有效，换个数据目录就全部失效，
    而库名是人在专家卡和面板里能看懂、能勾的东西。查询时由
    `rag.resolve_bases` 换回 id，解析不到的库直接跳过（不会让参谋整段报废）。
    """
    if not isinstance(raw, dict):
        return None
    codes = [str(c).strip() for c in raw.get("codes") or [] if str(c or "").strip()]
    units = [str(u).strip() for u in raw.get("units") or [] if str(u or "").strip()]
    bases = [str(b).strip() for b in raw.get("bases") or [] if str(b or "").strip()]
    if not codes and not units and not bases:
        return None
    out: dict[str, list[str]] = {"codes": codes, "units": units}
    if bases:
        # 去重保序：面板里同一个库被勾两次时不要写两遍
        out["bases"] = list(dict.fromkeys(bases))
    return out


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """把 expert.md 拆成 frontmatter dict + 正文。

    只认「首行 `---`、成对闭合」的 YAML 块；解析失败或没有 frontmatter 时
    返回空 dict，正文原样返回。这里**故意不引 yaml 库**：专家卡就那七八个
    字段，够用的子集是 id/name/avatar/brief/focus/angle 加一个 domain
    （codes/units/bases 三串）。引 yaml 只会多一个可选依赖，还得为它写容错。
    """
    stripped = text.lstrip("\ufeff").strip()
    if not stripped.startswith(_FM_DELIM):
        return {}, stripped
    lines = stripped.splitlines()
    if lines[0].strip() != _FM_DELIM:
        return {}, stripped
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FM_DELIM:
            end = i
            break
    if end is None:
        return {}, stripped
    fm: dict[str, Any] = {}
    for raw in lines[1:end]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip("'\"").strip()
        if not key or not val:
            continue
        # domain 是唯一的嵌套块，key 单独占一行、值在下面缩进的行里。
        # 解析 YAML 流式 `[a, b]` 之外，也兼容「codes: [A, B]」这种写在一行的情况
        # 已经在 partition 里被当成普通 key=val 处理掉了——不对，codes 本身是 key，
        # 它在 domain 块下，会走到这里。所以这里只认非 domain 的顶层标量。
        fm[key] = val
    body = "\n".join(lines[end + 1 :]).strip()
    return fm, body


def _parse_domain_block(text: str) -> dict[str, list[str]] | None:
    """从 expert.md 的 frontmatter 里抠 domain 块（codes / units / bases 三个列表）。

    只认 frontmatter 区（首尾 `---` 之间）里的 domain 块，支持两种写法：

        domain:
          codes: [GY_VA_GROWTH, GDP_TOTAL]
          units: [市统计局, 市财政局]
          bases: [生态环境政策]

        domain:
          codes:
            - GY_VA_GROWTH
          units:
            - 市统计局
          bases:
            - 生态环境政策

    解析失败返回 None，等价于「没绑域」，不会让整份专家卡报废。
    """
    if not isinstance(text, str):
        return None
    # 只在 frontmatter 里找 domain，正文（frontmatter 之后）绝不允许混进来。
    fm = _frontmatter_only(text)
    if fm is None:
        return None
    return _clean_domain(
        {
            "codes": _extract_list_item(fm, "codes"),
            "units": _extract_list_item(fm, "units"),
            "bases": _extract_list_item(fm, "bases"),
        }
    )


def _frontmatter_only(text: str) -> str | None:
    """抽出 expert.md 的 frontmatter 区（首尾 `---` 之间，含 `---`），没有返回 None。"""
    stripped = text.lstrip("\ufeff").strip()
    if not stripped.startswith(_FM_DELIM):
        return None
    lines = stripped.splitlines()
    if lines[0].strip() != _FM_DELIM:
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FM_DELIM:
            return "\n".join(lines[: i + 1])
    return None


def _extract_list_item(text: str, key: str) -> list[str]:
    """在 frontmatter 里抠 `key:` 后面跟的列表（流式或块式），去重保序。

    `text` 已限定为 frontmatter 区，所以「缩进变浅」就是块式列表的边界，
    不会再吞到正文。
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(key + ":") or s == key:
            start = i
            break
    if start is None:
        return []
    items: list[str] = []
    first = lines[start].split(":", 1)[1].strip() if ":" in lines[start] else ""
    if first:
        items.extend(_split_list_tokens(first))
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    for line in lines[start + 1 :]:
        s = line.strip()
        if not s:
            continue
        indent = len(line) - len(line.lstrip())
        # 缩进回到 key 同级或更浅，说明这个列表块结束了。
        if indent <= base_indent:
            break
        items.extend(_split_list_tokens(s))
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _split_list_tokens(s: str) -> list[str]:
    """把 `[A, B]` 或 `- A` 或 `- "A B"` 拆成词条。

    处理三种：流式 `[A, B]`、块式 `- A`、带引号的 `- "A B"`（单位名可能含括号、
    逗号，得整体保留）。引号内的逗号不拆。
    """
    s = s.strip()
    out: list[str] = []
    if s.startswith("["):
        s = s[1:]
        if s.endswith("]"):
            s = s[:-1]
        # 引号内的逗号不拆：先把 "..." 里的逗号换成占位，拆完再换回来。
        quoted = re.findall(r'"([^"]*)"', s)
        for q in quoted:
            s = s.replace(f'"{q}"', "\x00")
        for part in s.split(","):
            part = part.strip()
            if part == "\x00":
                out.append(quoted.pop(0) if quoted else part)
            elif part:
                out.append(part)
    else:
        s = s.lstrip("-").strip()
        s = s.strip("'\"").strip()
        if s:
            out.append(s)
    return out


def _read_expert_md(md_path: Path) -> dict[str, Any] | None:
    """读一份 expert.md，拼成和旧 json 条目同构的 dict；读坏/缺 id 返回 None。"""
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None
    fm, body = _parse_frontmatter(text)
    eid = str(fm.get("id") or "").strip()
    if not eid:
        return None
    item = {k: str(fm.get(k) or "").strip() for k in _EXPERT_KEYS}
    if not item["name"]:
        item["name"] = eid
    dom = _clean_domain(fm.get("domain")) or _parse_domain_block(text)
    if dom:
        item["domain"] = dom
    # 正文作为 angle 的补充源：frontmatter 没写 angle 时，从正文「看问题的方式」
    # 小节里取。这是给手写专家卡的兜底，让业务方只写正文也能用。
    if not item["angle"] and body:
        item["angle"] = _angle_from_body(body)
    return item


def _angle_from_body(body: str) -> str:
    """从正文里抠「看问题的方式」小节作为 angle；没有就退回正文首段。

    专家卡的正文约定有一个「## 看问题的方式」小节，里面就是这位参谋怎么判断。
    frontmatter 有 angle 时这里用不上；没写 angle 时从这里兜底，避免空手。
    """
    m = re.search(r"看问题的方式\s*\n(.*)", body, flags=re.S)
    if m:
        seg = m.group(1).strip()
        # 截到下一个标题为止
        seg = re.split(r"\n\s*#", seg)[0].strip()
        if seg:
            return seg
    # 没有该小节就取正文第一个非空段，掐掉标题行
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:200]
    return ""


def _load() -> list[dict[str, Any]]:
    """读专家名单，优先级：experts/ 目录 → 旧 data/experts.json（自动迁移）→ 内置默认。

    experts/ 目录存在且至少有一位能读出来，就以它为准；否则退回旧 json，
    读到旧 json 就顺手把它迁成 MD（下次起目录优先，json 不再参与）。
    json 也没有或读坏，才用内置默认。三个来源都保证「有域不丢、字段完整」。
    """
    # 1) 首选 experts/ 目录
    if EXPERTS_DIR.is_dir():
        loaded: dict[str, dict[str, Any]] = {}
        for md in sorted(EXPERTS_DIR.glob("*/expert.md")):
            item = _read_expert_md(md)
            if not item:
                continue
            loaded.setdefault(item["id"], item)  # 同名目录只认第一个（setdefault 保序）
        if loaded:
            # 排序保持「内置默认的声明顺序在前，新增专家按字母序排后」。
            # 内置顺序是有意的（总参谋打头、板块参谋按经济→项目→社会→民生→生态），
            # 换存储层不该把它打乱；目录遍历天然是字母序，会把它洗成 eco 打头。
            builtin_order = [e["id"] for e in _DEFAULT_EXPERTS]
            known = [loaded.pop(eid) for eid in builtin_order if eid in loaded]
            extra = [loaded[k] for k in sorted(loaded)]
            return known + extra

    # 2) 旧 data/experts.json（读坏或为空都继续往下）
    try:
        if EXPERTS_FILE.is_file():
            raw = json.loads(EXPERTS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list) and raw:
                out = []
                seen = set()
                for e in raw:
                    if not isinstance(e, dict) or not str(e.get("id") or "").strip():
                        continue
                    eid = str(e["id"]).strip()
                    if eid in seen:
                        continue
                    seen.add(eid)
                    item = {k: str(e.get(k) or "").strip() for k in _EXPERT_KEYS}
                    if not item["name"]:
                        item["name"] = eid
                    dom = _clean_domain(e.get("domain"))
                    if dom:
                        item["domain"] = dom
                    out.append(item)
                if out:
                    _migrate_json_to_md(out)
                    return out
    except Exception:  # noqa: BLE001
        pass
    return [dict(e) for e in _DEFAULT_EXPERTS]


def _domain_lines(dom: dict[str, Any] | None) -> list[str]:
    """domain 块的落盘格式：codes / units / bases 各一行，空的不写。

    迁移（旧 json → MD）和保存（面板 → MD）共用这一份。以前两处各手写一遍，
    加一个字段就得改两处，迟早分叉 —— 分叉的表现是「面板改完，另一位参谋
    重新读进来时 bases 没了」，而界面上只是少几条依据，没人会想到是格式问题。
    """
    if not dom:
        return []
    groups = (
        ("codes", dom.get("codes")),
        ("units", dom.get("units")),
        ("bases", dom.get("bases")),
    )
    rows = [(k, [str(x).strip() for x in (v or []) if str(x).strip()]) for k, v in groups]
    if not any(vals for _, vals in rows):
        return []
    out = ["domain:"]
    for key, vals in rows:
        if vals:
            out.append(f"  {key}: [" + ", ".join(vals) + "]")
    return out


def _migrate_json_to_md(items: list[dict[str, Any]]) -> None:
    """把旧 json 名单迁成 experts/<id>/expert.md。只写不删：json 留作兜底。

    迁移是「尽力而为」，任何一位写失败都不影响其余；下次 _load 只要目录里
    至少有一位，就以目录为准。domain 是唯一要专门处理的字段（要写成 YAML 块）。
    """
    for e in items:
        eid = str(e.get("id") or "").strip()
        if not eid:
            continue
        lines = ["---", f"id: {eid}"]
        for k in ("name", "avatar", "brief", "focus", "angle"):
            v = str(e.get(k) or "").strip()
            if v:
                lines.append(f"{k}: {v}")
        dom = e.get("domain")
        lines.extend(_domain_lines(dom if isinstance(dom, dict) else None))
        lines.append("---")
        lines.append("")
        lines.append(f"# {e.get('name') or eid}")
        lines.append("")
        lines.append("## 看问题的方式")
        lines.append("")
        if e.get("angle"):
            lines.append(e["angle"])
        body = "\n".join(lines) + "\n"
        try:
            d = EXPERTS_DIR / eid
            d.mkdir(parents=True, exist_ok=True)
            (d / "expert.md").write_text(body, encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue


def all_experts() -> list[dict[str, Any]]:
    return _load()


def expand_for_chief(expert_ids: list[str] | None) -> list[str]:
    """单选总参谋 → 展开成「全部分管参谋 + 总参谋」。

    总参谋的定位是收口，不是第七个角度：它要在各位分管参谋说完之后，把话归到板块、
    挑出一两个风险点、说清先看谁。只叫它一个人，它手里只有一份和分管参谋完全相同的
    数值，「挑重点」就只能是再猜一遍 —— 它自己 expert.md 里那句「从分管参谋给的依据里
    挑重点」也就成了空话。

    **只在名单里恰好只有总参谋时展开。** 团模式（名单前 3 位，第 1 位就是总参谋）
    不展开：团模式的语义是「一次带几位一起看」，把 6 位全拉进来就变成了另一个产品。
    但团模式里的总参谋同样走收口（见 `interpret`），只是它的同僚只有那两位。

    展开后的总参谋仍排在最后 —— 顺序就是发言顺序，也是界面上的渲染顺序。
    """
    ids = [str(x).strip() for x in (expert_ids or []) if str(x).strip()]
    if set(ids) != {CHIEF_ID}:
        return ids
    peers = [e["id"] for e in _load() if e["id"] != CHIEF_ID]
    if not peers:  # 名单里只剩总参谋，展开没有意义
        return ids
    return peers + [CHIEF_ID]


def find(eid: str | None) -> dict[str, Any] | None:
    for e in _load():
        if e["id"] == eid:
            return dict(e)
    return None


def save_experts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """整体替换名单并落盘。校验字段后写 experts/<id>/expert.md，返回规范化后的名单。

    编辑面板只提交身份字段；同 id 已有 domain 而提交里没带，**保留原 domain** ——
    否则在面板里改一句 focus 就会把这位参谋的知识域悄悄清空，界面上一无所有
    （解读退回无依据模式），用户完全察觉不到是编辑害的。

    名单里缺了谁 = 删除：把对应目录的 expert.md 移进 experts/_removed/ 留底，
    不物理抹掉（业务方手写的内容值得留个全尸）。移不动的（跨盘/权限）退化为改名
    `expert.md.removed`，绝不让删除失败把整个保存拖垮。
    """
    prev = {e["id"]: e for e in _load()}
    clean: list[dict[str, Any]] = []
    seen = set()
    for e in items or []:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "").strip()
        if not eid or eid in seen:
            continue
        seen.add(eid)
        item = {k: str(e.get(k) or "").strip() for k in _EXPERT_KEYS}
        if not item["name"]:
            item["name"] = eid
        if not item["focus"]:
            item["focus"] = item["angle"] or "综合"
        if not item["angle"]:
            item["angle"] = item["focus"] or "从整体看趋势与风险"
        dom = _clean_domain(e.get("domain")) or (
            prev.get(eid, {}).get("domain") if "domain" not in e else None
        )
        if dom:
            item["domain"] = dom
        clean.append(item)
    _write_md(clean)
    _remove_missing_md([e["id"] for e in clean])
    return clean


def _write_md(clean: list[dict[str, Any]]) -> None:
    """把规范化名单写成 experts/<id>/expert.md。复用迁移的同款落盘格式。"""
    for e in clean:
        eid = str(e.get("id") or "").strip()
        if not eid:
            continue
        lines = ["---", f"id: {eid}"]
        for k in ("name", "avatar", "brief", "focus", "angle"):
            v = str(e.get(k) or "").strip()
            if v:
                lines.append(f"{k}: {v}")
        dom = e.get("domain")
        lines.extend(_domain_lines(dom if isinstance(dom, dict) else None))
        lines.append("---")
        lines.append("")
        lines.append(f"# {e.get('name') or eid}")
        lines.append("")
        lines.append("## 看问题的方式")
        lines.append("")
        if e.get("angle"):
            lines.append(e["angle"])
        body = "\n".join(lines) + "\n"
        try:
            d = EXPERTS_DIR / eid
            d.mkdir(parents=True, exist_ok=True)
            (d / "expert.md").write_text(body, encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue


def _remove_missing_md(kept_ids: list[str]) -> None:
    """把名单里已不存在的专家目录移进 _removed/ 留底。"""
    if not EXPERTS_DIR.is_dir():
        return
    kept = set(kept_ids)
    removed_dir = EXPERTS_DIR / "_removed"
    for d in EXPERTS_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("_") or d.name in kept:
            continue
        if not (d / "expert.md").is_file():
            continue
        try:
            removed_dir.mkdir(parents=True, exist_ok=True)
            d.rename(removed_dir / d.name)
        except Exception:  # noqa: BLE001
            # 跨盘/占用时退化为改名，绝不让删除失败拖垮保存
            try:
                (d / "expert.md").rename(d / "expert.md.removed")
            except Exception:  # noqa: BLE001
                continue


# 落库时只保留回放要用的字段：回看历史不需要知道「是谁算的」，
# 存 model/model_ref 只会让 chats.json 白白变胖。
META_KEYS = ("id", "name", "avatar", "brief", "focus", "ok", "text", "why", "note", "refs", "skipped")

# 依据条目只存「哪一条」，不存正文：正文有几百字，而回放只需要显示
# 「参谋当时看了哪几条」并支持点击跳转。ref 留着，前端据此跳回台账或会议记录。
REF_META_KEYS = ("kind", "kind_label", "id", "title", "ref")


def to_meta(views: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把解读裁成可落库的样子。存进去的必须是**裁剪后**的结果，
    否则下一轮读历史时字段忽多忽少，前端渲染会不一致。"""
    return [{k: v for k, v in ev.items() if k in META_KEYS} for ev in (views or [])]


def _refs_meta(refs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [{k: r.get(k) for k in REF_META_KEYS} for r in (refs or [])]


def _numbers_block(result: dict[str, Any]) -> str:
    """把这一轮的数值摊给参谋看 —— 它现在可以引用，也可以算，但都要留痕。

    以前这里只喂方向（「同比上升」），理由是怕专家报数。代价是它根本不知道
    是 8% 还是 2%，只能输出「偏高、要盯住」这类正确但没用的话。

    现在改成喂数 + 后置校验（`numgate`）：数字可以出现，但每一个都得追到
    这份清单，或者由清单里的数按写出来的算式算得。清单本身来自指标接口，
    所以「数值过模型」不等于「模型编数」。
    """
    rows: list[str] = []
    for item in result.get("numbers") or []:
        label = str(item.get("label") or "").strip()
        value = item.get("value")
        if not label or value is None:
            continue
        unit = str(item.get("unit") or "").strip()
        rows.append(f"- {label}：{value}{unit}")

    if not rows:
        # 退回 facts（compare/trend 这类路径没有 numbers 字段）
        for fact in result.get("facts") or []:
            label = str(fact.get("label") or "").strip()
            value = str(fact.get("value") or "").strip()
            if label and value:
                rows.append(f"- {label}：{value}")

    note = str(result.get("target_note") or "").strip()
    if note:
        rows.append(f"- {note}")

    head = "可用数值（唯一允许引用的数；要算新数必须把算式写出来）："
    body = "\n".join(rows) if rows else "（本轮没有数值，只能谈方向）"
    hint = (
        "\n口径提示：如果问的是单月而指标按累计公布，可以用相邻两期累计相减，"
        "把算式写进正文，例如「1024.8 - 882.6 = 142.2」。"
        if result.get("prev_cumulative") is not None
        else ""
    )
    return head + "\n" + body + hint


def _signals(result: dict[str, Any]) -> list[str]:
    """把数值折成方向（升/降/持平），这样专家拿到的输入里没有数字。

    两条取数路径都得覆盖，否则专家会「有数看不见」：

    - `intent=value`（单指标）：数值在 `facts`，同比环比是两个独立条目，
      且都属于同一个指标，用 `metric_names[0]` 当标签是对的。
    - `intent=compare`（多指标）：数值在 `compare_rows`，**`facts` 是空列表**。
      这里每行自带 name 和 yoy，标签必须逐行取 —— 用 names[0] 统一套会把
      A 指标的方向安到 B 头上，比没有信号更危险。
    """
    out: list[str] = []

    for row in result.get("compare_rows") or []:
        yoy = row.get("yoy")
        if isinstance(yoy, (int, float)):
            tone = "上升" if yoy > 0 else "下降" if yoy < 0 else "持平"
            out.append(f"{row.get('name') or '指标'} 同比{tone}")

    if out:
        return out

    label = "指标"
    names = result.get("metric_names") or []
    if names:
        label = names[0]
    for f in result.get("facts") or []:
        raw = f.get("raw")
        if f.get("label") in ("环比", "同比") and isinstance(raw, (int, float)):
            tone = "上升" if raw > 0 else "下降" if raw < 0 else "持平"
            out.append(f"{label} {f['label']}{tone}")
    return out


def _refs_block(refs: list[dict[str, Any]] | None) -> str:
    """把域内检索到的记录拼成 prompt 里的那一段。

    每条都写明「是什么、谁的、原文摘录」：参谋要点名一件事，就得先知道它属于谁、
    原话怎么写。只给标题不给摘录的话，它只能凭标题猜内容，那和编没有区别。

    两类依据分开写：`kind="doc"` 是上传政策库的原文（讲要求、口径），
    其余是系统记录（讲正在发生什么）。混在同一个标题下，模型会把
    「文件要求 2027 年达到 X」和「本月已经做到 Y」当成同一类事实。

    没有记录时**不省略这一段**，而是明写「本域内无相关记录」。省掉的话，
    模型会以为自己漏看了什么，反而更爱凭印象补一句。
    """
    rows = list(refs or [])
    records = [r for r in rows if r.get("kind") != "doc"]
    docs = [r for r in rows if r.get("kind") == "doc"]
    if not records and not docs:
        return "可参考的记录：本域内没有相关记录（只凭上面的指标信号判断，不要推测具体事项）。"

    def one(r: dict[str, Any]) -> str:
        body = str(r.get("quote") or r.get("text") or "").strip()
        if len(body) > REF_CHARS:
            body = body[:REF_CHARS] + "…"
        src = r.get("source") or {}
        dept = str(src.get("department") or "").strip() or "—"
        return f"- 〔{r.get('kind_label') or '记录'}〕{r.get('title')}（{dept}）：{body}"

    lines: list[str] = []
    if records:
        lines.append("可参考的记录（原文摘录，其中带数字；你可以据此判断好坏，但不得复述那些数字）：")
        lines.extend(one(r) for r in records)
    if docs:
        if records:
            lines.append("")
        # 政策原文里几乎每篇都有数字（目标值、期限、补贴标准）。数字闸只放行
        # 本轮指标清单里的数，所以这里必须说清「一个数都别搬」—— 说了模型
        # 仍可能搬，但那是闸的事，不是这段话的责任。
        lines.append(
            "可参考的政策原文（只借要求与方向。这段原文里的数字 —— 目标值、比例、年份、"
            "期限 —— 一个都不要写进你的话里；要提目标就说「文件提出的目标」，把数留在原文）："
        )
        lines.extend(one(r) for r in docs)
    return "\n".join(lines)


# 总参谋的收口要求。单独拆出来是因为它和「说四句话」是两件事：
# 前面几位说「我这块怎么样」，总参谋要说「这几块里先看哪一块」。
_CHIEF_ASK = (
    "按你的角色收口：先把这件事归到某个板块，再从上面各位的说法里挑一两个最该盯的风险点，"
    "说清先看哪位参谋、下一步找谁。**不要复述他们已有的依据** —— 该说的话他们都说过了，"
    "你重复一句，市长就多读一句没用的。"
)


def _peer_block(views: list[dict[str, Any]] | None) -> str:
    """把已经发过言的参谋原话摆到总参谋面前。

    没有这一段，总参谋和分管参谋拿到的是同一份数值，「挑重点」等于把同一份数据再猜
    一遍，而界面会把它当成「综合判断」展示 —— 这是最容易被当成有价值内容的一种空话。

    跳过的那几位也列进去：**「这件事没压在他的板块上」本身就是判断主次的依据**。
    只报说过话的人，总参谋会以为全组都发了言，从而把「只有一个板块在承压」读成
    「各板块普遍反映」，方向正好相反。
    """
    said: list[str] = []
    silent: list[str] = []
    for v in views or []:
        if v.get("skipped"):
            silent.append(f"{v.get('name') or v.get('id')}（{v.get('focus') or '—'}，本轮不涉其域）")
        elif v.get("ok") and v.get("text"):
            said.append(f"- {v.get('name') or v.get('id')}：{v.get('text')}")

    out: list[str] = []
    if said:
        out.append("【分管参谋已经说过的，别重复】\n" + "\n".join(said))
    if silent:
        out.append("【本轮没有发言的（不是没看，是不涉其域）】" + "、".join(silent))
    return "\n\n".join(out)


def _out_of_scope(expert: dict[str, Any], codes: list[str]) -> bool:
    """本轮问的指标，一个都不属于这位参谋的域。

    这时**不调模型**，直接说明「不是他关注的事」。理由是实测出来的：几位参谋
    一起叫上、问的是工业加空气质量时，民生参谋会逐字复述经济与生态的观点 ——
    那不是「三个角度」，是同一段话印了三遍，而市长会以为真有三个人在会诊。
    它之所以能复述，是因为方向信号是全组共享的；它之所以不该复述，是因为
    这些指标跟他的关注面一点关系都没有。

    只在**拿到指标代码**且该参谋有域时才判：知识库路径下 codes 是空的、
    自建专家没有 domain，那时没有依据判断相关性，交给 prompt 去说 ——
    宁可多一次调用，也不要因为判错而集体沉默。
    """
    dom = set((expert.get("domain") or {}).get("codes") or ())
    got = {str(c) for c in (codes or ())}
    if not dom or not got:
        return False
    return not (dom & got)


def _interpret_one(
    expert: dict[str, str],
    user_content: str,
    result: dict[str, Any],
    model_ref: str | None,
    refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一位专家的完整一轮：调模型 → 过数字闸 → 给出 ok/why。

    单独拆出来是为了让 `interpret` 的循环只管「报进度 + 收集结果」，
    失败分支再多也不会漏掉一次 done 事件。

    `refs` 无论成败都写进 view：参谋读了哪几条是**事实**，不因为这段解读
    被数字闸丢掉就跟着消失。用户看到一个「信息不足」，还得能知道它当时
    手上有什么，否则只会以为系统没检索。
    """
    view: dict[str, Any] = {
        "id": expert["id"],
        "name": expert["name"],
        "avatar": expert["avatar"],
        "brief": expert.get("brief", ""),
        "focus": expert["focus"],
        "ok": False,
        "text": "",
        "why": "",
        "refs": _refs_meta(refs),
    }
    if not llmclient.available():
        view["why"] = "未配置模型，专家解读需要模型；数值答案不受影响。"
        return view
    try:
        resp = llmclient.chat_text(
            _SYSTEM.format(**expert),
            user_content,
            model_ref=model_ref,
            temperature=0.4,
            # deepseek-v4-pro 这类推理模型会把 token 花在 thinking 上，320 会被
            # 偶发吃光、导致 content 为空。这里给足额度，最终文本仍在下面按 260 字截。
            max_tokens=2000,
        )
    except llmclient.ModelUnavailable:
        view["why"] = "未配置模型。"
        return view
    except llmclient.ModelError as exc:
        view["why"] = f"模型调用失败：{str(exc)[:80]}"
        return view

    text = str((resp.get("text") or "")).strip()
    view["model"] = resp.get("model")
    view["model_ref"] = resp.get("model_ref")
    if not text:
        view["why"] = "模型没有给出内容。"
    else:
        titles = [str(r.get("title") or "") for r in (refs or [])]
        gate = numgate.audit(text, result, extra_literals=titles)
        if gate["ok"]:
            view["ok"] = True
            view["text"] = text[:400]
            if gate["computed"]:
                # 让界面能标出「这段里有推算」，并可展开看算式
                view["computed"] = gate["computed"][:4]
        else:
            # 整段过不了就先逐句救一次：建议句通常不含数，没道理跟着违规句一起丢。
            # 门槛放到 10 字 —— 四句话里能留下一句动作，就比整段空着有用。
            # 留下的句子仍然逐句过闸，底线没变：每个数都追得到来源。
            salvaged = numgate.salvage(text, result, extra_literals=titles, min_chars=10)
            if salvaged:
                kept, dropped = salvaged
                bad = "、".join(str(x) for x in gate["bad"][:3])
                view["ok"] = True
                view["text"] = kept[:400]
                view["note"] = (
                    f"删去了 {len(dropped)} 句含无法溯源数字的话（{bad}）；"
                    "留下来的每个数都能追到来源。"
                )
                again = numgate.audit(kept, result, extra_literals=titles)
                if again["computed"]:
                    view["computed"] = again["computed"][:4]
            else:
                # 一句都救不回来：不是「修正」，是丢弃。数字只要出现过，
                # 就没法判断它是不是编的。但要把拦下了什么说出来。
                bad = "、".join(str(x) for x in gate["bad"][:3])
                view["why"] = f"解读里的数字无法溯源（{bad}），已丢弃；数值以指标接口为准。"
                # 原文留一份：用户问「为什么丢了」时能自己看，否则只剩一句「已丢弃」
                view["discarded"] = text[:1200]
    return view


def interpret(
    result: dict[str, Any],
    expert_ids: list[str],
    model_ref: str | None = None,
    on_step: Any = None,
    refs_by_expert: dict[str, list[dict[str, Any]]] | None = None,
    cap: int | None = None,
) -> list[dict[str, Any]]:
    """给一份已经算好的答案配专家解读。

    返回值永远是列表，每项都带 ok：失败的那一项说明原因，不影响其它项，
    也绝不影响答案本身。

    `on_step` 用来报「现在轮到谁」：专家是串行调模型的，累起来要好几秒，
    没有这一步的流式界面会从「解析完成」直接卡到出结果，看着像死了。

    `refs_by_expert` 是每位参谋的域内检索结果（`{专家 id: [条目]}`），由调用方
    先查好再传进来。**这里不做检索**：experts 只负责「拿到什么就说什么」，
    检索的范围和可见性交给 knowledge，两边各管一段，才不会出现「专家自己
    去库里翻，翻到了别人分管的事」。

    `cap` 见 `cap_for`：默认按 MAX_PER_CALL 截，总参谋在场时放开，
    因为它就是被展开出来收口的，砍到 3 位等于让它对着半张桌子做综合。
    """
    raw = [str(x) for x in (expert_ids or [])]
    ids = raw[: cap_for(raw, cap)]  # 一次会诊不该变成刷模型；总参谋在场例外
    if not ids:
        return []

    names = list(result.get("metric_names") or [])
    codes = [str(c) for c in (result.get("metric_codes") or [])]
    signals = _signals(result)
    question = str(result.get("question") or "").strip()

    base = [
        f"市长问的是：{question}",
        f"涉及指标：{'、'.join(names) if names else '（未命中具体指标）'}",
        f"方向信号：{'；'.join(signals) if signals else '（无同比环比数据）'}",
    ]

    # 总参谋从名单里摘出来，排到最后单独跑：它要说的话依赖别人说了什么，
    # 和分管参谋挤在同一个 for 里就只能是「并列的第七个角度」。
    peers = [eid for eid in ids if eid != CHIEF_ID]
    with_chief = CHIEF_ID in ids
    total = len(peers) + (1 if with_chief else 0)

    def emit(phase: str, expert: dict[str, str], i: int, ok: bool | None = None) -> None:
        if not on_step:
            return
        payload: dict[str, Any] = {"phase": phase, "name": expert["name"], "index": i, "total": total}
        if ok is not None:
            payload["ok"] = ok
        on_step("experts", payload)

    def one(eid: str, i: int, peer_views: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        """一位参谋的一轮。`peer_views` 非 None 表示这位是收口的总参谋。"""
        expert = find(eid)
        if expert is None:
            return None
        emit("start", expert, i)
        if _out_of_scope(expert, codes):
            # 进度事件照报：前端已经显示「正在看材料」，这一位不报 done 会永远停在那
            view: dict[str, Any] = {
                "id": expert["id"],
                "name": expert["name"],
                "avatar": expert["avatar"],
                "brief": expert.get("brief", ""),
                "focus": expert["focus"],
                "ok": False,
                "skipped": True,
                "text": "",
                "why": f"本轮问的不是你关注的事（{expert['focus']}），未作解读。",
                "refs": [],
            }
            emit("done", expert, i, False)
            return view

        refs = list((refs_by_expert or {}).get(eid) or [])
        extra: list[str] = []
        if total > 1:
            extra.append("同一份数值对每位参谋都一样，区别在你看问题的角度。")
        if peer_views is not None:
            block = _peer_block(peer_views)
            if block:
                extra += ["", block, _CHIEF_ASK]
        # 总参谋不绑域。照搬「本域内没有相关记录」会让它以为自己没检索过、
        # 手上没有依据，于是收口收成一句「信息不足」—— 而它真正的依据是上面各位的话。
        ref_block = (
            "可参考的记录：总参谋不绑域，你的依据就是上面各位的原话和这份数值。"
            if peer_views is not None and not refs
            else _refs_block(refs)
        )
        user_content = "\n".join(
            base + [_numbers_block(result)] + extra + [ref_block, "按你的角色说四句话以内。"]
        )
        view = _interpret_one(expert, user_content, result, model_ref, refs)
        emit("done", expert, i, view["ok"])
        return view

    views: list[dict[str, Any]] = []
    for i, eid in enumerate(peers, 1):
        v = one(eid, i, None)
        if v is not None:
            views.append(v)
    if with_chief:
        # 收口放在最后：拿到的是**已裁过**的 views，所以被数字闸丢掉的那几位
        # 不会以「说过话」的身份出现在总参谋面前。它得知道谁真的没说话。
        v = one(CHIEF_ID, total, views)
        if v is not None:
            views.append(v)

    return views
