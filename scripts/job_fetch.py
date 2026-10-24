#!/usr/bin/env python3
"""四平台岗位抓取与投递匹配（电鸭 / LinkedIn / Upwork / WWR）。

2026-10-22 项目目标调整后的主管道（2026-10-23 用户要求恢复 LinkedIn，渠道由三个扩为四个）：
  * 全职：只找远程岗，只看技能匹配度；
  * 非全职（兼职 / 零活 / 项目制 / 合同）：不限地域，优先「技能范围内偏简单、单位时间产出高」的活。

取数方式（均实测）：
  电鸭    三类列表页——精选职位（categories/2xotV7）、简历智能匹配（jobs-channel）、
          社区招聘（categories/gk3tzg），每页 25 条、只有 200 字摘要；命中候选后用登录态
          headless Chrome 抓详情页，解析 __NEXT_DATA__ 拿全文（精选职位优先抓，不受摘要排序影响）。
          profile 默认 /tmp/elduck-profile；未登录时详情页跳验证码，自动退回 RSS 摘要（detail=rss_only）。
  WWR     三个 RSS：全部（remote-jobs.rss）+ programming + devops；正文、类型、地区全在 feed 里。
  Upwork  Jina Reader 抓公开搜索页（官方 RSS 已 410；直连 403 Cloudflare）
          → X-With-Links-Summary + X-Timeout 拿「描述块 + 职位链接」，按顺序配对
  LinkedIn 免登录 jobs-guest 端点（无账号 = 无封号风险）：列表 f_WT=2（官方 workplace-type=remote
          过滤）+ start=0/25 两页，地点桶 Remote / China；仅对标题命中目标角色的新岗拉
          jobPosting/{id} 详情（JD / 雇佣类型 / 级别 / 申请人数），上限 --li-details。
          列表页 location 是 LinkedIn 的招聘地点（不是公司总部），境外具体地点记地域受限。

产出：
  docs/jobs/jobs-<时间戳>.jsonl     全量标准化岗位（含 JD、评分、命中原因、信息充足度）
  docs/jobs/brief-<时间戳>.md       全职（全球 / 待确认 / 地域受限）与非全职推荐表
  docs/jobs/seen.json               已见岗位去重标记
  docs/jobs/li-detail.json          LinkedIn 详情缓存（ID → JD / 雇佣类型，跨 run 复用）

信息充足度（2026-10-23 立）：分数只能衡量「拿到的文本」里的匹配度——电鸭列表只有 200 字摘要、
LinkedIn 卡片没有正文、Upwork 搜索块可能很短，这些岗拿不到 JD 全文就被系统性低估（实测：
电鸭摘要级平均技能分 25.8 / 全文级 54.0，LinkedIn 卡片 32.4 / 全文 63.6）。所以按
`detail` 与 JD 字数（JD_LOW_CHARS）分两池：拿到正文的（rich）进分数可比的推荐表，
拿不到的写 `info_low=true` 在简报末尾单列「信息不足 · 待补全文」，不跟全文岗混排。

调权重时不必重抓：`--from-jsonl docs/jobs/jobs-xxx.jsonl` 直接离线重算。

用法：
  python3 scripts/job_fetch.py                       # 四平台默认查询
  python3 scripts/job_fetch.py --platforms wwr       # 只跑一个平台
  python3 scripts/job_fetch.py --details 0           # 不抓电鸭详情（快；默认 15 条）
  python3 scripts/job_fetch.py --li-details 0        # 不抓 LinkedIn 详情（快；默认 80 条）
  python3 scripts/job_fetch.py --queries "python automation,llm,quant"

边界：
  * Upwork 走第三方 Reader，公开页偶发 Cloudflare 403，脚本带重试；仍失败会记入 failures。
  * 电鸭详情依赖本机 Chrome + 已登录 profile；没有就只用 RSS 200 字摘要（会标注 detail=rss_only）。
  * LinkedIn guest 端点是匿名公开页，按 IP 级限流，脚本每次请求间隔 1 秒、失败按页跳过。
  * 只做个人求职用途，控制频率；不做批量转售 / 再分发。
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "jobs"
SEEN_FILE = OUT_DIR / "seen.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.86 Safari/537.36")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ELEDUCK_PROFILE = os.environ.get("ELEDUCK_PROFILE", "/tmp/elduck-profile")
ELEDUCK_RSS = "https://eleduck.com/feed/latest.xml"
WWR_RSS = "https://weworkremotely.com/remote-jobs.rss"
WWR_FEEDS = [
    (WWR_RSS, "全部"),
    ("https://weworkremotely.com/categories/remote-programming-jobs.rss", "programming"),
    ("https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss", "devops"),
]
UPWORK_SEARCH = "https://www.upwork.com/nx/search/jobs/"

# LinkedIn 免登录 guest 端点（2026-09-19 打通，2026-10-23 恢复进主管道）
LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LI_DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
LI_QUERIES = [
    "LLM", "AI Agent", "machine learning engineer", "quant developer",
    "python automation", "大模型",
]
LI_LOCS = ("Remote", "China")  # 两个地点桶：全球远程 + 国内远程
LI_STARTS = (0, 25)             # guest 每页约 10 条（登录版 25），翻两页
LI_MAX_DETAIL = 80              # 详情上限：每岗一次请求，只拉角色命中的岗（有缓存，重跑不重拉）

DEFAULT_QUERIES = [
    # 全职远程方向（Upwork 上也会出现，但主要是非全职）
    "ai engineer", "llm engineer", "machine learning engineer", "quant developer",
    "python developer", "data engineer",
    # 非全职：技能范围内偏简单的活
    "python automation", "web scraping", "data cleaning", "excel automation",
    "chatbot", "prompt engineering", "backtest", "api integration", "pandas",
]

# ---------------------------------------------------------------- 画像关键词
# 来源：docs/profile-facts.md（本人四份简历的事实清单，2026-10-22）
STRONG = [
    "python", "pandas", "numpy", "sql", "scikit-learn", "sklearn", "xgboost", "lightgbm",
    "pytorch", "tensorflow", "bert", "lstm", "machine learning", "deep learning",
    "time series", "anomaly detection", "fraud", "anti-fraud", "risk", "risk control",
    "quant", "quantitative", "backtest", "backtesting", "trading", "market data",
    "clickhouse", "kafka", "redis", "docker", "etl", "data pipeline", "data warehouse",
    "spark", "pyspark", "llm", "large language model", "agent", "multi-agent",
    "prompt engineering", "claude", "anthropic", "openai", "mcp", "evaluation",
    "structured output", "api integration", "automation", "flask", "typescript",
    "javascript", "shell", "mysql", "hive", "hbase", "git",
    "多智能体", "智能体", "编排", "工作流", "提示词", "评测", "工具调用", "微调",
    "大模型", "知识库", "向量检索", "数据清洗", "特征工程", "风控", "反欺诈",
    "量化", "回测", "机器学习", "深度学习", "时序预测", "异常检测", "算法",
    "数仓", "数据管道", "数据处理", "数据分析",
]
MEDIUM = [
    "langchain", "langgraph", "rag", "vector database", "embedding", "nlp", "aws", "gcp",
    "azure", "airflow", "dbt", "supabase", "postgres", "n8n", "web scraping",
    "playwright", "selenium", "excel", "vba", "streamlit", "fastapi", "django", "react",
    "vue", "node.js", "shopify", "meta ads", "recommendation", "forecasting",
    "视频", "多模型", "知识图谱", "推荐系统",
]
WEAK = [
    "java", "golang", "go ", "rust", "c++", "c#", ".net", "php", "ruby", "ios", "android",
    "swift", "kotlin", "react native", "flutter", "unity", "unreal", "3d", "computer vision",
    "opencv", "phd", "research scientist", "salesforce", "sap", "wordpress", "magento",
    "terraform", "ansible", "embedded", "firmware", "verilog", "fpga", "solidity",
    "smart contract", "site reliability", "sre", "on-call", "node.js", "nest.js", "next.js",
    "kubernetes", "infrastructure as code", "networking", "cloud architect",
]
ROLE_FIT = [
    "ai engineer", "llm engineer", "agent engineer", "machine learning engineer",
    "ml engineer", "python developer", "data engineer", "data scientist", "quant developer",
    "quantitative developer", "quant researcher", "risk modeler", "fraud", "automation engineer",
    "backend engineer", "software engineer", "ai developer", "applied scientist",
]

SIMPLE = [
    "simple", "small", "quick", "one-off", "one off", "short task", "few hours", "tiny",
    "mini", "bug", "fix", "data cleaning", "clean data", "excel", "spreadsheet", "csv",
    "scrape", "scraping", "automation", "automate", "report", "dashboard", "chatbot",
    "prompt", "api integration", "webhook", "zapier", "n8n", "pandas", "backtest",
    "simple script", "small script", "single script", "proof of concept", "poc",
    "简单", "小工具", "脚本", "一次性", "单次", "短期", "半天", "小活", "零活",
    "按项目", "接单", "外包", "excel", "表格", "爬虫", "数据清洗", "自动化", "看板",
]
COMPLEX = [
    "architect", "architecture", "platform", "enterprise", "senior", "lead engineer",
    "lead developer", "team lead", "tech lead", "staff engineer", "principal",
    "head of", "long-term", "long term", "ongoing", "3 to 6 months",
    "more than 6 months", "30+ hrs", "scale", "scalable", "white-label", "founding",
    "compliance", "security clearance", "phd", "research scientist", "computer vision",
    "ios", "android", "react native", "unity", "game engine", "kubernetes", "terraform",
    "架构", "平台", "系统", "团队", "负责人", "主导", "资深", "专家", "长期", "持续",
]

FULLTIME = [
    "全职", "full-time", "full time", "长期岗", "月薪", "年薪", "五险一金", "坐班",
    "到岗", "双休", "转正", "正式岗", "招聘", "岗位", "hire", "hiring",
]
PARTTIME = [
    "兼职", "part-time", "part time", "零活", "外包", "项目制", "接单", "一次性",
    "短期", "按项目", "按次", "半天", "小时", "freelance", "contract", "task",
    "gig", "side project", "小活",
]

ON_SITE = ["坐班", "到岗", "现场", "驻场", "办公室", "北京", "上海", "深圳", "广州", "杭州"]


# ---------------------------------------------------------------- 工具

def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read()


def clean_html(raw: str) -> str:
    raw = re.sub(r"<(script|style|noscript|svg)\b[^>]*>.*?</\1\s*>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"</(?:p|div|li|tr|h[1-6]|br)\s*>|<br\s*/?>", "\n", raw, flags=re.I)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = text.replace("\u00a0", " ")
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clip(text: str, n: int = 4000) -> str:
    text = " ".join((text or "").split())
    return text[:n]


def parse_dt(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def hits(text: str, words: list[str]) -> list[str]:
    low = (text or "").lower()
    return [w for w in words if w in low]


# ---------------------------------------------------------------- 电鸭

ELEDUCK_FEEDS = [
    ("categories/2xotV7", "精选职位"),
    ("jobs-channel", "简历智能匹配"),
    ("categories/gk3tzg", "社区招聘"),
]


def eleDuck_dom(url: str, timeout: int = 50) -> str | None:
    """登录态 headless Chrome 取 DOM（电鸭页面会跳验证码，curl 拿不到列表）。"""
    if not Path(CHROME).exists():
        return None
    cmd = [
        CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", f"--user-data-dir={ELEDUCK_PROFILE}",
        f"--user-agent={UA}", "--virtual-time-budget=15000", "--dump-dom", url,
    ]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).stdout.decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError):
        return None


def eleDuck_page(path: str, page: int = 1, label: str = "") -> list[dict]:
    url = f"https://eleduck.com/{path}" + (f"?page={page}" if page > 1 else "")
    raw = eleDuck_dom(url)
    if not raw:
        return []
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', raw, re.S)
    if not m:
        return []
    try:
        pp = json.loads(m.group(1))["props"]["initialProps"]["pageProps"]
        posts = pp["postList"]["posts"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return []
    out = []
    for p in posts:
        pid = p.get("id")
        title = (p.get("title") or "").strip()
        if not pid or not title:
            continue
        out.append({
            "platform": "电鸭",
            "id": pid,
            "title": title,
            "url": f"https://eleduck.com/posts/{pid}",
            "posted": p.get("published_at") or "",
            "jd": p.get("summary") or "",
            "feed": label,
            "closed": bool(p.get("closed")),
            "deleted": bool(p.get("deleted")),
            "hidden": bool(p.get("hide")),
            "category": (p.get("category") or {}).get("name") if isinstance(p.get("category"), dict) else "",
            "detail": "list",
        })
    return out


def fetch_eleduck(pages: int = 1, details: int = 6, days: int = 45) -> list[dict]:
    """三类列表页（精选 / 简历智能匹配 / 社区招聘）→ 预筛 → 详情补全。

    列表页每条只有 200 字摘要，详情页才有完整 JD；按摘要的初步技能分排序后只抓前 N 条详情，
    避免为几十条不相关帖子各起一次 Chrome。
    """
    items: dict[str, dict] = {}
    for path, label in ELEDUCK_FEEDS:
        for page in range(1, pages + 1):
            got = eleDuck_page(path, page, label)
            if not got:
                continue
            for j in got:
                items.setdefault(j["id"], j)
    if not items:  # Chrome / profile 不可用 → 退回 RSS（只有最新 25 条、200 字摘要）
        raw = http_get(ELEDUCK_RSS).decode("utf-8", "replace")
        for it in ET.fromstring(raw).findall(".//item"):
            link = (it.findtext("link") or "").strip()
            title = (it.findtext("title") or "").strip()
            if not link or not title:
                continue
            items[link.rstrip("/").rsplit("/", 1)[-1]] = {
                "platform": "电鸭",
                "id": link.rstrip("/").rsplit("/", 1)[-1],
                "title": title,
                "url": link,
                "posted": it.findtext("pubDate") or "",
                "jd": clean_html(it.findtext("description") or ""),
                "feed": "RSS",
                "closed": False, "deleted": False, "hidden": False,
                "category": "",
                "detail": "rss_only",
            }

    now = datetime.now().astimezone()
    candidates = []
    for j in items.values():
        if j.get("closed") or j.get("deleted") or j.get("hidden"):
            continue
        posted = parse_dt(j.get("posted") or "")
        if posted and days > 0:
            try:
                age = (now - posted).days
            except TypeError:
                age = 0
            if age > days:
                continue
        if not job_like(j["title"], j["jd"]):
            continue
        candidates.append(j)

    # 摘要只是 200 字：先按「标题 + 摘要」粗排，详情只抓最可能相关的几条
    candidates.sort(key=lambda j: -skill_score(j)[0])
    if details > 0:
        curated = [j for j in candidates if j.get("feed") == "精选职位"]
        others = [j for j in candidates if j.get("feed") != "精选职位"]
        to_detail = curated + others[: max(0, details - len(curated))]
        for j in to_detail:
            detail = eleDuck_detail(j["id"])
            if detail:
                j.update(detail)
            time.sleep(0.5)
    return candidates


def eleDuck_detail(post_id: str, timeout: int = 50) -> dict | None:
    """登录态 headless Chrome 抓详情，解析 __NEXT_DATA__。失败返回 None。"""
    if not Path(CHROME).exists():
        return None
    cmd = [
        CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", f"--user-data-dir={ELEDUCK_PROFILE}",
        f"--user-agent={UA}", "--virtual-time-budget=15000", "--dump-dom",
        f"https://eleduck.com/posts/{post_id}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        raw = proc.stdout.decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        post = data["props"]["initialProps"]["pageProps"]["post"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    content = post.get("raw_content") or clean_html(post.get("content") or "")
    if not content:
        return None
    return {
        "jd": content,
        "closed": bool(post.get("closed")),
        "hidden": bool(post.get("hide")) or bool(post.get("hided_at")),
        "deleted": bool(post.get("deleted")),
        "status": post.get("status") or "",
        "category": (post.get("category") or {}).get("name") or "",
        "tags": [t.get("name") if isinstance(t, dict) else str(t) for t in (post.get("tags") or [])],
        "comments": post.get("comments_count") or 0,
        "views": post.get("views_count") or 0,
        "contact": post.get("contact_info"),
        "detail": "full",
    }


def job_like(title: str, jd: str) -> bool:
    """电鸭 latest 是全站动态：先排掉自荐、日记、求助、工具广告，再要求出现岗位信号。"""
    text = f"{title} {jd}"
    negative_title = [
        "承接", "可承接", "代搭", "提供服务", "服务范围", "业务场景", "日记", "复盘",
        "分享", "教程", "思考", "心得", "如何", "经验", "聊聊", "踩坑", "我做了",
        "想转", "求职", "找搭子", "组队", "免费出人力", "接单｜", "接单|", "求职",
    ]
    if any(n in title for n in negative_title):
        return False
    signals = ["招聘", "招人", "诚招", "岗位", "jd", "职责", "要求", "预算", "薪资",
               "月薪", "元/", "k/", "k-", "全职", "兼职", "外包", "项目制", "零活",
               "找人", "需求", "短期"]
    return sum(1 for s in signals if s.lower() in text.lower()) >= 2


# ---------------------------------------------------------------- WWR

def wwr_items() -> list[dict]:
    out: dict[str, dict] = {}
    for url, label in WWR_FEEDS:
        try:
            raw = http_get(url, timeout=40).decode("utf-8", "replace")
            root = ET.fromstring(raw)
        except Exception:  # 单个 feed 失败不影响其它
            continue
        for it in root.findall(".//item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            if not title or not link:
                continue
            typ = (it.findtext("type") or "").strip()
            out.setdefault(link, {
                "platform": "WWR",
                "id": link.rstrip("/").rsplit("/", 1)[-1],
                "title": title,
                "company": title.split(":", 1)[0].strip() if ":" in title else "",
                "url": link,
                "posted": it.findtext("pubDate") or "",
                "expires": it.findtext("expires_at") or "",
                "employment": "full-time" if "full" in typ.lower() else ("part-time" if typ else "unknown"),
                "remote": True,
                "location": it.findtext("region") or "",
                "jd": clean_html(it.findtext("description") or ""),
                "tags": [x for x in (it.findtext("category") or "").split(",") if x],
                "feed": label,
                "detail": "feed",
            })
    return list(out.values())


# ---------------------------------------------------------------- Upwork

def jina(url: str, timeout_s: int = 20, tries: int = 3) -> str:
    last = ""
    for attempt in range(tries):
        req = urllib.request.Request(
            "https://r.jina.ai/" + url,
            headers={
                "User-Agent": UA,
                "X-With-Links-Summary": "true",
                "X-Timeout": str(timeout_s),
            },
        )
        try:
            body = urllib.request.urlopen(req, timeout=90).read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(5 * (attempt + 1))
            continue
        if "error 403" in body.lower() or "cloudflare" in body.lower():
            last = "403/cloudflare"
            time.sleep(6 * (attempt + 1))
            continue
        return body
    raise RuntimeError(f"jina 失败：{last}")


def parse_upwork_page(md: str) -> list[dict]:
    links = re.findall(r"^- \[([^\]]+)\]\((https://www\.upwork\.com/jobs/[^)]+)\)", md, re.M)
    links = [(html_lib.unescape(t).strip(), u.split("?")[0]) for t, u in links]
    body = md.split("Links/Buttons:")[0]
    blocks = re.split(r"(?=^\*   \*\*(?:Hourly|Fixed price))", body, flags=re.M)[1:]
    jobs = []
    for i, block in enumerate(blocks):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        meta = " ".join(lines[:3]).replace("*", "")
        desc = "\n".join(ln for ln in lines[3:] if not re.fullmatch(r"\+\d+", ln))
        hourly = re.search(r"Hourly:\s*\$([\d,.]+)\s*-\s*\$([\d,.]+)", meta)
        fixed = "Fixed price" in meta
        level = next((x for x in ("Expert", "Intermediate", "Entry Level") if x in meta), "")
        est = re.search(r"Est\. time:\s*(.+)$", meta)
        budget = re.search(r"Est\. budget:\s*\$([\d,.]+)", meta)
        title, url = links[i] if i < len(links) else ("", "")
        if not url:
            continue
        jobs.append({
            "platform": "Upwork",
            "id": url.rsplit("_~", 1)[-1] if "_~" in url else url.rsplit("/", 2)[-2],
            "title": title,
            "url": url,
            "employment": "part-time",
            "remote": True,
            "location": "Remote",
            "jd": desc,
            "budget": (
                f"${budget.group(1)}" if budget else
                (f"${hourly.group(1)}-${hourly.group(2)}/h" if hourly else "")
            ),
            "budget_value": float((budget or hourly).group(1).replace(",", "")) if (budget or hourly) else 0.0,
            "budget_kind": "fixed" if fixed else ("hourly" if hourly else ""),
            "level": level,
            "est_time": est.group(1) if est else "",
            "detail": "search",
        })
    return jobs


def upwork_items(queries: list[str], pages: int = 1) -> tuple[list[dict], list[str]]:
    jobs, failures = [], []
    for q in queries:
        for page in range(1, pages + 1):
            url = f"{UPWORK_SEARCH}?q={urllib.parse.quote(q)}"
            if page > 1:
                url += f"&page={page}"
            try:
                md = jina(url)
            except RuntimeError as exc:
                failures.append(f"Upwork [{q} p{page}]: {exc}")
                continue
            parsed = parse_upwork_page(md)
            if not parsed:
                failures.append(f"Upwork [{q} p{page}]: 0 条（页面结构或渲染时机变化）")
            for j in parsed:
                j["source_query"] = q
            jobs += parsed
            time.sleep(2)
    return jobs, failures


# ---------------------------------------------------------------- LinkedIn

LI_EMPLOYMENT = {
    "full-time": "full-time", "part-time": "part-time",
    "contract": "part-time", "temporary": "part-time",
    "internship": "unknown", "volunteer": "unknown", "other": "unknown",
}
LI_DETAIL_HINTS = [w.lower() for w in (
    "ai engineer", "llm", "agent", "machine learning", "ml engineer", "python",
    "data engineer", "data scientist", "quant", "automation", "backend", "full stack",
    "算法", "大模型", "智能体", "量化", "自动化", "数据",
)]
LI_HOME_MARKS = ("china", "hong kong", "taiwan", "macau")
LI_CACHE = OUT_DIR / "li-detail.json"  # 详情缓存（ID → JD / 雇佣类型），跨 run 复用省请求


def load_li_cache() -> dict:
    if LI_CACHE.exists():
        try:
            return json.loads(LI_CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001  缓存坏了不影响抓取，重新攒
            return {}
    return {}


def save_li_cache(cache: dict) -> None:
    if len(cache) > 3000:  # 只留最近 3000 条，防无限增长
        for k in list(cache)[:-3000]:
            cache.pop(k, None)
    LI_CACHE.parent.mkdir(parents=True, exist_ok=True)
    LI_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def li_priority(job: dict) -> tuple:
    """详情抓取优先级：境内 / 近岸岗 → 全球远程岗 → 境外具体地点；同级按标题强词命中数降序。
    详情每岗一次请求且有上限，先把最值得看的拿到 JD。"""
    loc = (job.get("location") or "").lower()
    if any(k in loc for k in LI_HOME_MARKS):
        geo = 0
    elif any(k in loc for k in ("remote", "anywhere", "worldwide")):
        geo = 1
    else:
        geo = 2
    strong = len(hits(job["title"].lower(), [w.lower() for w in STRONG]))
    return (geo, -strong)


def li_get(url: str, timeout: int = 30, retries: int = 1) -> str:
    """guest 接口免登录但按 IP 限流：请求间隔 1 秒，失败退避 4 秒重试一次。
    重试后仍失败就抛出去，由调用方记 failures（列表页跳过、详情页由连续失败断路器提前收尾）。
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        time.sleep(1.0 if attempt == 0 else 4.0)
        try:
            return http_get(url, timeout=timeout).decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last  # type: ignore[misc]


