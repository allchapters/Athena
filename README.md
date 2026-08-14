# Athena

The smallest agent harness that still does the real things.

Ten files are the agent — a model boundary, a loop, tools, a policy, a context
engine, memory, skills, sessions, sub-agents, and the object that composes them.
Four more are how you reach it: a CLI, a fleet runner, and the two files that
make `python3 -m athena` work. **Zero dependencies**: the standard library, and
one HTTPS call.

It is written to be read. Every file opens with the concept it teaches and the
design rules it follows, and no file is longer than a sitting. If you have ever
wanted to know what is actually inside a coding agent, the answer is about 1,600
lines, and they are all here.

```python
from athena import Harness

print(Harness(workdir="build").run("write a prime sieve, test it, and fix what fails"))
```

## Running it

One environment variable, then one of three forms.

```bash
export ATHENA_API_KEY=...          # or GEMINI_API_KEY
export ATHENA_MODEL=...            # optional; defaults to gemini-3.1-pro-preview
```

```bash
python3 -m athena                              # interactive, safe mode: asks before it writes
python3 -m athena -p "fix the failing test"    # headless, yolo mode: for scripts and CI
python3 -m athena --resume                     # carry on from the last session in this directory
```

The flags that matter:

| Flag | Meaning |
| --- | --- |
| `-d, --workdir DIR` | the directory the agent is sandboxed to. It cannot read or write outside it |
| `--mode safe\|yolo\|read-only` | ask before every change, never ask, or refuse every change |
| `-m, --model` | override `ATHENA_MODEL` for one run |
| `--resume [SESSION]` | continue the newest transcript, or a named `.jsonl` |
| `--max-turns N` | hard stop on model turns in one run (default 120) |

`safe` is the default when a human is at the prompt and `yolo` when `-p` is
given, because an approver with nobody to ask can only say no. Destructive shell
commands — `sudo`, `rm -rf /`, `curl | sh`, `git push --force` — are refused in
every mode, including `yolo`.

Every run appends to `.athena/sessions/<timestamp>-<task>.jsonl` in the working
directory, one JSON line per message, as it happens. That file is the agent's
entire state: kill the process at any moment and `--resume` picks the
conversation back up, including telling the model which tool call died unanswered.

## Anatomy

Built in five days, one concept at a time. Read it in this order.

| Day | Files | What it adds |
| --- | --- | --- |
| 1 | `provider.py`, `loop.py` | The one boundary that speaks to a model, and the loop that makes an agent an agent: think, act, observe, repeat. |
| 2 | `tools.py`, `security.py` | Hands. Six filesystem-and-shell tools whose schemas are derived from their signatures, a sandbox that is one `realpath` comparison, and permission as a single pure decision per call. |
| 3 | `context.py`, `memory.py`, `skills.py` | The window is a budget, so the old middle gets summarised. The system prompt is built, not pasted, and `ATHENA.md` carries what outlives a run. Skills advertise a name and load their instructions only when needed. |
| 4 | `session.py`, `subagent.py` | Append-only transcripts, repaired on load — a torn last line dropped, an interrupted tool call answered honestly. And delegation: spend a second context window instead of your own. |
| 5 | `harness.py`, `cli.py`, `fleet.py`, `__init__.py`, `__main__.py` | The object that composes all of the above into two methods, the terminal it is driven from, and the same harness run over many directories at once. |

Each day has a demo under `demos/` that proves its claims against a real model
rather than asserting them — including a real `kill -9` mid-task, and the resume
that survives it.

```bash
source Athena-key.sh && python3 demos/day5_product.py
```

## Composing it

The harness is a library first. Registering a tool is the main extension point,
and it is one decorator: the description is what the model reads to decide
whether to call, and each keyword is what it reads to decide what to pass.

```python
import subprocess

from athena import Harness, tool


@tool("Run this project's test suite and return its output.",
      target="A single test file to run, or leave empty for all of them")
def run_tests(target=""):
    done = subprocess.run(f"python3 -m pytest -q {target}", shell=True,
                          capture_output=True, text=True, cwd="service")
    return (done.stdout + done.stderr)[-4000:]


agent = Harness(
    workdir="service",
    extra_tools=[run_tests],
    system_extra="Always finish by running run_tests, and do not stop while it fails.",
)
print(agent.run("make the auth tests pass without weakening any assertion"))
```

A tool is not only new capability — it is how you tell the agent the one correct
way to do something it could otherwise do five wrong ways with `bash`.

Everything else is a keyword argument: `policy=Policy("safe", approver=...)` to
put a human or a web request in the loop, `budget_tokens=` to change when
compaction fires, `on_event=` to watch every turn, `enable_subagents=False` to
forbid delegation, `session_path=` to write the transcript where you want it.

And when one directory is not enough, `run_fleet` runs N of them in parallel —
each job in its own sandbox, results in input order, and a job that raises comes
back as a result instead of taking the fleet with it.

```python
from athena import Harness, run_fleet

jobs = [{"name": name, "workdir": f"ports/{name}", "task": f"port the client to {name}"}
        for name in ("go", "rust", "typescript")]

for done in run_fleet(jobs, lambda workdir: Harness(workdir=workdir), max_workers=3):
    print(done["name"], "ok" if done["ok"] else "FAILED", done["report"][:200])
```
