#!/usr/bin/env python3
"""台账口径计算与安全写回：漏斗、转化率、成本、期望值全部由代码算。

为什么需要它：转化率口径（`docs/bidding-record-sop.md` 第 1 节）写得很细，但每次靠
模型手算，错一格就是假结论——分母该不该计入、parked 算不算在途、won 要不要退款确认，
任何一处理解偏差都会让「哪个渠道值得投」的判断失真。本脚本把口径固化成代码：

  * stats     按 SOP 口径算漏斗、转化率、按渠道 / 类型的投入产出、死层与输因计数；
  * open      列出在途样本与静默天数，按 30 天关闭规则提示该跟进还是该判 lost；
  * validate  检查 CSV 结构（列数、枚举、日期、数字、逗号 / 引号污染、逻辑矛盾）；
  * add       追加一行（自动生成 record_id，拒绝暗含逗号 / 换行的脏字段）；
  * update    按 record_id 只改指定列，保留其余列与行序（防错列）；
  * next-id   生成下一个编号（渠道前缀 + 日期 + 序号）；
  * report    生成可离线打开的漏斗 HTML 报告（内联 SVG，无外部依赖）。

口径（与 SOP v0.2.2 一致，不另立一套）：
  转化成功 won   = 收到钱且退款条款排除全退（台账 amount_received + refund_irreversible）
  转化率         = won / (won + lost)，只算「已投出且有判定结果」的样本
  在途           = 已投出、尚无 won/lost 判定（含 parked）——不进分母
  pass / 观望    = 从未对外投出——不进分母（选单漏斗）
  每小时毛收入   = 该渠道 won 实收合计 / 该渠道累计投入小时（时间计入投入、不折价）

用法：
  python3 scripts/ledger.py validate
  python3 scripts/ledger.py stats --md
  python3 scripts/ledger.py open
  python3 scripts/ledger.py next-id --source 电鸭-私信
  python3 scripts/ledger.py add --source 电鸭-私信 --type AI应用 --summary "…" \\
      --fit 78 --decision sent --sent today --version L-私信-v1.0 --hours 0.5 --cost 5
  python3 scripts/ledger.py update --id DY-260905-01 --set connected_at=today
  python3 scripts/ledger.py report --out docs/reports/funnel-20261021.html

台账文件默认 docs/ledger.csv（docs/ 已被 .gitignore 忽略，真实内容不进 git）。
"""

from __future__ import annotations

import argparse
import csv
import html as html_lib
import json
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "docs" / "ledger.csv"

HEADER = [
    "record_id", "created_at", "source", "job_type", "summary_clean", "fit_score",
    "decision", "sent_at", "proposal_version", "connected_at", "engaged_at",
    "outcome", "close_date", "hours_spent", "cost_money", "amount_received",
    "refund_irreversible", "dead_layer", "lost_reason_layer", "next_action", "lesson",
]
DATE_FIELDS = {"created_at", "sent_at", "connected_at", "engaged_at", "close_date"}
NUM_FIELDS = {"fit_score", "hours_spent", "cost_money", "amount_received"}
FREE_TEXT_FIELDS = {"source", "job_type", "summary_clean", "proposal_version", "next_action", "lesson"}

DECISIONS = {"sent", "parked", "passed"}
OUTCOMES = {"won", "lost", "parked", ""}
# 选单层 = 从未投出就被 pass / 观望的样本，它不属于「投出后死在哪一层」，
# 单列是为了让漏斗瓶颈统计覆盖入口（漏投也是漏，且是最便宜能改的一层）。
DEAD_LAYERS = {"选单层", "投出层", "联系层", "提案层", "对话层", ""}
LOST_REASONS = {"方案", "信任", "报价", "时机", "对手", "无回应", "其他", ""}

# 渠道前缀：record_id = 前缀-YYMMDD-序号。新渠道在这里加一行，未知渠道用 GEN 并提示。
SOURCE_PREFIX = {
    "电鸭": "DY",
    "Upwork": "UP",
    "BOSS": "BOSS",
    "CKHR": "CK",
    "官网直投": "OFF",
    "LinkedIn": "LI",
    "微信": "WX",
}

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?$")
ID_RE = re.compile(r"^(?P<prefix>[A-Z]+)-(?P<date>\d{6})-(?P<seq>\d{2,})$")


