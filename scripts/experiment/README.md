# 实验计划与执行模块：按需求验收

依据《需求分析》FR-20–32、FR-34–37、本地 FR-62，以及《详细设计 3》第 3–11 节。
代码使用文档规定的 `plan/{stage,expand,precheck,render}.py`、
`experiment/{stage,coder,repair,aggregate,analysis}.py` 和 `runtime/data.py`。
文献同学的模块不改动，读取他已经批准的 `idea/selected.md` 版本。

默认离线验收使用 **FakeLLM + FakeExecutor**。FakeLLM 提供预设模型回复，FakeExecutor 提供预设运行结果；
不训练 Agent、不训练分类模型、不调用真实 API，也不需要 API Key。已有数据缺失时，离线入口会要求先准备。
阶段已支持显式选择真实后端；这些离线测试不能证明真实大模型一定能正确生成或修复代码。

后端选择配置位于 `project.yaml`，已登记在详细设计 3 第 12 节：

```yaml
experiment:
  llm_backend: fake  # 默认；正式调用时由用户显式改为 api
```

`api` 模式直接使用主干的 LLMGateway/LiteLLMBackend。模型路由、Key 变量名、重试和扣费
沿用主干第 6 节：`models.roles` 中的 `exp.planner`、`exp.coder`、`exp.analyst` 可分别选模型，
或沿用已有 strong 档位。Key 仍只放环境变量或 `${AIR_HOME}/.env`。
本地训练仍需独立显式开启 `allow_local_execution`；选择 API 不会自动批准计划或启动训练。
现有 demo 和 local 入口固定使用 FakeLLM。生产运行使用主干引擎及已登记的真实阶段实现。
离线适配测试通过本地模拟响应验证 API 分支，不消耗费用。
另于 2026-10-23 在用户明确授权后验证了真实 OpenRouter 计划调用、RTX 4060 CUDA
执行和真实 API 分析。计划及重要日志仍由用户逐项审批，用户全局模型配置没有修改。
真实 API 需要项目已有的可选依赖 `python -m pip install -e '.[llm]'`。
预算记账由原网关负责；编码工具循环在下一次请求前检查现有 Budget。
费用在响应后记账，硬上限并非供应商侧的预付限额；到达上限后由主干暂停。

适配离线测试（无需 Key、网络或 GPU）：

```bash
python -m pytest tests/test_experiment_stage.py -q -k api
```

在已有 air 环境、项目根目录运行。每次使用新空目录：

```bash
cd /home/plj/ai-researcher-agent
export AIR_HOME="$PWD/local_results"
export AIR_DATA_DIR="$AIR_HOME/data/sst2"
export AIR_MODEL_DIR="$AIR_HOME/data/model"

# 完整冒烟流程：计划、trial、正式运行、分析、总结日志
python -m scripts.experiment.demo \
  --workspace "$AIR_HOME/current_validation/manual_acceptance_new" \
  --approve-plan-for-simulation

# 从文献模块已批准的选题开始，并注入一次失败验证自动修复
python -m scripts.experiment.demo \
  --from-project "$AIR_HOME/literature_plan_execution01/plan" \
  --workspace "$AIR_HOME/current_validation/manual_handoff_new" \
  --scenario retry --approve-plan-for-simulation

# 验证文档内的复现政策（不是自定义创新规则）
python -m scripts.experiment.demo \
  --workspace "$AIR_HOME/current_validation/manual_replication_new" \
  --scenario replicate --approve-plan-for-simulation
```

入口中的 `--approve-plan-for-simulation` 只允许在新模拟工作区演示人工审批，不批准源项目。
普通任务自动调度；重要异常日志、计划变更和写作前总结按需求保留人工审批。
复现场景会模拟批准中间日志以完成第二轮，最终日志仍停在 `LogPending`。
测试还会验证批准最终日志后仅转入 `Writing` 状态；实验模块不生成论文。

