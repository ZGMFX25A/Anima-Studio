"""模型注册表：models.json + ComfyUI 实际可用文件。"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .comfy_client import ComfyClient

FEATURES = ("character_ref", "pose_ref")


@dataclass
class ModelEntry:
    id: str
    file: str
    label: str
    family: str
    defaults: dict
    features: dict[str, bool]
    notes: dict[str, str] = field(default_factory=dict)


@dataclass
class Registry:
    text_encoder: str
    clip_type: str
    vae: str
    default_model: str
    models: dict[str, ModelEntry]
    adapters: dict[str, dict]
    # 以下由 ComfyUI 实时查询，带缓存
    _client: ComfyClient | None = None
    _cache: dict = field(default_factory=dict)
    _cache_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def load(cls, path: Path, client: ComfyClient) -> "Registry":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        models = {
            mid: ModelEntry(id=mid, file=m["file"], label=m.get("label", mid), family=m["family"],
                            defaults=m["defaults"], features={f: bool(m.get("features", {}).get(f)) for f in FEATURES},
                            notes=m.get("notes", {}))
            for mid, m in d["models"].items()
        }
        return cls(text_encoder=d["text_encoder"], clip_type=d.get("clip_type", "stable_diffusion"), vae=d["vae"],
                   default_model=d["default_model"], models=models, adapters=d["adapters"], _client=client)

    def engine_lists(self, max_age: float = 30.0) -> dict[str, list[str]]:
        """ComfyUI 中实际存在的模型文件 / 采样器 / 调度器 / LoRA / 超分模型。"""
        with self._lock:
            if self._cache and time.time() - self._cache_at < max_age:
                return self._cache
            c = self._client
            self._cache = {
                "diffusion_models": c.input_choices("UNETLoader", "unet_name"),
                "loras": c.input_choices("LoraLoaderModelOnly", "lora_name"),
                "samplers": c.input_choices("KSampler", "sampler_name"),
                "schedulers": c.input_choices("KSampler", "scheduler"),
                "upscale_models": c.input_choices("UpscaleModelLoader", "model_name"),
                "nodes": list(c._get("/object_info").json().keys()),
            }
            self._cache_at = time.time()
            return self._cache

    def get(self, model_id: str | None) -> ModelEntry | None:
        return self.models.get(model_id or self.default_model)
