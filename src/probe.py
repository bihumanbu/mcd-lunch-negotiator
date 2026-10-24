#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP 探针：验证麦当劳 MCP 连通性并拉取真实数据样本。
仅用于本地开发验证，Token 从私有凭据文件读取，不落盘、不打印。
"""
import json
import os
import sys
import urllib.request

CRED = os.path.expanduser("~/.workbuddy/credentials/mcd-mcp.json")
if not os.path.exists(CRED):
    CRED = r"D:/WB2home/.workbuddy/credentials/mcd-mcp.json"

with open(CRED, "r", encoding="utf-8") as f:
    TOKEN = json.load(f)["token"]

ENDPOINT = "https://mcp.mcd.cn"
_next_id = [0]


def rpc(method, params=None, timeout=45):
    _next_id[0] += 1
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": _next_id[0],
        "method": method,
        "params": params or {},
    }).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + TOKEN,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    raw = raw.strip()
    # 兼容 SSE 帧
    if raw.startswith("event:") or raw.startswith("data:"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    return json.loads(raw)


def call_tool(name, args=None, timeout=60):
    r = rpc("tools/call", {"name": name, "arguments": args or {}}, timeout=timeout)
    if "error" in r:
        return {"__error__": r["error"]}
    res = r.get("result", {})
    content = res.get("content") or []
    texts = [c.get("text", "") for c in content if isinstance(c, dict)]
    raw = "\n".join(t for t in texts if t)
    try:
        return json.loads(raw)
    except Exception:
        return {"__raw__": raw[:4000]}


def brief(obj, depth=0, maxs=900):
    s = json.dumps(obj, ensure_ascii=False)
    return s[:maxs] + ("..." if len(s) > maxs else "")


if __name__ == "__main__":
    print("=" * 60)
    init = rpc("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0.1"},
    })
    print("SERVER:", json.dumps(init.get("result", {}).get("serverInfo", {}), ensure_ascii=False))

    print("\n" + "=" * 60)
    print("[1] now-time-info")
    print(brief(call_tool("now-time-info")))

    print("\n" + "=" * 60)
    print("[2] query-my-account")
    print(brief(call_tool("query-my-account")))

    print("\n" + "=" * 60)
    print("[3] query-my-coupons")
    c = call_tool("query-my-coupons", {"page": "1", "pageSize": "50"})
    print(brief(c, maxs=2500))

    print("\n" + "=" * 60)
    print("[4] order-list (近 5 单)")
    o = call_tool("order-list", {"page": "1", "pageSize": "5"})
    print(brief(o, maxs=2500))
