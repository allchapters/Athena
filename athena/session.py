"""Day 4 — sessions: making a conversation survive the process that had it.

Concept this file teaches: an agent that only exists in memory is a demo. The
transcript is the agent's entire state — its plan, its findings, its half-done
work — so it is written to disk one line at a time, as it happens, and can be
read back after a crash, a Ctrl-C, or a laptop lid. Append-only JSONL is the
whole mechanism: no schema, no migration, no database, and a file that is still
useful when the program that wrote it is gone.

The interesting half is not saving, it is *loading*. A process killed mid-write
leaves a torn last line, and a process killed between "the model asked for three
tools" and "the third tool answered" leaves a conversation that no provider will
accept — every tool call must have a response. load() fixes both, so resuming is
not a special case the rest of the harness has to know about.

Design rules:
  * Write as you go, not at the end. A transcript flushed on exit is a transcript
    lost exactly when it mattered.
  * A torn tail is expected, not corruption. Stop at the bad line, keep the good
    ones, say nothing — that is what "the process died here" looks like.
  * Repair on load, always. The rule that every tool call has a result is the
    provider's, so satisfying it belongs at the boundary where history re-enters,
    not in the caller that forgot to check.
  * Repair tells the truth. An interrupted call gets a tool result saying it was
    interrupted — inventing a plausible success would have the agent build on
    work that never happened.
"""

import json
import os
import re
import time

SESSION_DIR = os.path.join(".athena", "sessions")

# What an interrupted tool call gets told. Written in the past tense, and naming
# the cause: the model reads this and has to decide whether to run it again, so
# "we don't know if this happened" is the one thing it must not be left thinking.
INTERRUPTED = "Interrupted before this ran (process restarted)."

# Long enough to describe a task, short enough to stay a filename.
MAX_SLUG_CHARS = 40


def new_session(workdir: str, label: str = "session") -> str:
    """Create the session directory and return the path for a fresh transcript.

    The name is timestamp-first so that a plain listing is in chronological
    order, and label-second so a human can tell which run was which. Nothing is
    written yet — an empty session is just a path that does not exist, which is
    exactly what a run that did nothing should leave behind.
    """
    directory = os.path.join(workdir, SESSION_DIR)
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{int(time.time())}-{_slug(label)}.jsonl")


def append(path: str, message: dict) -> None:
    """Append one message to the transcript as a single JSON line.

    One line per message is what makes a partial file readable: a crash can cost
    the last message but never the ones before it. Opened and closed per call so
    the bytes are with the operating system by the time this returns — a held
    handle with a buffer in it is the failure mode this whole file exists to
    avoid. ensure_ascii=False keeps the file readable by a person: an agent that
    wrote a filename in Japanese should not have it stored as \\uXXXX escapes.
    """
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(message, ensure_ascii=False) + "\n")


def load(path: str) -> list:
    """Read a transcript back, tolerating a torn tail, and repair the pairing.

    Line by line rather than one big parse, because the last line of a file
    written by a process that was killed is very often half a line. That is a
    stopping point, not an error: everything before it is intact, and everything
    after it — there is nothing after it — was never durable in the first place.
    """
    if not os.path.exists(path):
        # A session path that was never written to. No history is a valid
        # history, and the caller is about to start a conversation anyway.
        return []

    messages = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                # The torn tail. Stop here — a file cannot be half-broken in the
                # middle, so anything we could salvage past this point would be
                # from a write that raced the one that died.
                break
    return repair(messages)


def latest(workdir: str) -> str | None:
    """Return the most recently written .jsonl in the session directory, or None.

    Most recently *written*, not most recently created: resuming a session
    appends to it, so mtime is what "the one I was just working in" means. The
    filename breaks ties, because two sessions can share a second.
    """
    directory = os.path.join(workdir, SESSION_DIR)
    if not os.path.isdir(directory):
        return None
    paths = [os.path.join(directory, name) for name in os.listdir(directory)
             if name.endswith(".jsonl")]
    if not paths:
        return None
    return max(paths, key=lambda path: (os.path.getmtime(path), path))


def repair(messages: list) -> list:
    """Close any tool calls the previous process never got to answer.

    The rule being enforced: every tool call in an assistant turn has exactly one
    tool message after it. Only the *last* assistant turn can be short — the loop
    runs its calls before asking the model again, so an earlier turn is complete
    by construction. So find that turn, count what answered it, and answer the
    rest with the truth.

    Mutates and returns the list it was given: the caller wants the repaired
    history, and handing back a copy invites a resume that saves the wrong one.
    """
    index = _last_assistant(messages)
    if index is None:
        return messages

    calls = messages[index].get("tool_calls") or []
    # Everything after the assistant turn is that turn's tool results — the loop
    # appends nothing else between a call and its response.
    answered = sum(1 for message in messages[index + 1:]
                   if message.get("role") == "tool")
    for call in calls[answered:]:
        messages.append({"role": "tool", "name": call["name"], "text": INTERRUPTED})
    return messages


def _last_assistant(messages: list) -> int | None:
    """Index of the final assistant message, or None if there isn't one."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "assistant":
            return index
    return None


def _slug(label: str) -> str:
    """Reduce a label to alphanumerics and dashes, clipped.

    A filename built from user text is a path-traversal bug waiting to happen, so
    this is a whitelist and not an escape: "../../etc/passwd" comes out as
    "etc-passwd". Runs of rubbish collapse to one dash and the edges are trimmed,
    so a label that was all punctuation still leaves a usable name.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", label).strip("-")[:MAX_SLUG_CHARS]
    return slug.strip("-").lower() or "session"
