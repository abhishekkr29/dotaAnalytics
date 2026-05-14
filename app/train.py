import json

import numpy as np
import xgboost as xgb
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.model_selection import GroupShuffleSplit

from app import config, db

FEATURE_COLS = [
    "minute", "gold_adv", "xp_adv",
    "tower_kills_radiant", "tower_kills_dire",
    "kills_radiant", "kills_dire", "roshan_kills",
    "avg_rank_tier",
    "r_hero_1", "r_hero_2", "r_hero_3", "r_hero_4", "r_hero_5",
    "d_hero_1", "d_hero_2", "d_hero_3", "d_hero_4", "d_hero_5",
]


def _load() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    select = ", ".join(["match_id", *FEATURE_COLS, "radiant_win"])
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {select} FROM snapshots "
            "WHERE avg_rank_tier IS NOT NULL AND r_hero_1 IS NOT NULL"
        ).fetchall()
    if not rows:
        raise SystemExit("no snapshot rows yet — run snapshots first")
    arr = np.array(rows, dtype=object)
    return (
        arr[:, 0].astype(np.int64),
        arr[:, 1:-1].astype(np.float32),
        arr[:, -1].astype(np.int32),
    )


def train(n_estimators: int = 400) -> dict:
    match_ids, X, y = _load()
    n_matches = int(np.unique(match_ids).size)
    if n_matches < 20:
        raise SystemExit(
            f"only {n_matches} parsed matches available — too few to train a useful model"
        )

    splitter = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
    train_idx, val_idx = next(splitter.split(X, y, groups=match_ids))

    model = xgb.XGBClassifier(
        n_estimators=n_estimators,
        max_depth=6,
        learning_rate=0.05,
        eval_metric="logloss",
        tree_method="hist",
        early_stopping_rounds=20,
    )
    model.fit(
        X[train_idx], y[train_idx],
        eval_set=[(X[val_idx], y[val_idx])],
        verbose=False,
    )

    val_pred = model.predict_proba(X[val_idx])[:, 1]
    metrics = {
        "n_rows": int(len(X)),
        "n_matches": n_matches,
        "n_train_rows": int(len(train_idx)),
        "n_val_rows": int(len(val_idx)),
        "val_log_loss": float(log_loss(y[val_idx], val_pred)),
        "val_auc": float(roc_auc_score(y[val_idx], val_pred)),
        "best_iteration": int(getattr(model, "best_iteration", -1)),
        "feature_cols": FEATURE_COLS,
    }
    model.save_model(str(config.MODEL_PATH))
    (config.DATA_DIR / "model_meta.json").write_text(json.dumps(metrics, indent=2))
    return metrics
