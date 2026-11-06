#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_release.py —— 把 oc-gov 打成一个「不含本机状态、解压即可跑」的交付包。

为什么要这个脚本，而不是直接压缩文件夹：
  1. .venv 有 380MB+，且 pyvenv.cfg 里写死了本机路径，拷到别处必坏 —— 不能带；
  2. data/ 里有会话、口令哈希、活跃 session、审计日志、上传的资料、模型配置，
     全部是本机私有状态，带出去就是事故 —— 不能带；
  3. config/local.env 和 config/notify.json 里有 API key / webhook / 邮箱授权码
     —— .gitignore 只管 git，管不了你手动打包；
  4. run-llm.cmd 硬编码了 D:\\ 和 E:\\ 的本机模型路径，且文件是 UTF-8 带中文路径
     —— 别人的机器上必然报错，交付包里不该有它。

用法：
    python tools/build_release.py                  # 打包 + 自检（默认）
    python tools/build_release.py --no-verify      # 只打包，跳过启动验证
    python tools/build_release.py --out D:\\dist    # 指定输出目录
    python tools/build_release.py --keep-staging   # 保留临时目录，便于排查

自检做了什么：把打好的 zip 解压到一个全新的空目录，用 OCGOV_DATA 指向一个空的
临时数据目录起服务，HTTP 请求首页确认 200，然后关掉。这验证的正是「对方第一次
双击」时会发生的事 —— 没有 data/、没有口令、没有模型配置。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- 排除清单

# 整目录排除（相对项目根）
EXCLUDE_DIRS = {
    ".git",            # 版本库，对方不需要；且含本机分支和远端地址
    ".venv",           # 380MB+，且路径写死不可搬迁
    "__pycache__",     # 字节码，跨 Python 版本还会误导
    ".workbuddy",      # 本地助手工作区
    "data",            # 运行时状态：会话/凭证/审计/上传资料/模型配置
    ".pytest_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
}

# 单文件排除（相对项目根的精确路径）
EXCLUDE_FILES = {
    "config/local.env",         # 真实 provider 与 key
    "config/notify.json",       # webhook / SMTP 授权码
    "config/llm.env",           # 本机模型路径（若存在）
    "docs/workbench.html",      # 生成物，内嵌了 data/ 快照，会夹带会话数据
    "run-llm.cmd",              # 硬编码本机 llama.cpp 与模型绝对路径
    "start-all.cmd",            # 依赖 run-llm.cmd，交付形态不需要
    "start-all.ps1",            # 同上
    ".DS_Store",
    "Thumbs.db",
}

# 后缀排除
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".bak", ".swp"}

# 敏感内容扫描：命中即视为「不该进包」，直接中止
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "疑似 OpenAI 风格 API key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "疑似 GitHub token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "疑似 AWS Access Key"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "疑似 Slack token"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"), "私钥"),
]
# 形如 KEY=value 的赋值，值够长才报警；占位符一律放行
ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|passwd|password|authorization)\b\s*[:=]\s*"
    r"[\"']?([A-Za-z0-9_\-\.]{16,})"
)
PLACEHOLDER_HINTS = ("your", "xxx", "example", "placeholder", "change", "todo", "redacted", "dummy")

# 本机绝对路径提示（不阻断，只提醒 —— README 里很多）
PATH_HINT_RE = re.compile(r"[A-Za-z]:[\\/](?:[^\s\"'<>|]{2,40})")

TEXT_SUFFIXES = {
    ".py", ".md", ".json", ".yaml", ".yml", ".txt", ".html", ".htm", ".js",
    ".css", ".cmd", ".bat", ".ps1", ".example", ".env", ".toml", ".cfg", ".ini",
}


def is_excluded(rel: Path) -> bool:
    parts = rel.parts
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    posix = rel.as_posix()
    if posix in EXCLUDE_FILES:
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    if rel.name.startswith("~$"):
        return True
    return False


def collect() -> tuple[list[Path], list[Path]]:
    """返回 (要打包的文件, 被排除的文件)，都相对项目根。

    用 os.walk 而不是 rglob：.venv 有上万个小文件，rglob 会逐个走进去再判定排除，
    剪枝能省掉绝大部分扫描时间。
    """
    keep: list[Path] = []
    drop: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        if here != ROOT:
            rel_dir = here.relative_to(ROOT)
            if any(p in EXCLUDE_DIRS for p in rel_dir.parts):
                continue
        for fn in sorted(filenames):
            rel = (here / fn).relative_to(ROOT)
            if is_excluded(rel):
                drop.append(rel)
            else:
                keep.append(rel)
    return keep, drop


