# 提示词文件说明

这里是发给 LLM 的全部文字，代码里不写死任何提示词。修改后**立即生效**（每次请求都会重新读取），不用重启。
每次请求实际发出的完整内容记录在 `frontend/logs/llm_last_request.json`，可对照调整。

```
prompts/
├─ fill/                 「AI 填写」：根据描述生成 / 修改绘画提示词字段
│  ├─ system/            系统提示词片段（按情况挑选后拼接成一条系统提示词）
│  ├─ user/              用户消息模板与图片标注
│  └─ schema.json        LLM 输出的 JSON 结构
├─ vision/               「识图」：AI 填写前先让识图模型逐张详细描述上传的图片（每张图单独一次 LLM 调用）
├─ search/               「联网检索」第一步：识别描述中的作品角色与画师（单独一次 LLM 调用）
├─ persona/              「人设」页：从 Danbooru 导入时的 AI 拆分、AI 整理
├─ import/               「从图片读取」：把其它工具生成的图片里的提示词原样拆到各字段
└─ test/                 其它：菜单 → LLM 的「测试」按钮
```

## fill/system/ —— 绘画用的系统提示词

按下表顺序拼接（用空行分隔），「何时使用」不满足的片段跳过：

| 顺序 | 文件 | 何时使用 | 内容 |
|---|---|---|---|
| 1 | `system.md` | 总是 | 角色与工作场景、不可逾越的界限、只输出 JSON、标签格式 |
| 2 | `fields.md` | 总是 | 10 个字段的定义（含画幅可选值 `{{aspect_ratios}}`）；nltags 用来把外貌和动作绑定到具体人物（多人时每人一句：指代 + 核心外貌 + 正在做什么，互动写清谁对谁，最后一句场景） |
| 3 | `mode_generate.md` | 点「生成」 | 从零生成全部字段 |
| 3 | `mode_modify.md` | 点「修改」 | 在当前字段 `{{current_fields}}` 基础上修改 |
| 4 | `safety_levels.md` | 总是 | 内容分级标准：四个等级的含义、标签示例、写进哪个字段、neg 反向约束 |
| 5 | `safety_fixed.md` | 指定了安全等级 | 必须使用 `{{safety}}` 并排除其它三个，按该等级如实写 |
| 5 | `safety_auto.md` | 安全等级「不指定」 | 由 LLM 判断，恰好一个 |
| 6 | `user_images.md` | 灵感图识图成功 | 按识图结果理解灵感图、只取描述指明的方面（`{{n_images}}` 张） |
| 7 | `ref_character.md` | 开启人物参考，识图成功 | 按识图结果写核心特征 2~5 个等 |
| 7 | `ref_character_noimage.md` | 开启人物参考，识图模型看不到图或识图失败 | 只用描述中的特征，没写则 appearance 留空 |
| 8 | `ref_pose.md` | 开启动作参考，识图成功 | 动作词与识图结果的姿势一致、必须写朝向（`{{pose_input_type}}`） |
| 8 | `ref_pose_noimage.md` | 开启动作参考，识图模型看不到图或识图失败 | 描述写了动作才填，否则 action 留空 |
| 9 | `ref_both.md` | 两种参考同时开启 | 两部分要求同时满足 |
| 10 | `persona.md` | 打开「人设匹配」且命中了人设（描述中出现名称 / 别名 / 角色库里这个角色的名称，或识别出的角色名模糊比对上，或在绘图页选用了人设） | 怎么用人设资料：基础外观全部保留、选造型、专属角色标签、画师偏好、同一人设多套造型同时出现 |
| 10 | `web_character.md` | 打开「联网检索」且查到了角色 | 怎么用角色资料：标准标签、外貌标签取舍、造型变体（已选定 / 可选列表）、可选的作品服装（只在描述明确指那一套时用，只说了一类服装或要新设计时按通用标签写）、同一角色多个造型同时出现、补充说明优先 |
| 10 | `web_outfit.md` | 打开「联网检索」且认出了作品服装（服装库，DESIGN §9.8） | 怎么用服装资料：服装标签 + 从简介 / 常见标签挑服装部件、别把常穿角色的外貌当服装、替换默认服装、谁穿 |
| 10 | `web_artist.md` | 打开「联网检索」且查到了画师 | artist 写标准画师标签（带 @），只写用户提到的画师 |
| 11 | `tag_reference.md` | 打开「标签参考」且有检索结果 | 怎么用 DanbooruSearch 的标准标签候选 |

## fill/user/ —— 用户消息

