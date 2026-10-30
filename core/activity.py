"""Bounded operational counters; never record payloads or credentials."""
import logging
import time
from collections import Counter, deque


logger = logging.getLogger(__name__)
counts = Counter()
recent = {}
alerts = {}


def record(kind):
    counts[kind] += 1
    now = time.monotonic()
    window = recent.setdefault(kind, deque(maxlen=121))
    window.append(now)
    while window and now - window[0] > 60:
        window.popleft()
    threshold = 5 if kind in ('discord.429', 'gateway.resume', 'gateway.disconnect') else 60
    if len(window) >= threshold and now - alerts.get(kind, -300) >= 300:
        alerts[kind] = now
        logger.warning('ACTIVITY burst kind=%s count=%s window=60s', kind, len(window))


def report():
    if counts:
        logger.info('ACTIVITY 15min operation_counts=%s', dict(counts))
        counts.clear()


class DiscordErrorSummary(logging.Handler):
    def emit(self, entry):
        if entry.exc_info and entry.exc_info[1] is not None:
            error = entry.exc_info[1]
            status = getattr(error, 'status', None)
            if status:
                record('discord.' + str(status))
                if status == 429:
                    # Keep the traceback but discard the HTML response body.
                    if hasattr(error, 'text'):
                        error.text = 'Rate limited; response body omitted'
                    error.args = ('HTTP 429: rate limited; response body omitted',)
