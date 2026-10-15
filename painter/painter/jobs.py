"""任务队列：单 GPU，串行执行。"""
from __future__ import annotations

import base64
import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from . import images
from .comfy_client import ComfyClient, EngineRejected, EngineUnavailable
from .config import settings
from .pipeline import RequestError, Uploads, build, resolve
from .registry import Registry
from .schemas import ApiError, ErrorCode, GenerateRequest, ImageResult, Job, JobStatus, Progress

log = logging.getLogger("painter.jobs")

# 进度权重：准备 5%，采样阶段平分 85%，解码与保存 10%
_PREP, _SAVE = 5.0, 10.0


class _Cancelled(Exception):
    pass


class JobFailed(Exception):
    def __init__(self, code: ErrorCode, message: str, detail=None):
        super().__init__(message)
        self.code, self.detail = code, detail


class JobManager:
    def __init__(self, client: ComfyClient, registry: Registry):
        self.client = client
        self.registry = registry
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.requests: dict[str, GenerateRequest] = {}
        self.files: dict[str, dict[str, Path]] = {}
        self.q: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.cancel_flags: dict[str, threading.Event] = {}
        self.done_events: dict[str, threading.Event] = {}
        self.current: tuple[str, str | None] | None = None  # (job_id, prompt_id)
        threading.Thread(target=self._worker, name="painter-worker", daemon=True).start()

    # ── 对外 ──
    def submit(self, req: GenerateRequest) -> Job:
        entry, params = resolve(req, self.registry)  # 同步校验，失败直接抛 RequestError
        self._validate_images(req)
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        job = Job(job_id=job_id, status=JobStatus.queued, created_at=time.time(), params=params,
                  client_meta=req.client_meta)
        with self.lock:
            self.jobs[job_id] = job
            self.requests[job_id] = req
            self.cancel_flags[job_id] = threading.Event()
            self.done_events[job_id] = threading.Event()
            self._trim()
        self.q.put(job_id)
        return job

    def get(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job and job.status == JobStatus.queued:
            ids = [j for j, x in self.jobs.items() if x.status == JobStatus.queued]
            job.progress.queue_position = ids.index(job_id) + (1 if self.current else 0)
        return job

    def list(self, limit: int = 50) -> list[Job]:
        return list(reversed(self.jobs.values()))[:limit]

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        self.done_events[job_id].wait(timeout)
        return self.jobs[job_id]

    def cancel(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if not job or job.status not in (JobStatus.queued, JobStatus.running):
            return job
        self.cancel_flags[job_id].set()
        if job.status == JobStatus.queued:
            self._finish(job, JobStatus.cancelled, ApiError(code=ErrorCode.CANCELLED, message="已取消"))
        elif self.current and self.current[0] == job_id and self.current[1]:
            try:
                self.client.interrupt(self.current[1])
            except EngineUnavailable:
                pass
        return job

    def file_path(self, job_id: str, name: str) -> Path | None:
        return self.files.get(job_id, {}).get(name)

    # ── 内部 ──
    def _trim(self):
        while len(self.jobs) > settings.job_history:
            old, job = next(iter(self.jobs.items()))
            if job.status in (JobStatus.queued, JobStatus.running):
                break
            for d in (self.jobs, self.requests, self.cancel_flags, self.done_events, self.files):
                d.pop(old, None)

    def _validate_images(self, req: GenerateRequest):
        try:
            if req.character_ref and req.character_ref.enabled:
                for i, r in enumerate(req.character_ref.images):
                    images.decode(r.image, f"character_ref.images[{i}]")
                    if r.mask:
                        images.decode(r.mask, f"character_ref.images[{i}].mask")
            if req.pose_ref and req.pose_ref.enabled:
                images.decode(req.pose_ref.image, "pose_ref.image")
        except images.BadImage as e:
            raise RequestError(ErrorCode.BAD_IMAGE, str(e)) from e

    def _finish(self, job: Job, status: JobStatus, error: ApiError | None = None):
        job.status = status
        job.error = error
        job.finished_at = time.time()
        if status == JobStatus.succeeded:
            job.progress = Progress(stage="done", percent=100.0)
        self.requests.pop(job.job_id, None)  # 释放 base64 大字段
        self.done_events[job.job_id].set()

    def _worker(self):
        while True:
            job_id = self.q.get()
            job = self.jobs.get(job_id)
            if job is None or job.status != JobStatus.queued:
                continue
            self.current = (job_id, None)
            job.status, job.started_at = JobStatus.running, time.time()
            try:
                try:
                    self._run(job, self.requests[job_id])
                except JobFailed as e:
                    if not _is_transient(e):
                        raise
                    # ComfyUI 动态显存（comfy-aimdo）偶发的底层分配错误，非显存不足：释放后重跑一次
                    log.warning("job %s transient engine fault, retrying once: %s", job_id, e)
                    self.client.free(unload_models=True)
                    self._run(job, self.requests[job_id])
                self._finish(job, JobStatus.succeeded)
            except _Cancelled:
                self._finish(job, JobStatus.cancelled, ApiError(code=ErrorCode.CANCELLED, message="已取消"))
            except JobFailed as e:
                self._finish(job, JobStatus.failed, ApiError(code=e.code, message=str(e), detail=e.detail))
            except RequestError as e:
                self._finish(job, JobStatus.failed, ApiError(code=e.code, message=str(e), detail=e.detail))
            except EngineUnavailable as e:
                self._finish(job, JobStatus.failed, ApiError(code=ErrorCode.ENGINE_UNAVAILABLE, message=str(e)))
            except Exception as e:  # noqa: BLE001
                log.exception("job %s crashed", job_id)
                self._finish(job, JobStatus.failed, ApiError(code=ErrorCode.ENGINE_ERROR, message=repr(e)))
            finally:
                self.current = None

    def _check_cancel(self, job: Job):
        if self.cancel_flags[job.job_id].is_set():
            raise _Cancelled()

    def _run(self, job: Job, req: GenerateRequest):
        p = job.params
        entry = self.registry.get(p.model)
        job.progress = Progress(stage="preparing", percent=1.0)

        # 1. 预处理并上传参考图
        up = Uploads()
        if p.character_ref:
            for i, r in enumerate(req.character_ref.images):
                data = images.prepare_character_ref(r.image, r.mask, f"character_ref.images[{i}]", settings.max_input_side)
                up.character_refs.append(self.client.upload_image(data, f"{job.job_id}_char{i}.png"))
        if p.pose_ref:
            data = images.prepare_pose_ref(req.pose_ref.image, req.pose_ref.input_type, "pose_ref.image",
                                           settings.max_input_side)
            up.pose = self.client.upload_image(data, f"{job.job_id}_pose.png")
        self._check_cancel(job)

        # 2. 构建并提交工作流
        wf = build(req, self.registry, entry, p, up)
        n_samplers = max(1, len(set(wf.sampler_nodes.values())))
        stage_order = ["base"] + p.stages
        cid = ComfyClient.new_client_id()
        ws = self.client.connect_ws(cid)
        try:
            try:
                prompt_id = self.client.submit(wf.graph, cid)
            except EngineRejected as e:
                raise JobFailed(ErrorCode.ENGINE_ERROR, f"工作流被 ComfyUI 拒绝: {e}", e.detail) from e
            self.current = (job.job_id, prompt_id)
            job.progress = Progress(stage="base", percent=_PREP)
            deadline = time.time() + settings.job_timeout

            # 3. 监听执行事件
            for ev in ComfyClient.events(ws):
                if time.time() > deadline:
                    self.client.interrupt(prompt_id)
                    raise JobFailed(ErrorCode.TIMEOUT, f"超过 {settings.job_timeout}s 未完成")
                if ev is None:
                    continue
                t, d = ev.get("type"), ev.get("data", {})
                if d.get("prompt_id") not in (None, prompt_id):
                    continue
                if t == "progress":
                    stage = wf.sampler_nodes.get(str(d.get("node")))
                    if stage:
                        k = stage_order.index(stage) if stage in stage_order else 0
                        frac = d["value"] / max(1, d["max"])
                        job.progress = Progress(stage=stage, step=d["value"], total_steps=d["max"],
                                                percent=round(_PREP + (100 - _PREP - _SAVE) * (k + frac) / n_samplers, 1))
                elif t == "execution_error":
                    raise _engine_error(d)
                elif t == "execution_interrupted":
                    if self.cancel_flags[job.job_id].is_set():
                        raise _Cancelled()
                    raise JobFailed(ErrorCode.ENGINE_ERROR, "执行被中断")
                elif t == "execution_success" or (t == "executing" and d.get("node") is None):
                    break
        finally:
            ws.close()

        # 4. 取回结果
        job.progress = Progress(stage="saving", percent=100 - _SAVE)
        hist = self._wait_history(prompt_id)
        if (hist.get("status") or {}).get("status_str") == "error":
            raise JobFailed(ErrorCode.ENGINE_ERROR, "ComfyUI 执行失败", hist.get("status"))
        out_dir = settings.output_dir / datetime.now().strftime("%Y-%m-%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        fmt = req.output.format
        ext = {"png": "png", "webp": "webp", "jpeg": "jpg"}[fmt]
        # v2：client_meta.form 是前端放进来的各字段原值与画幅、参考设置（不含参考图），读图时据此还原到各字段
        meta = {"format": "anima-studio/image", "version": 2, "job_id": job.job_id, "params": p.model_dump(),
                "prompt": req.prompt.model_dump()} if req.output.embed_metadata else None
        if meta is not None and isinstance(req.client_meta.get("form"), dict):
            meta["form"] = req.client_meta["form"]
        files = self.files.setdefault(job.job_id, {})
        for node_id, kind in wf.outputs.items():
            imgs = hist.get("outputs", {}).get(node_id, {}).get("images", [])
            for i, im in enumerate(imgs):
                raw = self.client.view(im["filename"], im.get("subfolder", ""), im.get("type", "temp"))
                use_fmt = fmt if kind == "final" else "png"
                data, w, h = images.encode_output(raw, use_fmt, req.output.quality,
                                                  meta if kind == "final" else None)
                name = f"{job.job_id}_{kind}_{i}.{ext if kind == 'final' else 'png'}"
                path = out_dir / name
                path.write_bytes(data)
                files[name] = path
                res = ImageResult(index=i, kind=kind, url=f"/v1/jobs/{job.job_id}/files/{name}", path=str(path),
                                  width=w, height=h, seed=p.seed if kind == "final" else None,
                                  base64=base64.b64encode(data).decode() if req.output.return_base64 else None)
                (job.images if kind == "final" else job.intermediates).append(res)
        if not job.images:
            raise JobFailed(ErrorCode.ENGINE_ERROR, "ComfyUI 没有返回图片", hist.get("status"))

    def _wait_history(self, prompt_id: str, wait: float = 30.0) -> dict:
        """ComfyUI 先发 execution_success、收尾后才把这次执行写进历史记录，紧接着去读可能还是空的：
        等记录出现（一次性整条写入）再返回，超时返回空。"""
        deadline = time.time() + wait
        while True:
            hist = self.client.history(prompt_id)
            if hist or time.time() > deadline:
                if not hist:
                    log.warning("prompt %s: history still empty after %.0fs", prompt_id, wait)
                return hist
            time.sleep(0.2)


_TRANSIENT_MARKERS = ("Fault failed", "device not ready", "VRAM Allocation failed")


def _is_transient(e: JobFailed) -> bool:
    msg = str(e) + " " + str((e.detail or {}).get("message", "") if isinstance(e.detail, dict) else "")
    return e.code == ErrorCode.ENGINE_ERROR and any(m in msg for m in _TRANSIENT_MARKERS)


def _engine_error(d: dict) -> JobFailed:
    msg = d.get("exception_message", "") or ""
    etype = d.get("exception_type", "") or ""
    detail = {"node_id": d.get("node_id"), "node_type": d.get("node_type"), "exception_type": etype,
              "message": msg.strip()}
    if "OutOfMemory" in etype or "out of memory" in msg.lower():
        return JobFailed(ErrorCode.OUT_OF_MEMORY, "显存不足，请降低分辨率/批量或减少参考图", detail)
    if "no person detected" in msg:
        return JobFailed(ErrorCode.NO_PERSON, "动作参考图中没有检测到人物", detail)
    return JobFailed(ErrorCode.ENGINE_ERROR, f"{d.get('node_type')}: {msg.strip()[:500]}", detail)
