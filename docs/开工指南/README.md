# 开工指南

每人读两页：**这一页**（所有人都要做的事），加上**自己那一页**：

| 分工 | 指南 | 详细设计 |
|---|---|---|
| 文献查阅与选题 | [文献交接入口](文献/README.md) | [2_文献查阅与选题_详细设计.md](../详细设计/2_文献查阅与选题_详细设计.md) |
| 实验计划与执行 | [实验.md](实验.md) | [3_实验计划与执行_详细设计.md](../详细设计/3_实验计划与执行_详细设计.md) |
| 论文撰写与绘图 | [论文.md](论文.md) | [4_论文撰写与绘图_详细设计.md](../详细设计/4_论文撰写与绘图_详细设计.md) |
| GUI | [GUI.md](GUI.md) | [5_GUI_详细设计.md](../详细设计/5_GUI_详细设计.md) |

指南只讲“从哪里开始、主干已经准备了什么、怎么测试”。要做什么、文件格式是什么，以详细设计为准。

---

## 1. 第一天：所有人都做

1. **安装**：按 [README 第 1 节](../../README.md#1-安装只需做一次)。用 Windows 的同学看其中 conda 的说明。
2. **跑通一遍完整流程**：按 [README 第 2 节](../../README.md#2-跑通一遍完整流程约-3-分钟不需要-api-key)，大约 3 分钟，不需要 API Key。
   跑完你会知道：一个项目会经过哪些状态、审批长什么样、项目文件夹里有哪些文件。
3. **（GUI 以外的同学）读示例阶段**：[`airesearcher/stages/example/README.md`](../../airesearcher/stages/example/README.md)。
   几十行代码演示了写一个阶段需要的全部写法，然后在自己电脑上跑一下：

   ```bash
   air dev new-workspace /tmp/ws --idea "比较两种小模型在情感分类上的表现" --impl idea=example
   ```

   ```bash
   air dev run-stage example --workspace /tmp/ws --steps 3 --fake-llm
   ```

   Windows 上把 `/tmp/ws` 换成任意一个空目录，如 `C:\air-ws`。
4. **建自己的分支**，第一个 PR 越小越好（例如“新建目录 + 一个能跑的空阶段 + 一条测试”）。
   提交流程见 [如何提交PR.md](../如何提交PR.md)。

## 2. 主干已经准备好的工具（不用自己写）

| 工具 | 在哪里 | 用来做什么 |
|---|---|---|
| 假大模型 `FakeLLM` | `airesearcher/testing/fake_llm.py` | 测试里按提示名返回预先写好的回复，不花钱、结果固定 |
| 假执行器 `FakeExecutor` | `airesearcher/testing/fake_executor.py` | 不真的跑实验，提交后立刻得到格式正确的运行结果，可以指定某个运行失败 |
| 假检索源 `FakeLiterature` | `airesearcher/testing/fake_literature.py` | 不联网，从 `fixtures/literature_snapshot.json` 返回 14 篇真实论文的元数据 |
| 样例项目 | `fixtures/sample_project/`（说明见 [fixtures/README.md](../../fixtures/README.md)） | 一个完整跑完的项目：文献、计划、11 个运行、汇总、图、论文、故意写错的 2 条论断 |
| 契约检查 | `air dev check <项目目录> [--owner 文献/实验/论文]` | 检查你写出的交接文件是否符合约定格式 |
| 单步调试 | `air dev run-stage <阶段> --workspace <目录>` | 不启动后台，一步一步执行你的阶段，打印每一步的结果 |
| skill 验证 | `air skills verify <名字>`、`air skills list` | 新版 skill 用小样例验证通过后才启用（论文、实验同学会用到） |
| 模型配置 | `air models`、`air models test <名字>` | 查看和试调用你配置的大模型，见 [README 第 9 节](../../README.md#9-配置大模型和-key真实调用大模型时才需要) |

## 3. 规则

- **只改自己负责的目录。** 需要改共用的东西（`airesearcher/core/`、`engine/`、`llm/`、别人的阶段）时，先在群里说一声，PR 里请对方审。
- **交接文件的格式已经冻结**（详细设计 1 第 11 节）。确实要改时，先通知用它的人，并同时更新 `airesearcher/core/models/` 和详细设计。
- **调大模型时只写档位或角色名，不写具体模型名**：`ctx.llm.complete(role="idea.parse", prompt_id="idea/parse", tier="fast", ...)`。
  具体用哪个模型由每个人自己的 `~/air/models.yaml` 决定，换模型不用改代码。
- **API Key 只放在环境变量或 `~/air/.env`，绝不提交到 Git。**
- **阶段里不要 `time.sleep`、不要自己跳过审批**；需要等就返回 `Wait`，需要用户决定就 `Stop(question=...)`。
- **有同学用 Windows**：读写文本文件都写 `encoding="utf-8"`；路径用 `pathlib.Path`，不要手写 `/` 拼接；不要调用 `ls`、`cp` 这类 shell 命令。
  CI 会在 Windows、macOS、Linux 上各跑一遍测试。
- **每个 PR 合并前**：`pytest -q`、`ruff check .` 通过；改了交接文件的，`air dev check` 也要通过。

## 4. 遇到问题

- 先看 [README 第 7 节“常见问题”](../../README.md#7-常见问题)；
- 主干的接口（`StageContext`、`StepResult`、档案、审批……）见详细设计 1 第 3~5 节；
- 还是不清楚就在群里问组长，附上你运行的命令和完整的报错。
