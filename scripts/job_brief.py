#!/usr/bin/env python3
"""每日岗位简报管道（Hopkins · ApplyOptimizerAgent）

四通道抓取 → 关键词/位置硬筛 → fit_score 预打分 → 产出 docs/briefs/ 简报。
预筛分只是机器预判（口径见下），是否投递由人终审，选中后按 bidding-record-sop 记台账。

通道与接口（2026-09-08 实测验证）：
  腾讯    careers.tencent.com/tencentcareer/api/post/Query   免登录 JSON API
  字节    jobs.bytedance.com/api/v1/search/job/posts          免登录 JSON API（POST）
  DeepSeek app.mokahr.com(Moka ATS) 首页握手 + jobs/v2 接口   返回 AES-128-CBC 加密，
          key=响应里 necromancer 字段，iv=页面 init-data 的 aesIv，用 openssl CLI 解密
  电鸭    eleduck.com/feed/latest.xml RSS                      免登录

fit_score 预打分口径（0-100，粗筛用，权重待 T9 用真实转化数据校准）：
  标题命中强关键词 +45（封顶）、命中中关键词 +15（封顶）
  JD 正文命中强关键词 +20 / 中关键词 +10（合计封顶 30）
  位置：广州 +10 / 深圳 +8 / 远程 +10；坐班其他城市 0 分但可通过位置关
  位置关（硬筛）：坐班岗要求广州/深圳/远程之一，不通过者单独归档不进推荐

用法：python3 scripts/job_brief.py   （全部通道一次跑完，单通道失败不影响其余）
产物：docs/briefs/brief-<时间戳>.md + docs/briefs/seen.json（已见岗位去重标记）
"""

import base64
import html as html_lib
import json
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
BRIEF_DIR = PROJECT / "docs" / "briefs"
SEEN_FILE = BRIEF_DIR / "seen.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 用户三条简历方向：AI应用Agent开发 / AI产品经理Agent方向 / 量化开发研究
STRONG_KW = ["agent", "llm", "大模型", "aigc", "智能体", "ai 应用", "ai应用", "量化", "ai平台"]
MID_KW = ["算法", "后端", "服务端", "数据", "平台", "基础设施", "产品经理", "python", "java", "go"]

CITY_GATE = ["广州", "深圳", "远程"]  # 坐班岗位置关；电鸭天然全远程


def http_get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read()


def polite():
    time.sleep(1.0)  # 低频礼貌抓取，全通道每日两次的量级远低于人工浏览


def http_json(req_or_url, tries=3, timeout=20, headers=None):
    """GET/POST → JSON 解析，带重试（平台偶发限流返回 HTML/空响应）。"""
    last = None
    for i in range(tries):
        try:
            if isinstance(req_or_url, str):
                req_or_url = urllib.request.Request(req_or_url, headers={"User-Agent": UA, **(headers or {})})
            return json.loads(urllib.request.urlopen(req_or_url, timeout=timeout).read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2.0 * (i + 1))
    raise last


# ---------------------------------------------------------------- 腾讯

def fetch_tencent():
    """腾讯社招 Query API，按关键词搜索，返回岗位列表。"""
    jobs = []
    for kw in ["Agent", "LLM", "大模型", "量化"]:
        for page in (1,):
            ts = int(time.time() * 1000)
            url = ("https://careers.tencent.com/tencentcareer/api/post/Query"
                   f"?timestamp={ts}&keyword={urllib.request.quote(kw)}"
                   f"&pageIndex={page}&pageSize=100&language=zh-cn&area=cn")
            try:
                data = http_json(url)
            except Exception:
                continue
            posts = (data.get("Data") or {}).get("Posts") or []
            for p in posts:
                # 只留技术/产品/数据类，滤掉市场、职能等噪声
                if not any(c in (p.get("CategoryName") or "") for c in ("技术", "产品", "数据")):
                    continue
                jobs.append({
                    "company": "腾讯",
                    "title": p.get("RecruitPostName", ""),
                    "city": p.get("LocationName", "") or p.get("CountryName", ""),
                    "dept": p.get("BGName", ""),
                    "category": p.get("CategoryName", ""),
                    "jd": (p.get("Responsibility") or "") + "\n" + (p.get("Requirement") or ""),
                    "url": f"https://careers.tencent.com/jobdesc.html?postId={p.get('PostId')}",
                    "updated": p.get("LastUpdateTime", ""),
                    "kw": kw,
                })
            polite()
    # PostId 去重
    seen, out = set(), []
    for j in jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            out.append(j)
    return out


