# Anima Painter —— 绘画模块

前端把用户输入整理成 `GenerateRequest`（JSON）发给本模块；本模块校验、构建 ComfyUI 工作流、执行，
并把结果图返回给前端。所有生成都在本机完成，只用 bf16 原版权重（不使用量化模型）。

```
前端 ──HTTP JSON──► 绘画模块 (127.0.0.1:8190) ──API──► ComfyUI 引擎 (127.0.0.1:8188)
                     │ 校验 / 预处理参考图 / 构建工作流 / 任务队列 / 保存结果
                     └─► 输出目录 <程序根目录>\output\<日期>\
```

## 启动

1. `start_comfyui.bat` —— 绘画引擎（首次使用前运行一次 `install_nodes.ps1` 安装专用节点）
2. `start_painter.bat` —— 绘画模块服务；交互式接口文档：<http://127.0.0.1:8190/docs>

绘画模块运行在根目录的 `.venv`（安装见根目录 [README.md](../README.md)）。环境变量（前缀 `PAINTER_`）可覆盖 `painter/config.py` 中的配置：端口、ComfyUI 地址、输出目录、超时等。
输出目录：`PAINTER_OUTPUT_DIR` 未设置时，使用前端「菜单 → 存储」的设置（`frontend/data/settings.json`），也没有则为根目录下的 `output`。

## 功能

