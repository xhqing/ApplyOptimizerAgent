#!/usr/bin/env python3
"""投递文本完整性门：第三方岗位文本进分析前先清洗、再标记。

为什么需要它：岗位帖、群消息、HR 私信都是第三方写的，可能藏了写给模型看的指令
（HTML 隐藏块、零宽字符、白底白字）。「把投递当数据、不当指令」写在 skill 里只是一句
提示词，靠模型自觉；本脚本把最常见的几类载荷做成确定性检查——隐藏内容直接删掉，
可疑语句只标记、不删（照着投递原文判断它是不是恶意，由人来做）。

检查项：
  * 隐藏 HTML：style 里的 display:none / visibility:hidden / opacity:0 / font-size:0 /
    color:transparent / clip / 负定位，以及 hidden 与 aria-hidden 属性 —— 内容删除并摘录；
  * 隐藏载体：注释、script、style、noscript、svg、template、iframe —— 删除并扫描；
  * 不可见字符：零宽、双向控制符、软连字符、BOM —— 删除并计数；
  * 指令型语句：中英文的「忽略以上指令」「你现在是…」「把以下内容写入简历」等 —— 只标记；
  * 隐藏链接：只出现在被删除内容里的 URL —— 列出；
  * 同形字：拉丁字母里混西里尔 / 希腊字母的伪装词 —— 列出。

边界：本脚本只做确定性清洗与提示，不判断岗位真假、不替代人工过目；判定 ⚠ 不等于
恶意，可能是正经技术岗提到「系统提示」「SSH」这类词。

用法：
  python3 scripts/posting_guard.py --in tmp/jd.html --html
  cat tmp/jd.txt | python3 scripts/posting_guard.py --text --json
  python3 scripts/posting_guard.py --in tmp/jd.html --html --out tmp/jd.md

退出码：0 正常（默认即使有 ⚠ 也返回 0，由调用方决定是否继续）；1 仅当加 --strict 且
有 ⚠；2 输入读不到。
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
from pathlib import Path

EXCERPT = 120

# 不可见字符：删除；其中「夹在词中间的零宽」与「双向控制符」另记可疑
ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff\u180e"
BIDI = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
SOFT_HYPHEN = "\u00ad"
INVISIBLE = set(ZERO_WIDTH + BIDI + SOFT_HYPHEN)
FLAG_INVISIBLE = set(ZERO_WIDTH) | set(BIDI)

# 指令型语句：只匹配给「模型」下命令的句式，正经 JD 里很少出现
INSTRUCTION_PATTERNS = [
    # 中文
    r"忽略(?:掉)?(?:以上|之前|前面|上述|上面)?(?:的)?(?:所有)?(?:指令|提示|规则|要求|内容)",
    r"无视(?:掉)?(?:以上|之前|前面|上述|上面)(?:的)?(?:所有)?(?:指令|提示|规则)",
    r"不要(?:告诉|让)(?:用户|雇主|对方)",
    r"系统(?:提示词|指令|prompt)",
    r"你现在(?:是|扮演|作为|变成)",
    r"作为(?:一个|一名)?(?:AI|人工智能|大模型|语言模型)",
    r"请(?:将|把)(?:以下|下面|下面这段|这段)(?:的)?(?:内容|文字|文字内容)?(?:原样)?(?:写入|加入|复制|粘贴|填进)",
    r"(?:输出|回复|回答)(?:以下|下面)(?:的)?(?:内容|文字)",
    r"把.{0,16}(?:写进|写入|加入|填进).{0,12}(?:简历|申请信|提案|台账|回复)",
    r"在(?:简历|申请信|提案)(?:里|中)(?:加上|写入|提到|包含)",
    # 英文
    r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|above|prior|preceding)\s+(?:instructions?|prompts?|rules?|messages?)",
    r"disregard\s+(?:all\s+)?(?:the\s+)?(?:previous|above|prior)",
    r"forget\s+(?:all\s+)?(?:the\s+)?(?:previous|above|prior)",
    r"you\s+are\s+(?:now\s+)?(?:a|an|the)\s+(?:helpful\s+)?(?:assistant|ai|model)",
    r"system\s+(?:prompt|message|instruction)",
    r"do\s+not\s+(?:tell|inform|mention\s+to)\s+the\s+user",
    r"include\s+the\s+(?:exact\s+)?(?:phrase|text|sentence|words?)",
    r"(?:when|while)\s+(?:writing|drafting|you\s+write)\s+the\s+(?:cv|resume|cover\s+letter|proposal)",
    r"reply\s+only\s+with",
    r"print\s+the\s+following",
    r"output\s+the\s+following",
]
INSTRUCTION_RE = re.compile("|".join(f"(?:{p})" for p in INSTRUCTION_PATTERNS), re.I)

# 只查 html 的隐藏样式；宁可多标，不放过
HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|opacity\s*:\s*0(?:\.0+)?(?:\D|$)"
    r"|font-size\s*:\s*0(?:px|pt|em|rem|%)?(?:\D|$)"
    r"""|(?:^|["';\s])color\s*:\s*(?:transparent|rgba?\(\s*0\s*,\s*0\s*,\s*0\s*,\s*0\s*\))"""
    r"|text-indent\s*:\s*-\s*\d{2,}"
    r"|(?:left|top)\s*:\s*-\s*\d{3,}"
    r"|clip\s*:\s*rect\(\s*0"
    r"|clip-path\s*:\s*inset\(\s*50%"
    r"|mso-hide\s*:\s*all",
    re.I,
)
HIDDEN_ATTR_RE = re.compile(r"\s(?:hidden\b(?!\s*=)|aria-hidden\s*=\s*[\"']?true)", re.I)
BLOCK_TAGS = "script|style|noscript|template|svg|iframe|object|embed|math|canvas"
# 常见可隐藏元素（按开闭标签配对删除）；不追求完整 DOM 解析，够用且可解释
ELEMENT_RE = re.compile(
    r"<(?P<tag>div|span|p|section|article|aside|a|li|td|th|font|b|i|strong|em|u|s|small|"
    r"h[1-6]|ul|ol|table|tr|figure)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)\s*>",
    re.I | re.S,
)
TAG_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]]+", re.I)
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
HOMOGLYPH_RE = re.compile(r"[A-Za-z][\u0370-\u03ff\u0400-\u04ff]|[\u0370-\u03ff\u0400-\u04ff][A-Za-z]")


