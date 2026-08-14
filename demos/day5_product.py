"""Day 5 demo — the product: the harness doing the three things it was built for.

Nothing is stubbed here and nothing is simulated. Each check drives the real
`Harness` — the same object `python3 -m athena` drives — against a real model,
and then goes looking for the evidence on disk.

  resume    A real process starts a six-file task and is killed with a real
            kill -9 while a tool call is outstanding. What is left behind is the
            hard case: an assistant turn asking for a tool that no result ever
            answered. A second process, in the same directory, calls resume() and
            then run("continue the task") — the recovered transcript carries the
            interruption notice, the harness keeps appending to the same file
            rather than starting a new one, and all six files exist at the end.

  delegate  A parent delegates two pieces of implementation to two children, each
            with its own clean context, and then runs the tests itself. Two
            things are checked that the final answer cannot tell you: that the
            parent really did run python3 itself, and that .athena/sessions holds
            exactly one file — a child that wrote a session of its own would be
            the next --resume, which is the bug persist=False exists to prevent.

  fleet     Two agents, two directories, one call. Plus the failure path, which
            costs nothing to check and is the one everybody gets wrong: a job
            that raises must come back as a result, not take the fleet with it.

Run:  source Athena-key.sh && python3 demos/day5_product.py
      source Athena-key.sh && python3 demos/day5_product.py resume
      source Athena-key.sh && python3 demos/day5_product.py delegate fleet
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# demos/ is not a package, so put the repo root on the path to import athena.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena import Harness, run_fleet, session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "scratch"

TASK = ("Create part1.txt through part5.txt one at a time, then SUMMARY.md "
        "describing each")

PARTS = tuple(f"part{n}.txt" for n in range(1, 6))

# Which tool call to die on, counting from 1. Late enough that real work is on
# disk and in the transcript, early enough that plenty is left to resume.
KILL_ON_CALL = 4

# How the child tells the parent "I am now stalled with a call outstanding".
MARKER = ".stalled"

DELEGATE_TASK = (
    "Use spawn_agent twice: delegate writing utils.py with a slugify(text) "
    "function to one child, and test_utils.py with five asserts to another; "
    "then run python3 test_utils.py yourself and report")


def show(kind, payload):
    """Print each loop event, so the whole conversation is visible."""
    if kind == "assistant":
        if payload["text"]:
            print(f"\n[assistant] {payload['text']}", flush=True)
        for call in payload["tool_calls"]:
            args = ", ".join(f"{k}={str(v)[:40]!r}" for k, v in call["args"].items())
            print(f"[assistant] calls {call['name']}({args})", flush=True)
    elif kind == "tool_end":
        result = str(payload["result"])
        head = result if len(result) <= 100 else f"{result[:100]}... (+{len(result) - 100})"
        print(f"[tool] {payload['call']['name']} -> {head}", flush=True)


# ---------------------------------------------------------------- check: resume

def check_resume():
    """(1) Kill an agent mid-call, then have a fresh process carry the task on."""
    workdir = _fresh("day5_resume")
    print(f"[workdir] {workdir}")

    print(f"\n--- process 1: works, then is kill -9'd on tool call {KILL_ON_CALL} ---")
    first = _spawn(["_work", str(workdir), str(KILL_ON_CALL)])
    killed = _kill_when_stalled(first, workdir / MARKER)
    print(first.communicate(timeout=120)[0])

    problems = []
    if not killed or first.returncode != -signal.SIGKILL:
        return _verdict("resume", [f"process 1 exited {first.returncode}, not by "
                                   "SIGKILL — nothing was interrupted"], "")

    logs = _sessions(workdir)
    if len(logs) != 1:
        problems.append(f"expected one session file, found {[p.name for p in logs]}")
    survived = sorted(p.name for p in workdir.glob("part*.txt"))
    print(f"[on disk] {len(logs)} session file, files so far: {survived}")

    # What the next process will actually be handed, and the reason this check
    # exists: an unanswered call, answered honestly by repair.
    recovered = session.load(str(logs[0]))
    notices = [m for m in recovered if m.get("text") == session.INTERRUPTED]
    calls = sum(len(m.get("tool_calls") or []) for m in recovered)
    results = sum(1 for m in recovered if m.get("role") == "tool")
    print(f"[loaded] {len(recovered)} messages, {calls} calls / {results} results, "
          f"{len(notices)} interrupted")
    if not notices:
        problems.append("the killed call was never answered by repair")
    if calls != results:
        problems.append(f"{calls} tool calls but {results} results — unsendable")

    print("\n--- process 2: resume() in the same directory, then continue ---")
    second = _spawn(["_continue", str(workdir)])
    output = second.communicate(timeout=900)[0]
    print(output)
    if second.returncode != 0:
        problems.append(f"process 2 exited {second.returncode}")
    if session.INTERRUPTED not in output:
        problems.append("the resumed transcript never showed the interruption notice")

    # Same file, still. Appending to the transcript it recovered is what makes
    # --resume idempotent; a second file would mean the history forked.
    logs = _sessions(workdir)
    if len(logs) != 1:
        problems.append(f"resuming forked the log: {[p.name for p in logs]}")

    missing = [name for name in (*PARTS, "SUMMARY.md")
               if not (workdir / name).is_file()]
    if missing:
        problems.append(f"missing at the end: {missing}")
    else:
        summary = (workdir / "SUMMARY.md").read_text()
        print(f"--- SUMMARY.md ---\n{summary}")
        unlisted = [name for name in PARTS if name not in summary]
        if unlisted:
            problems.append(f"SUMMARY.md does not describe {unlisted}")

    return _verdict("resume", problems,
                    f"kill -9 with a call outstanding, {len(notices)} interrupted "
                    f"call(s) answered, {len(recovered)} messages recovered into "
                    "the same log, all six files present")


def _work(workdir, kill_on):
    """Child half of the resume check: run the task, then stall mid-call.

    The stall is how a kill -9 is aimed. Athena's tools finish in microseconds, so
    a signal sent from outside would land between calls almost every time — and
    between calls the transcript is already coherent, which is the easy case.
    Blocking inside the tool_start event holds the process exactly where the
    damage is interesting: the assistant turn is on disk, its result never will
    be. The kill itself comes from another process, and is a real SIGKILL.
    """
    seen = []

    def on_event(kind, payload):
        show(kind, payload)
        if kind == "tool_start":
            seen.append(payload["name"])
            if len(seen) >= int(kill_on):
                Path(workdir, MARKER).write_text(str(os.getpid()))
                print(f"\n*** stalled before running call {len(seen)} "
                      f"({payload['name']}) — waiting to be killed ***", flush=True)
                # Long enough that the parent always wins the race, short enough
                # that a forgotten child eventually dies on its own.
                time.sleep(600)

    harness = Harness(workdir=workdir, on_event=on_event)
    print(f"\n{harness.run(TASK)}", flush=True)


def _continue(workdir):
    """Child half two: load what the corpse left and finish the job."""
    harness = Harness(workdir=workdir, on_event=show)
    if not harness.resume():
        print("[resume] nothing to resume", flush=True)
        return 1
    print(f"[resumed] {len(harness.messages)} messages from "
          f"{os.path.basename(harness.session_path)}", flush=True)
    for message in harness.messages:
        if message.get("text") == session.INTERRUPTED:
            print(f"[repaired] {message['name']}: {message['text']}", flush=True)
    print(f"\n{harness.run('continue the task')}", flush=True)
    return 0


def _kill_when_stalled(proc, marker):
    """Wait for the child to say it is stalled, then kill -9 it. Really."""
    for _ in range(1200):
        if marker.exists():
            subprocess.run(["kill", "-9", str(proc.pid)], check=False)
            print(f"*** kill -9 {proc.pid} ***", flush=True)
            return True
        if proc.poll() is not None:
            # It finished the whole task before reaching the stall point.
            return False
        time.sleep(0.5)
    proc.kill()
    return False


# -------------------------------------------------------------- check: delegate

def check_delegate():
    """(2) Two children write the code, the parent runs the tests itself."""
    workdir = _fresh("day5_delegate")
    print(f"[workdir] {workdir}\n[user] {DELEGATE_TASK}\n")

    parent = Harness(workdir=workdir, on_event=show)
    answer = parent.run(DELEGATE_TASK)
    print(f"\n=== final answer ===\n{answer}")

    problems = []
    used = [call["name"] for message in parent.messages
            for call in message.get("tool_calls") or []]
    print(f"\n[parent's own calls] {used}")
    if used.count("spawn_agent") != 2:
        problems.append(f"spawn_agent was called {used.count('spawn_agent')} times, not 2")

    # "Runs the tests itself" is a claim about the parent's transcript, not about
    # the answer text — an agent can report a test run it delegated.
    ran = [call["args"].get("command", "") for message in parent.messages
           for call in message.get("tool_calls") or [] if call["name"] == "bash"]
    if not any("test_utils.py" in command for command in ran):
        problems.append(f"the parent never ran the tests itself: {ran}")

    for name in ("utils.py", "test_utils.py"):
        if not (workdir / name).is_file():
            problems.append(f"{name} was never written")
    if not problems:
        source = (workdir / "test_utils.py").read_text()
        asserts = source.count("assert ")
        print(f"--- test_utils.py ({asserts} asserts) ---\n{source}")
        if asserts < 5:
            problems.append(f"test_utils.py has {asserts} asserts, not five")
        # The tests are the deliverable, so they get run here too, by us.
        done = subprocess.run([sys.executable, "test_utils.py"], cwd=workdir,
                              capture_output=True, text=True, timeout=120)
        print(f"[verified] python3 test_utils.py -> exit {done.returncode} "
              f"{(done.stdout + done.stderr).strip()[:200]}")
        if done.returncode != 0:
            problems.append(f"the tests do not pass: {done.stdout + done.stderr}")

    # The whole reason a child harness is built with persist=False.
    logs = _sessions(workdir)
    print(f"[sessions] {[p.name for p in logs]}")
    if len(logs) != 1:
        problems.append(f"expected exactly one session file, found {len(logs)}: "
                        f"{[p.name for p in logs]} — a child hijacked --resume")

    return _verdict("delegate", problems,
                    f"parent called {used}, ran the tests itself, and the two "
                    "children left no session behind")


# ----------------------------------------------------------------- check: fleet

def check_fleet():
    """(3) Two agents in two directories, and a failure that stays a result."""
    root = _fresh("day5_fleet")
    jobs = [
        {"name": "greet", "workdir": str(root / "greet"),
         "task": "Write hello.py printing exactly 'hello' and run it to prove it works"},
        {"name": "add", "workdir": str(root / "add"),
         "task": "Write add.py with a main that prints 2+2 and run it to prove it works"},
    ]
    print(f"[fleet] {len(jobs)} jobs under {root}")

    started = time.time()
    results = run_fleet(jobs, lambda workdir: Harness(workdir=workdir), max_workers=4)
    print(f"[fleet] finished in {time.time() - started:.0f}s")

    problems = []
    if [r["name"] for r in results] != [j["name"] for j in jobs]:
        problems.append(f"results out of order: {[r['name'] for r in results]}")
    for result in results:
        print(f"\n--- {result['name']} ok={result['ok']} ---\n{result['report'][:300]}")
        if not result["ok"]:
            problems.append(f"{result['name']} failed: {result['report']}")

    for job, expected in zip(jobs, ("hello.py", "add.py")):
        if not Path(job["workdir"], expected).is_file():
            problems.append(f"{job['name']} never wrote {expected}")
    # Isolation: each job's sandbox holds its own file and not the other's.
    if Path(jobs[0]["workdir"], "add.py").exists():
        problems.append("the jobs wrote into each other's directories")

    # The failure path, free of charge: a factory that raises must produce a
    # result, and must not stop the fleet.
    def explode(workdir):
        raise RuntimeError("no API key")

    failed = run_fleet([{"name": "doomed", "workdir": str(root), "task": "anything"}],
                       explode)
    print(f"\n[failure path] {failed}")
    if failed != [{"name": "doomed", "ok": False, "report": "RuntimeError: no API key"}]:
        problems.append(f"a raising job was not turned into a result: {failed}")

    return _verdict("fleet", problems,
                    f"{len(results)} agents ran in parallel in their own "
                    "directories, in order, and a raising job came back as data")


# ----------------------------------------------------------------------- harness

def _fresh(name):
    """An empty scratch directory, so every run starts from nothing."""
    workdir = ROOT / name
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    return workdir


def _sessions(workdir):
    """Every transcript in a working directory, sorted."""
    return sorted(Path(workdir, session.SESSION_DIR).glob("*.jsonl"))


def _spawn(args):
    """Start this file's child half in a real process, line-buffered."""
    return subprocess.Popen([sys.executable, __file__, *args],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


def _verdict(name, problems, summary):
    """Print PASS with what was proven, or FAIL with every reason."""
    if problems:
        print(f"\n### FAIL {name}")
        for problem in problems:
            print(f"  - {problem}")
        return False
    print(f"\n### PASS {name}: {summary}")
    return True


CHECKS = {"resume": check_resume, "delegate": check_delegate, "fleet": check_fleet}

# The two child modes of the resume check, which run in their own processes.
CHILDREN = {"_work": _work, "_continue": _continue}


def main():
    """Run the named checks, or all of them."""
    if len(sys.argv) > 1 and sys.argv[1] in CHILDREN:
        return CHILDREN[sys.argv[1]](*sys.argv[2:]) or 0

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