# ---------------------------------------------------------------- 读写

def parse_date(value: str) -> date | None:
    v = (value or "").strip()
    if not v:
        return None
    if not ISO_RE.match(v):
        raise ValueError(f"日期格式应为 YYYY-MM-DD（created_at 可带 HH:MM），实际 {v!r}")
    # 允许 created_at 带时间（早期台账按分钟记，保留不迁移），只取日期部分参与计算
    return date.fromisoformat(v[:10])


def resolve_today(value: str) -> str:
    return date.today().isoformat() if value.strip().lower() == "today" else value


def read_table(path: Path) -> tuple[list[str], list[dict], list[str]]:
    """返回 (表头, 行列表, 结构错误)。行列表保持行序，字段缺失补空串。"""
    if not path.exists():
        raise FileNotFoundError(f"台账不存在：{path}")
    text = path.read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], [], ["台账为空"]

    rows: list[dict] = []
    errors: list[str] = []
    header = next(csv.reader([lines[0]]))
    if header != HEADER:
        missing = [c for c in HEADER if c not in header]
        extra = [c for c in header if c not in HEADER]
        errors.append(
            f"表头与 SOP 不一致（缺少 {missing or '无'}；多出 {extra or '无'}）"
        )
    for lineno, raw in enumerate(lines[1:], start=2):
        try:
            fields = next(csv.reader([raw]))
        except csv.Error as exc:  # 引号不配对等
            errors.append(f"第 {lineno} 行解析失败：{exc}")
            continue
        if len(fields) != len(header):
            errors.append(
                f"第 {lineno} 行有 {len(fields)} 列，表头 {len(header)} 列"
                f"（多半是字段里混了英文逗号或引号）"
            )
        row = {key: "" for key in HEADER}
        for key, value in zip(header, fields):
            row[key] = value.strip()
        row["_line"] = str(lineno)
        rows.append(row)
    return header, rows, errors


