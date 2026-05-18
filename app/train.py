import json
import re
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import xgboost as xgb
from sklearn.isotonic import IsotonicRegression
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

# Per-bracket calibrators need at least this many val-fold samples to fit reliably.
_CALIBRATION_MIN_SAMPLES = 50


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


def _fit_calibrators(val_X: np.ndarray, val_y: np.ndarray, val_pred: np.ndarray) -> dict:
    """Fit one isotonic regression per rank bracket. Skip undersized brackets."""
    rank_col = FEATURE_COLS.index("avg_rank_tier")
    val_ranks = val_X[:, rank_col].astype(int) // 10
    cals: dict = {}
    bucket_stats = []
    for bucket in sorted(set(val_ranks)):
        mask = val_ranks == bucket
        n = int(mask.sum())
        if n < _CALIBRATION_MIN_SAMPLES:
            bucket_stats.append({"bucket": int(bucket), "n": n, "fitted": False})
            continue
        cals[int(bucket)] = IsotonicRegression(out_of_bounds="clip").fit(val_pred[mask], val_y[mask])
        bucket_stats.append({"bucket": int(bucket), "n": n, "fitted": True})
    return cals, bucket_stats


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

    # Per-bracket isotonic calibration on val-fold predictions.
    calibrators, bucket_stats = _fit_calibrators(X[val_idx], y[val_idx], val_pred)
    cal_path = config.DATA_DIR / "calibrators.joblib"
    if calibrators:
        joblib.dump(calibrators, cal_path)
    elif cal_path.exists():
        cal_path.unlink()

    # Effective val_auc after calibration (per-bucket transform, then concat).
    calibrated_pred = val_pred.copy()
    rank_col = FEATURE_COLS.index("avg_rank_tier")
    val_buckets = X[val_idx, rank_col].astype(int) // 10
    for bucket, cal in calibrators.items():
        m = val_buckets == bucket
        if m.any():
            calibrated_pred[m] = cal.transform(val_pred[m])

    metrics = {
        "n_rows": int(len(X)),
        "n_matches": n_matches,
        "n_train_rows": int(len(train_idx)),
        "n_val_rows": int(len(val_idx)),
        "val_log_loss": float(log_loss(y[val_idx], val_pred)),
        "val_auc": float(roc_auc_score(y[val_idx], val_pred)),
        "val_log_loss_calibrated": float(log_loss(y[val_idx], calibrated_pred)),
        "val_auc_calibrated": float(roc_auc_score(y[val_idx], calibrated_pred)),
        "best_iteration": int(getattr(model, "best_iteration", -1)),
        "feature_cols": FEATURE_COLS,
        "calibration": {
            "brackets_fitted": list(sorted(calibrators.keys())),
            "min_samples_threshold": _CALIBRATION_MIN_SAMPLES,
            "bucket_stats": bucket_stats,
        },
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "parsed_match_count_at_train": n_matches,
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


def parsed_count_since_train() -> dict:
    """How many new parsed matches have accumulated since the last `train` invocation."""
    meta_path = config.DATA_DIR / "model_meta.json"
    if not meta_path.exists():
        return {"trained": False, "delta": None, "current_parsed": _current_parsed_count()}
    meta = json.loads(meta_path.read_text())
    at_train = meta.get("parsed_match_count_at_train") or meta.get("n_matches", 0)
    current = _current_parsed_count()
    return {
        "trained": True,
        "trained_at": meta.get("trained_at"),
        "parsed_at_train": at_train,
        "current_parsed": current,
        "delta": current - at_train,
    }


def _current_parsed_count() -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM matches WHERE parsed").fetchone()[0])


def _refresh_doc(m: dict) -> None:
    if not _DOC_PATH.exists():
        return
    text = _DOC_PATH.read_text()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cal_brackets = m.get("calibration", {}).get("brackets_fitted") or []

    current = (
        "| Field | Value |\n"
        "|---|---|\n"
        f"| Trained on | **{today}** |\n"
        f"| `n_matches` | {m['n_matches']} |\n"
        f"| `n_rows` (snapshots) | {m['n_rows']:,} |\n"
        f"| `n_train_rows` / `n_val_rows` | {m['n_train_rows']:,} / {m['n_val_rows']:,} (80/20 group split) |\n"
        f"| `val_log_loss` | {m['val_log_loss']:.4f} |\n"
        f"| **`val_auc`** | **{m['val_auc']:.4f}** |\n"
        f"| `val_auc_calibrated` | {m.get('val_auc_calibrated', m['val_auc']):.4f} |\n"
        f"| Calibrated brackets | {cal_brackets or '—'} |\n"
        f"| `best_iteration` | {m['best_iteration']} |\n"
        f"| Feature count | {len(m['feature_cols'])} |\n"
        "| Artifact path | `data/turbo_winprob.json` + `data/calibrators.joblib` |\n"
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
        last_data = next(
            (line for line in reversed(history.strip().split("\n"))
             if line.startswith("|") and not line.startswith("|---") and "Date" not in line),
            "",
        )
        if last_data.strip() != new_row.strip():
            history = history.rstrip("\n") + "\n" + new_row
            text = text[:mt.start(2)] + history + text[mt.end(2):]

    _DOC_PATH.write_text(text)
