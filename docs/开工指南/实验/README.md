# 实验计划与执行 Agent：简明使用说明

功能和验收详情见 [交接清单](实验_交接清单.md)。以下从完整源码仓库运行，使用 Ubuntu / bash；其他机器替换本地路径。

## 1. 环境

新终端先执行；已有环境无需重装：

```bash
conda activate air
cd /home/plj/ai-researcher-agent
export AIR_HOME="${AIR_HOME:-$HOME/air}"
export AIR_DATA_DIR="${AIR_DATA_DIR:-$AIR_HOME/data/sst2}"
export HF_HOME="${HF_HOME:-$AIR_HOME/data/hf_cache}"
```

首次安装或更新依赖时执行一次：

```bash
python -m pip install -e '.[dev,llm]'
# 只有运行 SST-2 真实训练才需要；已有兼容 CUDA 版 torch 会保留
python -m pip install -r tasks/text_cls_lowres/template/requirements.txt
```

## 2. 先跑免费完整演示

```bash
EXP_DEMO="$AIR_HOME/demos/experiment_fake_$(date +%Y%m%d_%H%M%S)"
python -m scripts.experiment.demo \
  --workspace "$EXP_DEMO" --approve-plan-for-simulation
air dev check "$EXP_DEMO" --owner 实验
```

使用 **FakeLLM + FakeExecutor + smoke**，无 API、无训练，预期完成一轮并停在 `LogPending`。参数只模拟该隔离目录的计划审批；模拟数字不能用于论文。每次换新空目录。

## 3. 准备真实项目（数据/模型只准备一次）

```bash
python tasks/text_cls_lowres/prepare_data.py --out "$AIR_DATA_DIR"
python tasks/text_cls_lowres/prepare_model.py --out "$AIR_DATA_DIR/model_manifest.json"
python -c 'import torch; print(torch.__version__, torch.cuda.is_available()); assert torch.cuda.is_available()'
```

已有数据会校验后复用。若设置了 `AIR_MODEL_DIR`，它也必须指向有效模型；未设置时使用 manifest 记录的路径。模型配置和 Key 沿用 `$AIR_HOME/models.yaml`、`$AIR_HOME/.env`，`key_env` 与变量名保持一致。缺模板时用 `air models init` 生成，不把 Key 写入仓库。

使用文献部分已登记的项目，在选题等待审批时先停止后台，合并以下字段到该项目的 `project.yaml`，保留其他配置。`python` 替换为 `python -c 'import sys; print(sys.executable)'` 的输出：

```yaml
dev:
  stage_impl: {plan: real, experiment: real}
experiment:
  llm_backend: api
  allow_local_execution: true
  python: /home/plj/miniconda3/envs/air/bin/python
budget:
  llm_usd: {soft: 0.8, hard: 1.0}
```

这里的 $1 是可调整的项目停止线，不是供应商硬扣费上限。模型沿用已配置的 strong 档或 `exp.planner` / `exp.coder` / `exp.analyst` 路由。清除冲突的 `AIR_STAGE_IMPL` 环境变量，避免覆盖项目配置。任务必须与选题一致，例如 `tasks/text_cls_lowres`。

## 4. 正式连续运行与审批

终端一执行第 1 节的环境设置后启动后台并保持打开：

```bash
air serve
```

终端二使用同一环境和 `AIR_HOME`，把项目编号与审批编号替换为实际值：

```bash
PID="p-实际项目编号"
air status "$PID"
air approvals "$PID"
air show ap-实际审批编号 -p "$PID"
# 阅读内容并决定批准后再执行
air approve ap-实际审批编号 -p "$PID"
air status "$PID" --wait
air runs "$PID"
```

顺序为：**选题审批 → 自动生成计划 → 计划审批 → 自动试运行、正式执行和分析 → 重要日志审批 → Writing 交接**。每次用最新的实际审批编号；普通任务自动推进。最终日志批准后，后续写作由主干的 writing 阶段配置决定。

如果只想验证本模块，到写作交接即可。demo/local 脚本固定使用 FakeLLM；真实 API 流程使用这里的主干引擎。失败问题用 `air question "$PID"` 查看，再按实际选项回答；需要人工处理的错误不会靠反复 `resume` 自动绕过。

若提示启动状态不确定，先检查对应进程与日志，再选择“标记 lost 并用新编号重试”或“结束”；旧运行记录会保留。缺少 pip / `nvidia-smi` 只会在环境快照的 `unknown` 中说明，不阻止运行。切换任务时，方法别名、GPU 需求和试运行缩减参数写在该任务的 `task.yaml`（见详细设计 §3.2）。

## 5. 查看结果

正式结果默认在 `$AIR_HOME/projects/$PID/`：`plan/` 看计划，`runs/` 看每次实验，`artifacts/aggregates/` 看统计表，`logs/round_*.md` 看 Agent 分析与决定，`environments/` 看环境与复现说明。研究产物为英文；论文同学通过 `airesearcher.services.runs` 读取。

免费回归：`AIR_REAL_LLM=0 python -m pytest -q`。交接检查：`air dev check "$AIR_HOME/projects/$PID" --owner 实验`。数据、Key、模型权重和个人运行结果不提交 Git。
