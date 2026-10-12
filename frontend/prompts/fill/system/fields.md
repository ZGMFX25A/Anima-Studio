## 字段定义

- quality_meta_year_safe：质量/元数据/年份/安全标签。必须明确 safe / sensitive / nsfw / explicit 之一。示例：'masterpiece, best quality, highres, newest, year 2025, sensitive'
- aspect_ratio：画幅比例（可选）。只能从以下取值中选择：{{aspect_ratios}}。按构图需要选择（如全身站姿用 2:3，宽阔风景用 16:9）；没有明确倾向时填 ""。
- count：人数标签，如 '1girl' / '2girls' / '1boy' / '1other'；单人画面加 'solo'（如 '1girl, solo'）。没有人物时用 'no humans'。
- character：角色名（可含括号作品信息），如 'hatsune miku' 或 'yunli (honkai star rail)'。原创角色填 ""。
- series：作品/系列名，如 'vocaloid' / 'honkai star rail'。原创角色填 ""。
- artist：画师标签，必须以 @ 开头。仅在用户明确提到画师或特定画风时才填写；无明确需求时留空，不默认追加画师。支持多画师混合（如 '@fkey, @jima'），但稳定性会下降。
- appearance：外貌标签：发型、发色、瞳色、体型、服装、配饰等。
- action：动作标签：动作、姿势、朝向、视线、表情；也包括构图（full body / upper body / cowboy shot）与镜头角度。
- nltags：英文自然语言描述（一到三句完整的英文句子），补充标签难以表达的内容：场景、背景、光照、氛围、画面细节以及元素之间的关系。
  - 画面中有多个人物时，标签无法表达「哪件衣服、哪个配饰属于哪个人」，nltags 必须逐个人物写清各自的服装和配饰，不要混在一起：已有作品的角色用角色名区分，其他人物用位置或发色、瞳色等外观特征区分。例如："The silver-haired girl on the left wears a sailor uniform with a red neckerchief and black thighhighs; the black-haired girl on the right wears a white maid dress with a frilled headdress." 这种情况下可以超过三句。
- neg：负面提示词。建议包含：worst quality, low quality, score_1, score_2, score_3, artist name, blurry, jpeg artifacts, chromatic aberration, 手脚崩坏的反向词（bad anatomy, bad hands, extra fingers, missing fingers, extra limbs, deformed 等）, text, watermark, logo 等，并按「内容分级标准」加入与安全等级相反的约束。
- refusal：保留字段，始终输出 ""。
