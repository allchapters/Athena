"""Day 5 — the front door: the terminal an agent is actually used from.

Concept this file teaches: an interface is a *set of promises about what the user
can see and stop*. The harness can already do everything; what a person needs on
top of it is narrower and non-negotiable — show me what you are about to do, let
me refuse it, let me interrupt you, and do not lose my work when I do.

So three forms, one object behind all of them:

    python3 -m athena                          interactive, safe mode, asks
    python3 -m athena -p "task"                headless, yolo, for scripts
    python3 -m athena --resume                 carry on from the last session

Design rules:
  * Modes default to their context. A human at a prompt gets safe, because they
    are there to be asked; a script gets yolo, because nobody is there to answer.
  * Show the call, not the payload. A 300-line write_file rendered in full drowns
    the decision the user is being asked to make.
  * Deny by default at every dead end. EOF, Ctrl-C, an empty answer, anything
    that is not a yes, all mean no.
  * Ctrl-C is a feature. The transcript is already on disk, so interrupting costs
    a turn, never a session — and the message says so, because a user who does
    not know that will not dare press it.
  * Printing is never the reason a run fails. Everything here clips, and nothing
    here raises.
"""

import argparse
import os
import sys

from .harness import Harness
from .security import MODES, Policy

# Clipping ceilings — a terminal line, not a context window.
MAX_ARG_CHARS = 60
MAX_RESULT_CHARS = 140

# Dim for tool output, so the agent's own words stay the thing you read. Only
# when a terminal is listening: escape codes in a redirected log are noise.
DIM, BOLD, RESET = ("\033[2m", "\033[1m", "\033[0m") if sys.stdout.isatty() else ("", "", "")

BANNER = """{bold}Athena{reset} — model {model}, mode {mode}
  jail: {workdir}
  Ctrl-C interrupts a run, Ctrl-D exits."""


def main(argv=None) -> int:
    """Parse arguments, build one Harness, and hand it to one of the two modes."""
    args = _parse(argv)
    # The default that matters most in this file: -p means no human is watching.
    mode = args.mode or ("yolo" if args.prompt else "safe")

    harness = Harness(workdir=args.workdir, model=args.model,
                      policy=Policy(mode, approver=_approve),
                      on_event=_show, max_turns=args.max_turns)

    if args.resume is not None:
        if harness.resume(args.resume or None):
            print(f"{DIM}[resumed] {len(harness.messages)} messages from "
                  f"{os.path.relpath(harness.session_path, harness.workdir)}{RESET}")
        else:
            print(f"{DIM}[resume] no previous session in {harness.workdir}{RESET}")

    if args.prompt:
        return _headless(harness, args.prompt)
    return _interactive(harness, mode)


def _parse(argv):
    """Define the command line. Every default is a decision, so each is stated."""
    parser = argparse.ArgumentParser(
        prog="athena", description="The smallest agent harness that still works.")
    parser.add_argument("-p", "--prompt",
                        help="run this task headlessly and exit (default: yolo mode)")
    parser.add_argument("-d", "--workdir", default=".",
                        help="the directory the agent is sandboxed to (default: .)")
    parser.add_argument("-m", "--model", help="override ATHENA_MODEL")
    parser.add_argument("--mode", choices=MODES,
                        help="safe asks before writing, yolo never asks, "
                             "read-only refuses to write "
                             "(default: safe, or yolo with -p)")
    # nargs="?" so --resume alone means "the latest one", which is what it means
    # to a person who has just had a process die on them.
    parser.add_argument("--resume", nargs="?", const="", default=None,
                        metavar="SESSION",
                        help="continue the last session, or the given .jsonl file")
    parser.add_argument("--max-turns", type=int, default=120,
                        help="hard stop on model turns in one run (default: 120)")
    return parser.parse_args(argv)


def _headless(harness, prompt) -> int:
    """Run one task and print its answer. For scripts, cron, and CI."""
    answer = harness.run(prompt)
    print(f"\n{answer}")
    return 0


