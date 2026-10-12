"""Anima Painter 专用 ComfyUI 节点。

- AnimaPainterPoseSkeleton: 任意宽高比的动作骨架生成（照片检测 / 现成骨架图）。
- AnimaPainterControlApply: Anima Control-LoRA 的 ControlEmbedder 注入，只作用于生成帧，
  与 in-context 人物参考（参考帧拼接在 T 轴上）共存时不会污染参考帧。
"""
from .pose_nodes import AnimaPainterPoseSkeleton
from .control_nodes import AnimaPainterControlApply

NODE_CLASS_MAPPINGS = {
    "AnimaPainterPoseSkeleton": AnimaPainterPoseSkeleton,
    "AnimaPainterControlApply": AnimaPainterControlApply,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "AnimaPainterPoseSkeleton": "Anima Painter Pose Skeleton",
    "AnimaPainterControlApply": "Anima Painter Control Apply",
}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
