"""全量微调与 LoRA 共用加载、训练、预测流程。"""

from .neural import run_neural


def run(train, evaluation, config, out_dir):
    return run_neural(train, evaluation, config, out_dir, use_lora=False)
