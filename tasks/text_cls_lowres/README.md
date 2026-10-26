# 第一步：低资源文本分类实验模板

本目录实现项目计划指定的第一步：

- docs/开工指南/实验.md「先做这几件事」第 1 项：创建任务、手写三种方法的模板。
- docs/详细设计/3_实验计划与执行_详细设计.md 第 3.1 节：SST-2、TF-IDF、全量微调、LoRA。
- 同文档第 3.2–3.3 节：task.yaml、统一入口、受保护数据/评价模块。
- 同文档第 3.5、5.3 节：准备数据/模型，训练时离线，使用 @@AIR_METRIC 协议。

目前交付手写模板和准备脚本。计划 agent、编码修复 agent、自动审批流程和九次运行的调度在后续步骤接入。
直接运行模板时结果保存在指定的 --out 目录；标准 runs/<id>/ 的状态文件和原始 metrics.jsonl 以后由现有 LocalExecutor/wrapper 创建。
task.yaml 的 protected_files、semantic_keys 是供后续 agent 读取的约束声明；本步还没有实现编码工具的强制保护。

## 1. 依赖

先进入你已经配置好的项目环境，再进入仓库根目录：

    cd /home/plj/ai-researcher-agent

若已经安装这些实验依赖，不必重复安装。缺依赖时可运行：

    python -m pip install -r tasks/text_cls_lowres/template/requirements.txt

该文件只定义兼容范围，不是已验证的锁文件。请保留原先可用的 CUDA PyTorch；不要为此切换到 CPU 版。
本次只生成代码，未执行安装、下载、训练或测试；当前安装组合需要由首次试运行确认。
公开 Hugging Face 数据和模型下载不需要项目的 LLM API Key。

## 2. 一次性准备

以下命令使用项目默认缓存位置，若已有 AIR_HOME 则保留它：

    export AIR_HOME="${AIR_HOME:-$HOME/air}"
    export AIR_DATA_DIR="$AIR_HOME/data/sst2"
    export HF_HOME="$AIR_HOME/data/hf_cache"
    python tasks/text_cls_lowres/prepare_data.py --out "$AIR_DATA_DIR"
    python tasks/text_cls_lowres/prepare_model.py --out "$AIR_DATA_DIR/model_manifest.json"

两条准备命令会联网。数据脚本解析并记录数据集 commit，模型脚本记录模型 commit 和各文件哈希；
再次运行会复用并检查已有文件，不覆盖已有数据或改变划分。
模型缓存使用预训练 distilbert/distilbert-base-uncased，不使用已在 SST-2 上微调的分类模型。
只有 prepare 脚本联网，run.py 强制离线并使用 local_files_only。

只检查数据、不联网：

    python tasks/text_cls_lowres/prepare_data.py --out "$AIR_DATA_DIR" --check

本地 train/dev 从官方 train 划分；dev 固定为 1000 条，split_seed=42；
官方 validation 的 872 条保留为本地 test。官方无公开标签的 test 不用于计算准确率。
脚本排除训练池与 final test 之间规范化后精确重复的文本，去除训练池重复和冲突标签后再划分。
此检查只覆盖精确文本重复，不能证明无语义、片段或电影来源层面的重叠。
SST-2 数据卡当前将许可列为 unknown，task.yaml 如实保留待核实提示。

## 3. 先做三个短试运行

同一个终端保留上面的环境变量。每个 --out 必须为空；再次尝试请换目录名，例如 trial02。

    python tasks/text_cls_lowres/template/run.py --config tasks/text_cls_lowres/configs/tfidf_lr.yaml --out "$AIR_HOME/manual_runs/text_cls_lowres/tfidf_trial01" --trial
    python tasks/text_cls_lowres/template/run.py --config tasks/text_cls_lowres/configs/full_ft.yaml --out "$AIR_HOME/manual_runs/text_cls_lowres/full_ft_trial01" --trial
    python tasks/text_cls_lowres/template/run.py --config tasks/text_cls_lowres/configs/lora.yaml --out "$AIR_HOME/manual_runs/text_cls_lowres/lora_trial01" --trial

