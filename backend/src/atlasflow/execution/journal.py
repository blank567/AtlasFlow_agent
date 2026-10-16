"""Small validated-model-stage wrapper, shared by real and injected gateways."""
from __future__ import annotations

from functools import wraps
from typing import Any

from atlasflow.execution.context import CURRENT_EXECUTION, CURRENT_INVOCATION
from atlasflow.execution.contracts import ExecutionControlError, decode_safe, encode_safe


def durable_model_stage(name: str):
    def decorator(function):
        @wraps(function)
        async def wrapped(*args: Any, **kwargs: Any):
            context = CURRENT_EXECUTION.get()
            if context is None:
                return await function(*args, **kwargs)
            lease = await context.runtime.begin_invocation(
                context, kind="llm", name=name, inputs={"args": args[1:], "kwargs": kwargs})
            if lease.replayed:
                return decode_safe(lease.replay)
            await context.runtime.dispatch(lease)
            token = CURRENT_INVOCATION.set(lease.invocation_id)
            try:
                result = await function(*args, **kwargs)
            except ExecutionControlError:
                raise
            except Exception:
                await context.runtime.fail(lease)
                raise
            finally:
                CURRENT_INVOCATION.reset(token)
            # Storage failure here is not a model failure and must escape retry.
            await context.runtime.confirm(lease, result)
            return decode_safe(encode_safe(result))
        return wrapped
    return decorator
