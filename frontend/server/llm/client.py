"""OpenAI 兼容 / Anthropic 两种格式的 LLM 调用：拉取模型列表、对话、测试。

对话接口（协议）有三种：Chat Completions、Responses、Messages（Anthropic）。每个模型第一次调用时
按优先级自动尝试，接口明确表示不支持就换下一种；试通后记到模型的 protocol 里，以后直接用。"""
from __future__ import annotations

import base64
import copy
import io
import re
import time

import httpx
from PIL import Image

from . import store

ANTHROPIC_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
BUDGETS = {"low": 2048, "medium": 8192, "high": 16000}
MAX_TOKENS = 16000
_LEVEL_TEXT = {"off": "不思考", "on": "思考", "default": "模型默认", "auto": "自动"}
PROTOCOLS = ["chat", "responses", "messages"]
PROTOCOL_TEXT = {"chat": "Chat Completions", "responses": "Responses", "messages": "Messages"}
_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None, retryable: bool | None = None):
        super().__init__(message)
        self.status = status
        # 过一会儿再试可能成功：连接失败 / 超时、限流、服务端错误。401 / 403 / 404 / 400 之类重试也不会变
        self.retryable = retryable if retryable is not None else (status in (408, 429) or (status or 0) >= 500)


# ───────────────────────────── 通用 ─────────────────────────────

def _anthropic_base(p: dict) -> str:
    b = p["base_url"].rstrip("/")
    return b[:-3] if b.endswith("/v1") else b


def _openai_base(p: dict) -> str:
    """OpenAI 系接口的根地址：OpenAI 格式的服务商原样使用；Anthropic 格式的（通常不带 /v1）补上 /v1。"""
    if p["format"] == "openai":
        return p["base_url"].rstrip("/")
    return _anthropic_base(p) + "/v1"


def _headers(p: dict, proto: str | None = None) -> dict:
    key = p.get("api_key", "")
    proto = proto or ("messages" if p["format"] == "anthropic" else "chat")
    if proto == "messages":
        h = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
        if p["format"] == "openai":      # OpenAI 兼容服务商上的 Claude：不知道中转认哪种，两种都带
            h["authorization"] = f"Bearer {key}"
            h["x-api-key"] = key
        elif p.get("auth") == "bearer":
            h["authorization"] = f"Bearer {key}"
        else:
            h["x-api-key"] = key
        return h
    return {"authorization": f"Bearer {key}", "content-type": "application/json"}


def _err_text(r: httpx.Response) -> str:
    try:
        d = r.json()
        e = d.get("error", d)
        if isinstance(e, dict):
            return str(e.get("message") or e)
        return str(e)
    except ValueError:
        return r.text[:500]


# ───────────────────────────── 模型列表 ─────────────────────────────

def _anthropic_entry(m: dict) -> dict:
    caps = m.get("capabilities") or {}
    entry = {"id": m["id"], "name": m.get("display_name") or m["id"], "vision": None, "vision_source": None}
    if caps:
        img = caps.get("image_input") or {}
        if "supported" in img:
            entry["vision"], entry["vision_source"] = bool(img["supported"]), "api"
        th = caps.get("thinking") or {}
        types = th.get("types") or {}
        eff = caps.get("effort") or {}
        if not th.get("supported"):
            entry["thinking"] = {"mode": "none", "levels": ["off"]}
        elif (types.get("adaptive") or {}).get("supported"):
            lv = [x for x in ANTHROPIC_EFFORTS if (eff.get(x) or {}).get("supported")] if eff.get("supported") else ["auto"]
            entry["thinking"] = {"mode": "adaptive", "levels": ["off"] + lv}
        elif (types.get("enabled") or {}).get("supported"):
            entry["thinking"] = {"mode": "budget", "levels": ["off", "low", "medium", "high"]}
        else:
            entry["thinking"] = {"mode": "none", "levels": ["off"]}
    else:
        entry["thinking"] = {"mode": "unknown", "levels": ["off", "low", "medium", "high"]}
    return entry


def _openai_entry(m: dict, style: str) -> dict:
    entry = {"id": m["id"], "name": m.get("name") or m["id"], "vision": None, "vision_source": None,
             "thinking": {"mode": style, "levels": list(store.THINKING_STYLES[style])}}
    mods = (m.get("architecture") or {}).get("input_modalities")
    if isinstance(mods, list):
        entry["vision"], entry["vision_source"] = "image" in mods, "api"
    return entry


