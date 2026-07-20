# Interactive Web LLM Evaluation

用于记录大模型生成单文件交互应用的测评提示词、原始答案与运行结果。

## 题目

| 编号 | 题目 | 提示词 |
| --- | --- | --- |
| 01 | 2048 + Roguelike 游戏 | [`prompts/01-2048-roguelike.md`](prompts/01-2048-roguelike.md) |
| 02 | FPV 穿越机花飞模拟器 | [`prompts/02-fpv-drone-simulator.md`](prompts/02-fpv-drone-simulator.md) |
| 03 | 赛车实时遥测与多圈对比工具 | [`prompts/03-racing-telemetry-analyzer.md`](prompts/03-racing-telemetry-analyzer.md) |
| 04 | 小丑牌网页版 | [`prompts/04-balatro-web.md`](prompts/04-balatro-web.md) |
| 05 | 双叉臂悬挂运动学显示页面 | [`prompts/05-double-wishbone-suspension.md`](prompts/05-double-wishbone-suspension.md) |

## 记录约定

- 提示词原文保存在 `prompts/`。
- 模型生成的原始单文件答案保存在 `answers/<model>/`，不修改模型输出。
- 运行截图、录屏或其他证据保存在 `artifacts/<model>/`。
- 每个答案文件沿用题目编号和指定的产物类型，例如 `answers/<model>/01-2048-roguelike.html` 或 `answers/<model>/03-racing-telemetry-analyzer.py`。