def li_clean(raw: str) -> str:
    """去标签 + 反复反转义（LinkedIn 偶发双重编码 &amp;amp;，解到稳定为止）。"""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw or ""))
    for _ in range(3):
        prev, text = text, html_lib.unescape(text)
        if text == prev:
            break
    return text.strip()


def linkedin_items(max_detail: int = LI_MAX_DETAIL,
                   queries: list[str] | None = None,
                   locs: tuple[str, ...] = LI_LOCS) -> tuple[list[dict], list[str]]:
    """LinkedIn 岗位搜索（免登录 guest 端点）。返回 (岗位列表, 失败清单)。

    列表：f_WT=2 是 LinkedIn 官方 workplace-type=remote 过滤，返回岗位全部为远程工作类型；
    地点桶 Remote 拿全球远程岗、China 拿国内远程岗（实测均有结果）。
    详情：只对角色命中（卡片级预筛后）的岗拉，上限 max_detail（只算本次真正发出的新请求；
    命中本地详情缓存的不重拉，缓存跨 run 留存——否则「先列表、后详情」的两阶段回填
    会被 seen.json 永久卡住）。详情解析 JD 正文 + 雇佣类型 / 级别 / 行业 / 申请人数，
    雇佣类型写回 job["employment"]，供全职 / 非全职两套打分口径分流。
    限流：单页失败跳过并计入 failures；全部页都失败才抛异常（通道级失败）。
    无账号参与 = 无封号风险。
    """
    kw_list = queries or LI_QUERIES
    cards: dict[str, dict] = {}
    failures: list[str] = []
    failed = 0
    for kw in kw_list:
        for loc in locs:
            for start in LI_STARTS:
                url = (f"{LI_SEARCH}?keywords={urllib.parse.quote(kw)}"
                       f"&location={urllib.parse.quote(loc)}&f_WT=2&start={start}")
                try:
                    page = li_get(url)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    failures.append(f"LinkedIn [{kw} / {loc} p{start}]: {type(exc).__name__}")
                    continue
                for seg in page.split("<li>")[1:]:
                    if "base-search-card" not in seg:
                        continue
                    urn = re.search(r'data-entity-urn="urn:li:jobPosting:(\d+)"', seg)
                    title = re.search(r'base-search-card__title">(.*?)</h3>', seg, re.S)
                    if not (urn and title):
                        continue
                    jid = urn.group(1)
                    if jid in cards:
                        continue
                    company = re.search(r'base-search-card__subtitle">(.*?)</h4>', seg, re.S)
                    city = re.search(r'job-search-card__location">(.*?)</span>', seg, re.S)
                    posted = re.search(r'job-search-card__listdate" datetime="([^"]+)"', seg)
                    cards[jid] = {
                        "platform": "LinkedIn",
                        "id": jid,
                        "title": li_clean(title.group(1)),
                        "company": li_clean(company.group(1)) if company else "",
                        "url": f"https://www.linkedin.com/jobs/view/{jid}",
                        "posted": posted.group(1) if posted else "",
                        "expires": "",
                        "employment": "unknown",
                        "remote": True,
                        "location": li_clean(city.group(1)) if city else "",
                        "jd": "",
                        "tags": [],
                        "detail": "card",
                        "source_query": kw,
                    }
    if cards and failed >= len(kw_list) * len(locs) * len(LI_STARTS):
        raise RuntimeError(f"guest 接口全部页失败（{failed} 页，可能被限流或页面改版）")

    # 卡片级预筛：角色不对的直接丢掉（同 WWR 按类目预筛的思路）——几百张非目标岗卡片
    # 会把「类型待确认」区洗成噪声，而它们本来也进不了推荐位。
    kept = []
    for job in cards.values():
        if classify_role(job) == "other":
            continue
        kept.append(job)
    if len(kept) < len(cards):
        print(f"[info] LinkedIn 卡片级预筛：按角色排掉 {len(cards) - len(kept)} 条",
              file=sys.stderr)
    kept.sort(key=li_priority)

    cache = load_li_cache()
    fetched = cached = 0
    fail_streak = 0
    for job in kept:
        jid = job["id"]
        if jid in cache:
            job.update(cache[jid])
            cached += 1
            continue
        if fetched >= max_detail:
            continue
        try:
            detail_html = li_get(LI_DETAIL.format(id=jid))
            fail_streak = 0
        except Exception:  # noqa: BLE001
            failures.append(f"LinkedIn 详情 [{job['title'][:40]}]: 请求失败")
            fail_streak += 1
            # 连续失败断路器：限流后继续请求只会白白消耗时间，缓存已保留、下次续补。
            if fail_streak >= 4:
                failures.append(f"LinkedIn: 连续 {fail_streak} 次详情请求失败，疑似限流——本轮提前停止抓详情"
                                "（已拉到的写进 li-detail.json，下次 run 续补）")
                break
            time.sleep(3)
            continue
        dm = re.search(r"show-more-less-html__markup[^>]*>(.*?)</div>", detail_html, re.S)
        criteria = {
            li_clean(k).lower(): li_clean(v)
            for k, v in re.findall(
                r'job-criteria-subheader">(.*?)</h3>\s*<span[^>]*>(.*?)</span>',
                detail_html, re.S)
        }
        jd = li_clean(dm.group(1)) if dm else ""
        meta = [f"{k}: {v}" for k, v in criteria.items() if v]
        pm = re.search(r"posted-time-ago__text[^>]*>(.*?)</", detail_html, re.S)
        am = re.search(r"num-applicants__caption[^>]*>(.*?)</", detail_html, re.S)
        if pm:
            meta.append("发布：" + li_clean(pm.group(1)))
        if am:
            meta.append(li_clean(am.group(1)))
        job["jd"] = (jd + "\n" + " | ".join(meta))[:4000]
        job["tags"] = [v for k, v in criteria.items() if k in ("industries", "seniority level")]
        job["employment"] = LI_EMPLOYMENT.get(criteria.get("employment type", "").lower(), "unknown")
        job["detail"] = "full" if jd else "card"
        if jd:  # 只缓存真正拿到 JD 的，空结果下次重试
            cache[jid] = {k: job[k] for k in ("jd", "employment", "tags", "detail")}
        fetched += 1
    if fetched or cached:
        save_li_cache(cache)
    if fetched == 0 and cached == 0:
        failures.append("LinkedIn: 本次未拉到任何详情（列表卡片仍可用，JD 为空会拉低匹配分）")
    return kept, failures