# ---------------------------------------------------------------- 字节

BD_BODY = {
    "keyword": "", "limit": 20, "offset": 0,
    "job_category_id_list": [], "tag_id_list": [], "location_code_list": [],
    "subject_id_list": [], "recruitment_id_list": [],
    "portal_type": 2, "job_function_id_list": [], "portal_entrance": 1,
}


def fetch_bytedance():
    jobs = []
    for kw in ["Agent", "LLM", "大模型", "量化"]:
        body = dict(BD_BODY, keyword=kw, limit=20)
        data = http_json(urllib.request.Request(  # 失败直接抛，不静默吞
            "https://jobs.bytedance.com/api/v1/search/job/posts",
            data=json.dumps(body).encode(),
            headers={"User-Agent": UA, "Content-Type": "application/json",
                     "Referer": "https://jobs.bytedance.com/experienced/position?keywords="
                                + urllib.request.quote(kw),  # 头部值必须 latin-1，中文需转义
                     "Origin": "https://jobs.bytedance.com"}))
        for p in (data.get("data") or {}).get("job_post_list") or []:
            # 只留技术/产品类
            cat = (p.get("job_category") or {}).get("name", "") if isinstance(p.get("job_category"), dict) else ""
            if cat and not any(c in cat for c in ("技术", "产品", "研发", "算法")):
                continue
            cities = "、".join(c.get("name", "") for c in (p.get("city_list") or []))
            jobs.append({
                "company": "字节",
                "title": p.get("title", ""),
                "city": cities,
                "dept": p.get("job_subject", {}).get("name", "") if isinstance(p.get("job_subject"), dict) else str(p.get("job_subject") or ""),
                "category": cat,
                "jd": (p.get("description") or "") + "\n" + (p.get("requirement") or ""),
                "url": f"https://jobs.bytedance.com/experienced/position/{p.get('id')}/detail",
                "updated": p.get("publish_time", ""),
                "kw": kw,
            })
        polite()
    seen, out = set(), []
    for j in jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            out.append(j)
    return out


# ---------------------------------------------------------------- DeepSeek（Moka ATS）

DS_PORTAL = "https://app.mokahr.com/social-recruitment/high-flyer/140576"
DS_API = "https://app.mokahr.com/api/outer/ats-apply/website/jobs/v2"


def fetch_deepseek():
    import urllib.parse
    class Jar(urllib.request.HTTPRedirectHandler):
        pass
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    raw = opener.open(urllib.request.Request(DS_PORTAL, headers={"User-Agent": UA}), timeout=20).read().decode()
    m = re.search(r'<input id="init-data" type="hidden" value="(.*?)"/?>', raw, re.S)
    if not m:
        raise RuntimeError("init-data 未找到（Moka 页面结构可能变化）")
    init = json.loads(html_lib.unescape(m.group(1)))
    iv = init["aesIv"].encode()
    polite()

    jobs, seen_ids = [], set()

    def add_row(p):
        if p.get("id") in seen_ids:
            return
        seen_ids.add(p.get("id"))
        locs = p.get("locations") or []
        city = "、".join(sorted({l.get("cityName") or (l.get("address") or "")[:8] for l in locs}))
        jobs.append({
            "company": "DeepSeek",
            "title": p.get("title", ""),
            "city": city,
            "dept": (p.get("zhineng") or {}).get("name", "") if isinstance(p.get("zhineng"), dict) else "",
            "category": "",
            "jd": p.get("jobDescription") or "",
            "url": DS_PORTAL,
            "updated": p.get("updatedAt", ""),
            "kw": "",
        })

    for p in init.get("jobs") or []:  # SSR 首页内嵌的岗位（API 分页有上限，先收下）
        add_row(p)

    # API 忽略翻页参数（实测 pageNo 无效），改按关键词扩查扩大覆盖（实测并集约 31/37）
    for kw in ("", "算法", "开发", "数据", "工程", "产品", "研究"):
        body = json.dumps({"orgId": "high-flyer", "siteId": 140576, "pageNo": 1,
                           "pageSize": 30, "keyword": kw, "type": "social", "orderBy": 0}).encode()
        req = urllib.request.Request(
            DS_API, data=body,
            headers={"User-Agent": UA, "Content-Type": "application/json",
                     "Referer": DS_PORTAL, "Origin": "https://app.mokahr.com"})
        resp = json.loads(opener.open(req, timeout=20).read().decode())
        if "necromancer" not in resp:
            print(f"[warn] DeepSeek kw={kw!r} 响应异常: {str(resp)[:80]}", file=sys.stderr)
            continue
        dec = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d",
             "-K", resp["necromancer"].encode().hex(), "-iv", iv.hex()],
            input=base64.b64decode(resp["data"]), capture_output=True)
        try:
            out = json.loads(dec.stdout.decode())
        except Exception:
            print(f"[warn] DeepSeek kw={kw!r} 解密失败", file=sys.stderr)
            continue
        rows = (out.get("data") or {}).get("jobs") or []
        for p in rows:
            add_row(p)
        polite()
    return jobs


