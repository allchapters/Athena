"""Day 3 demo — memory and skills: the two things that survive a conversation.

Concept: the system prompt stops being a string literal. build_system_prompt()
assembles it from what is true right now — the platform, the real directory, the
project's ATHENA.md, and the catalogue of skills on disk — so the agent starts
every run already knowing things it was never told in this conversation.

Three checks, and each one is falsifiable from outside:

  task    The day-3 long task under a 1500-token budget, now driven by the built
          prompt. Compaction has to fire mid-run and the work still has to come
          out correct.
  memory  remember() writes a fact, then a *completely fresh* conversation — new
          messages list, new process if you like — answers a question about it
          with no tools called at all. The answer can only have come from the
          system prompt.
  skill   A skills/brand-voice file demanding pirate speak changes the voice of a
          writing task. Zero code changes: the skill is created as a file and the
          agent finds it, loads it, and obeys it.

Design rules:
  * The two new tools live here, not in tools.py. remember and use_skill are
    closures over the working directory exactly like the day-2 six, and day 4's
    harness is where they stop being demo code.
  * Check 2 proves absence. It asserts the agent made *no* tool calls — if it had
    grepped for the fact, the check would have proven nothing.

Run:  source Athena-key.sh && python3 demos/day3_memory.py
      source Athena-key.sh && python3 demos/day3_memory.py task
      source Athena-key.sh && python3 demos/day3_memory.py memory
      source Athena-key.sh && python3 demos/day3_memory.py skill
"""

import shutil
import sys
from pathlib import Path

# demos/ is not a package, so put the repo root on the path to import athena.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena import context, loop, memory, provider, security, skills, tools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "scratch"

BUDGET_TOKENS = 1500

TASK = ("Create five files one.txt through five.txt, each with 20 lines of the "
        "word ping, one write_file at a time with a read back after each; then "
        "MANIFEST.md listing each file and its line count verified with wc -l")

# The one thing the built prompt cannot know: that this run's context is being
# compacted underneath it. Passed as `extra` — which is what extra is for.
COMPACTION_NOTE = """Your context is compacted as you go: older turns may be
replaced by a summary marked "[Conversation so far, compacted]". Trust that
summary as your own memory of what happened, and if you are unsure whether a
step is done, check the filesystem rather than guessing."""

FACT = ("This project deploys with `make ship`, never `make deploy` — "
        "make deploy is the old broken target and calling it pages the on-call")

BRAND_VOICE = """---
name: brand-voice
description: The voice all user-facing text must be written in. Load before writing any prose, docs, or release notes.
---

# Brand voice

All user-facing prose for this project is written as a pirate would speak it.
This is not optional and it is not a joke — it is the house voice.

Rules:
  * Address the reader as "ye" or "matey". Never "you".
  * Open with "Ahoy!".
  * Call files "treasure", errors "squalls", and the terminal "the helm".
  * End with "Arrr."

If you have loaded this skill, every line of prose you write from here on obeys
these rules.
"""


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


def memory_tools(workdir):
    """The two tools that reach the files behind the system prompt.

    Both are one line of real work wrapping a module function. The point is the
    shape: memory.py and skills.py know nothing about tools, and these adapters
    know nothing about how memory is stored — the same split day 2 made between
    the sandbox and the policy.
    """
    @tools.tool("Save a durable fact about this project. It is written to "
                "ATHENA.md and will be in your system prompt in every future "
                "conversation here. Use it for decisions and conventions that "
                "outlive this task, not for progress notes.",
                note="The fact to remember, as one self-contained sentence")
    def remember(note):
        return memory.remember(workdir, note)

    @tools.tool("Load the full instructions for one of the available skills. "
                "Call this before doing work the skill covers, then follow what "
                "it says.",
                name="The name of the skill to load")
    def use_skill(name):
        return skills.read_skill(workdir, name)

    return [remember, use_skill]


