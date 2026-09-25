#!/usr/bin/env python
"""把 GitHub Star 的项目全部导出成 CSV / Markdown 表格。

依赖：已登录的 gh CLI (gh auth login)

用法:
  python stars.py                        # 导出 star -> stars.csv
  python stars.py --user some-name       # 导出指定用户的公开 star
  python stars.py --account other-acct   # 指定用哪个 gh 账号的 token
  python stars.py --all                  # 同时生成 .md 和 .html
  python stars.py -o my-stars.csv        # 指定输出文件名
  python stars.py --sort stars           # 按 star 数排序（默认按收藏时间倒序）

默认导出对象依次为：--user 参数 > 同目录 .stars-user 文件 > 当前登录账号。
"""
from __future__ import annotations  # 让 str | None 这类注解在 3.8 / 3.9 上也能用

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# \s 在 str 模式下会匹配 Unicode 空白，包括全角空格 U+3000 和 NBSP U+00A0
_WS = re.compile(r"\s+")


def clean_text(text) -> str:
    """折叠所有空白字符为单个半角空格并 strip。

    有些仓库会在描述里塞几百上千个全角空格（U+3000）或换行，
    不处理的话在表格里会撑出一大片空白。
    """
    return _WS.sub(" ", str(text or "")).strip()


def clip(text, limit: int) -> str:
    """按字符数截断并加省略号；limit <= 0 表示不截断。"""
    text = str(text or "")
    if limit and limit > 0 and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def resolve_user(cli_user: str | None) -> str | None:
    """确定要导出谁：命令行 > 同目录的 .stars-user 配置文件 > 当前登录账号。

    .stars-user 是 gitignored 的，把自己的用户名写在里面就不用每次带 --user，
    也不必把用户名硬编码进脚本或批处理（方便开源）。
    """
    if cli_user:
        return cli_user.strip()
    cfg = Path(__file__).with_name(".stars-user")
    if cfg.exists():
        first = cfg.read_text(encoding="utf-8").splitlines()
        if first and first[0].strip():
            return first[0].strip()
    return None

COLUMNS = [
    ("starred_at", "收藏时间"),
    ("full_name", "仓库"),
    ("language", "语言"),
    ("stars", "Star数"),
    ("description", "描述"),
    ("topics", "标签"),
    ("url", "链接"),
    ("homepage", "官网"),
    ("archived", "已归档"),
    ("fork", "是Fork"),
    ("license", "协议"),
    ("created_at", "创建时间"),
    ("pushed_at", "最后提交"),
]


def gh_api(path: str, account: str | None = None) -> list[dict]:
    """调用 gh api 并返回所有分页的 JSON 对象列表。"""
    cmd = [
        "gh",
        "api",
        "--paginate",
        "-H",
        "Accept: application/vnd.github.star+json",
        path,
        "--jq",
        ".[]",
    ]
    env = None
    if account:
        token = subprocess.run(
            ["gh", "auth", "token", "--user", account],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if token.returncode != 0:
            sys.exit(f"拿不到账号 {account} 的 token，先 `gh auth login` 一下。")
        import os

        env = {**os.environ, "GH_TOKEN": token.stdout.strip()}

    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )
    if proc.returncode != 0:
        sys.exit(f"gh api 失败:\n{proc.stderr.strip()}")

    items = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def to_row(item: dict) -> dict:
    repo = item.get("repo", item)  # 带 starred_at 时仓库信息在 repo 里
    lic = (repo.get("license") or {}).get("spdx_id") or ""
    return {
        "starred_at": (item.get("starred_at") or "")[:19].replace("T", " "),
        "full_name": repo.get("full_name", ""),
        "language": repo.get("language") or "",
        "stars": repo.get("stargazers_count", 0),
        "description": clean_text(repo.get("description")),
        "topics": clean_text(" ".join(repo.get("topics") or [])),
        "url": repo.get("html_url", ""),
        "homepage": repo.get("homepage") or "",
        "archived": "是" if repo.get("archived") else "",
        "fork": "是" if repo.get("fork") else "",
        "license": "" if lic == "NOASSERTION" else lic,
        "created_at": (repo.get("created_at") or "")[:10],
        "pushed_at": (repo.get("pushed_at") or "")[:10],
    }


def _truthy(v) -> bool:
    """兼容 CSV 里可能出现的多种真值写法。"""
    return str(v).strip().lower() in ("是", "true", "yes", "1", "y")