def fetch_models(p: dict) -> list[dict]:
    if not p.get("api_key"):
        raise LLMError("请先填写 Token")
    raw: list[dict] = []
    try:
        if p["format"] == "anthropic":
            after = None
            for _ in range(20):
                params = {"limit": 1000}
                if after:
                    params["after_id"] = after
                r = httpx.get(f"{_anthropic_base(p)}/v1/models", headers=_headers(p), params=params, timeout=30)
                if r.status_code != 200:
                    raise LLMError(f"获取模型失败（HTTP {r.status_code}）：{_err_text(r)}", r.status_code)
                d = r.json()
                raw += d.get("data", [])
                if not d.get("has_more"):
                    break
                after = d.get("last_id")
            fresh = [_anthropic_entry(m) for m in raw if m.get("id")]
        else:
            r = httpx.get(f"{p['base_url'].rstrip('/')}/models", headers=_headers(p), timeout=30)
            if r.status_code != 200:
                raise LLMError(f"获取模型失败（HTTP {r.status_code}）：{_err_text(r)}", r.status_code)
            d = r.json()
            raw = d.get("data", d if isinstance(d, list) else [])
            store.apply_style(p)
            fresh = [_openai_entry(m, p["thinking_style"]) for m in raw if isinstance(m, dict) and m.get("id")]
    except httpx.HTTPError as e:
        raise LLMError(f"连接失败：{e}") from e

    old = {m["id"]: m for m in p.get("models", [])}
    out = []
    for m in sorted(fresh, key=lambda x: x["id"]):
        o = old.get(m["id"])
        if o:
            m["enabled"] = o.get("enabled", False)
            if o.get("vision_source") in ("test", "manual"):
                m["vision"], m["vision_source"] = o.get("vision"), o.get("vision_source")
            unsup = list(o.get("unsupported_levels") or [])
            if o.get("thinking", {}).get("can_disable") is False and "off" not in unsup:
                unsup.append("off")      # 旧版只记了 can_disable
            if unsup:                    # 接口拒绝过的档位继续去掉（OpenAI 兼容格式由 apply_style 处理）
                m["unsupported_levels"] = unsup
                m["thinking"] = store.thinking_with(m["thinking"]["mode"], m["thinking"]["levels"], unsup)
            for k in ("structured", "last_test", "protocol"):
                if k in o:
                    m[k] = o[k]
            m["level"] = o.get("level") if o.get("level") in m["thinking"]["levels"] else m["thinking"]["levels"][0]
        else:
            m["enabled"] = False
            m["level"] = m["thinking"]["levels"][0]
        out.append(m)
    # 已勾选但这次列表里没有的模型保留（可能是中转临时不返回）
    ids = {m["id"] for m in out}
    out += [o for o in old.values() if o.get("enabled") and o["id"] not in ids]
    return out


# ───────────────────────────── 对话 ─────────────────────────────

def _fallback_level(levels: list[str], level: str) -> str | None:
    """档位被接口拒绝后改用哪一档：「不思考」→ 最低档；其它 → 低一档，没有更低的就高一档，
    再没有就「不思考」；全被拒绝 → 「模型默认」（不发送思考参数）。None = 无法再降。"""
    if level == "default" or level not in levels:
        return None
    rest = [x for x in levels if x != level]
    on = [x for x in rest if x != "off"]
    if level == "off":
        return on[0] if on else "default"
    i = levels.index(level)
    lower = [x for x in levels[:i] if x != "off"]
    higher = [x for x in levels[i + 1:] if x != "off"]
    return lower[-1] if lower else higher[0] if higher else "off" if "off" in rest else "default"


def _reject_level(p: dict, m: dict, thinking: dict, level: str, nxt: str) -> dict:
    """记住被拒绝的档位：从模型的档位里去掉；模型默认档位是它时改成 nxt。返回新的 thinking。"""
    unsup = sorted({*(m.get("unsupported_levels") or []), level}, key=_level_rank)
    m["unsupported_levels"] = unsup
    th = store.thinking_with(thinking.get("mode"), thinking["levels"], unsup)
    patch = {"thinking": th, "unsupported_levels": unsup}
    if m.get("level") in (level, None):
        patch["level"] = nxt
    m.update(patch)              # 调用方手里的 m 也同步，后续请求不再先试这一档
    store.update_model(p["id"], m["id"], patch)
    return th


