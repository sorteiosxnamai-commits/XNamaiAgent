import pytest

from app.http_resilience import is_transient_status, with_retries


@pytest.mark.asyncio
async def test_with_retries_succeeds_after_transient_failure():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("slow")
        return "ok"

    value = await with_retries(
        operation,
        max_attempts=2,
        backoff_seconds=0,
        retry_exceptions=(TimeoutError,),
    )
    assert value == "ok"
    assert attempts == 2


def test_transient_status_codes():
    assert is_transient_status(503) is True
    assert is_transient_status(404) is False
