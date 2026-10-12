"""LLM 辅助填写：组装提示词（全部来自 frontend/prompts/）、调用模型、校验结果。"""
from __future__ import annotations

import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .. import config, gallery, personas
from ..generation import FIELD_NAMES, load_json
from ..web import research, tagsearch
from ..web.net import WebError
from . import client, retry, store

SAFETY = ("safe", "sensitive", "nsfw", "explicit")


def _read(name: str) -> str:
    return (config.PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def _render(text: str, values: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def _tokens(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def _normalize(raw: dict, mode: str, safety: str, current: dict, presets: dict, warnings: list[str]) -> dict:
    out: dict = {}
    for k in FIELD_NAMES + ("aspect_ratio",):
        v = raw.get(k)
        if v is None:
            out[k] = current.get(k, "") if mode == "modify" else ""
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        out[k] = str(v).strip()
    # 画幅：只接受预设
    if out["aspect_ratio"] and out["aspect_ratio"] not in presets:
        warnings.append(f"LLM 给出的比例 {out['aspect_ratio']} 不在预设中，已忽略")
        out["aspect_ratio"] = current.get("aspect_ratio", "") if mode == "modify" else ""
    # 画师：每项以 @ 开头
    if out["artist"]:
        out["artist"] = ", ".join(t if t.startswith("@") else "@" + t for t in _tokens(out["artist"]))
    # 安全标签
    q = _tokens(out["quality_meta_year_safe"])
    if safety in SAFETY:
        q = [t for t in q if t.lower() not in SAFETY] + [safety]
        neg = [t for t in _tokens(out["neg"]) if t.lower() != safety]
        out["neg"] = ", ".join(neg)
    else:
        found = [t for t in q if t.lower() in SAFETY]
        if len(found) > 1:
            kept, seen = [], False
            for t in q:
                if t.lower() in SAFETY:
                    if seen:
                        continue
                    seen = True
                kept.append(t)
            q = kept
            warnings.append(f"LLM 给出了多个安全标签，已保留 {found[0]}")
        elif not found:
            warnings.append("LLM 没有给出安全标签")
    out["quality_meta_year_safe"] = ", ".join(q)
    return out


def fill(req: dict) -> dict:
    rid = str(req.get("request_id") or "")    # 页面「取消」用；一次填写可能包含识别角色 + 正式请求两次 LLM 调用
    try:
        return _fill(req, rid)
    finally:
        retry.clear(rid)


def _fill(req: dict, rid: str) -> dict:
    t_start = time.time()
    mode = "modify" if req.get("mode") == "modify" else "generate"
    p, m = store.find_model(req.get("provider_id", ""), req.get("model_id", ""))
    if not p or not m:
        raise client.LLMError("所选 LLM 模型不存在，请在 菜单 → LLM 中重新勾选")
    level = req.get("level") or m.get("level") or "off"
    levels = (m.get("thinking") or {}).get("levels") or ["off"]
    if level not in levels:
        level = levels[0]
    safety = req.get("safety") if req.get("safety") in SAFETY else ""
    vision = m.get("vision") is True
    current = req.get("current_fields") or {}
    presets = load_json("defaults.json")["aspect_presets"]
    labels = json.loads((config.PROMPTS_DIR / "fill" / "user" / "labels.json").read_text(encoding="utf-8"))
    warnings: list[str] = []

    user_imgs = [h for h in (req.get("user_images") or []) if h][:3]
    ch = req.get("character") or {}
    ch_imgs = [h for h in (ch.get("images") or []) if h][:3] if ch.get("enabled") else []
    po = req.get("pose") or {}
    po_img = po.get("image") if po.get("enabled") else None
    pose_type = labels.get("pose_input_type_skeleton" if po.get("input_type") == "skeleton"
                           else "pose_input_type_photo", "")

    values = {
        "safety": safety, "description": (req.get("description") or "").strip() or labels.get("empty_description", ""),
        "current_fields": json.dumps({k: current.get(k, "") for k in FIELD_NAMES + ("aspect_ratio",)},
                                     ensure_ascii=False, indent=2),
        "aspect_ratios": ", ".join(presets), "n_images": len(user_imgs), "pose_input_type": pose_type,
    }

    desc = (req.get("description") or "").strip()

    # —— 人设匹配（可关）：描述里出现人设的名称 / 别名，或在绘图页选用了人设；本地完成，不联网、不调用 LLM ——
    persona_entries: list[dict] = []
    if req.get("persona_match", True):
        persona_entries = [{**h, "outfit_id": None, "linked": False} for h in personas.match(desc)]
        link = req.get("persona_linked") or {}
        lp = personas.get(link["id"]) if isinstance(link.get("id"), int) else None
        if lp:
            hit = next((e for e in persona_entries if e["persona"]["id"] == lp["id"]), None)
            if hit:
                hit.update(outfit_id=link.get("outfit_id"), linked=True)
            else:
                persona_entries.append({"persona": lp, "matched": None, "outfit_id": link.get("outfit_id"),
                                        "linked": True})
    covered_names = {n for e in persona_entries for n in personas.names_norm(e["persona"])}
    covered_tags = {t for e in persona_entries for t in personas.tags_underscore(e["persona"])}

    # —— 联网检索（可选）：识别角色与画师 → 资料库 / Danbooru；标签参考 → DanbooruSearch，两者并行 ——
    want_web, want_tags = bool(req.get("web_character")), bool(req.get("tag_reference"))
    found, found_art, results, artists, tags, tag_error, research_log = [], [], [], [], None, None, {}
    if want_web or want_tags:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut = ex.submit(tagsearch.search, desc, safety != "safe") if want_tags and desc else None
            if want_web:
                try:
                    cur = current if mode == "modify" else {}
                    found, found_art, research_log["extract"] = research.extract(p, m, desc, cur, rid)
                    # 已由人设提供资料的角色不再联网检索
                    found = [q for q in found if not ({personas.norm(q["name"]), personas.norm(q["name_en"])}
                                                      & covered_names)]
                    results = [r for r in research.research_characters(found)
                               if not (r["character"] and r["character"]["tag"] in covered_tags)]
                    artists = research.research_artists(found_art)
                except (client.LLMError, WebError, ValueError) as e:
                    if retry.is_cancelled(rid):
                        raise client.LLMError("已取消") from e
                    warnings.append(f"识别角色 / 画师失败：{e}")
            if fut:
                try:
                    tags = fut.result()
                except WebError as e:
                    tag_error = str(e)
                    warnings.append(f"标签参考不可用：{e}")
            elif want_tags:
                tag_error = "描述为空，未检索"
        for kind, lst in (("角色", results), ("画师", artists)):
            for r in lst:
                who = r["query"]["name"] or r["query"]["name_en"]
                if r["status"] == "not_found":
                    warnings.append(f"未在 Danbooru 找到{kind}「{who}」")
                elif r["status"] == "error":
                    warnings.append(f"检索{kind}「{who}」失败：{r['error']}")
        brief = lambda lst: [{"query": r["query"], "status": r["status"], "tag": (r["character"] or {}).get("tag")}
                             for r in lst]
        research_log.update(found=found, found_artists=found_art, results=brief(results), artists=brief(artists),
                            tags=(tags or {}).get("results"), tag_error=tag_error)
    persona_text = personas.block(persona_entries)
    persona_info = [{"id": e["persona"]["id"], "name": e["persona"]["name"], "matched": e["matched"],
                     "linked": e["linked"],
                     "outfit": next((o["name"] for o in e["persona"]["outfits"] if o["id"] == e["outfit_id"]), None)}
                    for e in persona_entries]
    if persona_entries:
        research_log["personas"] = persona_info
    char_text = research.character_block(results, labels)
    artist_text = research.artist_block(artists)
    tag_text = research.tag_block(tags)

    # —— 系统提示词 ——
    sections = ["system.md", "fields.md", f"mode_{mode}.md", "safety_levels.md",
                "safety_fixed.md" if safety else "safety_auto.md"]
    if user_imgs:
        if vision:
            sections.append("user_images.md")
        else:
            warnings.append("当前模型不支持图片，灵感参考图未发送")
            user_imgs = []
    if ch_imgs:
        sections.append("ref_character.md" if vision else "ref_character_noimage.md")
    if po_img:
        sections.append("ref_pose.md" if vision else "ref_pose_noimage.md")
    if ch_imgs and po_img:
        sections.append("ref_both.md")
    if persona_text:
        sections.append("persona.md")
    if char_text:
        sections.append("web_character.md")
    if artist_text:
        sections.append("web_artist.md")
    if tag_text:
        sections.append("tag_reference.md")
    system = "\n\n".join(_render(_read(f"fill/system/{s}"), values) for s in sections)

    # —— 用户消息：带标注的图片 + 描述 ——
    parts: list[dict] = []
    img_log: list[str] = []

    def add_image(label: str, h: str) -> None:
        try:
            part = client.image_part(gallery.ref_bytes(h))
        except FileNotFoundError:
            warnings.append(f"{label} 文件已丢失，未发送")
            return
        parts.append({"type": "text", "text": label})
        parts.append(part)
        img_log.append(f"{label} {part['size'][0]}x{part['size'][1]}")

    for i, h in enumerate(user_imgs, 1):
        add_image(_render(labels["user_image"], {"n": i}), h)
    if vision:
        for i, h in enumerate(ch_imgs, 1):
            add_image(_render(labels["character_image"], {"n": i}), h)
        if po_img:
            add_image(labels["pose_image"], po_img)
    blocks = (persona_text, char_text, artist_text, tag_text)
    for block in blocks:
        if block:
            parts.append({"type": "text", "text": block})
    parts.append({"type": "text", "text": _render(_read("fill/user/user_message.md"), values)})

    schema = json.loads((config.PROMPTS_DIR / "fill" / "schema.json").read_text(encoding="utf-8"))
    log: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": mode, "provider": p["name"],
                 "format": p["format"], "model": m["id"], "level": level, "safety": safety or "auto",
                 "vision": vision, "sections": sections, "images": img_log, "system": system,
                 "references": [b for b in blocks if b], "user_text": parts[-1]["text"],
                 "research": research_log or None}
    try:
        raw, _ = retry.chat(p, m, level, system, parts, schema, retry.json_check(FIELD_NAMES), log, rid)
    except client.LLMError:
        _write_log(log)
        raise
    warnings += retry.notes(log)
    fields = _normalize(raw, mode, safety, current, presets, warnings)
    # 命中的原创人物：名称 / 别名不能出现在 character 里
    fields["character"], removed = personas.strip_original_names(fields["character"], persona_entries)
    if removed:
        log["character_removed"] = removed
        warnings.append(f"原创人物的名称不是角色标签，已从 character 去掉：{'、'.join(removed)}")
    if not any(fields[k] for k in FIELD_NAMES if k not in ("quality_meta_year_safe", "neg")):
        warnings.append("LLM 返回的字段几乎全是空的，可能悄悄拒绝了这个请求，可以换个模型试试")
    log["result"] = fields
    log["warnings"] = warnings
    _write_log(log)
    return {"fields": fields, "warnings": warnings, "elapsed": time.time() - t_start, "mode": mode,
            "research": research.summary(results, artists, tags, tag_error, want_web, persona_info)
            if (want_web or want_tags or persona_info) else None}


def test(req: dict) -> dict:
    """测试按钮：文字连通 + 识图。只在用户点击时调用。"""
    p, m = store.find_model(req.get("provider_id", ""), req.get("model_id", ""))
    if not p or not m:
        raise client.LLMError("模型不存在")
    level = req.get("level") or m.get("level") or "off"
    t0 = time.time()
    result = {"text_ok": False, "vision": None, "answer": "", "error": None, "level_note": None}
    try:
        left, right = random.sample(list(client.PROBE_COLORS), 2)
        r = client.chat(p, m, level, "", [client.probe_image(left, right),
                                          {"type": "text", "text": _read("test/probe.md")}], None)
        level, result["level_note"] = r["level"], r["level_note"]
        ans = r["text"].strip().lower()
        result["text_ok"] = True
        result["answer"] = r["text"].strip()[:100]
        result["expected"] = f"{left} / {right}"
        il, ir = ans.find(left), ans.find(right)
        result["vision"] = il >= 0 and ir > il      # 两种颜色都答对且顺序正确
    except client.LLMError as e:
        result["error"] = str(e)
        try:  # 带图失败：再用纯文字确认连通性
            level = m.get("level") if level in (m.get("unsupported_levels") or []) else level
            r = client.chat(p, m, level, "", [{"type": "text", "text": "Reply with OK."}], None)
            level, result["level_note"] = r["level"], r["level_note"] or result.get("level_note")
            result["text_ok"] = True
            result["vision"] = False
            result["answer"] = r["text"].strip()[:100]
        except client.LLMError as e2:
            result["error"] = str(e2)
    result["elapsed"] = round(time.time() - t0, 2)
    patch = {"last_test": {**result, "time": time.time(), "level": level}}
    if result["vision"] is not None:
        patch.update(vision=result["vision"], vision_source="test")
    store.update_model(p["id"], m["id"], patch)
    return result


def _write_log(d: dict) -> None:
    try:
        config.LLM_LOG_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
