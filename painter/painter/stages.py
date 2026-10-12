"""后处理阶段（精细重绘、超分）的插件接口。

每个阶段是一个 PostStage 子类，在同一个 ComfyUI 工作流里接在基础图之后：
    IMAGE ──► stage.build(ctx, image, spec) ──► IMAGE
新增实现时：继承 PostStage，实现 availability() 与 build()，然后在 STAGES 中替换占位实现。
请求结构（RefineSpec / UpscaleSpec）已在 schemas.py 中固定，实现不应改动对外字段；
实现特有参数放进 spec.params。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel

from .graph import Graph, Out
from .registry import ModelEntry, Registry
from .schemas import GenerateRequest, RefineSpec, UpscaleSpec


@dataclass
class BuildContext:
    """阶段可用的上下文（均为工作流中已存在的节点输出）。"""
    graph: Graph
    req: GenerateRequest
    registry: Registry
    model_entry: ModelEntry
    model: Out            # MODEL：基础模型 + 用户 LoRA + 已启用的参考
    plain_model: Out      # MODEL：基础模型 + 用户 LoRA（不含参考）
    clip: Out
    vae: Out
    positive: Out
    negative: Out
    seed: int
    steps: int
    cfg: float
    sampler_name: str
    scheduler: str
    width: int            # 进入本阶段的图片尺寸
    height: int


@dataclass
class StageResult:
    image: Out
    width: int
    height: int


class PostStage(ABC):
    name: ClassVar[str]
    spec_type: ClassVar[type[BaseModel]]

    def spec(self, req: GenerateRequest) -> BaseModel | None:
        s = getattr(req, self.name)
        return s if s is not None and s.enabled else None

    @abstractmethod
    def availability(self, registry: Registry) -> tuple[bool, str | None]:
        """(是否可用, 不可用原因)。"""

    def models(self, registry: Registry) -> list[str]:
        return []

    def validate(self, spec: BaseModel, req: GenerateRequest, registry: Registry) -> None:
        """请求级校验，失败抛 ValueError。"""

    @abstractmethod
    def build(self, ctx: BuildContext, image: Out, spec: BaseModel) -> StageResult:
        """把本阶段节点加入 ctx.graph，输入/输出均为 IMAGE。"""


class RefineStagePlaceholder(PostStage):
    name = "refine"
    spec_type = RefineSpec

    def availability(self, registry):
        return False, "精细重绘实现尚未安装（接口已预留）"

    def build(self, ctx, image, spec):
        raise NotImplementedError


class UpscaleStagePlaceholder(PostStage):
    name = "upscale"
    spec_type = UpscaleSpec

    def availability(self, registry):
        return False, "超分实现尚未安装（接口已预留）"

    def models(self, registry):
        return registry.engine_lists().get("upscale_models", [])

    def build(self, ctx, image, spec):
        raise NotImplementedError


STAGES: dict[str, PostStage] = {
    "refine": RefineStagePlaceholder(),
    "upscale": UpscaleStagePlaceholder(),
}
