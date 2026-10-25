# AI Researcher Agent（DSA4213 第 6 组）

一个“AI 研究助理”：从一句研究想法出发，依次完成**选题 → 实验计划 → 跑实验 → 分析 → 写论文**，
每个关键节点都停下来等你审批。设计文档在 [`docs/`](docs/)，主干的详细设计见
[`docs/详细设计/1_Agent主干_详细设计.md`](docs/详细设计/1_Agent主干_详细设计.md)。

> **现在的进度：** 主干（流程引擎、档案、审批、后台、命令行）是真实代码；
> 文献、实验、论文三个阶段目前是**假实现**（不调用大模型、不联网），但生成的文件格式与设计文档完全一致，
> 所以整条流程已经可以从头跑到尾。各位组员之后用真实实现逐个替换即可（见第 5 节）。

---

## 1. 安装（只需做一次）

Mac 和 Windows 都可以。推荐用 **conda**（Anaconda 或 Miniconda）管理 Python，两个系统的命令完全一样。

- **Windows**：打开“Anaconda Prompt”（开始菜单里搜索），下面所有命令都在这里输入；
- **Mac**：打开“终端”。

先进入仓库目录（把路径换成你电脑上的位置），例如：

```bash
cd Desktop/ai-researcher-agent
```

创建一个名为 `air` 的 Python 环境（只需一次）：

```bash
conda create -n air python=3.12 -y
```

进入这个环境：

```bash
conda activate air
```

安装本项目：

```bash
pip install -e ".[dev]"
```

- 以后每次新开窗口，都要先运行一次 `conda activate air`（命令行前面会出现 `(air)`）。
- 不用 conda 也可以：任何 Python 3.11 以上的虚拟环境（如 `python -m venv .venv`）都行。
- 以后要真实调用大模型时，再运行 `pip install -e ".[llm]"` 并设置 `ANTHROPIC_API_KEY`。**用假实现跑流程不需要任何 API Key。**
- 有 MacTeX / TeX Live / MiKTeX（带 `latexmk`）时会真的编译出论文 PDF；没有也能跑完，只是生成一个占位 PDF。

## 2. 跑通一遍完整流程（约 3 分钟，不需要 API Key）

需要**两个终端窗口**（Windows 上是两个 Anaconda Prompt），两个都先运行 `conda activate air`。

**终端 1：启动后台**（保持这个窗口开着，不要关）

```bash
air serve
```

看到“AI Researcher 后台启动：http://127.0.0.1:8765”就成功了。

**终端 2：一步一步操作**

① 新建项目（用一句话写你的研究想法）：

```bash
air new --idea "比较三种方法在冒烟任务上的得分差异" --task tasks/smoke
```

② 看看现在在做什么、需要你做什么：

```bash
air status
```

`air status` 每次都会告诉你三件事：**现在的状态**（中文）、**需要你处理什么**、**接下来可以输入的命令**。
不确定下一步该做什么时，就输入 `air status`。

③ 第 1 次审批：选题（idea）。先看内容，再批准：

```bash
air show ap-0001
```

```bash
air approve ap-0001
```

④ 第 2 次审批：实验计划。系统会自动写好计划，用 `--wait` 等它写完：

```bash
air status --wait
```

```bash
air approve ap-0002
```

⑤ 批准计划后，系统开始**真的运行** 9 个小实验（每个约 2 秒）。等它们跑完、写好过程日志：

```bash
air status --wait
```

想看运行情况可以输入 `air runs`；看某个运行的输出：`air logs <运行编号>`。

⑥ 第 3 次审批：过程日志（实验结果的总结）：

```bash
air show ap-0003
```

```bash
air approve ap-0003
```

⑦ 第 4 次审批：最终手稿（论文）。等论文写好：

```bash
air status --wait
```

```bash
air show ap-0004
```

```bash
air approve ap-0004
```

⑧ 完成！

```bash
air status
```

会显示“已完成”，并给出论文 PDF 的位置。

> **审批编号会变：** 上面的 `ap-0001` ~ `ap-0004` 是“每次都直接批准”时的编号。如果你中途用了
> `air revise`（退回修改），系统会生成新版本、新的审批编号。**永远以 `air status` 显示的编号为准。**

### 2.1 试试“退回修改”和“拒绝”

```bash
air revise ap-0001 -m "研究问题再具体一点"
```

系统会按你的意见生成新版本（文件里能看到“修订说明”），再次请你审批。
`air reject ap-xxxx -m "理由"` 是拒绝：系统会问你“重写还是结束项目”，用 `air answer redo` 或 `air answer end` 回答。

### 2.2 试试“系统向你提问”

建项目时故意让一个实验运行失败：