| 文件 | 内容 |
|---|---|
| `user_message.md` | 用户消息正文模板（`{{description}}` = AI 填写卡片里的描述） |
| `labels.json` | 识图结果里每张图的标题（灵感图 / 人物参考图 / 动作参考图），动作参考图类型说明，描述为空时的占位文字 |
| `vision_block.md` | 识图结果块的外框（`{{items}}` = 下面每张图拼起来） |
| `vision_item.md` | 单张图的识图结果：`{{label}}`（labels.json 里的标题）`{{text}}`（识图模型的原文） |
| `character_block.md` | 角色资料块的外框（`{{characters}}` = 下面每个角色拼起来） |
| `character_item.md` | 单个角色的排版：`{{name}}` `{{tag}}` `{{base_tag}}`（用的是造型变体时的本体标签）`{{series}}` `{{outfit}}`（识别出的服装原文）`{{wiki}}` `{{appearance}}` `{{notes}}`（变体时连同本体的补充说明）`{{variants}}`（服装没对上变体时附的可选变体：标签（名称）：特有服装标签，用「；」分隔）`{{outfit_tag}}`（服装名字对上的作品服装标签，资料在服装资料块）`{{outfit_options}}`（都没对上时附的可选作品服装：标签（别名，这个角色穿过）：简介开头／角色本身没有的常见标签，用「；」分隔）；值为空的行自动去掉 |
| `outfit_block.md` / `outfit_item.md` | 服装资料块（作品服装，来自服装库）与单套服装的排版：`{{name}}` `{{tag}}` `{{series}}` `{{wearers}}`（描述中穿它的人物）`{{other_names}}` `{{wiki}}` `{{appearance}}`（常见标签与比例）`{{notes}}`；值为空的行自动去掉 |
| `artist_block.md` / `artist_item.md` | 画师资料块与单个画师的排版：`{{name}}` `{{tag}}` `{{other_names}}` `{{post_count}}` `{{notes}}` |
| `tag_block.md` | 标签参考块（`{{tags}}` = 「标签（中文）」逗号分隔） |
| `persona_block.md` | 人设资料块的外框（`{{personas}}` = 下面每个人设拼起来） |
| `persona_item.md` | 单个人设的排版：`{{name}}` `{{kind}}` `{{how}}` `{{tag}}` `{{series}}` `{{count}}` `{{artist}}` `{{base_tags}}` `{{appearance_desc}}` `{{selected}}` `{{outfits}}`；值为空的行自动去掉 |
| `persona_outfit_item.md` | 单套造型的排版（保留行首缩进）：`{{name}}` `{{default}}` `{{tags}}` `{{desc}}` `{{character_tag}}` |
| `persona_labels.json` | 人设资料块里的固定文字：来源说明（描述中提到 / 绘图页选用）、类型名、「（默认）」、没有造型时的占位 |

用户消息的结构（只有文字，不含图片）：识图结果块（灵感图 → 人物参考图 → 动作参考图）→ 人设资料块 → 角色资料块 → 服装资料块 → 画师资料块 → 标签参考块 → `user_message.md`。

## vision/ —— 识图

AI 填写时，上传的图片（灵感图、已开启的人物参考图 / 动作参考图）先由「识图模型」逐张单独描述（并行），
描述原文整理成识图结果块（`fill/user/vision_*.md`）交给正式填写；正式填写不再收到图片，所以生成用的模型不必能看图。

| 文件 | 内容 |
|---|---|
| `system.md` | 所有识图共用的系统提示词：工作场景、如实描述、标注看不清 / 无法判断、括号附 Danbooru 标签、`【小节名】` 输出格式 |
| `inspiration.md` | 灵感参考图：整体、人物（逐人）、动作与表情、构图与镜头、场景、光线色彩、画风 |
| `character.md` | 人物参考图：只重外观——最显著的特征、头发、脸、身体、服装（含最有辨识度的部件）、配饰 |
| `pose_photo.md` | 动作参考图（照片）：只重姿势——人数与景别、朝向、躯干、手臂、腿、支撑接触、镜头角度 |
| `pose_skeleton.md` | 动作参考图（骨架图）：同上，另附 OpenPose / DWPose 配色与判断朝向的读图提示 |
| `user.md` | 图片后面的一句用户消息 |

系统提示词 = `system.md` + 对应用途的文件。识图模型在 AI 填写卡片里选：「同上」= 用 AI 填写的模型和思考档；
单独选的模型用它在「菜单 → LLM」里的默认思考档。下拉框只列出能看图的模型。

