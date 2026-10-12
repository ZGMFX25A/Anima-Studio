# 提示词文件说明

这里是发给 LLM 的全部文字，代码里不写死任何提示词。修改后**立即生效**（每次请求都会重新读取），不用重启。
每次请求实际发出的完整内容记录在 `frontend/logs/llm_last_request.json`，可对照调整。

```
prompts/
├─ fill/                 「AI 填写」：根据描述生成 / 修改绘画提示词字段
│  ├─ system/            系统提示词片段（按情况挑选后拼接成一条系统提示词）
│  ├─ user/              用户消息模板与图片标注
│  └─ schema.json        LLM 输出的 JSON 结构
├─ search/               「联网检索」第一步：识别描述中的作品角色与画师（单独一次 LLM 调用）
├─ persona/              「人设」页：从 Danbooru 导入时的 AI 拆分、AI 整理
└─ test/                 其它：菜单 → LLM 的「测试」按钮
```

## fill/system/ —— 绘画用的系统提示词

按下表顺序拼接（用空行分隔），「何时使用」不满足的片段跳过：

| 顺序 | 文件 | 何时使用 | 内容 |
|---|---|---|---|
| 1 | `system.md` | 总是 | 角色与工作场景、不可逾越的界限、只输出 JSON、标签格式 |
| 2 | `fields.md` | 总是 | 10 个字段的定义（含画幅可选值 `{{aspect_ratios}}`）；多人时 nltags 要逐个人物写清服装和配饰 |
| 3 | `mode_generate.md` | 点「生成」 | 从零生成全部字段 |
| 3 | `mode_modify.md` | 点「修改」 | 在当前字段 `{{current_fields}}` 基础上修改 |
| 4 | `safety_levels.md` | 总是 | 内容分级标准：四个等级的含义、标签示例、写进哪个字段、neg 反向约束 |
| 5 | `safety_fixed.md` | 指定了安全等级 | 必须使用 `{{safety}}` 并排除其它三个，按该等级如实写 |
| 5 | `safety_auto.md` | 安全等级「不指定」 | 由 LLM 判断，恰好一个 |
| 6 | `user_images.md` | 附带了灵感图且模型能看图 | 灵感图只作参考（`{{n_images}}` 张） |
| 7 | `ref_character.md` | 开启人物参考，模型能看图 | 观察人物参考图，核心特征 2~5 个等 |
| 7 | `ref_character_noimage.md` | 开启人物参考，模型看不到图 | 只用描述中的特征，没写则 appearance 留空 |
| 8 | `ref_pose.md` | 开启动作参考，模型能看图 | 动作词与骨架一致、必须写朝向（`{{pose_input_type}}`） |
| 8 | `ref_pose_noimage.md` | 开启动作参考，模型看不到图 | 描述写了动作才填，否则 action 留空 |
| 9 | `ref_both.md` | 两种参考同时开启 | 两部分要求同时满足 |
| 10 | `persona.md` | 打开「人设匹配」且命中了人设（描述中出现名称 / 别名，或在绘图页选用了人设） | 怎么用人设资料：基础外观全部保留、选造型、专属角色标签、画师偏好 |
| 10 | `web_character.md` | 打开「联网检索」且查到了角色 | 怎么用角色资料：标准标签、外貌标签取舍、补充说明优先 |
| 10 | `web_artist.md` | 打开「联网检索」且查到了画师 | artist 写标准画师标签（带 @），只写用户提到的画师 |
| 11 | `tag_reference.md` | 打开「标签参考」且有检索结果 | 怎么用 DanbooruSearch 的标准标签候选 |

## fill/user/ —— 用户消息

| 文件 | 内容 |
|---|---|
| `user_message.md` | 用户消息正文模板（`{{description}}` = AI 填写卡片里的描述） |
| `labels.json` | 每张图片前的标注文字（灵感图 / 人物参考图 / 动作参考图），动作参考图类型说明，描述为空时的占位文字 |
| `character_block.md` | 角色资料块的外框（`{{characters}}` = 下面每个角色拼起来） |
| `character_item.md` | 单个角色的排版：`{{name}}` `{{tag}}` `{{series}}` `{{wiki}}` `{{appearance}}` `{{notes}}`；值为空的行自动去掉 |
| `artist_block.md` / `artist_item.md` | 画师资料块与单个画师的排版：`{{name}}` `{{tag}}` `{{other_names}}` `{{post_count}}` `{{notes}}` |
| `tag_block.md` | 标签参考块（`{{tags}}` = 「标签（中文）」逗号分隔） |
| `persona_block.md` | 人设资料块的外框（`{{personas}}` = 下面每个人设拼起来） |
| `persona_item.md` | 单个人设的排版：`{{name}}` `{{kind}}` `{{how}}` `{{tag}}` `{{series}}` `{{count}}` `{{artist}}` `{{base_tags}}` `{{appearance_desc}}` `{{selected}}` `{{outfits}}`；值为空的行自动去掉 |
| `persona_outfit_item.md` | 单套造型的排版（保留行首缩进）：`{{name}}` `{{default}}` `{{tags}}` `{{desc}}` `{{character_tag}}` |
| `persona_labels.json` | 人设资料块里的固定文字：来源说明（描述中提到 / 绘图页选用）、类型名、「（默认）」、没有造型时的占位 |

用户消息的结构：`[标注] 图片` × N（灵感图 → 人物参考图 → 动作参考图）→ 人设资料块 → 角色资料块 → 画师资料块 → 标签参考块 → `user_message.md`。

## search/ —— 识别角色与画师

| 文件 | 内容 |
|---|---|
| `extract_system.md` | 系统提示词：找出描述中已有作品的角色（原文写法 / 英文名 / 作品）和用户明确提到的画师（原文写法 / 英文名） |
| `extract_user.md` | 用户消息模板（`{{description}}`；「修改」时 `{{current_character}}` `{{current_artist}}` 为当前字段） |
| `extract_schema.json` | 输出结构 `{"characters": [{"name", "name_en", "series"}], "artists": [{"name", "name_en"}]}` |

这一步使用所选模型的最低思考档（能关就关）。

## persona/ —— 人设页的 AI 拆分与整理

| 文件 | 内容 |
|---|---|
| `split_system.md` | 导入已有角色时的系统提示词：把 Danbooru 共现标签拆成 中文名 / 主体 / 基础标签 / 外观描述 / 造型 / 未归类 |
| `split_user.md` | 用户消息：`{{tag}}` `{{series}}` `{{post_count}}` `{{other_names}}` `{{wiki}}` `{{notes}}`（角色库补充说明）`{{tags}}`（共现标签与比例）`{{variants}}` `{{description}}`（AI 助手的补充描述）；值为空的行自动去掉 |
| `split_variant_item.md` | 每个勾选的造型变体：`{{tag}}` `{{post_count}}` `{{tags}}` |
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