```bash
air new --idea "测试失败时的提问" --task tasks/smoke --smoke-fail E2-seed=1:exit1
```

批准 idea 和计划后，`air status --wait` 会停在“需要你回答问题”。查看问题和选项：

```bash
air question
```

选择“跳过这个组合，继续其他运行”：

```bash
air answer skip
```

（也可以 `air answer retry` 重试，或 `air answer end` 结束项目。）

### 2.3 用网页做同样的事

上面每一步都可以在网页里点按钮完成（和命令行共用同一个后台，两边看到的状态一样）。
第一次需要 Node.js，在 `web/` 目录运行一次 `npm install` 和 `npm run build`，然后重启 `air serve`，
浏览器打开 <http://127.0.0.1:8765/>。详见 [web/README.md](web/README.md)。

## 3. 命令一览

| 命令 | 作用 |
|---|---|
| `air serve` | 启动后台（一直开着） |
| `air new --idea "..." --task tasks/smoke` | 新建项目并开始 |
| `air list` | 列出所有项目 |
| `air status [项目编号]` | 当前状态、需要你做什么、下一步命令；加 `--wait` 会一直等到需要你处理 |
| `air approvals` / `air show <审批编号>` | 待审批列表 / 查看待审内容 |
| `air approve <审批编号> [-m 意见]` | 批准 |
| `air revise <审批编号> -m 意见` | 退回修改 |
| `air reject <审批编号> -m 理由` | 拒绝 |
| `air question` / `air answer <选项> [-m 文字]` | 查看 / 回答系统的问题 |
| `air pause` / `air resume` / `air cancel` / `air reopen` | 暂停 / 继续 / 取消 / 重新打开已完成的项目 |
| `air runs` / `air logs <运行编号> [-f]` | 查看实验运行 / 运行日志 |
| `air cancel-run <运行编号>` | 取消一个正在跑的运行 |
| `air label <运行编号> trusted\|suspicious\|invalid -m 原因` | 人工标注运行：无效的不计入汇总，可疑的计入但标出 |
| `air template upload <zip>` / `air template list` | 上传论文模板 / 查看已上传的模板 |
| `air export [--what paper\|archive] [-o 文件]` | 导出论文交付包或整个研究档案（zip，含 `ARCHIVE_INDEX.md`） |
| `air checkpoints` / `air rollback <检查点>` | 查看可回滚的检查点 / 回滚（项目正在工作时先 `air pause`） |
| `air eval run <配置>` | 批量运行评价轨迹，输出 `summary.csv`（见 [eval/README.md](eval/README.md)） |
| `air models` / `air models test <名字>` / `air models init` | 查看已配置的大模型 / 试调用一次 / 生成配置模板（见第 9 节） |
| `air dev new-workspace <目录>` / `air dev run-stage <阶段> --workspace <目录>` | 开发调试用（不经过后台，见第 5 节）；`new-workspace --from fixtures/sample_project --register` 让后台显示样例项目 |
| `air dev check <目录>` | 检查项目里的交接文件是否符合约定格式 |
| `air skills list` / `air skills verify <名字>` | 查看 skill 版本 / 用小样例验证新版本，通过后启用 |

- 大多数命令的“项目编号”可以省略，省略时用**最近创建的项目**。
- 每个命令都可以加 `--help` 查看说明，例如 `air new --help`。
- 浏览器打开 http://127.0.0.1:8765/docs 可以看到后台全部接口（GUI 同学对接用）。

## 4. 项目文件在哪里

所有项目放在 `~/air/projects/<项目编号>/`（可以用环境变量 `AIR_HOME` 改位置）。里面都是普通文件，可以直接打开看：

| 位置 | 内容 |
|---|---|
| `project.yaml` | 项目配置 |
| `idea/selected.md` | 选题结果（开头是给程序读的 YAML，后面是给人读的正文） |
| `plan/experiment_plan.md`、`plan/tasks.json` | 实验计划、展开后的运行清单 |
| `runs/<运行编号>/` | 每次运行的配置、日志、原始指标 `metrics.jsonl`、状态 `status.json` |
| `artifacts/aggregates/` | 汇总结果（均值、标准差、95% 区间） |
| `logs/round_1.md` | 过程日志 |
| `paper/` | 论文：`main.tex`、`sections/`、`claims.jsonl`、`build/main.pdf`、`review/` 核验报告 |
| `approvals/`、`questions/` | 审批记录、问题与回答 |
| `research_log.jsonl` | 事件日志（发生过的每件事） |
| `.archive/` | 所有产物的历史版本（只增不删） |

## 5. 给组员：用真实实现替换假实现