def write_table(path: Path, rows: list[dict]) -> None:
    """原子写：先写临时文件再替换，避免中途失败写坏台账。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=str(path.parent), delete=False
    ) as fh:
        tmp = Path(fh.name)
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(HEADER)
        for row in rows:
            writer.writerow([row.get(key, "") for key in HEADER])
    tmp.replace(path)


# ---------------------------------------------------------------- 校验

def check_row(row: dict) -> tuple[list[str], list[str]]:
    """返回 (错误, 警告)。错误会挡住写入 / 计入统计；警告只提示。"""
    errors: list[str] = []
    warnings: list[str] = []
    rid = row.get("record_id") or "（无编号）"

    for field in DATE_FIELDS:
        try:
            parse_date(row.get(field, ""))
        except ValueError as exc:
            errors.append(f"{rid}: {field} {exc}")

    for field in NUM_FIELDS:
        value = row.get(field, "")
        if not value:
            continue
        try:
            number = float(value)
        except ValueError:
            errors.append(f"{rid}: {field} 不是数字（{value!r}）")
            continue
        if number < 0:
            errors.append(f"{rid}: {field} 不能为负（{value!r}）")
    if row.get("fit_score"):
        try:
            if not 0 <= float(row["fit_score"]) <= 100:
                errors.append(f"{rid}: fit_score 应在 0-100（{row['fit_score']}）")
        except ValueError:
            pass

    if row.get("decision") and row["decision"] not in DECISIONS:
        errors.append(f"{rid}: decision 只能是 sent / parked / passed，实际 {row['decision']!r}")
    if row.get("outcome", "") not in OUTCOMES:
        errors.append(f"{rid}: outcome 只能是 won / lost / parked / 空，实际 {row['outcome']!r}")
    if row.get("dead_layer", "") not in DEAD_LAYERS:
        errors.append(f"{rid}: dead_layer 只能是 {' / '.join(sorted(DEAD_LAYERS - {''}))} 或空")
    if row.get("lost_reason_layer", "") not in LOST_REASONS:
        errors.append(
            f"{rid}: lost_reason_layer 只能是 {' / '.join(sorted(LOST_REASONS - {''}))} 或空"
            "（具体原因写进 lesson / next_action，枚举字段只放词表里的值）"
        )
    if row.get("refund_irreversible", "") not in {"true", "false", ""}:
        errors.append(f"{rid}: refund_irreversible 只能是 true / false / 空")

    for field in FREE_TEXT_FIELDS:
        value = row.get(field, "")
        if any(ch in value for ch in (",", '"', "\n", "\r")):
            errors.append(f"{rid}: {field} 含英文逗号 / 引号 / 换行，会把 CSV 列挤错")

    # 逻辑一致性
    if row.get("decision") == "sent" and not row.get("sent_at"):
        errors.append(f"{rid}: decision=sent 但没有 sent_at（样本进不了分母）")
    if row.get("sent_at") and row.get("decision") not in {"sent", ""}:
        errors.append(f"{rid}: 有 sent_at 但 decision={row.get('decision')}（已投出应为 sent）")
    if row.get("outcome") == "won":
        if not row.get("amount_received"):
            errors.append(f"{rid}: outcome=won 但 amount_received 为空")
        if row.get("refund_irreversible") != "true":
            errors.append(f"{rid}: outcome=won 但 refund_irreversible 不是 true（口径要求收到不可全退的钱）")
    if row.get("dead_layer") == "选单层" and row.get("sent_at"):
        errors.append(f"{rid}: dead_layer=选单层 但有 sent_at（投出后死层应记 投出层 / 联系层 / 提案层 / 对话层）")
    if row.get("outcome") == "lost":
        if not row.get("dead_layer"):
            warnings.append(f"{rid}: lost 但未记 dead_layer（漏斗瓶颈统计会漏）")
        if not row.get("lost_reason_layer"):
            warnings.append(f"{rid}: lost 但未记 lost_reason_layer（归因统计会漏）")
    if row.get("sent_at") and row.get("outcome") and row["outcome"] != "parked" and not row.get("close_date"):
        warnings.append(f"{rid}: 已有终态 outcome={row['outcome']} 但 close_date 为空")
    if row.get("outcome") and not row.get("sent_at"):
        warnings.append(f"{rid}: 有 outcome 但没有 sent_at，不计入转化率分母")
    return errors, warnings


def validate_rows(rows: list[dict]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen: dict[str, str] = {}
    for row in rows:
        rid = row.get("record_id", "")
        if not rid:
            errors.append(f"第 {row.get('_line', '?')} 行缺少 record_id")
        elif rid in seen:
            errors.append(f"record_id 重复：{rid}（第 {seen[rid]} 行与第 {row.get('_line')} 行）")
        else:
            seen[rid] = row.get("_line", "?")
        e, w = check_row(row)
        errors += e
        warnings += w
    return errors, warnings


# ---------------------------------------------------------------- 统计

def fnum(value: str) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def as_bool(value: str) -> bool:
    return (value or "").lower() == "true"


def compute_stats(rows: list[dict]) -> dict:
    sent = [r for r in rows if r.get("sent_at")]
    won = [r for r in sent if r.get("outcome") == "won"]
    lost = [r for r in sent if r.get("outcome") == "lost"]
    decided = won + lost

    # 按渠道 / 类型汇总：只看已投出的样本（选单层 pass 不影响渠道价值口径）
    def group(key: str) -> list[dict]:
        buckets: dict[str, list[dict]] = {}
        for row in sent:
            name = row.get(key) or "（未填）"
            buckets.setdefault(name, []).append(row)
        out = []
        for name, items in buckets.items():
            w = sum(1 for r in items if r.get("outcome") == "won")
            lost_n = sum(1 for r in items if r.get("outcome") == "lost")
            hours = sum(fnum(r.get("hours_spent")) for r in items)
            revenue = sum(fnum(r.get("amount_received")) for r in items)
            out.append({
                "name": name,
                "sent": len(items),
                "connected": sum(1 for r in items if r.get("connected_at")),
                "engaged": sum(1 for r in items if r.get("engaged_at")),
                "won": w,
                "lost": lost_n,
                "decided": w + lost_n,
                "conversion": round(w / (w + lost_n), 4) if w + lost_n else None,
                "hours": round(hours, 2),
                "cost": round(sum(fnum(r.get("cost_money")) for r in items), 2),
                "revenue": round(revenue, 2),
                "revenue_per_hour": round(revenue / hours, 2) if hours else None,
            })
        out.sort(key=lambda x: (-x["sent"], x["name"]))
        return out

    hours_all = sum(fnum(r.get("hours_spent")) for r in rows)
    revenue_all = sum(fnum(r.get("amount_received")) for r in rows)
    cost_all = sum(fnum(r.get("cost_money")) for r in rows)
    today = date.today()

    def sent_within(days: int) -> int:
        count = 0
        for row in sent:
            try:
                if today - parse_date(row["sent_at"]) <= timedelta(days=days):
                    count += 1
            except ValueError:
                continue
        return count

    stats = {
        "file_rows": len(rows),
        "leads": len(rows),
        "sent": len(sent),
        "connected": sum(1 for r in sent if r.get("connected_at")),
        "engaged": sum(1 for r in sent if r.get("engaged_at")),
        "won": len(won),
        "lost": len(lost),
        "decided": len(decided),
        "in_flight": len(sent) - len(decided),
        "selected_out": sum(1 for r in rows if not r.get("sent_at") and r.get("decision") in {"passed", "parked"}),
        "conversion": round(len(won) / len(decided), 4) if decided else None,
        "sample_small": len(decided) < 10,
        "sent_last_7d": sent_within(7),
        "hours_total": round(hours_all, 2),
        "cost_total": round(cost_all, 2),
        "revenue_total": round(revenue_all, 2),
        "revenue_per_hour": round(revenue_all / hours_all, 2) if hours_all else None,
        "by_source": group("source"),
        "by_type": group("job_type"),
        "dead_layers": {},
        "lost_reasons": {},
    }
    for row in lost:
        dead = row.get("dead_layer") or "（未记）"
        reason = row.get("lost_reason_layer") or "（未记）"
        stats["dead_layers"][dead] = stats["dead_layers"].get(dead, 0) + 1
        stats["lost_reasons"][reason] = stats["lost_reasons"].get(reason, 0) + 1
    return stats


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def money(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f}"


def render_stats_md(stats: dict) -> str:
    lines = [
        "## 漏斗（SOP v0.2.2 口径）",
        "",
        "| 层级 | 数量 | 说明 |",
        "|---|---|---|",
        f"| 线索 | {stats['leads']} | 台账行数 |",
        f"| 投出 | {stats['sent']} | `sent_at` 非空 |",
        f"| 回应 | {stats['connected']} | `connected_at` 非空 |",
        f"| 谈单 | {stats['engaged']} | `engaged_at` 非空 |",
        f"| 成交 | {stats['won']} | `outcome=won` |",
        "",
        f"- 转化率：**{pct(stats['conversion'])}**（won {stats['won']} / 已判定 {stats['decided']}；"
        f"在途 {stats['in_flight']} 不进分母）",
        f"- 近 7 天投出：{stats['sent_last_7d']} 条",
        f"- 累计投入：{stats['hours_total']} 小时 / {money(stats['cost_total'])} 元；"
        f"实收 {money(stats['revenue_total'])} 元；"
        f"每小时毛收入 {money(stats['revenue_per_hour'])} 元",
        f"- 选单层 pass / 观望：{stats['selected_out']} 条（不进分母）",
    ]
    if stats["sample_small"]:
        lines.append(
            f"- ⚠ 已判定样本 {stats['decided']} 条 < 10：按 SOP 样本量纪律，"
            "只积累、不做「哪套模板更好 / 哪个价格带更好」的结论"
        )
    if stats["by_source"]:
        lines += [
            "", "### 按渠道", "",
            "| 渠道 | 投出 | 回应 | 谈单 | 成交 | 已判定 | 转化率 | 小时 | 货币成本 | 实收 | 每小时毛收入 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for item in stats["by_source"]:
            lines.append(
                f"| {item['name']} | {item['sent']} | {item['connected']} | {item['engaged']} | "
                f"{item['won']} | {item['decided']} | {pct(item['conversion'])} | "
                f"{item['hours']} | {money(item['cost'])} | {money(item['revenue'])} | "
                f"{money(item['revenue_per_hour'])} |"
            )
    if stats["dead_layers"]:
        lines += ["", "### 死层", "", "| 层 | 样本 |", "|---|---|"]
        for key, count in sorted(stats["dead_layers"].items(), key=lambda x: -x[1]):
            lines.append(f"| {key} | {count} |")
    if stats["lost_reasons"]:
        lines += ["", "### 输单归因", "", "| 归因 | 样本 |", "|---|---|"]
        for key, count in sorted(stats["lost_reasons"].items(), key=lambda x: -x[1]):
            lines.append(f"| {key} | {count} |")
    return "\n".join(lines)


def render_report(stats: dict, rows: list[dict]) -> str:
    """生成可离线打开的 HTML：内联 CSS + 内联 SVG，无外部依赖。"""
    def esc(value: object) -> str:
        return html_lib.escape(str(value), quote=True)

    funnel = [
        ("投出", stats["sent"]), ("回应", stats["connected"]),
        ("谈单", stats["engaged"]), ("成交", stats["won"]),
    ]
    maxv = max((v for _, v in funnel), default=0) or 1
    bars = []
    for i, (label, value) in enumerate(funnel):
        width = int(560 * value / maxv) if value else 0
        y = 20 + i * 52
        bars.append(
            f'<text x="0" y="{y + 16}" font-size="13" fill="#333">{esc(label)}</text>'
            f'<rect x="52" y="{y}" width="{max(width, 2)}" height="22" rx="4" fill="#2f6feb" opacity="{0.55 + i * 0.15:.2f}"/>'
            f'<text x="{52 + max(width, 2) + 8}" y="{y + 16}" font-size="13" fill="#333">{value}</text>'
        )
    funnel_svg = (
        f'<svg viewBox="0 0 640 230" width="100%" height="230" role="img" '
        f'aria-label="漏斗：投出 {stats["sent"]}，回应 {stats["connected"]}，'
        f'谈单 {stats["engaged"]}，成交 {stats["won"]}">' + "".join(bars) + "</svg>"
    )

    source_rows = "".join(
        f"<tr><td>{esc(i['name'])}</td><td>{i['sent']}</td><td>{i['connected']}</td>"
        f"<td>{i['engaged']}</td><td>{i['won']}</td><td>{i['decided']}</td>"
        f"<td>{pct(i['conversion'])}</td><td>{i['hours']}</td><td>{money(i['cost'])}</td>"
        f"<td>{money(i['revenue'])}</td></tr>"
        for i in stats["by_source"]
    ) or '<tr><td colspan="10">暂无已投出样本</td></tr>'

    recent = sorted(
        (r for r in rows if r.get("sent_at")),
        key=lambda r: r.get("sent_at", ""), reverse=True,
    )[:15]
    recent_rows = "".join(
        f"<tr><td>{esc(r.get('record_id',''))}</td><td>{esc(r.get('source',''))}</td>"
        f"<td>{esc(r.get('summary_clean',''))}</td><td>{esc(r.get('sent_at',''))}</td>"
        f"<td>{esc(r.get('outcome') or '在途')}</td><td>{esc(r.get('next_action',''))}</td></tr>"
        for r in recent
    ) or '<tr><td colspan="6">暂无已投出样本</td></tr>'

    return f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>投递漏斗报告 {esc(date.today().isoformat())}</title>
<style>
  body {{ font: 14px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; color:#222; margin:0; padding:32px; background:#f7f8fa; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:#666; margin-bottom:24px; }}
  .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:24px; }}
  .card {{ background:#fff; border-radius:10px; padding:14px 18px; min-width:150px; box-shadow:0 1px 3px rgba(0,0,0,.06); border-left:3px solid #2f6feb; }}
  .card b {{ display:block; font-size:24px; }}
  .card span {{ color:#666; font-size:12px; }}
  section {{ background:#fff; border-radius:10px; padding:18px 20px; margin-bottom:18px; box-shadow:0 1px 3px rgba(0,0,0,.06); }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid #eef0f3; }}
  th {{ color:#666; font-weight:600; }}
  .warn {{ color:#b3261e; }}
</style>
<h1>投递漏斗报告</h1>
<div class="sub">生成于 {esc(datetime.now().strftime('%Y-%m-%d %H:%M'))} · 数据源 docs/ledger.csv · 口径见 docs/bidding-record-sop.md</div>
<div class="cards">
  <div class="card"><b>{stats['sent']}</b><span>投出</span></div>
  <div class="card"><b>{stats['connected']}</b><span>回应</span></div>
  <div class="card"><b>{stats['engaged']}</b><span>谈单</span></div>
  <div class="card"><b>{stats['won']}</b><span>成交</span></div>
  <div class="card"><b>{pct(stats['conversion'])}</b><span>转化率（won/{stats['decided']}）</span></div>
  <div class="card"><b>{stats['hours_total']}</b><span>累计小时</span></div>
</div>
{"<section class='warn'>⚠ 已判定样本不足 10 条：按 SOP 样本量纪律，只积累，不做模板 / 价格带结论。</section>" if stats['sample_small'] else ""}
<section><h2>五级漏斗</h2>{funnel_svg}</section>
<section><h2>按渠道</h2>
<table><thead><tr><th>渠道</th><th>投出</th><th>回应</th><th>谈单</th><th>成交</th><th>已判定</th><th>转化率</th><th>小时</th><th>货币成本</th><th>实收</th></tr></thead>
<tbody>{source_rows}</tbody></table></section>
<section><h2>最近投出</h2>
<table><thead><tr><th>编号</th><th>渠道</th><th>岗位</th><th>投出日期</th><th>结果</th><th>下一步</th></tr></thead>
<tbody>{recent_rows}</tbody></table></section>
</html>
"""


