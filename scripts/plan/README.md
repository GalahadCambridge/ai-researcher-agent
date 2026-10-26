# 第 2 步：先用 FakeLLM 跑通计划阶段

依据 `docs/开工指南/实验.md` 第 2 项和详细设计 3 的 4.1–4.6 节。
以下 demo 的模型回复全部由 `FakeLLM` 预设，不调用 API。
`PlanStage` 默认 `experiment.llm_backend: fake`，即使已经配置 Key 也不会自动调用真实后端。
正式调用可显式设置为 `api`，使用项目原有 LLMGateway；模型、Key 变量名和预算沿用主干配置。
demo 仍固定使用 FakeLLM，不提供付费调用开关；API 适配先通过本地模拟响应测试。
不需要 `.[llm]`、API Key 或 Ollama；日志中的 `ollama/fake-plan` 只是本地测试别名。

从仓库根目录运行，使用已有 `air` 环境。若主干依赖尚未安装，先执行一次
`python -m pip install -e '.[dev]'`。新增主干依赖仅为 `jsonschema`，实验环境中已安装则无需另装。
不要为本步安装 `.[llm]`。

```bash
python -m scripts.plan.demo --workspace "$AIR_HOME/dev_workspaces/plan_smoke01" --task smoke
python -m scripts.plan.demo --workspace "$AIR_HOME/dev_workspaces/plan_repair01" --task smoke --scenario repair
python -m scripts.plan.demo --workspace "$AIR_HOME/dev_workspaces/plan_sst2_01" --task text_cls_lowres
```

每次使用新空目录。请先准备 SST-2 数据与模型。本轮离线验收只使用已有资产、不加载 GPU 模型；生产 ensure_data 会按详细设计处理缺失数据的准备和版本确认。
数据目录依次取 `AIR_DATA_DIR`、`$AIR_HOME/data/sst2`；不读取原先手动训练结果来捏造计划结果。
两种任务默认都用人工编写的测试选题，不是同学 Agent 的真实选题。

成功标志：`state=PlanPending`、`backend=FakeLLM`、`llm_usd=0`、`training_submissions=0`。
工作区产出 `plan/experiment_plan.md`、`plan/tasks.json`、`plan/precheck.json`，以及引擎的审批和版本记录。
默认 SST-2 计划为 3 方法 × 500 条训练数据 × 3 种子，dev 评价，共 9 次待执行任务。
这些只是待执行任务，本步不会运行它们，不会创建真实实验结果。
许可未知、测试选题的待确认事项是预期的 warn；任何 fail 都不能自动变成通过。

可选场景：

- `--scenario repair`：第一份预设计划错误地把 test 用于 explore，检查失败；第二份预设回复修正。
- `--scenario fail`：三份预设回复始终错误，自动修订两次后停在 `Paused`，无默认答案，不自动提交审批。
- `--scenario bad-json`：先返回坏 JSON，验证网关格式重试也只调用 FakeLLM。
- `--scenario revise`：模拟退回计划，再读取第二份预设回复，生成修订版本。

FakeLLM **不理解或推理**选题与修改意见，以上修复和修订只是预先写好的不同响应。
测试可验证控制流程、文件契约和保护规则，不能证明真实模型能正确规划或修复。
成本使用 task.yaml 的保守估计，预算计算包含允许的额外种子；不是本机测得的耗时。
计划允许 `needs_code=true`；执行阶段使用受限编码工具和语义守卫。现有 schema 不支持的配置按用户要求停止，不修改 schema。
规划规则已放入并验证项目的 `skills/experiment-planning/`，结果分析由实验模块自己的 analysis.py 完成。SSH 不在本期范围。

接同学工作时，先在源项目完成选题审批，再使用一个新目录：

```bash
python -m scripts.plan.demo --workspace "$AIR_HOME/dev_workspaces/plan_handoff01" \
  --from-project /实际的源项目目录
```

入口从源项目的检查点和审批记录确认已批准版本，读取档案中的那个版本，复制到独立工作区，
原始项目不改动；原版本引用写入 `idea/import_source.json`。源项目 task 必须真实匹配所需任务，
smoke 选题不能强行改成 SST-2。只映射明确的方法别名，LoRA-RITE 不会被换成 LoRA。
导入模式仍然只用 FakeLLM，不运行同学的文献阶段、不请求 API。

```bash
python -m pytest tests/test_plan_stage.py -q
air dev check "$AIR_HOME/dev_workspaces/plan_sst2_01" --owner 实验
```

测试用小型本地数据夹具，不依赖真实数据、GPU 或 API，并主动阻断网络与真实 LLM 后端调用。
通用 `air dev run-stage plan --fake-llm` 只内置示例阶段的回复，不适用于本阶段；请先使用专用 demo。
开发工作区不会登记到后台，入口停止在计划待审批处；本步不要启动后续假实验阶段来替代真实训练。
