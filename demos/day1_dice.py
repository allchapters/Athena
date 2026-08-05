"""Day 1 demo — the smallest complete agent: one tool, one loop, one answer.

Concept: a *tool* is just an object with .spec (what the model is told) and .run
(what actually happens). Nothing more. Day 2 generates both from a function
signature; today we write them by hand so the shape is unmistakable.

Design rules:
  * The spec is a promise. Whatever it describes, .run must deliver.
  * Tool arguments arrive as JSON, so a "number" may reach you as a string.
  * The harness stays silent unless asked; on_event is where output comes from.

Run:  source Athena-key.sh && python3 demos/day1_dice.py
"""

import random
import sys
from pathlib import Path

# demos/ is not a package, so put the repo root on the path to import athena.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena import loop, provider  # noqa: E402  (import follows the path fix)

TASK = "Roll 3 dice and tell me whether the total beats 10"


class RollDice:
    """A hand-written tool: .spec describes it, .run performs it."""

    spec = {"schema": {
        "name": "roll_dice",
        "description": "Roll count six-sided dice",
        "parameters": {
            "type": "object",
            "properties": {"count": {"type": "string", "description": "How many dice"}},
            "required": ["count"],
        },
    }}

    def run(self, count):
        """Return the individual rolls as a list.

        The schema declares `count` a string, and the model honours that, so
        coerce before use. Returning the rolls — not the total — leaves the
        arithmetic to the model, which is what makes the transcript worth reading.
        """
        return [random.randint(1, 6) for _ in range(int(count))]


def show(kind, payload):
    """Print each loop event, so the whole conversation is visible."""
    if kind == "assistant":
        if payload["text"]:
            print(f"\n[assistant] {payload['text']}")
        for call in payload["tool_calls"]:
            print(f"[assistant] calls {call['name']}({call['args']})")
        usage = payload["usage"]
        print(f"           ({usage['input']} in / {usage['output']} out tokens)")
    elif kind == "tool_end":
        print(f"[tool] {payload['call']['name']} -> {payload['result']}")


def allow_everything(call):
    """Day 1 policy: permit every call. Day 5 replaces this with real rules."""
    return None


def main():
    """Run the dice task and print the transcript."""
    messages = [{"role": "user", "text": TASK}]
    print(f"[user] {TASK}")
    answer = loop.run_loop(
        model=provider.DEFAULT_MODEL,
        system="You are Athena, a concise assistant. Use tools when they help.",
        messages=messages,
        tools={"roll_dice": RollDice()},
        on_event=show,
        before_tool=allow_everything,
    )
    print(f"\n=== final answer ===\n{answer}")
    print(f"\n[{len(messages)} messages: {' -> '.join(m['role'] for m in messages)}]")


if __name__ == "__main__":
    main()
