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

| 模型 | 01 | 02 | 03 | 04 | 05 |
| --- | --- | --- | --- | --- | --- |
| DeepSeek | [已归档](answers/01-2048-roguelike/deepseek/) | [已归档](answers/02-fpv-drone-simulator/deepseek/) | [已归档](answers/03-racing-telemetry-analyzer/deepseek/) | [已归档](answers/04-balatro-web/deepseek/) | [已归档](answers/05-double-wishbone-suspension/deepseek/) |
| Kimi K3 | [已归档](answers/01-2048-roguelike/kimi-k3/) | [已归档](answers/02-fpv-drone-simulator/kimi-k3/) | [已归档](answers/03-racing-telemetry-analyzer/kimi-k3/) | [已归档](answers/04-balatro-web/kimi-k3/) | 待补充 |
| GLM 5.2 | [已归档](answers/01-2048-roguelike/glm-5.2/) | [已归档](answers/02-fpv-drone-simulator/glm-5.2/) | [已归档](answers/03-racing-telemetry-analyzer/glm-5.2/) | [已归档](answers/04-balatro-web/glm-5.2/) | 待补充 |
| Qwen 3.8 Max Preview | 待补充 | [已归档](answers/02-fpv-drone-simulator/qwen-3.8-max-preview/) | 待补充 | 待补充 | [已归档](answers/05-double-wishbone-suspension/qwen-3.8-max-preview/) |
| GPT 5.6 Sol | 待补充 | 待补充 | 待补充 | 待补充 | 待补充 |
| Opus 4.8 | 待补充 | 待补充 | 待补充 | 待补充 | 待补充 |

更多目录说明见 [`answers/README.md`](answers/README.md)。
