"""绘画模块对外接口契约（前端 ⇄ 绘画模块）。

所有图片输入均为 base64 字符串（可带 data URL 前缀，PNG/JPEG/WebP）。
所有尺寸单位为像素。字段为 None 表示"使用模型默认值"。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ───────────────────────────── 输入：通用 ─────────────────────────────

class Prompt(_Strict):
    positive: str = Field(..., min_length=1, description="正向提示词，原样送入文本编码器（质量词/画师 tag 由前端拼好）")
    negative: str = Field("", description="负向提示词；cfg=1 时无效")


class SamplerParams(_Strict):
    steps: int | None = Field(None, ge=1, le=150)
    cfg: float | None = Field(None, ge=1.0, le=30.0)
    sampler_name: str | None = Field(None, description="ComfyUI 采样器名，可选值见 /v1/capabilities")
    scheduler: str | None = Field(None, description="ComfyUI 调度器名，可选值见 /v1/capabilities")


class LoraSpec(_Strict):
    name: str = Field(..., description="models/loras 下的文件名")
    strength: float = Field(1.0, ge=-4.0, le=4.0)


# ───────────────────────────── 输入：参考 ─────────────────────────────

class RefImage(_Strict):
    image: str = Field(..., description="base64 图片。带 alpha 通道时自动以 alpha 为主体蒙版，主体合成到白底")
    mask: str | None = Field(None, description="可选 base64 灰度蒙版，白=主体。优先级高于 alpha")


class CharacterRef(_Strict):
    """人物参考：Anima In-Context Character LoRA + 参考帧注入。"""
    enabled: bool = False
    images: list[RefImage] = Field(default_factory=list, max_length=3,
                                   description="1~3 张，推荐 全身 + 脸部特写；主体在白底上效果最好")
    strength: float = Field(1.0, ge=0.0, le=3.0, description="参考注意力强度，1.0 中性；像不像不够时 1.2~1.5")
    start_percent: float = Field(0.0, ge=0.0, le=1.0, description="参考生效的采样区间起点")
    end_percent: float = Field(1.0, ge=0.0, le=1.0, description="参考生效的采样区间终点")

    @model_validator(mode="after")
    def _check(self):
        if self.enabled and not self.images:
            raise ValueError("character_ref.enabled 时 images 至少 1 张")
        if self.start_percent >= self.end_percent:
            raise ValueError("character_ref.start_percent 必须小于 end_percent")
        return self


class PoseRef(_Strict):
    """动作参考：Anima-Control-Pose Control-LoRA。"""
    enabled: bool = False
    image: str | None = Field(None, description="base64 图片")
    input_type: Literal["photo", "skeleton"] = Field(
        "photo", description="photo=任意人物图，自动检测骨架；skeleton=已渲染好的 DWPose 骨架图（黑底）")
    strength: float = Field(1.0, ge=0.0, le=2.0, description="1.0 标准跟随；越高越贴骨架但画质可能下降")
    fit: Literal["person", "contain", "cover"] = Field(
        "person", description="骨架放置方式：person=按人物包围盒缩放居中占满画面（推荐，模型只在骨架占满画面时可靠跟随）；"
                              "contain=整张参考图完整放入；cover=整张参考图裁切填满")
    margin: float = Field(0.05, ge=0.0, le=0.4, description="fit=person 时人物四周留白比例")
    persons: Literal["largest", "all"] = Field("largest", description="photo 模式下使用最大的人物或全部人物")
    hands: bool = True
    face: bool = True
    feet: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.enabled and not self.image:
            raise ValueError("pose_ref.enabled 时必须提供 image")
        return self


# ─────────────────────── 输入：后处理阶段（预留） ───────────────────────

class RefineSpec(_Strict):
    """精细重绘（预留接口，当前未安装实现；enabled=true 会返回 STAGE_UNAVAILABLE）。

    语义：在基础图之上做局部或全图低噪重绘。
    """
    enabled: bool = False
    model: str | None = Field(None, description="重绘模型 id，None=沿用基础模型")
    mode: Literal["full", "mask", "auto_face", "auto_hand"] = Field(
        "full", description="full=全图；mask=按 mask 重绘；auto_*=自动检测区域（需检测模型）")
    mask: str | None = Field(None, description="mode=mask 时的 base64 蒙版（白=重绘区域），尺寸与基础图一致")
    denoise: float = Field(0.35, ge=0.0, le=1.0)
    prompt: Prompt | None = Field(None, description="None=沿用基础提示词")
    sampler: SamplerParams = Field(default_factory=SamplerParams)
    keep_references: bool = Field(True, description="重绘时是否继续施加人物/动作参考")
    params: dict[str, Any] = Field(default_factory=dict, description="实现相关的扩展参数")


class UpscaleSpec(_Strict):
    """超分（预留接口，当前未安装实现；enabled=true 会返回 STAGE_UNAVAILABLE）。"""
    enabled: bool = False
    model: str | None = Field(None, description="超分模型 id（models/upscale_models 下）")
    scale: float = Field(2.0, ge=1.0, le=8.0, description="目标放大倍数")
    tile_size: int | None = Field(None, ge=128, le=2048, description="分块大小；None=实现自定")
    denoise: float = Field(0.0, ge=0.0, le=1.0, description=">0 时放大后再做一次低噪重绘（hires fix）")
    params: dict[str, Any] = Field(default_factory=dict, description="实现相关的扩展参数")


class OutputSpec(_Strict):
    format: Literal["png", "webp", "jpeg"] = "png"
    quality: int = Field(95, ge=1, le=100, description="webp/jpeg 质量")
    return_base64: bool = Field(False, description="任务结果里直接附带 base64（否则只给 URL）")
    include_intermediates: bool = Field(False, description="同时返回中间产物：骨架图、各阶段前的图")
    embed_metadata: bool = Field(True, description="在 PNG 中写入生成参数")


# ───────────────────────────── 输入：总请求 ─────────────────────────────

class GenerateRequest(_Strict):
    schema_version: str = SCHEMA_VERSION
    model: str | None = Field(None, description="模型 id，见 /v1/capabilities；None=默认模型")
    prompt: Prompt
    width: int = Field(1024, ge=256, le=2048, description="必须为 16 的倍数；推荐总像素 ≈ 1024²")
    height: int = Field(1024, ge=256, le=2048, description="必须为 16 的倍数")
    seed: int | None = Field(None, ge=0, le=2**53 - 1, description="None=随机（不超过 2^53，JS 可安全表示）；批量时同一 seed 产生一组不同图")
    batch_size: int = Field(1, ge=1, le=4)
    sampler: SamplerParams = Field(default_factory=SamplerParams)
    loras: list[LoraSpec] = Field(default_factory=list, max_length=8)
    character_ref: CharacterRef | None = None
    pose_ref: PoseRef | None = None
    refine: RefineSpec | None = None
    upscale: UpscaleSpec | None = None
    post_order: list[Literal["refine", "upscale"]] = Field(
        default_factory=lambda: ["refine", "upscale"], description="后处理阶段执行顺序")
    output: OutputSpec = Field(default_factory=OutputSpec)
    client_meta: dict[str, Any] = Field(default_factory=dict, description="前端透传字段，原样返回")

    @field_validator("width", "height")
    @classmethod
    def _mult16(cls, v: int) -> int:
        if v % 16:
            raise ValueError("宽高必须是 16 的倍数")
        return v

    @field_validator("post_order")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("post_order 不能重复")
        return v


# ───────────────────────────── 输出 ─────────────────────────────

class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class ErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"          # 参数校验失败
    UNKNOWN_MODEL = "UNKNOWN_MODEL"
    FEATURE_UNSUPPORTED = "FEATURE_UNSUPPORTED"  # 该模型不支持所选参考
    STAGE_UNAVAILABLE = "STAGE_UNAVAILABLE"      # 精细重绘/超分实现未安装
    BAD_IMAGE = "BAD_IMAGE"                      # 图片无法解码
    NO_PERSON = "NO_PERSON"                      # 动作参考图里没检测到人
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"    # ComfyUI 未启动
    OUT_OF_MEMORY = "OUT_OF_MEMORY"
    ENGINE_ERROR = "ENGINE_ERROR"                # ComfyUI 执行出错
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class ApiError(BaseModel):
    code: ErrorCode
    message: str
    detail: Any = None


class Progress(BaseModel):
    stage: str = Field("queued", description="queued|preparing|base|refine|upscale|saving|done")
    step: int = 0
    total_steps: int = 0
    percent: float = Field(0.0, description="整个任务的估算进度 0~100")
    queue_position: int | None = None


class ImageResult(BaseModel):
    index: int
    kind: Literal["final", "base", "pose_skeleton", "refine", "upscale"] = "final"
    url: str = Field(..., description="相对 URL，GET 获取图片")
    path: str = Field(..., description="本机保存路径")
    width: int
    height: int
    seed: int | None = None
    base64: str | None = None


class ResolvedParams(BaseModel):
    """实际使用的参数（填充默认值后）。前端可用来复现。"""
    model: str
    model_file: str
    width: int
    height: int
    seed: int
    batch_size: int
    steps: int
    cfg: float
    sampler_name: str
    scheduler: str
    character_ref: bool
    pose_ref: bool
    stages: list[str]


class Job(BaseModel):
    schema_version: str = SCHEMA_VERSION
    job_id: str
    status: JobStatus
    progress: Progress = Field(default_factory=Progress)
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    params: ResolvedParams | None = None
    images: list[ImageResult] = Field(default_factory=list)
    intermediates: list[ImageResult] = Field(default_factory=list)
    error: ApiError | None = None
    client_meta: dict[str, Any] = Field(default_factory=dict)


# ───────────────────────────── 能力查询 ─────────────────────────────

class ModelInfo(BaseModel):
    id: str
    label: str
    family: str
    file: str
    installed: bool
    defaults: SamplerParams
    features: dict[str, bool]
    notes: dict[str, str] = Field(default_factory=dict, description="各功能的实测说明，前端可作为提示展示")


class StageInfo(BaseModel):
    name: str
    available: bool
    reason: str | None = None
    models: list[str] = Field(default_factory=list)


class Capabilities(BaseModel):
    schema_version: str = SCHEMA_VERSION
    default_model: str
    models: list[ModelInfo]
    samplers: list[str]
    schedulers: list[str]
    loras: list[str]
    stages: list[StageInfo]
    limits: dict[str, Any]