# ---------------------------------------------------------------- CKHR 群等无 API 渠道（收件箱）

def fetch_inbox():
    """CKHR 微信群等无 API 渠道：把群里的岗位消息粘贴进 docs/briefs/inbox.md，
    条目之间用一行 --- 分隔，每条首行写「标题」（建议带上公司名），其余行是 JD/消息原文。
    跑脚本时自动并入简报打分排序，处理完自动归档为 inbox-archive-<时间戳>.md。
    文件里的 <!-- 注释 --> 会被忽略，格式说明可放在注释里。"""
    if not INBOX_FILE.exists():
        return []
    raw = INBOX_FILE.read_text(encoding="utf-8")
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)  # 剥离说明注释
    blocks = [b.strip() for b in re.split(r"^---+\s*$", raw, flags=re.M) if b.strip()]
    jobs = []
    for b in blocks:
        lines = [ln for ln in b.splitlines() if ln.strip()]
        if not lines:
            continue
        title = lines[0].strip().lstrip("#").strip()
        body = "\n".join(lines[1:]).strip()
        city = "远程（CKHR）"
        for c in ("广州", "深圳", "北京", "杭州", "上海"):
            if c in b:
                city = c
                break
        jobs.append({
            "company": "CKHR群",
            "title": title,
            "city": city,
            "dept": "",
            "category": "",
            "jd": body or title,
            "url": "",
            "updated": "",
            "kw": "",
        })
    if jobs:  # 处理完归档，避免下轮重复
        archive = BRIEF_DIR / f"inbox-archive-{datetime.now().strftime('%Y%m%d-%H%M')}.md"
        INBOX_FILE.rename(archive)
    return jobs


# ---------------------------------------------------------------- 电鸭 RSS

def fetch_eleduck():
    xml = http_get("https://eleduck.com/feed/latest.xml", timeout=20).decode(errors="replace")
    root = ET.fromstring(re.sub(r'xmlns="[^"]+"', "", xml, count=1))
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", "", item.findtext("description") or "")
        items.append({"company": "电鸭", "title": title, "city": "远程", "dept": "",
                      "category": "", "jd": desc[:2000], "url": link, "updated": "", "kw": ""})
    return items


# ---------------------------------------------------------------- 筛选与打分

def hits(text, kws):
    low = (text or "").lower()
    return [k for k in kws if k in low]


def score_job(j):
    title_hits_s = hits(j["title"], STRONG_KW)
    title_hits_m = hits(j["title"], MID_KW)
    jd_s = hits(j["jd"], STRONG_KW)
    jd_m = hits(j["jd"], MID_KW)
    s = min(45, 45 * bool(title_hits_s)) + min(15, 15 * bool(title_hits_m))
    s += min(20, 5 * len(jd_s)) + min(10, 2 * len(jd_m))
    if "广州" in j["city"]:
        s += 10
    elif "深圳" in j["city"]:
        s += 8
    elif "远程" in j["city"]:
        s += 10
    reason = []
    if title_hits_s:
        reason.append("标题命中: " + "/".join(title_hits_s[:3]))
    if jd_s:
        reason.append("JD命中: " + "/".join(jd_s[:3]))
    return min(100, s), "；".join(reason) or "关键词弱相关"


