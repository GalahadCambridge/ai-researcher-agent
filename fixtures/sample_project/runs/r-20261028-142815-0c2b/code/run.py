"""冒烟任务脚本：只用标准库。

配置（config.yaml，JSON 格式）：
  method     baseline | main | ablation   决定指标的大致水平
  seed       随机种子                      同一 (method, seed) 结果完全相同
  seconds    运行多少秒（默认 60）
  fail_mode  none | exit1 | hang | no_metrics
"""

import argparse
import json
import random
import sys
import time

from air_report import metric

LEVEL = {"baseline": 0.70, "main": 0.78, "ablation": 0.74}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--trial", action="store_true")
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    method = cfg.get("method", "baseline")
    seed = int(cfg.get("seed", 0))
    seconds = float(cfg.get("seconds", 60))
    if args.trial:
        seconds = min(seconds, 1.0)
    fail_mode = cfg.get("fail_mode", "none")
    print(f"smoke: method={method} seed={seed} seconds={seconds} fail_mode={fail_mode}", flush=True)

    t0 = time.time()
    steps = max(1, int(seconds / 0.5))
    for i in range(steps):
        time.sleep(seconds / steps)
        print(f"step {i + 1}/{steps}", flush=True)
        if fail_mode == "exit1" and i + 1 >= steps // 2:
            print("模拟错误：RuntimeError('smoke failure')", file=sys.stderr, flush=True)
            sys.exit(1)
    if fail_mode == "hang":
        while True:
            time.sleep(1)
    if fail_mode == "no_metrics":
        return
    rnd = random.Random(f"{method}-{seed}")
    score = LEVEL.get(method, 0.6) + rnd.uniform(-0.02, 0.02)
    metric("score", round(score, 4), split="test")
    metric("run_seconds", round(time.time() - t0, 3))


if __name__ == "__main__":
    main()
