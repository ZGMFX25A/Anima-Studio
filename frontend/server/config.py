"""前端服务路径与端口配置。"""
from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = FRONTEND_DIR.parent

HOST = "127.0.0.1"
PORT = 8200

COMFY_DIR = ROOT_DIR / "ComfyUI_windows_portable"
# ComfyUI 便携版自带的 Python，只用来运行 ComfyUI；前端与绘画模块运行在根目录的 .venv
COMFY_PYTHON = COMFY_DIR / "python_embeded" / "python.exe"
PAINTER_DIR = ROOT_DIR / "painter"

COMFY_PORT = 8188
PAINTER_PORT = 8190
COMFY_URL = f"http://127.0.0.1:{COMFY_PORT}"
PAINTER_URL = f"http://127.0.0.1:{PAINTER_PORT}"
# 绘画引擎未就绪时，从这里读取模型列表作为兜底（只读）
PAINTER_MODELS_JSON = PAINTER_DIR / "painter" / "models.json"

WEB_DIR = FRONTEND_DIR / "web"
PROMPTS_DIR = FRONTEND_DIR / "prompts"
CONFIG_DIR = FRONTEND_DIR / "config"
DATA_DIR = FRONTEND_DIR / "data"
REFS_DIR = DATA_DIR / "refs"
THUMBS_DIR = DATA_DIR / "thumbs"
LOGS_DIR = FRONTEND_DIR / "logs"
DB_PATH = DATA_DIR / "gallery.db"
SETTINGS_PATH = DATA_DIR / "settings.json"
LLM_LOG_PATH = LOGS_DIR / "llm_last_request.json"

for _d in (DATA_DIR, REFS_DIR, THUMBS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
