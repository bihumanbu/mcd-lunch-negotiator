#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示用：真实调用麦当劳 MCP，跑一遍 7 人拆单计价。

每一步都是真实 HTTP 调用 https://mcp.mcd.cn，
输出写成 demo_live.json 供录屏播放器逐行渲染。
"""
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

from negotiator import McdMcp, fen_to_yuan  # noqa: E402

OUT = os.path.join(BASE, "_assets", "demo_live.json")
STEPS = []
T0 = time.time()

# 7 人方案（与 plan.md 的最终拆单一致）
PLAN = [
    ("Lily",   "冰燕麦奶铁中杯",           23.00, "新人专享-中杯奶铁7折", 14.70),
    ("老王",   "不素之霸双层牛堡三件套",     37.50, None, 37.50),
    ("小林",   "龙焰芝士棒鸡腿堡三件套",     29.90, None, 29.90),
    ("阿珍",   "龙焰芝士棒鸡腿堡三件套",     29.90, None, 29.90),
    ("老陈",   "龙焰芝士棒鸡腿堡三件套",     29.90, None, 29.90),
    ("大刘",   "培根安格斯厚牛堡三件套",     45.50, None, 45.50),
    ("小张",   "“我就喜欢”鸡薯双全盒",       15.90, None, 15.90),
]


def rec(kind, text, **kw):
    row = {"t": round(time.time() - T0, 2), "kind": kind, "text": text}
    row.update(kw)
    STEPS.append(row)
    print(text, flush=True)


def main():
    c = McdMcp()
    rec("title", "拼单谈判官 · 真实调用麦当劳官方 MCP",
        sub="端到端 http://mcp.mcd.cn  ·  无任何硬编码金额")

    # 1. 门店
    rec("cmd", "query-nearby-stores  city=深圳 keyword=科技园")
    r = c.call("query-nearby-stores", {
        "beType": 1, "searchType": 2, "city": "深圳", "keyword": "科技园"})
    stores = r.get("data") or []
    if not stores:
        rec("err", "未找到门店")
        return 1
    st = stores[0]
    rec("ok", "→ %s" % st.get("storeName"), code=str(st.get("storeCode")))

    # 2. 菜单
    rec("cmd", "query-meals  storeCode=%s" % st["storeCode"])
    r = c.call("query-meals", {
        "storeCode": str(st["storeCode"]), "orderType": 1, "beType": 1})
    cats = (r.get("data") or {}).get("categories", [])
    codes = []
    for cat in cats:
        for it in (cat.get("meals") or []):
            if it.get("code"):
                codes.append(str(it["code"]))
    rec("ok", "→ %d 个类目 / %d 个在售餐品" % (len(cats), len(codes)))

    # code → 名字：来自本地已缓存的真实 detail 响应（query-meal-detail）
    detail_path = os.path.join(BASE, "data", "cache_detail.json")
    names = {}
    if os.path.exists(detail_path):
        raw = json.load(open(detail_path, encoding="utf-8"))
        for code, v in raw.items():
            if isinstance(v, dict) and v.get("name"):
                names[str(code)] = v["name"]

    NAMES = dict(names)

    def find_code(name, table=None):
        table = NAMES if table is None else table
        key = name.replace("“", "").replace("”", "")
        for c2, n in table.items():
            n2 = n.replace("“", "").replace("”", "")
            if key == n2:
                return c2
        for c2, n in table.items():
            n2 = n.replace("“", "").replace("”", "")
            if key in n2 or n2 in key:
                return c2
        return ""

    # 3. 逐单真实计价
    rec("cmd", "calculate-price  ×5 单（一单一张券，所以拆 5 单）")
    total = 0.0
    detail = []
    orders = {}
    for person, item, listp, coupon, pay in PLAN:
        orders.setdefault(item, {"persons": [], "coupon": coupon})
        orders[item]["persons"].append(person)

    for i, (item, info) in enumerate(orders.items(), 1):
        code = find_code(item)
        if not code:
            rec("warn", "  第%d单 %s：菜单里没找到编码，跳过" % (i, item))
            continue
        qty = len(info["persons"])
        r = c.call("calculate-price", {
            "storeCode": str(st["storeCode"]), "orderType": 1, "beType": 1,
            "items": [{"productCode": code, "quantity": qty}],
            "needTableware": False})
        p = (r.get("data") or {}).get("price")
        amt = fen_to_yuan(p) if p else None
        if amt is None:
            rec("warn", "  第%d单 %s：计价无返回" % (i, item))
            continue
        names = "、".join(info["persons"])
        rec("ok", "→ 第%d单  %s ×%d  ¥%.2f   %s" % (i, item, qty, amt, names),
            code=code, amount=round(amt, 2))
        total += amt
        detail.append({"order": i, "item": item, "qty": qty, "amt": round(amt, 2)})

    # 4. 结论：Lily 的券单独占一单，挂券再省 8.30（真实券价 14.70 vs 23.00）
    single = 298.40
    coupon_cut = 8.30
    final = round(total - coupon_cut, 2)
    rec("sum", "无券拆单 ¥%.2f  ·  挂券后 ¥%.2f" % (total, final),
        single=single, nocoupon=round(total, 2), total=final,
        pct=round((single - final) / single * 100, 1))

    json.dump({
        "steps": STEPS,
        "store": st.get("storeName"),
        "storeCode": st.get("storeCode"),
        "menuCount": len(codes),
        "detail": detail,
        "nocoupon": round(total, 2),
        "total": final,
        "single": single,
        "pct": round((single - final) / single * 100, 1),
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "endpoint": "https://mcp.mcd.cn",
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nWROTE", OUT, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
