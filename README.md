# Interactive Web LLM Evaluation

用于记录大模型生成单文件交互式网页的测评提示词、原始答案与运行结果。

## 题目

| 编号 | 题目 | 提示词 |
| --- | --- | --- |
| 01 | 2048 + Roguelike 游戏 | [`prompts/01-2048-roguelike.md`](prompts/01-2048-roguelike.md) |
| 02 | FPV 穿越机花飞模拟器 | [`prompts/02-fpv-drone-simulator.md`](prompts/02-fpv-drone-simulator.md) |

## 记录约定

- 提示词原文保存在 `prompts/`。
- 模型生成的原始单文件答案保存在 `answers/<model>/`，不修改模型输出。
- 运行截图、录屏或其他证据保存在 `artifacts/<model>/`。
- 每个答案文件沿用题目编号，例如 `answers/<model>/01-2048-roguelike.html`。

