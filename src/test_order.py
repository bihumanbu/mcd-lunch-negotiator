"""create-order 探针：验证下单接口返回什么（是否含支付入口）。

原则：
- 只创建 1 个商品 × 1 份，最小金额
- 不执行任何支付动作
- 拿到 orderId 后立即取消，避免留下挂单
- 全过程落盘到 docs/order-probe.json 作为证据

运行前请确认：这是真实下单，会产生真实订单（可取消）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcd_client import McdMcp, fen_to_yuan

STORE = "1420762"
BE_TYPE = 1
ORDER_TYPE = 1
# 龙焰芝士棒鸡腿堡（单点 ¥27.00，最小可单点商品之一）
SKU = "521953"

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "order-probe.json")


def main():
    c = McdMcp()
    log = {}

    # 1. 账号状态
    print("=== 1. query-my-account ===")
    try:
        acc = c.call("query-my-account", {})
        log["account"] = acc.get("data") if isinstance(acc, dict) else acc
        print(json.dumps(log["account"], ensure_ascii=False)[:400])
    except Exception as e:
        print("账号查询失败（可忽略）:", str(e)[:150])
        log["account"] = {"error": str(e)[:200]}

    # 2. 计价，拿 takeWayList
    print("\n=== 2. calculate-price（拿 takeWayCode）===")
    items = [{"productCode": SKU, "quantity": 1}]
    pr = c.call("calculate-price", {
        "storeCode": STORE, "orderType": ORDER_TYPE, "beType": BE_TYPE,
        "items": items, "needTableware": False})
    pd = pr.get("data") or {}
    log["price"] = pd
    print("price(分):", pd.get("price"), "-> ¥%s" % fen_to_yuan(pd.get("price")))
    take_list = pd.get("takeWayList") or []
    print("takeWayList:", json.dumps(take_list, ensure_ascii=False))
    if not take_list:
        print("!! 没有 takeWayList，无法下单")
        save(log)
        return
    take_code = take_list[0]["code"]

    # 3. 下单（不支付）
    print("\n=== 3. create-order（真实创建，不支付）===")
    args = {
        "storeCode": STORE, "orderType": ORDER_TYPE, "beType": BE_TYPE,
        "items": items, "needTableware": False, "takeWayCode": take_code,
    }
    print("入参:", json.dumps(args, ensure_ascii=False))
    try:
        r = c.call("create-order", args)
        log["create_order"] = r
        print("返回:", json.dumps(r, ensure_ascii=False)[:1500])
    except Exception as e:
        print("下单异常:", str(e)[:500])
        log["create_order"] = {"error": str(e)[:500]}
        save(log)
        return

    # 4. 找 orderId，看返回里有没有支付入口
    data = r.get("data") if isinstance(r, dict) else None
    order_id = None
    if isinstance(data, dict):
        for k in ("orderId", "order_id", "orderNo", "orderCode"):
            if data.get(k):
                order_id = str(data[k])
                break
    log["order_id"] = order_id
    print("\norderId:", order_id)

    # 检查是否含支付相关字段
    s = json.dumps(r, ensure_ascii=False)
    hits = [kw for kw in ("pay", "Pay", "payment", "支付", "二维码", "qr", "Qr", "QR", "url", "Url", "URL")
            if kw in s]
    log["payment_hints"] = hits
    print("支付相关字段线索:", hits or "无")

    # 5. 查订单状态
    if order_id:
        print("\n=== 4. query-order ===")
        try:
            q = c.call("query-order", {"orderId": order_id})
            log["query_order"] = q
            print("返回:", json.dumps(q, ensure_ascii=False)[:1200])
        except Exception as e:
            print("查询异常:", str(e)[:300])
            log["query_order"] = {"error": str(e)[:300]}

        # 6. 立即取消，不留挂单
        print("\n=== 5. cancel-order（清理）===")
        try:
            cc = c.call("cancel-order", {"orderId": order_id, "cancelReasonCode": "1"})
            log["cancel_order"] = cc
            print("取消返回:", json.dumps(cc, ensure_ascii=False)[:600])
        except Exception as e:
            print("取消失败，请手动取消！:", str(e)[:300])
            log["cancel_order"] = {"error": str(e)[:300]}

    save(log)


def save(log):
    try:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        print("\n证据已落盘:", os.path.abspath(OUT))
    except Exception as e:
        print("落盘失败:", e)


if __name__ == "__main__":
    main()
