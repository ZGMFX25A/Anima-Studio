"""生成任务：把前端表单整理成绘画模块请求、提交、后台跟踪并写入图库。"""
from __future__ import annotations

import json
import logging
import threading
import time

import httpx

from . import config, gallery, imagemeta

log = logging.getLogger("frontend.generation")

PROMPT_ORDER = ("quality_meta_year_safe", "count", "character", "series", "artist", "appearance", "action", "nltags")
FIELD_NAMES = PROMPT_ORDER + ("neg",)
TERMINAL = ("succeeded", "failed", "cancelled")


def load_json(name: str) -> dict:
    return json.loads((config.CONFIG_DIR / name).read_text(encoding="utf-8"))


class FormError(ValueError):
    pass


def _clean(text: str | None) -> str:
    text = (text or "").replace("\r", " ").replace("\n", " ")
    parts = [p.strip() for p in text.split(",")]
    return ", ".join(p for p in parts if p)


def _num(d: dict, key: str, default: float) -> float:
    """页面数字框清空时传来 null：按默认值处理。"""
    v = d.get(key)
    return default if v in (None, "") else float(v)


def compose_positive(fields: dict) -> str:
    return ", ".join(s for s in (_clean(fields.get(k)) for k in PROMPT_ORDER) if s)


def resolve_size(form: dict) -> tuple[int, int]:
    ar = form.get("aspect_ratio") or ""
    presets = load_json("defaults.json")["aspect_presets"]
    if ar in presets:
        return tuple(presets[ar])
    try:
        w, h = int(form.get("width")), int(form.get("height"))
    except (TypeError, ValueError) as e:
        raise FormError("自定义尺寸无效") from e
    if not (256 <= w <= 2048 and 256 <= h <= 2048) or w % 16 or h % 16:
        raise FormError("自定义尺寸需在 256–2048 之间且为 16 的倍数")
    return w, h


def build_request(form: dict) -> tuple[dict, list[str]]:
    """返回 (绘画模块请求, 引用到的参考图哈希)。"""
    fields = form.get("fields") or {}
    positive = compose_positive(fields)
    if not positive:
        raise FormError("正向提示词为空：请至少填写一个字段")
    w, h = resolve_size(form)
    used: list[str] = []
    req: dict = {
        "model": form.get("model") or None,
        "prompt": {"positive": positive, "negative": _clean(fields.get("neg"))},
        "width": w, "height": h,
        "seed": int(form["seed"]) if form.get("seed_mode") == "locked" and form.get("seed") not in (None, "") else None,
        "batch_size": 1,
        "sampler": {
            "steps": int(form["steps"]) if form.get("steps") not in (None, "") else None,
            "cfg": float(form["cfg"]) if form.get("cfg") not in (None, "") else None,
            "sampler_name": form.get("sampler") or None,
            "scheduler": form.get("scheduler") or None,
        },
        "output": {"format": "png", "embed_metadata": True},
        # form 由绘画模块写进图片的 anima_painter 文本块，读图时还原到各字段（见 imagemeta.py）
        "client_meta": {"source": "anima-frontend",
                        "form": imagemeta.form_subset({**form, "width": w, "height": h})},
    }
    cr = form.get("character_ref") or {}
    if cr.get("enabled"):
        hashes = [x for x in (cr.get("images") or []) if x][:3]
        if not hashes:
            raise FormError("已开启人物参考，但没有上传人物参考图")
        try:
            imgs = [{"image": gallery.ref_b64(x)} for x in hashes]
        except FileNotFoundError as e:
            raise FormError("人物参考图文件已丢失，请重新上传") from e
        used += hashes
        req["character_ref"] = {"enabled": True, "images": imgs, "strength": _num(cr, "strength", 1.0),
                                "start_percent": _num(cr, "start_percent", 0.0),
                                "end_percent": _num(cr, "end_percent", 1.0)}
    pr = form.get("pose_ref") or {}
    if pr.get("enabled"):
        hsh = pr.get("image")
        if not hsh:
            raise FormError("已开启动作参考，但没有上传动作参考图")
        try:
            img = gallery.ref_b64(hsh)
        except FileNotFoundError as e:
            raise FormError("动作参考图文件已丢失，请重新上传") from e
        used.append(hsh)
        req["pose_ref"] = {"enabled": True, "image": img, "input_type": pr.get("input_type", "photo"),
                           "strength": _num(pr, "strength", 1.0), "fit": pr.get("fit", "person"),
                           "margin": _num(pr, "margin", 0.05), "persons": pr.get("persons", "largest"),
                           "hands": bool(pr.get("hands", True)), "face": bool(pr.get("face", True)),
                           "feet": bool(pr.get("feet", True))}
    used += [x for x in ((form.get("llm") or {}).get("images") or []) if x]
    return req, used


# ───────────────────────────── 任务跟踪 ─────────────────────────────

