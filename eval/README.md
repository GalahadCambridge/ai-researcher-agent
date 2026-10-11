# 评价工具

批量运行评价轨迹，每条轨迹是一个独立的项目，最后汇总成一张 `summary.csv`。说明见详细设计 1 第 12 节和附录 B。

## 先跑一遍冒烟配置（不需要 API Key，约 30 秒）

```bash
air eval run eval/configs/smoke_batch.yaml
```

看到 4 行，`结果` 一列都是 `completed`。结果在 `eval/results/smoke_batch/`：

- `summary.csv`：每条轨迹一行（组、实例、第几次、结果、费用、运行数、提问次数……）；
- `<组>/<实例>/rep<k>/`：每条轨迹的完整项目目录，可以用 `air dev check <目录>` 检查，或在里面看论文、运行记录。

中途按 Ctrl+C 停下后，再运行同一条命令：跑完的轨迹跳过，没跑完的从断点继续。想从头再来，删掉 `eval/results/<名字>/`。

只跑某一组：

```bash
air eval run eval/configs/smoke_batch.yaml --arm full
```

## 正式评价

1. 在 `eval/instances/` 下为每个研究问题写一个文件，照 `example.yaml` 的格式；
2. 在 `eval/prompt_pins.yaml` 里写好要固定的提示版本；
3. 改 `eval/configs/main_comparison.yaml` 的 `instances`，确认 `~/air/models.yaml` 里有配置中用到的模型（`air models test claude-fast`）；
4. 运行：

```bash
air eval run eval/configs/main_comparison.yaml
```

`结果` 一列的含义：`completed` 走到已完成；`needs_intervention` 遇到没有默认选项的问题，需要人处理；`budget_exhausted` 预算用尽；
`failed` 出错停止；`paused` 暂停且没有问题可答；`timeout` 超过 `timeout_hours`；`error` 轨迹本身出错（`reason` 一列有原因）。
它们都计入完成率的分母。

## 指标脚本

各支柱负责人的指标计算脚本放在 `eval/metrics/` 下：读 `summary.csv` 里每行的 `workspace` 目录中的文件来计算。
