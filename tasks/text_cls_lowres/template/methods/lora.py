"""训练 DistilBERT attention 的 LoRA 参数及分类头。"""

from .neural import run_neural


def run(train, evaluation, config, out_dir):
    return run_neural(train, evaluation, config, out_dir, use_lora=True)
