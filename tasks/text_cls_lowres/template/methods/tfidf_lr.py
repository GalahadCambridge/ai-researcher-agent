"""特征和分类器只在训练子集拟合；评价集只 transform/predict。"""

import time


def run(train, evaluation, config, out_dir):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline

    model = make_pipeline(
        TfidfVectorizer(ngram_range=(1, 2), max_features=config["max_features"]),
        LogisticRegression(C=config["classifier_c"], max_iter=1000, random_state=config["seed"]),
    )
    start = time.perf_counter()
    model.fit([r["text"] for r in train], [r["label"] for r in train])
    train_seconds = time.perf_counter() - start
    start = time.perf_counter()
    predictions = model.predict([r["text"] for r in evaluation]).tolist()
    eval_seconds = time.perf_counter() - start
    if config["save_model"]:
        import joblib

        joblib.dump(model, out_dir / "model.joblib")
    return predictions, {"train_seconds": train_seconds, "eval_seconds": eval_seconds, "device": "cpu"}
