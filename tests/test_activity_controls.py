import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from core import activity


class ControlsTests(unittest.TestCase):
    def test_telemetry_memory_is_bounded(self):
        for _ in range(1000):
            activity.record('test.operation')
        self.assertLessEqual(len(activity.recent['test.operation']), 121)
        activity.counts.clear()
        activity.recent.clear()
        activity.alerts.clear()

    def test_escaped_429_is_not_replayed(self):
        # Isolate the helper without loading credentials or the Discord runtime.
        source = Path('modules/registro_eventos.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                        and n.name == 'retry_discord_rate_limit')

        class HTTPError(Exception):
            status = 429

        namespace = {'discord': SimpleNamespace(HTTPException=HTTPError),
                     'record': lambda kind: None, 'logger': logging.getLogger('test')}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'helper', 'exec'), namespace)
        operation = AsyncMock(side_effect=HTTPError())
        with self.assertRaises(HTTPError):
            asyncio.run(namespace[function.name](operation, action='test'))
        self.assertEqual(operation.await_count, 1)