# ---------------------------------------------------------------- 子命令

def cmd_validate(args) -> int:
    path = Path(args.file)
    try:
        header, rows, errors = read_table(path)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    row_errors, warnings = validate_rows(rows)
    errors += row_errors
    for msg in errors:
        print(f"❌ {msg}")
    for msg in warnings:
        print(f"⚠ {msg}")
    if not errors:
        print(f"✅ 结构正常：{len(rows)} 行 / {len(header)} 列")
    print(f"—— 错误 {len(errors)}、警告 {len(warnings)}")
    return 1 if errors else 0


def cmd_stats(args) -> int:
    try:
        _, rows, errors = read_table(Path(args.file))
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    if errors:
        for msg in errors:
            print(f"❌ {msg}", file=sys.stderr)
        print("先跑 `ledger.py validate` 修结构，统计不可信。", file=sys.stderr)
        return 1
    stats = compute_stats(rows)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    else:
        print(render_stats_md(stats))
    return 0


def cmd_open(args) -> int:
    try:
        _, rows, errors = read_table(Path(args.file))
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    if errors:
        for msg in errors:
            print(f"❌ {msg}", file=sys.stderr)
        return 1

    today = date.today()
    lines = ["## 在途样本", ""]
    count = 0
    for row in rows:
        if row.get("outcome") in {"won", "lost"}:
            continue
        if not row.get("sent_at"):
            if not (args.include_parked and row.get("decision") == "parked"):
                continue
        stamps = [
            row.get(field) for field in ("sent_at", "connected_at", "engaged_at", "close_date")
            if row.get(field)
        ]
        last = max(stamps)
        try:
            quiet = (today - parse_date(last)).days
        except ValueError:
            quiet = -1
        if quiet < 0:
            action = "日期异常，先修台账"
        elif quiet >= 30:
            action = "≥30 天无推进：按 SOP 转 lost"
        elif row.get("outcome") == "parked":
            action = f"parked：每 7 天轻跟进一次（已 {quiet} 天，最多 3 次）"
        else:
            action = f"待跟进（已 {quiet} 天静默）"
        lines.append(
            f"- **{row.get('record_id')}** {row.get('source')} ｜ {row.get('summary_clean')} ｜ "
            f"最后活动 {last}（{quiet} 天）｜ {action}"
            + (f" ｜ 计划：{row.get('next_action')}" if row.get("next_action") else "")
        )
        count += 1
    if not count:
        lines.append("（无在途样本）")
    print("\n".join(lines))
    return 0