def read_csv(path: str) -> list[dict]:
    """把之前导出的 CSV 读回内部结构（表头是中文，这里反向映射）。

    注意：archived / fork 保持 CSV 里的原始字符串（如 "是"/""），
    不要转成 bool，否则回写 CSV 会变成 True/False 而污染数据。
    """
    label2key = {label: key for key, label in COLUMNS}
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for raw in csv.DictReader(f):
            r = {label2key.get(k, k): v for k, v in raw.items()}
            try:
                r["stars"] = int(r.get("stars") or 0)
            except (TypeError, ValueError):
                r["stars"] = 0
            # 旧版 CSV 可能留着未折叠的空白（如成百上千个全角空格），这里补洗一遍
            for k in ("description", "topics"):
                if k in r:
                    r[k] = clean_text(r[k])
            rows.append(r)
    return rows


def write_html(rows: list[dict], out: Path, max_desc: int = 500) -> None:
    """把数据内嵌进单文件 HTML（双击即可离线使用）。"""
    tpl_path = Path(__file__).with_name("stars_template.html")
    if not tpl_path.exists():
        print(f"⚠️  找不到模板 {tpl_path}，跳过 HTML。", file=sys.stderr)
        return

    keys = [k for k, _ in COLUMNS] + ["created_at", "pushed_at"]
    payload = [
        {
            **{k: r.get(k, "") for k in keys},
            # JSON 里给真正的 bool，前端 JS 才能直接做真值判断
            "archived": _truthy(r.get("archived")),
            "fork": _truthy(r.get("fork")),
        }
        for r in rows
    ]
    # 内嵌 JSON：转义 </ 防止提前终止 <script> 标签。
    # 注意：HTML 里保留完整描述，由前端 JS 截断显示 + 点击展开，
    # 所以只有这一步会写入 __MAX_DESC__。
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (
        tpl_path.read_text(encoding="utf-8")
        .replace("__STARS_DATA__", blob)
        .replace("__MAX_DESC__", str(max_desc))
    )
    out.write_text(html, encoding="utf-8")
    print(f"✅ HTML（{out.stat().st_size / 1024:.0f} KB，已内嵌数据）→ {out.resolve()}")


