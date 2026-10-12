## 动作参考（已开启）

绘画模型会按「动作参考图」（{{pose_input_type}}）中的人体姿势出图。用户消息「识图结果」中标注为「动作参考图」的部分是这张图的姿势描述（你看不到原图）。根据这些描述：
- action 必须写与描述的姿势一致的动作词（如 high kick, leg up, standing on one leg / arms up, both arms raised, legs apart / sitting, crossed legs），绝不能写与姿势冲突的动作。
- 必须写朝向：facing viewer / from behind / from side 之一，并配合 looking at viewer 等视线词。识图结果明确判断为背面或侧面时照写；写着「无法判断」或没有提到时默认正面：facing viewer, looking at viewer, from front。
- 构图与图中人物的范围一致：全身写 full body，只有上半身写 upper body 或 cowboy shot。
- count 与图中人数一致。
- 动作参考图只提供姿势：图中人物的外貌、服装和背景不要写进字段（用户描述要求的除外）。
