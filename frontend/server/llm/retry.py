"""LLM 请求的自动重试与取消（AI 填写、识别角色、人设 AI 拆分 / 整理共用）。

出错最多再试 MAX_RETRIES 次（共 1 + MAX_RETRIES 次请求），仍失败才报错：
- 连接失败 / 超时、429、5xx：依次等待 2 / 4 / 6 / 8 / 8 秒后重试；
- 返回非 JSON、JSON 缺少应有的字段、模型拒绝：立即重试；
- 401 / 403 / 404 / 400 等请求本身的问题：不重试。
页面点「取消」时调用 cancel(request_id)，在下一次重试前停下。
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable

from . import client

MAX_RETRIES = 5
# 模型用自然语言拒绝（而不是输出 JSON）时常见的说法
REFUSAL_HINTS = ("i can't", "i cannot", "i can’t", "i'm sorry", "i’m sorry", "i am sorry", "i won't", "i will not",
                 "unable to", "not able to", "cannot assist", "can't help", "抱歉", "无法", "不能", "很遗憾", "违反")
_TIPS = {"refusal": "可以换一个限制更少的模型、降低安全等级，或调整描述后再试。",
         "json": "原文已记录到 logs/llm_last_request.json。",
         "empty": "可以换个模型试试。",
         "http": "请检查网络或服务商状态。"}

_cancelled: set[str] = set()
_lock = threading.Lock()


def cancel(request_id: str) -> None:
    if request_id:
        with _lock:
            _cancelled.add(request_id)


def is_cancelled(request_id: str) -> bool:
    with _lock:
        return bool(request_id) and request_id in _cancelled


def clear(request_id: str) -> None:
    """整个操作结束后调用（一次操作可能包含多次 LLM 请求，中途不能清掉取消标记）。"""
    with _lock:
        _cancelled.discard(request_id)


def parse_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        d = json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a < 0 or b <= a:
            raise
        d = json.loads(t[a:b + 1])
    if not isinstance(d, dict):
        raise ValueError("不是 JSON 对象")
    return d


def json_check(keys: tuple[str, ...]) -> Callable[[str], tuple[dict | None, str, str]]:
    """检查回复：能解析成 JSON、没有拒绝、至少含 keys 中的一个键。返回 (JSON, 错误说明, 错误类型)。"""
    def check(text: str):
        try:
            raw = parse_json(text)
        except ValueError:
            t = text.strip()
            if any(h in t.lower() for h in REFUSAL_HINTS):
                return None, f"模型拒绝了这个请求：「{t[:120]}」", "refusal"
            return None, "LLM 返回的内容不是有效的 JSON", "json"
        refusal = str(raw.get("refusal") or "").strip()
        if refusal:
            return None, f"LLM 拒绝了这个请求：{refusal}", "refusal"
        if keys and not any(k in raw for k in keys):   # 偶发：JSON 模式下只回了 {"type": "json_object"} 之类
            return None, "LLM 返回的 JSON 里没有应有的字段", "empty"
        return raw, "", ""
    return check


def chat(p: dict, m: dict, level: str, system: str, parts: list[dict], schema: dict | None,
         check: Callable[[str], tuple[dict | None, str, str]], log: dict, request_id: str = "") -> tuple[dict, dict]:
    """带重试的一次 LLM 调用。返回 (检查通过的 JSON, 最后一次 client.chat 的结果)。

    每次的错误记在 log["retries"]；失败时 log["error"] 为原因（日志文件由调用方写）。
    """
    retries: list[dict] = []
    for attempt in range(1 + MAX_RETRIES):
        if is_cancelled(request_id):
            log.update(error="已取消", retries=retries)
            raise client.LLMError("已取消")
        if attempt and retries[-1]["kind"] == "http":
            time.sleep(min(2 * attempt, 8))
        try:
            res = client.chat(p, m, level, system, parts, schema)
        except client.LLMError as e:
            if not e.retryable:
                log.update(error=str(e), retries=retries)
                raise
            retries.append({"attempt": attempt + 1, "kind": "http", "error": str(e)})
            continue
        log.update(request=res["request"], attempts=res["attempts"], response_text=res["text"],
                   elapsed=round(res["elapsed"], 2))
        if res["level_note"]:
            log["level_note"], log["level"] = res["level_note"], res["level"]
        level = res["level"]            # 档位被自动改掉后，重试沿用新档位
        raw, err, kind = check(res["text"])
        if not err:
            if retries:
                log["retries"] = retries
            return raw, res
        retries.append({"attempt": attempt + 1, "kind": kind, "error": err, "text": res["text"][:200]})
    last = retries[-1]
    log.update(error=f"重试 {MAX_RETRIES} 次仍失败：{last['error']}", retries=retries)
    raise client.LLMError(f"已自动重试 {MAX_RETRIES} 次仍失败。最后一次的错误：{last['error']}。{_TIPS[last['kind']]}")


def notes(log: dict) -> list[str]:
    """给用户的提示：思考档位被自动改掉、重试后才成功。"""
    out = [log["level_note"]] if log.get("level_note") else []
    if r := log.get("retries"):
        out.append(f"出错后自动重试了 {len(r)} 次才成功（第一次的错误：{r[0]['error'][:80]}）")
    return out
