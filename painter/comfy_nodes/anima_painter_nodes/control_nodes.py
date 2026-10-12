"""Control-LoRA 注入（Anima-Control-Pose 训练契约的复现）。

与原版 AnimaControlApply 的区别：
  1. 控制 token 只加到 x_embedder 输出的前 n_gen 帧（生成帧）。in-context 人物参考会把参考帧
     拼在 T 轴后面，原版会把骨架信号也广播到参考帧上。
  2. 控制 latent 与生成 latent 尺寸不一致时直接报错，而不是静默广播失败。
  3. ControlEmbedder 按文件路径缓存，避免每次执行重复读盘。
LoRA 的低秩增量仍由上游 LoraLoaderModelOnly 加载。
"""
import os

import torch
import torch.nn as nn
from einops import rearrange

WRAPPER_KEY = "anima_painter_control"
_EMBEDDER_CACHE = {}


class ControlEmbedder(nn.Module):
    """与训练时相同的零初始化 patch-embed（spatial_patch=2, temporal_patch=1）。"""

    def __init__(self, in_channels=16, spatial_patch_size=2, temporal_patch_size=1, model_channels=2048):
        super().__init__()
        self.spatial_patch_size = spatial_patch_size
        self.temporal_patch_size = temporal_patch_size
        self.proj = nn.Linear(in_channels * spatial_patch_size * spatial_patch_size * temporal_patch_size, model_channels)
        nn.init.zeros_(self.proj.weight)
        nn.init.zeros_(self.proj.bias)

    def forward(self, x_B_C_T_H_W):
        x = rearrange(
            x_B_C_T_H_W,
            "b c (t r) (h m) (w n) -> b t h w (c r m n)",
            r=self.temporal_patch_size, m=self.spatial_patch_size, n=self.spatial_patch_size,
        )
        return self.proj(x)


def _resolve(path):
    if os.path.isabs(path):
        return path
    import folder_paths
    full = folder_paths.get_full_path("loras", path)
    if not full:
        raise FileNotFoundError(f"control adapter not found in models/loras: {path}")
    return full


def _load_embedder(path, model_channels):
    key = (path, model_channels)
    if key not in _EMBEDDER_CACHE:
        import safetensors.torch as st
        sd = {k.split("control_embedder.", 1)[1]: v for k, v in st.load_file(path).items() if "control_embedder." in k}
        if not sd:
            raise ValueError(f"no control_embedder.* keys in {path}")
        emb = ControlEmbedder(in_channels=sd["proj.weight"].shape[1] // 4, model_channels=model_channels)
        emb.load_state_dict(sd, strict=True)
        _EMBEDDER_CACHE[key] = emb.eval()
    return _EMBEDDER_CACHE[key]


class AnimaPainterControlApply:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": ("MODEL",),
            "control_latent": ("LATENT",),
            "adapter_name": ("STRING", {"default": "anima_pose_preview2.safetensors"}),
            "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05}),
        }}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "apply"
    CATEGORY = "AnimaPainter"

    def apply(self, model, control_latent, adapter_name, strength):
        if strength <= 0:
            return (model,)
        dit = model.get_model_object("diffusion_model")
        embedder = _load_embedder(_resolve(adapter_name), getattr(dit, "model_channels", 2048))
        ctrl = control_latent["samples"]
        if ctrl.ndim == 4:
            ctrl = ctrl.unsqueeze(2)
        # 控制 latent 必须与采样器空间一致（训练时 VAE latent 直接进 embedder，未做 process_latent_in）
        n_ctrl_frames = ctrl.shape[2]

        def wrapper(executor, *args, **kwargs):
            x = args[0]
            if x.shape[-2:] != ctrl.shape[-2:]:
                raise ValueError(
                    f"pose control latent {tuple(ctrl.shape[-2:])} != generation latent {tuple(x.shape[-2:])}; "
                    "skeleton must be rendered at the generation width/height")
            dev, dt = x.device, x.dtype
            embedder.to(dev, dt)
            tokens = strength * embedder(ctrl.to(dev, dt))  # (1, Tc, h, w, D)

            def add_control(_module, _inputs, output):
                out = output.clone()
                out[:, :n_ctrl_frames] = out[:, :n_ctrl_frames] + tokens.to(out.device, out.dtype)
                return out

            handle = dit.x_embedder.register_forward_hook(add_control)
            try:
                return executor(*args, **kwargs)
            finally:
                handle.remove()

        m = model.clone()
        m.add_wrapper_with_key("diffusion_model", WRAPPER_KEY, wrapper)
        return (m,)
