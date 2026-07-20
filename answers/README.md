# Answers

模型答案按题目归档，每道题下面再按模型区分：

```text
answers/
├── 01-2048-roguelike/
│   └── deepseek/
├── 02-fpv-drone-simulator/
│   └── deepseek/
├── 03-racing-telemetry-analyzer/
│   └── deepseek/
├── 04-balatro-web/
│   └── deepseek/
└── 05-double-wishbone-suspension/
    └── deepseek/
```

新增模型时，在对应题号目录下创建模型目录，例如：

```text
answers/01-2048-roguelike/qwen/
answers/01-2048-roguelike/kimi/
```

所有答案均按模型原始输出归档，不主动修正代码或合并文件。第 04 题的 DeepSeek 输出本身是多文件网页，因此保留其 `css/`、`js/` 目录结构。
