from token_dashboard.pricing import Pricing, UsageBreakdown

M = 1_000_000


def test_anthropic_cache_tiers_differ_from_input(pricing: Pricing):
    r = pricing.rate("anthropic", "claude-opus-4-8")
    assert r.input == 5.0
    assert r.output == 25.0
    # Cache read is ~0.1x input; writes are 1.25x (5m) and 2x (1h).
    assert r.cache_read == 0.5
    assert r.cache_write_5m == 6.25
    assert r.cache_write_1h == 10.0


def test_cost_bills_each_class_at_its_own_rate(pricing: Pricing):
    # 1M of each class for Opus 4.8.
    cost = pricing.cost(
        "anthropic",
        "claude-opus-4-8",
        UsageBreakdown(
            input_tokens=M,
            output_tokens=M,
            cache_read_tokens=M,
            cache_create_5m=M,
            cache_create_1h=0,
        ),
    )
    # 5 + 25 + 0.5 + 6.25 = 36.75
    assert round(cost, 4) == 36.75


def test_cache_read_is_not_billed_at_input_rate(pricing: Pricing):
    # A cache-heavy request (typical of coding logs) must cost far less than if
    # cache reads were billed at the base input rate.
    u = UsageBreakdown(
        input_tokens=10_000, cache_read_tokens=2_000_000, output_tokens=5_000
    )
    correct = pricing.cost("anthropic", "claude-opus-4-8", u)
    naive = (
        10_000 + 2_000_000
    ) * 5.0 / M + 5_000 * 25.0 / M  # if cache read == input rate
    assert correct < naive / 5  # order-of-magnitude cheaper


def test_openai_uses_cached_input_rate(pricing: Pricing):
    r = pricing.rate("openai", "gpt-5.3-codex")
    assert r.input == 1.75
    assert r.cache_read == 0.175
    assert r.output == 14.0


def test_unknown_model_falls_back_to_provider_default(pricing: Pricing):
    r = pricing.rate("anthropic", "claude-something-new")
    assert r.input == 5.0  # default, not zero


def test_prefix_match(pricing: Pricing):
    r = pricing.rate("openai", "gpt-5.4-mini-2026-01-01")
    assert r.input == 0.75  # matched gpt-5.4-mini
