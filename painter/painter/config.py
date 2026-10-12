"""运行配置。均可用环境变量覆盖（前缀 PAINTER_）。"""
import json
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parents[1]
FRONTEND_SETTINGS = ROOT_DIR / "frontend" / "data" / "settings.json"


def _default_output_dir() -> Path:
    """图片保存目录：PAINTER_OUTPUT_DIR 未设置时，取前端「菜单 → 存储」的设置；也没有则为根目录下的 output。
    由前端启动时会通过 PAINTER_OUTPUT_DIR 传入；这里让单独启动（start_painter.bat）也存到同一个目录。"""
    try:
        d = json.loads(FRONTEND_SETTINGS.read_text(encoding="utf-8"))
        if v := (d.get("storage") or {}).get("output_dir"):
            return Path(v)
    except (OSError, ValueError, AttributeError):
        pass
    return ROOT_DIR / "output"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAINTER_")

    host: str = "127.0.0.1"
    port: int = 8190
    comfy_url: str = "http://127.0.0.1:8188"
    output_dir: Path = Field(default_factory=_default_output_dir)
    registry_path: Path = PACKAGE_DIR / "models.json"
    # 单个任务的最长执行时间（秒），6GB 显存下 1024² + 双参考可能需要数分钟
    job_timeout: int = 1800
    # 参考图上传前的最长边限制（像素），只影响上传体积，编码时会再缩放到出图尺寸
    max_input_side: int = 2048
    # 已完成任务在内存里保留的数量
    job_history: int = 200


settings = Settings()
