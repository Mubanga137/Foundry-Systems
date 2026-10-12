"""
Thread-Safe Multi-Key Gemini API Key Manager

Implements:
  - Round-robin key selection across configured API keys.
  - Per-key rate limiting (configurable RPM).
  - 429/quota cooldown with automatic failover to next healthy key.
  - 401/403 permanent removal from rotation pool.
  - Key redaction in all log output.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def redact_key(key: str) -> str:
    """Redact an API key for safe logging: e.g. AIzaSyD...****89aB."""
    if not key or len(key) <= 6:
        return "****"
    if len(key) <= 12:
        return key[:6] + "...****"
    return key[:7] + "...****" + key[-4:]


@dataclass
class _KeyState:
    """Internal state tracking for a single API key."""
    key: str
    is_active: bool = True
    cooldown_until: float = 0.0
    request_timestamps: deque = field(default_factory=deque)


class GeminiKeyManager:
    """
    Thread-safe round-robin API key manager with rate limiting and cooldown.

    Usage:
        km = GeminiKeyManager(keys=["key1", "key2", "key3"], rpm_per_key=10)
        key = km.acquire_key()
        try:
            # make API call
            km.release_key(key, success=True)
        except RateLimitError:
            km.release_key(key, success=False, error_code=429)
    """

    def __init__(
        self,
        keys: list[str],
        rpm_per_key: int = 10,
        cooldown_seconds: int = 60,
    ):
        if not keys:
            raise ValueError("At least one API key must be provided")

        self._lock = threading.Lock()
        self._rpm = rpm_per_key
        self._cooldown_seconds = cooldown_seconds
        self._keys: list[_KeyState] = [_KeyState(key=k) for k in keys]
        self._current_index = 0
        logger.info(
            "GeminiKeyManager initialized with %d keys: %s",
            len(keys),
            [redact_key(k) for k in keys],
        )

    @property
    def active_key_count(self) -> int:
        """Number of keys currently in the active rotation pool."""
        with self._lock:
            return sum(1 for k in self._keys if k.is_active)

    @property
    def total_key_count(self) -> int:
        """Total number of keys (including deactivated)."""
        return len(self._keys)

    def _is_rate_limited(self, ks: _KeyState) -> bool:
        """Check if a key has exceeded its per-minute request quota."""
        now = time.monotonic()
        # Purge timestamps older than 60 seconds
        while ks.request_timestamps and ks.request_timestamps[0] < now - 60:
            ks.request_timestamps.popleft()
        return len(ks.request_timestamps) >= self._rpm

    def _is_in_cooldown(self, ks: _KeyState) -> bool:
        """Check if a key is in temporary cooldown."""
        return time.monotonic() < ks.cooldown_until

    def acquire_key(self) -> str:
        """
        Acquire the next available API key using round-robin selection.

        Skips keys that are:
          - Permanently deactivated (401/403)
          - In temporary cooldown (429)
          - Rate-limited (too many recent requests)

        Raises RuntimeError if no keys are available.
        """
        with self._lock:
            attempts = 0
            total = len(self._keys)

            while attempts < total:
                ks = self._keys[self._current_index]
                self._current_index = (self._current_index + 1) % total

                if not ks.is_active:
                    attempts += 1
                    continue

                if self._is_in_cooldown(ks):
                    attempts += 1
                    continue

                if self._is_rate_limited(ks):
                    attempts += 1
                    continue

                # Record the request timestamp
                ks.request_timestamps.append(time.monotonic())
                return ks.key

            raise RuntimeError(
                "No API keys available — all keys are rate-limited, "
                "in cooldown, or permanently deactivated."
            )

    def release_key(
        self,
        key: str,
        *,
        success: bool = True,
        error_code: int | None = None,
    ) -> None:
        """
        Report the result of an API call for a key.

        Args:
            key: The API key that was used.
            success: Whether the call succeeded.
            error_code: HTTP error code if the call failed.
        """
        with self._lock:
            ks = self._find_key_state(key)
            if ks is None:
                return

            if success:
                return

            if error_code in (429,):
                # Temporary cooldown
                ks.cooldown_until = time.monotonic() + self._cooldown_seconds
                logger.warning(
                    "Key %s hit rate limit (429). Cooldown for %ds.",
                    redact_key(key),
                    self._cooldown_seconds,
                )
            elif error_code in (401, 403):
                # Permanent removal
                ks.is_active = False
                logger.error(
                    "Key %s returned %d. Permanently removed from rotation.",
                    redact_key(key),
                    error_code,
                )
            else:
                logger.warning(
                    "Key %s failed with error code %s.",
                    redact_key(key),
                    error_code,
                )

    def _find_key_state(self, key: str) -> _KeyState | None:
        """Find the _KeyState for a given key string."""
        for ks in self._keys:
            if ks.key == key:
                return ks
        return None

    def get_healthy_keys(self) -> list[str]:
        """Return list of redacted healthy key identifiers (for diagnostics)."""
        with self._lock:
            return [
                redact_key(ks.key)
                for ks in self._keys
                if ks.is_active and not self._is_in_cooldown(ks)
            ]
