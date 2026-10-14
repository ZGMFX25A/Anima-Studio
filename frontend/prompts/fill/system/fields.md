## 字段定义

- quality_meta_year_safe：质量/元数据/年份/安全标签。必须明确 safe / sensitive / nsfw / explicit 之一。示例：'masterpiece, best quality, highres, newest, year 2025, sensitive'
- aspect_ratio：画幅比例（可选）。只能从以下取值中选择：{{aspect_ratios}}。按构图需要选择（如全身站姿用 2:3，宽阔风景用 16:9）；没有明确倾向时填 ""。
- count：人数标签，如 '1girl' / '2girls' / '1boy' / '1other'；单人画面加 'solo'（如 '1girl, solo'）。没有人物时用 'no humans'。
- character：角色名（可含括号作品信息），如 'hatsune miku' 或 'yunli (honkai star rail)'。原创角色填 ""。
- series：作品/系列名，如 'vocaloid' / 'honkai star rail'。原创角色填 ""。
- artist：画师标签，必须以 @ 开头。仅在用户明确提到画师或特定画风时才填写；无明确需求时留空，不默认追加画师。支持多画师混合（如 '@fkey, @jima'），但稳定性会下降。
- appearance：外貌标签：发型、发色、瞳色、体型、服装、配饰等。
- action：动作标签：动作、姿势、朝向、视线、表情；也包括构图（full body / upper body / cowboy shot）与镜头角度。
- nltags：英文自然语言描述（完整的英文句子）。标签只是一串并列的词，模型分不清「哪个外貌、哪个动作属于哪个人」；nltags 的主要用途就是把外貌和动作**绑定到具体人物**，辅助标签区分人物。
  - 多个人物：每个人物单独一句，依次写：
    1. 指代：已有作品的角色用角色名；其他人物（包括原创人物）用最显著的外观特征或位置，如 "the silver-haired girl on the left"、"the tall black-haired boy"。同一人物在全文中用同一个指代。
    2. 核心外貌：发型发色、瞳色、服装、配饰等能把这个人和其他人区分开的特征。
    3. 正在做什么：动作、姿势、朝向、视线、表情。
    人物之间有互动时写清谁对谁做什么（如 "the blonde girl hugs the silver-haired girl from behind"）。人物句写完后，最多再用一句写场景与光线。
    例如："The silver-haired girl on the left wears a sailor uniform with a red neckerchief and black thighhighs, and waves at the viewer with a bright smile. The black-haired girl on the right wears a white maid dress with a frilled headdress, holding a tray with both hands and looking at the silver-haired girl. They stand in a sunlit café."
  - 单个人物：一到两句，写人物正在做什么以及与周围物品、环境的关系，再用一句写场景与光线；外貌只写标签难以表达的部分。
  - 没有人物：一到两句写场景、光线与氛围。
  - nltags 与标签必须一致：每个人物的外貌与动作在 appearance / action 里都要有对应的标签，nltags 只是把它们分配到人身上，不要写出标签里没有的人物特征，也不要与标签矛盾。
  - 只写画面内容，不写评价、总结或强调性的空话（如 "the contrast between them is clear"）。
- neg：负面提示词。建议包含：worst quality, low quality, score_1, score_2, score_3, artist name, blurry, jpeg artifacts, chromatic aberration, 手脚崩坏的反向词（bad anatomy, bad hands, extra fingers, missing fingers, extra limbs, deformed 等）, text, watermark, logo 等，并按「内容分级标准」加入与安全等级相反的约束。
- refusal：保留字段，始终输出 ""。
