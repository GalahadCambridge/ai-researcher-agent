"""受保护的评价函数：只从真实标签和预测计算指标。"""


def classification_metrics(labels, predictions):
    from sklearn.metrics import accuracy_score, f1_score

    if len(labels) != len(predictions) or not labels:
        raise ValueError("预测数必须与非空评价集的标签数一致")
    if any(value not in (0, 1) for value in predictions):
        raise ValueError("预测标签必须为 0 或 1")
    return {
        "accuracy": float(accuracy_score(labels, predictions)),
        "macro_f1": float(f1_score(labels, predictions, labels=[0, 1], average="macro", zero_division=0)),
    }
