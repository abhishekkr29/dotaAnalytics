"""Per-user daily cost tracking for Anthropic API usage.

Server-key calls count against DAILY_COST_CAP_CENTS. BYO-key calls bypass the cap
but are still tracked for stats so users can see their own spend in the dashboard.
"""

from datetime import datetime, timezone

from app import config, db


class BudgetExceeded(Exception):
    pass


def current_usage(account_id: int) -> dict:
    """Read current daily/monthly usage. Treat the daily counter as 0 if it hasn't reset today."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT daily_cost_used_cents, daily_cost_reset_at, monthly_cost_used_cents "
            "FROM users WHERE account_id = %s",
            (account_id,),
        ).fetchone()

    if not row:
        return {"daily_cents": 0, "monthly_cents": 0, "cap_cents": config.DAILY_COST_CAP_CENTS}

    daily, reset_at, monthly = row
    today = datetime.now(timezone.utc).date()
    if reset_at is None or reset_at.date() < today:
        daily = 0

    return {
        "daily_cents": daily or 0,
        "monthly_cents": monthly or 0,
        "cap_cents": config.DAILY_COST_CAP_CENTS,
    }


def check_budget(account_id: int, use_byo_key: bool) -> None:
    """Raise BudgetExceeded if a server-key call would exceed the daily cap. BYO bypasses."""
    if use_byo_key:
        return
    usage = current_usage(account_id)
    if usage["daily_cents"] >= usage["cap_cents"]:
        raise BudgetExceeded(
            f"Daily server-key budget exhausted ({usage['daily_cents']}¢ of "
            f"{usage['cap_cents']}¢). Add your own ANTHROPIC_API_KEY in Settings "
            "to bypass, or wait until UTC midnight."
        )


def charge(account_id: int, cents: int, use_byo_key: bool) -> None:
    """Record a charge. Daily counter only tracks server-key spend (the capped one)."""
    server_charge = 0 if use_byo_key else cents
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO users (
                account_id, daily_cost_used_cents, daily_cost_reset_at,
                monthly_cost_used_cents, last_seen_at
            )
            VALUES (%s, %s, NOW(), %s, NOW())
            ON CONFLICT (account_id) DO UPDATE SET
                daily_cost_used_cents =
                    CASE WHEN users.daily_cost_reset_at IS NULL
                              OR users.daily_cost_reset_at::date < NOW()::date
                         THEN EXCLUDED.daily_cost_used_cents
                         ELSE users.daily_cost_used_cents + EXCLUDED.daily_cost_used_cents
                    END,
                daily_cost_reset_at = NOW(),
                monthly_cost_used_cents =
                    users.monthly_cost_used_cents + EXCLUDED.monthly_cost_used_cents,
                last_seen_at = NOW();
            """,
            (account_id, server_charge, cents),
        )


# Anthropic pricing as of 2026-05 (per million tokens). Used to convert token usage to cents.
PRICE_PER_MTOK = {
    "claude-haiku-4-5":   {"input": 100,  "output": 500,  "cache_read": 10},
    "claude-sonnet-4-6":  {"input": 300,  "output": 1500, "cache_read": 30},
    "claude-opus-4-7":    {"input": 1500, "output": 7500, "cache_read": 150},
}


def estimate_cents(model_id: str, usage: dict) -> int:
    """Convert Anthropic usage dict to cents. Returns at least 1 if any tokens were used."""
    rates = PRICE_PER_MTOK.get(model_id)
    if not rates:
        return 0
    in_tokens   = usage.get("input_tokens", 0)
    out_tokens  = usage.get("output_tokens", 0)
    cache_read  = usage.get("cache_read_input_tokens", 0)
    micro_cents = (
        in_tokens   * rates["input"]      +
        out_tokens  * rates["output"]     +
        cache_read  * rates["cache_read"]
    )
    # rates are in cents per 1M tokens → cents = micro_cents / 1_000_000
    cents = micro_cents // 1_000_000
    if cents == 0 and (in_tokens + out_tokens + cache_read) > 0:
        return 1
    return int(cents)
