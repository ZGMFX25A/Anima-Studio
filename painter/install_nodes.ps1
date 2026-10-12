# 把 painter 专用节点同步到 ComfyUI custom_nodes（修改节点源码后重新运行，然后重启 ComfyUI）
$src = Join-Path $PSScriptRoot "comfy_nodes\anima_painter_nodes"
$dst = Join-Path $PSScriptRoot "..\ComfyUI_windows_portable\ComfyUI\custom_nodes\anima_painter_nodes"
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Copy-Item $src $dst -Recurse
Write-Host "installed -> $dst"
