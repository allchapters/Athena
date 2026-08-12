"""Day 4 demo — the spine: a killed agent that picks up where it was cut off,
and an agent that spends someone else's context window.

Two checks, and neither is simulated:

  resume    A child process runs a real task, appending each message to a JSONL
            transcript, and is SIGKILLed halfway through writing one — a real
            kill -9, not a raised exception. That single death leaves both of the
            damage patterns this file has to survive: a torn last line, and an
            assistant turn whose tool call no result ever answered. A second
            process loads that file and has to find a coherent conversation
            anyway: torn line dropped, stranded call answered honestly, pairing
            intact. It then finishes the task.

  subagent  A parent agent delegates a task to a child with its own clean
            context, and the proof is what is *missing* from the parent's
            transcript: the child's tool calls are not in it. The depth limit is
            checked at the floor, where it has to refuse.

The Harness that composes all of this arrives tomorrow. MiniAgent below is a
stand-in: the fifteen lines of wiring needed to give subagent_tool something with
a .run(), and nothing more.

Run:  source Athena-key.sh && python3 demos/day4_durable.py
      source Athena-key.sh && python3 demos/day4_durable.py resume
      source Athena-key.sh && python3 demos/day4_durable.py subagent
"""

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

# demos/ is not a package, so put the repo root on the path to import athena.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena import loop, memory, provider, security, session, subagent, tools  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "scratch"

TASK = ("Create three files a.txt, b.txt and c.txt, each containing 5 lines of "
        "the word ping, one write_file at a time with a read back after each; "
        "then write MANIFEST.md listing each file and its line count as verified "
        "with wc -l")

# Kill on this tool call, counting from 1. Late enough that real work is on disk
# and in the transcript, early enough that plenty is left to resume.
KILL_ON_CALL = 5

FILES = ("a.txt", "b.txt", "c.txt")


def show(kind, payload):
    """Print each loop event, so the whole conversation is visible."""
    if kind == "assistant":
        if payload["text"]:
            print(f"\n[assistant] {payload['text']}")
        for call in payload["tool_calls"]:
            args = ", ".join(f"{k}={str(v)[:40]!r}" for k, v in call["args"].items())
            print(f"[assistant] calls {call['name']}({args})")
    elif kind == "tool_end":
        result = str(payload["result"])
        head = result if len(result) <= 120 else f"{result[:120]}... (+{len(result) - 120})"
        print(f"[tool] {payload['call']['name']} -> {head}")


class MiniAgent:
    """A stand-in for tomorrow's Harness: a workdir, a depth, and a .run().

    It exists so subagent_tool has something to build. The only thing worth
    noticing is that the tool list includes spawn_agent built from *this* agent's
    depth, so each level hands its children a limit one step tighter.
    """

    def __init__(self, workdir, depth=0, on_event=show, session_path=None):
        self.workdir = str(workdir)
        self.depth = depth
        self.on_event = on_event
        self.session_path = session_path
        self.messages = []
        kit = tools.core_tools(self.workdir)
        kit.append(subagent.subagent_tool(self._child, depth=depth))
        self.tools = {t.name: t for t in kit}

    def _child(self, depth):
        """Factory handed to subagent_tool: a fresh agent, one level deeper.

        A new MiniAgent means a new messages list, which is the whole point — the
        child's context starts empty and the parent's never sees inside it.
        """
        label = f"{'  ' * depth}[sub-agent depth {depth}]"
        return MiniAgent(self.workdir, depth=depth,
                         on_event=lambda kind, payload: _prefixed(label, kind, payload))

    def run(self, task, resume=None):
        """Run one task to completion and return the final answer."""
        self.messages = list(resume) if resume else [{"role": "user", "text": task}]
        if self.session_path and not resume:
            session.append(self.session_path, self.messages[0])
        return loop.run_loop(
            model=provider.DEFAULT_MODEL,
            system=memory.build_system_prompt(self.workdir),
            messages=self.messages,
            tools=self.tools,
            on_event=self._record,
            before_tool=security.Policy("yolo").check,
        )

    def _record(self, kind, payload):
        """Persist as we go, then report.

        Persisting on the event rather than after the run is the durability
        property: the transcript is only ever as stale as the last thing that
        happened, so a process that dies here loses a message, not a session.
        """
        if self.session_path:
            if kind == "assistant":
                session.append(self.session_path, self.messages[-1])
            elif kind == "tool_end":
                session.append(self.session_path, self.messages[-1])
        self.on_event(kind, payload)


