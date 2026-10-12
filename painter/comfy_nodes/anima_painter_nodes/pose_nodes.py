"""任意宽高比的动作骨架节点。

原版 AnimaPoseControl 把输入强行缩放成正方形再检测/渲染，非正方形出图时骨架会变形且尺寸
与生成 latent 不一致。这里：按原图比例检测 → 关键点等比映射到目标画布（contain/cover）→
在 width×height 画布上按训练时的 R0_thin 规格渲染（黑底、线宽 2、关节半径 3、DWPose 配色）。
"""
import numpy as np
import torch

BODY = list(range(17))
FEET = list(range(17, 23))
FACE = list(range(23, 91))
HAND = list(range(91, 133))
COCO_LIMBS = [(5, 7), (7, 9), (6, 8), (8, 10), (11, 13), (13, 15), (12, 14), (14, 16),
              (5, 6), (11, 12), (5, 11), (6, 12), (0, 5), (0, 6)]
_PAL = [(255, 0, 0), (255, 128, 0), (255, 255, 0), (128, 255, 0), (0, 255, 0), (0, 255, 128), (0, 255, 255),
        (0, 128, 255), (0, 0, 255), (128, 0, 255), (255, 0, 255), (255, 0, 128), (180, 180, 180), (220, 220, 220)]
LIMB_COLOR = {l: _PAL[i % len(_PAL)] for i, l in enumerate(COCO_LIMBS)}
STYLES = {"R0_thin": (2, 3), "R1_thick": (8, 6), "R2_puppet": (22, 11)}
KPT_THR = 0.3

_ESTIMATOR = None


def _estimator():
    global _ESTIMATOR
    if _ESTIMATOR is None:
        from rtmlib import Wholebody
        # 与 Anima-Control-Pose 训练数据相同的检测器配置（DWPose / COCO-WholeBody 133 点）
        _ESTIMATOR = Wholebody(to_openpose=False, mode="balanced", backend="onnxruntime", device="cpu")
    return _ESTIMATOR


def detect(img_rgb):
    """img_rgb: HWC uint8. 返回 [(kp[133,2], sc[133]), ...]，坐标为原图像素。"""
    import cv2
    kp_all, sc_all = _estimator()(cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR))
    if kp_all is None or len(kp_all) == 0:
        return []
    return [(np.asarray(k, float), np.asarray(s, float)) for k, s in zip(kp_all, sc_all)]


def _bbox_area(kp, sc):
    pts = kp[BODY][sc[BODY] >= KPT_THR]
    if len(pts) < 2:
        return 0.0
    w, h = pts.max(0) - pts.min(0)
    return float(w * h)


def person_transform(people, dst_w, dst_h, margin):
    """以人物关键点包围盒为准缩放居中，使人物占满画布（留 margin 比例边距）。
    实测 Anima-Control-Pose 只在骨架占满画面时才可靠跟随（训练数据为满画幅全身图）。"""
    pts = np.concatenate([kp[sc >= KPT_THR] for kp, sc in people if (sc >= KPT_THR).any()], axis=0)
    x0, y0 = pts.min(0)
    x1, y1 = pts.max(0)
    bw, bh = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    avail_w, avail_h = dst_w * (1 - 2 * margin), dst_h * (1 - 2 * margin)
    scale = min(avail_w / bw, avail_h / bh)
    ox = dst_w / 2.0 - (x0 + x1) / 2.0 * scale
    oy = dst_h / 2.0 - (y0 + y1) / 2.0 * scale
    return scale, ox, oy


def fit_transform(src_w, src_h, dst_w, dst_h, mode):
    scale = min(dst_w / src_w, dst_h / src_h) if mode == "contain" else max(dst_w / src_w, dst_h / src_h)
    ox = (dst_w - src_w * scale) / 2.0
    oy = (dst_h - src_h * scale) / 2.0
    return scale, ox, oy


