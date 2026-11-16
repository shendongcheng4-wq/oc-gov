#!/usr/bin/env python
"""离线快照导出：把工作台此刻的数据烤进一个自包含 html，双击就能看。

    python tools/export_offline.py
    python tools/export_offline.py --no-threads --out dist/demo.html

做法不是另写一套页面 —— 那等于分叉出一份会过时的界面。这里拿正在用的
`app/web/index.html` 原文，在它前面插一段「接口垫片」：所有 /api/* 请求
改从内联快照里答。界面代码一行不改，改一处不会漏另一处。

由此定下三条边界，写在这里免得以后当成 bug 修：

1. **只读**。写接口（POST/PATCH/DELETE）一律回 501 并附一句中文原因。
   回 ok 更"顺"，但那是骗人 —— 快照里的台账不会因为点了「签收」而变，
   下一次刷新还是原样，用户会以为操作丢了。
2. **不含模型**。问答入口拦在 `ask()` 上，直接说清楚不能问。
   不能让请求打到「未收录接口」，那会显示成「服务端出错了」。
3. **不联网**。垫片拦的是 `window.fetch`，命中即返回本地快照，未命中也不外发。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"

sys.path.insert(0, str(APP))
import main as srv  # noqa: E402  —— main 自己会把 core / metrics / meeting 塞进 sys.path

WEB_INDEX = APP / "web" / "index.html"


# ---------------------------------------------------------------- 起服务
def _boot() -> ThreadingHTTPServer:
    """在随机端口起一个真服务，靠它取数。

    不直接调 workbench.xxx()：那样只能拿到函数返回值，拿不到 handler 层的
    组装（口径字段、权限过滤、演示身份回落）。快照要和线上看到的一致，
    就得走同一个入口。端口用 0 让系统分配，不占用户正在用的 8090。
    """
    srv.Handler.log_message = lambda *a, **k: None  # noqa: ARG005  别刷屏
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _fetch(port: int, path: str, person: str, method: str = "GET") -> tuple[int, str, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    req.add_header("X-Person-Id", person)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001  单个接口失败不该毁掉整次导出
        return 0, "", json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


def _items(doc) -> list:
    """列表接口的字段名不统一（会话/会议/督办现在都叫 items，老代码里还有
    threads / meetings），统一在这里兜一下 —— 按错名字取会安静地拿到空列表，
    表现是"会话面板空白"，不报错，最难查。"""
    if not isinstance(doc, dict):
        return []
    for key in ("items", "threads", "meetings", "list"):
        val = doc.get(key)
        if isinstance(val, list):
            return val
    return []


# ---------------------------------------------------------------- 垫片
SHIM = r"""<script>
/* ── 离线快照垫片 ───────────────────────────────────────────────
   这份文件里的界面代码与 app/web/index.html 完全一致，区别只在下面这一段：
   /api/* 不再发请求，改从内联快照里答。因此不联网、不需要服务、不需要模型。
   快照生成于 @@TS@@，数据期次 @@PERIOD@@。
   未收录的接口回 501 并在控制台记一笔，不做"假装成功"的处理。 */
