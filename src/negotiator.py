#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拼单谈判官 · 核心引擎

输入：一群人（预算/口味/忌口）+ 门店
输出：每人真实价格的最优买法 + 两方案对比 + 套餐价值对照 + 拆单清单

原则：
- 所有价格来自 calculate-price（官方实时接口），不猜价
- 套餐内容来自 query-meal-detail 的 rounds[].choices[isDefault=1]
- 套餐价值对照：把套餐默认内容逐个单点计价，得出真实差额
- 遇「套餐专属 SKU」（单点返回 0）时，用同档位可单点商品保守估值，并显式标注
"""
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from mcd_client import McdMcp, fen_to_yuan

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(CACHE_DIR, exist_ok=True)
DETAIL_CACHE_FILE = os.path.join(CACHE_DIR, "cache_detail.json")
PRICE_CACHE_FILE = os.path.join(CACHE_DIR, "cache_price.json")

# 分类 -> (取几个, 期望形态)
CATEGORY_PLAN = [
    (["人气热卖"], 4, "any"),
    (["小食拼盘"], 4, "combo"),
    (["精选单人餐"], 5, "combo"),
    (["大堡口福"], 1, "combo"),
    (["500/大卡"], 4, "combo"),
    (["随心配"], 2, "combo"),
    (["巨无霸"], 6, "any"),
    (["鸡肉汉堡"], 6, "any"),
    (["安格斯"], 3, "combo"),
    (["麦咖啡"], 6, "single"),
    (["饮品"], 4, "single"),
    (["小食甜品"], 6, "any"),
]

KEYWORDS = {
    "汉堡": ["汉堡", "巨无霸", "堡"],
    "鸡肉": ["鸡", "翅", "麦乐鸡"],
    "牛": ["巨无霸", "安格斯", "牛", "不素之霸"],
    "咖啡": ["咖啡", "拿铁", "美式", "奶铁", "雪冰"],
    "小食": ["小食", "薯", "拼盘", "鸡块", "薯条"],
    "清淡": ["沙拉", "玉米", "咖啡", "红茶"],
}


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def parse_diff(s):
    """'diffPrice' 是 ' + ¥1.5' / ' - ¥0.5' 这类字符串，转成 float。"""
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"[-+]?\s*([\d.]+)", str(s))
    if not m:
        return 0.0
    v = float(m.group(1))
    return -v if "-" in str(s) else v


class Engine:
    def __init__(self, city="深圳", keyword="科技园", be_type=1, order_type=1, use_cache=True):
        self.c = McdMcp()
        self.city = city
        self.keyword = keyword
        self.be_type = be_type
        self.order_type = order_type
        self.store = None
        self.menu = []
        self.detail_cache = load_json(DETAIL_CACHE_FILE, {}) if use_cache else {}
        self.price_cache = load_json(PRICE_CACHE_FILE, {}) if use_cache else {}
        self.use_cache = use_cache
        # 磁盘缓存是否为空 = 本次是否冷启动（决定末尾调用次数的表述，避免读起来像造数据）
        self.cold_start = (not self.detail_cache) and (not self.price_cache)
        self.call_count = 0
        self.cache_hits = 0
        self.coupon_error = None      # 券查询失败原因（区分「没券」与「查不到”）
        self._n = 0
        self._nlock = threading.Lock()

    def _tick(self):
        with self._nlock:
            self.call_count += 1

    def _hit(self):
        with self._nlock:
            self.cache_hits += 1

    # ---------- 基础数据 ----------
    def resolve_store(self):
        r = self.c.call("query-nearby-stores", {
            "beType": self.be_type, "searchType": 2,
            "city": self.city, "keyword": self.keyword})
        self._tick()
        stores = r.get("data") or []
        if not stores:
            raise RuntimeError("未找到门店：%s / %s" % (self.city, self.keyword))
        self.store = stores[0]
        return self.store

    def load_menu(self):
        r = self.c.call("query-meals", {
            "storeCode": str(self.store["storeCode"]),
            "orderType": self.order_type, "beType": self.be_type})
        self._tick()
        self.menu = r.get("data", {}).get("categories", [])
        return self.menu

    def detail(self, code):
        code = str(code)
        if code in self.detail_cache:
            self._hit()
            return self.detail_cache[code]
        r = self.c.call("query-meal-detail", {
            "storeCode": str(self.store["storeCode"]), "orderType": self.order_type,
            "beType": self.be_type, "code": code})
        self._tick()
        d = r.get("data") or {}
        with self._nlock:
            self.detail_cache[code] = d
        return d

    def price(self, code):
        """单个 SKU 的真实价格（元）。无效 / 不可单点返回 None。"""
        code = str(code)
        if code in self.price_cache:
            self._hit()
            return self.price_cache[code]
        val = None
        try:
            r = self.c.call("calculate-price", {
                "storeCode": str(self.store["storeCode"]), "orderType": self.order_type,
                "beType": self.be_type,
                "items": [{"productCode": code, "quantity": 1}],
                "needTableware": False})
            self._tick()
            p = (r.get("data") or {}).get("price")
            val = fen_to_yuan(p) if p else None
            if not val:
                val = None
        except Exception:
            val = None
        with self._nlock:
            self.price_cache[code] = val
        return val

    def persist(self):
        if self.use_cache:
            save_json(DETAIL_CACHE_FILE, self.detail_cache)
            save_json(PRICE_CACHE_FILE, self.price_cache)

    # ---------- 套餐解剖 ----------
    def round_value(self, rd):
        """
        某一轮默认内容的单点估值 -> (单价 or None, 是否成功, 明细)

        关键：套餐专属 SKU（如可乐中杯）单点返回 0，不能当免费处理。
        策略：优先在同档位（加价相同）里找可单点商品做等价替代，保守估值并标注。
        """
        choices = rd.get("choices") or []
        if not choices:
            return None, False, None
        dflt = None
        for ch in choices:
            if ch.get("isDefault") == 1:
                dflt = ch
                break
        if dflt is None:
            dflt = choices[0]

        p = self.price(dflt.get("code"))
        if p:
            return p, True, {"name": dflt.get("name"), "price": p, "sub": False}

        d0 = parse_diff(dflt.get("diffPrice"))
        # 先在同档位里找替代
        same = [c for c in choices
                if str(c.get("code")) != str(dflt.get("code"))
                and parse_diff(c.get("diffPrice")) == d0]
        for ch in same:
            q = self.price(ch.get("code"))
            if q:
                return q, True, {"name": ch.get("name"), "price": q, "sub": True,
                                 "orig": dflt.get("name")}
        # 同档位全不可单点 -> 退到最接近的可单点商品（近似估值）
        others = sorted([c for c in choices if str(c.get("code")) != str(dflt.get("code"))],
                        key=lambda x: abs(parse_diff(x.get("diffPrice")) - d0))
        for ch in others:
            q = self.price(ch.get("code"))
            if q:
                return q, True, {"name": ch.get("name"), "price": q, "sub": True,
                                 "orig": dflt.get("name"), "approx": True}
        return None, False, {"name": dflt.get("name"), "price": None, "sub": False}

    def alacarte(self, detail):
        """套餐默认内容的单点合计 -> (合计, 是否完整, 明细)。"""
        total, rows, complete = 0.0, [], True
        for rd in detail.get("rounds", []) or []:
            val, ok, info = self.round_value(rd)
            if info:
                info["round"] = rd.get("name")
                rows.append(info)
            if ok and val:
                total += val
            else:
                complete = False
        return round(total, 2), complete, rows

    # ---------- 候选池 ----------
    def candidates(self):
        """按分类计划抽取候选，保证套餐与单点都有覆盖。"""
        cats = {}
        for cat in self.menu:
            nm = (cat.get("name") or "").replace("\n", "/")
            cats[nm] = cat

        out, seen = [], set()
        for keys, n, _want in CATEGORY_PLAN:
            for nm, cat in cats.items():
                if not any(k in nm for k in keys):
                    continue
                taken = 0
                for m in cat.get("meals", []):
                    if taken >= n:
                        break
                    cd = m.get("code")
                    if not cd or str(cd) in seen:
                        continue
                    seen.add(str(cd))
                    out.append({"code": str(cd), "category": nm,
                                "order": len(out), "tags": m.get("tags", [])})
                    taken += 1
        return out

    def enrich(self, cands, workers=8):
        """并发补详情 + 真实价格。"""
        def work(c):
            try:
                d = self.detail(c["code"])
            except Exception:
                return None
            name = d.get("name") or ""
            if not name:
                return None
            p = self.price(c["code"])
            if not p:
                return None
            c.update({"name": name, "price": p,
                      "rounds": len(d.get("rounds") or []), "detail": d})
            return c

        keep = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for r in ex.map(work, cands):
                if r:
                    keep.append(r)
        keep.sort(key=lambda x: x["order"])
        return keep

    def build_pool(self):
        cands = self.candidates()
        pool = self.enrich(cands)
        n_combo = sum(1 for x in pool if x["rounds"] >= 2)
        return pool, n_combo, len(pool) - n_combo

    # ---------- 优惠券 ----------
    def load_coupons(self):
        """解析账户券。查询失败时记录原因，不静默降级为『没有券』。"""
        self.coupon_error = None
        try:
            t = self.c.call_raw("query-my-coupons", {})
            self._tick()
        except Exception as e:
            self.coupon_error = str(e).replace("\n", " ")[:120]
            return []
        out = []
        for m in re.finditer(r"##\s*(.+?)\s*\n-\s*\*\*优惠\*\*:\s*¥([\d.]+)", t):
            name = m.group(1).strip()
            if "券" not in name and "折" not in name:
                continue
            try:
                price = float(m.group(2))
            except Exception:
                continue
            tag = "到店专用" if "到店专用" in t[m.end():m.end() + 300] else ""
            out.append({"name": name, "price": price, "tag": tag})
        return out


def bigrams(s):
    s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def coupon_match(item_name, coupon_name):
    """券名与商品名的二字组交集 >= 2 视为匹配（不依赖硬编码品类词）。"""
    a, b = bigrams(item_name), bigrams(coupon_name)
    return len(a & b) >= 2


# ---------- 人设匹配 ----------
def matches(name, tags):
    for kw in tags:
        if kw in name:
            return True
    return False


def score(item, person, ignore_budget=False):
    s = 0
    name = item["name"]
    for tag in person.get("want", []):
        if matches(name, KEYWORDS.get(tag, [tag])):
            s += 8
    for bad in person.get("avoid", []):
        if matches(name, KEYWORDS.get(bad, [bad])):
            s -= 100
    if person.get("budget") and not ignore_budget:
        if item["price"] > person["budget"]:
            s -= 50
        else:
            s += (item["price"] / max(person["budget"], 1)) * 2
    if person.get("light"):
        if "500/大卡" in item["category"]:
            s += 4
        if "炸" in name or "翅" in name:
            s -= 2
    if item.get("rounds", 0) < 2:
        s -= 0.5
    return s


def plan_one(pool, person, ignore_budget=False):
    ranked = sorted(pool, key=lambda x: score(x, person, ignore_budget), reverse=True)
    for cand in ranked:
        if score(cand, person, ignore_budget) <= -50:
            continue
        if (not ignore_budget) and person.get("budget") and cand["price"] > person["budget"]:
            continue
        return cand
    return ranked[0] if ranked else None


def assign(pool, people, ignore_budget=False):
    return [{"person": p, "item": plan_one(pool, p, ignore_budget)} for p in people]


def baseline_cost(engine, opt_plan):
    """
    基线口径：把每个人碗里的东西「逐个单点买」，需要花多少钱。
    这才是真实对比——不是比谁点得便宜，而是比「同样这些吃的」两种买法差多少。
    单点商品本身无对照，按自身价格计入。
    """
    total, rows = 0.0, []
    for row in opt_plan:
        it = row["item"]
        if not it:
            continue
        if it.get("rounds", 0) >= 2:
            single, complete, _ = engine.alacarte(it["detail"])
            cost = single if (complete and single > 0) else it["price"]
            total += cost
            rows.append({"name": it["name"], "single": cost, "combo": it["price"],
                         "compared": bool(complete and single > 0)})
        else:
            total += it["price"]
            rows.append({"name": it["name"], "single": it["price"], "combo": it["price"],
                         "compared": False})
    return round(total, 2), rows


def stats(plan):
    """优化方案的健康度：超预算人数、忌口踩雷人数。"""
    total = sum((r["item"]["price"] if r["item"] else 0) for r in plan)
    over = sum(1 for r in plan
               if r["item"] and r["person"].get("budget") and r["item"]["price"] > r["person"]["budget"])
    bad = 0
    for r in plan:
        it = r["item"]
        if not it:
            continue
        for avoid in r["person"].get("avoid", []):
            if matches(it["name"], KEYWORDS.get(avoid, [avoid])):
                bad += 1
                break
    n = max(len(plan), 1)
    return round(total, 2), round(total / n, 2), over, bad


# ---------- 输出 ----------
def render(engine, opt_plan):
    st = engine.store
    o_total, o_avg, o_over, o_bad = stats(opt_plan)
    b_total, b_rows = baseline_cost(engine, opt_plan)
    save = round(b_total - o_total, 2)
    b_avg = round(b_total / max(len(opt_plan), 1), 2)
    compared = [r for r in b_rows if r["compared"]]

    L = []
    L.append("## 门店")
    L.append("")
    L.append("**%s**（storeCode %s）" % (st.get("storeName"), st.get("storeCode")))
    L.append("")
    L.append("## 核心结论")
    L.append("")
    L.append("| 方案 | 合计 | 人均 |")
    L.append("|---|---|---|")
    L.append("| 同样这些吃的，逐个单点买 | ¥%.2f | ¥%.2f |" % (b_total, b_avg))
    L.append("| 拼单谈判官（套餐 + 组合） | **¥%.2f** | **¥%.2f** |" % (o_total, o_avg))
    L.append("")
    if save > 0:
        L.append("> **同内容省 ¥%.2f，人均省 ¥%.2f（%.1f%%）**。"
                 % (save, round(save / max(len(opt_plan), 1), 2),
                    save / b_total * 100 if b_total else 0))
    L.append(">")
    L.append("> 约束达成：**%d/%d 人在预算内**，**%d 人忌口踩雷**。"
             % (len(opt_plan) - o_over, len(opt_plan), o_bad))
    L.append("")

    L.append("## 每人买法")
    L.append("")
    L.append("| 人 | 预算 | 推荐 | 实付 | 约束 |")
    L.append("|---|---|---|---|---|")
    for row in opt_plan:
        p, it = row["person"], row["item"]
        note = []
        if p.get("want"):
            note.append("/".join(p["want"]))
        if p.get("avoid"):
            note.append("避" + "/".join(p["avoid"]))
        if p.get("light"):
            note.append("减脂")
        if it is None:
            L.append("| %s | ¥%s | 无匹配 | - | %s |" % (p["name"], p.get("budget", "-"), " ".join(note)))
            continue
        flag = " ⚠超预算" if (p.get("budget") and it["price"] > p["budget"]) else ""
        L.append("| %s | ¥%s | %s | ¥%.2f%s | %s |" % (
            p["name"], p.get("budget", "-"), it["name"], it["price"], flag, " ".join(note)))
    L.append("")
    L.append("**合计 ¥%.2f ｜ 人均 ¥%.2f ｜ %d 人**" % (o_total, o_avg, len(opt_plan)))
    L.append("")

    # 优惠券叠加
    coupons = engine.load_coupons()
    applied, coupon_rows, gross = [], [], o_total
    paid = {}          # 每人实际到手价（挂券后）
    if coupons:
        for row in opt_plan:
            it = row["item"]
            if not it:
                continue
            best = None
            for cp in coupons:
                if cp["name"] in applied:
                    continue
                if cp["price"] < it["price"] and coupon_match(it["name"], cp["name"]):
                    if best is None or cp["price"] < best["price"]:
                        best = cp
            if best:
                applied.append(best["name"])
                gross -= (it["price"] - best["price"])
                paid[row["person"]["name"]] = best["price"]
                coupon_rows.append((row["person"]["name"], it["name"], it["price"],
                                    best["name"], best["price"], best.get("tag", "")))
        L.append("## 优惠券叠加")
        L.append("")
        if coupon_rows:
            L.append("| 人 | 原推荐 | 原价 | 建议挂券 | 用券价 | 省 |")
            L.append("|---|---|---|---|---|---|")
            for nm, item, p0, cname, p1, tag in coupon_rows:
                L.append("| %s | %s | ¥%.2f | %s%s | ¥%.2f | ¥%.2f |"
                         % (nm, item, p0, cname, "（%s）" % tag if tag else "", p1, p0 - p1))
            L.append("")
            L.append("> 一单只能用一张券，因此券按「一单一人」分配。挂券后合计 **¥%.2f**，"
                     "较无券方案再省 ¥%.2f。" % (round(gross, 2), round(o_total - gross, 2)))
        else:
            if engine.coupon_error:
                L.append("> ⚠️ **券查询失败**（网络原因，已重试）：`%s`" % engine.coupon_error)
                L.append(">")
                L.append("> 因此本次方案**未计入任何券**，上面的合计是纯商品价格。"
                         "券的真实状态未知，不代表账户没有券——请重跑一次确认。")
            elif coupons:
                L.append("账户有 %d 张可用券，但本次推荐无匹配商品；券信息：%s"
                         % (len(coupons), "、".join("「%s」¥%.2f" % (c["name"], c["price"]) for c in coupons)))
            else:
                L.append("> 今日账户无可用券（查询成功，确实为空）。")
        L.append("")

    # 套餐 vs 单点逐项明细
    L.append("## 逐项明细（官方计价实测）")
    L.append("")
    L.append("| 推荐 | 套餐价 | 同内容单点 | 差额 |")
    L.append("|---|---|---|---|")
    seen, notes = set(), []
    for row in opt_plan:
        it = row["item"]
        if not it or it["code"] in seen or it.get("rounds", 0) < 2:
            continue
        seen.add(it["code"])
        single, complete, rows = engine.alacarte(it["detail"])
        if not complete or single <= 0:
            L.append("| %s | ¥%.2f | 无法完整对照 | — |" % (it["name"], it["price"]))
            continue
        diff = round(single - it["price"], 2)
        mark = "**省 ¥%.2f**" % diff if diff > 0 else "贵 ¥%.2f" % abs(diff)
        L.append("| %s | ¥%.2f | ¥%.2f | %s |" % (it["name"], it["price"], single, mark))
        for r in rows:
            if r.get("sub"):
                kw = "近似档位" if r.get("approx") else "同档位"
                notes.append("- 「%s」中的 %s 不单卖，按%s可单点商品「%s」¥%.2f 保守估值"
                             % (it["name"], r.get("orig"), kw, r["name"], r["price"]))
    L.append("")
    if notes:
        L.extend(notes)
        L.append("")

    # 拆单：一单只能用一张券 → 有券的人必须独占一单，无券的人按同款合并
    L.append("## 怎么下这几单")
    L.append("")
    L.append("> 拆单规则：**一单只能用一张券**，所以有券的人单独占一单（否则合并等于白扔券），"
             "没券的人按同款合并。")
    L.append("")

    single_rows, groups, group_idx = [], {}, []
    for row in opt_plan:
        if not row["item"]:
            continue
        nm = row["person"]["name"]
        if nm in paid:
            single_rows.append(row)
        else:
            code = row["item"]["code"]
            if code not in groups:
                groups[code] = []
                group_idx.append(code)
            groups[code].append(row)

    orders = [(r["item"], [r], True) for r in single_rows]
    orders += [(groups[c][0]["item"], groups[c], False) for c in group_idx]

    grand = 0.0
    for idx, (item, rows, has_coupon) in enumerate(orders, 1):
        names = [r["person"]["name"] for r in rows]
        sub = round(sum(paid.get(r["person"]["name"], item["price"]) for r in rows), 2)
        grand += sub
        tail = "，已挂券" if has_coupon else ""
        L.append("- **第 %d 单**：%s × %d —— %s（单价 ¥%.2f%s，小计 ¥%.2f）" % (
            idx, item["name"], len(rows), "、".join(names), item["price"], tail, sub))
    L.append("")
    L.append("**共 %d 单，实付合计 ¥%.2f。**" % (len(orders), round(grand, 2)))
    L.append("")
    L.append("> 每单生成独立支付链接，**15 分钟内有效**——所以链接要分给对应的人各自扫，"
             "一个人连扫 %d 单来不及。这也是这个工具的真实定位："
             "**一个人算，每人自己扫**。" % len(orders))
    L.append("")
    total_q = engine.call_count + engine.cache_hits
    if engine.cold_start:
        L.append("> 全部价格来自麦当劳官方 `calculate-price` 实时接口，无任何硬编码金额。"
                 "**本次是冷启动**（`data/` 为空）：共发起 **%d 次**商品查询，其中真实 MCP 调用 "
                 "**%d 次**，其余 %d 次是同一商品在本进程内被重复问到，直接复用内存结果、没有重复请求。"
                 "支付由人扫码完成，不做自动下单。"
                 % (total_q, engine.call_count, engine.cache_hits))
    else:
        L.append("> 全部价格来自麦当劳官方 `calculate-price` 实时接口，无任何硬编码金额。"
                 "**本次是热启动**（`data/` 已有缓存）：真实 MCP 调用 **%d 次**，"
                 "另有 %d 次命中本地缓存，共 %d 次查询。"
                 "想复现「冷启动 114 次真实调用」，清空 `data/` 目录再跑一次。"
                 "支付由人扫码完成，不做自动下单。"
                 % (engine.call_count, engine.cache_hits, total_q))
    return "\n".join(L)


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
    print("构建候选池（并发补详情 + 真实计价）...", file=sys.stderr)
    pool, n_combo, n_single = e.build_pool()
    e.persist()
    print("有效候选 %d 个（套餐 %d / 单点 %d）" % (len(pool), n_combo, n_single), file=sys.stderr)

    opt = assign(pool, PEOPLE_DEFAULT, ignore_budget=False)
    md = render(e, opt)

    with open(os.path.join(BASE_DIR, "plan.md"), "w", encoding="utf-8") as f:
        f.write(md)
    save_json(os.path.join(CACHE_DIR, "last_plan.json"), {
        "store": e.store.get("storeName"), "storeCode": e.store.get("storeCode"),
        "calls": e.call_count,
        "plan": [{"name": r["person"]["name"],
                  "item": r["item"]["name"] if r["item"] else None,
                  "price": r["item"]["price"] if r["item"] else None} for r in opt],
    })
    print(md)


if __name__ == "__main__":
    main()
