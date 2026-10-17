"""Danbooru 查询：角色名解析（补全接口支持中日文别名 / 旧名 / 单词匹配）、wiki 简介、共现外貌标签。"""
from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import httpx

from .. import config
from .net import WebError, client

BASE = "https://danbooru.donmai.us"
UA = "AnimaStudio/1.0 (local prompt helper)"
CAT_ARTIST, CAT_COPYRIGHT, CAT_CHARACTER = 1, 3, 4
MIN_INTERVAL = 0.35              # 请求间隔（Danbooru 建议持续访问约 1 次/秒以内，短时可更快）
WIKI_CHARS = 600
APPEARANCE_TOP = 25

_rate_lock = threading.Lock()
_last = [0.0]


def _get(path: str, params: dict | None = None, retries: int = 2, net_override: dict | None = None):
    """GET JSON；404 返回 None；5xx / 429（Danbooru 偶发超时）自动重试。"""
    for attempt in range(retries + 1):
        with _rate_lock:
            wait = _last[0] + MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            with client(20, net_override) as c:
                r = c.get(BASE + path, params=params, headers={"User-Agent": UA})
        except httpx.HTTPError as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise WebError(f"无法连接 Danbooru：{e}") from e
        if r.status_code == 404:
            return None
        if r.status_code == 200:
            return r.json()
        if (r.status_code >= 500 or r.status_code == 429) and attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            continue
        raise WebError(f"Danbooru 返回 HTTP {r.status_code}（{path}）")


def autocomplete(query: str, net_override: dict | None = None, category: int = CAT_CHARACTER) -> list[dict]:
    """某一类别的补全结果：[{tag, post_count, matched}]，tag 已是正式标签（别名、各语言别名已解析）。"""
    query = query.strip()
    if not query:
        return []
    d = _get("/autocomplete.json", {"search[query]": query, "search[type]": "tag_query", "limit": 10},
             net_override=net_override) or []
    out = []
    for x in d:
        if x.get("category") == category and x.get("value") and x["value"] not in (o["tag"] for o in out):
            out.append({"tag": x["value"], "post_count": x.get("post_count") or 0,
                        "matched": x.get("antecedent") or x.get("label") or ""})
    return out


def find_character(name: str, name_en: str, series: str) -> list[dict]:
    """AI 填写的联网检索：返回候选（最多 4 个，最可能的在前）。

    有作品时只认属于该作品的候选，宁可找不到也不认错（以前「晶」+ 碧蓝档案会认成原神的晶蝶）。
    """
    items = search_characters(name, series, name_en, full=False)["items"]
    return [c for c in items if c["series_match"] is not False][:4]


# ───────────── 角色模糊搜索（名字 + 作品） ─────────────

def _skey(text: str) -> str:
    return re.sub(r"[\s_]+", "_", unicodedata.normalize("NFKC", text or "").strip().lower())


def _same_series(a: str, b: str) -> bool:
    """作品写法是否对得上：blue_archive = blue_archive；fate ≈ fate_(series) ≈ fate/grand_order。"""
    return a == b or any(a.startswith(b + x) or b.startswith(a + x) for x in ("_", "/"))


def series_keys(series: str) -> list[str]:
    """作品（任意语言）→ 比对用的作品关键字：英文写法本身 + Danbooru 作品标签（补全能解析中日文名、别名）。"""
    series = (series or "").strip()
    if not series:
        return []
    keys = [_skey(series)] if series.isascii() else []
    keys += [c["tag"] for c in autocomplete(series, category=CAT_COPYRIGHT)[:1]]
    return list(dict.fromkeys(k for k in keys if k))


def _series_hit(tag: str, keys: list[str]) -> bool:
    """角色标签括号里的作品对得上：akira_(blue_archive)、akira_(miria)_(blue_archive)。"""
    return any(_same_series(q, k) for q in re.findall(r"\(([^()]+)\)", tag) for k in keys)