def demo_rows() -> list[dict]:
    """生成一份假数据，用于截图 / 演示 / 无网络时试跑（完全不访问 API）。

    覆盖各种渲染情况：已归档、fork、空描述、无语言、超长描述、
    几十个标签、不同协议和时间分布。
    """
    from datetime import datetime, timedelta

    # (仓库, 语言, Star数, 描述, 标签, 已归档, 是fork, 协议, 收藏于几天前, 官网)
    specs = [
        ("octocat/awesome-selfhosted", "Python", 41200,
         "A curated list of self-hostable services and tools. PRs welcome.",
         "awesome-list selfhosted docker", False, False, "MIT", 3, "https://selfhosted.example.com"),
        ("octocat/terminal-tools", "Rust", 9840,
         "Fast, dependency-free CLI utilities for everyday terminal work.",
         "cli rust terminal productivity", False, False, "Apache-2.0", 9, ""),
        ("sample-labs/vector-db-bench", "Python", 3270,
         "Benchmark harness for comparing open-source vector databases.",
         "benchmark vector-database embeddings python", False, False, "MIT", 15, ""),
        ("sample-labs/ui-kit", "TypeScript", 15600,
         "Headless, accessible React components with zero runtime styles.",
         "react typescript components accessibility", False, False, "MIT", 22, "https://uikit.example.com"),
        ("demo-org/llm-from-scratch", "Jupyter Notebook", 27800,
         "Build a small GPT from scratch, with careful math notes and no dependencies.",
         "llm transformer tutorial notebook education", False, False, "MIT", 31, ""),
        ("demo-org/zig-http", "Zig", 2410,
         "Minimal HTTP/1.1 server in Zig. Educational, readable, fast.",
         "zig http server networking", False, False, "MIT", 44, ""),
        ("acme/gopls", "Go", 8730,
         "Language server for Go. Editor-agnostic, batteries included.",
         "go lsp editor tooling", False, False, "BSD-3-Clause", 52, ""),
        ("acme/observability-stack", "Go", 1120,
         "OpenTelemetry collector configs and dashboards that actually make sense.",
         "observability opentelemetry monitoring grafana", False, False, "Apache-2.0", 61, ""),
        ("moonlight/data-pipeline", "Python", 4390,
         "Typed, testable ETL pipelines with a tiny API surface.",
         "etl pipeline data-engineering typing", False, False, "MIT", 73, ""),
        ("moonlight/old-experiments", "C++", 12,
         "Random experiments from 2019. Kept for nostalgia.",
         "", True, False, "Unlicense", 88, ""),
        ("orchard/wasm-runtime", "Rust", 33400,
         "A small, embeddable WebAssembly runtime.",
         "wasm runtime rust embedded sandbox", False, False, "Apache-2.0", 96, ""),
        ("orchard/css-scope", "JavaScript", 780,
         "Scope CSS without a build step. 2KB, no deps.",
         "css javascript shadow-dom browser", False, False, "MIT", 104, ""),
        ("pixel-co/screenshot-diff", "Swift", 1860,
         "Pixel-perfect visual regression testing for iOS.",
         "ios swift testing visual-regression", False, False, "MIT", 118, ""),
        ("pixel-co/design-tokens", "TypeScript", 520,
         "Design tokens as code, exportable to CSS / Swift / Kotlin.",
         "design-tokens typescript css", False, False, "MIT", 129, "https://tokens.example.com"),
        ("northwind/sql-formatter", "Ruby", 6400,
         "Opinionated SQL formatter with dialect support.",
         "sql formatter ruby postgres", False, False, "MIT", 141, ""),
        ("northwind/legacy-monolith", "Java", 96,
         "Split out of our monolith. Public for transparency.",
         "", False, False, "Apache-2.0", 155, ""),
        ("octocat/dotfiles", "Shell", 21800,
         "My dotfiles. Copy-paste at your own risk.",
         "dotfiles shell zsh", False, True, "MIT", 166, ""),
        ("demo-org/awesome-llm-agents", "Python", 55300,
         "A gigantic, only lightly maintained list of LLM agent frameworks, papers, prompts, "
         "toolchains, benchmarks and assorted links. Contributions are welcome, but please read "
         "the contributing guide before opening a PR, as the maintainers are volunteers and "
         "cannot review large diffs quickly. See the appendix for a full taxonomy.",
         "llm agents awesome-list papers prompts toolchains benchmark", False, False, "CC0-1.0", 171, ""),
        ("sample-labs/kotlin-flows-demo", "Kotlin", 340,
         "Teaching material for Kotlin Flow and coroutines.",
         "kotlin coroutines flow android", False, False, "Apache-2.0", 183, ""),
        ("acme/vue-datagrid", "Vue", 7200,
         "Virtualized data grid for Vue 3. 60fps at 100k rows.",
         "vue datagrid virtual-scroll table", False, False, "MIT", 192, "https://datagrid.example.com"),
        ("moonlight/haskell-parser", "Haskell", 1450,
         "Parser combinators explained with a JSON parser you can read in one sitting.",
         "haskell parser json tutorial", False, False, "BSD-3-Clause", 205, ""),
        ("orchard/elixir-chat", "Elixir", 3900,
         "Realtime chat reference app built on Phoenix LiveView.",
         "elixir phoenix liveview websocket", False, False, "MIT", 219, ""),
        ("northwind/lua-neovim-config", "Lua", 880,
         "A minimal, well-commented Neovim config.",
         "neovim lua configuration", False, False, "MIT", 233, ""),
        ("pixel-co/csharp-grpc", "C#", 2760,
         "Production-ready gRPC templates for .NET.",
         "dotnet grpc templates csharp", False, False, "MIT", 248, ""),
        ("demo-org/html-report", "HTML", 430,
         "Single-file HTML reports from CSV. No server, no tracking.",
         "html report csv offline single-file", False, False, "MIT", 262, ""),
        ("sample-labs/sqlite-ext", "C", 5100,
         "Useful SQLite extensions in a single amalgamation file.",
         "sqlite c extension embedded", False, False, "MIT", 279, ""),
        ("acme/deprecated-sdk", "JavaScript", 6800,
         "Deprecated. Use @acme/sdk-next instead.",
         "deprecated sdk javascript", True, False, "MIT", 298, ""),
        ("octocat/cobol-modern", "COBOL", 1900,
         "Modern COBOL examples. Yes, really.",
         "cobol legacy mainframe", False, False, "MIT", 315, ""),
        ("demo-org/mystery-repo", "", 0,
         "", "", False, False, "", 330, ""),
    ]

    base = datetime(2026, 9, 20, 14, 30, 0)
    rows = []
    for i, (name, lang, star, desc, topics, arch, fork, lic, days, home) in enumerate(specs):
        starred = base - timedelta(days=days, minutes=i * 17)
        rows.append({
            "starred_at": starred.strftime("%Y-%m-%d %H:%M:%S"),
            "full_name": name,
            "language": lang,
            "stars": star,
            "description": clean_text(desc),
            "topics": clean_text(topics),
            "url": f"https://github.com/{name}",
            "homepage": home,
            "archived": "是" if arch else "",
            "fork": "是" if fork else "",
            "license": lic,
            "created_at": (starred - timedelta(days=200 + (i * 53) % 1200)).strftime("%Y-%m-%d"),
            "pushed_at": (starred - timedelta(days=(i * 7) % 90)).strftime("%Y-%m-%d"),
        })
    return rows


