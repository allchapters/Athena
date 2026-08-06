"""Day 2 demo — the agent gets hands: six tools, a sandbox, and a policy.

Concept: yesterday's loop is unchanged. All that happened is that its two sockets
got filled — `tools` with the generated core tools, `before_tool` with a Policy —
and the talker became a builder. That is the payoff of writing the loop with
sockets: capability arrives as an argument, never as an edit.

Design rules:
  * The sandbox is a directory, chosen here. The agent is never told about any
    other one, and resolve() stops it finding one.
  * yolo mode, and it still refuses the catastrophic — the deny list sits above
    every mode, so "trust it" never means "trust it with rm -rf ~".
  * The transcript is the product. Watch the tool calls, not just the answer.

Run:  source Athena-key.sh && python3 demos/day2_build.py
      source Athena-key.sh && python3 demos/day2_build.py "your own task"
"""

import sys
from pathlib import Path

# demos/ is not a package, so put the repo root on the path to import athena.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena import loop, provider, security, tools  # noqa: E402  (after path fix)

WORKDIR = Path(__file__).resolve().parent.parent / "scratch"

TASK = ("Create fib.py with an iterative fib(n), a __main__ printing fib(30), "
        "run it and confirm the output is 832040")

SYSTEM = """You are Athena, a careful coding agent.

You work inside a sandboxed directory. Use your tools to inspect and change it;
prefer reading a file before editing it, and verify your work by running it
rather than by reasoning about it. If a tool refuses, say so plainly and stop —
do not look for a way around it. Finish with a short report of what you did."""


def show(kind, payload):
    """Print each loop event, so the whole conversation is visible."""
    if kind == "assistant":
        if payload["text"]:
            print(f"\n[assistant] {payload['text']}")
        for call in payload["tool_calls"]:
            print(f"[assistant] calls {call['name']}({_brief(call['args'])})")
        usage = payload["usage"]
        print(f"           ({usage['input']} in / {usage['output']} out tokens)")
    elif kind == "tool_end":
        # Tool output is the noisiest thing here and the least often read in
        # full, so show the head and say how much was kept back.
        result = str(payload["result"])
        head = result if len(result) <= 300 else f"{result[:300]}... (+{len(result) - 300})"
        print(f"[tool] {payload['call']['name']} -> {head}")


def _brief(args):
    """One-line argument preview: enough to follow, not enough to bury."""
    return ", ".join(f"{k}={str(v)[:60]!r}" for k, v in args.items())


def main():
    """Run the task in the scratch sandbox under a yolo policy."""
    task = sys.argv[1] if len(sys.argv) > 1 else TASK
    kit = tools.core_tools(str(WORKDIR))
    policy = security.Policy("yolo")

    messages = [{"role": "user", "text": task}]
    print(f"[workdir] {WORKDIR}")
    print(f"[tools] {', '.join(t.name for t in kit)}")
    print(f"[policy] {policy.mode}")
    print(f"\n[user] {task}")
    answer = loop.run_loop(
        model=provider.DEFAULT_MODEL,
        system=SYSTEM,
        messages=messages,
        tools={t.name: t for t in kit},
        on_event=show,
        before_tool=policy.check,
    )
    print(f"\n=== final answer ===\n{answer}")
    print(f"\n[{len(messages)} messages]")


if __name__ == "__main__":
    main()
