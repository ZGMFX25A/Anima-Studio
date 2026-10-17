"""人设的 AI 处理：从 Danbooru 导入已有角色并拆分成 基础外观 / 造型；按补充描述整理人设。

提示词全部在 prompts/persona/，每次请求重新读取。完整请求写入 logs/llm_last_request.json。
"""
from __future__ import annotations

import json
import re
import time

from .. import config, gallery, personas
from ..personas import PersonaError, clean_tags, split_list, tag_set, tag_text
from ..web import danbooru, research, tagsearch, variant_cache
from ..web.library import CHARACTERS
from ..web.net import WebError
from . import client, retry, store
from .fill import _write_log

MAX_VARIANTS = 6
FALLBACK_FREQ = 0.3        # 变体造型兜底：取该变体共现比例 ≥ 30% 的标签


def _read(name: str) -> str:
    return (config.PROMPTS_DIR / "persona" / name).read_text(encoding="utf-8").strip()


def _render(text: str, values: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


def _drop_empty_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not re.search(r"[：:]\s*$", line))


def _labels() -> dict:
    return json.loads(_read("labels.json"))


def _model(req: dict) -> tuple[dict, dict, str]:
    p, m = store.find_model(req.get("provider_id", ""), req.get("model_id", ""))
    if not p or not m:
        raise client.LLMError("所选 LLM 模型不存在，请在 菜单 → LLM 中勾选模型")
    levels = (m.get("thinking") or {}).get("levels") or ["off"]
    level = req.get("level") or m.get("level") or levels[0]
    return p, m, level if level in levels else levels[0]


def _fmt(general: list[tuple[str, float]]) -> str:
    return ", ".join(f"{tag_text(t)}（{round(f * 100)}%）" for t, f in general)


def _call(p: dict, m: dict, level: str, system: str, parts: list[dict], log: dict, request_id: str) -> dict:
    """带自动重试（见 retry.py）的一次拆分 / 整理请求。"""
    schema = json.loads(_read("schema.json"))
    log.update(provider=p["name"], format=p["format"], model=m["id"], level=level, system=system,
               user_text=parts[-1]["text"])
    try:
        raw, _ = retry.chat(p, m, level, system, parts, schema, retry.json_check(("base_tags", "outfits")), log,
                            request_id)
    except client.LLMError:
        _write_log(log)
        raise
    return raw


def _tags(v) -> str:
    """LLM 给出的标签（字符串或数组）→ 规范写法。"""
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v)
    return clean_tags([tag_text(t) for t in split_list(str(v or ""))])


def _normalize(raw: dict, allowed_tags: set[str] | None) -> dict:
    """allowed_tags：造型 character_tag 允许的取值（下划线写法）；None 表示保持 LLM 给的原值。"""
    base = _tags(raw.get("base_tags"))
    base_set = tag_set(base)
    outfits, used = [], set(base_set)
    for o in raw.get("outfits") or []:
        if not isinstance(o, dict):
            continue
        tags = clean_tags(_tags(o.get("tags")), exclude=base_set)
        used |= tag_set(tags)
        ct = tag_text(str(o.get("character_tag") or ""))
        if allowed_tags is not None and ct.replace(" ", "_") not in allowed_tags:
            ct = ""
        outfits.append({"name": str(o.get("name") or "").strip(), "tags": tags, "desc": str(o.get("desc") or "").strip(),
                        "character_tag": ct, "default": bool(o.get("default"))})
    unassigned = [t for t in split_list(_tags(raw.get("unassigned"))) if t.casefold() not in used]
    return {"display_name": str(raw.get("display_name") or "").strip(), "count": _tags(raw.get("count")),
            "base_tags": base, "appearance_desc": str(raw.get("appearance_desc") or "").strip(),
            "outfits": personas.clean_outfits(outfits), "unassigned": unassigned}


# ───────────────────────────── Danbooru 导入 ─────────────────────────────