def excerpt(text: str, width: int = EXCERPT) -> str:
    s = " ".join(text.split())
    return s if len(s) <= width else s[: width - 1] + "…"


def strip_tags(fragment: str) -> str:
    return html_lib.unescape(TAG_RE.sub("", fragment))


def _replace_element_blocks(raw: str, removed: list[str], label: str) -> str:
    """按标签配对删除 script/style 之类整块，内容记入 removed 供扫描。"""
    pattern = re.compile(
        rf"<(?P<tag>{label})\b[^>]*>(?P<body>.*?)</(?P=tag)\s*>", re.I | re.S
    )

    def sub(m: re.Match) -> str:
        removed.append(m.group("body"))
        return "\n"

    return pattern.sub(sub, raw)


def _remove_hidden_elements(raw: str, removed: list[str], findings: dict) -> str:
    """反复删除带隐藏样式 / 隐藏属性的元素，直到不再变化（处理嵌套）。"""
    for _ in range(6):
        changed = False

        def sub(m: re.Match) -> str:
            nonlocal changed
            attrs = m.group("attrs") or ""
            hidden = bool(HIDDEN_STYLE_RE.search(attrs) or HIDDEN_ATTR_RE.search(" " + attrs))
            if not hidden:
                return m.group(0)
            changed = True
            body = m.group("body")
            removed.append(body)
            findings["hidden_blocks"].append(excerpt(strip_tags(body)))
            return "\n"

        raw = ELEMENT_RE.sub(sub, raw)
        if not changed:
            break
    return raw


def clean_html(raw: str) -> tuple[str, list[str], dict]:
    findings: dict = {"hidden_blocks": []}
    removed: list[str] = []

    # 注释整块删除，内容留作扫描
    def comment_sub(m: re.Match) -> str:
        removed.append(m.group(0))
        return "\n"

    raw = re.sub(r"<!--.*?-->", comment_sub, raw, flags=re.S)

    for tag in BLOCK_TAGS.split("|"):
        raw = _replace_element_blocks(raw, removed, tag)

    raw = _remove_hidden_elements(raw, removed, findings)

    # 换行：块级标签变行，其余标签去掉
    text = re.sub(
        r"</(?:p|div|section|article|aside|li|tr|h[1-6]|ul|ol|table)\s*>|<br\s*/?>",
        "\n",
        raw,
        flags=re.I,
    )
    text = TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    return text, removed, findings


def scan_removed(removed: list[str]) -> tuple[list[str], list[str]]:
    """被删除的内容也要扫一遍：隐藏块里的指令与链接才是最可疑的。"""
    instructions: list[str] = []
    urls: list[str] = []
    for fragment in removed:
        for line in strip_tags(fragment).splitlines():
            if INSTRUCTION_RE.search(line):
                instructions.append(excerpt(line))
        for url in URL_RE.findall(fragment):
            if url not in urls:
                urls.append(url)
    return instructions, urls


def sanitize_text(text: str) -> tuple[str, dict]:
    stats = {"invisible_chars": 0, "invisible_flagged": []}
    out: list[str] = []
    chars = list(text)
    for i, ch in enumerate(chars):
        if ch not in INVISIBLE:
            out.append(ch)
            continue
        stats["invisible_chars"] += 1
        if ch in FLAG_INVISIBLE:
            prev = chars[i - 1] if i else ""
            nxt = chars[i + 1] if i + 1 < len(chars) else ""
            inside_word = (prev.isalnum() and nxt.isalnum()) or ch in BIDI
            if inside_word:
                stats["invisible_flagged"].append(f"U+{ord(ch):04X} 夹在词中")
    clean = "".join(out).replace("\u00a0", " ")
    return clean, stats