| 功能 | 实现 | 说明 |
|---|---|---|
| 基础绘画 | Anima DiT + Qwen3-0.6B 文本编码器 + Qwen-Image VAE | 模型见 `painter/models.json` |
| 人物参考 | [Anima-InContext-Character](https://huggingface.co/darask0/Anima-InContext-Character) LoRA + 参考帧注入 | 1~3 张参考图 |
| 动作参考 | [Anima-Control-Pose preview2](https://huggingface.co/Claquasse/Anima-Control-Pose) Control-LoRA + 自研骨架节点 | 照片自动检测或直接给骨架图 |
| 精细重绘 | **预留接口** `RefineSpec` / `PostStage` | 未实现时请求返回 `STAGE_UNAVAILABLE` |
| 超分 | **预留接口** `UpscaleSpec` / `PostStage` | 同上 |

## 模型兼容性（2026-10-10 实测，RTX 3060 Laptop 6GB）

动作跟随用 body-PCK@0.1 衡量（对输出重新检测骨架与目标比对，≥0.6 视为跟随）。832×1216，默认采样参数。

| 模型 | 人物参考 | 动作参考（高踢腿） | 动作+人物（中等动作） | 单张耗时 基础 / +人物 |
|---|---|---|---|---|
| anima-base | ✅ 还原好 | ✅ PCK 0.82，4/4 | ✅ PCK 0.75，4/4 | ~50s / ~200s |
| anima-aesthetic | ✅ 还原好 | ⚠️ PCK 0.49，2/4 | ✅ PCK 0.82，2/2 | ~50s / ~200s |
| anima-turbo | ⚠️ 发色/画风会偏 | ⚠️ PCK 0.44，1/4 | ✅ PCK 0.79，2/2 | ~10s / ~36s |
| anima-2.9b-preview | ❌ 禁用 | ❌ 禁用 | ❌ | ~100s / — |
| anima-2.9b-turbo-fuse（非官方） | ❌ 禁用 | ❌ 禁用 | ❌ | ~25s（10 步，848×1264）/ — |

- anima-2.9b-turbo-fuse：[Langzaigg/anima29b-turbo1.1-unofficial](https://huggingface.co/Langzaigg/anima29b-turbo1.1-unofficial) 的融合版完整模型（BF16，40 层，基于 2.9B），
  默认 10 步、cfg 1.0、euler_ancestral + normal；cfg 1.0 时负向提示词无效。因基于 2.9B，两种参考均不可用。

- 2.9B 为 40 层架构，两个适配器都只覆盖 28 层：人物参考会原样复制参考图（LoRA 开关均如此），动作跟随低于无控制基线。
- **动作参考要点**：骨架必须占满画面才会被可靠跟随，所以默认 `fit="person"`。
- **开启参考功能时提示词的注意事项**（人物参考 / 动作参考 / 两者同开）见 [PROMPT_GUIDE.md](PROMPT_GUIDE.md)。
- ComfyUI 动态显存偶发 `Fault failed` 底层错误（非显存不足）时，绘画模块会释放显存并自动重跑一次。
- **已知限制**：极端动作 + 人物参考时，人物参考图本身的姿势会压过动作参考（base 高踢腿+人物 PCK 0.22）。人物参考图尽量用中性站姿；
  或改用 aesthetic/base 单独动作参考后再做精细重绘。
- `/v1/capabilities` 中每个模型的 `notes` 字段给出上述说明，前端可直接作为提示展示。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/health` | 服务与引擎状态、队列长度 |
| GET | `/v1/capabilities` | 模型列表（含默认参数、各模型支持的参考）、采样器/调度器、可用 LoRA、后处理阶段可用性、参数范围 |
| POST | `/v1/jobs` | 提交任务，立即返回 `202 Job`（status=queued） |
| GET | `/v1/jobs/{job_id}` | 查询任务（状态、进度、结果） |
| GET | `/v1/jobs?limit=50` | 最近任务 |
| POST | `/v1/jobs/{job_id}/cancel` | 取消排队中或执行中的任务 |
| GET | `/v1/jobs/{job_id}/files/{name}` | 下载结果图（`ImageResult.url`） |
| POST | `/v1/generate` | 同步：提交并等待完成，返回 `Job`（调试用） |
| POST | `/v1/engine/free?unload_models=true` | 释放显存（无任务执行时） |

推荐前端流程：`POST /v1/jobs` → 每 1 秒 `GET /v1/jobs/{id}` 读 `progress` → `succeeded` 后按 `images[].url` 取图。

### 请求 `GenerateRequest`

字段为 `null`/省略 表示使用默认值。完整定义见 [`painter/schemas.py`](painter/schemas.py)。

```jsonc
{
  "schema_version": "1.0",
  "model": "anima-base",                 // /v1/capabilities 中的 id；省略=默认模型
  "prompt": {
    "positive": "masterpiece, best quality, 1girl, ...",   // 原样送入编码器，质量词/画师 tag 由前端拼接
    "negative": "worst quality, low quality, ..."
  },
  "width": 832, "height": 1216,          // 256~2048，16 的倍数；推荐总像素≈1024²
  "seed": 12345,                         // 省略=随机（≤2^53）
  "batch_size": 1,                       // 1~4
  "sampler": { "steps": null, "cfg": null, "sampler_name": null, "scheduler": null },  // null=模型默认
  "loras": [ { "name": "xxx.safetensors", "strength": 0.8 } ],

  "character_ref": {                     // 人物参考（可省略）
    "enabled": true,
    "images": [ { "image": "<base64>", "mask": null } ],   // 1~3 张；带 alpha 自动抠图到白底；mask 白=主体
    "strength": 1.0,                     // 0~3，1.0 中性；不像时 1.2~1.5
    "start_percent": 0.0, "end_percent": 1.0
  },

  "pose_ref": {                          // 动作参考（可省略）
    "enabled": true,
    "image": "<base64>",
    "input_type": "photo",               // photo=自动检测骨架 / skeleton=现成 DWPose 骨架图（黑底）
    "strength": 1.0,                     // 0~2
    "fit": "person",                     // person（推荐）/ contain / cover
    "margin": 0.05,
    "persons": "largest",                // largest / all
    "hands": true, "face": true, "feet": true
  },

  "refine":  { "enabled": false, ... },  // 预留：精细重绘，字段见下
  "upscale": { "enabled": false, ... },  // 预留：超分，字段见下
  "post_order": ["refine", "upscale"],

  "output": {
    "format": "png",                     // png / webp / jpeg
    "quality": 95,
    "return_base64": false,              // true 时结果直接附带 base64
    "include_intermediates": false,      // true 时附带骨架图、各阶段前的图
    "embed_metadata": true               // PNG 写入 anima_painter 文本块（参数 JSON）
  },
  "client_meta": {}                      // 前端透传，原样返回
}
```

### 响应 `Job`

```jsonc
{
  "schema_version": "1.0",
  "job_id": "20261010-155606-307ff9",
  "status": "succeeded",                 // queued / running / succeeded / failed / cancelled
  "progress": { "stage": "base", "step": 12, "total_steps": 30, "percent": 38.0, "queue_position": null },
  "created_at": 1791622566.1, "started_at": ..., "finished_at": ...,
  "params": {                            // 实际使用的参数（默认值已填充），可直接用于复现
    "model": "anima-base", "model_file": "anima-base-v1.0.safetensors",
    "width": 832, "height": 1216, "seed": 12345, "batch_size": 1,
    "steps": 30, "cfg": 4.0, "sampler_name": "er_sde", "scheduler": "simple",
    "character_ref": false, "pose_ref": false, "stages": []
  },
  "images": [ { "index": 0, "kind": "final", "url": "/v1/jobs/<id>/files/<name>", "path": "<保存目录>\\<日期>\\<文件名>.png",
                "width": 832, "height": 1216, "seed": 12345, "base64": null } ],
  "intermediates": [ /* kind = pose_skeleton / base / refine / upscale */ ],
  "error": null,                         // { code, message, detail }
  "client_meta": {}
}
```

### 错误

HTTP 错误统一为 `{"error": {"code", "message", "detail"}}`；任务执行期的错误写在 `Job.error`。

| code | HTTP | 含义 |
|---|---|---|
| `INVALID_REQUEST` | 422 | 参数校验失败（`detail` 列出字段） |
| `UNKNOWN_MODEL` | 422 | 模型 id 不存在或文件未安装 |
| `FEATURE_UNSUPPORTED` | 422 | 该模型不支持所选参考（见 capabilities 的 `features`） |
| `BAD_IMAGE` | 422 | 图片无法解码 |
| `STAGE_UNAVAILABLE` | 501 | 精细重绘/超分实现未安装 |
| `ENGINE_UNAVAILABLE` | 503 | ComfyUI 未启动 |
| `NO_PERSON` | Job | 动作参考图中没检测到人物 |
| `OUT_OF_MEMORY` | Job | 显存不足 |
| `ENGINE_ERROR` / `TIMEOUT` / `CANCELLED` | Job | 执行失败 / 超时 / 已取消 |

## 预留接口：精细重绘与超分

### 请求字段（已冻结，实现不得改动，扩展参数放 `params`）

```jsonc
"refine": {
  "enabled": true,
  "model": null,                 // 重绘模型 id；null=沿用基础模型
  "mode": "full",                // full / mask / auto_face / auto_hand
  "mask": null,                  // mode=mask 时：base64，白=重绘区域，尺寸=基础图
  "denoise": 0.35,
  "prompt": null,                // null=沿用基础提示词
  "sampler": { ... },            // 同 SamplerParams
  "keep_references": true,       // 重绘时继续施加人物/动作参考
  "params": {}
},
"upscale": {
  "enabled": true,
  "model": "xxx.pth",            // models/upscale_models 下的文件
  "scale": 2.0,
  "tile_size": null,
  "denoise": 0.0,                // >0 时放大后再低噪重绘
  "params": {}
}
```

### 实现方式（[`painter/stages.py`](painter/stages.py)）

后处理阶段与基础绘画在**同一个** ComfyUI 工作流中串联：`IMAGE → stage.build() → IMAGE`。

```python
class PostStage(ABC):
    name: ClassVar[str]                     # "refine" / "upscale"，与请求字段同名
    spec_type: ClassVar[type[BaseModel]]    # RefineSpec / UpscaleSpec
    def availability(self, registry) -> tuple[bool, str | None]   # 是否可用 + 原因（显示在 capabilities）
    def models(self, registry) -> list[str]                        # 可选模型列表
    def validate(self, spec, req, registry) -> None                # 请求级校验，失败抛 ValueError
    def build(self, ctx: BuildContext, image: Out, spec) -> StageResult   # 加节点，返回新的 IMAGE 与尺寸
```

`BuildContext` 提供：`graph`、`req`、`registry`、`model_entry`、`model`（含参考补丁）、`plain_model`（不含参考）、
`clip`、`vae`、`positive`、`negative`、`seed`、`steps`、`cfg`、`sampler_name`、`scheduler`、`width`、`height`。
阶段内新增的 KSampler 类节点会自动计入进度（`progress.stage = "refine"/"upscale"`）。

接入新实现：写一个 `PostStage` 子类，替换 `STAGES` 中对应的占位实现即可，前端与其它代码无需改动。

## 文件

| 路径 | 作用 |
|---|---|
| `painter/schemas.py` | **接口契约**（请求/响应/错误码） |
| `painter/models.json` | 模型注册表：文件、默认采样参数、各模型支持的参考 |
| `painter/pipeline.py` | 校验 + 工作流构建 |
| `painter/stages.py` | 后处理阶段插件接口（精细重绘/超分预留） |
| `painter/jobs.py` | 串行任务队列、进度、结果保存 |
| `painter/server.py` | HTTP 服务 |
| `comfy_nodes/anima_painter_nodes/` | ComfyUI 专用节点（任意比例骨架、仅作用于生成帧的 Control 注入） |
