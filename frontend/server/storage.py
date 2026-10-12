"""图片保存目录设置（data/settings.json 的 storage 段）。绘画模块通过环境变量 PAINTER_OUTPUT_DIR 使用它。"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from . import config
from .llm import store

# 绘画模块的默认输出目录（painter/painter/config.py 中的 output_dir）：程序根目录下的 output
DEFAULT_OUTPUT = str(config.ROOT_DIR / "output")


def get() -> dict:
    with store._lock:
        d = store._load()
    return {"output_dir": (d.get("storage") or {}).get("output_dir", "")}


def effective() -> str:
    return get()["output_dir"] or DEFAULT_OUTPUT


def validate(path: str) -> str:
    """检查目录：必须是绝对路径；不存在就创建；能写入。返回规范化后的路径。"""
    p = Path(path.strip().strip('"'))
    if not p.is_absolute():
        raise ValueError(r"请填写完整路径，例如 D:\AI图片")
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / f".write_test_{uuid.uuid4().hex[:6]}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as e:
        raise ValueError(f"无法写入该目录：{e}") from e
    return str(p)


def set_output_dir(path: str) -> dict:
    value = validate(path) if path and path.strip() else ""
    if value and Path(value) == Path(DEFAULT_OUTPUT):
        value = ""            # 与默认相同则按默认处理
    with store._lock:
        d = store._load()
        d.setdefault("storage", {})["output_dir"] = value
        store._save(d)
    return get()


def browse(initial: str) -> str | None:
    """弹出 Windows 的选择文件夹对话框（在运行本程序的电脑上），返回所选路径；取消返回 None。"""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$f.Description = '选择图片保存目录'; $f.ShowNewFolderButton = $true;"
        f"$f.SelectedPath = '{initial.replace(chr(39), chr(39) * 2)}';"
        "$w = New-Object System.Windows.Forms.Form; $w.TopMost = $true;"
        "if ($f.ShowDialog($w) -eq 'OK') { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $f.SelectedPath }"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, timeout=600)
    out = r.stdout.decode("utf-8", "replace").strip()
    return out or None