def search(q: str, series: str = "", fuzzy: bool = False) -> list[dict]:
    """角色候选。默认：Danbooru 补全（标签、中日文名、别名、旧名都能命中）；
    fuzzy：模糊搜索（名字可只写一部分 + 作品，见 danbooru.search_characters）。"""
    q = (q or "").strip().lstrip("@")
    if not q:
        return []
    if fuzzy:
        cands = danbooru.search_characters(q, series)["items"]
    else:
        cands = danbooru.autocomplete(q)
        if not cands and " " in q:
            cands = danbooru.autocomplete(q.replace(" ", "_"))
    out = []
    for c in cands:
        lib = CHARACTERS.get_by_tag(c["tag"])
        out.append({**c, "text": tag_text(c["tag"]), "in_library": bool(lib),
                    "series": tag_text(lib["series_tag"]) if lib else ""})
    return out


def variants(tag: str, refresh: bool = False) -> dict:
    """造型变体列表：本体在角色库且缓存过列表时直接用缓存（refresh 时重新联网）；in_library = 该变体已缓存。"""
    lib = CHARACTERS.get_by_tag(tag)
    if lib and lib["variants_at"] and not refresh:
        lst, at = lib["variants"], lib["variants_at"]
    elif lib and not lib["parent_tag"]:
        lst, at = variant_cache.refresh_list(lib), time.time()
    else:
        lst, at = danbooru.variants(tag), None
    have = CHARACTERS.tags_present(v["tag"] for v in lst)
    return {"items": [{"tag": v["tag"], "post_count": v["post_count"], "text": tag_text(v["tag"]),
                       "in_library": v["tag"] in have} for v in lst], "cached_at": at}


def _source(tag: str, display: str) -> dict:
    """角色资料：角色库有缓存时只联网取一次共现标签（失败则用缓存的外貌标签）；没有则完整检索并存入角色库。
    本体与其它变体随后在后台补齐（见 web/variant_cache.py）。"""
    lib = CHARACTERS.get_by_tag(tag)
    if lib:
        try:
            general = danbooru.general_tags(tag, 100)
        except WebError:
            general = [(a["tag"], a["freq"] or 0.0) for a in lib["appearance"]]
        return {"tag": tag, "series_tag": lib["series_tag"], "wiki": lib["wiki"], "other_names": lib["other_names"],
                "post_count": lib["post_count"], "notes": lib["notes"], "general": general, "library": "cached"}
    prof = danbooru.profile(tag, general_limit=100)
    if not prof["post_count"] and not prof["general"] and not prof["wiki"]:
        raise PersonaError(f"Danbooru 上找不到角色标签 {tag}")
    CHARACTERS.save_from_danbooru(prof, display or tag_text(tag), [display] if display else [])
    return {**prof, "notes": "", "library": "saved"}


def _variant_source(v: str, parent: dict | None, post_count) -> tuple[dict, bool]:
    """勾选的变体，与本体同一规则：角色库有就只联网取一次共现标签（失败用缓存的外貌标签）；
    没有就完整取一遍并存入角色库（明确用到，不受图片数门槛限制）。返回 (资料, 是否新存入)。WebError 由调用方处理。"""
    lv = CHARACTERS.get_by_tag(v)
    if lv:
        try:
            general = danbooru.general_tags(v, 60)
        except WebError:
            general = [(a["tag"], a["freq"] or 0.0) for a in lv["appearance"]]
        return {"tag": v, "general": general, "wiki": lv["wiki"], "notes": lv["notes"],
                "post_count": lv["post_count"]}, False
    if parent:
        prof, _ = variant_cache.save_variant(parent, v, post_count)
        saved = True
    else:    # 本体没能存进角色库（理论上不会）：只取资料不保存
        prof, saved = danbooru.profile(v, post_count), False
    return {"tag": v, "general": prof["general"], "wiki": prof["wiki"], "notes": "",
            "post_count": prof["post_count"]}, saved


def import_character(req: dict) -> dict:
    rid = str(req.get("request_id") or "")
    try:
        return _import_character(req, rid)
    finally:
        retry.clear(rid)