# ---------------------------------------------------------------- 分类 / 打分

ROLE_PATTERNS = {
    "ai": ["ai engineer", "ai developer", "llm", "agent", "genai", "generative ai",
           "prompt", "ai application", "applied ai", "large language model",
           "artificial intelligence", "ai/ml", "ai specialist", "ai platform",
           "人工智能", "大模型", "智能体"],
    "ml": ["machine learning", "ml engineer", "data scientist", "applied scientist",
           "research engineer", "deep learning", "algorithm engineer", "algorithms engineer",
           "机器学习", "算法工程师", "算法"],
    "data": ["data engineer", "analytics engineer", "data analyst", "bi ", "etl",
             "data warehouse", "data platform", "数据工程", "数据分析", "数据开发"],
    "quant": ["quant", "quantitative", "trading", "backtest", "market data", "portfolio",
              "量化", "回测", "交易"],
    "risk": ["risk", "fraud", "anti-fraud", "credit model", "model risk", "风控", "反欺诈"],
    "backend": ["backend", "back-end", "software engineer", "python developer",
                "full stack", "full-stack", "platform engineer", "api",
                "后端", "全栈", "服务端", "开发工程师"],
    "automation": ["automation", "rpa", "n8n", "workflow", "integration", "自动化"],
    "pm": ["product manager", "product owner", "产品经理"],
}
NON_TECH_TITLE = [
    "sales", "account executive", "customer success", "customer support", "marketing",
    "designer", "writer", "recruiter", "talent", "finance", "accounting",
    "hr ", "people ops", "legal", "admin", "community manager", "social media", "seo",
    "content", "copywriter", "editor", "virtual assistant", "bookkeeper", "cold call",
    "security", "support", "growth", "operations manager", "operations associate",
    "运营", "设计", "剪辑", "助理", "客服", "销售", "市场", "人事", "行政", "财务",
    "法务", "内容", "编辑", "写作", "翻译", "增长", "推广", "投放", "营销", "代申请",
    "detection & response", "detection and response", "threat detection",
    "threat intelligence", "incident response", "customer engineer", "site engineer",
    "network engineer", "elearning", "e-learning", "manager", "teach", "trainer",
    "training specialist", "admin tasks",
]
RESTRICTED_REGION = ["usa", "u.s.", "united states", "us only", "us-only", "canada",
                     "europe", "emea", "uk only", "united kingdom", "australia", "latam",
                     "north america", "south america", "americas", "apac"]
