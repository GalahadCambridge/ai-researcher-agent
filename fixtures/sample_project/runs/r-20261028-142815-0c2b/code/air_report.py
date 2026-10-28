"""指标上报（详细设计 3 第 5.3 节）。不依赖本项目的任何包，复制到别处也能直接运行。

    from air_report import metric
    metric("accuracy", 0.873, split="test")
"""

import json
import sys


def metric(name, value, split=None, step=None):
    line = json.dumps({"name": name, "value": float(value), "split": split, "step": step})
    sys.stdout.write("@@AIR_METRIC " + line + "\n")
    sys.stdout.flush()
