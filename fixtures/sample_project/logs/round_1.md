# 第 1 轮过程日志（假实现）

## 观察

### C1：main 是否优于基线 baseline

| method | n | mean | std | 95% CI |
|---|---|---|---|---|
| baseline | 3 | 0.6942 | 0.0138 | [0.6600, 0.7285] |
| main | 3 | 0.7799 | 0.0086 | [0.7584, 0.8014] |

| 对比 | 均值差 | Welch t | p |
|---|---|---|---|
| {"method": "main"} vs {"method": "baseline"} | +0.0857 | 9.1231 | 0.0017 |

排除的运行：2 个（r-20261028-142810-7783: kind=trial；r-20261028-142813-f994: failed: nonzero_exit）


### C2：消融：ablation 与 main 的差别

| method | n | mean | std | 95% CI |
|---|---|---|---|---|
| ablation | 3 | 0.7371 | 0.0176 | [0.6933, 0.7809] |
| main | 3 | 0.7799 | 0.0086 | [0.7584, 0.8014] |

| 对比 | 均值差 | Welch t | p |
|---|---|---|---|
| {"method": "main"} vs {"method": "ablation"} | +0.0428 | 3.7752 | 0.0343 |

排除的运行：1 个（r-20261028-142813-f994: failed: nonzero_exit）


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


## 解释

- C1：{'method': 'main'} 相对 {'method': 'baseline'} 的 score 均值差为 +0.0857（Welch p = 0.0017），证据等级：supported_by_experiment。
- C2：{'method': 'main'} 相对 {'method': 'ablation'} 的 score 均值差为 +0.0428（Welch p = 0.0343），证据等级：supported_by_experiment。

## 异常与处置
- 跳过的失败组合：无

## 决定与理由
- stop_and_write：计划内实验全部完成（停止条件 1）。“得到正面结果”不是停止条件。

## 预计成本
- 进入写作阶段，不再提交新运行。
