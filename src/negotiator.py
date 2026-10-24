#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拼单谈判官 · 核心引擎

输入：一群人（预算/口味/忌口）+ 门店
输出：每人一个真实价格的最优买法 + 拆单方案 + 单点对照组

原则：
- 所有价格来自 calculate-price（官方实时接口），不猜价
- 套餐内容来自 query-meal-detail 的 rounds[].choices[isDefault=1]
- 套餐价值对照：把套餐默认内容逐个单点计价，得出「买套餐 vs 单点」的真实差额
"""
import json
import os
import sys
import time

from mcd_client import McdMcp, fen_to_yuan

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(CACHE_DIR, exist_ok=True)


class Engine:
    def __init__(self, city="深圳", keyword="科技园", be_type=1, order_type=1):
        self.c = McdMcp()
        self.city = city
        self.keyword = keyword
        self.be_type = be_type
        self.order_type = order_type
        self.store = None
        self.menu = None
        self.detail_cache = {}
        self.price_cache = {}
        self.call_count = 0

    # ---------- 基础数据 ----------
    def resolve_store(self):
        r = self.c.call("query-nearby-stores", {
            "beType": self.be_type, "searchType": 2,
            "city": self.city, "keyword": self.keyword})
        self.call_count += 1
        stores = r.get("data") or []
        if not stores:
            raise RuntimeError("未找到门店：%s / %s" % (self.city, self.keyword))
        self.store = stores[0]
        return self.store

    def load_menu(self):
        r = self.c.call("query-meals", {
            "storeCode": str(self.store["storeCode"]),
            "orderType": self.order_type, "beType": self.be_type})
        self.call_count += 1
        self.menu = r.get("data", {}).get("categories", [])
        return self.menu

    def detail(self, code):
        if code in self.detail_cache:
            return self.detail_cache[code]
        r = self.c.call("query-meal-detail", {
            "storeCode": str(self.store["storeCode"]), "orderType": self.order_type,
            "beType": self.be_type, "code": str(code)})
        self.call_count += 1
        d = r.get("data") or {}
        self.detail_cache[code] = d
        return d

    def price(self, code):
        """单个 SKU 的真实价格（元）。缓存 + 无效 SKU 返回 None。"""
        if code in self.price_cache:
            return self.price_cache[code]
        try:
            r = self.c.call("calculate-price", {
                "storeCode": str(self.store["storeCode"]), "orderType": self.order_type,
                "beType": self.be_type,
                "items": [{"productCode": str(code), "quantity": 1}],
                "needTableware": False})
            self.call_count += 1
            d = r.get("data") or {}
            p = d.get("price")
            val = fen_to_yuan(p) if p is not None else None
            if val == 0:
                val = None
        except Exception:
            val = None
        self.price_cache[code] = val
        return val

    # ---------- 套餐解剖 ----------
    def default_content(self, detail):
        """取套餐默认内容：每个 round 里 isDefault=1 的选项。"""
        out = []
        for rd in detail.get("rounds", []):
            picked = None
            for ch in rd.get("choices", []):
                if ch.get("isDefault") == 1:
                    picked = ch
                    break
            if not picked and rd.get("choices"):
                picked = rd["choices"][0]
            if picked:
                out.append({"round": rd.get("name"), "code": picked.get("code"),
                            "name": picked.get("name"), "diff": picked.get("diffPrice")})
        return out

    def a_la_carte_total(self, content):
        """把套餐内容逐个单点计价求和 -> (合计, 明细)。用于计算套餐省多少。"""
        total = 0.0
        rows = []
        for item in content:
            p = self.price(item["code"])
            if p:
                total += p
                rows.append((item["name"], p))
        return round(total, 2), rows

    # ---------- 选品 ----------
    def candidates(self, keyword_filter=None, max_price=None, limit=60):
        """从菜单收集候选 SKU（去重），可按分类关键词过滤。"""
        out = []
        seen = set()
        for cat in self.menu:
            cname = (cat.get("name") or "").replace("\n", "/")
            if keyword_filter and not any(k in cname for k in keyword_filter):
                continue
            for m in cat.get("meals", []):
                cd = m.get("code")
                if cd and cd not in seen:
                    seen.add(cd)
                    out.append({"code": cd, "category": cname, "tags": m.get("tags", [])})
        return out[:limit]

    def enrich(self, cands, only_combo=True):
        """补详情与价格，过滤掉非套餐 / 无效价格。"""
        keep = []
        for c in cands:
            try:
                d = self.detail(c["code"])
            except Exception:
                continue
            rounds = d.get("rounds") or []
            if only_combo and len(rounds) < 2:
                continue
            name = d.get("name") or ""
            p = self.price(c["code"])
            if not p:
                continue
            c.update({"name": name, "price": p, "rounds": len(rounds), "detail": d})
            keep.append(c)
        return keep


# ---------- 人设匹配 ----------
KEYWORDS = {
    "汉堡": ["汉堡", "巨无霸", "堡"],
    "鸡肉": ["鸡", "翅", "麦乐鸡"],
    "牛": ["巨无霸", "安格斯", "牛"],
    "咖啡": ["咖啡", "拿铁", "美式", "奶铁"],
    "小食": ["小食", "薯", "拼盘"],
    "清淡": ["沙拉", "玉米", "咖啡"],
}


def score(item, person):
    s = 0
    name = item["name"]
    # 口味匹配
    for tag in person.get("want", []):
        for kw in KEYWORDS.get(tag, [tag]):
            if kw in name:
                s += 3
                break
    # 忌口
    for bad in person.get("avoid", []):
        for kw in KEYWORDS.get(bad, [bad]):
            if kw in name:
                s -= 100
    # 预算：不超预算 + 尽量贴近（满足感）
    if person.get("budget"):
        if item["price"] > person["budget"]:
            s -= 50
        else:
            s += (item["price"] / max(person["budget"], 1)) * 2
    # 减脂：优先低卡分类
    if person.get("light"):
        if "500/大卡" in item["category"]:
            s += 4
        if "炸" in name or "翅" in name:
            s -= 2
    return s


def assign(engine, cands, people):
    plan = []
    used = {}
    for p in people:
        ranked = sorted(cands, key=lambda x: score(x, p), reverse=True)
        pick = None
        for cand in ranked:
            if score(cand, p) <= -50:
                break
            if p.get("budget") and cand["price"] > p["budget"]:
                continue
            pick = cand
            break
        if not pick:
            pick = ranked[0]
        used[pick["code"]] = used.get(pick["code"], 0) + 1
        plan.append({"person": p, "item": pick})
    return plan, used


def render(engine, plan, used):
    store = engine.store
    lines = []
    lines.append("## 门店")
    lines.append("%s（storeCode %s）" % (store.get("storeName"), store.get("storeCode")))
    lines.append("")
    lines.append("## 每人买法")
    lines.append("| 人 | 预算 | 推荐 | 实付 | 备注 |")
    lines.append("|---|---|---|---|---|")
    total = 0.0
    for row in plan:
        p = row["person"]
        it = row["item"]
        total += it["price"]
        note = []
        if p.get("want"):
            note.append("/".join(p["want"]))
        if p.get("avoid"):
            note.append("避" + "/".join(p["avoid"]))
        if p.get("light"):
            note.append("减脂")
        lines.append("| %s | ¥%s | %s | ¥%.2f | %s |" % (
            p["name"], p.get("budget", "-"), it["name"], it["price"], " ".join(note)))
    lines.append("")
    lines.append("**合计 ¥%.2f ｜ 人均 ¥%.2f ｜ 组内 %d 人**" % (
        total, total / max(len(plan), 1), len(plan)))
    lines.append("")

    # 套餐 vs 单点对照
    lines.append("## 买套餐到底省不省（官方计价实测）")
    lines.append("| 推荐 | 套餐价 | 同内容单点 | 差额 |")
    lines.append("|---|---|---|---|")
    seen = set()
    for row in plan:
        it = row["item"]
        if it["code"] in seen:
            continue
        seen.add(it["code"])
        content = engine.default_content(it["detail"])
        single, _ = engine.a_la_carte_total(content)
        if single and single > 0:
            diff = round(single - it["price"], 2)
            mark = "省 ¥%.2f" % diff if diff > 0 else "贵 ¥%.2f" % abs(diff)
            lines.append("| %s | ¥%.2f | ¥%.2f | %s |" % (it["name"], it["price"], single, mark))
    lines.append("")

    # 拆单 / 合并建议
    lines.append("## 怎么下这几单")
    groups = {}
    for row in plan:
        it = row["item"]
        groups.setdefault(it["code"], []).append(row["person"]["name"])
    idx = 1
    for code, names in groups.items():
        item = next(x["item"] for x in plan if x["item"]["code"] == code)
        lines.append("- **第 %d 单**：%s × %d —— %s（单价 ¥%.2f）" % (
            idx, item["name"], len(names), "、".join(names), item["price"]))
        idx += 1
    lines.append("")
    lines.append("> 价格全部来自麦当劳官方 calculate-price 实时接口；共调用 MCP %d 次。" % engine.call_count)
    return "\n".join(lines)


PEOPLE_DEFAULT = [
    {"name": "老王", "budget": 40, "want": ["汉堡"], "avoid": ["鸡肉"]},
    {"name": "小林", "budget": 30, "want": ["鸡肉"], "light": True},
    {"name": "阿珍", "budget": 35, "want": ["汉堡"], "avoid": ["牛"]},
    {"name": "大刘", "budget": 50, "want": ["汉堡"]},
    {"name": "小张", "budget": 25, "want": ["鸡肉"]},
    {"name": "Lily", "budget": 40, "want": ["咖啡"]},
    {"name": "老陈", "budget": 32, "want": []},
]


def main():
    city = sys.argv[1] if len(sys.argv) > 1 else "深圳"
    kw = sys.argv[2] if len(sys.argv) > 2 else "科技园"
    e = Engine(city, kw)
    print("门店解析中...", file=sys.stderr)
    e.resolve_store()
    print("菜单加载中...", file=sys.stderr)
    e.load_menu()
    cands = e.candidates(limit=45)
    print("候选 SKU %d，正在补详情与真实价格..." % len(cands), file=sys.stderr)

    # 只对「套餐」补详情，控制调用量
    combo = e.enrich(cands, only_combo=True)
    print("有效套餐 %d 个" % len(combo), file=sys.stderr)

    plan, used = assign(e, combo, PEOPLE_DEFAULT)
    md = render(e, plan, used)
    out = os.path.join(os.path.dirname(CACHE_DIR), "plan.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(md)


if __name__ == "__main__":
    main()