**先看各角色的开工指南**：[docs/开工指南/](docs/开工指南/README.md)（文献、实验、论文、GUI 各一页：从哪里开始、主干已经准备了什么、测试怎么写）。

| 阶段 | 假实现（现在） | 真实实现放这里 | 负责人 |
|---|---|---|---|
| 选题 idea | `airesearcher/stages/fakes/idea.py` | `airesearcher/stages/idea/stage.py` | 文献 |
| 实验计划 plan | `airesearcher/stages/fakes/plan.py` | `airesearcher/stages/plan/stage.py` | 实验 |
| 执行与分析 experiment | `airesearcher/stages/fakes/experiment.py` | `airesearcher/stages/experiment/stage.py` | 实验 |
| 论文 writing | `airesearcher/stages/fakes/writing.py` | `airesearcher/stages/writing/stage.py` | 论文 |

1. **先读示例阶段**：[`airesearcher/stages/example/README.md`](airesearcher/stages/example/README.md)，几十行代码演示了写阶段需要的全部写法。
2. 新建你的 `stage.py`，提供一个 `create_stage()` 函数，返回你的阶段对象（有 `name` 和 `step(ctx)`）。
3. 让项目用你的实现：建项目时加 `--impl idea=real`（其他阶段仍用假实现），或者设置环境变量
   `AIR_STAGE_IMPL=idea=real`，或者改 `project.yaml` 里的 `dev.stage_impl`。
4. 你的阶段**写出的文件格式必须和假实现一样**（都在设计文档里冻结了，`airesearcher/core/models/` 里有对应的数据模型）。
   假实现就是“格式样例”：不确定时，跑一遍流程，打开假实现生成的文件对照即可。
5. 不启动后台、只调试自己的阶段：

```bash
air dev new-workspace /tmp/ws --impl idea=real
```

```bash
air dev run-stage idea --workspace /tmp/ws --steps 5
```

每一步会打印阶段返回了什么（`Continue` / `NeedsApproval` / ……）。遇到审批可以加 `--auto-approve` 自动批准。

6. **自查格式**：检查你的阶段写出的文件是否符合约定（“契约”）：

```bash
air dev check /tmp/ws
```

全部显示 ✓ 才算对接得上。只看自己负责的部分可以加 `--owner 文献`（或 `实验`、`论文`）。

7. **样例项目** `fixtures/sample_project/` 是一个完整跑完的项目（含失败运行、试运行、故意写错的论断等），
   开发时可以直接拿它当输入，说明见 [`fixtures/README.md`](fixtures/README.md)。

## 6. 哪些是真的，哪些是假的

| 部分 | 状态 |
|---|---|
| 流程引擎（状态机、检查点、重启恢复）、档案（版本、血缘、人工编辑检测）、审批、提问、预算、权限、事件日志 | **真实** |
| 后台 API（FastAPI）、命令行 `air` | **真实**（SSE 每秒检查一次变化后推送） |
| LLM 网关：`complete()`、工具调用循环 `tool_loop()`、按阶段选模型（提示文件、格式校验与重试、记账、调用日志、录制/回放） | **真实**；已用假模型测试，真实模型测试见第 9 节 |
| 本地执行器 + 运行包装器、冒烟任务 `tasks/smoke` | **真实但精简**（没有环境 pip freeze、数据校验、GPU 串行），实验同学接手补全 |
| 汇总统计 `services/runs.py`、引用核验 `services/literature.py` | **真实** |
| 证据核验 `services/evidence.py` | **部分**：只做了数字重算、过期证据、结论相反、引用不存在四项检查 |
| 文献检索、选题、实验计划、编码 Agent、分析决策、论文写作 | **假实现**（固定内容，不调用大模型） |
| 测试替身 `FakeLLM`、`FakeExecutor`、`FakeLiterature`；skill 版本登记与 `air skills verify` | **真实**（给组员写测试用） |
| SQLite 索引 | **没做**：目前直接读文件（数据量小，够用） |
| GUI（`web/`，Vue 3 + Element Plus） | **真实**：P1~P14 全部页面、SSE 实时更新、审批防重复与过期处理；用法见 [web/README.md](web/README.md) |

## 7. 常见问题

- **“连不上后台”**：终端 1 的 `air serve` 没开，或者被关掉了。重新运行 `air serve`。
- **端口被占用**：`air serve --port 8766`，然后在终端 2 先告诉命令行新地址：Mac 上运行 `export AIR_SERVER=http://127.0.0.1:8766`，Windows（Anaconda Prompt）上运行 `set AIR_SERVER=http://127.0.0.1:8766`。
- **关掉后台再打开会怎样？** 项目从上次的位置继续；正在跑的实验不受影响（它们是独立进程），重启后会自动核对状态。
- **`air: command not found` / “air 不是内部或外部命令”**：先 `conda activate air`。
- **Windows 上中文显示成乱码**：用 Windows Terminal 或 Anaconda Prompt；文件本身都是 UTF-8 编码，用 VS Code 打开即可正常显示。