# ---------------------------------------------------------------- 敏感扫描


def scan_secrets(files: list[Path]) -> tuple[list[str], list[str]]:
    """返回 (阻断级问题, 提示级问题)。"""
    blockers: list[str] = []
    warnings: list[str] = []
    for rel in files:
        if rel.suffix.lower() not in TEXT_SUFFIXES:
            continue
        p = ROOT / rel
        try:
            if p.stat().st_size > 2 * 1024 * 1024:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for pat, label in SECRET_PATTERNS:
            for m in pat.finditer(text):
                blockers.append(f"{rel}: {label} -> {m.group(0)[:24]}...")

        for m in ASSIGN_RE.finditer(text):
            value = m.group(2)
            low = value.lower()
            if any(h in low for h in PLACEHOLDER_HINTS):
                continue
            if low in {"localhost", "127.0.0.1", "bearer"}:
                continue
            blockers.append(f"{rel}: 疑似硬编码凭证 {m.group(1)}={value[:6]}...")

        for m in PATH_HINT_RE.finditer(text):
            token = m.group(0)
            if token.startswith(("D:\\溧阳", "E:\\octop", "C:\\Users", "D:\\workbuddy")):
                warnings.append(f"{rel}: 本机路径 {token}")

    return blockers, warnings


# ---------------------------------------------------------------- 包内说明页

