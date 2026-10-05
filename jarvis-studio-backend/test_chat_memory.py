"""Chat memory must survive a mid-chat model switch.

Run: python test_chat_memory.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["JARVIS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="jarvis-memory-test-")

import llm.groq_bridge as gb  # noqa: E402
from llm import gemini_bridge  # noqa: E402


def _fill(n_turns: int) -> None:
    gb._history.clear()
    for i in range(n_turns):
        gb._history.append({"role": "user", "content": f"user turn {i}: remember topic-{i}"})
        gb._history.append({"role": "assistant", "content": f"ok, topic-{i} noted."})


def test_groq_prompt_keeps_early_turns():
    """A Groq turn after a long Gemini chat must still see the first topic."""
    _fill(20)
    hist = gb._history_for_prompt(model="openai/gpt-oss-20b")
    blob = " ".join(m["content"] for m in hist)
    assert "topic-0" in blob, blob[:200]
    assert hist[-1]["content"] == "ok, topic-19 noted."


def test_gemini_prompt_keeps_early_turns():
    _fill(20)
    hist = gb._history_for_prompt(model="gemini-3.5-flash")
    blob = " ".join(m["content"] for m in hist)
    assert "topic-0" in blob
    assert len(hist) == 40


def test_groq_budget_drops_oldest_not_the_chat_head_arbitrarily():
    gb._history.clear()
    gb._history.append({"role": "user", "content": "KEEP-ME " + ("x" * 3000)})
    gb._history.append({"role": "assistant", "content": "ack-early"})
    for i in range(8):
        gb._history.append({"role": "user", "content": f"later-{i} " + ("y" * 3000)})
        gb._history.append({"role": "assistant", "content": f"ack-{i}"})
    hist = gb._history_for_prompt(model="openai/gpt-oss-20b")
    blob = " ".join(m["content"] for m in hist)
    assert "later-7" in blob
    total = sum(len(m["content"]) for m in hist)
    assert total <= gb._GROQ_HISTORY_CHAR_BUDGET + 50  # clip suffix slack


def test_gemini_merges_consecutive_roles_and_starts_as_user():
    sys_text, contents = gemini_bridge._to_gemini([
        {"role": "system", "content": "you are jarvis"},
        {"role": "assistant", "content": "orphan note"},
        {"role": "assistant", "content": "second note"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ])
    assert "you are jarvis" in sys_text
    assert contents[0]["role"] == "user"
    roles = [c["role"] for c in contents]
    assert roles == ["user", "model", "user", "model"], roles


def test_voice_outcome_is_a_real_turn():
    gb._history.clear()
    gb.remember_turn_outcome("open notepad", "Opened Notepad.")
    gb.remember_turn_outcome("type hello", "Typed hello.")
    hist = gb._history_for_prompt(model="gemini-2.5-flash")
    assert hist[0]["role"] == "user" and "notepad" in hist[0]["content"]
    assert hist[-1]["role"] == "assistant" and "hello" in hist[-1]["content"]


if __name__ == "__main__":
    test_groq_prompt_keeps_early_turns()
    test_gemini_prompt_keeps_early_turns()
    test_groq_budget_drops_oldest_not_the_chat_head_arbitrarily()
    test_gemini_merges_consecutive_roles_and_starts_as_user()
    test_voice_outcome_is_a_real_turn()
    print("ok")
