import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import xgboost as xgb
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.model_selection import GroupShuffleSplit

from app import config, db

_DOC_PATH = Path(__file__).parent.parent / "docs" / "TRAINING.md"

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
    try:
        _refresh_doc(metrics)
    except Exception as e:
        print(f"warning: failed to refresh {_DOC_PATH.name}: {e}")
    return metrics


def refresh_doc_from_meta() -> dict:
    """Re-sync docs/TRAINING.md from the existing data/model_meta.json without retraining."""
    meta_path = config.DATA_DIR / "model_meta.json"
    if not meta_path.exists():
        raise SystemExit(
            "no data/model_meta.json — run `train` at least once before `refresh-doc`"
        )
    metrics = json.loads(meta_path.read_text())
    _refresh_doc(metrics)
    return {"doc": str(_DOC_PATH), "source": str(meta_path), "val_auc": metrics.get("val_auc")}


def _refresh_doc(m: dict) -> None:
    """Sync the AUTO blocks in docs/TRAINING.md with the latest training metrics.

    Best-effort: returns silently if the doc is missing (e.g., running outside the
    repo). Edits two HTML-marker regions:
      - <!-- AUTO:current-model:start --> ... :end -->  : the current snapshot table
      - <!-- AUTO:run-history:start --> ... :end -->    : append a row per run
    """
    if not _DOC_PATH.exists():
        return
    text = _DOC_PATH.read_text()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    current = (
        "| Field | Value |\n"
        "|---|---|\n"
        f"| Trained on | **{today}** |\n"
        f"| `n_matches` | {m['n_matches']} |\n"
        f"| `n_rows` (snapshots) | {m['n_rows']:,} |\n"
        f"| `n_train_rows` / `n_val_rows` | {m['n_train_rows']:,} / {m['n_val_rows']:,} (80/20 group split) |\n"
        f"| `val_log_loss` | {m['val_log_loss']:.4f} |\n"
        f"| **`val_auc`** | **{m['val_auc']:.4f}** |\n"
        f"| `best_iteration` | {m['best_iteration']} |\n"
        f"| Feature count | {len(m['feature_cols'])} |\n"
        "| Artifact path | `data/turbo_winprob.json` |\n"
    )
    text = re.sub(
        r"(<!-- AUTO:current-model:start -->\n).*?(<!-- AUTO:current-model:end -->)",
        lambda mt: mt.group(1) + current + mt.group(2),
        text,
        flags=re.DOTALL,
    )

    history_re = re.compile(
        r"(<!-- AUTO:run-history:start -->\n)(.*?)(<!-- AUTO:run-history:end -->)",
        flags=re.DOTALL,
    )
    new_row = (
        f"| {today} | {m['n_matches']} | {m['n_rows']:,} | "
        f"{m['val_auc']:.4f} | {m['val_log_loss']:.4f} | {m['best_iteration']} |\n"
    )
    mt = history_re.search(text)
    if mt:
        history = mt.group(2)
        # Dedup against the last data row in the block
        last_data = next(
            (line for line in reversed(history.strip().split("\n"))
             if line.startswith("|") and not line.startswith("|---") and "Date" not in line),
            "",
        )
        if last_data.strip() != new_row.strip():
            history = history.rstrip("\n") + "\n" + new_row
            text = text[:mt.start(2)] + history + text[mt.end(2):]

    _DOC_PATH.write_text(text)
