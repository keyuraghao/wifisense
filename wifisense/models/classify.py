"""Supervised activity classifier over windowed features."""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import GroupKFold, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ..signal.features import feature_columns


def build_model(n_estimators: int = 400, seed: int = 0) -> Pipeline:
    return Pipeline([
        ("scale", StandardScaler()),
        ("clf", RandomForestClassifier(
            n_estimators=n_estimators,
            min_samples_leaf=2,
            class_weight="balanced_subsample",
            n_jobs=-1,
            random_state=seed,
        )),
    ])


def grouped_cv_report(df: pd.DataFrame, group_col: str = "session",
                      n_splits: int | None = None, seed: int = 0) -> dict:
    """Cross-validate with sessions held out as whole groups.

    This is the single most important methodological choice in the project.
    Sliding windows overlap, so neighbouring windows share raw samples; a plain
    random split puts near-duplicate windows in train and test and reports
    ~99% accuracy that means nothing. Grouping by session forces the model to
    generalise across recordings.
    """
    feats = feature_columns(df)
    X = df[feats].to_numpy()
    y = df["label"].to_numpy()
    groups = df[group_col].to_numpy()

    n_groups = len(np.unique(groups))
    if n_groups < 2:
        raise ValueError(
            f"grouped CV needs >=2 sessions, found {n_groups}. "
            "Record each condition at least twice, on different occasions."
        )
    k = n_splits or min(5, n_groups)

    model = build_model(seed=seed)
    y_pred = cross_val_predict(model, X, y, groups=groups, cv=GroupKFold(n_splits=k))

    labels = sorted(np.unique(y))
    return {
        "n_windows": len(df),
        "n_sessions": int(n_groups),
        "n_splits": k,
        "labels": labels,
        "accuracy": float((y_pred == y).mean()),
        "report": classification_report(y, y_pred, zero_division=0),
        "confusion": pd.DataFrame(
            confusion_matrix(y, y_pred, labels=labels),
            index=[f"true_{l}" for l in labels],
            columns=[f"pred_{l}" for l in labels],
        ),
        "y_pred": y_pred,
    }


def fit_final(df: pd.DataFrame, seed: int = 0) -> tuple[Pipeline, list[str]]:
    feats = feature_columns(df)
    model = build_model(seed=seed)
    model.fit(df[feats].to_numpy(), df["label"].to_numpy())
    return model, feats


def feature_importance(model: Pipeline, feats: list[str], top: int = 15) -> pd.Series:
    imp = model.named_steps["clf"].feature_importances_
    return pd.Series(imp, index=feats).sort_values(ascending=False).head(top)


def save_model(model: Pipeline, feats: list[str], path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "features": feats}, path)


def load_model(path) -> tuple[Pipeline, list[str]]:
    d = joblib.load(path)
    return d["model"], d["features"]