RESTRICTED_JD = [
    "must be based in the us", "must reside in the us", "us residents only",
    "must be authorized to work in the us", "us-only", "us only", "canada only",
    "eu only", "uk only", "australia only", "must be based in canada",
    "must be located in the united states",
]


def non_tech_hit(title_low: str) -> list[str]:
    """标题里的非技术词命中。ASCII 词按词边界匹配——否则短词会误伤（“seo” 会命中 “Seoul”，
    把 AI 工程师岗当成市场岗排掉）；中文词没有词边界可用，仍按子串匹配。"""
    out = []
    for w in NON_TECH_TITLE:
        w = w.strip()
        if not w:
            continue
        if w.isascii():
            if re.search(rf"\b{re.escape(w)}\b", title_low):
                out.append(w)
        elif w in title_low:
            out.append(w)
    return out


def classify_role(job: dict) -> str:
    title = job["title"].lower()
    text = f"{title} {job.get('jd','')[:2500]}".lower()
    if non_tech_hit(title):
        return "other"
    if "product manager" in title or "product owner" in title:
        return "pm" if any(k in title for k in ("ai", "agent", "llm", "technical")) else "other"
    for role, kws in ROLE_PATTERNS.items():
        if any(k in title for k in kws):
            return role
    # 标题只有泛称（engineer / developer / analyst）时，用 JD 前段判断
    if any(k in title for k in ("engineer", "developer", "scientist", "analyst")):
        for role, kws in ROLE_PATTERNS.items():
            if any(k in text for k in kws):
                return role
    return "other"


