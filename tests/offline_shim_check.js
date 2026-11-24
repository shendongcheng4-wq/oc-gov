/* 离线垫片自检：不开浏览器，只验「按身份取快照」这段逻辑。
 *
 * 为什么要单独测：垫片是导出物里唯一没有别处可对照的代码 —— 它错了，
 * 页面会静默显示别人的数据（切到秘书看到市长的日历），比报错难查得多。
 *
 *   node tests/offline_shim_check.js dist/xxx.html
 *
 * 验四件事：
 *   1. 带 X-Person-Id 能取到那个人自己的日程（秘书有交办、局长没有）；
 *   2. 不带身份时退回快照导出的那个身份，不是空白；
 *   3. 四身份共用的接口（罗盘）谁都拿得到；
 *   4. 写接口仍然回 501 —— 只读这条边界不许被悄悄放开。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const file = process.argv[2];
if (!file) {
  console.error("用法：node tests/offline_shim_check.js <导出的 html>");
  process.exit(2);
}

const html = fs.readFileSync(path.resolve(file), "utf-8");

// 取第一段「var SNAP = …」的脚本：垫片是唯一含这个赋值的地方
const at = html.indexOf("var SNAP = ");
if (at < 0) {
  console.error("✗ 没找到垫片（var SNAP），导出结构变了？");
  process.exit(1);
}
const open = html.lastIndexOf("<script>", at);
const close = html.indexOf("</script>", at);
const code = html.slice(open + "<script>".length, close);

// —— 最小 DOM 壳：垫片只在收尾处用一次 document ——
const noop = () => {};
const el = () => ({ textContent: "", className: "", title: "", style: {}, appendChild: noop });
const store = {};
const sandbox = {
  window: {},
  document: { head: { appendChild: noop }, createElement: el, querySelector: () => null },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
  },
  Response,
  ReadableStream,
  TextDecoder,
  console,
};
sandbox.window = Object.assign(sandbox.window, { console, localStorage: sandbox.localStorage });

const vm = require("vm");
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "offline-shim.js" });

const f = sandbox.window.fetch;
if (typeof f !== "function") {
  console.error("✗ 垫片没接管 window.fetch");
  process.exit(1);
}

const cases = [];
function check(name, cond, detail) {
  cases.push({ name, ok: !!cond, detail });
}

async function get(url, pid) {
  const init = pid ? { headers: { "X-Person-Id": pid } } : undefined;
  const res = await f(url, init);
  let body = null;
  try { body = await res.json(); } catch { body = null; }
  return { status: res.status, body };
}

(async () => {
  const sch = "/api/schedule?days=14";

  const sec = await get(sch, "secretary");
  check("秘书日程 200", sec.status === 200, sec.status);
  check("秘书日程里有 1 件领导交办",
    sec.body && sec.body.summary && sec.body.summary.assignments === 1,
    sec.body && sec.body.summary && sec.body.summary.assignments);

  const bur = await get(sch, "bureau");
  check("局长日程是另一份（无交办）",
    bur.body && bur.body.summary && bur.body.summary.assignments === 0,
    bur.body && bur.body.summary && bur.body.summary.assignments);
  check("秘书和局长的日程不是同一份",
    JSON.stringify(sec.body) !== JSON.stringify(bur.body), "");

  // 不带身份：退回 META.person（导出时排在第一个的身份）
  const fallback = await get(sch);
  check("不带身份也能取到（退回导出身份）", fallback.status === 200 && !!fallback.body, fallback.status);

  // localStorage 里的身份：登录后前端会用 X-Session，这条兜底得管用
  store["ocgov.person"] = "secretary";
  const viaStore = await get("/api/workspace");
  check("没带 X-Person-Id 时按 localStorage 认人", viaStore.status === 200, viaStore.status);
  delete store["ocgov.person"];

  const ov = await get("/api/overview");
  check("四身份共用的罗盘取得到", ov.status === 200 && !!ov.body.title, ov.body && ov.body.title);

  const write = await get("/api/assignment/done", "secretary");
  // get() 走的是 GET，这里单独发一个 POST 验只读边界
  const post = await f("/api/assignment/done", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Person-Id": "secretary" },
    body: JSON.stringify({ directive_id: "ZS2026-001", no: 1, done: true }),
  });
  const postBody = await post.json().catch(() => ({}));
  check("写接口回 501（只读边界）",
    post.status === 501 && /只读/.test(String(postBody.error || "")),
    `${post.status} ${postBody.error || ""}`);

  const miss = await get("/api/knowledge?q=财政");
  check("真检索不含在快照里（诚实报缺，不拿目录冒充）",
    miss.status === 501 && /检索/.test(String((miss.body || {}).error || "")),
    `${miss.status} ${(miss.body || {}).error || ""}`);

  let bad = 0;
  for (const c of cases) {
    if (c.ok) {
      console.log(`✓ ${c.name}`);
    } else {
      bad++;
      console.log(`✗ ${c.name}　${c.detail !== undefined ? "( " + c.detail + " )" : ""}`);
    }
  }
  console.log(`\n${cases.length - bad}/${cases.length} 通过`);
  process.exit(bad ? 1 : 0);
})();
