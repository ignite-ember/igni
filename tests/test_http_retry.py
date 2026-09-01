"""Tests for the HTTP retry logic."""

import asyncio

import httpx
import pytest

from ember_code.core.utils.http_retry import retry_with_backoff


@pytest.mark.asyncio
async def test_retry_succeeds_on_first_attempt():
    """Verify success on first attempt returns immediately."""
    call_count = 0

    async def success_func():
        nonlocal call_count
        call_count += 1
        return "success"

    result, metadata = await retry_with_backoff(success_func)
    assert result == "success"
    assert call_count == 1
    assert metadata.attempt_count == 1
    assert metadata.retry_count == 0
    assert not metadata.retried


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_error():
    """Verify retry succeeds after a 429 error."""
    call_count = 0

    async def fail_then_succeed():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            response = httpx.Response(429)
            raise httpx.HTTPStatusError("Rate limited", request=None, response=response)
        return "success"

    result, metadata = await retry_with_backoff(fail_then_succeed)
    assert result == "success"
    assert call_count == 2
    assert metadata.attempt_count == 2
    assert metadata.retry_count == 1
    assert metadata.retried


@pytest.mark.asyncio
async def test_retry_on_timeout():
    """Verify retry succeeds after a timeout error."""
    call_count = 0

    async def timeout_then_succeed():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.TimeoutException("Timeout")
        return "success"

    result, metadata = await retry_with_backoff(timeout_then_succeed)
    assert result == "success"
    assert call_count == 2
    assert metadata.retried


@pytest.mark.asyncio
async def test_retry_on_connect_error():
    """Verify retry succeeds after a connect error."""
    call_count = 0

    async def connect_error_then_succeed():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("Connection failed")
        return "success"

    result, metadata = await retry_with_backoff(connect_error_then_succeed)
    assert result == "success"
    assert call_count == 2
    assert metadata.retried


@pytest.mark.asyncio
async def test_retry_on_5xx_errors():
    """Verify retry succeeds after 5xx errors."""
    for status_code in [500, 502, 503, 504]:
        call_count = 0

        # ``status_code`` bound as a default rather than closed over: a closure
        # over a loop variable sees the *last* value, so if this were ever called
        # after the loop — a deferred retry, an asyncio task — every iteration
        # would raise 504 and the test would pass while checking one status.
        async def server_error_then_succeed(status_code: int = status_code):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                response = httpx.Response(status_code)
                raise httpx.HTTPStatusError(
                    f"Server error {status_code}", request=None, response=response
                )
            return "success"

        result, metadata = await retry_with_backoff(server_error_then_succeed)
        assert result == "success"
        assert call_count == 2
        assert metadata.retried