def cmd_next_id(args) -> int:
    prefix = args.prefix
    if not prefix:
        for key, value in SOURCE_PREFIX.items():
            if key in (args.source or ""):
                prefix = value
                break
    if not prefix:
        prefix = "GEN"
        print("（未匹配到渠道前缀，用 GEN；确认后用 --prefix 指定）", file=sys.stderr)
    stamp = date.today().strftime("%y%m%d")
    seq = 0
    try:
        _, rows, _ = read_table(Path(args.file))
    except FileNotFoundError:
        rows = []
    for row in rows:
        m = ID_RE.match(row.get("record_id", ""))
        if m and m.group("prefix") == prefix and m.group("date") == stamp:
            seq = max(seq, int(m.group("seq")))
    print(f"{prefix}-{stamp}-{seq + 1:02d}")
    return 0


def _clean_free_text(value: str) -> str:
    return value.replace(",", "，").replace('"', "“").replace("\n", " ").replace("\r", " ")


def cmd_add(args) -> int:
    path = Path(args.file)
    try:
        _, rows, errors = read_table(path)
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(",".join(HEADER) + "\n", encoding="utf-8")
        rows, errors = [], []
    if errors:
        for msg in errors:
            print(f"❌ {msg}", file=sys.stderr)
        print("台账结构有问题，先跑 validate。", file=sys.stderr)
        return 1

    record_id = args.id
    if not record_id:
        stamp = date.today().strftime("%y%m%d")
        prefix = args.prefix
        if not prefix:
            for key, value in SOURCE_PREFIX.items():
                if key in (args.source or ""):
                    prefix = value
                    break
        prefix = prefix or "GEN"
        seq = 0
        for row in rows:
            m = ID_RE.match(row.get("record_id", ""))
            if m and m.group("prefix") == prefix and m.group("date") == stamp:
                seq = max(seq, int(m.group("seq")))
        record_id = f"{prefix}-{stamp}-{seq + 1:02d}"

    row = {key: "" for key in HEADER}
    row.update({
        "record_id": record_id,
        "created_at": resolve_today(args.created or "today"),
        "source": args.source,
        "job_type": args.type or "",
        "summary_clean": args.summary,
        "fit_score": args.fit or "",
        "decision": args.decision,
        "sent_at": resolve_today(args.sent or ""),
        "proposal_version": args.version or "",
        "connected_at": resolve_today(args.connected or ""),
        "engaged_at": resolve_today(args.engaged or ""),
        "outcome": args.outcome or "",
        "close_date": resolve_today(args.close or ""),
        "hours_spent": args.hours or "",
        "cost_money": args.cost or "",
        "amount_received": args.amount or "",
        "refund_irreversible": args.refund or "",
        "dead_layer": args.dead or "",
        "lost_reason_layer": args.lost_reason or "",
        "next_action": args.next_action or "",
        "lesson": args.lesson or "",
    })
    if args.fix_commas:
        for field in FREE_TEXT_FIELDS:
            row[field] = _clean_free_text(row[field])

    errors, warnings = check_row(row)
    for msg in warnings:
        print(f"⚠ {msg}", file=sys.stderr)
    if errors:
        for msg in errors:
            print(f"❌ {msg}", file=sys.stderr)
        return 1
    if args.dry_run:
        print(",".join(row.get(key, "") for key in HEADER))
        return 0

    rows.append(row)
    write_table(path, rows)
    print(f"✅ 已追加 {record_id}（{len(rows)} 行）")
    return 0