HQ_RE = re.compile(r"headquarters:\s*([^\n]{0,100})", re.I)
TZ_RESTRICT = re.compile(
    r"(unable to consider applications from candidates in other time zones"
    r"|time zone[^.\n]{0,50}(?:cet|est|pst|gmt)"
    r"|must be (?:based|located) in (?:the )?(?:us|usa|united states|canada|uk|europe)"
    r"|us[- ]only|us residents only|must be authorized to work in the us)",
    re.I,
)
COUNTRY_RE = re.compile(
    r"\b(united states|usa|u\.s\.|us|canada|india|mexico|mx|uk|united kingdom|sweden|belgium|"
    r"germany|france|spain|netherlands|europe|americas|el salvador|singapore|australia|"
    r"japan|korea|brazil|latam|argentina|colombia)\b",
    re.I,
)


def region_status(job: dict) -> tuple[str, str]:
    """返回 (global|restricted|unknown, 说明)。

    只看两处证据：JD 里的 Headquarters 行 / 明确的身份与时区限制，以及 feed 的 region 字段。
    公司总部在某个国家、但没写「Remote - 国家」的，记为 unknown（工作许可待确认），不直接判死。
    """
    region = (job.get("location") or "").strip()
    jd = job.get("jd") or ""
    low = jd.lower()

    m = TZ_RESTRICT.search(low)
    if m:
        return "restricted", "时区/身份限制：" + " ".join(m.group(1).split())[:70]

    hq = HQ_RE.search(jd)
    hq_text = " ".join(hq.group(1).split()) if hq else ""
    looks_like_place = bool(hq_text) and (
        bool(COUNTRY_RE.search(hq_text)) or "," in hq_text or "remote" in hq_text.lower()
    )
    if looks_like_place:
        h = hq_text.lower()
        country = COUNTRY_RE.search(h)
        if "remote" in h and country:
            return "restricted", "Headquarters: " + hq_text[:70]
        if "anywhere" in h or "worldwide" in h:
            return "global", hq_text[:70]
        if h.strip() == "remote":
            return "global", hq_text[:70]
        return "unknown", "公司总部 " + hq_text[:60] + "（工作许可待确认）"

    if job.get("platform") == "Upwork":
        return "global", "Upwork 远程（全球接单，JD 另有身份要求时以 JD 为准）"
    if job.get("platform") == "电鸭":
        return "global", "电鸭远程（国内 / 亚洲为主，投前确认办公地与结算）"
    if job.get("platform") == "LinkedIn":
        # 列表页 location 是 LinkedIn 的招聘地点（不是公司总部）：境内 / 近岸算可行，
        # 境外具体地点算地域受限（JD 里另有明确时区 / 身份限制时上面已优先判过）。
        loc = region.lower()
        if any(k in loc for k in ("china", "hong kong", "taiwan", "macau")):
            return "global", "LinkedIn 招聘地点：" + region + "（境内 / 近岸）"
        if any(k in loc for k in ("remote", "anywhere", "worldwide")):
            return "global", region
        if loc:
            return "restricted", "LinkedIn 招聘地点：" + region + "（需确认能否异地雇佣）"
        return "unknown", ""

    if region:
        r = region.lower()
        if any(k in r for k in ("anywhere", "worldwide", "global")):
            return "global", region
        if any(k in r for k in ("only", "usa", "united states", "canada", "europe",
                                "americas", "north america")):
            return "restricted", region
        return "unknown", region
    return "unknown", ""


