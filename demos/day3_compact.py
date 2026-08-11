"""Day 3 demo — the long task: a context budget small enough to hurt.

Concept: the loop's last empty socket gets filled. `before_turn` runs once per
turn, and today it is compaction, so a task with more steps than the window has
room for finishes anyway. Nothing in loop.py changes — the agent simply stops
being able to run out of context.

The budget here (1500 tokens) is deliberately far too small for the task. That is
the point: compaction has to fire several times mid-run, and the agent has to
keep working correctly across the seams.

Design rules:
  * The demo prints when compaction fires. compact() itself stays silent — it is
    arithmetic and one model call, not a narrator.
  * The task is verifiable from outside. Five files, twenty lines each, and a
    manifest counted with wc -l: correct or not, no judgement call.

Run:  source Athena-key.sh && python3 demos/day3_compact.py
      source Athena-key.sh && python3 demos/day3_compact.py 1500 "your own task"
"""

import shutil
import sys
from pathlib import Path

# demos/ is not a package, so put the repo root on the path to import athena.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena import context, loop, provider, security, tools  # noqa: E402

WORKDIR = Path(__file__).resolve().parent.parent / "scratch" / "day3"

BUDGET_TOKENS = 1500

TASK = ("Create five files one.txt through five.txt, each with 20 lines of the "
        "word ping, one write_file at a time with a read back after each; then "
        "MANIFEST.md listing each file and its line count verified with wc -l")

SYSTEM = """You are Athena, a careful coding agent.

You work inside a sandboxed directory. Use your tools to inspect and change it;
prefer reading a file before editing it, and verify your work by running it
rather than by reasoning about it. Work one step at a time.

Your context is compacted as you go: older turns may be replaced by a summary
marked "[Conversation so far, compacted]". Trust that summary as your own memory
of what happened, and if you are unsure whether a step is done, check the
filesystem rather than guessing. Finish with a short report of what you did."""


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
        result = str(payload["result"])
        head = result if len(result) <= 200 else f"{result[:200]}... (+{len(result) - 200})"
        print(f"[tool] {payload['call']['name']} -> {head}")


def _brief(args):
    """One-line argument preview: enough to follow, not enough to bury."""
    return ", ".join(f"{k}={str(v)[:40]!r}" for k, v in args.items())


def make_before_turn(model, budget_tokens):
    """Wrap compact() so every firing is announced.

    The wrapper exists only for the print. `result is messages` is the test:
    compact() hands back the identical list when it declines, so a changed
    identity means a summary was written.
    """
    def before_turn(messages):
        before = context.estimate_tokens(messages)
        result = context.compact(model, messages, budget_tokens)
        if result is not messages:
            after = context.estimate_tokens(result)
            print(f"\n*** compacted: {len(messages)} msgs / ~{before} tok "
                  f"-> {len(result)} msgs / ~{after} tok (budget {budget_tokens}) ***")
        return result
    return before_turn


def main():
    """Run a long task under a context budget too small to hold it."""
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else BUDGET_TOKENS
    task = sys.argv[2] if len(sys.argv) > 2 else TASK

    # Start clean, or the agent reads last run's files and skips the work.
    shutil.rmtree(WORKDIR, ignore_errors=True)
    kit = tools.core_tools(str(WORKDIR))
    policy = security.Policy("yolo")

    messages = [{"role": "user", "text": task}]
    print(f"[workdir] {WORKDIR}")
    print(f"[tools] {', '.join(t.name for t in kit)}")
    print(f"[policy] {policy.mode}")
    print(f"[budget] {budget} tokens, keeping {context.KEEP_RECENT} recent messages")
    print(f"\n[user] {task}")
    answer = loop.run_loop(
        model=provider.DEFAULT_MODEL,
        system=SYSTEM,
        messages=messages,
        tools={t.name: t for t in kit},
        on_event=show,
        before_tool=policy.check,
        before_turn=make_before_turn(provider.DEFAULT_MODEL, budget),
    )
    print(f"\n=== final answer ===\n{answer}")
    print(f"\n[{len(messages)} messages, ~{context.estimate_tokens(messages)} tokens]")


if __name__ == "__main__":
    main()
