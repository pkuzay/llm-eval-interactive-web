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
- 模型生成的原始答案保存在 `answers/<题号>/<model>/`，不修改模型输出。
- 运行截图、录屏或其他证据保存在 `artifacts/<题号>/<model>/`。
- 每道题下面按模型分别建立目录，便于横向比较同一道题的不同回答。

## 答案索引

| 题号 | DeepSeek 答案 |
| --- | --- |
| 01 | [`answers/01-2048-roguelike/deepseek/`](answers/01-2048-roguelike/deepseek/) |
| 02 | [`answers/02-fpv-drone-simulator/deepseek/`](answers/02-fpv-drone-simulator/deepseek/) |
| 03 | [`answers/03-racing-telemetry-analyzer/deepseek/`](answers/03-racing-telemetry-analyzer/deepseek/) |
| 04 | [`answers/04-balatro-web/deepseek/`](answers/04-balatro-web/deepseek/) |
| 05 | [`answers/05-double-wishbone-suspension/deepseek/`](answers/05-double-wishbone-suspension/deepseek/) |

更多目录说明见 [`answers/README.md`](answers/README.md)。