class Tracker:
    """提交后由后台线程跟踪任务直到结束，成功后写入图库（浏览器关闭也不丢记录）。"""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def submit(self, form: dict) -> tuple[int, dict]:
        req, refs = build_request(form)
        try:
            r = httpx.post(f"{config.PAINTER_URL}/v1/jobs", json=req, timeout=60)
        except httpx.HTTPError:
            return 503, {"error": {"code": "PAINTER_UNAVAILABLE", "message": "绘画模块未启动或无响应"}}
        body = r.json()
        if r.status_code != 202:
            return r.status_code, body
        jid = body["job_id"]
        pid = (form.get("persona") or {}).get("id")
        entry = {"job": body, "form": form, "refs": refs, "positive": req["prompt"]["positive"],
                 "negative": req["prompt"]["negative"], "gallery_id": None, "submitted_at": time.time(),
                 "persona_id": pid if isinstance(pid, int) else None}   # 人设预览：只在人设页显示
        with self.lock:
            self.jobs[jid] = entry
            # 只保留最近 100 个
            for old in sorted(self.jobs, key=lambda k: self.jobs[k]["submitted_at"])[:-100]:
                if self.jobs[old]["job"]["status"] in TERMINAL:
                    self.jobs.pop(old, None)
        threading.Thread(target=self._follow, args=(jid,), daemon=True).start()
        return 202, self.view(jid)

    def _follow(self, jid: str) -> None:
        misses = 0
        while True:
            time.sleep(1.0)
            try:
                r = httpx.get(f"{config.PAINTER_URL}/v1/jobs/{jid}", timeout=10)
                if r.status_code == 404:   # 绘画模块重启过，任务已不在
                    return self._fail(jid, "JOB_LOST", "绘画模块重启过，任务已丢失，请重新生成")
                r.raise_for_status()
                job = r.json()
                misses = 0
            except (httpx.HTTPError, ValueError):
                misses += 1
                if misses >= 30:
                    return self._fail(jid, "PAINTER_UNAVAILABLE", "与绘画模块的连接中断")
                continue
            with self.lock:
                e = self.jobs.get(jid)
                if not e:
                    return
                e["job"] = job
            if job["status"] in TERMINAL:
                if job["status"] == "succeeded":
                    # 写不进图库时转为失败：否则页面上一直是「已完成但没有图」，既不提示也关不掉
                    try:
                        if not self._record(jid):
                            self._fail(jid, "ENGINE_ERROR", "绘画模块没有返回图片")
                    except Exception as ex:  # noqa: BLE001
                        log.exception("任务 %s 写入图库失败", jid)
                        self._fail(jid, "RECORD_FAILED", f"图片已生成，但写入图库失败：{ex}")
                return

    def _fail(self, jid: str, code: str, message: str) -> None:
        with self.lock:
            e = self.jobs.get(jid)
            if e:
                e["job"] = {**e["job"], "status": "failed", "error": {"code": code, "message": message}}

    def _record(self, jid: str) -> int | None:
        e = self.jobs.get(jid)
        if not e:
            return None
        job = e["job"]
        params = job.get("params") or {}
        gid = None
        for img in job.get("images", []):
            if img.get("kind") != "final":
                continue
            gid = gallery.add(job_id=jid, path=img["path"], width=img["width"], height=img["height"],
                              seed=img.get("seed", params.get("seed")), params=params, positive=e["positive"],
                              negative=e["negative"], form=e["form"], refs=e["refs"], persona_id=e["persona_id"])
            with self.lock:
                e["gallery_id"] = gid
            log.info("任务 %s 完成，已写入图库 #%s", jid, gid)
        return gid

    def view(self, jid: str) -> dict | None:
        with self.lock:
            e = self.jobs.get(jid)
            if not e:
                return None
            j = e["job"]
            return {"job_id": jid, "status": j["status"], "progress": j.get("progress"), "error": j.get("error"),
                    "params": j.get("params"), "gallery_id": e["gallery_id"], "submitted_at": e["submitted_at"],
                    "persona_id": e["persona_id"]}

    def active(self, persona_id: int | None = None) -> list[dict]:
        """persona_id 为空：绘图页的任务（不含人设预览）；否则只返回该人设的预览任务。"""
        with self.lock:
            ids = sorted((k for k, e in self.jobs.items() if e["persona_id"] == persona_id),
                         key=lambda k: self.jobs[k]["submitted_at"])
        return [v for v in (self.view(k) for k in ids) if v]

    def dismiss(self, jid: str) -> None:
        """页面上关掉失败 / 取消的任务：从本次运行的列表中移除（刷新页面后也不会再出现）。"""
        with self.lock:
            e = self.jobs.get(jid)
            if e and e["job"]["status"] in ("failed", "cancelled"):
                self.jobs.pop(jid, None)

    def forget_gallery(self, gid: int) -> None:
        """图库记录被删除后，把对应任务从本次运行的列表中移除（刷新页面后也不会再出现）。"""
        with self.lock:
            for k in [k for k, e in self.jobs.items() if e.get("gallery_id") == gid]:
                self.jobs.pop(k, None)

    def cancel(self, jid: str) -> tuple[int, dict]:
        try:
            r = httpx.post(f"{config.PAINTER_URL}/v1/jobs/{jid}/cancel", timeout=10)
        except httpx.HTTPError:
            return 503, {"error": {"code": "PAINTER_UNAVAILABLE", "message": "绘画模块未启动或无响应"}}
        return r.status_code, r.json()


TRACKER = Tracker()