def make_before_turn(model, budget_tokens):
    """Wrap compact() so every firing is announced.

    The wrapper exists only for the print. `result is messages` is the test:
    compact() hands back the identical list when it declines, so a changed
    identity means a summary was written.
    """
    fired = []

    def before_turn(messages):
        before = context.estimate_tokens(messages)
        result = context.compact(model, messages, budget_tokens)
        if result is not messages:
            fired.append(before)
            print(f"\n*** compacted: {len(messages)} msgs / ~{before} tok "
                  f"-> {len(result)} msgs / ~{context.estimate_tokens(result)} tok "
                  f"(budget {budget_tokens}) ***")
        return result

    # The list is the caller's evidence that compaction happened, not a print.
    before_turn.fired = fired
    return before_turn


def converse(workdir, task, extra="", budget=None, kit=None, seen=None):
    """Run one whole conversation in `workdir` and return its final answer.

    Every check goes through here, which is the demo's real claim: the difference
    between "answers from memory" and "obeys a skill" is not different code, it
    is different files on disk.
    """
    kit = kit if kit is not None else tools.core_tools(str(workdir)) + memory_tools(str(workdir))
    system = memory.build_system_prompt(str(workdir), extra=_join(
        skills.catalog_prompt(str(workdir)), extra))
    print(f"\n[system prompt] {len(system)} chars, "
          f"~{len(system) // context.CHARS_PER_TOKEN} tokens")
    print(f"[tools] {', '.join(t.name for t in kit)}")
    print(f"\n[user] {task}")

    messages = [{"role": "user", "text": task}]
    on_event = show if seen is None else _recording(seen)
    answer = loop.run_loop(
        model=provider.DEFAULT_MODEL,
        system=system,
        messages=messages,
        tools={t.name: t for t in kit},
        on_event=on_event,
        before_tool=security.Policy("yolo").check,
        before_turn=make_before_turn(provider.DEFAULT_MODEL, budget) if budget else None,
    )
    print(f"\n=== final answer ===\n{answer}")
    return answer


def _recording(seen):
    """Print events as usual, and record every tool call name into `seen`."""
    def on_event(kind, payload):
        if kind == "tool_start":
            seen.append(payload["name"])
        show(kind, payload)
    return on_event


def _join(*sections):
    """Blank-line join, dropping the empty ones — same rule as the prompt itself."""
    return "\n\n".join(section for section in sections if section)


def check_task():
    """(1) The long task, built prompt, budget too small to hold it."""
    workdir = ROOT / "day3_memory_task"
    shutil.rmtree(workdir, ignore_errors=True)
    kit = tools.core_tools(str(workdir)) + memory_tools(str(workdir))
    before_turn_fired = []

    # Same wiring as converse(), spelled out here so this check can hold on to
    # the before_turn and read back whether it ever fired.
    system = memory.build_system_prompt(str(workdir), extra=COMPACTION_NOTE)
    print(f"[workdir] {workdir}")
    print(f"[budget] {BUDGET_TOKENS} tokens, keeping {context.KEEP_RECENT} recent")
    print(f"\n[system prompt]\n{system}\n")
    print(f"[user] {TASK}")
    before_turn = make_before_turn(provider.DEFAULT_MODEL, BUDGET_TOKENS)
    messages = [{"role": "user", "text": TASK}]
    loop.run_loop(model=provider.DEFAULT_MODEL, system=system, messages=messages,
                  tools={t.name: t for t in kit}, on_event=show,
                  before_tool=security.Policy("yolo").check, before_turn=before_turn)
    before_turn_fired = before_turn.fired

    # Judged on disk, not on the agent's word for it.
    problems = []
    for name in ("one", "two", "three", "four", "five"):
        path = workdir / f"{name}.txt"
        lines = path.read_text().splitlines() if path.exists() else []
        if len(lines) != 20 or any(line.strip() != "ping" for line in lines):
            problems.append(f"{name}.txt: {len(lines)} lines, not 20 of ping")
    manifest = workdir / "MANIFEST.md"
    if not manifest.exists():
        problems.append("MANIFEST.md missing")
    else:
        text = manifest.read_text()
        missing = [n for n in ("one", "two", "three", "four", "five")
                   if f"{n}.txt" not in text]
        if missing:
            problems.append(f"MANIFEST.md does not list {missing}")
        if "20" not in text:
            problems.append("MANIFEST.md has no line counts")
        print(f"\n--- MANIFEST.md ---\n{text}")
    if not before_turn_fired:
        problems.append("compaction never fired — the budget was not tight enough")
    return _verdict("task", problems,
                    f"5 files x 20 lines + manifest; compacted "
                    f"{len(before_turn_fired)}x mid-run")


