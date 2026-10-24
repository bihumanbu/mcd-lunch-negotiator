#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦当劳 MCP 轻量客户端。

设计要点（踩坑记录见 MCP_INTEGRATION.md）：
1. mcd-mcp 的 tools/call 返回不是纯 JSON。实测三种形态：
   a) "API Response Information" 说明文档 + "## Original Response" 后的原始 JSON
   b) 纯 Markdown 文本（query-my-coupons 券列表）
   c) TOON 文本（list-nutrition-foods）
   所以必须做分形态解析，不能直接 json.loads。
2. 无状态 HTTP：不需要维持 Mcp-Session-Id。
3. Token 只从私有凭据文件读，绝不硬编码、绝不写日志。
"""
import json
import os
import re
import urllib.error
import urllib.request

ENDPOINT = "https://mcp.mcd.cn"
_CANDIDATE_CREDS = [
    # 可选：私有凭据文件（不纳入版本控制）
    os.path.join(os.path.expanduser("~"), ".workbuddy", "credentials", "mcd-mcp.json"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".token.json"),
]


class McpError(RuntimeError):
    pass


def load_token():
    for p in _CANDIDATE_CREDS:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)["token"]
    env = os.environ.get("MCD_MCP_TOKEN")
    if env:
        return env
    raise McpError("未找到 MCP Token：请配置 ~/.workbuddy/credentials/mcd-mcp.json 或环境变量 MCD_MCP_TOKEN")


class McdMcp:
    def __init__(self, token=None, timeout=60):
        self.token = token or load_token()
        self.timeout = timeout
        self._id = 0
        self.trace = []          # 调用审计日志，用于生成 MCP_INTEGRATION 证据
        self._initialized = False

    # ---------- 传输层 ----------
    def _post(self, payload):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            ENDPOINT,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer " + self.token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise McpError("HTTP %s: %s" % (e.code, detail))
        return _strip_sse(raw)

    def rpc(self, method, params=None):
        self._id += 1
        raw = self._post({"jsonrpc": "2.0", "id": self._id, "method": method,
                          "params": params or {}})
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except Exception:
            obj = _first_json(raw)
            if obj is None:
                raise McpError("无法解析 RPC 响应：%s" % raw[:200])
            return obj

    def initialize(self):
        if self._initialized:
            return
        self.rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcd-lunch-negotiator", "version": "0.1"},
        })
        self._initialized = True

    # ---------- 业务层 ----------
    def list_tools(self):
        self.initialize()
        res = self.rpc("tools/list").get("result", {})
        return res.get("tools", [])

    def call_raw(self, name, args=None, timeout=None):
        """调用工具，返回原始文本（不做解析）。"""
        self.initialize()
        old = self.timeout
        if timeout:
            self.timeout = timeout
        try:
            resp = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        finally:
            self.timeout = old

        if "error" in resp:
            raise McpError("%s -> %s" % (name, json.dumps(resp["error"], ensure_ascii=False)[:300]))

        result = resp.get("result", {})
        content = result.get("content") or []
        text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("text"))
        self.trace.append({"tool": name, "args": args or {}, "chars": len(text)})
        if result.get("isError"):
            raise McpError("%s 返回错误：%s" % (name, text[:300]))
        return text

    def call(self, name, args=None):
        """调用工具并尽力解析成 Python 对象（dict/list）。"""
        text = self.call_raw(name, args)
        parsed = parse_response(text)
        return parsed


# ---------- 解析层 ----------
def _strip_sse(raw):
    raw = raw.strip()
    if raw.startswith("event:") or raw.startswith("data:"):
        chunks = []
        for line in raw.splitlines():
            if line.startswith("data:"):
                chunks.append(line[5:].strip())
        if chunks:
            return "\n".join(chunks)
    return raw


def parse_response(text):
    """
    三种返回形态统一解析：
    1. 含 "## Original Response" -> 取其后 JSON
    2. 纯 JSON -> 直接 loads
    3. Markdown / TOON -> 返回 {"__text__": ..., "__format__": ...}
    """
    if text is None:
        return {}
    t = text.strip()

    # 形态 1
    marker = "## Original Response"
    if marker in t:
        tail = t.split(marker, 1)[1].strip()
        tail = tail.lstrip("：: \n")
        obj = _first_json(tail)
        if obj is not None:
            return obj

    # 形态 2
    if t.startswith("{") or t.startswith("["):
        try:
            return json.loads(t)
        except Exception:
            pass

    # 形态 3：说明文本 + JSON 混排（mcd-mcp 最常见），兜底全文找第一个完整 JSON
    obj = _first_json(t)
    if obj is not None and isinstance(obj, dict) and ("success" in obj or "code" in obj or "data" in obj):
        return obj

    # 形态 4
    fmt = "toon" if re.match(r"^\{[A-Za-z]", t) else "markdown"
    return {"__text__": t, "__format__": fmt}


def _first_json(s):
    """从字符串中抠出第一个完整 JSON 对象。"""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    return None
    return None


def yuan(cents_or_str):
    """麦当劳计价接口返回「分」，菜单/订单返回「元」字符串 —— 统一转成元（float）。"""
    if cents_or_str is None:
        return None
    if isinstance(cents_or_str, (int, float)):
        return round(float(cents_or_str) / 100.0, 2) if float(cents_or_str) > 1000 else round(float(cents_or_str), 2)
    try:
        return round(float(str(cents_or_str)), 2)
    except Exception:
        return None


def fen_to_yuan(v):
    """明确按「分」解释。"""
    try:
        return round(float(v) / 100.0, 2)
    except Exception:
        return None


if __name__ == "__main__":
    c = McdMcp()
    tools = c.list_tools()
    print("工具数:", len(tools))
    print("示例:", [t["name"] for t in tools[:8]])