(function () {
  var SNAP = @@SNAP@@;
  var META = SNAP.__meta__;
  window.__OFFLINE__ = META;
  var MISS = (window.__offlineMiss = []);

  /* 这几条路径的查询串只是"筛一下"或"默认值"，拿不带查询串的版本答不会
     变成假数据；/api/knowledge 的 q= 是真的检索，不在名单里 —— 拿全量清单
     冒充检索结果，比直接说没有更糟。 */
  var QUERY_OK = {
    "/api/oversight": 1, "/api/schedule": 1, "/api/threads": 1,
    "/api/notify": 1, "/api/workspace": 1, "/api/experts": 1
  };

  function body(rec) {
    return new Response(rec.b, {
      status: rec.s || 200,
      headers: { "Content-Type": rec.ct || "application/json; charset=utf-8" }
    });
  }
  function apiPath(raw) {
    var s = String(raw || ""), at = s.indexOf("/api/");
    if (at < 0) return "";
    s = s.slice(at);
    var cut = s.length;
    var q = s.indexOf("?"), h = s.indexOf("#");
    if (q >= 0 && q < cut) cut = q;
    if (h >= 0 && h < cut) cut = h;
    return s.slice(0, cut);
  }
  function query(raw) {
    var s = String(raw || ""), at = s.indexOf("?");
    if (at < 0 || s.indexOf("/api/") < 0) return "";
    var q = s.slice(at);
    var h = q.indexOf("#");
    if (h >= 0) q = q.slice(0, h);
    return q === "?" ? "" : q;   // /api/knowledge? 这种空查询串要按不带参数算
  }
  function plain(msg) {
    return body({ s: 501, ct: "application/json; charset=utf-8",
                  b: JSON.stringify({ ok: false, error: msg }) });
  }

  /* 未收录时要能看懂：只说"没有这个接口"等于把内部路径甩给用户。
     /api/knowledge 的 q= 是真的语义检索，本地复现不了 —— 与其用关键词筛一遍
     冒充检索结果，不如说清楚这份文件里没有。 */
  var MISS_MSG = {
    "/api/knowledge": "离线快照不含检索：这是一份固定时刻的数据快照，搜不了；翻目录可以看全量，要搜请回工作台。"
  };

  window.fetch = function (input, init) {
    var raw = typeof input === "string" ? input : (input && input.url) || "";
    var method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
    var path = apiPath(raw);
    if (!path) return Promise.resolve(new Response("", { status: 204 }));

    var rec = SNAP[method + " " + path + query(raw)];
    if (!rec && QUERY_OK[path]) rec = SNAP[method + " " + path];
    if (rec && rec !== META) return Promise.resolve(body(rec));

    if (method !== "GET") {
      return Promise.resolve(plain("离线快照只读：这份文件不能提交修改，要改请回工作台。"));
    }
    MISS.push(method + " " + path + query(raw));
    if (window.console) console.warn("[offline] 快照未收录：", method, path, query(raw));
    return Promise.resolve(plain(MISS_MSG[path] || ("离线快照没有收录这个接口：" + path)));
  };

  /* 问答不走 fetch 垫片：让请求落到"未收录"会显示成「服务端出错了」，
     那是假故障。直接把入口拦住，说清楚这是快照、问不了。 */
  window.__offlineAskGuard = function () {
    var orig = window.ask;
    if (typeof orig !== "function") return;
    window.ask = function (q, opts) {
      var box = document.getElementById("q");
      var text = (q != null ? String(q) : (box && box.value) || "").trim();
      if (!text) return orig.call(this, q, opts);   // 空问题照旧：原来会提示先输入
      if (window.toast) {
        window.toast("这是离线快照：不含模型，问不了数。点看板上任意数字，可查口径与走势。");
      } else {
        alert("这是离线快照：不含模型，问不了数。");
      }
    };
  };

  window.__offlineBadge = function () {
    var bar = document.querySelector(".topbar");
    if (!bar) return;
    var s = document.createElement("span");
    s.className = "offline-badge";
    s.textContent = "离线快照";
    s.title = "数据截至 " + META.generated + "（指标期次 " + META.period + "）。"
            + "不含模型，不能问数；写操作不可用。";
    bar.appendChild(s);
  };

  var css = document.createElement("style");
  css.textContent = ".offline-badge{margin-left:auto;display:inline-flex;align-items:center;gap:6px;"
    + "height:20px;padding:0 9px;border-radius:999px;background:rgba(255,255,255,.16);"
    + "border:1px solid rgba(255,255,255,.3);color:#fff;font-size:11.5px;letter-spacing:.4px;"
    + "white-space:nowrap;cursor:help}"
    + ".offline-badge::before{content:'';width:6px;height:6px;border-radius:50%;background:#ffd66b}";
  document.head.appendChild(css);
})();
</script>
"""

TAIL = r"""<script>
/* 快照收尾：挂角标、拦问数入口。放在正文脚本之后 —— ask/toast 已经定义好了。 */
(function () {
  if (!window.__OFFLINE__) return;
  if (window.__offlineBadge) window.__offlineBadge();
  if (window.__offlineAskGuard) window.__offlineAskGuard();
})();
</script>
"""


# ---------------------------------------------------------------- 取数
def collect(port: int, person: str, with_threads: bool, max_threads: int) -> tuple[dict, dict]:
    records: dict[str, dict] = {}
    stats: dict[str, int] = {}

    def take(path: str, label: str, method: str = "GET") -> dict | None:
        status, ctype, text = _fetch(port, path, person, method)
        if status != 200:
            print(f"  ! {label:<28} HTTP {status}  {text[:120]}")
            return None
        key = f"{method} {path}"
        if key in records:
            return None
        records[key] = {"s": status, "b": text, "ct": "application/json; charset=utf-8"}
        stats[label] = stats.get(label, 0) + 1
        try:
            return json.loads(text)
        except ValueError:
            return None

    # 首屏一次性要的
    ov = take("/api/overview", "城市罗盘")
    take("/api/health", "健康检查")
    take("/api/focus", "民情焦点")
    take("/api/workspace", "个人工作台")
    people = take("/api/people", "身份名录")
    take("/api/auth/whoami", "登录态")
    take("/api/experts", "参谋名单")
    take("/api/models", "模型配置")
    take("/api/models/resolved", "可选模型")
    take("/api/plugins", "插件清单")
    take("/api/ask/examples", "首屏示例")
    take("/api/notify", "站内提醒")

    # 指标：罗盘 4 张 + 板块 5 块，但点开要的是全量 11 个，所以按字典走
    codes: list[str] = []
    for src in ((ov or {}).get("kpis") or []) + ((ov or {}).get("boards") or []):
        c = src.get("code")
        if c and c not in codes:
            codes.append(c)
    for c in getattr(srv.wb.md, "METRICS", {}) or {}:
        if c not in codes:
            codes.append(c)
    for c in codes:
        take(f"/api/metric/{c}", "指标明细")

    # 督办：列表 + 每一条详情（点开预警条要）
    # 两个都存：预警条读不带参数的 /api/oversight，台账弹窗读 ?window=N。
    # 只存带参数的版本，预警条会显示「台账暂无数据」—— 少了数据不报错，最难查。
    od = take("/api/oversight", "督办台账")
    take("/api/oversight?window=5", "督办扫描")
    for it in _items(od):
        iid = it.get("id")
        if iid:
            take(f"/api/oversight/{iid}", "督办详情")

    # 指示 / 知识库 / 公文 / 日程
    take("/api/directive", "领导指示")
    take("/api/knowledge", "知识库目录")
    take("/api/knowledge?kind=doc", "知识库·文档")
    take("/api/rag/bases", "政策库清单")
    take("/api/briefdoc/options", "材料助手选项")
    take("/api/wenshu/options", "公文选项")
    take("/api/schedule?days=14", "AI 日程")

    # 会议
    mt = take("/api/meetings", "会议清单")
    for m in _items(mt):
        mid = m.get("id")
        if not mid:
            continue
        take(f"/api/meetings/{mid}", "会议详情")
        take(f"/api/meetings/{mid}/notice", "会议通知")

    # 会话：脱不脱敏由 --no-threads 决定；默认带上，快照才不像空壳
    if with_threads:
        th = take("/api/threads", "历史会话")
        for t in _items(th)[:max_threads]:
            tid = t.get("id")
            if tid:
                take(f"/api/threads/{tid}", "会话正文")

    meta = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "period": (ov or {}).get("period") or "—",
        "disclosed_on": (ov or {}).get("disclosed_on") or "",
        "city": (ov or {}).get("city") or "",
        "person": person,
        "threads": with_threads,
        "endpoints": len(records),
    }
    return records, meta


# ---------------------------------------------------------------- 拼装
def build_html(records: dict, meta: dict) -> str:
    snap = {"__meta__": meta, **records}
    blob = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
    # 内联进 <script> 的两个致命序列：`</script>` 提前闭合、行分隔符当换行
    blob = blob.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

    page = WEB_INDEX.read_text(encoding="utf-8")
    shim = (SHIM.replace("@@SNAP@@", blob)
                .replace("@@TS@@", meta["generated"])
                .replace("@@PERIOD@@", meta["period"]))

    # 垫片必须早于正文脚本，但 charset 得留在最前面 —— 放到 <head> 之后第一行
    anchor = re.search(r'<meta charset="[^"]*">', page)
    if not anchor:
        raise SystemExit("index.html 里找不到 <meta charset>，结构变了，导出中止")
    page = page[: anchor.end()] + "\n" + shim + page[anchor.end():]

    if "</body>" not in page:
        raise SystemExit("index.html 里找不到 </body>，结构变了，导出中止")
    page = page.replace("</body>", TAIL + "</body>", 1)
    return page


def main() -> None:
    ap = argparse.ArgumentParser(description="导出离线的单文件 AI 市长工作台（不含模型）")
    ap.add_argument("--out", default="", help="输出路径，默认 dist/ai-mayor-offline-<时间>.html")
    ap.add_argument("--person", default="", help="按哪个身份取数，默认后端认定的当前身份")
    ap.add_argument("--no-threads", action="store_true", help="不导出历史会话（外发前建议加）")
    ap.add_argument("--max-threads", type=int, default=12, help="最多带几条会话正文")
    args = ap.parse_args()

    httpd = _boot()
    port = httpd.server_address[1]
    try:
        # 身份：先问接口，问不到就用默认
        who = ""
        try:
            _s, _c, txt = _fetch(port, "/api/people", args.person or "mayor")
            who = str((json.loads(txt).get("current") or {}).get("id") or "")
        except Exception:  # noqa: BLE001
            pass
        person = args.person or who or "mayor"

        print(f"取数中（身份 {person}，端口 {port}）…")
        records, meta = collect(port, person, not args.no_threads, args.max_threads)
    finally:
        httpd.shutdown()

    page = build_html(records, meta)

    out = Path(args.out) if args.out else (ROOT / "dist" / f"ai-mayor-offline-{datetime.now():%Y%m%d-%H%M}.html")
    if not out.is_absolute():
        out = (ROOT / out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")

    size = out.stat().st_size / 1024
    print(f"\n已导出 {out}")
    print(f"  {size:,.0f} KB · {meta['endpoints']} 个接口 · 指标期次 {meta['period']} · 数据截至 {meta['generated']}")
    if not args.no_threads:
        print("  含历史会话正文：外发前请过一眼，或加 --no-threads 重导。")
    print("  双击打开即可，不需要服务、不需要模型、不联网。")
    print("  能做的：看罗盘与看板、点开指标查口径/走势/来源、翻督办台账与知识库、切身份与主题。")
    print("  不能做的：问数（不含模型）、任何写入（签收/催办/建会/保存）。")


if __name__ == "__main__":
    main()
