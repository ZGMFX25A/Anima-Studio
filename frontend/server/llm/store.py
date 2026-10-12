"""LLM 服务商配置的读写（frontend/data/settings.json，本机明文保存）。"""
from __future__ import annotations

import json
import threading
import uuid

from .. import config

_lock = threading.RLock()

DEFAULT_BASE = {"openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com"}
LEVEL_ORDER = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]

# OpenAI 兼容格式下各家控制思考的方式不同，按服务商选择：
#   openai   —— reasoning_effort（不思考 = "none"）
#   deepseek —— thinking.type enabled/disabled + reasoning_effort（low/high/max）
#   qwen     —— enable_thinking true/false
#   none     —— 不发送任何思考参数，由模型默认
THINKING_STYLES = {
    "openai": ["off", "low", "medium", "high"],
    "deepseek": ["off", "low", "high", "max"],
    "qwen": ["off", "on"],
    "none": ["default"],
}


def detect_style(base_url: str) -> str:
    u = (base_url or "").lower()
    if "deepseek" in u:
        return "deepseek"
    if "dashscope" in u or "aliyuncs" in u:
        return "qwen"
    return "openai"


def _map_level(old: str | None, levels: list[str]) -> str:
    """切换思考参数方式后，把原档位映射到最接近的新档位。"""
    if old in levels:
        return old
    if old in (None, "off", "default"):
        return levels[0]
    cand = {"minimal": "low", "medium": "high", "xhigh": "high", "max": "high", "auto": "high"}.get(old, old)
    if cand in levels:
        return cand
    return "on" if "on" in levels else levels[0]


def supported_levels(levels: list[str], unsupported: list[str] | None) -> list[str]:
    """去掉接口拒绝过的档位；全被拒绝时只剩「模型默认」（不发送思考参数）。"""
    lv = [x for x in levels if x not in (unsupported or [])]
    return lv or ["default"]


def thinking_with(mode: str, levels: list[str], unsupported: list[str] | None) -> dict:
    th = {"mode": mode, "levels": supported_levels(levels, unsupported)}
    if "off" in (unsupported or []):
        th["can_disable"] = False
    return th


def apply_style(p: dict) -> None:
    """OpenAI 兼容服务商：让每个模型的思考档位与服务商的思考参数方式一致（去掉接口拒绝过的档位）。"""
    if p.get("format") != "openai":
        return
    style = p.get("thinking_style")
    if style not in THINKING_STYLES:
        style = p["thinking_style"] = detect_style(p.get("base_url", ""))
    for m in p.get("models", []):
        th = m.get("thinking") or {}
        if th.get("mode") != style:
            m.pop("unsupported_levels", None)      # 换了思考参数方式，之前被拒绝的档位不再适用
        want = thinking_with(style, THINKING_STYLES[style], m.get("unsupported_levels"))
        if th != want:
            m["thinking"] = want
        m["level"] = _map_level(m.get("level"), want["levels"])


def _load() -> dict:
    try:
        d = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"providers": []}
    for p in d.get("providers", []):
        apply_style(p)
    return d


def _save(d: dict) -> None:
    tmp = config.SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config.SETTINGS_PATH)


def mask(key: str) -> str:
    if not key:
        return ""
    return key[:4] + "…" + key[-4:] if len(key) > 10 else "…" + key[-2:]


def public(p: dict) -> dict:
    q = {k: v for k, v in p.items() if k != "api_key"}
    q["api_key_masked"] = mask(p.get("api_key", ""))
    q["has_key"] = bool(p.get("api_key"))
    return q


def providers() -> list[dict]:
    with _lock:
        return _load()["providers"]


def get(pid: str) -> dict | None:
    with _lock:
        return next((p for p in _load()["providers"] if p["id"] == pid), None)


def create(data: dict) -> dict:
    fmt = data.get("format") if data.get("format") in DEFAULT_BASE else "openai"
    p = {"id": uuid.uuid4().hex[:8], "name": (data.get("name") or "").strip() or fmt,
         "format": fmt, "base_url": (data.get("base_url") or "").strip() or DEFAULT_BASE[fmt],
         "auth": data.get("auth") if data.get("auth") in ("x-api-key", "bearer") else "x-api-key",
         "api_key": (data.get("api_key") or "").strip(), "models": []}
    if data.get("thinking_style") in THINKING_STYLES:
        p["thinking_style"] = data["thinking_style"]
    apply_style(p)
    with _lock:
        d = _load()
        d["providers"].append(p)
        _save(d)
    return p


def update(pid: str, data: dict) -> dict | None:
    with _lock:
        d = _load()
        p = next((x for x in d["providers"] if x["id"] == pid), None)
        if not p:
            return None
        for k in ("name", "base_url"):
            if k in data and data[k] is not None:
                p[k] = str(data[k]).strip()
        if data.get("format") in DEFAULT_BASE:
            p["format"] = data["format"]
        if data.get("auth") in ("x-api-key", "bearer"):
            p["auth"] = data["auth"]
        if data.get("api_key"):          # 留空表示不修改
            p["api_key"] = data["api_key"].strip()
        if data.get("thinking_style") in THINKING_STYLES:
            p["thinking_style"] = data["thinking_style"]
        apply_style(p)
        _save(d)
        return p


def delete(pid: str) -> bool:
    with _lock:
        d = _load()
        n = len(d["providers"])
        d["providers"] = [p for p in d["providers"] if p["id"] != pid]
        _save(d)
        return len(d["providers"]) != n


def set_models(pid: str, models: list[dict]) -> dict | None:
    with _lock:
        d = _load()
        p = next((x for x in d["providers"] if x["id"] == pid), None)
        if not p:
            return None
        p["models"] = models
        apply_style(p)
        _save(d)
        return p


def update_model(pid: str, model_id: str, patch: dict) -> dict | None:
    """更新单个模型的设置（enabled / vision / level / thinking / unsupported_levels / structured / last_test / protocol）。"""
    with _lock:
        d = _load()
        p = next((x for x in d["providers"] if x["id"] == pid), None)
        if not p:
            return None
        m = next((x for x in p["models"] if x["id"] == model_id), None)
        if not m:
            return None
        for k, v in patch.items():
            if k == "vision":
                m["vision"] = v
                m["vision_source"] = patch.get("vision_source", "manual") if v is not None else None
            elif k in ("enabled", "level", "thinking", "unsupported_levels", "structured", "last_test",
                       "vision_source", "protocol"):
                m[k] = v
        lv = m.get("thinking", {}).get("levels") or ["off"]
        if m.get("level") not in lv:
            m["level"] = lv[0]
        _save(d)
        return m


def find_model(pid: str, model_id: str) -> tuple[dict | None, dict | None]:
    p = get(pid)
    if not p:
        return None, None
    return p, next((m for m in p["models"] if m["id"] == model_id), None)


def enabled_models() -> list[dict]:
    out = []
    for p in providers():
        for m in p["models"]:
            if m.get("enabled"):
                out.append({"key": f"{p['id']}::{m['id']}", "provider_id": p["id"], "provider": p["name"],
                            "model_id": m["id"], "label": m.get("name") or m["id"], "vision": m.get("vision"),
                            "levels": m.get("thinking", {}).get("levels") or ["off"], "level": m.get("level")})
    return out