def _mentions_level(low: str, level: str) -> bool:
    """错误信息是否针对这一档的取值；不是的话视为接口不认整个思考参数，不该逐档去掉。"""
    if level == "off":
        return "disabl" in low or any(re.search(rf"\b{w}\b", low) for w in ("none", "false", "off"))
    words = {"on": ("true", "enabled"), "auto": ("adaptive",)}.get(level, (level,))
    return any(re.search(rf"\b{re.escape(w)}\b", low) for w in words)


def _level_rank(x: str) -> int:
    return store.LEVEL_ORDER.index(x) if x in store.LEVEL_ORDER else len(store.LEVEL_ORDER)


def _is_openai_family(model_id: str) -> bool:
    name = model_id.lower().rsplit("/", 1)[-1]
    if name.startswith("gpt-oss"):      # 开源权重，各家一般只在 Chat Completions 上提供
        return False
    return name.startswith("gpt") or "codex" in name or re.match(r"o[1-9](?![0-9a-z])", name) is not None


def protocol_order(p: dict, m: dict) -> list[str]:
    """尝试接口的顺序：记住的接口最先；其次按模型名（claude / gpt 系）；再按服务商格式。"""
    if "claude" in m["id"].lower():
        order = ["messages", "chat", "responses"]
    elif _is_openai_family(m["id"]):
        order = ["responses", "chat", "messages"]
    elif p["format"] == "anthropic":
        order = ["messages", "chat", "responses"]
    else:
        order = ["chat", "responses", "messages"]
    if m.get("protocol") in PROTOCOLS:
        order = [m["protocol"]] + [x for x in order if x != m["protocol"]]
    return order


_PROTO_HINTS = ("protocol", "endpoint", "not found", "invalid url", "unknown url", "no route", "unsupported",
                "not support", "not allowed", "does not exist")


def _wrong_protocol(status: int, low: str) -> bool:
    """接口是否在说「这个模型 / 中转不走这种接口」：只在这时换下一种；鉴权、限流、服务端错误照原样报出。"""
    if status in (404, 405):
        return True
    return status in (400, 415, 422) and any(s in low for s in _PROTO_HINTS)


def chat(p: dict, m: dict, level: str, system: str, parts: list[dict], schema: dict | None) -> dict:
    """发送一次对话。parts: [{"type":"text","text":...} | {"type":"image","media_type":...,"data":b64}]。

    按 protocol_order 依次尝试三种接口，接口明确不支持就换下一种，试通后记到模型的 protocol。
    遇到接口不支持的参数（结构化输出 / 关闭思考 / 思考强度）会自动降级重试，并把结论记到模型设置里。
    返回 {"text", "request" (去掉图片数据), "attempts", "elapsed", "level"（实际使用的档位）,
    "level_note"（档位被自动改掉时给用户的提示，否则 None）, "protocol"（实际使用的接口）}。
    """
    t0 = time.time()
    attempts: list[str] = []
    fails: list[str] = []
    for proto in protocol_order(p, m):
        res = _chat_with(p, m, proto, level, system, parts, schema, attempts)
        if res is not None:
            if m.get("protocol") != proto:
                m["protocol"] = proto
                store.update_model(p["id"], m["id"], {"protocol": proto})
            res.update(attempts=attempts, elapsed=time.time() - t0, protocol=proto)
            return res
        fails.append(f"{PROTOCOL_TEXT[proto]}：{attempts[-1]}")
        if m.get("protocol") == proto:   # 记住的接口不再可用（例如中转改了）：清掉，重新按优先级试
            m["protocol"] = None
            store.update_model(p["id"], m["id"], {"protocol": None})
    raise LLMError("三种接口都不支持该模型。" + "；".join(fails), 400)