def _copyright_hit(tag: str, keys: list[str]) -> bool:
    """标签里没写作品（东方角色）或写的是简称（kaga_(kancolle)）时，看共现的作品标签。

    只认出现比例 ≥ 30% 的作品：联动 / 同人混搭图会带出别的作品（晶蝶的共现作品里也有碧蓝档案）。
    """
    return any(_same_series(c, k) for c, f in related(tag, "copyright", 5) if f >= 0.3 for k in keys)


def _wiki_names(query: str, title_key: str = "", limit: int = 30, parts: tuple[str, ...] = ()) -> list[dict]:
    """wiki 别名里包含 query 的角色：只写名字的一部分也能命中（如「晶」→ 清澄晶 → akira_(blue_archive)）。

    parts：按顺序都出现即可（「*优香*睡衣*」→ 早濑优香(睡衣)），这时忽略 query。
    每次 2–6 秒；标题过滤与按图片数排序同时用会让 Danbooru 数据库超时（HTTP 500），所以二选一。
    """
    parts = parts or (query,)
    params = {"search[other_names_match]": "*" + "*".join(parts) + "*", "search[tag][category]": CAT_CHARACTER,
              "limit": limit, "only": "title,other_names"}
    if title_key:
        params["search[title_ilike]"] = f"*{title_key}*"
    else:
        params["search[order]"] = "post_count"
    ps = [x.casefold() for x in parts]
    return [{"tag": x["title"], "matched": next((n for n in x.get("other_names") or []
                                                 if all(p in n.casefold() for p in ps)), "")}
            for x in _get("/wiki_pages.json", params) or [] if x.get("title")]


def _tag_matches(pattern: str, limit: int = 20) -> list[dict]:
    d = _get("/tags.json", {"search[name_matches]": pattern, "search[category]": CAT_CHARACTER,
                            "search[order]": "count", "limit": limit, "only": "name,post_count"}) or []
    return [{"tag": x["name"], "post_count": x.get("post_count") or 0, "matched": ""} for x in d if x.get("name")]


def _tag_info(tags: list[str]) -> dict[str, dict]:
    tags = [t for t in dict.fromkeys(tags) if t and "," not in t]
    if not tags:
        return {}
    d = _get("/tags.json", {"search[name_comma]": ",".join(tags), "only": "name,category,post_count",
                            "limit": len(tags)}) or []
    return {x["name"]: x for x in d if x.get("name")}


def _wiki_cands(query: str, keys: list[str], fallback: bool, parts: tuple[str, ...] = ()) -> list[dict]:
    """wiki 别名检索 + 补上图片数。有作品时先按作品过滤标题；fallback：过滤后没有结果时不带作品再查一次。"""
    wiki = _wiki_names(query, keys[-1], parts=parts) if keys else []
    if not wiki and (fallback or not keys):
        wiki = _wiki_names(query, parts=parts)
    info = _tag_info([w["tag"] for w in wiki])
    return [{**w, "post_count": info[w["tag"]].get("post_count") or 0} for w in wiki
            if (info.get(w["tag"]) or {}).get("category") == CAT_CHARACTER]


