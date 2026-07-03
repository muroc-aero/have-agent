"""ULID generation (stdlib only).

Sortable 26-char Crockford base32 IDs: 48-bit millisecond timestamp +
80-bit randomness. Monotonic within a process so same-millisecond IDs
still sort in creation order.
"""

import os
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MAX_RANDOM = (1 << 80) - 1

_lock = threading.Lock()
_last_ts = -1
_last_random = 0


def new_ulid() -> str:
    global _last_ts, _last_random
    with _lock:
        ts = time.time_ns() // 1_000_000
        if ts <= _last_ts:
            ts = _last_ts
            if _last_random >= _MAX_RANDOM:
                ts += 1
                rand = int.from_bytes(os.urandom(10))
            else:
                rand = _last_random + 1
        else:
            rand = int.from_bytes(os.urandom(10))
        _last_ts, _last_random = ts, rand
    value = (ts << 80) | rand
    return "".join(_CROCKFORD[(value >> (5 * i)) & 0x1F] for i in range(25, -1, -1))
