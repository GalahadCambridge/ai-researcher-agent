"""兼容详细设计 3 第 5.3 节；原始 metrics.jsonl 由已有 wrapper 写入。"""

import json
import math


def metric(name, value, split=None, step=None):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"指标 {name} 必须是有限数值")
    payload = {"name": name, "value": value, "split": split, "step": step}
    print("@@AIR_METRIC " + json.dumps(payload, allow_nan=False), flush=True)