def classify_employment(job: dict) -> str:
    if job["platform"] == "Upwork":
        return "part-time"
    if job["platform"] == "WWR":
        return job.get("employment") or "unknown"
    if job["platform"] == "LinkedIn" and job.get("employment") in ("full-time", "part-time"):
        return job["employment"]  # 详情页的 Employment type 字段最权威，拿到就用
    if job.get("closed") or job.get("hidden") or job.get("deleted"):
        return "closed"
    title = job["title"].lower()
    text = f"{title} {job.get('jd','')}".lower()
    title_ft = any(w.lower() in title for w in ("全职", "full-time", "full time"))
    title_pt = any(w.lower() in title for w in ("兼职", "part-time", "part time", "零活", "外包", "项目制"))
    if title_ft and not title_pt:
        return "full-time"
    if title_pt and not title_ft:
        return "part-time"
    ft = sum(1 for w in FULLTIME if w.lower() in text)
    pt = sum(1 for w in PARTTIME if w.lower() in text)
    if ft > pt:
        return "full-time"
    if pt > ft:
        return "part-time"
    return "unknown"


def remote_ok(job: dict) -> bool:
    if job["platform"] in ("WWR", "Upwork", "LinkedIn"):
        return True
    text = f"{job['title']} {job['jd']}"
    if "远程" in text or "remote" in text.lower():
        return True
    return not any(w in text for w in ON_SITE)


def skill_score(job: dict) -> tuple[int, list[str], list[str]]:
    text = f"{job['title']} {job['jd']}".lower()
    strong = hits(text, STRONG)
    medium = hits(text, MEDIUM)
    weak = hits(text, WEAK)
    role = hits(text, ROLE_FIT)
    score = 25 + 6 * min(len(strong), 8) + 3 * min(len(medium), 6) - 4 * min(len(weak), 6)
    if role:
        score += 5
    return max(0, min(100, score)), strong, medium + weak


def simplicity_score(job: dict) -> tuple[int, list[str], list[str]]:
    text = f"{job['title']} {job['jd']}".lower()
    simple = hits(text, [w.lower() for w in SIMPLE])
    complex_ = hits(text, [w.lower() for w in COMPLEX])
    score = 50 + 8 * min(len(simple), 6) - 6 * min(len(complex_), 8)
    if job.get("budget_kind") == "fixed" and job.get("budget_value"):
        b = job["budget_value"]
        if b <= 200:
            score += 10
        elif b <= 500:
            score += 5
        elif b >= 50000:
            score -= 35
        elif b >= 10000:
            score -= 20
        elif b >= 2000:
            score -= 10
    est = (job.get("est_time") or "").lower()
    if "less than 1 month" in est:
        score += 5
    if "3 to 6 months" in est or "more than 6 months" in est or "30+ hrs" in est:
        score -= 10
    return max(0, min(100, score)), simple, complex_


def efficiency_score(job: dict) -> int:
    kind = job.get("budget_kind")
    value = job.get("budget_value") or 0
    if kind == "fixed" and value:
        return int(max(10, min(100, 20 + 25 * math.log10(max(value, 1) / 10))))
    if kind == "hourly" and value:
        return int(max(10, min(100, 15 + 35 * math.log10(max(value, 1) / 5))))
    # 电鸭 / WWR：能从文本里捞到月薪就换算，否则中性
    text = f"{job['title']} {job['jd']}"
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~到]\s*(\d+(?:\.\d+)?)\s*(?:k|K|千)", text)
    if m:
        monthly = float(m.group(1)) * 1000
        return int(max(10, min(100, 15 + 35 * math.log10(max(monthly, 1) / 5000))))
    m = re.search(r"(\d{4,6})\s*元?\s*/\s*月", text)
    if m:
        monthly = float(m.group(1))
        return int(max(10, min(100, 15 + 35 * math.log10(max(monthly, 1) / 5000))))
    return 50


JD_LOW_CHARS = 400  # JD 正文少于这个字数视为「信息不足」：拿不到正文就评不出匹配度


