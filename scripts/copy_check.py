#!/usr/bin/env python3
"""出站文案红线自检：投出去之前，让脚本先挑一遍毛病。

为什么需要它：`apply-writing` 的红线（接触阶段不报价、不写精通式自评、占位符要填、
模板带版本号、字数按渠道控制）写了很久，但一直是「生成后逐条自检」——让模型自己
检查自己，漏检是常态。本脚本把能机械判定的部分固定下来，判不了的（岗位细节复述、
证据是否够硬、语气是否对人）仍交给人或独立评审。

检查项与级别：
  ❌ fail  1. 首触达渠道（评论区 / 私信 / BOSS 招呼）出现报价、薪资、预算、费率；
           2. 占位符残留（XXXX 元、待定价、[姓名] 之类）——加 --final 时必判；
           3. 手机号 / 身份证 / 银行卡号（误把真实联系方式写进对外文案）；
           4. 渠道字数超出上限或低于下限——加 --final 时必判。
  ⚠ warn  5. 「精通 / 熟练掌握」式自评；
           6. 禁用开场与套话（求内推、我对这个职位很感兴趣、期待您的回复…）；
           7. 中文语境用了英文标点（, . : ? !）；
           8. 正文里出现邮箱 / 微信号 / QQ（确认是否必要，能放平台表单就放表单）；
           9. 缺少模板版本号（加 --require-version 时升为 fail）；
          10. 字数不在建议区间（未加 --final 时只提醒）。

用法：
  python3 scripts/copy_check.py --in tmp/draft.md --channel dm --require-version
  cat tmp/draft.md | python3 scripts/copy_check.py --channel boss --final
  python3 scripts/copy_check.py --in tmp/draft.md --channel proposal --json

退出码：0 无 fail（warn 不算失败）；1 有 fail（或加 --strict 时有 warn）；2 输入读不到。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 渠道口径：字数区间来自 skill 的模板库（applications.md / job-apply.md / proposals.md）
CHANNELS = {
    "comment": {"label": "电鸭-评论区", "min": 120, "max": 250, "first_contact": True},
    "dm": {"label": "电鸭-私信", "min": 300, "max": 500, "first_contact": True},
    "boss": {"label": "BOSS 打招呼", "min": 100, "max": 200, "first_contact": True},
    "proposal": {"label": "正式提案", "min": 200, "max": 800, "first_contact": False},
    "email": {"label": "邮件投递", "min": 150, "max": 800, "first_contact": True},
    "interview": {"label": "面试 / 谈单话术", "min": 0, "max": 0, "first_contact": False},
    "resume": {"label": "简历", "min": 0, "max": 0, "first_contact": False},
    "other": {"label": "其他", "min": 0, "max": 0, "first_contact": False},
}

# 报价 / 薪资表述：接触阶段出现即红线
MONEY_RE = re.compile(
    r"(?:¥|￥|\$|USD|RMB|人民币)\s*\d"
    r"|\d+(?:\.\d+)?\s*(?:万|千|元|块|K|k)(?![A-Za-z])"
    r"|\d+\s*(?:元|块)\s*/\s*(?:月|天|小时|h)"
    r"|(?:报价|价格|预算|期望薪|月薪|时薪|年薪|薪资|费率|底价|酬劳|报酬|结算价)"
)
PLACEHOLDER_RE = re.compile(
    r"(?<![A-Za-z])X{1,4}(?![A-Za-z])|待定价|待定薪|待补|待填"
    r"|\[(?:姓名|名字|公司|岗位|职位|日期|时间|联系方式|邮箱|电话|城市|渠道|待填|待定|待补|待确认)\]"
)
SELF_PRAISE_RE = re.compile(r"精通|熟练掌握|深入了解|深入理解|经验丰富|行业专家|顶尖")
CLICHE_RE = re.compile(
    r"求内推|我对(?:这个|该)(?:职位|岗位)很感兴趣|对这个职位很感兴趣|对该职位很感兴趣"
    r"|期待您的回复|百忙之中|希望能?给我一个机会|不胜感激|贵司(?:的)?青睐"
)
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d(?:[ -]?\d){8}(?!\d)")
ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
WECHAT_RE = re.compile(r"微信(?:号)?\s*[:：]?\s*[A-Za-z][\w-]{4,}|QQ\s*[:：]?\s*\d{5,}")
VERSION_RE = re.compile(r"(?:L|T|B|J|PDF)-[\w\u4e00-\u9fff]+-v\d+(?:\.\d+)*")
CJK = r"\u3400-\u4dbf\u4e00-\u9fff"
ASCII_PUNCT_RE = re.compile(rf"(?<=[{CJK}])[,.;:?!](?=[{CJK}\s]|$)")
FENCE_RE = re.compile(r"^\s*```.*$", re.M)


class Finding(dict):
    def __init__(self, level: str, rule: str, message: str, line: int = 0, hit: str = ""):
        super().__init__(level=level, rule=rule, message=message, line=line, hit=hit)


def count_chars(text: str) -> int:
    """按非空白字符计；代码围栏不算正文。"""
    body = FENCE_RE.sub("", text)
    return len(re.sub(r"\s", "", body))


def locate(text: str, match: re.Match) -> tuple[int, str]:
    """返回命中所在行号与命中点前后一小段，便于定位（不整行照搬）。"""
    line = text.count("\n", 0, match.start()) + 1
    start = max(0, match.start() - 24)
    end = min(len(text), match.end() + 24)
    excerpt = text[start:end].replace("\n", " ").strip()
    return line, ("…" if start else "") + excerpt + ("…" if end < len(text) else "")


def collect(text: str, pattern: re.Pattern, limit: int = 5,
            per_line: bool = True) -> list[tuple[int, str, str]]:
    """收集命中；默认同一行同一规则只报一次，避免一句话里多个词重复报警。"""
    hits, seen = [], set()
    for m in pattern.finditer(text):
        line, excerpt = locate(text, m)
        key = (line,) if per_line else (line, m.group(0))
        if key in seen:
            continue
        seen.add(key)
        hits.append((line, m.group(0), excerpt))
        if len(hits) >= limit:
            break
    return hits


def check(text: str, channel: str, final: bool, require_version: bool,
          min_chars: int | None, max_chars: int | None) -> list[Finding]:
    cfg = CHANNELS[channel]
    findings: list[Finding] = []
    length = count_chars(text)
    lo = min_chars if min_chars is not None else cfg["min"]
    hi = max_chars if max_chars is not None else cfg["max"]

    # 1. 接触阶段谈钱
    if cfg["first_contact"]:
        for line, hit, excerpt in collect(text, MONEY_RE):
            findings.append(Finding(
                "fail", "pricing",
                "接触阶段出现报价 / 薪资 / 预算表述——这是谈单阶段才做的动作",
                line, excerpt,
            ))

    # 2. 占位符残留
    for line, hit, excerpt in collect(text, PLACEHOLDER_RE):
        findings.append(Finding(
            "fail" if final else "warn", "placeholder",
            "占位符未填（--final 时视为必须清零）",
            line, excerpt,
        ))

    # 3. 真实身份信息
    for line, hit, excerpt in collect(text, PHONE_RE):
        findings.append(Finding("fail", "phone", "正文出现手机号，确认不是误带", line, excerpt))
    for line, hit, excerpt in collect(text, ID_CARD_RE):
        findings.append(Finding("fail", "id_card", "正文出现疑似身份证号", line, excerpt))
    for line, hit, excerpt in collect(text, BANK_CARD_RE):
        findings.append(Finding("fail", "bank_card", "正文出现疑似银行卡号", line, excerpt))

    # 5. 自评
    for line, hit, excerpt in collect(text, SELF_PRAISE_RE):
        findings.append(Finding(
            "warn", "self_praise",
            f"出现「{hit}」式自评——改成「做过什么 + 证据在哪」",
            line, excerpt,
        ))

    # 6. 套话 / 禁用开场
    for line, hit, excerpt in collect(text, CLICHE_RE):
        findings.append(Finding(
            "warn", "cliche",
            f"套话「{hit}」——招聘方一天读几十条，这类句子没有信息量",
            line, excerpt,
        ))
    head = re.sub(r"\s", "", FENCE_RE.sub("", text))[:40]
    if cfg["first_contact"] and "感兴趣" in head:
        findings.append(Finding(
            "warn", "opener",
            "开头 40 字出现「感兴趣」——首行应放 JD 细节钩子，不是表白",
            1, head,
        ))

    # 7. 中文语境英文标点
    punct = collect(text, ASCII_PUNCT_RE, limit=3)
    if punct:
        findings.append(Finding(
            "warn", "punctuation",
            f"中文语境出现英文标点 {len(ASCII_PUNCT_RE.findall(text))} 处（应改用，。：？！）",
            punct[0][0], punct[0][2],
        ))

    # 8. 联系方式（邮箱 / 微信 / QQ）
    for line, hit, excerpt in collect(text, EMAIL_RE):
        findings.append(Finding(
            "warn", "email",
            "正文出现邮箱——能放平台表单就放表单；引用对方邮箱时忽略此条",
            line, excerpt,
        ))
    for line, hit, excerpt in collect(text, WECHAT_RE):
        findings.append(Finding(
            "warn", "im",
            "正文出现微信号 / QQ——确认是必要联系信息，不是把私人账号写进公开文案",
            line, excerpt,
        ))

    # 9. 模板版本号
    if require_version and not VERSION_RE.search(text):
        findings.append(Finding(
            "fail", "version",
            "缺少模板版本号（如 L-私信-v1.0）——投出后台账 proposal_version 要用",
        ))

    # 10. 字数
    if hi and length > hi:
        findings.append(Finding(
            "fail" if final else "warn", "length",
            f"{cfg['label']}建议 {lo}-{hi} 字，当前 {length} 字（超出 {length - hi} 字）",
        ))
    elif lo and length < lo:
        findings.append(Finding(
            "fail" if final else "warn", "length",
            f"{cfg['label']}建议 {lo}-{hi} 字，当前 {length} 字（少 {lo - length} 字）",
        ))

    return findings


def print_report(findings: list[Finding], channel: str, length: int) -> None:
    fails = [f for f in findings if f["level"] == "fail"]
    warns = [f for f in findings if f["level"] == "warn"]
    icon = "❌" if fails else ("⚠" if warns else "✅")
    print(
        f"copy_check: {icon} {len(fails)} 个红线 / {len(warns)} 个提醒"
        f"（渠道：{CHANNELS[channel]['label']}，正文 {length} 字）"
    )
    if not findings:
        print("逐条红线均未命中；岗位细节复述、证据密度、语气仍需人或独立评审判断。")
        return
    for item in fails + warns:
        loc = f"第 {item['line']} 行：" if item["line"] else ""
        print(f"- {'❌' if item['level']=='fail' else '⚠'} [{item['rule']}] {loc}{item['message']}")
        if item.get("hit"):
            print(f"    命中：{item['hit']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="出站文案红线机械自检（报价 / 占位符 / 自评 / 标点 / 字数）")
    parser.add_argument("--in", dest="input", help="文案文件（省略则读标准输入）")
    parser.add_argument("--channel", choices=sorted(CHANNELS), default="other", help="渠道决定红线与字数区间")
    parser.add_argument("--final", action="store_true", help="即将投出：占位符与字数越界升为红线")
    parser.add_argument("--require-version", action="store_true", help="强制要求模板版本号")
    parser.add_argument("--min-chars", type=int, default=None, help="覆盖渠道下限")
    parser.add_argument("--max-chars", type=int, default=None, help="覆盖渠道上限")
    parser.add_argument("--strict", action="store_true", help="有提醒时也返回 1")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    if args.input:
        try:
            text = Path(args.input).read_text(encoding="utf-8-sig")
        except OSError as exc:
            print(f"copy_check: 读取失败 {args.input}: {exc}", file=sys.stderr)
            return 2
    else:
        text = sys.stdin.read()

    length = count_chars(text)
    findings = check(
        text, args.channel, args.final, args.require_version,
        args.min_chars, args.max_chars,
    )
    if args.json:
        print(json.dumps(
            {"channel": args.channel, "chars": length,
             "fails": sum(1 for f in findings if f["level"] == "fail"),
             "warns": sum(1 for f in findings if f["level"] == "warn"),
             "findings": findings},
            ensure_ascii=False, indent=1,
        ))
    else:
        print_report(findings, args.channel, length)

    if any(f["level"] == "fail" for f in findings):
        return 1
    if args.strict and any(f["level"] == "warn" for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