def city_ok(city):
    return any(c in (city or "") for c in CITY_GATE)


# ---------------------------------------------------------------- 主流程

def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1))


def main():
    BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    channels, failures = {}, []

    for name, fn in [("腾讯", fetch_tencent), ("字节", fetch_bytedance),
                     ("DeepSeek", fetch_deepseek), ("电鸭", fetch_eleduck),
                     ("CKHR群", fetch_inbox)]:
        try:
            channels[name] = fn()
            print(f"[ok] {name}: {len(channels[name])} 条", file=sys.stderr)
        except Exception as e:
            channels[name] = []
            failures.append(f"{name}: {type(e).__name__} {e}")
            print(f"[fail] {name}: {e}", file=sys.stderr)

    seen = load_seen()
    now = datetime.now()
    scored, location_blocked, duck = [], [], []
    for name, jobs in channels.items():
        for j in jobs:
            key = j["url"] if name == "电鸭" else f'{j["company"]}|{j["title"]}'
            j["new"] = key not in seen
            seen[key] = now.strftime("%Y-%m-%d")
            if name == "电鸭":
                duck.append(j)  # 电鸭单列（远程零活，与坐班漏斗分开归线）
                continue
            if not city_ok(j["city"]):
                location_blocked.append(j)
                continue
            s, reason = score_job(j)
            j["score"], j["reason"] = s, reason
            scored.append(j)

    scored.sort(key=lambda x: -x["score"])
    save_seen(seen)

    def row(j):
        flag = "🆕 " if j["new"] else ""
        return (f"| {flag}{j['company']} | {j['title']} | {j['city']} | {j['score']} | "
                f"{j['reason']} | [JD]({j['url']}) |")

    lines = [
        f"# 岗位简报 {now.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"> 机器预筛产物，fit_score 为粗筛分（口径见 scripts/job_brief.py 头注释），投递由人终审。",
        f"> 城市字段取自平台 API 主城市，多城岗位（标题含多城）以 JD 页为准。",
        f"> 通道状态：{'、'.join(f'{k} {len(v)}条' for k, v in channels.items())}"
        + (f"；失败：{'；'.join(failures)}" if failures else "；全部成功"),
        "",
        "## 推荐位（过位置关 + 标题强命中 + score≥45）",
        "",
        "| 公司 | 岗位 | 城市 | 分 | 命中 | 链接 |",
        "|---|---|---|---|---|---|",
    ]
    candidates = [j for j in scored if j["score"] >= 45 and hits(j["title"], STRONG_KW)]
    candidates.sort(key=lambda x: -x["score"])
    top, rest = candidates[:25], candidates[25:]  # 推荐位封顶 25 条防刷屏
    lines += [row(j) for j in top] or ["（本时段无推荐位岗位）"]
    lines += ["", "## 其他过位置关（未进推荐位）", ""]
    lines += [row(j) for j in rest] or ["（无）"]
    lines += [
        "", f"## 位置关不通过（{len(location_blocked)} 条，北京/杭州等，仅留痕）", "",
        "| 公司 | 岗位 | 城市 |", "|---|---|---|",
    ]
    lines += [f'| {j["company"]} | {j["title"]} | {j["city"]} |' for j in location_blocked] or ["（无）"]
    duck_kw = [j for j in duck if hits(j["title"] + j["jd"], STRONG_KW + MID_KW)]
    lines += [
        "", f"## 电鸭 RSS 近帖（{len(duck)} 条，关键词相关 {len(duck_kw)} 条）", "",
        "| 🆕 | 标题 | 链接 |", "|---|---|---|",
    ]
    lines += [f'| {"🆕" if j["new"] else ""} | {j["title"]} | [帖]({j["url"]}) |'
              for j in duck_kw] or ["（无相关帖）"]

    out = BRIEF_DIR / f"brief-{now.strftime('%Y%m%d-%H%M')}.md"
    out.write_text("\n".join(lines) + "\n")
    print(out)
    print(f"推荐 {len(top)} / 次级 {len(rest)} / 位置关外 {len(location_blocked)} / 电鸭 {len(duck)}")


if __name__ == "__main__":
    main()