def score_job(job: dict) -> dict:
    skill, strong, other = skill_score(job)
    simple, simple_hits, complex_hits = simplicity_score(job)
    eff = efficiency_score(job)
    employment = classify_employment(job)
    remote = remote_ok(job)
    role = classify_role(job)
    region, region_note = region_status(job)

    if employment == "full-time":
        final = skill
    elif employment == "part-time":
        final = round(0.55 * simple + 0.30 * skill + 0.15 * eff)
    else:
        final = round(0.5 * skill + 0.5 * simple)

    # 技能范围门槛：全职只看目标角色族；非全职至少要有一条可迁移的硬技能
    eligible = True
    if employment == "full-time":
        eligible = role in ("ai", "ml", "data", "quant", "risk", "backend", "automation", "pm")
    elif employment == "part-time":
        title = job["title"].lower()
        tech_title = ["python", "automation", "scrap", "data", "excel", "chatbot", "api",
                      "script", "pandas", "llm", " ai ", "agent", "backtest", "trading",
                      "sql", "dashboard", "power query", "自动化", "脚本", "爬虫",
                      "数据", "表格", "智能体", "大模型"]
        eligible = role != "other" or (skill >= 50 and any(t in title for t in tech_title))

    title_low = job["title"].lower()
    level_risk = any(k in title_low for k in (
        "principal", "staff engineer", "staff software", "architect", "head of",
        "director", "vp ", "chief ",
    ))
    if employment == "full-time" and level_risk:
        final = max(0, final - 15)

    reasons = []
    # 信息充足度：分数只能衡量「拿到的文本」里的匹配度，拿不到正文的岗不能拿去和全文岗比。
    # 电鸭列表只有 200 字摘要（detail=list）、LinkedIn 卡片没有正文（detail=card），
    # Upwork 搜索页描述块也可能很短（< JD_LOW_CHARS）——这些都记 info_low，简报里单列。
    jd_len = len((job.get("jd") or "").strip())
    if job.get("detail") in ("list", "card"):
        info_reason = "只有列表摘要 / 展示卡片，未拿到 JD 正文（可补）"
    elif jd_len < JD_LOW_CHARS:
        info_reason = f"JD 原文仅 {jd_len} 字（帖子本身就短，补不了）"
    else:
        info_reason = ""
    info_low = bool(info_reason)
    if info_low:
        reasons.append(f"信息不足：{info_reason}")
    if level_risk and employment == "full-time":
        reasons.append("级别偏高（Principal / Staff / Architect），需评估是否够得着")
    if strong:
        reasons.append("命中：" + "、".join(strong[:5]))
    if employment == "part-time" and simple_hits:
        reasons.append("简单信号：" + "、".join(simple_hits[:4]))
    if employment == "part-time" and complex_hits:
        reasons.append("复杂信号：" + "、".join(complex_hits[:3]))
    if job.get("budget"):
        reasons.append("预算 " + job["budget"])
    if region == "restricted":
        reasons.append("地域限制：" + region_note)
    elif region == "unknown" and region_note:
        reasons.append("地点待确认：" + region_note)
    return {
        **job,
        "employment": employment,
        "remote": remote,
        "role": role,
        "region": region,
        "region_note": region_note,
        "eligible": eligible,
        "skill": skill,
        "simple": simple,
        "efficiency": eff,
        "score": final,
        "reasons": reasons,
        "strong_hits": strong,
        "jd_len": jd_len,
        "info_low": info_low,
        "info_reason": info_reason,
    }


# ---------------------------------------------------------------- 主流程

def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")