def _chat_with(p, m, proto, level, system, parts, schema, attempts) -> dict | None:
    """用一种接口发送（含参数自动降级）。成功返回结果；接口表示不支持这种协议时返回 None；其它错误抛出。"""
    asked = level
    thinking = m.get("thinking") or {"mode": "unknown", "levels": ["off"]}
    use_format = bool(schema) and m.get("structured") is not False
    use_effort = True
    # OpenAI 系接口：json_schema → json_object → 不约束。已知只支持 json_object 的模型（含 DeepSeek）直接从 object 开始
    json_mode = "object" if (m.get("structured") == "object" or thinking.get("mode") == "deepseek") else "schema"
    for _ in range(12):
        fmt_schema = schema if use_format else None
        if proto == "messages":
            url, body = _anthropic_body(p, m, thinking, level, system, parts, fmt_schema, use_effort)
        elif proto == "responses":
            url, body = _responses_body(p, m, thinking, level, system, parts, fmt_schema, json_mode, use_effort)
        else:
            url, body = _openai_body(p, m, thinking, level, system, parts, fmt_schema, json_mode, use_effort)
        try:
            r = httpx.post(url, headers=_headers(p, proto), json=body, timeout=600)
        except httpx.HTTPError as e:
            raise LLMError(f"连接失败：{e}", retryable=True) from e
        if r.status_code == 200:
            if proto != "messages" and use_format and json_mode == "object" and m.get("structured") != "object":
                store.update_model(p["id"], m["id"], {"structured": "object"})   # 记住，下次不再先试 json_schema
            text = _extract_text(proto, r.json())
            note = None
            if level != asked:
                note = (f"「{m['id']}」不支持思考强度「{_LEVEL_TEXT.get(asked, asked)}」，"
                        f"已自动改用「{_LEVEL_TEXT.get(level, level)}」，并从该模型的档位中去掉")
            return {"text": text, "request": _sanitize(body), "level": level, "level_note": note}
        msg = _err_text(r)
        low = msg.lower()
        attempts.append(f"HTTP {r.status_code}: {msg}")
        if r.status_code not in (400, 422):
            if _wrong_protocol(r.status_code, low):
                return None
            raise LLMError(f"HTTP {r.status_code}：{msg}", r.status_code)
        # —— 自动降级 ——
        if use_format and any(s in low for s in ("format", "schema", "response_format", "output_config")) \
                and "effort" not in low:
            if proto != "messages" and json_mode == "schema":
                json_mode = "object"
                continue
            use_format = False
            store.update_model(p["id"], m["id"], {"structured": False})
            continue
        if _sent_thinking(body) and any(s in low for s in ("effort", "reasoning", "output_config", "thinking")):
            nxt = _fallback_level(thinking["levels"], level) if _mentions_level(low, level) else None
            if nxt is not None:         # 这一档被拒绝：换相邻档位，并记住
                thinking = _reject_level(p, m, thinking, level, nxt)
                level = nxt
                continue
            if use_effort:
                use_effort = False      # 档位之外的思考参数也不认：改为不发送
                continue
        if _wrong_protocol(r.status_code, low):
            return None
        raise LLMError(f"HTTP {r.status_code}：{msg}", r.status_code)
    raise LLMError("多次自动降级后仍失败：" + " | ".join(attempts[-3:]))


def _anthropic_body(p, m, thinking, level, system, parts, schema, use_effort):
    content = []
    for x in parts:
        if x["type"] == "text":
            content.append({"type": "text", "text": x["text"]})
        else:
            content.append({"type": "image", "source": {"type": "base64", "media_type": x["media_type"],
                                                        "data": x["data"]}})
    body: dict = {"model": m["id"], "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": content}]}
    if system:
        body["system"] = system
    oc: dict = {}
    mode = thinking.get("mode") if level != "default" else None    # 模型默认：不发送思考参数
    if mode not in (None, "adaptive", "budget", "none"):
        mode = "unknown"                 # 档位来自 OpenAI 兼容服务商：按未知处理（adaptive + effort）
    if mode == "adaptive":
        if level == "off":
            body["thinking"] = {"type": "disabled"}
        else:
            body["thinking"] = {"type": "adaptive"}
            if level != "auto" and use_effort:
                oc["effort"] = level
    elif mode == "budget":
        if level in BUDGETS:
            body["thinking"] = {"type": "enabled", "budget_tokens": BUDGETS[level]}
            body["max_tokens"] = BUDGETS[level] + 8000
    elif mode == "unknown" and level != "off":
        body["thinking"] = {"type": "adaptive"}
        if use_effort and level in _EFFORT_LEVELS:
            oc["effort"] = level
    if schema:
        oc["format"] = {"type": "json_schema", "schema": schema}
    if oc:
        body["output_config"] = oc
    return f"{_anthropic_base(p)}/v1/messages", body


def _sent_thinking(body: dict) -> bool:
    return any(k in body for k in ("reasoning_effort", "reasoning", "thinking", "enable_thinking")) or \
        "effort" in body.get("output_config", {})