def main() -> None:
    # Windows 控制台默认 GBK，强制 UTF-8 输出，避免中文报错乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    if shutil.which("gh") is None:
        sys.exit("没找到 gh CLI，装一个：https://cli.github.com/")

    ap = argparse.ArgumentParser(description="导出 GitHub Star 列表")
    ap.add_argument("-o", "--output", default="github-stars.csv", help="CSV 输出路径")
    ap.add_argument(
        "--user",
        help="要导出的用户名；省略时依次尝试 .stars-user 配置文件、当前登录账号",
    )
    ap.add_argument(
        "--from-csv",
        help="不从 API 拉取，直接读已有 CSV 重建 md/html（改模板时很省事）",
    )
    ap.add_argument("--account", help="使用哪个 gh 登录账号的 token")
    ap.add_argument("--md", action="store_true", help="同时生成同名 .md 表格")
    ap.add_argument("--html", action="store_true", help="同时生成可搜索的单文件 HTML")
    ap.add_argument("--all", action="store_true", help="= --md --html")
    ap.add_argument(
        "--demo",
        action="store_true",
        help="用内置假数据生成样例（截图/演示用，不调 API）",
    )
    ap.add_argument(
        "--max-desc",
        type=int,
        default=500,
        help="描述截断长度（字符），默认 500；填 0 表示不截断（CSV/Markdown 生效，HTML 可点击展开全文）",
    )
    ap.add_argument(
        "--sort",
        choices=["starred_at", "stars", "full_name", "language"],
        default="starred_at",
        help="排序字段（默认收藏时间倒序）",
    )
    args = ap.parse_args()

    if args.from_csv:
        print(f"从 {args.from_csv} 读取（不调用 API）...", file=sys.stderr)
        rows = read_csv(args.from_csv)
        if not rows:
            sys.exit("CSV 里没有数据。")
    elif args.demo:
        rows = demo_rows()
        print(f"使用内置演示数据（{len(rows)} 条假仓库，不访问 API）", file=sys.stderr)
    else:
        user = resolve_user(args.user)
        path = (
            f"/users/{user}/starred?per_page=100"
            if user
            else "/user/starred?per_page=100"
        )
        print(f"正在拉取 {user or '当前登录账号'} 的 star ...", file=sys.stderr)
        items = gh_api(path, args.account)
        if not items:
            sys.exit("0 条结果。检查一下用户名，或者这个账号确实没 star。")
        rows = [to_row(it) for it in items]
    reverse = args.sort in ("starred_at", "stars")
    rows.sort(key=lambda r: r[args.sort], reverse=reverse)

    # --from-csv 且未指定输出名时，就地重建产物（csv 会被原样重写，无损）
    if args.from_csv and args.output == "github-stars.csv":
        out = Path(args.from_csv)
    else:
        out = Path(args.output)
    # utf-8-sig: 带 BOM，Excel/WPS 打开中文和 Emoji 不乱码
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[k for k, _ in COLUMNS], extrasaction="ignore")
        w.writerow({k: label for k, label in COLUMNS})
        for r in rows:
            w.writerow({**r, "description": clip(r["description"], args.max_desc)})
    print(f"✅ {len(rows)} 条已写入 {out.resolve()}")

    if args.md or args.all:
        md = out.with_suffix(".md")
        with md.open("w", encoding="utf-8") as f:
            f.write(f"# GitHub Starred ({len(rows)})\n\n")
            f.write("| " + " | ".join(label for _, label in COLUMNS) + " |\n")
            f.write("|" + "---|" * len(COLUMNS) + "\n")
            for r in rows:
                row = {**r, "description": clip(r["description"], args.max_desc)}
                cells = [str(row[k]).replace("|", "\\|") for k, _ in COLUMNS]
                cells[1] = f"[{r['full_name']}]({r['url']})"
                f.write("| " + " | ".join(cells) + " |\n")
        print(f"✅ Markdown 表格已写入 {md.resolve()}")

    if args.html or args.all:
        write_html(rows, out.with_suffix(".html"), args.max_desc)


if __name__ == "__main__":
    main()
