"""HTTP retry logic with exponential backoff for transient errors.

Provides :func:`retry_with_backoff` for wrapping HTTP operations that
may fail transiently (429, 5xx errors) and should be retried.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

RetryCallback = Callable[[int, float], None]
"""Callback signature for retry events: (attempt_number, delay_seconds)"""


@dataclass(frozen=True)
class RetryMetadata:
    """Metadata about retries that occurred during an operation.

    Attributes:
        attempt_count: Total number of attempts (1 = no retries, success on first try).
        retry_count: Number of retries after the initial attempt.
        final_delay_seconds: Total time spent waiting between retries.
    """

    attempt_count: int
    retry_count: int
    final_delay_seconds: float

    @property
    def retried(self) -> bool:
        """True if any retries occurred."""
        return self.retry_count > 0


async def retry_with_backoff(
    func: Callable[..., Any],
    *args: Any,
    max_retries: int = 10,
    initial_delay: float = 0.1,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    retry_callback: RetryCallback | None = None,
    **kwargs: Any,
) -> tuple[Any, RetryMetadata]:
    """Retry an async function with exponential backoff on transient errors.

    Retries on:
    * httpx.TimeoutException
    * httpx.ConnectError
    * httpx.HTTPStatusError with status 429, 500, 502, 503, 504

    Other exceptions (programming errors, auth failures, 4xx) propagate
    immediately — they won't succeed with retries.

    Args:
        func: Async callable to retry.
        *args: Positional arguments to pass to func.
        max_retries: Maximum number of retry attempts (default 10).
        initial_delay: Initial delay in seconds before first retry (default 0.1).
        max_delay: Maximum delay between retries in seconds (default 30.0).
        backoff_factor: Multiplier for exponential backoff (default 2.0).
        retry_callback: Optional callback invoked on each retry. Called as
            ``retry_callback(attempt_number, delay_seconds)`` before sleeping.
        **kwargs: Keyword arguments to pass to func.

    Returns:
        Tuple of (result, metadata) where metadata indicates if retries occurred.

    Raises:
        The last exception encountered if all retries are exhausted.
    """
    last_exc: httpx.HTTPError | None = None
    delay = initial_delay
    total_delay = 0.0

    for attempt in range(max_retries + 1):
        try:
            result = await func(*args, **kwargs)
            metadata = RetryMetadata(
                attempt_count=attempt + 1,
                retry_count=attempt,
                final_delay_seconds=total_delay,
            )
            return result, metadata
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            is_retryable = True
        except httpx.HTTPStatusError as e:
            last_exc = e
            is_retryable = e.response.status_code in {429, 500, 502, 503, 504}
        except httpx.HTTPError as e:
            last_exc = e
            is_retryable = False

        if not is_retryable or attempt == max_retries:
            logger.error(
                "HTTP request failed after %d attempts: %s",
                attempt + 1,
                last_exc,
            )
            assert last_exc is not None
            raise last_exc

        logger.debug(
            "HTTP request failed (attempt %d/%d), retrying in %.2fs: %s",
            attempt + 1,
            max_retries + 1,
            delay,
            last_exc,
        )
        if retry_callback:
            retry_callback(attempt + 2, delay)  # Next attempt number for the user
        await asyncio.sleep(delay)
        total_delay += delay
        delay = min(delay * backoff_factor, max_delay)

    # Should not reach here
    assert last_exc is not None
    raise last_exc