def _openai_body(p, m, thinking, level, system, parts, schema, json_mode, use_effort):
    content = []
    for x in parts:
        if x["type"] == "text":
            content.append({"type": "text", "text": x["text"]})
        else:
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{x['media_type']};base64,{x['data']}"}})
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": content})
    body: dict = {"model": m["id"], "messages": msgs}
    style = thinking.get("mode") if level != "default" else None   # 模型默认：不发送思考参数
    if style not in (None, *store.THINKING_STYLES):
        style = "openai"                 # 档位来自 Anthropic 服务商：用 reasoning_effort
    if use_effort:
        if style == "deepseek":
            if level == "off":
                body["thinking"] = {"type": "disabled"}
            else:
                body["thinking"] = {"type": "enabled"}
                body["reasoning_effort"] = level
        elif style == "qwen":
            body["enable_thinking"] = level != "off"
        elif style in ("openai", "reasoning_effort") and (level == "off" or level in _EFFORT_LEVELS):
            body["reasoning_effort"] = "none" if level == "off" else level
    if schema:
        if json_mode == "schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "prompt_fields", "strict": True, "schema": schema}}
        elif json_mode == "object":
            body["response_format"] = {"type": "json_object"}
    return f"{_openai_base(p)}/chat/completions", body


def _responses_body(p, m, thinking, level, system, parts, schema, json_mode, use_effort):
    content = []
    for x in parts:
        if x["type"] == "text":
            content.append({"type": "input_text", "text": x["text"]})
        else:
            content.append({"type": "input_image", "image_url": f"data:{x['media_type']};base64,{x['data']}"})
    body: dict = {"model": m["id"], "input": [{"role": "user", "content": content}]}
    if system:
        body["instructions"] = system
    style = thinking.get("mode") if level != "default" else None   # 模型默认：不发送思考参数
    if use_effort and style not in (None, "none", "qwen") and (level == "off" or level in _EFFORT_LEVELS):
        body["reasoning"] = {"effort": "none" if level == "off" else level}
    if schema:
        if json_mode == "schema":
            body["text"] = {"format": {"type": "json_schema", "name": "prompt_fields", "strict": True,
                                       "schema": schema}}
        elif json_mode == "object":
            body["text"] = {"format": {"type": "json_object"}}
    return f"{_openai_base(p)}/responses", body


def _extract_text(proto: str, d: dict) -> str:
    if proto == "messages":
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    if proto == "responses":
        if isinstance(d.get("output_text"), str) and d["output_text"]:
            return d["output_text"]
        return "".join(c.get("text", "") for o in d.get("output") or [] if o.get("type") == "message"
                       for c in o.get("content") or [] if c.get("type") == "output_text")
    ch = (d.get("choices") or [{}])[0].get("message", {})
    c = ch.get("content")
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c or ""


def _sanitize(body: dict) -> dict:
    """日志用：把图片数据替换成尺寸说明。"""
    b = copy.deepcopy(body)
    for msg in b.get("messages", []) + b.get("input", []):
        if not isinstance(msg.get("content"), list):
            continue
        for part in msg["content"]:
            src = part.get("source") if part.get("type") == "image" else None
            if src and "data" in src:
                src["data"] = f"<image {len(src['data']) * 3 // 4} bytes>"
            if part.get("type") == "image_url":
                url = part["image_url"]["url"]
                part["image_url"]["url"] = f"<image {len(url) * 3 // 4} bytes>"
            if part.get("type") == "input_image":
                part["image_url"] = f"<image {len(part['image_url']) * 3 // 4} bytes>"
    return b


# ───────────────────────────── 图片与测试 ─────────────────────────────

def image_part(raw: bytes, max_side: int = 1536) -> dict:
    """压缩到长边 max_side，透明背景铺白，转 JPEG。"""
    img = Image.open(io.BytesIO(raw))
    img.load()
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return {"type": "image", "media_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode(),
            "size": list(img.size)}


PROBE_COLORS = {"red": (220, 30, 30), "green": (30, 170, 60), "blue": (30, 70, 220),
                "yellow": (240, 220, 30), "purple": (140, 40, 190), "orange": (245, 140, 20)}


def probe_image(left: str, right: str) -> dict:
    """左右两半不同颜色的测试图（两种颜色随机，避免模型看不到图时靠猜答对）。"""
    img = Image.new("RGB", (128, 64), PROBE_COLORS[left])
    img.paste(Image.new("RGB", (64, 64), PROBE_COLORS[right]), (64, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return {"type": "image", "media_type": "image/png", "data": base64.b64encode(buf.getvalue()).decode()}