def search_characters(name: str, series: str = "", name_en: str = "", full: bool = True) -> dict:
    """角色模糊搜索：名字（任意语言，可以只写一部分）+ 作品（可选，任意语言）。

    来源：补全（中日文名、别名、旧名）、英文名 + 作品、wiki 别名包含名字（中日文）、标签名包含名字（英文）。
    有作品时，标签括号里的作品或共现作品标签对得上的记 series_match = True、排在前面；没给作品时为 None。
    full=True（角色库 / 人设导入的模糊搜索）：各来源同时查，全部列出。
    full=False（AI 填写）：按 补全 → 共现作品确认 → wiki 依次查，一找到属于该作品的候选就停，少发请求。
    """
    name = (name or "").strip().lstrip("@").replace("*", "")
    en = (name_en or "").replace("_", " ").strip().replace("*", "")
    if not name and not en:
        return {"items": [], "series_tags": []}
    keys = series_keys(series)
    ascii_name = en or (name if name.isascii() else "")
    stext = series.strip() if series.strip().isascii() else (re.sub(r"_\([^()]*\)$", "", keys[-1]).replace("_", " ")
                                                               if keys else "")
    tries = list(dict.fromkeys(t for t in (name, f"{ascii_name} {stext}" if ascii_name and stext else "",
                                           en if en != name else "") if t))
    cjk = [q for q in (name, en) if q and not q.isascii()]
    pool: dict[str, dict] = {}
    verified: set[str] = set()

    def add(cands: list[dict]) -> None:
        for c in cands:
            if c["tag"] not in pool and c.get("post_count"):
                pool[c["tag"]] = {"tag": c["tag"], "post_count": c["post_count"], "matched": c.get("matched") or ""}

    def hit() -> bool:
        return any(_series_hit(t, keys) for t in pool) or bool(verified) if keys else bool(pool)

    def verify(n: int, stop: bool) -> None:
        """图片最多、标签里看不出作品的几个候选，查共现作品标签确认。"""
        for c in sorted((c for c in pool.values() if not _series_hit(c["tag"], keys)), key=lambda c: -c["post_count"])[:n]:
            if _copyright_hit(c["tag"], keys):
                verified.add(c["tag"])
                if stop:
                    return

    if full:
        jobs = [lambda q=q: autocomplete(q) for q in tries]
        if ascii_name:
            words = _skey(ascii_name)
            jobs += [lambda: _tag_matches(f"*{words}*")]
            if keys:
                jobs += [lambda: _tag_matches(f"*{words}*{keys[-1]}*")]
        jobs += [lambda q=q: _wiki_cands(q, keys, True) for q in cjk]
        errors = []
        with ThreadPoolExecutor(max_workers=4) as ex:      # 请求起始仍按 MIN_INTERVAL 错开（见 _get）
            for f in [ex.submit(j) for j in jobs]:
                try:
                    add(f.result())
                except WebError as e:
                    errors.append(e)
        if errors and len(errors) == len(jobs):
            raise errors[0]
        if not pool:      # 「睡衣优香」这种 服装+名字 整段查不到：拆成两段、两种顺序再查（别名写的是「早濑优香(睡衣)」）
            splits = [(q[:i], q[i:]) for q in cjk for i in sorted({len(q) // 2, (len(q) + 1) // 2}) if 2 <= i <= len(q) - 2]
            pats = list(dict.fromkeys(p for a, b in splits[:2] for p in ((a, b), (b, a))))
            with ThreadPoolExecutor(max_workers=4) as ex:
                for f in [ex.submit(_wiki_cands, "", keys, True, p) for p in pats]:
                    try:
                        add(f.result())
                    except WebError:
                        pass
        if keys and not hit():
            verify(2, False)
    else:
        for q in tries:
            add(autocomplete(q))
            if hit():
                break
        if keys and not hit():
            verify(3, True)
        if not hit():
            for q in cjk or [en or name]:
                add(_wiki_cands(q, keys, False))
                if hit():
                    break

    qs = {_skey(x) for x in (name, en) if x}
    for c in pool.values():
        c["series_match"] = (_series_hit(c["tag"], keys) or c["tag"] in verified) if keys else None
        # 名字对得上：等于补全 / 别名命中的写法，或等于去掉最后一个括号的标签（akira ↔ akira_(blue_archive)）
        c["exact"] = bool(qs & {_skey(c["tag"]), _skey(c["matched"]), _skey(re.sub(r"_\([^()]*\)$", "", c["tag"]))})
    items = sorted(pool.values(), key=lambda c: (not c["series_match"], not c["exact"], -c["post_count"]))
    return {"items": items[:20], "series_tags": keys}


_DTEXT = [
    (re.compile(r"\[\[([^\]|]+)\|([^\]]*)\]\]"), lambda m: m.group(2) or m.group(1).split("(")[0].strip()),
    (re.compile(r"\[\[([^\]]+)\]\]"), lambda m: m.group(1)),
    (re.compile(r'"([^"]+)":\S+'), lambda m: m.group(1)),
    (re.compile(r"\[/?(?:b|i|u|s|tn|spoiler|expand[^\]]*|quote|code|nodtext)\]", re.I), lambda m: ""),
    (re.compile(r"^h\d\.\s*", re.M), lambda m: ""),
    (re.compile(r"\{\{([^}]+)\}\}"), lambda m: m.group(1)),
]


def clean_dtext(body: str, limit: int = WIKI_CHARS) -> str:
    # 去掉图片 / 资源嵌入行（如 "* !post #4447168: Default"）
    text = "\n".join(line for line in (body or "").splitlines() if not re.search(r"!(?:post|asset)\s*#\d+", line))
    for pat, rep in _DTEXT:
        text = pat.sub(rep, text)
    # 「See also / External links」之后多是链接列表，对外貌没有帮助
    text = re.split(r"^\s*(?:See also|External links?|Related tags?|Tag group)\s*:?\s*$", text, flags=re.M | re.I)[0]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(r"\n\s*Appearance\s*$", "", text, flags=re.I).strip()
    if len(text) > limit:
        cut = text[:limit]
        dot = max(cut.rfind(". "), cut.rfind("。"), cut.rfind("\n"))
        text = (cut[:dot + 1] if dot > limit * 0.5 else cut).rstrip() + " …"
    return text


def _stoplist() -> set[str]:
    try:
        return set(json.loads((config.CONFIG_DIR / "danbooru_stoplist.json").read_text(encoding="utf-8"))["tags"])
    except (OSError, ValueError, KeyError):
        return set()


def related(tag: str, category: str, limit: int) -> list[tuple[str, float]]:
    d = _get("/related_tag.json", {"query": tag, "category": category, "limit": limit}) or {}
    return [(t["tag"]["name"], round(float(t.get("frequency") or 0), 2)) for t in d.get("related_tags", [])
            if t.get("tag", {}).get("name")]


def profile(tag: str, post_count: int | None = None, general_limit: int = 60, series_tag: str | None = None) -> dict:
    """取角色资料：wiki 简介、别名、作品标签、外貌标签（共现比例）。

    general：排除停用词后的全部共现通用标签 [(tag, freq)]，人设导入拆分时使用（不存进角色库）。
    series_tag：已知作品时（变体沿用本体的）不再查共现作品标签，少一次请求。
    """
    wiki = _get(f"/wiki_pages/{tag}.json") or {}
    general = general_tags(tag, general_limit)
    appearance = [{"tag": t, "freq": f} for t, f in general if f >= 0.08][:APPEARANCE_TOP]
    if series_tag is None:
        copy = [t for t, _ in related(tag, "copyright", 3)]
        series_tag = copy[0] if copy else ""
    if post_count is None:
        info = _get("/tags.json", {"search[name]": tag, "only": "post_count"}) or [{}]
        post_count = info[0].get("post_count") if info else None
    return {
        "tag": tag, "series_tag": series_tag, "post_count": post_count or 0,
        "other_names": [n for n in (wiki.get("other_names") or []) if n][:20],
        "wiki": clean_dtext(wiki.get("body") or ""), "appearance": appearance, "general": general,
    }


def general_tags(tag: str, limit: int = 60) -> list[tuple[str, float]]:
    """排除停用词后的共现通用标签 [(tag, freq)]。"""
    stop = _stoplist()
    return [(t, f) for t, f in related(tag, "general", limit) if t not in stop]


def variants(tag: str, limit: int = 15) -> list[dict]:
    """造型变体标签：Danbooru 给部分角色的服装 / 形态单独建的角色标签。

    aris_(blue_archive) → 查 aris_(*)_(blue_archive)（如 aris_(maid)_(blue_archive)）；
    hatsune_miku → 查 hatsune_miku_(*)（如 hatsune_miku_(append)）。没有图片的标签与 cosplay 标签不列出。
    """
    m = re.match(r"^(.+?)_(\([^()]+\))$", tag)
    pattern = f"{m.group(1)}_(*)_{m.group(2)}" if m else f"{tag}_(*)"
    d = _get("/tags.json", {"search[name_matches]": pattern, "search[category]": CAT_CHARACTER,
                            "search[order]": "count", "limit": limit + 5, "only": "name,post_count"}) or []
    out = [{"tag": x["name"], "post_count": x.get("post_count") or 0} for x in d
           if x.get("name") and x["name"] != tag and (x.get("post_count") or 0) > 0
           and not x["name"].endswith("_(cosplay)")]
    return out[:limit]


_VARIANT2 = re.compile(r"^(.+?)_\(.+\)_(\([^()]+\))$")     # aris_(maid)_(blue_archive)
_VARIANT1 = re.compile(r"^(.+?)_\(([^()]+)\)$")            # hatsune_miku_(append)


def base_of(tag: str) -> str | None:
    """变体标签 → 本体标签（联网确认，与 variants() 的匹配规则相反）；不是变体返回 None。

    aris_(maid)_(blue_archive) → aris_(blue_archive)；hatsune_miku_(append) → hatsune_miku。
    本体必须是有图的角色标签；只有一个括号时括号里不能是作品（aris_(blue_archive) 不是 aris 的变体）。
    """
    if tag.endswith("_(cosplay)"):
        return None
    m = _VARIANT2.match(tag)
    if m:
        base, qual = f"{m.group(1)}_{m.group(2)}", ""
    else:
        m = _VARIANT1.match(tag)
        if not m:
            return None
        base, qual = m.group(1), m.group(2)
    info = _tag_info([base, qual])
    b = info.get(base) or {}
    if b.get("category") != CAT_CHARACTER or not b.get("post_count"):
        return None
    if qual and (info.get(qual) or {}).get("category") == CAT_COPYRIGHT:
        return None
    return base


def qualifier(variant: str, base: str) -> str:
    """变体相对本体多出的括号：aris_(maid)_(blue_archive) → maid。"""
    m = re.match(r"^(.+?)_(\([^()]+\))$", base)
    stem = m.group(1) if m else base
    rest = variant[len(stem):] if variant.startswith(stem) else variant
    qs = re.findall(r"\(([^()]+)\)", rest)
    if m and len(qs) > 1:
        qs = qs[:-1]
    return " ".join(qs).replace("_", " ") or variant.replace("_", " ")


def find_artist(name: str, name_en: str) -> list[dict]:
    """画师：按 原文写法 → 英文名 查补全（日文名 / 中文名 / 旧名都能命中），按作品数降序，最多 4 个。"""
    for q in (name.lstrip("@"), name_en.lstrip("@").replace("_", " ")):
        if q and q.strip():
            cands = autocomplete(q, category=CAT_ARTIST)
            if cands:
                return sorted(cands, key=lambda c: -c["post_count"])[:4]
    return []


def artist_profile(tag: str, post_count: int | None = None) -> dict:
    """画师资料：各语言别名（来自 /artists）、作品数。"""
    arts = _get("/artists.json", {"search[name]": tag, "only": "name,other_names,group_name", "limit": 1}) or []
    other = [n for n in (arts[0].get("other_names") or []) if n and "@" not in n][:20] if arts else []
    if post_count is None:
        info = _get("/tags.json", {"search[name]": tag, "only": "post_count"}) or [{}]
        post_count = info[0].get("post_count") if info else None
    return {"tag": tag, "post_count": post_count or 0, "other_names": other}


def ping(net_override: dict | None = None) -> dict:
    t0 = time.time()
    d = autocomplete("hatsune miku", net_override)
    return {"ok": bool(d), "elapsed": round(time.time() - t0, 2), "sample": d[0]["tag"] if d else None}
