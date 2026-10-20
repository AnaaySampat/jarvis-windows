"""gemini_stream keeps the spaces between streamed chunks (live 2026-10-19: "Ican't",
"yourcomputer", "The`C:\\…`" — each chunk was stripped before it was joined)."""
import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from unittest import mock

from llm import gemini_bridge as gb


def _sse(text: str) -> str:
    return "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]})


class FakeClient:
    def __init__(self, chunks):
        self.lines = [_sse(c) for c in chunks]

    @asynccontextmanager
    async def stream(self, *_a, **_k):
        lines = self.lines

        class Resp:
            status_code = 200

            async def aiter_lines(self):
                for line in lines:
                    yield line
        yield Resp()


class StreamTests(unittest.TestCase):
    def test_chunk_edges_survive(self):
        async def run():
            with mock.patch.object(gb, "_client", lambda: FakeClient(["I", " can't reach", " your\ncomputer."])), \
                 mock.patch.object(gb, "_request_auth", mock.AsyncMock(return_value=({}, {}))):
                return [full async for _d, full in gb.gemini_stream(
                    [{"role": "user", "content": "hi"}], "gemini-x", "key")][-1]
        self.assertEqual(asyncio.run(run()), "I can't reach your\ncomputer.")


if __name__ == "__main__":
    unittest.main()
