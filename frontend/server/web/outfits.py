"""服装库：Danbooru 单独建的服装标签（普通标签，不挂在角色下，几个角色共用，如 mischievous_straight_uniform_(blue_archive)）。

- 作品服装目录（只是缓存，不进服装库）：按作品取（3–5 次请求，CATALOG_DAYS 天内不重取）——标签括号里写着这个作品、
  名字或 wiki 第一句看得出是服装的普通标签，连同 wiki（别名、简介、里面提到的角色）；图片 MIN_POSTS 张以上的
  常见标签与常穿的角色每套再查 1 次（后台），也记在目录里，供 AI 填写的可选项用。
- 服装库只存用到的那套（用户定的）：名字对上的、点名查到的、AI 从可选项里选用的、在服装库里添加 / 导入的。
- 什么时候取：角色存入 / 用到时随造型变体在后台一起补（variant_cache）；AI 填写写了服装、变体没对上、
  而这个作品的目录从没取过时当场取。
- 认服装（AI 填写）：描述的服装写法里整个出现了这个作品某套服装的名字（名称、名称映射、Danbooru 别名、标签去掉作品与
  uniform 这类泛称）才认定；标签写法撞了常见普通标签的（china_dress_(love_live!)）不拿标签写法认。
  对不上时把这个作品的服装（这个角色穿过的在前）作为可选项附给填写的 LLM：只有描述明确指那一套时才用，
  只说了一类服装（「偶像服」）或要新设计时按通用标签写（规则在 fill/system/web_character.md）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata

from . import danbooru
from .danbooru import _VARIANT2
from .library import OUTFITS
from .net import WebError

MIN_POSTS = 20        # 与造型变体一致：图片太少的共现标签不准，后台不补
CATALOG_DAYS = 30
OPTIONS = 8           # 对不上名字时最多给 LLM 几套

log = logging.getLogger("frontend.outfits")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

# 服装的泛称：只写这些词不算点名某套服装（「uniform」不能对上所有制服）
_GENERIC = {"uniform", "uniforms", "outfit", "outfits", "costume", "costumes", "clothes", "clothing", "dress", "swimsuit",
            "attire", "wear", "suit", "school", "set"}


def _key(text: str) -> str:
    """比对用：全半角统一、不分大小写、去掉空格与符号（イタズラ☆ストレート = イタズラストレート）。"""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text or "").casefold())


def _text_key(text: str) -> str:
    """描述里的服装写法：英文去掉泛称（mischievous straight uniform → mischievousstraight）。"""
    return _key(" ".join(w for w in re.split(r"[\s_]+", text or "") if w.casefold() not in _GENERIC))


def _phrase(tag: str) -> str:
    return re.sub(r"_\([^()]+\)$", "", tag).replace("_", " ")


def series_key(ch: dict) -> str:
    """角色所在作品的服装目录键：标签括号里的作品（aris_(blue_archive) → blue_archive、kaga_(kancolle) → kancolle），
    没有括号时用角色的作品标签（hatsune_miku → vocaloid）。"""
    m = re.search(r"_\(([^()]+)\)$", ch.get("tag") or "")
    return m.group(1) if m else (ch.get("series_tag") or "")


def cues(row: dict, generic: set[str] = frozenset()) -> set[str]:
    """一套服装的名字：名称、名称映射、Danbooru 别名、标签（去掉作品括号；再去掉泛称后还剩两个词以上的也算）。

    generic：去掉作品括号后撞了常见普通标签的服装（china_dress_(love_live!)），这个写法（以及等于它的名称）不算名字，
    只认别名和名称映射里别的写法——「china dress」多半是泛指旗袍，不是 Love Live! 那一套。
    """
    words = _phrase(row["tag"]).split()
    core = [w for w in words if w not in _GENERIC]
    plain = {_key(" ".join(words)), _key(" ".join(core))} if row["tag"] in generic else set()
    out = {row.get("name", ""), *row.get("names", []), *row.get("other_names", []), " ".join(words)}
    if len(core) >= 2:
        out.add(" ".join(core))
    skip = _key(row.get("series_tag", ""))
    keys = set()
    for c in out:
        k = _key(c)
        if k and k != skip and k not in plain and len(k) >= (4 if k.isascii() else 3) and len(k) <= 40:
            keys.add(k)
    return keys


def _hit(cue: str, text: str, partial: bool = False) -> bool:
    """名字整个出现在描述的服装写法里才算；partial（服装库的模糊搜索）：只写了名字的一部分也算。

    AI 填写不认「只写一部分」：cheerleader 会撞上 millennium cheerleader outfit，交给 LLM 按可选项判断。
    """
    return cue in text or (partial and len(text) >= (3 if text.isascii() else 2) and text in cue)


def wear(row: dict, tag: str) -> float:
    """这个角色穿这套服装的比例（造型变体算它的本体；只在 wiki 里提到的算 0.01），没穿过为 0。"""
    best = 0.0
    for m in row.get("members") or []:
        v = _VARIANT2.match(m["tag"])
        if tag in (m["tag"], f"{v.group(1)}_{v.group(2)}" if v else ""):
            best = max(best, m["freq"] if m.get("freq") is not None else 0.01)
    return best


# ───────────── 作品服装目录 ─────────────

def _lock(series: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(series, threading.Lock())


def ensure_series(series: str, profiles: bool = False) -> None:
    """作品服装目录：没取过或过期了才联网（3–5 次请求）。目录只是缓存，不往服装库里存。

    profiles（后台用）：目录里图片够、还没有常见标签的逐套补齐（每套 1 次请求，记在目录里），
    以及服装库里这个作品还没补齐的条目。
    """
    if not series:
        return
    with _lock(series):
        cat = OUTFITS.catalog(series)
        if not cat or time.time() - (cat["fetched_at"] or 0) > CATALOG_DAYS * 86400:
            OUTFITS.set_catalog(series, danbooru.series_outfits(series))
    if profiles:
        for x in (OUTFITS.catalog(series) or {}).get("items") or []:
            if x["post_count"] >= MIN_POSTS and not x.get("profiled_at"):
                prof = danbooru.outfit_profile(x["tag"], x["post_count"], page={"other_names": [], "body": ""})
                OUTFITS.update_catalog_item(series, x["tag"], {"appearance": prof["appearance"],
                                                               "related_members": prof["members"],
                                                               "profiled_at": time.time()})
        for row in OUTFITS.of_series(series):
            if row["fetched_at"] is None and row["source"] == "danbooru":
                save_profile(row)


def _members(x: dict) -> list[dict]:
    """目录条目的常穿角色：共现（有比例）+ wiki 里提到的（比例为 None）。"""
    out = list(x.get("related_members") or [])
    have = {m["tag"] for m in out}
    return out + [{"tag": t, "freq": None} for t in x.get("members") or [] if t not in have]


def _save_item(x: dict, series: str) -> dict:
    """目录里的一套存入服装库（用到时才存）；后台已补好的常见标签一起存，没补好的之后再补。"""
    return OUTFITS.save_from_danbooru({**x, "series_tag": series, "appearance": x.get("appearance") or [],
                                       "members": _members(x), "fetched_at": x.get("profiled_at")}, _phrase(x["tag"]), [])


def _unsaved(series: str, have: set[str]) -> list[dict]:
    """目录里还没存入服装库的，按服装库条目的样子给出（id 为 None，item 是目录条目）。"""
    return [{"id": None, "tag": x["tag"], "name": _phrase(x["tag"]), "names": [], "series_tag": series,
             "post_count": x["post_count"], "other_names": x.get("other_names") or [], "wiki": x.get("wiki") or "",
             "appearance": x.get("appearance") or [], "members": _members(x), "item": x, "edited": []}
            for x in (OUTFITS.catalog(series) or {}).get("items") or [] if x["tag"] not in have]


def save_used(o: dict) -> dict:
    """用到了这套（名字对上，或 AI 从可选项里选用）：还没在服装库里就存入；常见标签没补好的在后台补。"""
    if o.get("id"):
        return OUTFITS.get(o["id"]) or o
    row = OUTFITS.get_by_tag(o["tag"]) or _save_item(o["item"], o["series_tag"])
    if row["fetched_at"] is None:
        def later():
            try:
                save_profile(row)
            except WebError as e:
                log.info("服装库：取 %s 的常见标签失败（%s），下次再补", row["tag"], e)
        threading.Thread(target=later, name="outfit-profile", daemon=True).start()
    log.info("服装库：用到了 %s，已存入", row["tag"])
    return row


def save_profile(row: dict) -> dict:
    """补齐一套服装的常见标签与常穿的角色（1 次请求；wiki 与别名沿用目录里取到的）。"""
    prof = danbooru.outfit_profile(row["tag"], row["post_count"], page={"other_names": row["other_names"], "body": ""})
    have = {m["tag"] for m in prof["members"]}
    prof["members"] += [m for m in row["members"] if m["tag"] not in have]
    prof["wiki"] = prof["wiki"] or row["wiki"]
    return OUTFITS.save_from_danbooru(prof, "", [])


def fetch_profile(tag: str, post_count: int | None = None) -> dict:
    """服装库「添加 / 导入 / 重新获取」用的完整资料（wiki + 共现，2 次请求）；作品目录里有这一套时作品沿用目录。"""
    prof = danbooru.outfit_profile(tag, post_count)
    m = re.search(r"_\(([^()]+)\)$", tag)
    if m and OUTFITS.catalog(m.group(1)):
        prof["series_tag"] = m.group(1)
    return prof


# ───────────── AI 填写：认服装 ─────────────

def pick(base: dict, outfit: str, texts: list[str], fetch: bool = True) -> tuple[dict | None, list[dict]]:
    """角色（本体，没有对上造型变体）穿的作品服装 → (名字对上的服装库条目, 对不上时交给 LLM 的可选服装)。

    texts：服装原文；没写服装时是描述里这个角色所在的那几句（只认名字，不给可选项）。
    fetch：这个作品的目录从没取过时当场取（只在写了服装时）。
    比对的是服装库里这个作品的 + 目录里的；名字对上就存入服装库（用到了），对不上的进可选项（AI 选用了才存，见 save_used）。
    """
    s = series_key(base)
    if not s or not texts:
        return None, []
    if fetch and outfit and not OUTFITS.catalog(s):
        try:
            ensure_series(s)
        except WebError as e:
            log.info("服装库：取 %s 的服装目录失败（%s）", s, e)
    rows = OUTFITS.of_series(s)
    rows += _unsaved(s, {r["tag"] for r in rows})
    keys = [k for k in (_text_key(t) for t in texts) if k]     # 只写了 uniform 这类泛称时没有名字可比
    generic = OUTFITS.generic_tags()
    best, best_len = None, 0
    for row in rows:
        for cue in cues(row, generic):
            if any(_hit(cue, k) for k in keys) and len(cue) > best_len:
                best, best_len = row, len(cue)
    if best:
        if best["id"] is None:
            best = _save_item(best["item"], s)
            if best["fetched_at"] is None:
                try:
                    best = save_profile(best)
                except WebError as e:
                    log.info("服装库：取 %s 的常见标签失败（%s），下次再补", best["tag"], e)
        return best, []
    if not outfit:
        return None, []
    rows.sort(key=lambda r: (-wear(r, base["tag"]), -r["post_count"]))
    return None, rows[:OPTIONS]


def resolve(q: dict) -> tuple[dict | None, str]:
    """识别出的作品服装（{name, name_en, series, wearer}，不管谁穿）→ (服装库条目, 联网出错信息)。

    先查服装库：名称映射精确命中，或名字整个出现在写法里（有作品时只认这个作品的）；
    再查 Danbooru（不要求标签带作品括号，但要属于这个作品），存入服装库，原文写法记进名称映射，下次直接命中。
    """
    names = [n for n in (q["name"], q["name_en"]) if n]
    for n in names:
        row = OUTFITS.lookup(n)
        if row:
            return OUTFITS.get(row["id"]), ""
    s = danbooru._skey(q.get("series", ""))
    texts = [k for k in (_text_key(n) for n in names) if k]
    generic = OUTFITS.generic_tags()
    best, best_len = None, 0
    for row in OUTFITS.all_rows():
        if s and s.isascii() and row["series_tag"] and not danbooru._same_series(s, row["series_tag"]):
            continue
        for cue in cues(row, generic):
            if any(cue in t for t in texts) and len(cue) > best_len:
                best, best_len = row, len(cue)
    if not best:
        try:
            cands = danbooru.find_named_outfit(q["name"], q["name_en"], q.get("series", ""))
        except WebError as e:
            return None, str(e)
        if not cands:
            return None, ""
        c = cands[0]
        best = OUTFITS.get_by_tag(c["tag"])
        if not best:
            try:
                prof = fetch_profile(c["tag"], c["post_count"])
            except WebError as e:
                return None, str(e)
            prof["series_tag"] = c["series_tag"] or prof["series_tag"]
            best = OUTFITS.save_from_danbooru(prof, "", [])
            log.info("服装库：「%s」→ %s，已存入", q["name"] or q["name_en"], c["tag"])
    if q["name"]:
        OUTFITS.add_names(best["id"], [q["name"]])
    return OUTFITS.get(best["id"]), ""


def scan(description: str, skip: set[int]) -> list[dict]:
    """描述里直接写了服装库里某套服装的名字（没有跟着角色，如「穿イタズラ☆ストレート的女孩」）。只查本地。"""
    d = _key(description)
    if not d:
        return []
    generic = OUTFITS.generic_tags()
    out = [row for row in OUTFITS.all_rows()
           if row["id"] not in skip and any(len(c) >= 4 and c in d for c in cues(row, generic))]
    return sorted(out, key=lambda r: -r["post_count"])[:3]


# ───────────── 服装库「模糊搜索」 ─────────────

def fuzzy_search(name: str, series: str) -> dict:
    """服装库的模糊搜索：服装名（中日英文，可以只写一部分）和作品都可选，至少填一个。

    - 服装库里名字对上的（不联网，排最前）；
    - 填了作品：这个作品的服装目录（名字对上的在前，其余照列，供按意思挑——「偶像服」对不上任何名字时列出全部）；
    - 填了名字：Danbooru 按名字查（补全、标签名包含名字、wiki 别名包含名字），不要求作品。
    结果 series_match：标签括号里的作品对得上为 True、括号是别的作品为 False、标签没写作品为 None（如 hatsuboshi_gakuen_school_uniform）。
    """
    keys = danbooru.series_keys(series) if series.strip() else []
    s = keys[-1] if keys else ""
    k = _key(name)
    items: dict[str, dict] = {}
    errors = []

    def smatch(tag: str, row_series: str = "") -> bool | None:
        if not keys:
            return None
        if row_series:
            return any(danbooru._same_series(row_series, x) for x in keys)
        return True if danbooru._series_hit(tag, keys) else (False if re.search(r"_\([^()]+\)$", tag) else None)

    if k:
        for row in OUTFITS.all_rows():
            if any(_hit(c, k, partial=True) for c in cues(row)):
                items[row["tag"]] = {"tag": row["tag"], "post_count": row["post_count"], "matched": row["name"],
                                     "exact": True, "local": True, "series_match": smatch(row["tag"], row["series_tag"]),
                                     "desc": brief(row["wiki"], 140)}
    if s:
        try:
            ensure_series(s)       # 只列出，选中的那套由 /import 存入
        except WebError as e:
            errors.append(str(e))
        lib = {r["tag"]: r for r in OUTFITS.of_series(s)}
        cat = (OUTFITS.catalog(s) or {}).get("items") or []
        for x in [*cat, *(r for t, r in lib.items() if t not in {c["tag"] for c in cat})]:
            if x["tag"] in items:
                continue
            row = lib.get(x["tag"]) or {**x, "name": "", "names": [], "series_tag": s}
            items[x["tag"]] = {"tag": x["tag"], "post_count": x.get("post_count") or 0, "matched": "",
                               "exact": bool(k) and any(_hit(c, k, partial=True) for c in cues(row)),
                               "series_match": True, "desc": brief(row.get("wiki", ""), 140)}
    if k:
        try:
            for c in danbooru.search_outfits(name):
                if c["tag"] not in items:
                    items[c["tag"]] = {"tag": c["tag"], "post_count": c["post_count"], "matched": c.get("matched") or "",
                                       "exact": True, "series_match": smatch(c["tag"]), "desc": ""}
        except WebError as e:
            errors.append(str(e))
    if errors and not items:
        raise WebError(errors[0])
    out = sorted(items.values(), key=lambda c: (not c["exact"], c["series_match"] is False, c["series_match"] is not True,
                                                not c.get("local"), -c["post_count"]))
    have = OUTFITS.tags_present(c["tag"] for c in out)
    return {"items": [{**c, "in_library": c["tag"] in have} for c in out[:30]], "series_tags": keys,
            "offline": errors[0] if errors else None}


def brief(wiki: str, limit: int) -> str:
    text = " ".join((wiki or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > limit * 0.4 else cut.rstrip()) + ("" if dot > limit * 0.4 else " …")