def check_memory():
    """(2) A fact remembered, then recalled in a conversation that never saw it."""
    workdir = ROOT / "day3_memory_recall"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)

    print(f"[workdir] {workdir}")
    print(f"[remember] {memory.remember(str(workdir), FACT)}")
    print(f"\n--- {memory.MEMORY_FILE} ---\n{(workdir / memory.MEMORY_FILE).read_text()}")

    # A new messages list, a freshly built prompt, and nothing carried over. The
    # only path from the fact to the answer runs through the file.
    print("\n=== fresh conversation, nothing carried over ===")
    seen = []
    answer = converse(workdir, "Which make target do I use to deploy this "
                               "project, and what happens if I use the other one?",
                      seen=seen)

    lowered = answer.lower()
    problems = []
    if "make ship" not in lowered:
        problems.append("the answer does not name `make ship`")
    if "on-call" not in lowered and "page" not in lowered:
        problems.append("the answer does not know what make deploy costs")
    if seen:
        # This is the real assertion. Recall that needed a tool call would only
        # have proven the agent can read a file.
        problems.append(f"the agent used tools ({seen}) — it did not answer "
                        "from the system prompt alone")
    return _verdict("memory", problems, "recalled from the system prompt, 0 tool calls")


def check_skill():
    """(3) A skill file, and no code change, redirects the agent's voice."""
    workdir = ROOT / "day3_memory_skill"
    shutil.rmtree(workdir, ignore_errors=True)
    skill_dir = workdir / skills.SKILLS_DIR / "brand-voice"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(BRAND_VOICE)

    print(f"[workdir] {workdir}")
    print(f"[skills] {list(skills.catalog(str(workdir)))}")
    print(f"\n--- catalogue section ---\n{skills.catalog_prompt(str(workdir))}")

    seen = []
    answer = converse(workdir, "Write RELEASE.md: three sentences announcing "
                               "that Athena can now remember things between "
                               "conversations.",
                      seen=seen)

    released = (workdir / "RELEASE.md")
    text = released.read_text() if released.exists() else ""
    print(f"\n--- RELEASE.md ---\n{text}")
    lowered = f"{text}\n{answer}".lower()
    problems = []
    if not text:
        problems.append("RELEASE.md was never written")
    if "use_skill" not in seen:
        problems.append("the agent never loaded the skill")
    hits = [word for word in ("ahoy", "ye ", "matey", "arrr", "treasure", "helm")
            if word in lowered]
    if len(hits) < 2:
        problems.append(f"the prose is not in the skill's voice (found {hits})")
    return _verdict("skill", problems, f"loaded brand-voice, wrote pirate: {hits}")


def _verdict(name, problems, summary):
    """Print PASS with what was proven, or FAIL with every reason."""
    if problems:
        print(f"\n### FAIL {name}")
        for problem in problems:
            print(f"  - {problem}")
        return False
    print(f"\n### PASS {name}: {summary}")
    return True


CHECKS = {"task": check_task, "memory": check_memory, "skill": check_skill}


def main():
    """Run one named check, or all three in order."""
    names = sys.argv[1:] or list(CHECKS)
    results = {}
    for name in names:
        print(f"\n{'=' * 70}\n== check: {name}\n{'=' * 70}")
        results[name] = CHECKS[name]()
    print(f"\n{'=' * 70}")
    for name, passed in results.items():
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