## 8. 开发者

```bash
pytest -q
```

```bash
ruff check .
```

```bash
lint-imports
```

`lint-imports` 检查模块之间的依赖规则（例如各阶段之间不能互相导入，见详细设计 1 第 2 节）。

**自动检查（CI）：** 每个 PR 都会在 GitHub 上自动跑上面三项检查，并且在 Mac、Windows、Linux 三个系统上各跑一遍测试。
结果显示在 PR 页面底部：全部绿色 ✓ 才能合并；有红色 ✗ 时点进去看哪一项失败，修好后再推送，检查会自动重跑。

**契约测试** 在 `tests/contracts/`，说明见那里的 README：每位组员负责自己提供的交接文件格式。

测试包括：从创建到完成的端到端流程（引擎、HTTP、CLI 三种方式）、审批去重与过期（409）、提问与回答、
审批待处理时重启、运行进行中重启、后台停机期间运行结束、档案版本（含 `paper/` 目录）、引擎转换表。

## 9. 配置大模型和 Key（真实调用大模型时才需要）

用假实现跑流程**不需要**这一节。等你的阶段要真正调用大模型时：

**① 生成配置模板**（只需一次）：

```bash
air models init
```

它会在 `~/air/`（Windows 上是 `C:\Users\你的用户名\air\`）生成两个文件：

| 文件 | 作用 |
|---|---|
| `models.yaml` | 登记你能用的模型，并指定每个阶段用哪个 |
| `.env` | 存放 API Key。**只在你的电脑上，不要提交到 git，不要发给别人** |

**② 填 Key**：用记事本 / VS Code 打开 `~/air/.env`，去掉行首的 `#`，填上自己的 Key，例如 `ANTHROPIC_API_KEY=sk-ant-...`。

**③ 看看配置是否生效**（需要后台在运行）：

```bash
air models
```

会列出每个模型、Key 是否已设置（只显示 ✓/✗，不显示 Key 本身）、以及每个阶段实际会用哪个模型。

**④ 试调用一次**（费用不到 1 美分）：

```bash
air models test claude-fast
```

**换模型、给某个阶段单独指定模型**：改 `~/air/models.yaml`，保存后立即生效，不用重启后台。例如让文献阶段的大批量阅读用另一个模型：

```yaml
stages:
  idea: {fast: gpt-mini}
```

- `fast` / `strong` 是“档位”：`fast` 用于大批量、便宜的调用，`strong` 用于写作、编码、核验。阶段代码里只写档位，不写具体模型名，所以换模型不用改代码。
- 支持 Anthropic、OpenAI、本地模型（Ollama）、**第三方中转站**等，写法见 `configs/models.yaml` 里的说明。
  中转站一般是 OpenAI 兼容接口，这样写：

  ```yaml
  models:
    relay-gpt: {model: openai/gpt-6.1-sol, api_base: "https://中转站域名/v1", key_env: RELAY_API_KEY, price: [1.0, 4.0]}
  ```

  `model` 必须是“供应商/模型名”的形式（中转站写 `openai/`；只写模型名但填了 `api_base` 时会自动补上 `openai/`）；
  `price` 是中转站的价格（美元 / 百万 token），用于预算记账，不写时按偏高的默认价估算。
  中转站用 Claude 也写 `openai/`（如 `openai/claude-haiku-5-5`）；写成 `anthropic/…` 会按 Anthropic 官方格式发请求，中转站通常返回 404。
- **OpenRouter**：模型名照 OpenRouter 网站上的写（如 `anthropic/claude-haiku-5.5`），地址填 OpenRouter，会自动走 OpenRouter 通道：

  ```yaml
  models:
    claude-fast: {model: anthropic/claude-haiku-5.5, api_base: "https://openrouter.ai/api/v1", key_env: OPENROUTER_API_KEY}
  ```
- 你登记的模型的接口域名会被自动允许访问，不需要改项目的网络白名单。

**开发者：真实模型测试**（会产生几美分费用，CI 中不运行）：

```bash
AIR_REAL_LLM=1 pytest -q tests/test_llm_real.py
```

Windows 上先运行 `set AIR_REAL_LLM=1`，再运行 `pytest -q tests/test_llm_real.py`。测哪些模型由 `AIR_REAL_MODELS` 决定（默认 `claude-fast,gpt-mini`），缺 Key 的会自动跳过。
