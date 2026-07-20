# Answers

模型答案按题目归档，每道题下面再按模型区分：

```text
answers/
├── 01-2048-roguelike/
│   ├── deepseek/
│   ├── kimi-k3/
│   ├── glm-5.2/
│   ├── qwen-3.8-max-preview/
│   └── gpt-5.6-sol/
├── 02-fpv-drone-simulator/
│   ├── deepseek/
│   ├── kimi-k3/
│   ├── glm-5.2/
│   ├── qwen-3.8-max-preview/
│   └── gpt-5.6-sol/
├── 03-racing-telemetry-analyzer/
│   ├── deepseek/
│   ├── kimi-k3/
│   ├── glm-5.2/
│   ├── qwen-3.8-max-preview/
│   └── gpt-5.6-sol/
├── 04-balatro-web/
│   ├── deepseek/
│   ├── kimi-k3/
│   ├── glm-5.2/
│   ├── qwen-3.8-max-preview/
│   └── gpt-5.6-sol/
└── 05-double-wishbone-suspension/
    ├── deepseek/
    ├── glm-5.2/
    ├── qwen-3.8-max-preview/
    ├── gpt-5.6-sol/
    └── kimi-k3/
```

最终对比模型及目录名：

| 模型 | 目录名 |
| --- | --- |
| DeepSeek | `deepseek` |
| Kimi K3 | `kimi-k3` |
| GLM 5.2 | `glm-5.2` |
| Qwen 3.8 Max Preview | `qwen-3.8-max-preview` |
| GPT 5.6 Sol | `gpt-5.6-sol` |
| Opus 4.8 | `opus-4.8` |

新增结果时，在对应题号目录下创建模型目录。所有答案均按模型原始输出归档，不主动修正代码或合并文件。若模型同时给出说明文本与代码，则分别保存为 `response.md` 和原始代码文件。第 04 题的 DeepSeek 与 Kimi K3 输出均为多文件网页，因此保留其原始目录结构。
