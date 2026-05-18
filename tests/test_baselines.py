from app import baselines


def test_rank_bucket():
    assert baselines._rank_bucket(11) == 1   # Herald
    assert baselines._rank_bucket(43) == 4   # Crusader 3
    assert baselines._rank_bucket(77) == 7   # Divine 7
    assert baselines._rank_bucket(None) is None
    assert baselines._rank_bucket(0) is None


def test_lookup_returns_none_when_no_baseline():
    payload = {"samples": {}}
    assert baselines.lookup(payload, 43, 74, "black_king_bar") is None


def test_lookup_returns_baseline_when_present():
    payload = {
        "samples": {
            "4|74|black_king_bar": {
                "median_seconds": 840, "p25_seconds": 720, "p75_seconds": 1020, "n_samples": 84,
            }
        }
    }
    b = baselines.lookup(payload, 43, 74, "black_king_bar")
    assert b is not None
    assert b["median_seconds"] == 840
    assert b["n_samples"] == 84


def test_format_delta_late_vs_early():
    b = {"median_seconds": 840, "n_samples": 84, "p25_seconds": 720, "p75_seconds": 1020}
    late = baselines.format_delta(1050, b)  # 17:30 vs 14:00 → +3:30 late
    assert "late" in late
    assert "+03:30" in late or "+3:30" in late or "03:30" in late

    early = baselines.format_delta(720, b)  # 12:00 vs 14:00 → 2:00 early
    assert "early" in early

    on_time = baselines.format_delta(860, b)  # 14:20 → on-time (within 30s)
    assert "on-time" in on_time