def _prefixed(label, kind, payload):
    """Print a sub-agent's events indented, so nesting is visible."""
    if kind == "assistant":
        for call in payload["tool_calls"]:
            print(f"{label} calls {call['name']}")
        if payload["text"]:
            print(f"{label} {payload['text'][:200]}")


# ---------------------------------------------------------------- check: resume

def _child_run(session_path, kill_on):
    """The subprocess half of the resume check: work, then die mid-write.

    Runs in its own process because the failure being tested is the process
    ending. An exception would unwind, flush, and generally behave — SIGKILL is
    the one that does not, and it is the one that happens.
    """
    workdir = Path(session_path).parent.parent.parent
    if kill_on:
        _arrange_a_crash(session_path, kill_on)
    agent = MiniAgent(workdir, session_path=session_path)
    resume = session.load(session_path) if os.path.exists(session_path) else None
    if resume:
        print(f"[resumed] {len(resume)} messages recovered from disk", flush=True)
        for message in resume:
            if message.get("text") == session.INTERRUPTED:
                print(f"[repaired] {message['name']}: {message['text']}", flush=True)
    print(f"\n{agent.run(TASK, resume=resume)}", flush=True)


def _arrange_a_crash(session_path, kill_on):
    """Wrap session.append so the `kill_on`th tool result is written only halfway.

    Being killed *during* a write is the case worth testing and the one that
    cannot be waited for, so it is arranged: write half the line, then SIGKILL
    from inside the write itself. What the next process finds on disk is a real
    partial flush — an assistant turn asking for a tool, and half a line where
    its answer was going.
    """
    real_append = session.append
    written = []

    def append(path, message):
        if message.get("role") == "tool":
            written.append(message["name"])
            if len(written) >= kill_on:
                line = json.dumps(message, ensure_ascii=False) + "\n"
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line[:len(line) // 2])
                print(f"\n*** SIGKILL mid-write of result {len(written)} "
                      f"({message['name']}): {len(line) // 2} of {len(line)} "
                      f"bytes reached the file ***", flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
        real_append(path, message)

    session.append = append


def check_resume():
    """(1) Kill an agent mid-call, then have a fresh process carry on."""
    workdir = ROOT / "day4_resume"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)

    first = session.new_session(workdir, "day 4: kill and resume")
    print(f"[workdir] {workdir}")
    print(f"[session] {os.path.relpath(first, workdir)}")

    print(f"\n--- process 1: works, dies mid-write of tool result {KILL_ON_CALL} ---")
    done = _spawn(first, KILL_ON_CALL)
    print(done.stdout)
    if done.returncode != -signal.SIGKILL:
        return _verdict("resume", [f"process 1 exited {done.returncode}, "
                                   f"not by SIGKILL — nothing was interrupted"], "")

    problems = []
    lines = Path(first).read_bytes().splitlines(keepends=True)
    if lines[-1].endswith(b"\n"):
        problems.append("the last line is whole — the crash left nothing torn")
    print(f"[on disk] {len(lines)} lines, the last one torn: {lines[-1][-48:]!r}")

    recovered = session.load(first)
    print(f"[loaded] {len(lines)} lines (one torn) -> "
          f"{len(recovered)} messages after repair")
    for message in recovered:
        if message.get("text") == session.INTERRUPTED:
            print(f"[repaired] {message['name']}: {message['text']}")

    # The pairing rule, which is the only thing the provider actually insists on.
    calls = sum(len(m.get("tool_calls") or []) for m in recovered
                if m.get("role") == "assistant")
    results = sum(1 for m in recovered if m.get("role") == "tool")
    if calls != results:
        problems.append(f"{calls} tool calls but {results} results — unsendable")
    if not any(m.get("text") == session.INTERRUPTED for m in recovered):
        problems.append("the interrupted call was never answered by repair")
    if recovered and recovered[-1].get("role") != "tool":
        problems.append("the recovered transcript does not end on a tool result")

    # latest() is how a resuming process finds the session with no argument.
    if session.latest(workdir) != first:
        problems.append(f"latest() returned {session.latest(workdir)}, not {first}")

    # A second session file, seeded with the repaired history: append-only means
    # the crashed transcript stays exactly as the crash left it, as evidence.
    second = session.new_session(workdir, "resumed")
    for message in recovered:
        session.append(second, message)
    print(f"\n--- process 2: loads {os.path.relpath(second, workdir)} and carries on ---")
    done = _spawn(second, 0)
    print(done.stdout)
    if done.returncode != 0:
        problems.append(f"process 2 exited {done.returncode}")

    for name in FILES:
        path = workdir / name
        text = path.read_text() if path.exists() else ""
        if [line.strip() for line in text.splitlines()] != ["ping"] * 5:
            problems.append(f"{name}: {len(text.splitlines())} lines, not 5 of ping")
    manifest = workdir / "MANIFEST.md"
    if not manifest.exists():
        problems.append("MANIFEST.md missing")
    else:
        print(f"\n--- MANIFEST.md ---\n{manifest.read_text()}")
        missing = [n for n in FILES if n not in manifest.read_text()]
        if missing:
            problems.append(f"MANIFEST.md does not list {missing}")
    return _verdict("resume", problems,
                    f"killed mid-write on result {KILL_ON_CALL}, torn line "
                    f"dropped, stranded call answered, {len(recovered)} messages "
                    "recovered, task finished")


def _spawn(session_path, kill_on):
    """Run this file's child half in a real subprocess and capture its output."""
    return subprocess.run(
        [sys.executable, __file__, "_child", str(session_path), str(kill_on)],
        capture_output=True, text=True, timeout=600)


# -------------------------------------------------------------- check: subagent

def check_subagent():
    """(2) Delegation: the child's work lands, the child's transcript does not."""
    workdir = ROOT / "day4_subagent"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    # Something worth delegating: finding the answer costs several tool calls,
    # and the answer is one line.
    (workdir / "data.txt").write_text("".join(
        f"{'ERROR' if n % 7 == 0 else 'ok'} line {n}\n" for n in range(1, 121)))

    problems = []

    # The floor first, because it is the case with a wrong answer available: an
    # agent already at the limit must be told to do the work itself.
    floor = subagent.subagent_tool(lambda depth: _explode(), depth=2, max_depth=2)
    refusal = floor.run(task="anything")
    print(f"[depth 2/2] spawn_agent -> {refusal}")
    if not refusal.startswith("ERROR: sub-agent depth limit reached"):
        problems.append(f"the depth limit did not refuse: {refusal!r}")
    if floor.name != "spawn_agent":
        problems.append(f"tool is named {floor.name}, not spawn_agent")
    if floor.spec["schema"]["parameters"]["required"] != ["task"]:
        problems.append(f"schema takes {floor.spec['schema']['parameters']}")

    print(f"\n[workdir] {workdir}")
    parent = MiniAgent(workdir)
    print(f"[tools] {', '.join(parent.tools)}")
    answer = parent.run(
        "Delegate to a sub-agent, using spawn_agent, the job of counting how many "
        "lines of data.txt contain the word ERROR. Do not open data.txt yourself. "
        "Then write the number it reports into REPORT.md as a single line "
        "'ERROR lines: N'.")
    print(f"\n=== final answer ===\n{answer}")

    used = [call["name"] for message in parent.messages
            for call in message.get("tool_calls") or []]
    print(f"\n[parent's own calls] {used}")
    report = (workdir / "REPORT.md")
    text = report.read_text() if report.exists() else ""
    print(f"--- REPORT.md ---\n{text}")

    if "spawn_agent" not in used:
        problems.append("the parent never delegated")
    if "17" not in text:
        problems.append(f"REPORT.md does not carry the child's answer (17): {text!r}")
    # The point of the whole file: the parent paid for one tool result, not for
    # the child's reading of a 120-line file.
    leaked = [name for name in used if name in ("read_file", "grep", "bash")]
    if leaked:
        problems.append(f"the parent did the work itself ({leaked}) — "
                        "nothing was delegated")
    return _verdict("subagent", problems,
                    f"parent called {used}, child's transcript never entered it")


def _explode():
    """A factory that must not be called: proof the limit refuses before spawning."""
    raise AssertionError("make_harness called past the depth limit")


def _verdict(name, problems, summary):
    """Print PASS with what was proven, or FAIL with every reason."""
    if problems:
        print(f"\n### FAIL {name}")
        for problem in problems:
            print(f"  - {problem}")
        return False
    print(f"\n### PASS {name}: {summary}")
    return True


CHECKS = {"resume": check_resume, "subagent": check_subagent}


def main():
    """Run one named check, or both; `_child` is the resume check's subprocess."""
    if len(sys.argv) > 1 and sys.argv[1] == "_child":
        return _child_run(sys.argv[2], int(sys.argv[3])) or 0

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
