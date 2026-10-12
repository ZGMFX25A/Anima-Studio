"""前端服务 HTTP 接口：页面、绘画模块代理、图库、系统管理、LLM。"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, gallery, personas, procs, storage
from .generation import TRACKER, FormError, load_json
from .llm import client as llm_client
from .llm import fill as llm_fill
from .llm import persona as llm_persona
from .llm import retry as llm_retry
from .llm import store as llm_store
from .personas import PersonaError
from .web import danbooru, net, tagsearch
from .web.library import LIBS
from .web.net import WebError

log = logging.getLogger("frontend.app")
app = FastAPI(title="Anima Studio Frontend", docs_url="/api/docs", openapi_url="/api/openapi.json")


def err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.middleware("http")
async def _no_cache(request: Request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/api/files/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ───────────────────────────── 能力与默认值 ─────────────────────────────

def _fallback_capabilities() -> dict:
    d = json.loads(config.PAINTER_MODELS_JSON.read_text(encoding="utf-8"))
    return {
        "default_model": d["default_model"], "samplers": [], "schedulers": [],
        "models": [{"id": k, "label": m.get("label", k), "installed": True, "defaults": m["defaults"],
                    "features": m.get("features", {}), "notes": m.get("notes", {})} for k, m in d["models"].items()],
    }


@app.get("/api/bootstrap")
def bootstrap():
    try:
        r = httpx.get(f"{config.PAINTER_URL}/v1/capabilities", timeout=10)
        r.raise_for_status()
        caps, ready = r.json(), True
    except (httpx.HTTPError, ValueError):
        caps, ready = _fallback_capabilities(), False
    return {"capabilities": caps, "engine_ready": ready, "defaults": load_json("defaults.json"),
            "models_ui": {k: v for k, v in load_json("models_ui.json").items() if not k.startswith("_")}}


# ───────────────────────────── 参考图 ─────────────────────────────

@app.post("/api/refs")
def upload_ref(data: str = Body(..., embed=True)):
    try:
        return {"hash": gallery.save_ref(data)}
    except ValueError as e:
        return err(422, "BAD_IMAGE", str(e))


@app.get("/api/files/ref/{h}")
def get_ref(h: str, thumb: int = 0):
    if thumb:
        b = gallery.ref_thumbnail(h)
        return Response(b, media_type="image/webp") if b else err(404, "NOT_FOUND", "参考图不存在")
    p = gallery.ref_path(h)
    return FileResponse(p) if p else err(404, "NOT_FOUND", "参考图不存在")


# ───────────────────────────── 生成任务 ─────────────────────────────

@app.post("/api/jobs")
def submit_job(form: dict = Body(..., embed=True)):
    try:
        status, body = TRACKER.submit(form)
    except FormError as e:
        return err(422, "INVALID_FORM", str(e))
    return JSONResponse(status_code=status, content=body)


@app.get("/api/jobs")
def list_jobs(persona_id: int | None = None):
    """不带 persona_id：绘图页的任务；带 persona_id：该人设的预览任务。"""
    return TRACKER.active(persona_id)


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    v = TRACKER.view(jid)
    return v if v else err(404, "NOT_FOUND", "任务不存在")


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    status, body = TRACKER.cancel(jid)
    return JSONResponse(status_code=status, content=body)


# ───────────────────────────── 图库 ─────────────────────────────

@app.get("/api/gallery")
def gallery_list(favorite: int = 0, offset: int = 0, limit: int = 40):
    return gallery.list_items(bool(favorite), max(0, offset), min(max(1, limit), 200))


@app.get("/api/gallery/{gid}")
def gallery_get(gid: int):
    d = gallery.get(gid)
    return d if d else err(404, "NOT_FOUND", "记录不存在")


@app.post("/api/gallery/{gid}/favorite")
def gallery_favorite(gid: int):
    fav = gallery.toggle_favorite(gid)
    return {"favorite": fav} if fav is not None else err(404, "NOT_FOUND", "记录不存在")


@app.post("/api/gallery/{gid}/delete")
def gallery_delete(gid: int, keep: list[str] = Body(default=[], embed=True)):
    if not gallery.delete(gid, set(keep)):
        return err(404, "NOT_FOUND", "记录不存在")
    TRACKER.forget_gallery(gid)
    return {"ok": True}


@app.post("/api/gallery/{gid}/reveal")
def gallery_reveal(gid: int):
    p = gallery.image_path(gid)
    if not p or not p.exists():
        return err(404, "NOT_FOUND", "文件不存在")
    subprocess.Popen(["explorer", f"/select,{p}"])
    return {"ok": True}


@app.get("/api/files/image/{gid}")
def gallery_image(gid: int):
    p = gallery.image_path(gid)
    return FileResponse(p) if p and p.exists() else err(404, "NOT_FOUND", "文件不存在")


@app.get("/api/files/thumb/{gid}")
def gallery_thumb(gid: int):
    p = gallery.thumbnail(gid)
    return FileResponse(p) if p else err(404, "NOT_FOUND", "文件不存在")


# ───────────────────────────── 系统 ─────────────────────────────

def _painter_health() -> dict | None:
    try:
        r = httpx.get(f"{config.PAINTER_URL}/v1/health", timeout=3)
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


@app.get("/api/system/status")
def system_status():
    comfy = procs.COMFY.info()
    comfy["stats"] = procs.comfy_stats() if comfy["state"] == "running" else None
    if comfy["state"] in ("stopped", "unresponsive") and comfy["error"]:
        comfy["log_tail"] = procs.COMFY.log_tail()
    painter = procs.PAINTER.info()
    h = _painter_health() if painter["state"] == "running" else None
    painter["queue"] = h.get("queue") if h else None
    painter["running_job"] = h.get("running") if h else None
    if painter["state"] in ("stopped", "unresponsive") and painter["error"]:
        painter["log_tail"] = procs.PAINTER.log_tail()
    return {"comfyui": comfy, "painter": painter}


def _cancel_all_jobs() -> None:
    try:
        jobs = httpx.get(f"{config.PAINTER_URL}/v1/jobs", params={"limit": 50}, timeout=5).json()
        for j in jobs:
            if j["status"] in ("queued", "running"):
                httpx.post(f"{config.PAINTER_URL}/v1/jobs/{j['job_id']}/cancel", timeout=5)
    except (httpx.HTTPError, ValueError):
        pass


@app.post("/api/system/{service}/{action}")
def system_control(service: str, action: str, force: int = 0):
    svc = procs.SERVICES.get(service)
    if not svc or action not in ("start", "stop", "restart"):
        return err(404, "NOT_FOUND", "未知操作")
    if action in ("stop", "restart"):
        h = _painter_health()
        busy = bool(h and (h.get("running") or h.get("queue")))
        if busy and not force:
            return err(409, "BUSY", "有任务正在生成或排队")
        if busy:
            _cancel_all_jobs()
    getattr(svc, action)()
    return svc.info()


@app.post("/api/engine/free-vram")
def free_vram():
    try:
        r = httpx.post(f"{config.PAINTER_URL}/v1/engine/free", params={"unload_models": True}, timeout=30)
    except httpx.HTTPError:
        return err(503, "PAINTER_UNAVAILABLE", "绘画模块未启动")
    if r.status_code == 409:
        return err(409, "BUSY", "有任务正在执行，暂不能释放显存")
    if r.status_code != 200:
        return err(r.status_code, "ENGINE_ERROR", "释放显存失败（引擎可能未启动）")
    return {"ok": True}


# ───────────────────────────── LLM ─────────────────────────────

@app.get("/api/llm/providers")
def llm_providers():
    return [llm_store.public(p) for p in llm_store.providers()]


@app.post("/api/llm/providers")
def llm_create(data: dict = Body(...)):
    return llm_store.public(llm_store.create(data))


@app.put("/api/llm/providers/{pid}")
def llm_update(pid: str, data: dict = Body(...)):
    p = llm_store.update(pid, data)
    return llm_store.public(p) if p else err(404, "NOT_FOUND", "服务商不存在")


@app.delete("/api/llm/providers/{pid}")
def llm_delete(pid: str):
    return {"ok": llm_store.delete(pid)}


@app.post("/api/llm/providers/{pid}/fetch-models")
def llm_fetch(pid: str):
    p = llm_store.get(pid)
    if not p:
        return err(404, "NOT_FOUND", "服务商不存在")
    try:
        models = llm_client.fetch_models(p)
    except llm_client.LLMError as e:
        return err(502, "LLM_ERROR", str(e))
    return llm_store.public(llm_store.set_models(pid, models))


@app.post("/api/llm/providers/{pid}/model")
def llm_model(pid: str, data: dict = Body(...)):
    mid = data.pop("model_id", "")
    m = llm_store.update_model(pid, mid, data)
    return m if m else err(404, "NOT_FOUND", "模型不存在")


@app.get("/api/llm/enabled")
def llm_enabled():
    return llm_store.enabled_models()


@app.post("/api/llm/test")
def llm_test(data: dict = Body(...)):
    try:
        return llm_fill.test(data)
    except llm_client.LLMError as e:
        return err(502, "LLM_ERROR", str(e))


@app.post("/api/llm/fill")
def llm_fill_api(data: dict = Body(...)):
    try:
        return llm_fill.fill(data)
    except llm_client.LLMError as e:
        return err(502, "LLM_ERROR", str(e))


@app.post("/api/llm/cancel")
def llm_cancel(request_id: str = Body(..., embed=True)):
    """页面上点了「取消」：AI 填写 / 人设 AI 拆分与整理在下一次重试前停下。"""
    llm_retry.cancel(request_id)
    return {"ok": True}


# ───────────────────────────── 存储 ─────────────────────────────

def _storage_state() -> dict:
    p = procs.PAINTER
    want = procs._painter_env()
    running = p.refresh() == "running"
    return {**storage.get(), "default": storage.DEFAULT_OUTPUT, "effective": storage.effective(),
            "painter_running": running, "painter_external": p.external,
            # 绘画模块正在用的目录与设置不一致（外部启动的进程无法确认，也算）
            "pending_restart": running and (p.external or p.applied_env != want)}


@app.get("/api/storage")
def storage_get():
    return _storage_state()


@app.put("/api/storage")
def storage_put(output_dir: str = Body("", embed=True), force: int = 0):
    try:
        storage.validate(output_dir) if output_dir.strip() else None
    except ValueError as e:
        return err(422, "INVALID", str(e))
    h = _painter_health()
    busy = bool(h and (h.get("running") or h.get("queue")))
    if busy and not force:
        return err(409, "BUSY", "有任务正在生成或排队，保存后需要重启绘画模块")
    storage.set_output_dir(output_dir)
    if busy:
        _cancel_all_jobs()
    if procs.PAINTER.refresh() != "stopped":
        procs.PAINTER.restart()          # 带上新的 PAINTER_OUTPUT_DIR 重新启动
    return _storage_state()


@app.post("/api/storage/browse")
def storage_browse():
    try:
        path = storage.browse(storage.effective())
    except Exception as e:  # noqa: BLE001
        return err(500, "BROWSE_FAILED", f"无法打开选择文件夹对话框：{e}")
    return {"path": path}


@app.post("/api/storage/open")
def storage_open():
    d = Path(storage.effective())
    d.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["explorer", str(d)])
    return {"ok": True}


# ───────────────────────────── 联网 ─────────────────────────────

@app.get("/api/web/settings")
def web_settings():
    return net.public()


@app.put("/api/web/settings")
def web_settings_update(data: dict = Body(...)):
    return net.update(data)


@app.post("/api/web/settings/reset-tagsearch")
def web_settings_reset():
    return net.reset_tagsearch()


@app.post("/api/web/test/{target}")
def web_test(target: str, data: dict = Body(default={})):
    """data：页面上尚未保存的输入，只用于这次测试，不写入设置。"""
    try:
        if target == "danbooru":
            return danbooru.ping(data)
        if target == "tagsearch":
            return tagsearch.test(data)
    except WebError as e:
        return err(502, "WEB_ERROR", str(e))
    return err(404, "NOT_FOUND", "未知目标")


@app.post("/api/web/tagsearch-cache/clear")
def web_clear_cache():
    return {"removed": tagsearch.clear_cache()}


# ───────────────────────────── 资料库（角色 / 画师） ─────────────────────────────
# kind = characters / artists

def _lib(kind: str):
    return LIBS.get(kind)


def _find(kind: str, name: str, series: str = ""):
    en = name if name.isascii() else ""
    if kind == "artists":
        return danbooru.find_artist(name, en)
    return danbooru.find_character(name, en, series)


def _profile(kind: str, tag: str, post_count=None):
    return danbooru.artist_profile(tag, post_count) if kind == "artists" else danbooru.profile(tag, post_count)


@app.get("/api/library/{kind}")
def library_list(kind: str, q: str = "", offset: int = 0, limit: int = 50):
    lib = _lib(kind)
    return lib.search(q, max(0, offset), min(max(1, limit), 200)) if lib else err(404, "NOT_FOUND", "未知资料库")


@app.get("/api/library/{kind}/{rid}")
def library_get(kind: str, rid: int):
    lib = _lib(kind)
    r = lib.get(rid) if lib else None
    return r if r else err(404, "NOT_FOUND", "条目不存在")


@app.post("/api/library/{kind}")
def library_create(kind: str, data: dict = Body(...)):
    lib = _lib(kind)
    if not lib:
        return err(404, "NOT_FOUND", "未知资料库")
    if kind == "characters":   # 角色库只存 Danbooru 上的已有角色
        return err(422, "INVALID", "角色库只保存 Danbooru 上的已有角色；原创角色请在「人设」页新建")
    try:
        return lib.create_manual(data)
    except ValueError as e:
        return err(422, "INVALID", str(e))


@app.post("/api/library/{kind}/fetch")
def library_fetch(kind: str, data: dict = Body(...)):
    """按名字从 Danbooru 检索并加入资料库（页面上的「从 Danbooru 添加」）。"""
    lib = _lib(kind)
    if not lib:
        return err(404, "NOT_FOUND", "未知资料库")
    name = (data.get("name") or "").strip().lstrip("@")
    if not name:
        return err(422, "INVALID", "请输入名字")
    hit = lib.lookup(name)
    if hit:
        return hit
    try:
        cands = _find(kind, name, data.get("series") or "")
        if not cands:
            return err(404, "NOT_FOUND", f"Danbooru 上没有找到「{name}」")
        best = cands[0]
        existing = lib.get_by_tag(best["tag"])
        if existing:
            lib.add_names(existing["id"], [name])
            return lib.get(existing["id"])
        return lib.save_from_danbooru(_profile(kind, best["tag"], best["post_count"]), name, [name])
    except WebError as e:
        return err(502, "WEB_ERROR", str(e))


@app.put("/api/library/{kind}/{rid}")
def library_update(kind: str, rid: int, data: dict = Body(...)):
    lib = _lib(kind)
    if not lib:
        return err(404, "NOT_FOUND", "未知资料库")
    try:
        r = lib.update(rid, data)
    except ValueError as e:
        return err(422, "INVALID", str(e))
    return r if r else err(404, "NOT_FOUND", "条目不存在")


@app.post("/api/library/{kind}/{rid}/refresh")
def library_refresh(kind: str, rid: int, keep_edited: bool = Body(True, embed=True)):
    lib = _lib(kind)
    r = lib.get(rid) if lib else None
    if not r:
        return err(404, "NOT_FOUND", "条目不存在")
    try:
        prof = _profile(kind, r["tag"])
    except WebError as e:
        return err(502, "WEB_ERROR", str(e))
    if not prof["post_count"] and not prof.get("appearance") and not prof.get("wiki") and not prof.get("other_names"):
        return err(404, "NOT_FOUND", f"Danbooru 上找不到标签 {r['tag']}")
    return lib.apply_refresh(rid, prof, keep_edited)


@app.delete("/api/library/{kind}/{rid}")
def library_delete(kind: str, rid: int):
    lib = _lib(kind)
    return {"ok": lib.delete(rid)} if lib else err(404, "NOT_FOUND", "未知资料库")


# ───────────────────────────── 人设 ─────────────────────────────

@app.get("/api/personas")
def persona_list(q: str = "", kind: str = "", group: str = ""):
    return personas.list_all(q, kind, group)


@app.post("/api/personas")
def persona_create(data: dict = Body(...)):
    try:
        return personas.create(data)
    except PersonaError as e:
        return err(422, "INVALID", str(e))


@app.post("/api/personas/danbooru/search")
def persona_search(q: str = Body("", embed=True)):
    try:
        return llm_persona.search(q)
    except WebError as e:
        return err(502, "WEB_ERROR", str(e))


@app.post("/api/personas/danbooru/variants")
def persona_variants(tag: str = Body(..., embed=True)):
    try:
        return llm_persona.variants(tag.strip().replace(" ", "_"))
    except WebError as e:
        return err(502, "WEB_ERROR", str(e))


@app.post("/api/personas/import")
def persona_import(data: dict = Body(...)):
    """从 Danbooru 取资料 →（可选）AI 拆分，返回草稿（不保存）。"""
    try:
        return llm_persona.import_character(data)
    except PersonaError as e:
        return err(422, "INVALID", str(e))
    except WebError as e:
        return err(502, "WEB_ERROR", str(e))
    except llm_client.LLMError as e:
        return err(502, "LLM_ERROR", str(e))


@app.post("/api/personas/refine")
def persona_refine(data: dict = Body(...)):
    """AI 整理：按补充描述补全 / 修正，返回结果（不保存）。"""
    try:
        return llm_persona.refine(data)
    except llm_client.LLMError as e:
        return err(502, "LLM_ERROR", str(e))


@app.get("/api/personas/{pid}")
def persona_get(pid: int):
    p = personas.get(pid)
    return p if p else err(404, "NOT_FOUND", "人设不存在")


@app.put("/api/personas/{pid}")
def persona_update(pid: int, data: dict = Body(...)):
    try:
        p = personas.update(pid, data)
    except PersonaError as e:
        return err(422, "INVALID", str(e))
    return p if p else err(404, "NOT_FOUND", "人设不存在")


@app.delete("/api/personas/{pid}")
def persona_delete(pid: int):
    n = personas.delete(pid)
    if n is None:
        return err(404, "NOT_FOUND", "人设不存在")
    return {"ok": True, "images_deleted": n}


@app.get("/api/personas/{pid}/images")
def persona_images(pid: int):
    return gallery.list_persona(pid)


@app.post("/api/personas/{pid}/cover")
def persona_cover(pid: int, gallery_id: int | None = Body(None), ref: str | None = Body(None)):
    """设为封面：gallery_id（预览图 / 历史图）或 ref（上传的图片哈希）。"""
    try:
        if gallery_id is not None:
            p = personas.set_cover_from_gallery(pid, gallery_id)
        elif ref:
            p = personas.set_cover_from_ref(pid, ref)
        else:
            return err(422, "INVALID", "缺少图片")
    except PersonaError as e:
        return err(422, "INVALID", str(e))
    except OSError as e:
        return err(422, "BAD_IMAGE", f"图片无法读取：{e}")
    return p if p else err(404, "NOT_FOUND", "人设不存在")


@app.delete("/api/personas/{pid}/cover")
def persona_cover_clear(pid: int):
    p = personas.clear_cover(pid)
    return p if p else err(404, "NOT_FOUND", "人设不存在")


@app.get("/api/files/persona/{pid}/cover")
def persona_cover_file(pid: int, thumb: int = 0):
    p = personas.cover_path(pid, bool(thumb))
    return FileResponse(p) if p.exists() else err(404, "NOT_FOUND", "没有封面")


# ───────────────────────────── 页面 ─────────────────────────────

app.mount("/", StaticFiles(directory=Path(config.WEB_DIR), html=True), name="web")