def cmd_update(args) -> int:
    path = Path(args.file)
    try:
        _, rows, errors = read_table(path)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    if errors:
        for msg in errors:
            print(f"❌ {msg}", file=sys.stderr)
        return 1

    target = None
    for row in rows:
        if row.get("record_id") == args.id:
            target = row
            break
    if target is None:
        print(f"找不到 {args.id}（用 `ledger.py stats` 看现有编号）", file=sys.stderr)
        return 1

    changed = []
    for pair in args.set or []:
        if "=" not in pair:
            print(f"--set 需要 field=value，实际 {pair!r}", file=sys.stderr)
            return 1
        field, value = pair.split("=", 1)
        field = field.strip()
        if field not in HEADER:
            print(f"未知字段 {field!r}；可用字段：{', '.join(HEADER)}", file=sys.stderr)
            return 1
        if field == "record_id":
            print("record_id 不允许改（编号是引用锚点）", file=sys.stderr)
            return 1
        if field in FREE_TEXT_FIELDS and args.fix_commas:
            value = _clean_free_text(value)
        value = resolve_today(value) if field in DATE_FIELDS else value
        target[field] = value
        changed.append(field)

    errors, warnings = check_row(target)
    for msg in warnings:
        print(f"⚠ {msg}", file=sys.stderr)
    if errors:
        for msg in errors:
            print(f"❌ {msg}", file=sys.stderr)
        return 1
    if not changed:
        print("没有要改的字段（--set 没给）", file=sys.stderr)
        return 1
    if args.dry_run:
        print(",".join(target.get(key, "") for key in HEADER))
        return 0

    write_table(path, rows)
    print(f"✅ 已更新 {args.id}：{', '.join(sorted(set(changed)))}")
    return 0