def _interactive(harness, mode) -> int:
    """Read tasks until end of input, running each in the same conversation.

    The same Harness across turns is the point: the second task can say "now do
    the same to the other file" and the agent knows what that means.
    """
    print(BANNER.format(bold=BOLD, reset=RESET, model=harness.model, mode=mode,
                        workdir=harness.workdir))
    while True:
        try:
            task = input(f"\n{BOLD}athena>{RESET} ").strip()
        except EOFError:
            # Ctrl-D. The conversation is on disk; there is nothing to save.
            print("\nbye")
            return 0
        except KeyboardInterrupt:
            # Ctrl-C at an empty prompt clears the line. It does not quit — a
            # reflex keypress must not end a session someone was working in.
            print()
            continue

        if not task:
            continue
        if task in ("exit", "quit"):
            return 0

        try:
            print(f"\n{harness.run(task)}")
        except KeyboardInterrupt:
            # Interrupting a run is normal and safe, and this is where that
            # promise is either kept or quietly broken. Every message up to now
            # is already a line in the file.
            print(f"\n{DIM}[interrupted] the session log is safe: "
                  f"{_where(harness)}\n"
                  f"  athena --resume continues it.{RESET}")
        except RuntimeError as exc:
            # A provider failure that already exhausted its retries. Report it
            # and keep the prompt open: the next task may well work.
            print(f"{DIM}[error] {exc}{RESET}", file=sys.stderr)


def _show(kind, payload):
    """Print one loop event: what was said, what was called, what came back."""
    if kind == "assistant":
        if payload["text"]:
            print(f"\n{payload['text']}")
        for call in payload["tool_calls"]:
            print(f"\n{_call_line(call)}")
    elif kind == "tool_end":
        print(f"{DIM}  {_head(payload['result'])}{RESET}")


def _approve(call, reason) -> bool:
    """Ask the human. Anything that is not a yes is a no.

    This is the approver Policy calls in safe mode, and it is the only place in
    Athena where a person is in the loop. `reason` is the policy's own summary of
    the call; the line above it is this file's, and shows the same call the way
    every other call in the transcript has been shown — so what is being approved
    looks like what has already been happening.
    """
    print(f"\n{_call_line(call)}")
    try:
        answer = input(f"  approve {reason} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # No terminal, or the user pressed Ctrl-C rather than answer. Both are
        # refusals, and neither should take the process down.
        print()
        return False
    return answer in ("y", "yes")


def _call_line(call) -> str:
    """Render a tool call as one line: the name, and the gist of its arguments."""
    args = ", ".join(f"{key}={_clip(value, MAX_ARG_CHARS)}"
                     for key, value in (call.get("args") or {}).items())
    return f"→ {call['name']}({args})"


def _head(result) -> str:
    """The first line of a tool result, clipped, with what was left off named.

    One line is enough to see that a write succeeded or a command failed, and the
    full result is in the transcript for when it is not. Saying how much was
    hidden matters more than showing it: a silently truncated result reads as a
    complete one.
    """
    lines = str(result).strip().splitlines() or [""]
    head = _clip(lines[0], MAX_RESULT_CHARS, quote=False)
    if len(lines) > 1:
        head += f" (+{len(lines) - 1} lines)"
    return head or "(no output)"


def _clip(value, limit, quote=True) -> str:
    """One-line, length-capped rendering of any value.

    Newlines become "\\n" rather than wrapping: a tool call has to stay one line
    to be scannable, and a multi-line argument that reflows the terminal hides
    the calls around it.
    """
    text = str(value).replace("\n", "\\n")
    if len(text) > limit:
        text = f"{text[:limit]}…"
    return repr(text) if quote else text


def _where(harness) -> str:
    """The session path, relative to the jail when it is inside it."""
    if not harness.session_path:
        return "(nothing written yet)"
    return os.path.relpath(harness.session_path, harness.workdir)
