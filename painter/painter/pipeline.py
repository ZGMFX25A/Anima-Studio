"""请求 → 校验 → ComfyUI 工作流。"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .graph import Graph, Out
from .registry import ModelEntry, Registry
from .schemas import ErrorCode, GenerateRequest, ResolvedParams
from .stages import STAGES, BuildContext


class RequestError(ValueError):
    def __init__(self, code: ErrorCode, message: str, detail=None):
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class Uploads:
    """已上传到 ComfyUI input 目录的图片名。"""
    character_refs: list[str] = field(default_factory=list)
    pose: str | None = None


@dataclass
class BuiltWorkflow:
    graph: dict
    outputs: dict[str, str]        # 输出节点 id → kind（final / base / pose_skeleton / refine / upscale）
    sampler_nodes: dict[str, str]  # 采样节点 id → 阶段名（用于进度）


def resolve(req: GenerateRequest, reg: Registry) -> tuple[ModelEntry, ResolvedParams]:
    entry = reg.get(req.model)
    if entry is None:
        raise RequestError(ErrorCode.UNKNOWN_MODEL, f"未知模型: {req.model}", sorted(reg.models))
    eng = reg.engine_lists()
    if entry.file not in eng["diffusion_models"]:
        raise RequestError(ErrorCode.UNKNOWN_MODEL, f"模型文件未安装: {entry.file}")

    char_on = bool(req.character_ref and req.character_ref.enabled)
    pose_on = bool(req.pose_ref and req.pose_ref.enabled)
    for feat, on in (("character_ref", char_on), ("pose_ref", pose_on)):
        if on and not entry.features.get(feat):
            raise RequestError(ErrorCode.FEATURE_UNSUPPORTED, f"模型 {entry.id} 不支持 {feat}")
        if on and reg.adapters[feat]["lora"] not in eng["loras"]:
            raise RequestError(ErrorCode.FEATURE_UNSUPPORTED, f"{feat} 适配器未安装: {reg.adapters[feat]['lora']}")

    for l in req.loras:
        if l.name not in eng["loras"]:
            raise RequestError(ErrorCode.INVALID_REQUEST, f"LoRA 不存在: {l.name}")

    d = entry.defaults
    s = req.sampler
    sampler_name = s.sampler_name or d["sampler_name"]
    scheduler = s.scheduler or d["scheduler"]
    if sampler_name not in eng["samplers"]:
        raise RequestError(ErrorCode.INVALID_REQUEST, f"未知采样器: {sampler_name}")
    if scheduler not in eng["schedulers"]:
        raise RequestError(ErrorCode.INVALID_REQUEST, f"未知调度器: {scheduler}")

    stages = []
    for name in req.post_order:
        stage = STAGES[name]
        spec = stage.spec(req)
        if spec is None:
            continue
        ok, why = stage.availability(reg)
        if not ok:
            raise RequestError(ErrorCode.STAGE_UNAVAILABLE, f"{name}: {why}")
        try:
            stage.validate(spec, req, reg)
        except ValueError as e:
            raise RequestError(ErrorCode.INVALID_REQUEST, f"{name}: {e}") from e
        stages.append(name)

    return entry, ResolvedParams(
        model=entry.id, model_file=entry.file, width=req.width, height=req.height,
        seed=req.seed if req.seed is not None else random.randrange(2**53),
        batch_size=req.batch_size, steps=s.steps or d["steps"], cfg=s.cfg if s.cfg is not None else d["cfg"],
        sampler_name=sampler_name, scheduler=scheduler,
        character_ref=char_on, pose_ref=pose_on, stages=stages,
    )


def build(req: GenerateRequest, reg: Registry, entry: ModelEntry, p: ResolvedParams, up: Uploads) -> BuiltWorkflow:
    g = Graph()
    outputs: dict[str, str] = {}
    sampler_nodes: dict[str, str] = {}
    want_inter = req.output.include_intermediates

    model = g.node("UNETLoader", "model", unet_name=entry.file, weight_dtype="default")
    clip = g.node("CLIPLoader", "text_encoder", clip_name=reg.text_encoder, type=reg.clip_type)
    vae = g.node("VAELoader", "vae", vae_name=reg.vae)

    for i, l in enumerate(req.loras):
        model = g.node("LoraLoaderModelOnly", f"lora_{i}", model=model, lora_name=l.name, strength_model=l.strength)
    plain_model = model

    # ── 人物参考 ──
    if p.character_ref:
        cr = req.character_ref
        ad = reg.adapters["character_ref"]
        model = g.node("LoraLoaderModelOnly", "character_ref_lora", model=model,
                       lora_name=ad["lora"], strength_model=ad.get("lora_strength", 1.0))
        ref_latent: Out | None = None
        for i, name in enumerate(up.character_refs):
            img, _mask = g.node("LoadImage", f"character_ref_{i}", outputs=2, image=name)
            lat = g.node("AnimaRefEncode", f"character_ref_encode_{i}", vae=vae, image=img,
                         target_width=p.width, target_height=p.height)
            ref_latent = lat if ref_latent is None else g.node(
                "AnimaRefLatentBatch", f"character_ref_batch_{i}", ref_latent_1=ref_latent, ref_latent_2=lat,
                fit_mode="pad")
        model = g.node("AnimaInContextApply", "character_ref_apply", model=model, ref_latent=ref_latent,
                       strength=cr.strength, start_percent=cr.start_percent, end_percent=cr.end_percent,
                       cond_only=True, fit_mode="pad", ref_timestep=0.0)

    # ── 动作参考 ──
    if p.pose_ref:
        pr = req.pose_ref
        ad = reg.adapters["pose_ref"]
        model = g.node("LoraLoaderModelOnly", "pose_ref_lora", model=model,
                       lora_name=ad["lora"], strength_model=ad.get("lora_strength", 1.0))
        img, _mask = g.node("LoadImage", "pose_ref_image", outputs=2, image=up.pose)
        skel = g.node("AnimaPainterPoseSkeleton", "pose_skeleton", image=img, width=p.width, height=p.height,
                      input_type=pr.input_type, fit=pr.fit, margin=pr.margin, style="R0_thin", persons=pr.persons,
                      hands=pr.hands, face=pr.face, feet=pr.feet)
        ctrl = g.node("VAEEncode", "pose_encode", pixels=skel, vae=vae)
        model = g.node("AnimaPainterControlApply", "pose_ref_apply", model=model, control_latent=ctrl,
                       adapter_name=ad["lora"], strength=pr.strength)
        if want_inter:
            outputs[g.add("PreviewImage", "out_pose_skeleton", images=skel)] = "pose_skeleton"

    # ── 基础采样 ──
    pos = g.node("CLIPTextEncode", "positive", clip=clip, text=req.prompt.positive)
    neg = g.node("CLIPTextEncode", "negative", clip=clip, text=req.prompt.negative)
    latent = g.node("EmptyLatentImage", "latent", width=p.width, height=p.height, batch_size=p.batch_size)
    samples = g.node("KSampler", "base_sampler", model=model, seed=p.seed, steps=p.steps, cfg=p.cfg,
                     sampler_name=p.sampler_name, scheduler=p.scheduler, positive=pos, negative=neg,
                     latent_image=latent, denoise=1.0)
    sampler_nodes[samples.node] = "base"
    image = g.node("VAEDecode", "base_decode", samples=samples, vae=vae)

    # ── 后处理阶段 ──
    w, h = p.width, p.height
    prev = "base"
    for name in p.stages:
        if want_inter:
            outputs[g.add("PreviewImage", f"out_{prev}", images=image)] = prev
        prev = name
        before = set(g.nodes)
        ctx = BuildContext(graph=g, req=req, registry=reg, model_entry=entry, model=model, plain_model=plain_model,
                           clip=clip, vae=vae, positive=pos, negative=neg, seed=p.seed, steps=p.steps, cfg=p.cfg,
                           sampler_name=p.sampler_name, scheduler=p.scheduler, width=w, height=h)
        res = STAGES[name].build(ctx, image, STAGES[name].spec(req))
        image, w, h = res.image, res.width, res.height
        for nid in set(g.nodes) - before:
            if g.nodes[nid]["class_type"] in ("KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced"):
                sampler_nodes[nid] = name

    outputs[g.add("PreviewImage", "out_final", images=image)] = "final"
    return BuiltWorkflow(graph=g.to_api(), outputs=outputs, sampler_nodes=sampler_nodes)