def cmd_report(args) -> int:
    try:
        _, rows, errors = read_table(Path(args.file))
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    if errors:
        for msg in errors:
            print(f"❌ {msg}", file=sys.stderr)
        return 1
    stats = compute_stats(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(stats, rows), encoding="utf-8")
    print(out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="台账口径计算与安全写回（SOP v0.2.2）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="台账路径（默认 docs/ledger.csv）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="检查结构、枚举、日期、数字与逻辑矛盾")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("stats", help="按 SOP 口径算漏斗 / 转化率 / 渠道投入产出")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--md", action="store_true", help="输出 Markdown（默认）")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("open", help="列出在途样本与静默天数")
    p.add_argument("--include-parked", action="store_true", help="连选单层的 parked 一起列")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("next-id", help="生成下一个 record_id")
    p.add_argument("--source", default="", help="渠道名（自动匹配前缀）")
    p.add_argument("--prefix", default="", help="直接指定前缀（如 DY）")
    p.set_defaults(func=cmd_next_id)

    p = sub.add_parser("add", help="追加一行（自动编号 + 字段校验）")
    p.add_argument("--id", default="", help="手动指定编号（默认自动生成）")
    p.add_argument("--prefix", default="", help="自动编号时用的渠道前缀")
    p.add_argument("--created", default="today", help="发现日期，默认今天")
    p.add_argument("--source", required=True, help="渠道 / 形式，如 电鸭-私信")
    p.add_argument("--type", default="", help="岗位类型：AI应用 / 自动化 / 轻量开发")
    p.add_argument("--summary", required=True, help="脱敏岗位一句话（不能含英文逗号）")
    p.add_argument("--fit", default="", help="fit_score 0-100")
    p.add_argument("--decision", required=True, choices=sorted(DECISIONS))
    p.add_argument("--sent", default="", help="投出日期（sent 必填，可写 today）")
    p.add_argument("--version", default="", help="模板版本号，如 L-私信-v1.0")
    p.add_argument("--connected", default="", help="联系上日期")
    p.add_argument("--engaged", default="", help="首次实质对话日期")
    p.add_argument("--outcome", default="", choices=sorted(OUTCOMES))
    p.add_argument("--close", default="", help="判定日期")
    p.add_argument("--hours", default="", help="累计投入小时")
    p.add_argument("--cost", default="", help="直接货币成本（元）")
    p.add_argument("--amount", default="", help="won 实收金额")
    p.add_argument("--refund", default="", choices=["", "true", "false"], help="是否确认不可全退")
    p.add_argument("--dead", default="", choices=sorted(DEAD_LAYERS), help="死层")
    p.add_argument("--lost-reason", dest="lost_reason", default="", choices=sorted(LOST_REASONS))
    p.add_argument("--next-action", dest="next_action", default="")
    p.add_argument("--lesson", default="")
    p.add_argument("--fix-commas", action="store_true", help="把自由文本里的英文逗号换成中文逗号")
    p.add_argument("--dry-run", action="store_true", help="只打印要写入的行，不落盘")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("update", help="按编号更新指定字段（只改这几列）")
    p.add_argument("--id", required=True, help="record_id")
    p.add_argument("--set", action="append", metavar="FIELD=VALUE", help="可重复，如 --set connected_at=today")
    p.add_argument("--fix-commas", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("report", help="生成离线 HTML 漏斗报告")
    p.add_argument("--out", required=True, help="输出路径，如 docs/reports/funnel.html")
    p.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
