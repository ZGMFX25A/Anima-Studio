@echo off
rem 绘画引擎（ComfyUI，仅本机访问）
cd /d "%~dp0..\ComfyUI_windows_portable"
.\python_embeded\python.exe -s ComfyUI\main.py --listen 127.0.0.1 --port 8188 --disable-auto-launch
