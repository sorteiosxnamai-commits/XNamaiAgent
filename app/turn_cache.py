"""Cache configuration reads within a single conversation turn."""
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import json

_CACHE: ContextVar[dict | None] = ContextVar("turn_configuration_cache", default=None)


def begin_turn_cache():
    return _CACHE.set({})


def end_turn_cache(token):
    _CACHE.reset(token)


def cached_turn_read(function):
    @wraps(function)
    def read(*args, **kwargs):
        cache = _CACHE.get()
        if cache is None:
            return function(*args, **kwargs)
        key = (function.__module__, function.__name__, json.dumps([args, kwargs], sort_keys=True, default=str))
        if key not in cache:
            cache[key] = deepcopy(function(*args, **kwargs))
        return deepcopy(cache[key])
    return read


def invalidates_turn_reads(function):
    @wraps(function)
    def write(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        finally:
            cache = _CACHE.get()
            if cache is not None:
                cache.clear()
    return write