@pytest.mark.asyncio
async def test_non_retryable_error_fails_immediately():
    """Verify non-retryable errors (4xx) fail immediately."""
    call_count = 0

    async def client_error():
        nonlocal call_count
        call_count += 1
        response = httpx.Response(400)
        raise httpx.HTTPStatusError("Bad request", request=None, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        await retry_with_backoff(client_error)
    assert call_count == 1


@pytest.mark.asyncio
async def test_exhausts_retries():
    """Verify all retries are exhausted on persistent failure."""
    call_count = 0

    async def always_fails():
        nonlocal call_count
        call_count += 1
        response = httpx.Response(429)
        raise httpx.HTTPStatusError("Rate limited", request=None, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        await retry_with_backoff(always_fails, max_retries=3)
    assert call_count == 4  # 1 initial + 3 retries


@pytest.mark.asyncio
async def test_exponential_backoff():
    """Verify exponential backoff delays are applied."""
    call_count = 0
    call_times = []

    async def fail_then_succeed():
        nonlocal call_count
        call_count += 1
        call_times.append(asyncio.get_event_loop().time())
        if call_count <= 3:
            response = httpx.Response(429)
            raise httpx.HTTPStatusError("Rate limited", request=None, response=response)
        return "success"

    result, metadata = await retry_with_backoff(
        fail_then_succeed, max_retries=3, initial_delay=0.01, backoff_factor=2.0
    )
    assert result == "success"
    assert call_count == 4
    assert metadata.retried

    # Verify backoff delays increase
    delay1 = call_times[1] - call_times[0]
    delay2 = call_times[2] - call_times[1]
    delay3 = call_times[3] - call_times[2]

    # Allow some tolerance for timing
    assert delay2 > delay1 * 1.5
    assert delay3 > delay2 * 1.5


@pytest.mark.asyncio
async def test_retry_logs_debug_when_retries_occur():
    """Verify retry debug logs when retries happen.

    Captures with a handler of its own rather than ``caplog``. pytest's capture
    handler lives on the root logger, and something in the backend boot takes it
    off again — no ``removeHandler`` call exists in this repository, so it comes
    from a third-party import. Measured: root carries four handlers when this test
    runs alone and three after ``test_backend_server.py``, and the missing one is
    pytest's, so ``caplog.records`` came back empty while the retry had
    demonstrably happened (``metadata.retried`` was true). A handler attached to
    the logger under test is immune to whatever else the process does to root.
    """
    import logging

    call_count = 0

    async def fail_once():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            response = httpx.Response(429)
            raise httpx.HTTPStatusError("Rate limited", request=None, response=response)
        return "success"

    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("ember_code.core.utils.http_retry")
    handler = _Collect(level=logging.DEBUG)
    previous_level = logger.level
    # Two suppressions were once documented here, each enough on its own to
    # empty this assertion:
    #
    #   1. ``logger.disabled`` was True, set by something never identified.
    #      Not ``dictConfig`` — traced and never called — and no
    #      ``.disabled = True`` assignment exists in this repository.
    #      Instrumenting ``Logger.__setattr__`` to catch the write made the
    #      failure disappear, so no explanation was ever confirmed.
    #   2. pytest's capture handler is removed from the root logger, so
    #      anything that does emit is not recorded — root carries four
    #      handlers when this file runs alone and three after
    #      ``test_backend_server.py``.
    #
    # (2) is real and the local handler above answers it.
    #
    # (1) is no longer reproducible. Instrumenting
    # ``logging.config.fileConfig`` and ``dictConfig`` across the whole
    # suite recorded **zero** calls to either, and a probe on this logger,
    # ``group_policy``'s and ``merge_plan``'s — created before collection so
    # ``disable_existing_loggers`` could reach them — was never disabled at
    # any point in 3432 tests.
    #
    # So the clearing is gone, and an assertion has taken its place. If the
    # flag ever comes back, this fails and says so, which is the outcome a
    # silent workaround denied: a suppressed logger makes an assertion about
    # log contents pass for the wrong reason.
    assert not logger.disabled, (
        'this logger is disabled, so nothing it emits will be recorded and the '
        'assertion below would pass vacuously. Something in the session set '
        '.disabled — the cause was never identified when this was last seen.'
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        result, metadata = await retry_with_backoff(fail_once)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert result == "success"
    assert metadata.retried
    # Debug logs should record the retry attempt
    debug_logs = [r for r in records if r.levelno == logging.DEBUG]
    assert len(debug_logs) > 0
    assert any("retrying" in r.getMessage().lower() for r in debug_logs)


@pytest.mark.asyncio
async def test_metadata_tracks_attempts():
    """Verify retry metadata properly tracks attempts."""
    call_count = 0

    async def fail_twice():
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            response = httpx.Response(429)
            raise httpx.HTTPStatusError("Rate limited", request=None, response=response)
        return "success"

    result, metadata = await retry_with_backoff(fail_twice)

    assert result == "success"
    assert metadata.attempt_count == 3
    assert metadata.retry_count == 2
    assert metadata.retried
    assert metadata.final_delay_seconds > 0