def key_of(job: dict) -> str:
    return f"{job['platform']}:{job.get('id') or job.get('url')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="四平台岗位抓取与投递匹配（电鸭 / LinkedIn / Upwork / WWR）")
    parser.add_argument("--platforms", default="eleduck,linkedin,upwork,wwr",
                        help="逗号分隔：eleduck,linkedin,upwork,wwr")
    parser.add_argument("--queries", default="", help="Upwork 搜索词，逗号分隔（默认按画像内置）")
    parser.add_argument("--pages", type=int, default=1, help="Upwork 每个查询翻几页")
    parser.add_argument("--details", type=int, default=15, help="电鸭最多抓几条详情（0=只用列表摘要）")
    parser.add_argument("--eleduck-pages", type=int, default=1, help="电鸭每个列表页翻几页（精选/简历匹配/社区招聘）")
    parser.add_argument("--eleduck-days", type=int, default=45, help="电鸭只看最近 N 天（精选职位保留更久）")
    parser.add_argument("--li-details", type=int, default=LI_MAX_DETAIL, help="LinkedIn 最多抓几条详情（0=只用列表卡片）")
    parser.add_argument("--top", type=int, default=12, help="每类推荐条数")
    parser.add_argument("--no-write", action="store_true", help="只打印统计，不写文件")
    parser.add_argument("--from-jsonl", default="", help="跳过抓取，用已有 JSONL 重算（调打分权重时用）")
    args = parser.parse_args(argv)

    platforms = {p.strip() for p in args.platforms.split(",") if p.strip()}
    queries = [q.strip() for q in args.queries.split(",") if q.strip()] or DEFAULT_QUERIES
    failures: list[str] = []
    jobs: list[dict] = []

    if args.from_jsonl:
        src = Path(args.from_jsonl)
        jobs = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"[ok] 从 {src} 载入 {len(jobs)} 条（跳过抓取）", file=sys.stderr)
    elif "eleduck" in platforms:
        try:
            duck = fetch_eleduck(args.eleduck_pages, args.details, args.eleduck_days)
            print(f"[ok] 电鸭: {len(duck)} 条候选（列表 {args.eleduck_pages} 页 × 3 类，详情 {args.details} 条）",
                  file=sys.stderr)
            jobs += duck
        except Exception as exc:  # noqa: BLE001
            failures.append(f"电鸭: {type(exc).__name__} {exc}")

    if not args.from_jsonl and "wwr" in platforms:
        try:
            wwr = wwr_items()
            print(f"[ok] WWR: {len(wwr)} 条（全部 + programming + devops）", file=sys.stderr)
            jobs += wwr
        except Exception as exc:  # noqa: BLE001
            failures.append(f"WWR: {type(exc).__name__} {exc}")

    if not args.from_jsonl and "upwork" in platforms:
        up, up_fail = upwork_items(queries, args.pages)
        failures += up_fail
        print(f"[ok] Upwork: {len(up)} 条（失败 {len(up_fail)}）", file=sys.stderr)
        jobs += up

    if not args.from_jsonl and "linkedin" in platforms:
        try:
            li, li_fail = linkedin_items(args.li_details)
            failures += li_fail
            print(f"[ok] LinkedIn: {len(li)} 条候选（详情命中 {sum(1 for j in li if j['detail'] == 'full')} 条，"
                  f"失败 {len(li_fail)}）", file=sys.stderr)
            jobs += li
        except Exception as exc:  # noqa: BLE001
            failures.append(f"LinkedIn: {type(exc).__name__} {exc}")

    # 去重 + 过期过滤
    seen = load_seen()
    now = datetime.now(timezone.utc)
    dedup: dict[str, dict] = {}
    for job in jobs:
        k = key_of(job)
        if k in dedup:
            continue
        if job["platform"] == "WWR" and job.get("expires"):
            exp = parse_dt(job["expires"])
            if exp and exp < now:
                continue
        if job.get("closed") or job.get("hidden") or job.get("deleted"):
            continue
        job["is_new"] = k not in seen
        seen.setdefault(k, datetime.now().strftime("%Y-%m-%d"))
        dedup[k] = score_job(job)
    scored = list(dedup.values())

    # 分两池：拿到 JD 正文的（分数可比）+ 只有摘要 / 卡片的（信息不足，单列，不进推荐表）
    rich = [j for j in scored if not j["info_low"]]
    low_info = sorted((j for j in scored if j["info_low"]), key=lambda x: -x["jd_len"])

    fulltime = [j for j in rich if j["employment"] == "full-time"]
    nonremote = [j for j in fulltime if not j["remote"]]
    full = [j for j in fulltime if j["remote"] and j["eligible"] and j["region"] == "global"]
    full_restricted = [j for j in fulltime if j["remote"] and j["eligible"] and j["region"] == "restricted"]
    full_unknown = [j for j in fulltime if j["remote"] and j["eligible"] and j["region"] == "unknown"]
    full_other = [j for j in fulltime if j["remote"] and not j["eligible"]]
    part_all = [j for j in rich if j["employment"] == "part-time"]
    part = [j for j in part_all if j["eligible"]]
    part_other = [j for j in part_all if not j["eligible"]]
    unknown = [j for j in rich if j["employment"] not in ("full-time", "part-time")]
    unknown.sort(key=lambda x: -x["score"])
    for bucket in (full, full_restricted, full_unknown, part):
        bucket.sort(key=lambda x: -x["score"])

    ts = datetime.now().strftime("%Y%m%d-%H%M")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not args.no_write:
        with (OUT_DIR / f"jobs-{ts}.jsonl").open("w", encoding="utf-8") as fh:
            for j in scored:
                fh.write(json.dumps(j, ensure_ascii=False) + "\n")
        save_seen(seen)

    def row(j: dict) -> str:
        flags = "🆕 " if j.get("is_new") else ""
        budget = f" | {j['budget']}" if j.get("budget") else ""
        why = "；".join(j["reasons"])[:110]
        return (f"| {j['score']} | {flags}{j['platform']} | {j['role']} | {j['title'][:64]} | "
                f"{j.get('company') or j.get('location') or ''}{budget} | {why} | {j['url']} |")

    def low_row(j: dict) -> str:
        flags = "🆕 " if j.get("is_new") else ""
        return (f"| {j['jd_len']} 字 | {flags}{j['platform']} | {j['role']} | {j['title'][:56]} | "
                f"{j.get('company') or j.get('location') or ''} | {j['employment']} | "
                f"{j['info_reason']} | {j['url']} |")

    lines = [
        f"# 岗位匹配简报 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "> 口径（2026-10-22）：全职只看远程 + 技能匹配；非全职不限地域，优先技能范围内偏简单、单位时间产出高的活。",
        f"> 抓取：电鸭 {sum(1 for j in scored if j['platform']=='电鸭')} / "
        f"LinkedIn {sum(1 for j in scored if j['platform']=='LinkedIn')} / "
        f"Upwork {sum(1 for j in scored if j['platform']=='Upwork')} / "
        f"WWR {sum(1 for j in scored if j['platform']=='WWR')}；"
        f"失败 {len(failures)}。其中拿到 JD 正文 {len(rich)} 条、信息不足 {len(low_info)} 条（单列在末尾）。",
        "",
        f"## 全职 · 远程（{len(full)} 条）",
        "",
        "| 分 | 平台 | 角色 | 岗位 | 公司 / 地点 / 预算 | 命中 | 链接 |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [row(j) for j in full[: args.top]] or ["| — | — | — | 本次无 | — | — | — |"]
    lines += ["", f"## 非全职 · 不限地域（{len(part)} 条）", "",
              "| 分 | 平台 | 角色 | 岗位 | 地点 / 预算 | 命中 | 链接 |", "|---|---|---|---|---|---|---|"]
    lines += [row(j) for j in part[: args.top]] or ["| — | — | — | 本次无 | — | — | — |"]
    if unknown:
        lines += ["", f"## 类型待确认（{len(unknown)} 条，需看 JD 判断）", ""]
        lines += [row(j) for j in unknown[:8]]
    if full_unknown:
        lines += ["", f"## 全职 · 远程但工作许可待确认（{len(full_unknown)} 条）", ""]
        lines += [row(j) for j in full_unknown[:8]]
    if full_restricted:
        lines += ["", f"## 全职 · 远程但地域受限（{len(full_restricted)} 条，投前先确认工作许可）", ""]
        lines += [row(j) for j in full_restricted[:8]]
    if nonremote:
        lines += ["", f"## 全职但非远程（{len(nonremote)} 条，按新目标排除）", ""]
        lines += [f"- {j['title']}（{j.get('location') or '地点未知'}）{j['url']}" for j in nonremote[:10]]
    lines += ["", "## 已排除（角色 / 技能范围不匹配）", "",
              f"- 全职非目标角色：{len(full_other)} 条；非全职超范围：{len(part_other)} 条。"
              f"（完整清单见 JSONL 的 eligible=false）", ""]
    if low_info:
        lines += ["", f"## 信息不足 · 待补全文（{len(low_info)} 条，分数不可比、不入上面的推荐表）", "",
                  f"> 只拿到列表摘要 / 展示卡片（JD 正文 < {JD_LOW_CHARS} 字），不是「不匹配」而是「没看到」。"
                  "补全文：电鸭调高 `--details`（每条详情起一次 Chrome）、LinkedIn 调高 `--li-details`"
                  "（详情有缓存，重跑不重拉）；Upwork 搜索页描述块本身就不保证是全文。", "",
                  "| JD | 平台 | 角色 | 岗位 | 公司 / 地点 | 类型 | 情况 | 链接 |", "|---|---|---|---|---|---|---|---|"]
        lines += [low_row(j) for j in low_info[:12]]
        if len(low_info) > 12:
            lines += ["", f"- 另有 {len(low_info) - 12} 条同类，完整清单见 JSONL 的 `info_low=true`。"]
    if failures:
        # 同类失败聚合成一行（40 行逐条列会把简报尾巴洗成噪声），再给几条样例。
        groups: dict[str, list[str]] = {}
        for f in failures:
            groups.setdefault(f.split(" [")[0], []).append(f)
        lines += ["", "## 抓取失败 / 降级", ""]
        for head, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"- {head}：{len(items)} 次")
            if len(items) <= 2:
                lines += [f"  - {x}" for x in items]
            else:
                lines += [f"  - 例：{items[0]}", f"  - 例：{items[-1]}"]
    lines += ["", "> Upwork 标题与描述来自公开搜索页；电鸭 detail=rss_only 表示只有 200 字摘要，"
                  "投递前需登录看详情。LinkedIn 走免登录 guest 端点：地点栏是它的招聘地点"
                  "（境外具体地点已归入「地域受限」，投前确认工作许可 / 时区）。", ""]

    brief = OUT_DIR / f"brief-{ts}.md"
    if not args.no_write:
        brief.write_text("\n".join(lines), encoding="utf-8")
        print(brief)
    else:
        print("\n".join(lines))
    print(f"全职 {len(full)}（待确认 {len(full_unknown)}、受限 {len(full_restricted)}）/ 非全职 {len(part)} / 待定 {len(unknown)} / "
          f"信息不足 {len(low_info)} / 非远程全职 {len(nonremote)} / 角色排除 {len(full_other) + len(part_other)} / 失败 {len(failures)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