def _import_character(req: dict, rid: str) -> dict:
    t0 = time.time()
    tag = (req.get("tag") or "").strip().replace(" ", "_")
    if not tag:
        raise PersonaError("请先选择一个角色")
    split = req.get("split", True)
    warnings: list[str] = []
    src = _source(tag, (req.get("name") or "").strip())
    picked = [v.strip().replace(" ", "_") for v in (req.get("variants") or []) if isinstance(v, str) and v.strip()]
    if len(picked) > MAX_VARIANTS:
        warnings.append(f"造型变体最多 {MAX_VARIANTS} 个，只用了前 {MAX_VARIANTS} 个")
        picked = picked[:MAX_VARIANTS]
    counts = {x["tag"]: x["post_count"] for x in (req.get("variant_counts") or []) if isinstance(x, dict) and x.get("tag")}
    parent = CHARACTERS.get_by_tag(tag)
    var_data, var_saved = [], 0
    for v in picked:
        try:
            d, saved = _variant_source(v, parent, counts.get(v))
            var_saved += saved
        except WebError as e:
            warnings.append(f"变体 {tag_text(v)} 的资料获取失败：{e}")
            d = {"tag": v, "general": [], "wiki": "", "notes": "", "post_count": counts.get(v)}
        var_data.append(d)
    variant_cache.schedule(tag)     # 没勾选的变体（图片数够的）在后台缓存

    labels = _labels()
    log: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "persona_import", "tag": tag,
                 "variants": picked, "library": src["library"]}
    if split:
        p, m, level = _model(req)
        var_text = "\n\n".join(_render(_read("split_variant_item.md"), {
            "tag": tag_text(v["tag"]), "post_count": v["post_count"] or "—",
            "wiki": " ".join((v["wiki"] or "").split()), "notes": " ".join((v["notes"] or "").split()),
            "tags": _fmt(v["general"]) or labels["empty"]}) for v in var_data)
        user = _drop_empty_lines(_render(_read("split_user.md"), {
            "tag": tag_text(tag), "series": tag_text(src["series_tag"]), "post_count": src["post_count"] or "",
            "other_names": "、".join(src["other_names"][:12]), "wiki": " ".join(src["wiki"].split()),
            "notes": " ".join((src.get("notes") or "").split()), "tags": _fmt(src["general"]),
            "variants": var_text or labels["no_variants"],
            "description": (req.get("description") or "").strip() or labels["empty"]}))
        raw = _call(p, m, level, _read("split_system.md"), [{"type": "text", "text": user}], log, rid)
        warnings += retry.notes(log)
        out = _normalize(raw, {v["tag"] for v in var_data})
    else:
        out = {"display_name": "", "count": "", "base_tags": "", "appearance_desc": "", "outfits": [],
               "unassigned": [tag_text(t) for t, f in src["general"] if f >= 0.1]}

    # 勾选的变体都要成为一套造型：LLM 漏掉的，用该变体的高比例标签兜底（去掉角色本体的高频标签，如发色、光环）
    base_set = tag_set(out["base_tags"]) | {tag_text(t).casefold() for t, f in src["general"] if f >= 0.5}
    have = {o["character_tag"].replace(" ", "_") for o in out["outfits"]}
    for v in var_data:
        if v["tag"] in have:
            continue
        tags = clean_tags([tag_text(t) for t, f in v["general"] if f >= FALLBACK_FREQ], exclude=base_set)
        out["outfits"].append({"name": danbooru.qualifier(v["tag"], tag), "tags": tags, "desc": "",
                               "character_tag": tag_text(v["tag"]), "default": False})
        if split:
            warnings.append(f"AI 没有给变体 {tag_text(v['tag'])} 单独列造型，已按共现标签自动补上")
    out["outfits"] = personas.clean_outfits(out["outfits"])

    # 别名：Danbooru 的 other_names 用下划线代替空格；单个英文词（aris、alice）太常见，容易误匹配，不收
    aliases, seen = [], set()
    for n in src["other_names"]:
        n = n.replace("_", " ").strip()
        k = personas.norm(n)
        if len(k) < 2 or k in seen or (n.isascii() and " " not in n):
            continue
        seen.add(k)
        aliases.append(n)
    aliases = aliases[:10]
    draft = {"kind": "existing", "name": out["display_name"], "character_tag": tag_text(tag),
             "series": tag_text(src["series_tag"]), "count": out["count"] or "1girl", "aliases": aliases,
             "base_tags": out["base_tags"], "appearance_desc": out["appearance_desc"], "outfits": out["outfits"],
             "source_tag": tag}
    if split:
        log.update(result={**draft, "unassigned": out["unassigned"]}, warnings=warnings)
        _write_log(log)
    return {"draft": draft, "unassigned": out["unassigned"], "warnings": warnings, "post_count": src["post_count"],
            "library": src["library"], "variants_saved": var_saved, "elapsed": time.time() - t0}


