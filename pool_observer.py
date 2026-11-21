"""Exercise the same serialization as a real pool submission during local profiling."""
from concurrent.futures import Future
from multiprocessing.reduction import ForkingPickler


def roundtrip(value):
    return ForkingPickler.loads(ForkingPickler.dumps(value))


def run_in_process(pool, function, /, *args, **kwargs):
    """Measure the Python SQL work normally submitted to a process pool."""
    future = Future()
    try:
        if pool._initializer and not getattr(pool, "_profile_initialised", False):
            pool._initializer(*pool._initargs)
            pool._profile_initialised = True
        # A real process pool serializes both sides, exercising e.g. Expr reducers.
        function, args, kwargs = roundtrip((function, args, kwargs))
        future.set_result(roundtrip(function(*args, **kwargs)))
    except Exception as error:
        future.set_exception(error)
    return future
