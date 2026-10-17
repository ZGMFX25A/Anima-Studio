"""LLM 辅助填写：组装提示词（全部来自 frontend/prompts/）、调用模型、校验结果。"""
from __future__ import annotations

import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .. import config, personas
from ..generation import FIELD_NAMES, load_json
from ..web import research, tagsearch
from ..web.net import WebError
from . import client, retry, store
from .vision import describe as describe_images

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
    rid = str(req.get("request_id") or "")    # 页面「取消」用；一次填写可能包含识图、识别角色、正式请求多次 LLM 调用
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
    # 识图模型：不选 = 同上（AI 填写的模型与思考档）；单独选的模型用它在「菜单 → LLM」里设置的默认思考档
    vp, vm, vlevel = p, m, level
    v_ids = (req.get("vision_provider_id") or "", req.get("vision_model_id") or "")
    if v_ids[1] and v_ids != (p["id"], m["id"]):
        vp, vm = store.find_model(*v_ids)
        if not vp or not vm:
            raise client.LLMError("所选识图模型不存在，请在 菜单 → LLM 中重新勾选")
        vlevels = (vm.get("thinking") or {}).get("levels") or ["off"]
        vlevel = vm.get("level") if vm.get("level") in vlevels else vlevels[0]
    vision = vm.get("vision") is True
    vision_fresh = bool(req.get("vision_no_cache"))    # 页面「重新识图」：这一次不用识图缓存
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

    # —— 识图：识图模型先逐张详细描述图片，结果作为文字参考交给正式填写（正式填写不再收图片）；与联网检索并行 ——
    v_items: list[dict] = []
    if vision:
        v_items = ([{"kind": "inspiration", "label": _render(labels["user_image"], {"n": i}), "hash": h}
                    for i, h in enumerate(user_imgs, 1)]
                   + [{"kind": "character", "label": _render(labels["character_image"], {"n": i}), "hash": h}
                      for i, h in enumerate(ch_imgs, 1)]
                   + ([{"kind": "pose", "label": labels["pose_image"], "hash": po_img,
                        "input_type": po.get("input_type")}] if po_img else []))
    elif user_imgs:
        warnings.append("当前识图模型不支持图片，灵感参考图未发送")
    v_ex = ThreadPoolExecutor(max_workers=1)
    v_fut = v_ex.submit(describe_images, vp, vm, vlevel, v_items, rid, not vision_fresh) if v_items else None
    v_ex.shutdown(wait=False)

    # —— 人设匹配（可关）：描述里出现人设的名称 / 别名（已有角色另外认角色库里这个角色的全部名称），
    #    或在绘图页选用了人设；本地完成，不联网、不调用 LLM ——
    persona_entries: list[dict] = []
    persona_on = req.get("persona_match", True)
    if persona_on:
        persona_entries = [{**h, "outfit_id": None, "linked": False}
                           for h in personas.match(desc, extra_names=research.persona_names)]
        link = req.get("persona_linked") or {}
        lp = personas.get(link["id"]) if isinstance(link.get("id"), int) else None
        if lp:
            hit = next((e for e in persona_entries if e["persona"]["id"] == lp["id"]), None)
            if hit:
                hit.update(outfit_id=link.get("outfit_id"), linked=True)
            else:
                persona_entries.append({"persona": lp, "matched": None, "outfit_id": link.get("outfit_id"),
                                        "linked": True})
    def cover(p: dict) -> None:
        covered_names.update(personas.names_norm(p), (personas.norm(n) for n in research.persona_names(p)))
        covered_tags.update(personas.tags_underscore(p))

    covered_names: set[str] = set()
    covered_tags: set[str] = set()
    for e in persona_entries:
        cover(e["persona"])

    # —— 联网检索（可选）：识别角色与画师 → 资料库 / Danbooru；标签参考 → DanbooruSearch，两者并行 ——
    want_web, want_tags = bool(req.get("web_character")), bool(req.get("tag_reference"))
    found, found_art, results, artists, tags, tag_error, research_log = [], [], [], [], None, None, {}
    outfit_entries, outfit_misses, found_out = [], [], []
    if want_web or want_tags:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut = ex.submit(tagsearch.search, desc, safety != "safe") if want_tags and desc else None
            if want_web:
                try:
                    cur = current if mode == "modify" else {}
                    found, found_art, found_out, research_log["extract"] = research.extract(p, m, desc, cur, rid)
                    # 识别出的角色名与人设模糊比对上的（「优香」→ 人设「早濑优香」），也按人设处理
                    if persona_on:
                        plist = personas.all_full()
                        for q in found:
                            if {personas.norm(q["name"]), personas.norm(q["name_en"])} & covered_names:
                                continue
                            hit = research.fuzzy_persona(q, plist)
                            if hit and all(e["persona"]["id"] != hit["id"] for e in persona_entries):
                                persona_entries.append({"persona": hit, "matched": q["name"] or q["name_en"],
                                                        "outfit_id": None, "linked": False})
                                cover(hit)
                            if hit:
                                covered_names.update({personas.norm(q["name"]), personas.norm(q["name_en"])} - {""})
                    # 已由人设提供资料的角色不再联网检索
                    found = [q for q in found if not ({personas.norm(q["name"]), personas.norm(q["name_en"])}
                                                      & covered_names)]
                    results = [r for r in research.research_characters(found, desc)
                               if not (r["character"] and ({r["character"]["tag"], (r.get("base") or {}).get("tag")}
                                                           & covered_tags))]
                    artists = research.research_artists(found_art)
                    # 作品服装不管谁穿（人设、原创角色也算）都查，所以不受上面「已由人设覆盖」的影响
                    outfit_entries, outfit_misses = research.research_outfits(results, desc, found_out)
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
        for m_ in outfit_misses:
            who = m_["query"]["name"] or m_["query"]["name_en"]
            warnings.append(f"检索服装「{who}」失败：{m_['error']}" if m_["error"] else f"未在 Danbooru 找到服装「{who}」")
        for kind, lst in (("角色", results), ("画师", artists)):
            for r in lst:
                who = r["query"]["name"] or r["query"]["name_en"]
                if r["status"] == "not_found":
                    warnings.append(f"未在 Danbooru 找到{kind}「{who}」")
                elif r["status"] == "error":
                    warnings.append(f"检索{kind}「{who}」失败：{r['error']}")
        brief = lambda lst: [{"query": r["query"], "status": r["status"], "tag": (r["character"] or {}).get("tag"),
                              **({"variant_of": r["base"]["tag"]} if r.get("base") else {}),
                              **({"fuzzy": r["fuzzy"]} if r.get("fuzzy") else {}),
                              **({"options": [v["tag"] for v in r["options"]]} if r.get("options") else {}),
                              **({"outfit": r["outfit"]["tag"]} if r.get("outfit") else {}),
                              **({"outfit_options": [o["tag"] for o in r["outfit_options"]]}
                                 if r.get("outfit_options") else {})}
                             for r in lst]
        research_log.update(found=found, found_artists=found_art, results=brief(results), artists=brief(artists),
                            found_outfits=found_out,
                            outfits=[{"tag": e["outfit"]["tag"], "wearers": e["wearers"]} for e in outfit_entries],
                            outfit_misses=[m_["query"] for m_ in outfit_misses],
                            tags=(tags or {}).get("results"), tag_error=tag_error)
    persona_text = personas.block(persona_entries)
    persona_info = [{"id": e["persona"]["id"], "name": e["persona"]["name"], "matched": e["matched"],
                     "linked": e["linked"],
                     "outfit": next((o["name"] for o in e["persona"]["outfits"] if o["id"] == e["outfit_id"]), None)}
                    for e in persona_entries]
    if persona_entries:
        research_log["personas"] = persona_info
    char_text = research.character_block(results, labels)
    outfit_text = research.outfit_block(outfit_entries)
    artist_text = research.artist_block(artists)
    tag_text = research.tag_block(tags)

    # —— 识图结果：某张图识图失败时按「看不到图」处理 ——
    v_res = v_fut.result() if v_fut else []
    if retry.is_cancelled(rid):
        raise client.LLMError("已取消")
    for r in v_res:
        if r["error"]:
            warnings.append(f"识图失败（{r['label']}）：{r['error']}")
        elif r.get("retries"):
            warnings.append(f"识图（{r['label']}）出错后自动重试了 {len(r['retries'])} 次才成功")
        if r.get("level_note") and r["level_note"] not in warnings:
            warnings.append(r["level_note"])
    seen = {k: [r for r in v_res if r["kind"] == k and r["text"]] for k in ("inspiration", "character", "pose")}
    if v_res and not any(seen.values()) and not desc:
        raise client.LLMError(f"识图失败，且没有填写描述：{v_res[0]['error']}")
    if vm is m:      # 同一个模型：识图时档位被接口拒绝并自动改掉的话，正式填写直接用改后的档位
        level = next((r["level"] for r in v_res if r.get("level") and r["level"] != level), level)
    values["n_images"] = len(seen["inspiration"])
    vision_text = _vision_block([r for r in v_res if r["text"]])

    # —— 系统提示词 ——
    sections = ["system.md", "fields.md", f"mode_{mode}.md", "safety_levels.md",
                "safety_fixed.md" if safety else "safety_auto.md"]
    if seen["inspiration"]:
        sections.append("user_images.md")
    if ch_imgs:
        sections.append("ref_character.md" if seen["character"] else "ref_character_noimage.md")
    if po_img:
        sections.append("ref_pose.md" if seen["pose"] else "ref_pose_noimage.md")
    if ch_imgs and po_img:
        sections.append("ref_both.md")
    if persona_text:
        sections.append("persona.md")
    if char_text:
        sections.append("web_character.md")
    if outfit_text:
        sections.append("web_outfit.md")
    if artist_text:
        sections.append("web_artist.md")
    if tag_text:
        sections.append("tag_reference.md")
    system = "\n\n".join(_render(_read(f"fill/system/{s}"), values) for s in sections)

    # —— 用户消息：识图结果 + 各参考资料块 + 描述（只有文字，不发图片） ——
    parts: list[dict] = []
    img_log = [f"{r['label']} " + ("（缓存）" if r["cached"] else f"{r['size'][0]}x{r['size'][1]}" if r.get("size")
                                   else "（未发送）") for r in v_res]
    blocks = (vision_text, persona_text, char_text, outfit_text, artist_text, tag_text)
    for block in blocks:
        if block:
            parts.append({"type": "text", "text": block})
    parts.append({"type": "text", "text": _render(_read("fill/user/user_message.md"), values)})

    schema = json.loads((config.PROMPTS_DIR / "fill" / "schema.json").read_text(encoding="utf-8"))
    log: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": mode, "provider": p["name"],
                 "format": p["format"], "model": m["id"], "level": level, "safety": safety or "auto",
                 "vision": vision, "sections": sections, "images": img_log, "system": system,
                 "references": [b for b in blocks if b], "user_text": parts[-1]["text"],
                 "research": research_log or None,
                 "recognition": {"provider": vp["name"], "model": vm["id"], "level": vlevel, "no_cache": vision_fresh,
                                 "results": [{k: r.get(k) for k in ("label", "kind", "size", "cached", "elapsed",
                                                                     "error", "retries", "text")} for r in v_res]}
                 if v_res else None}
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
            "research": research.summary(results, artists, tags, tag_error, want_web, persona_info, outfit_entries,
                                         outfit_misses)
            if (want_web or want_tags or persona_info) else None,
            "recognition": {"model": vm.get("name") or vm["id"],
                            "items": [{k: r.get(k) for k in ("label", "kind", "text", "error", "cached", "elapsed")}
                                      for r in v_res]} if v_res else None}


def _vision_block(results: list[dict]) -> str:
    if not results:
        return ""
    items = [_render(_read("fill/user/vision_item.md"), {"label": r["label"], "text": r["text"]}) for r in results]
    return _render(_read("fill/user/vision_block.md"), {"items": "\n\n".join(items)})


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
