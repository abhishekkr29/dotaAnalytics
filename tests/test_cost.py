from app import cost


def test_estimate_cents_minimum_one_when_any_tokens_used():
    """If usage is non-zero but the math rounds to zero, charge 1¢ as a floor."""
    tiny = cost.estimate_cents("claude-haiku-4-5", {"input_tokens": 100, "output_tokens": 10})
    assert tiny == 1


def test_estimate_cents_sonnet_typical_review():
    """A typical Sonnet 4.6 coach review (~2k in / 700 out) costs roughly 1.5¢ → 1¢ rounded."""
    cents = cost.estimate_cents(
        "claude-sonnet-4-6",
        {"input_tokens": 2000, "output_tokens": 700, "cache_read_input_tokens": 0},
    )
    # 2000 * 300 + 700 * 1500 = 600k + 1.05M = 1.65M micro-cents → 1 cent
    assert cents == 1


def test_estimate_cents_opus_pricier():
    """Opus 4.7 ~5x Sonnet. Same usage should bill noticeably more."""
    cents = cost.estimate_cents(
        "claude-opus-4-7",
        {"input_tokens": 2000, "output_tokens": 700},
    )
    # 2000 * 1500 + 700 * 7500 = 3M + 5.25M = 8.25M micro-cents → 8¢
    assert cents == 8


def test_estimate_cents_cache_read_discount():
    """Cache reads cost ~10x less than fresh input tokens. Use big counts to dodge the 1¢ floor."""
    fresh = cost.estimate_cents(
        "claude-sonnet-4-6", {"input_tokens": 1_000_000, "output_tokens": 0}
    )
    cached = cost.estimate_cents(
        "claude-sonnet-4-6",
        {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 1_000_000},
    )
    # fresh input rate is 300/Mtok, cache_read is 30/Mtok → fresh ≈ 10x cached
    assert fresh == 300 and cached == 30


def test_estimate_cents_unknown_model_returns_zero():
    assert cost.estimate_cents("not-a-real-model", {"input_tokens": 999}) == 0
