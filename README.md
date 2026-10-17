# Anima Studio

本地运行的 Anima 动漫绘图工具：网页前端 + 绘画模块，底层由 ComfyUI 执行，所有出图都在本机完成。

- **绘图**：Anima 系列模型，人物参考（1~3 张参考图）、动作参考（照片自动检测骨架，或直接给骨架图）
- **AI 填写**：用一段描述让 LLM 生成或修改提示词字段，可联网检索 Danbooru 角色与画师；
  上传的灵感图、人物 / 动作参考图先由「识图模型」逐张详细描述，再交给 LLM 生成（生成用的模型不必能看图）
- **人设**：保存常用角色（已有角色从 Danbooru 导入，原创角色由 AI 整理），一键填入绘图页；可导出为人设卡（一张 PNG 封面图，图片里带着完整资料）分享，拖进人设页即可导入，也能读取 SillyTavern 角色卡
- **历史 / 收藏**：每张图的完整参数，可一键复用

```
start.bat ──► 前端服务 frontend   127.0.0.1:8200（网页 + 进程管理）
                ├─ 绘画模块 painter 127.0.0.1:8190
                └─ ComfyUI 引擎    127.0.0.1:8188
```

## 系统要求

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 |
| 显卡 | NVIDIA：6GB 显存可用（实测 RTX 3060 Laptop 6GB：832×1216 约 50 秒/张）<br>AMD：RX 7000 / 9000 系列（**未实测**，建议 8GB 以上显存），见下文 [AMD 显卡](#amd-显卡) |
| Python | 3.10 及以上（用来建 `.venv`；实测 3.11） |
| 磁盘 | 最少约 6GB（一个基础模型 + 文本编码器 + VAE），全部模型约 26GB |

## 安装

以下命令都在程序根目录（本 README 所在目录）下执行。安装完成后的目录结构：

```
Anima-Studio\
├─ ComfyUI_windows_portable\   第 2 步放入（不在 git 中）
├─ .venv\                      第 5 步创建（不在 git 中）
├─ frontend\                   网页前端
├─ painter\                    绘画模块
├─ output\                     默认图片保存目录（首次出图时自动创建）
├─ requirements.txt
└─ start.bat
```

### 1. 获取代码

```bash
git clone https://github.com/ZGMFX25A/Anima-Studio.git Anima-Studio
```

### 2. ComfyUI 便携版

从 [ComfyUI Releases](https://github.com/Comfy-Org/ComfyUI/releases) 按显卡下载 Windows 便携版：

| 显卡 | 下载 |
|---|---|
| NVIDIA | `ComfyUI_windows_portable_nvidia.7z` |
| AMD | `ComfyUI_windows_portable_amd.7z` |

两种包解压后都是 `ComfyUI_windows_portable` 文件夹，整个放到程序根目录即可。需要支持 Anima 的版本（实测 NVIDIA 版 0.33.2）。

启动时程序会自动识别本机显卡和装的是哪一版 ComfyUI。装错了（例如 AMD 显卡装了 NVIDIA 版）不会启动 ComfyUI，并在控制台和 菜单 → 引擎 中说明该换哪个包。
笔记本同时有核显和 NVIDIA 独显时，按 NVIDIA 处理。

### 3. 下载模型

下表的目标目录都在 `ComfyUI_windows_portable\ComfyUI\models\` 下。文件名必须与表中一致（对应 [painter/painter/models.json](painter/painter/models.json)）。

| 用途 | 文件 | 放到 | 来源 |
|---|---|---|---|
| 基础模型（默认，**必需**） | `anima-base-v1.0.safetensors` | `diffusion_models\` | [circlestone-labs/Anima](https://huggingface.co/circlestone-labs/Anima) 的 `split_files/diffusion_models/` |
| 文本编码器（**必需**） | `qwen_3_06b_base.safetensors` | `text_encoders\` | 同上，`split_files/text_encoders/` |
| VAE（**必需**） | `qwen_image_vae.safetensors` | `vae\` | 同上，`split_files/vae/` |
| 人物参考 | `anima-incontext-character.safetensors` | `loras\` | [darask0/Anima-InContext-Character](https://huggingface.co/darask0/Anima-InContext-Character) |
| 动作参考 | `anima_pose_preview2.safetensors` | `loras\` | [Claquasse/Anima-Control-Pose](https://huggingface.co/Claquasse/Anima-Control-Pose) |
| 可选模型 | `anima-aesthetic-v1.1.safetensors`、`anima-turbo-v1.1.safetensors` | `diffusion_models\` | [circlestone-labs/Anima](https://huggingface.co/circlestone-labs/Anima) |
| 可选模型 | `Anima-2.9B-preview-v1.safetensors` | `diffusion_models\` | [Gazingstars123/Anima-2.9B](https://huggingface.co/Gazingstars123/Anima-2.9B) |
| 可选模型（非官方） | `anima29b-sm29t-fuse1.1.safetensors` | `diffusion_models\` | [Langzaigg/anima29b-turbo1.1-unofficial](https://huggingface.co/Langzaigg/anima29b-turbo1.1-unofficial) |

- 没下载的模型在页面里显示为「（未安装）」，不能选择，不影响其它模型。各模型的特点与参考功能兼容性见 [painter/README.md](painter/README.md)。
- 只用 bf16 原版权重，不支持量化模型。
- 模型权重各自遵循其发布页上的许可。

### 4. ComfyUI 节点与依赖

```powershell
# ① 人物参考节点：把 darask0/Anima-InContext-Character 仓库中的 comfyui-anima-incontext 文件夹
#    整个复制到 ComfyUI_windows_portable\ComfyUI\custom_nodes\

# ② 本程序的专用节点（任意比例骨架、动作控制注入）
powershell -ExecutionPolicy Bypass -File painter\install_nodes.ps1

# ③ 动作检测依赖，装进 ComfyUI 自带的 Python
ComfyUI_windows_portable\python_embeded\python.exe -m pip install rtmlib onnxruntime opencv-python
```

首次使用动作参考（照片模式）时，rtmlib 会自动下载 DWPose 检测模型，需要联网。
Claquasse/Anima-Control-Pose 仓库里附带的原版节点不需要安装，本程序用自己的节点代替。

### 5. Python 环境（.venv）

前端和绘画模块运行在根目录的 `.venv` 里；ComfyUI 仍用它自带的 Python，两者互不影响。

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

没有 `py` 命令时，用 `python -m venv .venv` 代替。

### 下载加速（国内网络）

- PyPI：`pip install` 后面加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`
- Hugging Face：网页把 `huggingface.co` 换成 `hf-mirror.com` 下载；用 `huggingface-cli` / `hf_hub_download` 时先设置环境变量 `HF_ENDPOINT=https://hf-mirror.com`。
  如果走代理下载很慢，可以再设置 `HF_HUB_DISABLE_XET=1`。

## AMD 显卡

> 本程序只在 NVIDIA 显卡上实测过。以下按 ComfyUI 对 AMD 显卡的支持情况整理，**未实测**。

| 显卡 | 情况 |
|---|---|
| RX 9070 / 9060 系列、RX 7900 / 7800 / 7700 系列、Ryzen AI Max / 300 系列 | ComfyUI 官方 AMD 便携版直接支持 |
| RX 7600 | 能运行，但 ComfyUI 不会自动启用 PyTorch 注意力，速度较慢 |
| RX 6000 系列及更老 | 不保证能用：ComfyUI 认为这些显卡不支持 bf16，会改用 fp16 计算 |

- 显卡驱动更新到最新版（AMD Software: Adrenalin Edition）。
- 安装步骤与 NVIDIA 相同，只是第 2 步下载 AMD 版；第 4 步的节点和依赖照装（动作检测在 CPU 上运行，与显卡无关）。
- 显存建议 8GB 以上。NVIDIA 版 ComfyUI 默认开启「动态显存」，AMD 版目前默认不开，显存利用不如 NVIDIA 版。
  出现显存不足时，在 菜单 → 引擎 的「ComfyUI 启动参数」里填 `--enable-dynamic-vram`，保存后重启 ComfyUI。

### 换显卡或装错了版本

模型、节点和动作检测依赖都装在 `ComfyUI_windows_portable` 里，换成另一版时：

1. 把 `ComfyUI_windows_portable\ComfyUI\models\` 里下载的模型移到别处；
2. 删除（或改名）旧的 `ComfyUI_windows_portable`，解压新下载的包放到程序根目录；
3. 把模型放回 `ComfyUI_windows_portable\ComfyUI\models\` 的对应子目录；
4. 重新执行安装第 4 步（节点与依赖）。

## 启动

双击 **`start.bat`**：依次启动 ComfyUI、绘画模块和前端，全部就绪后自动打开 <http://127.0.0.1:8200>。
在控制台窗口按 **Enter** 会关闭全部服务；直接关闭窗口，所有服务也会一并结束。

单独使用绘画模块：先运行 `painter\start_comfyui.bat`，再运行 `painter\start_painter.bat`。接口文档在 <http://127.0.0.1:8190/docs>，详见 [painter/README.md](painter/README.md)。

### 端口被占用

WSL / Hyper-V / Docker 有时会临时占用 8188、8190、8200 端口，导致服务启动失败（菜单 → 引擎 中提示「端口 … 无法绑定」）。这时：

1. 右键 `reset_ports.bat` →「以管理员身份运行」；
2. 按提示先关闭 WSL（`wsl --shutdown`）和 Docker Desktop；
3. 按 Enter：脚本重启 WinNAT，释放被临时占用的端口，然后自动关闭窗口；
4. 重新运行 `start.bat`。

脚本只重置，不会永久保留端口。

## 首次使用

在页面顶部的 **菜单** 页中设置：

| 菜单项 | 说明 |
|---|---|
| **LLM** | 「AI 填写」和「人设」需要一个 LLM 服务：添加服务商（OpenAI 兼容格式或 Anthropic 格式）、填 Base URL 与 API Key、拉取模型，再点「测试」检查连通性和识图能力。测出能识图的模型会出现在 AI 填写卡片的「识图模型」下拉里，可以和生成用的模型分开选（例如用能看图的模型识图、用不能看图的模型生成）。不配置也能手动填写提示词出图。 |
| **联网** | Danbooru 角色 / 画师检索的联网方式（直连 / 系统代理 / 自定义代理）。「标签参考」需要 [DanbooruSearch](https://sakizuki-danboorusearch.hf.space) 的 Key（可选）。 |
| **存储** | 图片保存目录，默认为根目录下的 `output\`（按日期分子目录）。改成其它目录后，单独用 `start_painter.bat` 启动绘画模块时也会用这个目录。 |
| **引擎** | 查看与启动 / 停止 / 重启 ComfyUI 和绘画模块；显示识别到的显卡与 ComfyUI 版本；「ComfyUI 启动参数」可追加命令行参数（一般留空）。 |

## 数据与隐私

以下内容只保存在本机，已在 `.gitignore` 中排除，不会进入 git：

| 位置 | 内容 |
|---|---|
| `frontend\data\` | 设置（**含 API Key**）、历史 / 收藏 / 人设 / 角色库（含画师库、服装库）数据库、缩略图、参考图 |
| `frontend\logs\` | 运行日志；`llm_last_request.json` 记录最近一次发给 LLM 的完整内容 |
| `output\` | 生成的图片 |

换电脑或重装时，备份 `frontend\data\` 即可保留设置和全部记录（图片在保存目录里，需要另外备份）。

## 更多文档

| 文档 | 内容 |
|---|---|
| [frontend/DESIGN.md](frontend/DESIGN.md) | 前端设计：各页面、LLM 辅助填写、联网检索、存储、人设、识图、人设卡 |
| [frontend/prompts/README.md](frontend/prompts/README.md) | 发给 LLM 的全部提示词文件说明，修改后即时生效 |
| [painter/README.md](painter/README.md) | 绘画模块：HTTP 接口、模型兼容性实测、已知限制 |
| [painter/PROMPT_GUIDE.md](painter/PROMPT_GUIDE.md) | 开启人物 / 动作参考时提示词的注意事项 |