START_HERE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>oc-gov AI 市长工作台 · 使用说明</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 48px 24px 80px;
    background: #f6f7f9; color: #1c2024;
    font: 15px/1.75 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  main { max-width: 780px; margin: 0 auto; }
  h1 { font-size: 26px; margin: 0 0 6px; letter-spacing: -0.3px; }
  .sub { color: #6b7280; font-size: 14px; margin-bottom: 34px; }
  h2 {
    font-size: 17px; margin: 38px 0 12px; padding-bottom: 8px;
    border-bottom: 1px solid #e3e6ea;
  }
  .card {
    background: #fff; border: 1px solid #e3e6ea; border-radius: 10px;
    padding: 18px 20px; margin: 14px 0;
  }
  ol, ul { padding-left: 22px; margin: 10px 0; }
  li { margin: 7px 0; }
  code {
    background: #eef1f4; padding: 1px 6px; border-radius: 4px;
    font: 13px/1.5 "Cascadia Code", Consolas, monospace; color: #0f172a;
  }
  pre {
    background: #1c2024; color: #e8eaed; padding: 14px 16px; border-radius: 8px;
    overflow-x: auto; font: 13px/1.7 "Cascadia Code", Consolas, monospace;
  }
  pre code { background: none; color: inherit; padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }
  th, td { border: 1px solid #e3e6ea; padding: 9px 12px; text-align: left; vertical-align: top; }
  th { background: #f0f2f5; font-weight: 600; }
  .warn {
    background: #fff8e6; border: 1px solid #f0dca8; border-radius: 8px;
    padding: 14px 18px; margin: 16px 0;
  }
  .warn strong { color: #92400e; }
  .muted { color: #6b7280; font-size: 13px; }
  footer { margin-top: 56px; padding-top: 18px; border-top: 1px solid #e3e6ea; color: #9aa1a9; font-size: 13px; }
</style>
</head>
<body>
<main>
  <h1>AI 市长工作台</h1>
  <div class="sub">版本 {{VERSION}} · 打包于 {{BUILT_AT}} · 纯本地运行，不联网也能用（模型除外）</div>

  <div class="warn">
    <strong>先说清楚：这套东西的定位。</strong>
    11 个指标里只有 GDP 一项是公开发布的年度真实值（2025 年 1746.29 亿元，来源《2025 年溧阳市国民经济和社会发展统计公报》），
    其余 10 个、以及同比和走势，全部是构造的演示数据。它证明的是「链路通、口径能被追问」，<b>不是</b>「数据可信」。
    拿去做正式汇报前请先看 <code>README.md</code> 的「这个页面没证明什么」一节。
  </div>

  <h2>一、把程序跑起来</h2>

  <p><b>前置条件只有一个：Windows + Python 3.11 或更高版本。</b>没装过 Python 的话，去
  <code>python.org</code> 下载安装时，<b>务必勾选「Add python.exe to PATH」</b>，否则脚本找不到它。</p>

  <div class="card">
    <ol>
      <li>把这个文件夹解压到<b>硬盘上的普通目录</b>，比如 <code>D:\\oc-gov\\</code>。<br>
          <span class="muted">别放在 C:\\Program Files 下面，那里写文件需要管理员权限，数据会存不下来。</span></li>
      <li>双击 <code>run.cmd</code>。窗口里会滚出几行日志，然后浏览器自动打开。</li>
      <li>看到 <code>http://127.0.0.1:8090</code> 的界面就成了。关掉黑窗口即停止服务。</li>
    </ol>
  </div>

  <p>换端口：命令行里执行 <code>run.cmd 8091</code>。<br>
  8090 被占用时脚本会直接报错并提示，不会静默失败。</p>

  <h2>二、第一次登录</h2>

  <p>首次启动时，<b>黑窗口里会打印一行登录口令</b>，长这样：</p>
  <pre><code>首次启动：已生成登录口令 XXXX-XXXX-XXXX</code></pre>

  <p>用它登录。<b>这个口令只打印一次</b>，磁盘上存的是 pbkdf2 哈希，不是明文 —— 忘了就只能重置（见第五节）。</p>

  <p>不登录也能浏览全部看板和指标，但<b>一切写操作会被拒绝</b>并留下审计记录：建档、流转、催办、建会都不行。
  登录后按身份判定权限，市领导 ⊃ 分管领导 ⊃ 委办局，越权会被挡下并说明理由。</p>

  <p>想固定口令，启动前设环境变量（环境变量优先级最高）：</p>
  <pre><code>set OCGOV_PASSWORD=你的口令
run.cmd</code></pre>

  <h2>三、接上你们的模型</h2>

  <p>本包<b>不包含任何模型</b>，也不含推理服务。它需要一个 OpenAI 兼容的接口地址。有两种接法：</p>

  <div class="card">
    <p><b>做法 A（推荐，不碰配置文件）：</b>打开页面右上角的模型胶囊 → 进入「模型配置」→ 新增服务商，填三项：</p>
    <ul>
      <li><b>Base URL</b> — 对方服务地址，要带 <code>/v1</code>，例如 <code>http://10.0.0.5:8099/v1</code> 或 <code>https://api.deepseek.com/v1</code></li>
      <li><b>模型名</b> — 服务端实际暴露的名字，例如 <code>local-qwen38-27b</code></li>
      <li><b>API Key</b> — 本地服务随便填个非空值（如 <code>local</code>）即可；云服务填真 key</li>
    </ul>
    <p>填完点「测试连通性」，它是真发一次请求，不是只检查字段格式。</p>
  </div>

  <div class="card">
    <p><b>做法 B（预先写好，适合批量部署）：</b>把 <code>config/local.env.example</code> 复制成
    <code>config/local.env</code>，填三行：</p>
    <pre><code>OCTOP_LLM_BASE_URL=http://10.0.0.5:8099/v1
OCTOP_LLM_MODEL=local-qwen38-27b
OCTOP_LLM_API_KEY=local</code></pre>
    <p>首次启动会自动导入成一条 provider 配置。</p>
  </div>

  <p><b>不配模型也能跑。</b>问数会退回规则解析，罗盘、看板、督办预警、会议台账、材料助手都不受影响 —— 少的是自由问句的理解能力，不是数据。</p>

  <h2>四、数据存在哪，怎么备份</h2>

  <p>所有状态都是 <code>data/</code> 目录下的 JSON 文件，第一次启动时自动生成，不需要你事先建目录。</p>

  <table>
    <tr><th>文件</th><th>内容</th><th>要不要备份</th></tr>
    <tr><td><code>oversight-state.json</code></td><td>督办台账</td><td>要，这是业务数据</td></tr>
    <tr><td><code>meetings.json</code></td><td>会议记录</td><td>要</td></tr>
    <tr><td><code>chats.json</code></td><td>对话历史</td><td>可选</td></tr>
    <tr><td><code>credentials.json</code> / <code>sessions.json</code></td><td>口令哈希与登录会话</td><td>不用，删了会重新生成</td></tr>
    <tr><td><code>audit.jsonl</code></td><td>审计日志，只追加</td><td>建议定期归档</td></tr>
    <tr><td><code>models.json</code></td><td>模型服务配置（含 key）</td><td>要，但别外发</td></tr>
  </table>

  <p><b>备份</b>：停掉服务，整个复制 <code>data/</code> 目录即可。<br>
  <b>重置成干净状态</b>：停掉服务，删掉 <code>data/</code> 整个目录，重新双击 <code>run.cmd</code>。会重新生成口令和 8 条示例督办台账。</p>

  <div class="warn">
    <strong>数据目录可以挪出程序目录。</strong>用环境变量 <code>OCGOV_DATA</code> 指到别处，
    这样升级程序时覆盖文件夹不会冲掉业务数据：
    <pre><code>set OCGOV_DATA=D:\\oc-gov-data
run.cmd</code></pre>
  </div>

  <h2>五、忘了口令怎么办</h2>

  <p>删掉 <code>data\\credentials.json</code>，重启服务，会重新打印一条新口令。已有的登录会话一并失效。</p>

  <h2>六、遇到问题</h2>

  <table>
    <tr><th>现象</th><th>原因与处理</th></tr>
    <tr><td>双击 <code>run.cmd</code> 一闪而过</td><td>没装 Python 或没进 PATH。开命令行执行 <code>python -V</code> 验证，应输出 3.11 以上版本号。</td></tr>
    <tr><td><code>port 8090 is already in use</code></td><td>已被占用。要么直接在浏览器打开 <code>http://127.0.0.1:8090/</code>（说明已在跑），要么换端口 <code>run.cmd 8091</code>。</td></tr>
    <tr><td>页面能开，但问数答「没听懂」</td><td>模型没配上，正在走规则解析。按第三节配一个。</td></tr>
    <tr><td>连通性测试失败</td><td>三个常见原因：Base URL 漏了 <code>/v1</code>；模型名和服务端暴露的不一致；对方机器防火墙没放行端口。</td></tr>
    <tr><td>重启后数据没了</td><td>很可能放在 <code>C:\\Program Files</code> 等受保护目录，写入被系统拦掉了。换到 <code>D:\\</code> 下的普通目录。</td></tr>
  </table>

  <h2>七、几条必须知道的限制</h2>

  <ul>
    <li><b>只支持单进程。</b>用 <code>gunicorn</code> 或多 worker 起两份会互相覆盖数据。多人同时用请先换 SQLite/PostgreSQL（改 <code>core/store.py</code>）。</li>
    <li><b>权限只做了最小一层。</b>登录、三档角色、写接口总闸门都有；但身份不是从组织架构同步的，<code>core/directory.py</code> 里是手写映射。读收窄只覆盖督办和知识库，指标/会议/日程仍是全量可读。</li>
    <li><b>提醒默认一条通道都没配。</b>站内信和浏览器桌面通知开箱可用；企微机器人、SMTP 邮件要填凭证才会真正送出去，页面「送达配置」里能看到当前状态。</li>
    <li><b>不响应式。</b>按最小宽度 1180 设计，窗口更窄会出现横向滚动。</li>
    <li><b>材料只收文本可读的格式。</b>md/txt/csv/json/html/docx/xlsx 可以，PDF、图片、扫描件、旧版 .doc/.xls 读不了。</li>
  </ul>

  <footer>
    本包不含任何运行时数据、登录口令或 API key —— 全部在你的机器上首次运行时生成。<br>
    技术细节与完整局限见同目录 <code>README.md</code>；权限设计见 <code>docs/permission-design.md</code>。
  </footer>
</main>
</body>
</html>
"""


# ---------------------------------------------------------------- 工具函数


def pick_port() -> int:
    for port in range(18190, 18290):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("找不到空闲端口")


def wait_http(url: str, timeout: float = 40.0) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                body = r.read().decode("utf-8", "ignore")
                if r.status == 200 and len(body) > 500:
                    return True, f"HTTP 200，{len(body)} 字节"
                last = f"HTTP {r.status}，{len(body)} 字节"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:  # 连接被拒 / 超时，服务还没起来
            last = type(e).__name__
        time.sleep(0.6)
    return False, last


def verify(zip_path: Path, keep: bool) -> tuple[bool, list[str]]:
    """解压到全新目录，以空 data 目录起服务，确认首页可访问。"""
    logs: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="ocgov-verify-"))
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        tops = [p for p in tmp.iterdir() if p.is_dir()]
        if len(tops) != 1:
            return False, [f"解压后顶层目录数量异常：{[p.name for p in tops]}"]
        pkg = tops[0]
        logs.append(f"解压到 {pkg.name}/")

        # 包里不该有这些
        for bad in ("data", "config/local.env", ".venv", "run-llm.cmd"):
            if (pkg / bad).exists():
                return False, logs + [f"包里混进了不该有的东西：{bad}"]

        data_dir = tmp / "_fresh_data"
        data_dir.mkdir()
        port = pick_port()
        env = dict(os.environ)
        env["OCGOV_DATA"] = str(data_dir)
        env["OCGOV_PASSWORD"] = "verify-only"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(pkg / "app" / "main.py"), str(port)],
            cwd=str(pkg),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        try:
            ok, detail = wait_http(f"http://127.0.0.1:{port}/")
            logs.append(f"首页探测：{detail}")
            if not ok:
                try:
                    out = proc.communicate(timeout=3)[0] or ""
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out = ""
                tail = "\n".join(out.strip().splitlines()[-25:])
                return False, logs + ["服务没起来，输出末尾：", tail]

            # 干净基线检查：data 目录里应该已经自动生出文件，且是新口令
            generated = sorted(p.name for p in data_dir.glob("*.json"))
            logs.append(f"首次启动自动生成：{', '.join(generated) or '（无）'}")
            if not generated:
                return False, logs + ["data 目录没有被初始化，首次启动可能没跑通"]
            return True, logs
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            logs.append(f"临时目录已保留：{tmp}")


# ---------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 oc-gov 交付包")
    ap.add_argument("--out", default=r"E:\octop-dashan\dist", help="输出目录")
    ap.add_argument("--no-verify", action="store_true", help="跳过解压启动验证")
    ap.add_argument("--keep-staging", action="store_true", help="保留解压验证的临时目录")
    ap.add_argument("--force", action="store_true", help="跳过敏感内容阻断（慎用）")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 66)
    print("  oc-gov 交付打包")
    print("=" * 66)

    keep, drop = collect()
    print(f"\n[1/5] 收集文件：保留 {len(keep)} 个，排除 {len(drop)} 个")

    dropped_dirs = sorted({str(Path(*r.parts[:2])) if len(r.parts) > 1 else r.as_posix()
                           for r in drop})
    preview = [d for d in dropped_dirs if not d.endswith(".pyc")][:12]
    if preview:
        print("      主要排除：" + "、".join(preview) + (" ..." if len(dropped_dirs) > 12 else ""))

    print("\n[2/5] 敏感内容扫描 ...")
    blockers, warnings = scan_secrets(keep)
    if warnings:
        uniq = sorted(set(warnings))
        print(f"      提示：{len(uniq)} 处本机路径/环境痕迹（不阻断）")
        for w in uniq[:5]:
            print(f"        · {w}")
        if len(uniq) > 5:
            print(f"        · ... 另有 {len(uniq) - 5} 处")
    if blockers:
        uniq = sorted(set(blockers))
        print(f"      [!] 发现 {len(uniq)} 处疑似凭证：")
        for b in uniq[:15]:
            print(f"        · {b}")
        if not args.force:
            print("\n      已中止。确认是误报后可用 --force 跳过，但请先人工核对。")
            return 2
        print("      --force 已指定，继续打包。")
    else:
        print("      未发现明文凭证。")

    # 版本号
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "unknown"
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        commit += "-dirty" if dirty else ""
    except Exception:
        commit = "unknown"

    today = dt.date.today().strftime("%Y%m%d")
    version = f"{today}.{commit}"
    name = f"oc-gov-{today}"

    staging = out_dir / "_staging"
    if staging.exists():
        shutil.rmtree(staging)
    pkg_root = staging / name

    print(f"\n[3/5] 复制到暂存区 ...")
    for rel in keep:
        dst = pkg_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)

    # 包内使用说明（文件名保持 ASCII，避免解压工具把中文名解乱）
    (pkg_root / "START-HERE.html").write_text(
        START_HERE.replace("{{VERSION}}", version).replace(
            "{{BUILT_AT}}", dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
        encoding="utf-8",
    )

    zip_path = out_dir / f"{name}.zip"
    if zip_path.exists():
        zip_path.unlink()

    print(f"\n[4/5] 压缩 ...")
    total_raw = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(pkg_root.rglob("*")):
            if p.is_file():
                arc = Path(name) / p.relative_to(pkg_root)
                z.write(p, arc.as_posix())
                total_raw += p.stat().st_size

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"      {zip_path}")
    print(f"      原始 {total_raw/1024/1024:.1f} MB → 压缩后 {size_mb:.2f} MB")

    shutil.rmtree(staging, ignore_errors=True)

    if args.no_verify:
        print("\n[5/5] 已跳过启动验证（--no-verify）")
        print("\n完成。注意：未经启动验证的包不保证在干净环境可用。")
        return 0

    print("\n[5/5] 解压到全新目录试跑 ...")
    ok, logs = verify(zip_path, args.keep_staging)
    for line in logs:
        for i, sub in enumerate(line.splitlines()):
            print(("      " if i == 0 else "        ") + sub)

    if not ok:
        print("\n      [!] 验证失败。这个包不要发出去。")
        return 1

    print("\n" + "=" * 66)
    print(f"  打包完成并通过验证：{zip_path.name}")
    print(f"  对方操作：解压 → 双击 run.cmd → 看窗口里的首次口令")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
