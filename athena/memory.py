"""Day 3 — memory: the part of the agent that outlives the conversation.

Concept this file teaches: compaction keeps a *run* alive, memory keeps *runs*
connected. Context is per-conversation and always shrinking; a file on disk is
neither. So the durable things — who the agent is, what this project expects,
what it was told once and must not be told again — live in text, and are read
back into the system prompt at the start of every run.

Design rules:
  * The system prompt is *built*, never pasted. One function assembles it, so an
    instruction added here reaches every demo and every day that follows.
  * Memory is a file a human can read, edit, and delete. ATHENA.md sits in the
    working directory in plain markdown; there is no database and no format to
    reverse-engineer, which is what makes it trustworthy.
  * Recall is not a tool call. The memory is already in the system prompt before
    the first turn, so the agent cannot forget to look — remembering costs a
    tool call, remembering *to remember* costs nothing.
  * Absent memory is normal. No ATHENA.md means no section, not an error and not
    an empty heading the model has to interpret.
  * Append, never rewrite. Adding a line cannot corrupt the lines already there.
"""

import os
import platform

# The name is deliberately loud and project-local: a file the agent reads on
# every run should be visible in a directory listing, not hidden in a dotfile.
MEMORY_FILE = "ATHENA.md"

BASE_PROMPT = """You are Athena, a small sharp coding agent. You work inside one
directory, using the tools you have been given.

Act, don't narrate. Do not describe a change you are about to make and then stop
— make it. Do not ask for permission you already have.

Inspect before you assume. Read a file before editing it and list a directory
before believing what is in it; the filesystem is the truth, your memory of it
is not.

Prefer edit_file for small changes. Rewriting a whole file to alter three lines
loses work that was already correct.

Verify after building, by running the thing or by reading it back — never by
reasoning that it should be fine.

Never repeat a failing call unchanged. An error is information: change the
arguments, the approach, or the assumption behind it, then try again.

When the task is complete, reply with a short summary of what you did and stop
calling tools."""


def build_system_prompt(workdir: str, extra: str = "") -> str:
    """Assemble the full system prompt for a run in `workdir`.

    Four sections, blank-line separated, in a deliberate order: who the agent is,
    where it is, what this project has taught it, then whatever the caller is
    adding today. Later sections are more specific than earlier ones, which is
    the order a reader — human or model — resolves a conflict in.
    """
    sections = [BASE_PROMPT, _environment(workdir)]

    memory = read_memory(workdir)
    if memory:
        # Labelled with the filename on purpose. The agent needs to know this
        # text is a file it can add to, not fixed instructions from the author.
        sections.append(f"Project memory ({MEMORY_FILE}):\n{memory}")

    if extra:
        sections.append(extra)
    return "\n\n".join(sections)


def read_memory(workdir: str) -> str:
    """Return the contents of workdir/ATHENA.md, or "" when there is none.

    No file is the common case and not a problem, so it reads as empty rather
    than raising. errors="replace" for the same reason read_file uses it: one
    stray byte in a note must not take down every run in this directory.
    """
    path = os.path.join(workdir, MEMORY_FILE)
    if not os.path.exists(path):
        return ""
    with open(path, errors="replace") as handle:
        return handle.read().strip()


def remember(workdir: str, note: str) -> str:
    """Append one note to workdir/ATHENA.md and confirm it.

    A bullet per note, appended: the file stays a list a person can read, and a
    write can only ever add to it. The return string is what the model sees as a
    tool result, so it names the file — that is how the agent learns where its
    own memory lives and that it may read it directly.
    """
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(workdir, MEMORY_FILE), "a") as handle:
        handle.write(f"- {note}\n")
    return f"Remembered in {MEMORY_FILE}"


def _environment(workdir: str) -> str:
    """State the platform and the real working directory in one line.

    Both matter and neither is guessable. The platform decides whether `wc -l`
    or `Get-Content` is the right call; realpath because the agent's tools have
    already resolved the sandbox, and a prompt naming a symlinked path invites
    it to report a location no later command will match.
    """
    return (f"Platform: {platform.system()}. "
            f"Working directory: {os.path.realpath(workdir)}")