试运行最多 50 条训练样本、1 个 epoch、64 条 dev 样本。
所有试运行都标 trial=true，只用于查错，不用于比较方法效果。
神经方法配置要求 CUDA；GPU 在当前解释器不可用时会明确报错，不会自动退回 CPU。
LoRA 的目标层是 DistilBERT 的 q_lin/v_lin，分类头 pre_classifier/classifier 保持可训练。
两个神经方法固定训练轮数，不用评价集分数选择 checkpoint。

## 4. 三个种子的手动正式运行

三个短试运行都成功后，可按开工指南第 3 项手动执行 3 方法 × 3 种子。
这是一轮 dev 开发验证，不能用来宣称 final test 性能：

    for method in tfidf_lr full_ft lora; do
      for seed in 0 1 2; do
        python tasks/text_cls_lowres/template/run.py \
          --config "tasks/text_cls_lowres/configs/$method.yaml" \
          --seed "$seed" \
          --out "$AIR_HOME/manual_runs/text_cls_lowres/dev01/${method}_seed${seed}" || break 2
      done
    done

默认训练规模 500。三种方法共用按 seed 生成的近似均衡训练子集；
同一 seed 下 train_ids.json 应完全相同；固定 dev/test 不随 seed 改变。
TF-IDF 在 CPU 上训练，两个神经方法按上述顺序使用 GPU。
全量微调学习率 3e-5、LoRA 学习率 3e-4 是预先指定的起始配置，没有做自动调参。
默认 batch_size=8、grad_accum_steps=2，完整组的等效批大小为 16。
OOM 时可以在神经方法的两份配置中同时把 batch_size 降为 4、grad_accum_steps 提高为 4；
保持比较设置一致，并使用新的输出目录。

只有冻结实验方案后，才在新输出目录显式添加 --eval-split test 进行最终评价。
最终 test 不能用于调参、选择训练轮数或决定是否继续尝试另一种方法。

## 5. 输出与成功标准

每次成功运行的 --out 目录包含：

- requested_config.json / effective_config.json：请求配置与实际配置（包含试运行缩减）。
- inputs.json / train_ids.json：数据版本、哈希、评价划分和实际训练样本 ID。
- environment.json：当前实验解释器、依赖版本、源代码哈希。
- predictions.jsonl：每条评价样本的 ID、真实标签和预测标签，可独立重算指标。
- result.json：accuracy、macro_f1、训练/评价耗时，以及神经方法的参数量、显存峰值等。
- failure.json：失败时保存的错误信息（成功时不生成）。
- model/ 或 model.joblib：只有配置 save_model=true 时才保存。

train_seconds 仅计算特征/分类器拟合或神经训练循环的时间，不含下载、模型加载和评价；
wall_seconds 记录本次入口总耗时（环境采集、数据与模型校验、加载、训练和评价等）。
epoch 日志写入 stdout；指标按 @@AIR_METRIC 输出，沿用仓库包装器协议，不手写原始 metrics.jsonl。
CUDA 运行可能存在非确定性；不能仅凭固定 seed 保证逐位一致。

第一步完成的标准：三种方法的短试运行成功，产生真实预测及三个必需指标，两个神经方法确实使用 CUDA。
方法效果不需要超过基线，也不应期待短试运行取得稳定分数。

退出码：2 为配置/入口文件/缺依赖错误，3 为数据或模型输入校验错误，1 为其余运行错误。
本步不会安装依赖、请求 LLM、自动修复或创建任何获批研究计划。

## 技术参考

- SST-2: https://huggingface.co/datasets/stanfordnlp/sst2
- Pretrained DistilBERT: https://huggingface.co/distilbert/distilbert-base-uncased
- PEFT LoRA: https://huggingface.co/docs/peft/en/package_reference/lora
- Hub revision / snapshot download: https://huggingface.co/docs/huggingface_hub/en/guides/download
