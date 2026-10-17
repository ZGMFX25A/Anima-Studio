"""造型变体缓存：角色存入角色库后，在后台补齐它所在的「本体 + 变体」。

- 存入的是本体：取变体列表存到本体行，再把图片数够的变体逐个取资料存入角色库（挂在本体下）。
- 存入的是变体（如 AI 填写里写了「米莉亚」）：先联网确认本体，把本体一起存上，再按上一条处理。
- 图片数不到 MIN_POSTS 的变体不自动缓存；用户单独删除过的变体也不自动取回。
  明确用到的变体（人设导入勾选、角色库点「获取」、模糊搜索导入、AI 填写直接命中）不受这两条限制，照常存。

只在后台线程里联网，不拖慢 AI 填写 / 导入；全部已缓存时只查数据库。同一个角色同时只排一次队。
同一趟也补齐角色所在作品的服装目录与服装库（见 outfits.py）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

from . import danbooru, outfits
from .library import CHARACTERS
from .net import WebError

MIN_POSTS = 20     # 图片太少的变体共现标签不准，不自动缓存

log = logging.getLogger("frontend.variants")
_cv = threading.Condition()
_queue: deque[str] = deque()
_pending: set[str] = set()     # 排队中 / 处理中的标签（处理变体时也含它的本体）
_worker: list[threading.Thread] = []


def display_name(parent: dict, vtag: str) -> str:
    """变体的显示名：本体改过名字时用「本体名(变体)」，如 天童爱丽丝(maid)；否则用标签。"""
    if parent["name"] and parent["name"] != parent["tag"].replace("_", " "):
        return f"{parent['name']}({danbooru.qualifier(vtag, parent['tag'])})"
    return vtag.replace("_", " ")


def save_variant(parent: dict, vtag: str, post_count: int | None = None, general_limit: int = 60) -> tuple[dict, dict]:
    """取一个变体的资料（作品沿用本体，少一次请求）存入角色库并挂到本体下。返回 (资料, 角色库条目)。"""
    prof = danbooru.profile(vtag, post_count, general_limit=general_limit, series_tag=parent["series_tag"])
    row = CHARACTERS.save_from_danbooru(prof, display_name(parent, vtag), [], parent_tag=parent["tag"])
    return prof, row


def refresh_list(parent: dict) -> list[dict]:
    """联网取本体的变体列表存进角色库（库里已有的变体随之挂上），需要缓存的变体排进后台。"""
    lst = danbooru.variants(parent["tag"])
    CHARACTERS.set_variants(parent["tag"], lst)
    schedule(parent["tag"])
    return lst


def schedule(tag: str) -> None:
    """排进后台：补齐 tag 所在的「本体 + 变体」。"""
    with _cv:
        if tag in _pending:
            return
        _pending.add(tag)
        _queue.append(tag)
        if not _worker or not _worker[0].is_alive():
            _worker[:] = [threading.Thread(target=_run, name="variant-cache", daemon=True)]
            _worker[0].start()
        _cv.notify_all()


def busy(tag: str) -> bool:
    """这个角色（本体）的变体是否正在后台缓存。"""
    with _cv:
        return tag in _pending


def wait_idle(timeout: float = 120) -> bool:
    """等后台缓存全部做完（测试用）。"""
    end = time.time() + timeout
    with _cv:
        while _pending:
            left = end - time.time()
            if left <= 0:
                return False
            _cv.wait(left)
        return True


def _run() -> None:
    while True:
        with _cv:
            while not _queue:
                _cv.wait()
            tag = _queue.popleft()
        extra: list[str] = []
        try:
            _ensure(tag, extra)
        except WebError as e:
            log.info("变体缓存：%s 联网失败（%s），下次用到时再试", tag, e)
        except Exception:     # noqa: BLE001  后台线程不能因为一个角色出错就停掉
            log.exception("变体缓存：处理 %s 出错", tag)
        finally:
            with _cv:
                _pending.difference_update([tag, *extra])
                _cv.notify_all()


def _ensure(tag: str, extra: list[str]) -> None:
    row = CHARACTERS.get_by_tag(tag)
    if not row:
        return
    if row["parent_tag"]:
        base = row["parent_tag"]
    elif row["variants_at"] is None:
        base = danbooru.base_of(tag) or tag        # 还没当本体处理过：先确认它是不是某个本体的变体
    else:
        base = tag
    if base != tag:
        with _cv:
            _pending.add(base)
            extra.append(base)
        if not CHARACTERS.get_by_tag(base):
            CHARACTERS.save_from_danbooru(danbooru.profile(base), "", [])
            log.info("变体缓存：%s 是 %s 的变体，已把本体一起存入角色库", tag, base)
        CHARACTERS.set_parent(tag, base)
    parent = CHARACTERS.get_by_tag(base)
    if parent["variants_at"] is None:
        CHARACTERS.set_variants(base, danbooru.variants(base))
        parent = CHARACTERS.get_by_tag(base)
    have = CHARACTERS.tags_present(v["tag"] for v in parent["variants"])
    todo = [v for v in parent["variants"]
            if v["tag"] not in have and not v.get("removed") and v["post_count"] >= MIN_POSTS]
    for v in todo:
        save_variant(parent, v["tag"], v["post_count"])
    if todo:
        log.info("变体缓存：%s 的 %d 个变体已存入角色库", base, len(todo))
    # 同一趟顺便补齐这个作品的服装目录（作品服装不挂在角色下，几个角色共用；已取过时只查数据库）
    try:
        outfits.ensure_series(outfits.series_key(parent), profiles=True)
    except WebError as e:
        log.info("服装库：%s 的作品服装联网失败（%s），下次用到时再试", base, e)
