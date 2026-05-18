"""One-shot XGBoost hyperparameter sweep for the win-prob model.

Runs a small grid over `max_depth × learning_rate × n_estimators` and prints
val_auc / val_log_loss per config. **Does not save artifacts** — the goal is to
pick the best config and then invoke the regular `train` command with it.

Usage:
    docker compose run --rm app python -m app.cli sweep
    docker compose run --rm app python -m app.cli sweep --out data/sweep_results.json
"""

import json
from itertools import product
from pathlib import Path

import numpy as np
import xgboost as xgb
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.model_selection import GroupShuffleSplit

from app import train as train_mod


_GRID = {
    "max_depth":     [4, 6, 8],
    "learning_rate": [0.03, 0.05, 0.1],
    "n_estimators":  [200, 400, 800],
}


def run(out_path: str | None = None) -> dict:
    match_ids, X, y = train_mod._load()
    n_matches = int(np.unique(match_ids).size)
    if n_matches < 20:
        return {"error": f"only {n_matches} parsed matches; need ≥20"}

    splitter = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    train_idx, val_idx = next(splitter.split(X, y, groups=match_ids))

    results: list[dict] = []
    configs = list(product(_GRID["max_depth"], _GRID["learning_rate"], _GRID["n_estimators"]))
    print(f"Running {len(configs)} configs on {n_matches} matches / {len(X):,} rows...")

    for md, lr, ne in configs:
        model = xgb.XGBClassifier(
            n_estimators=ne, max_depth=md, learning_rate=lr,
            eval_metric="logloss", tree_method="hist", early_stopping_rounds=20,
        )
        model.fit(
            X[train_idx], y[train_idx],
            eval_set=[(X[val_idx], y[val_idx])],
            verbose=False,
        )
        val_pred = model.predict_proba(X[val_idx])[:, 1]
        result = {
            "max_depth": md,
            "learning_rate": lr,
            "n_estimators": ne,
            "best_iteration": int(getattr(model, "best_iteration", -1)),
            "val_auc": float(roc_auc_score(y[val_idx], val_pred)),
            "val_log_loss": float(log_loss(y[val_idx], val_pred)),
        }
        results.append(result)
        print(
            f"  md={md} lr={lr} ne={ne:<4} → "
            f"val_auc {result['val_auc']:.4f} | "
            f"log_loss {result['val_log_loss']:.4f} | "
            f"best_iter {result['best_iteration']}"
        )

    results.sort(key=lambda r: r["val_auc"], reverse=True)
    best = results[0]
    out = {
        "n_matches": n_matches,
        "n_rows": int(len(X)),
        "best": best,
        "all": results,
    }

    if out_path:
        Path(out_path).write_text(json.dumps(out, indent=2))
        out["written_to"] = out_path

    print()
    print(f"Best:  md={best['max_depth']} lr={best['learning_rate']} "
          f"ne={best['n_estimators']} → val_auc {best['val_auc']:.4f}")
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
