import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager


DEFAULT_RETRIES = int(os.getenv("UPSTREAM_RETRIES", "3"))
DEFAULT_BACKOFF_SECONDS = float(os.getenv("UPSTREAM_RETRY_BACKOFF_SECONDS", "0.25"))
FAILURE_THRESHOLD = int(os.getenv("UPSTREAM_CIRCUIT_FAILURE_THRESHOLD", "3"))
RECOVERY_SECONDS = int(os.getenv("UPSTREAM_CIRCUIT_RECOVERY_SECONDS", "60"))


class UpstreamUnavailableError(RuntimeError):
    def __init__(self, source):
        self.source = source
        super().__init__(f"{source} is temporarily unavailable")


class BoundedTTLCache:
    def __init__(self, max_size, ttl_seconds):
        self.max_size = max(1, max_size)
        self.ttl_seconds = ttl_seconds
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            if now - item["created_at"] >= self.ttl_seconds:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return item["value"]

    def set(self, key, value, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self._items[key] = {"created_at": now, "value": value}
            self._items.move_to_end(key)
            while len(self._items) > self.max_size:
                self._items.popitem(last=False)


class KeyedLockPool:
    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    @contextmanager
    def acquire(self, key):
        with self._lock:
            item = self._items.setdefault(
                key,
                {"lock": threading.Lock(), "users": 0},
            )
            item["users"] += 1

        item["lock"].acquire()
        try:
            yield
        finally:
            item["lock"].release()
            with self._lock:
                item["users"] -= 1
                if item["users"] == 0 and self._items.get(key) is item:
                    del self._items[key]


class CircuitBreaker:
    def __init__(self, failure_threshold=FAILURE_THRESHOLD, recovery_seconds=RECOVERY_SECONDS):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._states = {}
        self._lock = threading.Lock()

    def allow_request(self, source, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._states.get(source)
            if state is None or state["failures"] < self.failure_threshold:
                return True
            return now - state["opened_at"] >= self.recovery_seconds

    def record_success(self, source):
        with self._lock:
            self._states.pop(source, None)

    def record_failure(self, source, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            state = self._states.setdefault(source, {"failures": 0, "opened_at": None})
            state["failures"] += 1
            if state["failures"] >= self.failure_threshold:
                state["opened_at"] = now


circuit_breaker = CircuitBreaker()


def call_with_resilience(source, operation, retries=DEFAULT_RETRIES, retry_if=None):
    if not circuit_breaker.allow_request(source):
        raise UpstreamUnavailableError(source)

    attempts = max(1, retries)
    for attempt in range(attempts):
        try:
            result = operation()
            circuit_breaker.record_success(source)
            return result
        except Exception as error:
            should_retry = retry_if(error) if retry_if is not None else True
            if not should_retry or attempt == attempts - 1:
                circuit_breaker.record_failure(source)
                raise UpstreamUnavailableError(source) from error
            time.sleep(DEFAULT_BACKOFF_SECONDS * (2 ** attempt))

    raise UpstreamUnavailableError(source)