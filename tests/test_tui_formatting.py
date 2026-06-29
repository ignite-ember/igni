"""Tests for ``frontend/tui/widgets/_formatting``:

  * format_elapsed_time(seconds) — '0.8s', '3.2s', '1m 12s'
  * format_token_count(n) — '999', '1.2k', '12k', '1.0m', etc.

These render to the live tool-call header and run-stats widget;
boundary bugs are silent and visual ("123k for a 130k value").
The token formatter has 8 explicit thresholds — pin each
transition so a refactor can't slip the boundary by 1.
"""

from __future__ import annotations

from ember_code.frontend.tui.widgets._formatting import (
    format_elapsed_time,
    format_token_count,
)

# ── format_elapsed_time ─────────────────────────────────────


class TestFormatElapsedTime:
    def test_zero(self):
        # Initial tick before any time has elapsed. ``0.0s`` is
        # the right placeholder — better than empty.
        assert format_elapsed_time(0.0) == "0.0s"

    def test_sub_second(self):
        # Most quick tool calls return in <1s. Show one decimal
        # so the user can see "0.3s" vs "0.8s".
        assert format_elapsed_time(0.3) == "0.3s"
        assert format_elapsed_time(0.8) == "0.8s"

    def test_just_under_one_minute(self):
        # The transition boundary. 59.9s still in seconds form;
        # 60s flips to "1m 0s".
        assert format_elapsed_time(59.9) == "59.9s"

    def test_exactly_one_minute(self):
        # ``60`` → ``1m 0s``. The format change should land
        # exactly at the minute mark. A refactor that uses
        # ``> 60`` instead of ``< 60`` would leave 60 in the
        # seconds form ("60.0s") — pin against that drift.
        assert format_elapsed_time(60.0) == "1m 0s"

    def test_one_minute_thirty_seconds(self):
        # Mixed minute + second display. Both halves are
        # integer-truncated (no decimals on the second part).
        assert format_elapsed_time(90.0) == "1m 30s"
        assert format_elapsed_time(90.7) == "1m 30s"

    def test_just_under_one_hour(self):
        # 3599s → 59m 59s. Doesn't roll to hours; the formatter
        # caps at minutes-and-seconds.
        assert format_elapsed_time(3599.0) == "59m 59s"

    def test_one_hour_displays_as_60m(self):
        # The formatter doesn't have an hours branch — past 1h
        # it just says "60m 0s", "120m 30s", etc. Pin the
        # behaviour so a future refactor adding "1h 0m 0s"
        # surfaces as a deliberate choice.
        assert format_elapsed_time(3600.0) == "60m 0s"


# ── format_token_count ──────────────────────────────────────


class TestFormatTokenCountSmall:
    def test_zero(self):
        # Zero tokens — render as the integer "0". Some tools
        # complete without any token usage (e.g. cache hits).
        assert format_token_count(0) == "0"

    def test_under_1000_renders_verbatim(self):
        # Below the first threshold, show the exact count.
        # 1.2 chars are fine here; abbreviating doesn't help.
        assert format_token_count(1) == "1"
        assert format_token_count(42) == "42"
        assert format_token_count(999) == "999"

    def test_at_1000_flips_to_k_with_one_decimal(self):
        # 1000 is the first threshold. Below it we show int,
        # at-or-above we show "1.0k" / "1.5k" with a single
        # decimal. The threshold uses ``<`` not ``<=``, so
        # 1000 ends up in the next branch.
        assert format_token_count(1000) == "1.0k"
        assert format_token_count(1500) == "1.5k"


class TestFormatTokenCountKBranches:
    def test_just_under_10k_uses_one_decimal(self):
        # ``9999 / 1000 = 9.999`` → rounds to ``10.0k``. This
        # produces the visual quirk of "10.0k → 10k" when the
        # count crosses 10_000 (one extra character vanishes).
        # Pinned so future refactors are aware of the
        # transition shape.
        assert format_token_count(9999) == "10.0k"

    def test_10k_drops_the_decimal(self):
        # ``10000 // 1000 = 10`` → "10k" with no decimal. The
        # convention is one-decimal precision below 10k, then
        # integer k above. Drift here would make the counter
        # show "10.0k" indefinitely.
        assert format_token_count(10_000) == "10k"
        assert format_token_count(12_345) == "12k"

    def test_just_under_1m_still_in_k(self):
        # 999_000 = 999k. The threshold to "m" is strict ``<``,
        # so 999_999 stays in the k branch (rounds down).
        assert format_token_count(999_999) == "999k"


class TestFormatTokenCountMBranches:
    def test_at_1m_flips_with_decimal(self):
        # First million. Same convention as k → one decimal up
        # to 10m, then drop it.
        assert format_token_count(1_000_000) == "1.0m"
        assert format_token_count(1_500_000) == "1.5m"

    def test_under_10m_keeps_decimal(self):
        # 9_999_999 = 10.0m for the same reason 9999 = 10.0k.
        assert format_token_count(9_999_999) == "10.0m"

    def test_at_10m_drops_decimal(self):
        assert format_token_count(10_000_000) == "10m"
        assert format_token_count(50_000_000) == "50m"

    def test_under_1b_still_in_m(self):
        assert format_token_count(999_000_000) == "999m"


class TestFormatTokenCountBBranches:
    def test_at_1b_flips_with_decimal(self):
        # Token counts in the billions are realistic on long-
        # lived agent runs (multi-hour orchestration jobs). The
        # b suffix prevents the badge from overflowing.
        assert format_token_count(1_000_000_000) == "1.0b"
        assert format_token_count(2_500_000_000) == "2.5b"

    def test_under_10b_keeps_decimal(self):
        assert format_token_count(9_500_000_000) == "9.5b"

    def test_at_10b_drops_decimal(self):
        assert format_token_count(10_000_000_000) == "10b"
        assert format_token_count(42_000_000_000) == "42b"


class TestFormatTokenCountTBranches:
    def test_at_1t_flips_with_decimal(self):
        # Trillions are mostly defensive — the formatter
        # handles them so a future model with stupendous
        # context doesn't crash the badge.
        assert format_token_count(1_000_000_000_000) == "1.0t"

    def test_under_10t_keeps_decimal(self):
        assert format_token_count(9_500_000_000_000) == "9.5t"

    def test_at_or_above_10t_drops_decimal(self):
        # Above 10t we drop the decimal. The implementation
        # has TWO branches here (< 10_000_000_000_000 and
        # else) — the final ``else`` handles arbitrarily big
        # numbers. Pin both.
        assert format_token_count(10_000_000_000_000) == "10t"
        # Way past the explicit upper bound — exercises the
        # final ``return`` branch.
        assert format_token_count(999_000_000_000_000) == "999t"
