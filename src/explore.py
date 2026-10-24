#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探索：门店 -> 菜单 -> 计价，摸清真实数据结构。"""
import json
import sys

from mcd_client import McdMcp, parse_response

c = McdMcp()

CITY = sys.argv[1] if len(sys.argv) > 1 else "深圳市"
KW = sys.argv[2] if len(sys.argv) > 2 else ""

print("=" * 70)
print("[A] query-nearby-stores  city=%s keyword=%s" % (CITY, KW or "-"))
args = {"beType": 1, "searchType": 2, "city": CITY}
if KW:
    args["keyword"] = KW
stores = c.call("query-nearby-stores", args)
print(json.dumps(stores, ensure_ascii=False)[:1800])

# 尝试解析门店列表
store_list = []
if isinstance(stores, dict):
    d = stores.get("data", stores)
    if isinstance(d, dict):
        for k in ("storeList", "list", "stores", "data"):
            if isinstance(d.get(k), list):
                store_list = d[k]
                break
    elif isinstance(d, list):
        store_list = d

if not store_list:
    print("\n!! 未解析出门店列表，停止。原始返回已打印。")
    sys.exit(0)

print("\n门店数:", len(store_list))
first = store_list[0]
print("首店字段:", list(first.keys()) if isinstance(first, dict) else type(first))
print("首店:", json.dumps(first, ensure_ascii=False)[:500])

store_code = first.get("storeCode") or first.get("storeId") or first.get("code")
print("\n选用 storeCode =", store_code)

print("\n" + "=" * 70)
print("[B] query-meals  orderType=1 beType=1")
meals = c.call("query-meals", {"storeCode": str(store_code), "orderType": 1, "beType": 1})
txt = json.dumps(meals, ensure_ascii=False)
print("返回长度:", len(txt))
print(txt[:2500])

# 菜单结构探测
def dig_products(obj):
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ("categories", "categoryList", "menu", "meals", "productList", "list", "data"):
            v = obj.get(k)
            if isinstance(v, list) and v:
                return v
        for v in obj.values():
            r = dig_products(v)
            if r:
                return r
    return []

cats = dig_products(meals)
print("\n一级列表长度:", len(cats))
if cats:
    print("首项 keys:", list(cats[0].keys()) if isinstance(cats[0], dict) else type(cats[0]))
    print("首项:", json.dumps(cats[0], ensure_ascii=False)[:600])