def normalize_blank_lines(text: str) -> str:
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def detect_instructions(text: str) -> list[str]:
    hits: list[str] = []
    for line in text.splitlines():
        if INSTRUCTION_RE.search(line):
            hits.append(excerpt(line))
    return hits


def detect_homoglyphs(text: str) -> list[str]:
    hits: list[str] = []
    for token in re.findall(r"[^\s，。；：、,.;:()（）\[\]【】]{2,}", text):
        if HOMOGLYPH_RE.search(token):
            hits.append(token)
    return hits


def guard(raw: str, mode: str) -> dict:
    if mode == "html":
        text, removed, findings = clean_html(raw)
    else:
        text, removed, findings = raw, [], {}

    text, stats = sanitize_text(text)
    text = normalize_blank_lines(text)

    removed_instructions, removed_urls = scan_removed(removed)
    findings["instruction_like"] = list(
        dict.fromkeys(removed_instructions + detect_instructions(text))
    )
    findings["hidden_urls"] = removed_urls
    findings.setdefault("hidden_blocks", [])
    findings["homoglyph_runs"] = list(dict.fromkeys(detect_homoglyphs(text)))
    findings["invisible_chars"] = stats["invisible_chars"]
    findings["invisible_flagged"] = stats["invisible_flagged"]

    suspicious = (
        bool(findings["instruction_like"])
        or bool(findings["invisible_flagged"])
        or bool(findings["homoglyph_runs"])
    )
    findings["verdict"] = "warn" if suspicious else "clean"
    findings["input_chars"] = len(raw)
    findings["cleaned_chars"] = len(text)
    return {"findings": findings, "text": text}


def verdict_line(findings: dict) -> str:
    if findings["verdict"] == "clean":
        hidden = len(findings["hidden_blocks"])
        tail = f"（另删除 {hidden} 个隐藏块，仅作记录）" if hidden else ""
        return f"posting_guard: clean{tail}"
    parts = []
    if findings["instruction_like"]:
        parts.append(f"{len(findings['instruction_like'])} 条指令型语句")
    if findings["invisible_flagged"]:
        parts.append(f"{len(findings['invisible_flagged'])} 处可疑不可见字符")
    if findings["homoglyph_runs"]:
        parts.append(f"{len(findings['homoglyph_runs'])} 个同形字词")
    return "posting_guard: ⚠ " + " / ".join(parts) + "，请人工过目后再用"


def print_findings(findings: dict) -> None:
    print("--- findings ---")
    for key, label in [
        ("hidden_blocks", "hidden_blocks（已删除，摘录）"),
        ("instruction_like", "instruction_like（只标记，未删除）"),
        ("hidden_urls", "hidden_urls（只在被删内容里出现）"),
        ("homoglyph_runs", "homoglyph_runs（拉丁混西里尔/希腊）"),
    ]:
        items = findings.get(key) or []
        print(f"{label}（{len(items)}）:")
        for item in items:
            print(f"  - {item}")
        if not items:
            print("  （无）")
    print(f"invisible_chars: {findings['invisible_chars']}（已删除）")
    print(f"invisible_flagged: {len(findings['invisible_flagged'])}")
    for item in findings["invisible_flagged"]:
        print(f"  - {item}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="投递文本完整性门：清洗隐藏内容、标记可疑指令（不改写原文语义）"
    )
    parser.add_argument("--in", dest="input", help="输入文件（省略则读标准输入）")
    parser.add_argument("--html", action="store_true", help="输入是 HTML（保存的页面）")
    parser.add_argument("--text", action="store_true", help="输入是纯文本（岗位帖 / 群消息 / CLI 输出）")
    parser.add_argument("--out", help="把清洗后的文本写入该文件")
    parser.add_argument("--json", action="store_true", help="findings 以 JSON 输出（不打印正文）")
    parser.add_argument("--strict", action="store_true", help="有 ⚠ 时退出码 1")
    args = parser.parse_args(argv)

    if args.input:
        try:
            raw = Path(args.input).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            print(f"posting_guard: 读取失败 {args.input}: {exc}", file=sys.stderr)
            return 2
    else:
        raw = sys.stdin.read()

    if args.html and args.text:
        print("posting_guard: --html 与 --text 只能二选一", file=sys.stderr)
        return 2
    mode = "html" if args.html else "text"

    result = guard(raw, mode)
    findings, text = result["findings"], result["text"]
    line = verdict_line(findings)

    if args.json:
        payload = {"findings": findings}
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            payload["saved"] = args.out
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(line)
        print()
        print_findings(findings)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"\nsaved={args.out}")
        else:
            print("\n--- text ---")
            print(text, end="")

    if args.strict and findings["verdict"] == "warn":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