编码工具只有 list_files/read_file/write_file/replace_in_file/run_trial/read_log/finish。
没有通用 shell、依赖安装或联网工具；每会话最多 15 轮工具调用、4 次 trial。
用户已明确要求保留现有 schema：不支持的配置会停止，不能通过修改 schema 绕过校验。
受保护文件、实验语义、原始运行与指标不可改。OOM 可在允许字段内调整等效批大小；
其他错误按类别有限修复；失败记录和 retry_of 保留，缺依赖请用户在实验解释器中安装。

结果按文档格式交接：

- `plan/experiment_plan.md`、`plan/tasks.json`、`plan/precheck.json`；
- `runs/<run_id>/`：代码、配置、原始指标、环境、资源、日志与状态；
- `artifacts/aggregates/<comparison>.json/.csv/.md` 和 `all.*`；
- `logs/round_<n>.md`：观察表、解释及证据等级、异常处置、假设状态、决定、成本、受影响实验；
- `environments/data_checksums.json`；真实本地运行另有环境锁和 REPRODUCE.md。

论文同学通过 `airesearcher.services.runs` 的 load_aggregate/resolve_value/recompute_aggregate/load_run 读取。
模拟指标不能当实验结论；dev 数字不能当最终 test 评价。
之前自加的结构化复现字段、额外结果清单及其导出入口已移除。

```bash
mkdir -p local_results/current_validation/test_tmp
AIR_REAL_LLM=0 python -m pytest -q -p no:cacheprovider \
  --basetemp=local_results/current_validation/test_tmp/manual_all
ruff check --no-cache .
air dev check local_results/current_validation/requirements_fake/handoff_final --owner 实验
```

运行时测试包含标准库 smoke 子进程，用来检查包装器、取消、超时和恢复；不会加载训练模型。
SSH、多 GPU、自动安装依赖、论文写作和可选非 NLP 演示不在本次实现范围。
`scripts.experiment.local` 保留原有显式本地训练入口，固定使用 FakeLLM。
真实 API 与 GPU 的独立验收工作区为 `${AIR_HOME}/projects/p-20261023-58db/`；
实际状态和费用见 `environments/complete_validation.json`，详细结果见 `logs/round_1.md`
及 `artifacts/aggregates/`。本次上限为累计 $1，包含早期失败与重试。
金额限制属于项目配置，可在 `project.yaml` 的 `budget.llm_usd` 中调整；创建项目时不指定
预算则使用主干默认 $4 提醒、$5 停止，不等于无限额。回归测试仍默认离线。

这次收尾已按详细设计 §7.3/§9.3 补齐方法源码与归档 Markdown 表输入，并用真实
编码 Agent 在隔离副本修复一次受控错误、完成 CUDA trial。编码提示现在明确所有路径
相对于项目根目录，并提供准确的 `trial_task_keys`。原已批准计划、日志和六次正式结果不变。
真实工具调用需要 LiteLLM 的 MCP 可选依赖，项目的 `.[llm]` 已包含该 extra；缺包仍由用户
安装，Agent 不会自动安装。更新已有环境可运行 `python -m pip install -e '.[llm]'`。

详细设计第 15 节的评价交接见同目录 `EVALUATION_HANDOFF.md`。成本试点只读现有项目，
不会训练或调用 API：

```bash
python -m eval.metrics.experiment_pilot \
  --workspace /home/plj/air/projects/p-20261023-58db \
  --annotations /home/plj/air/projects/p-20261023-58db/environments/alignment/annotations.json \
  --out /home/plj/air/projects/p-20261023-58db/environments/alignment/experiment_pilot.json
```

首轮交接试点费用约 $0.1423；加上本次编码与分析验证，累计记录费用约 $0.2991。
用户已对一条历史后续建议给出人工评分；它不表示批准追加实验。独立复现包和英文报告草稿
已准备，论文同学仍需按文档独立执行、记录结果。不能用 FakeLLM 或原作者重跑代替该项。
