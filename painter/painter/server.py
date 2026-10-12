"""绘画模块 HTTP 服务。启动: python -m painter"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from .comfy_client import ComfyClient, EngineUnavailable
from .config import settings
from .jobs import JobManager
from .pipeline import RequestError
from .registry import Registry
from .schemas import (SCHEMA_VERSION, ApiError, Capabilities, ErrorCode, GenerateRequest, Job, JobStatus,
                      ModelInfo, SamplerParams, StageInfo)
from .stages import STAGES

client = ComfyClient(settings.comfy_url)
registry = Registry.load(settings.registry_path, client)
manager = JobManager(client, registry)

app = FastAPI(title="Anima Painter", version=SCHEMA_VERSION,
              description="绘画模块：接收前端整理好的生成请求，驱动 ComfyUI 执行 Anima 工作流并返回图片。")

_STATUS = {
    ErrorCode.INVALID_REQUEST: 422, ErrorCode.UNKNOWN_MODEL: 422, ErrorCode.FEATURE_UNSUPPORTED: 422,
    ErrorCode.STAGE_UNAVAILABLE: 501, ErrorCode.BAD_IMAGE: 422, ErrorCode.ENGINE_UNAVAILABLE: 503,
}


def _err(code: ErrorCode, message: str, detail=None) -> JSONResponse:
    return JSONResponse(status_code=_STATUS.get(code, 500),
                        content={"error": ApiError(code=code, message=message, detail=detail).model_dump(mode="json")})


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, exc: RequestValidationError):
    detail = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]
    return _err(ErrorCode.INVALID_REQUEST, "请求参数校验失败", detail)


@app.exception_handler(RequestError)
async def _request_error(_: Request, exc: RequestError):
    return _err(exc.code, str(exc), exc.detail)


@app.exception_handler(EngineUnavailable)
async def _engine_down(_: Request, exc: EngineUnavailable):
    return _err(ErrorCode.ENGINE_UNAVAILABLE, str(exc))


@app.get("/v1/health")
def health():
    alive = client.alive()
    return {"status": "ok" if alive else "degraded", "engine": "up" if alive else "down",
            "schema_version": SCHEMA_VERSION, "queue": manager.q.qsize(),
            "running": manager.current[0] if manager.current else None}


@app.get("/v1/capabilities", response_model=Capabilities)
def capabilities():
    eng = registry.engine_lists(max_age=0)
    models = [ModelInfo(id=m.id, label=m.label, family=m.family, file=m.file,
                        installed=m.file in eng["diffusion_models"], defaults=SamplerParams(**m.defaults),
                        features={f: on and registry.adapters[f]["lora"] in eng["loras"] for f, on in m.features.items()},
                        notes=m.notes)
              for m in registry.models.values()]
    stages = []
    for name, st in STAGES.items():
        ok, why = st.availability(registry)
        stages.append(StageInfo(name=name, available=ok, reason=why, models=st.models(registry)))
    adapter_loras = {a["lora"] for a in registry.adapters.values()}
    return Capabilities(
        default_model=registry.default_model, models=models, samplers=eng["samplers"], schedulers=eng["schedulers"],
        loras=[l for l in eng["loras"] if l not in adapter_loras], stages=stages,
        limits={"width": [256, 2048], "height": [256, 2048], "size_multiple": 16, "batch_size": [1, 4],
                "character_ref_images": [1, 3], "max_input_side": settings.max_input_side})


@app.post("/v1/jobs", response_model=Job, status_code=202)
def create_job(req: GenerateRequest):
    return manager.submit(req)


@app.post("/v1/generate", response_model=Job)
async def generate(req: GenerateRequest):
    """同步便捷接口：提交并等待完成（适合调试；前端建议用 /v1/jobs + 轮询）。"""
    job = manager.submit(req)
    return await asyncio.to_thread(manager.wait, job.job_id, settings.job_timeout + 60)


@app.get("/v1/jobs", response_model=list[Job])
def list_jobs(limit: int = 50):
    return manager.list(limit)


@app.get("/v1/jobs/{job_id}", response_model=Job)
def get_job(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.post("/v1/jobs/{job_id}/cancel", response_model=Job)
def cancel_job(job_id: str):
    job = manager.cancel(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/v1/jobs/{job_id}/files/{name}")
def get_file(job_id: str, name: str):
    path = manager.file_path(job_id, name)
    if not path or not path.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(path)


@app.post("/v1/engine/free")
def free_vram(unload_models: bool = True):
    """释放 ComfyUI 显存（切换到其它显存密集任务前调用）。"""
    if manager.current:
        raise HTTPException(409, "a job is running")
    client.free(unload_models)
    return {"ok": True}


__all__ = ["app", "JobStatus"]
