## 内容分级标准

| 等级 | 含义 | 标签示例 |
|---|---|---|
| safe | 日常内容，没有性暗示 | — |
| sensitive | 性感、暗示，但没有裸露：暴露的服装、性感的姿势与表情；不出现乳头、生殖器或性行为 | swimsuit, bikini, lingerie, cleavage, midriff, bare shoulders, thighs, seductive smile |
| nsfw | 部分裸露或强烈的性暗示：上身裸露、透视、只穿内衣、衣衫半解；不出现生殖器或性行为 | topless, nipples, see-through, underwear only, partially undressed, open shirt |
| explicit | 完全裸露、生殖器、性行为 | nude, completely nude，以及用户描述的内容对应的 Danbooru 标签，如实写出 |

- 目标等级以内的内容都可以出现，高于目标等级的内容不能出现（例如 nsfw 下不写 explicit 级别的标签）。
- 衣着状态与身体特征写进 appearance；姿势、动作、表情、镜头写进 action；nltags 按「字段定义」把这些内容写进对应人物的句子（多人时写清各自的衣着状态与正在做什么、谁对谁做什么），不要只写场景与氛围。
- neg 中加入与等级相反的约束：
  - safe → 'sensitive, nsfw, explicit'
  - sensitive → 'nsfw, explicit, child, loli, shota'
  - nsfw → 'explicit, child, loli, shota'
  - explicit → 'censored, mosaic censoring, bar censor, child, loli, shota'
