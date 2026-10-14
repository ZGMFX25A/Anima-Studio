"""ComfyUI 引擎的显卡识别与启动参数。

- 显卡识别：本机是 NVIDIA 还是 AMD 显卡（Windows 显卡列表里的 PCI 厂商号），
  装的 ComfyUI 便携版是哪一版（便携版自带 PyTorch 的 torch/version.py，不用启动它）。
  两者明确对不上时（例如 AMD 显卡 + NVIDIA 版 ComfyUI，启动必然失败）不启动 ComfyUI，并说明该换哪个包。
- 额外启动参数：data/settings.json 的 engine.comfy_args，追加在 ComfyUI 命令行后面，重启 ComfyUI 后生效。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading

from . import config
from .llm import store

log = logging.getLogger("frontend.engine")

LABEL = {"nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel", "cpu": "CPU"}
PACKAGE = {"nvidia": "ComfyUI_windows_portable_nvidia.7z", "amd": "ComfyUI_windows_portable_amd.7z"}
_PCI_VENDOR = {"10DE": "nvidia", "1002": "amd", "8086": "intel"}
TORCH_VERSION_PY = config.COMFY_DIR / "python_embeded" / "Lib" / "site-packages" / "torch" / "version.py"

# ───────────────────────────── 显卡识别 ─────────────────────────────

_adapters: list[dict] | None = None
_adapters_lock = threading.Lock()


def _vendor_of(name: str, pnp: str) -> str:
    m = re.search(r"VEN_([0-9A-Fa-f]{4})", pnp or "")
    if m:
        return _PCI_VENDOR.get(m.group(1).upper(), "other")
    n = (name or "").lower()
    if "nvidia" in n:
        return "nvidia"
    if "radeon" in n or "amd" in n:
        return "amd"
    return "other"


def adapters() -> list[dict]:
    """本机显卡列表 [{name, vendor}]（只查一次）；查询失败返回空列表。"""
    global _adapters
    with _adapters_lock:
        if _adapters is None:
            # 非 ASCII 字符转成 \uXXXX 再输出，与控制台编码无关
            # （不用 [Console]::OutputEncoding：与 Get-CimInstance 同用时整条命令要多花约 8 秒）
            ps = ("$j = ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_VideoController | Select-Object Name, PNPDeviceID);"
                  "[regex]::Replace($j, '[^\\x00-\\x7F]', { param($m) '\\u{0:x4}' -f [int][char]$m.Value })")
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                rows = json.loads(r.stdout.decode("ascii", "replace").strip() or "[]")
                if isinstance(rows, dict):
                    rows = [rows]
                _adapters = [{"name": (x.get("Name") or "").strip(), "vendor": _vendor_of(x.get("Name"), x.get("PNPDeviceID"))}
                             for x in rows if isinstance(x, dict)]
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                log.warning("读取显卡列表失败：%s", e)
                _adapters = []
        return _adapters


def machine_vendor(gpus: list[dict]) -> str | None:
    """用来出图的显卡厂商：有 NVIDIA 就用 NVIDIA（笔记本常见核显 + NVIDIA 独显），其次 AMD。"""
    vendors = {g["vendor"] for g in gpus}
    for v in ("nvidia", "amd"):
        if v in vendors:
            return v
    return None


def comfy_flavor() -> dict:
    """已装的 ComfyUI 便携版：{installed, flavor: nvidia|amd|intel|cpu|None, torch}。"""
    if not config.COMFY_PYTHON.exists():
        return {"installed": False, "flavor": None, "torch": None}
    try:
        text = TORCH_VERSION_PY.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"installed": True, "flavor": None, "torch": None}   # 没装 PyTorch 或读不到：不判断
    vals = {}
    for k, v in re.findall(r"^(__version__|cuda|hip|rocm|xpu)\s*(?::[^=\n]*)?=\s*(.+?)\s*$", text, re.M):
        v = v.strip("'\"")
        vals[k] = None if v == "None" else v
    if vals.get("cuda"):
        flavor = "nvidia"
    elif vals.get("hip") or vals.get("rocm"):
        flavor = "amd"
    elif vals.get("xpu"):
        flavor = "intel"
    else:
        flavor = "cpu"
    return {"installed": True, "flavor": flavor, "torch": vals.get("__version__")}


def _names(gpus: list[dict], vendor: str | None) -> str:
    return "、".join(g["name"] for g in gpus if g["vendor"] == vendor)


def _replace_tip(vendor: str) -> str:
    return (f"请下载 {PACKAGE[vendor]}，解压后替换程序根目录的 ComfyUI_windows_portable 文件夹"
            "（替换前先把 ComfyUI\\models 里的模型移出来，换好后放回；再按 README.md 安装第 4 步重新安装节点与依赖）")


def gpu_status() -> dict:
    """显卡与 ComfyUI 版本是否对应。

    level：ok / warn（能启动，但有需要注意的地方）/ block（不启动 ComfyUI）；
    reason（block 时）：missing = 没装 ComfyUI；mismatch = 显卡与版本明确对不上（引擎页可「仍然启动」）。
    """
    gpus = [g for g in adapters() if g["vendor"] != "other"]
    vendor = machine_vendor(gpus)
    comfy = comfy_flavor()
    flavor = comfy["flavor"]
    # 显示 ComfyUI 实际会用的显卡：已装版本对应的显卡在就显示它（AMD 核显 + NVIDIA 独显 + AMD 版时是核显）
    used = flavor if flavor in ("nvidia", "amd") and any(g["vendor"] == flavor for g in gpus) else vendor
    out = {"gpus": gpus, "vendor": vendor, "gpu_vendor": used, "gpu_names": _names(gpus, used), "comfy": comfy,
           "level": "ok", "reason": None, "message": None, "oom_hint": None}

    def res(level, message, reason=None):
        out.update(level=level, message=message, reason=reason)
        return out

    if flavor == "amd" and "--enable-dynamic-vram" not in get_args().split():
        out["oom_hint"] = "AMD 显卡可以在 菜单 → 引擎 的「启动参数」里加 --enable-dynamic-vram，重启 ComfyUI 后再试"
    if not comfy["installed"]:
        tip = (f"本机是 {LABEL[vendor]} 显卡（{out['gpu_names']}），请下载 {PACKAGE[vendor]}" if vendor
               else "请按显卡下载 NVIDIA 版或 AMD 版")
        return res("block", f"没有找到 ComfyUI 便携版（{config.COMFY_DIR}）。{tip}，"
                            "解压后把 ComfyUI_windows_portable 文件夹放到程序根目录，见 README.md 安装第 2 步", "missing")
    if flavor in ("nvidia", "amd"):
        other = "amd" if flavor == "nvidia" else "nvidia"
        has_own = any(g["vendor"] == flavor for g in gpus)
        has_other = any(g["vendor"] == other for g in gpus)
        if has_other and not has_own:
            return res("block", f"本机是 {LABEL[other]} 显卡（{_names(gpus, other)}），但 ComfyUI 是 {LABEL[flavor]} 版，无法启动。"
                                f"{_replace_tip(other)}", "mismatch")
        if has_other and flavor == "amd":
            return res("warn", f"本机有 NVIDIA 显卡（{_names(gpus, 'nvidia')}），当前 AMD 版 ComfyUI 只能用 AMD 显卡"
                               f"（{_names(gpus, 'amd')}）。建议换成 NVIDIA 版：{_replace_tip('nvidia')}")
        if gpus and not has_own:
            return res("warn", f"没有检测到 {LABEL[flavor]} 显卡（检测到：{'、'.join(g['name'] for g in gpus)}），ComfyUI 可能无法启动")
        return out
    if flavor == "cpu":
        tip = f"本机是 {LABEL[vendor]} 显卡，建议换成显卡版：{_replace_tip(vendor)}" if vendor else "建议换成显卡版"
        return res("warn", f"ComfyUI 是 CPU 版，出图会非常慢。{tip}")
    if flavor == "intel":
        return res("warn", "ComfyUI 是 Intel 显卡版，本程序没有在 Intel 显卡上测试过")
    return out


def check_start() -> str | None:
    """启动 ComfyUI 前的检查：不能启动时返回原因。"""
    s = gpu_status()
    return s["message"] if s["level"] == "block" else None


def describe(s: dict | None = None) -> str:
    """一行说明，写在控制台日志里。"""
    s = s or gpu_status()
    gpu = f"{LABEL[s['gpu_vendor']]}（{s['gpu_names']}）" if s["gpu_vendor"] else "未检测到 NVIDIA / AMD 显卡"
    c = s["comfy"]
    if not c["installed"]:
        comfy = "未安装"
    elif c["flavor"]:
        comfy = f"{LABEL[c['flavor']]} 版" + (f"（PyTorch {c['torch']}）" if c["torch"] else "")
    else:
        comfy = "版本未知"
    return f"显卡：{gpu}；ComfyUI：{comfy}"


# ───────────────────────────── 额外启动参数 ─────────────────────────────

_RESERVED = ("--listen", "--port")     # 前端靠固定地址与端口连接 ComfyUI


def parse_args(text: str) -> list[str]:
    """把输入框里的参数拆成列表；不合法时抛 ValueError。"""
    text = (text or "").strip()
    if not text:
        return []
    try:
        parts = shlex.split(text, posix=False)
    except ValueError as e:
        raise ValueError("引号不成对") from e
    out = []
    for p in parts:
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
            p = p[1:-1]
        if any(p == r or p.startswith(r + "=") for r in _RESERVED):
            raise ValueError(f"{p.split('=')[0]} 由本程序固定设置，不能修改")
        out.append(p)
    return out


def get_args() -> str:
    with store._lock:
        d = store._load()
    return (d.get("engine") or {}).get("comfy_args", "")


def set_args(text: str) -> str:
    text = " ".join((text or "").split())
    parse_args(text)
    with store._lock:
        d = store._load()
        d.setdefault("engine", {})["comfy_args"] = text
        store._save(d)
    return text


def current_args() -> list[str]:
    """启动 ComfyUI 时追加的参数；设置文件里的值不合法时忽略（并记日志）。"""
    try:
        return parse_args(get_args())
    except ValueError as e:
        log.warning("ComfyUI 启动参数无效，已忽略：%s", e)
        return []