- 回复是纯文字（不要求 JSON）；为空或很短且带拒绝说法时按出错自动重试，仍失败则这张图按「看不到图」处理（用 `*_noimage.md`）。
- 同一张图 + 同一用途 + 同一模型与思考档 + 同一提示词的识图结果缓存在内存里（重启清空），反复点「生成 / 修改」不重复识图；改了 vision/ 下的文件会自动重新识图。
- 识图结果显示在 AI 填写卡片的「识图结果」里，也记在 `logs/llm_last_request.json` 的 `recognition`。

## search/ —— 识别角色与画师

| 文件 | 内容 |
|---|---|
| `extract_system.md` | 系统提示词：找出描述中已有作品的角色（原文写法 / 英文名 / 作品 / 服装；同一角色不同服装各列一项）和用户明确提到的画师（原文写法 / 英文名） |
| `extract_user.md` | 用户消息模板（`{{description}}`；「修改」时 `{{current_character}}` `{{current_artist}}` 为当前字段） |
| `extract_schema.json` | 输出结构 `{"characters": [{"name", "name_en", "series", "outfit"}], "artists": [{"name", "name_en"}]}` |

这一步使用所选模型的最低思考档（能关就关）。

## persona/ —— 人设页的 AI 拆分与整理

| 文件 | 内容 |
|---|---|
| `split_system.md` | 导入已有角色时的系统提示词：把 Danbooru 共现标签拆成 中文名 / 主体 / 基础标签 / 外观描述 / 造型 / 未归类 |
| `split_user.md` | 用户消息：`{{tag}}` `{{series}}` `{{post_count}}` `{{other_names}}` `{{wiki}}` `{{notes}}`（角色库补充说明）`{{tags}}`（共现标签与比例）`{{variants}}` `{{description}}`（AI 助手的补充描述）；值为空的行自动去掉 |
| `split_variant_item.md` | 每个勾选的造型变体：`{{tag}}` `{{post_count}}` `{{wiki}}`（变体的 Danbooru 简介）`{{notes}}`（角色库里该变体的补充说明）`{{tags}}`；值为空的行自动去掉 |
| `refine_system.md` | 「AI 整理」的系统提示词：按补充描述补全 / 修正，没提到的保持原样 |
| `refine_user.md` | 用户消息：`{{persona}}`（当前人设 JSON）`{{description}}`；前面依次是带标注的参考图、标签参考块（复用 `fill/user/tag_block.md`） |
| `schema.json` | 两者共用的输出结构：`display_name` `count` `base_tags` `appearance_desc` `outfits[{name, tags, desc, character_tag, default}]` `unassigned` `refusal` |
| `labels.json` | 参考图标注、空值占位、没有补充描述时的默认要求 |

使用 AI 助手里所选模型的默认思考档。代码层只做格式校验：造型标签去掉与基础标签重复的、专属角色标签只接受勾选的变体（整理时只接受原值）、默认造型恰好一个。

## fill/schema.json

LLM 输出 JSON 的结构（10 个字段 + `refusal`，全部为字符串）。接口支持结构化输出时直接作为约束发送。
`refusal` 正常为空；LLM 在其中写了原因时，前端会显示「LLM 拒绝了这个请求：原因」，不填入字段。
**改字段名需要同步改代码**（前端表单、拼接顺序都依赖这些键名）。

## test/ —— 其它提示词

| 文件 | 内容 |
|---|---|
| `probe.md` | 「测试」按钮的识图问题（测试图为左右两半随机两种颜色） |

## 占位符

`{{safety}}` `{{current_fields}}` `{{description}}` `{{aspect_ratios}}` `{{n_images}}` `{{pose_input_type}}`；
`labels.json` 中的 `{{n}}` 为图片序号。未知的占位符会原样保留。

## 代码层的处理

- **拒绝识别**：LLM 用自然语言拒绝（"I'm sorry…"、"抱歉…"）或填写了 `refusal` 时，显示明确的拒绝原因，不把空结果填进字段。

## import/ —— 从图片读取：AI 拆分

绘图页「从图片读取提示词」读到其它工具（WebUI、ComfyUI、NovelAI）或旧版本生成的图时，把图里的正 / 负向提示词交给 LLM 拆到各字段。
使用 AI 填写卡片里选的模型和思考档；一次请求，只有文字。

| 文件 | 内容 |
|---|---|
| `system.md` | 系统提示词：工作场景、10 个字段的归类说明、只分类不改写的拆分规则、输出要求 |
| `user.md` | 用户消息：`{{positive}}` `{{negative}}` |
| `labels.json` | 图片里没有负向提示词时 `{{negative}}` 的占位文字 |
| `schema.json` | 输出 JSON 结构（9 个字段 + refusal，没有 aspect_ratio） |

代码层校验：原文每个标签都要出现在某个字段里，LLM 漏掉的补进 appearance / neg 并提示；artist 每项补 @。
