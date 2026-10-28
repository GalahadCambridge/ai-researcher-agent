### all：全部实验

| experiment_id | method | n | mean | std | 95% CI |
|---|---|---|---|---|---|
| E1 | baseline | 3 | 0.6942 | 0.0138 | [0.6600, 0.7285] |
| E2 | main | 3 | 0.7799 | 0.0086 | [0.7584, 0.8014] |
| E3 | ablation | 3 | 0.7371 | 0.0176 | [0.6933, 0.7809] |

| 对比 | 均值差 | Welch t | p |
|---|---|---|---|
| {"experiment_id": "E2", "method": "main"} vs {"experiment_id": "E1", "method": "baseline"} | +0.0857 | 9.1231 | 0.0017 |
| {"experiment_id": "E3", "method": "ablation"} vs {"experiment_id": "E1", "method": "baseline"} | +0.0429 | 3.3181 | 0.032 |

排除的运行：2 个（r-20261028-142810-7783: kind=trial；r-20261028-142813-f994: failed: nonzero_exit）
