#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""录屏前预检：30 秒内确认「按下录制不会翻车」。

用法：
    python src/preflight.py

检查项：
  1. Token 是否可读
  2. MCP 是否真的连得上（initialize + tools/list）
  3. 必交文件是否齐全
  4. sample-plan.md 里的金额是否自洽（算得平）
  5. README 封面图路径是否指向真实文件
  6. 输出录屏窗口顺序

任何一项 FAIL 都不要开始录。
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

OK, BAD, WARN = "  [OK]  ", "  [FAIL]", "  [WARN]"
fails, warns = [], []


def line(status, msg, detail=""):
    print(status + " " + msg + (("  → " + detail) if detail else ""))


def check_token():
    print("\n[1/6] Token")
    try:
        from mcd_client import load_token
        tok = load_token()
    except Exception as e:
        fails.append("Token 读取失败")
        line(BAD, "读不到 Token", str(e)[:80])
        return None
    if not tok:
        fails.append("Token 为空")
        line(BAD, "Token 为空")
        return None
    line(OK, "Token 已读取", "%d 位，前 4 位 %s****" % (len(tok), tok[:4]))
    return tok


def check_mcp(tok):
    print("\n[2/6] MCP 连通性")
    if not tok:
        line(WARN, "跳过（无 Token）")
        return
    try:
        from mcd_client import McdMcp
        c = McdMcp(token=tok, retries=1)
        c.initialize()
        r = c.rpc("tools/list", {})
        n = len((r.get("result") or {}).get("tools") or [])
        if n >= 30:
            line(OK, "MCP 已连接", "tools/list 返回 %d 个工具" % n)
        else:
            fails.append("工具数异常")
            line(BAD, "工具数异常", "只返回 %d 个" % n)
    except Exception as e:
        fails.append("MCP 不通")
        line(BAD, "MCP 连接失败", str(e)[:110])
        line("", "     网络抖动很常见，重跑一次 preflight 即可；仍失败再录")


def check_files():
    print("\n[3/6] 必交文件")
    need = ["README.md", "CONTEST_DECLARATION.md", "MCP_INTEGRATION.md",
            "workbuddy.md", "src/negotiator.py", "src/mcd_client.py",
            "docs/sample-plan.md", "docs/demo-script.md", "docs/hero.png"]
    for f in need:
        p = os.path.join(BASE, f)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            line(OK, "%-26s %7d B" % (f, os.path.getsize(p)))
        else:
            fails.append("缺文件 " + f)
            line(BAD, "%-26s 缺失或为空" % f)


def check_numbers():
    print("\n[4/6] sample-plan.md 金额自洽")
    p = os.path.join(BASE, "docs", "sample-plan.md")
    if not os.path.isfile(p):
        line(WARN, "跳过（无 sample-plan.md）")
        return
    t = open(p, encoding="utf-8").read()

    def num(pat):
        m = re.search(pat, t)
        return float(m.group(1)) if m else None

    single = num(r"逐个单点买\s*\|\s*¥([\d.]+)")
    combo = num(r"组合）\s*\|\s*\**¥([\d.]+)")
    withc = num(r"实付合计\s*¥([\d.]+)")
    save_c = num(r"再省\s*¥([\d.]+)")

    if None in (single, combo, withc):
        fails.append("sample-plan 金额解析失败")
        line(BAD, "解析不出关键金额，别录", "检查 sample-plan.md 是否被改坏")
        return

    pairs = [
        ("单点 − 套餐 = 省", single - combo, num(r"同内容省\s*¥([\d.]+)")),
        ("套餐 − 券 = 实付", combo - withc, save_c),
        ("总省 = 单点 − 实付", single - withc, None),
    ]
    for label, calc, expect in pairs:
        if expect is None:
            line(OK, "%-20s ¥%.2f" % (label, calc))
        elif abs(calc - expect) <= 0.02:
            line(OK, "%-20s ¥%.2f" % (label, calc))
        else:
            fails.append("金额对不上：" + label)
            line(BAD, "%s 算得 ¥%.2f，文档写 ¥%.2f" % (label, calc, expect))

    # 每人合计是否等于套餐合计
    rows = re.findall(r"\|\s*[^|]+\s*\|\s*¥\d+\s*\|\s*[^|]+\|\s*¥([\d.]+)\s*\|", t)
    if rows:
        s = round(sum(float(x) for x in rows), 2)
        if abs(s - combo) <= 0.02:
            line(OK, "%-20s ¥%.2f" % ("每人合计之和", s))
        else:
            fails.append("每人金额之和 ≠ 合计")
            line(BAD, "每人之和 ¥%.2f ≠ 合计 ¥%.2f" % (s, combo))


def check_readme_image():
    print("\n[5/6] README 封面")
    p = os.path.join(BASE, "README.md")
    t = open(p, encoding="utf-8").read()
    m = re.search(r"!\[[^\]]*\]\((\.?/?docs/[^)]+)\)", t)
    if not m:
        warns.append("README 未引用封面图")
        line(WARN, "README 里没找到封面图引用")
        return
    rel = m.group(1).lstrip("./")
    fp = os.path.join(BASE, rel)
    if os.path.isfile(fp):
        line(OK, "封面已引用且文件存在", "%s（%d KB）" % (rel, os.path.getsize(fp) // 1024))
    else:
        fails.append("封面图路径失效")
        line(BAD, "封面图路径失效", rel)


def show_order():
    print("\n[6/6] 录屏窗口顺序（照这个顺序摆，切换不找）")
    for i, s in enumerate([
        "GitHub 仓库首页（首屏那张封面图，停 3 秒）",
        "WorkBuddy 自动化列表（11:25 那条定时，停 5 秒）",
        "docs/sample-plan.md（全程主画面，别重跑）",
        "终端（只跑 tools/list 或热启动，别清 data/）",
    ], 1):
        print("      %d. %s" % (i, s))


def main():
    print("=" * 62)
    print(" 录屏前预检 · 拼单谈判官")
    print("=" * 62)
    tok = check_token()
    check_mcp(tok)
    check_files()
    check_numbers()
    check_readme_image()
    show_order()

    print("\n" + "=" * 62)
    if fails:
        print(" 结论：先别录，有 %d 项要修" % len(fails))
        for f in fails:
            print("   ✗ " + f)
        print("=" * 62)
        return 1
    if warns:
        print(" 结论：可以录（%d 项提醒）" % len(warns))
        for w in warns:
            print("   ! " + w)
    else:
        print(" 结论：全部通过，可以录")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
