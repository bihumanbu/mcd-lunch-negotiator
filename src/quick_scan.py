#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速扫描：抓菜单候选 -> 详情 -> 计价，验证「套餐结构 vs 单点」的价差。"""
import json
import time

from mcd_client import McdMcp, fen_to_yuan

STORE = "1420762"
c = McdMcp()

menu = c.call("query-meals", {"storeCode": STORE, "orderType": 1, "beType": 1})
cats = menu.get("data", {}).get("categories", [])
print("分类数:", len(cats))
for cat in cats:
    print("  -", cat["name"].replace("\n", "/"), "| SKU:", len(cat.get("meals", [])))

# 收集候选：优先单人餐/组合类
codes = []
seen = set()
for cat in cats:
    for m in cat.get("meals", []):
        cd = m.get("code")
        if cd and cd not in seen:
            seen.add(cd)
            codes.append((cd, cat["name"], m.get("tags", [])))
print("\n去重候选 SKU:", len(codes))

# 取前 14 个查详情
print("\n=== 详情扫描（前 14）===")
details = {}
for cd, catname, tags in codes[:14]:
    try:
        d = c.call("query-meal-detail", {"storeCode": STORE, "orderType": 1, "beType": 1, "code": cd})
        data = d.get("data") or {}
        rounds = data.get("rounds") or []
        details[cd] = data
        rinfo = " | ".join("%s(%d项)" % (r.get("name"), len(r.get("choices", []))) for r in rounds)
        print("  %-12s %-30s rounds=%d  %s" % (cd, (data.get("name") or "?")[:28], len(rounds), rinfo[:90]))
    except Exception as e:
        print("  %-12s EXC %s" % (cd, str(e)[:80]))
    time.sleep(0.05)

# 批量计价
print("\n=== 计价（真实） ===")
batch = []
for cd, catname, tags in codes[:14]:
    if cd in details:
        batch.append(cd)

try:
    r = c.call("calculate-price", {
        "storeCode": STORE, "orderType": 1, "beType": 1,
        "items": [{"productCode": x, "quantity": 1} for x in batch],
        "needTableware": False,
    })
    data = r.get("data", {})
    print("总价: ¥%.2f  （原始 ¥%.2f）" % (fen_to_yuan(data.get("price")) or 0, fen_to_yuan(data.get("originalPrice")) or 0))
    for p in data.get("productList", []):
        print("  ¥%6.2f  %s x%d" % (fen_to_yuan(p.get("subtotal")) or 0, p.get("productName"), p.get("quantity")))
except Exception as e:
    print("计价异常:", str(e)[:300])

json.dump({"store": STORE, "categories": [x["name"] for x in cats],
           "codes": codes[:60], "details": details},
          open("../data_scan.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\n已保存 ../data_scan.json")