# ───────────────────────────── AI 整理 ─────────────────────────────

def refine(req: dict) -> dict:
    rid = str(req.get("request_id") or "")
    try:
        return _refine(req, rid)
    finally:
        retry.clear(rid)


def _refine(req: dict, rid: str) -> dict:
    t0 = time.time()
    p, m, level = _model(req)
    cur = req.get("persona") or {}
    labels = _labels()
    warnings: list[str] = []
    desc = (req.get("description") or "").strip()
    vision = m.get("vision") is True
    imgs = [h for h in (req.get("images") or []) if h][:3]
    if imgs and not vision:
        warnings.append("当前模型不支持图片，参考图未发送")
        imgs = []

    tags, tag_info = None, None
    if req.get("tag_reference") and desc:
        try:
            tags = tagsearch.search(desc, True)
            tag_info = {"count": len(tags["results"]), "cached": bool(tags.get("cached")),
                        "remaining": tags.get("remaining")}
        except WebError as e:
            warnings.append(f"标签参考不可用：{e}")

    shown = {k: cur.get(k, "") for k in ("name", "kind", "character_tag", "series", "count", "base_tags",
                                         "appearance_desc")}
    shown["outfits"] = [{k: o.get(k) for k in ("name", "tags", "desc", "character_tag", "default")}
                        for o in cur.get("outfits") or []]
    parts: list[dict] = []
    img_log = []
    for i, h in enumerate(imgs, 1):
        try:
            part = client.image_part(gallery.ref_bytes(h))
        except FileNotFoundError:
            warnings.append(f"参考图 {i} 文件已丢失，未发送")
            continue
        parts += [{"type": "text", "text": _render(labels["image"], {"n": i})}, part]
        img_log.append(f"参考图 {i} {part['size'][0]}x{part['size'][1]}")
    tb = research.tag_block(tags)
    if tb:
        parts.append({"type": "text", "text": tb})
    parts.append({"type": "text", "text": _render(_read("refine_user.md"), {
        "persona": json.dumps(shown, ensure_ascii=False, indent=2),
        "description": desc or labels["empty_description"]})})

    log: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "persona_refine", "images": img_log,
                 "references": [tb] if tb else []}
    raw = _call(p, m, level, _read("refine_system.md"), parts, log, rid)
    warnings += retry.notes(log)
    # 造型的专属角色标签只能沿用原值，不接受 LLM 新编的
    allowed = {o.get("character_tag", "").replace(" ", "_") for o in cur.get("outfits") or [] if o.get("character_tag")}
    out = _normalize(raw, allowed)
    # 保留原造型 id（按名称对应），前端据此保持预览选择
    old = {o.get("name"): o.get("id") for o in cur.get("outfits") or []}
    for o in out["outfits"]:
        if old.get(o["name"]):
            o["id"] = old[o["name"]]
    out["outfits"] = personas.clean_outfits(out["outfits"])
    log.update(result=out, warnings=warnings)
    _write_log(log)
    return {"result": {k: out[k] for k in ("count", "base_tags", "appearance_desc", "outfits")},
            "unassigned": out["unassigned"], "warnings": warnings, "tags": tag_info, "elapsed": time.time() - t0}
