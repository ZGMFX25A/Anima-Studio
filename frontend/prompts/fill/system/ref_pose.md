## 动作参考（已开启）

绘画模型会按标注为「动作参考图」的图片（{{pose_input_type}}）中的人体姿势出图。请观察图片，然后：
- action 必须写与图中姿势一致的动作词（如 high kick, leg up, standing on one leg / arms up, both arms raised, legs apart / sitting, crossed legs），绝不能写与图中姿势冲突的动作。
- 必须写朝向：facing viewer / from behind / from side 之一，并配合 looking at viewer 等视线词。骨架图本身不含朝向信息，无法判断时默认正面：facing viewer, looking at viewer, from front。
- 构图与图中人物的范围一致：全身写 full body，只有上半身写 upper body 或 cowboy shot。
- count 与图中人数一致。