def render(people, width, height, style="R0_thin", hands=True, face=True, feet=True):
    import cv2
    stick, joint = STYLES[style]
    cv = np.zeros((height, width, 3), np.uint8)
    for kp, sc in people:
        sc = sc.copy()
        if not hands:
            sc[HAND] = 0
        if not face:
            sc[FACE] = 0
        if not feet:
            sc[FEET] = 0
        p = lambda i: (int(round(kp[i][0])), int(round(kp[i][1])))
        for a, b in COCO_LIMBS:
            if sc[a] >= KPT_THR and sc[b] >= KPT_THR:
                cv2.line(cv, p(a), p(b), LIMB_COLOR[(a, b)], stick)
        for i in BODY:
            if sc[i] >= KPT_THR:
                cv2.circle(cv, p(i), joint, (255, 255, 255), -1)
        for i in FEET:
            if sc[i] >= KPT_THR:
                cv2.circle(cv, p(i), max(1, joint // 2), (255, 255, 255), -1)
        if style == "R0_thin":
            for i in HAND:
                if sc[i] >= KPT_THR:
                    cv2.circle(cv, p(i), 2, (0, 255, 255), -1)
            for i in FACE:
                if sc[i] >= KPT_THR:
                    cv2.circle(cv, p(i), 2, (255, 255, 255), -1)
    return cv


def _crop_to_content(img, margin):
    """骨架图（黑底）裁到非黑内容的包围盒，并按 margin 外扩。"""
    ys, xs = np.nonzero(img.max(axis=2) > 16)
    if len(xs) == 0:
        return img
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    pw, ph = int((x1 - x0) * margin), int((y1 - y0) * margin)
    out = np.zeros((y1 - y0 + 2 * ph, x1 - x0 + 2 * pw, 3), np.uint8)
    out[ph:ph + y1 - y0, pw:pw + x1 - x0] = img[y0:y1, x0:x1]
    return out


def letterbox(img, width, height, mode, fill=0):
    import cv2
    h, w = img.shape[:2]
    scale, ox, oy = fit_transform(w, h, width, height, mode)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    canvas = np.full((height, width, 3), fill, np.uint8)
    x0, y0 = round(ox), round(oy)
    sx0, sy0 = max(0, -x0), max(0, -y0)
    dx0, dy0 = max(0, x0), max(0, y0)
    cw, ch = min(nw - sx0, width - dx0), min(nh - sy0, height - dy0)
    canvas[dy0:dy0 + ch, dx0:dx0 + cw] = resized[sy0:sy0 + ch, sx0:sx0 + cw]
    return canvas


class AnimaPainterPoseSkeleton:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "width": ("INT", {"default": 1024, "min": 256, "max": 4096, "step": 16}),
            "height": ("INT", {"default": 1024, "min": 256, "max": 4096, "step": 16}),
            "input_type": (["photo", "skeleton"],),
            "fit": (["person", "contain", "cover"],),
            "margin": ("FLOAT", {"default": 0.05, "min": 0.0, "max": 0.4, "step": 0.01}),
            "style": (list(STYLES),),
            "persons": (["largest", "all"],),
            "hands": ("BOOLEAN", {"default": True}),
            "face": ("BOOLEAN", {"default": True}),
            "feet": ("BOOLEAN", {"default": True}),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("skeleton",)
    FUNCTION = "run"
    CATEGORY = "AnimaPainter"

    def run(self, image, width, height, input_type, fit, margin, style, persons, hands, face, feet):
        src = (image[0, :, :, :3].cpu().numpy() * 255).clip(0, 255).astype(np.uint8)
        if input_type == "skeleton":
            if fit == "person":
                src = _crop_to_content(src, margin)
                fit = "contain"
            out = letterbox(src, width, height, fit, fill=0)
        else:
            people = detect(src)
            if not people:
                raise ValueError("AnimaPainterPoseSkeleton: no person detected in pose reference image")
            if persons == "largest":
                people = [max(people, key=lambda p: _bbox_area(*p))]
            if fit == "person":
                scale, ox, oy = person_transform(people, width, height, margin)
            else:
                scale, ox, oy = fit_transform(src.shape[1], src.shape[0], width, height, fit)
            people = [(kp * scale + np.array([ox, oy]), sc) for kp, sc in people]
            out = render(people, width, height, style, hands, face, feet)
        return (torch.from_numpy(out.astype(np.float32) / 255.0)[None],)
