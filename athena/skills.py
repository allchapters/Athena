"""Day 3 — skills: teaching the agent something new without touching the code.

Concept this file teaches: *progressive disclosure*. Everything an agent might
ever need to know cannot fit in one system prompt, but it does not have to. Give
the model a one-line catalogue of what expertise exists, and let it pull the full
instructions in with a tool call only when the task actually calls for them. The
catalogue costs a few tokens per run; the skill costs nothing until it is used.

A skill is a directory with a SKILL.md in it. That is the entire format. Adding
one is a file, not a release — which is the point being made today.

Design rules:
  * Cheap to advertise, explicit to load. The prompt carries names and one-line
    descriptions; the body arrives only on request.
  * The description is the routing decision. It is all the model has when it
    chooses, so a missing one falls back to the name rather than to silence.
  * Front matter is read, not parsed. A hand-written SKILL.md with a slightly
    wrong header should still work; a YAML dependency to read one line would be
    a worse trade than a lenient scan.
  * No skills is a valid state, and it produces no prompt section at all.
  * A miss lists what does exist. "no skill named foo" leaves the model guessing;
    naming the alternatives lets it fix the call on the next turn.
"""

import os

SKILLS_DIR = "skills"

SKILL_FILE = "SKILL.md"

# How much of a description reaches the catalogue. A skill author who writes a
# paragraph here should not silently buy a paragraph of every future run.
MAX_DESCRIPTION_CHARS = 200

# Front matter is short by convention; scanning the whole file for a stray
# "description:" would let a line of the body impersonate the header.
FRONT_MATTER_LINES = 20

CATALOG_HEADER = "Skills available (load one with the use_skill tool when relevant):"


def catalog(workdir: str) -> dict:
    """Map skill name -> {"description", "path"} for every skill in `workdir`.

    The directory name is the skill's name: it is what the model will pass back
    to use_skill, so the identifier a human types when creating the folder is the
    identifier the agent uses. A directory with no SKILL.md is not a skill and is
    passed over in silence — half-built folders are normal in a working repo.
    """
    root = os.path.join(workdir, SKILLS_DIR)
    if not os.path.isdir(root):
        return {}

    found = {}
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, SKILL_FILE)
        if not os.path.isfile(path):
            continue
        found[name] = {"description": _description(path) or name, "path": path}
    return found


def catalog_prompt(workdir: str) -> str:
    """Render the catalogue as a system-prompt section, or "" when empty.

    Empty string rather than "No skills available": a sentence saying nothing is
    there is budget spent to tell the model not to think about something, and
    build_system_prompt drops a falsy section entirely.
    """
    available = catalog(workdir)
    if not available:
        return ""
    lines = [f"- {name}: {entry['description']}" for name, entry in available.items()]
    return "\n".join([CATALOG_HEADER, *lines])


def read_skill(workdir: str, name: str) -> str:
    """Return the full text of a skill, or an error naming the ones that exist.

    The whole file, front matter included. Trimming it to "just the useful part"
    would mean this file deciding what a skill author meant, and the model reads
    a stray header line without difficulty.
    """
    available = catalog(workdir)
    entry = available.get(name)
    if entry is None:
        names = ", ".join(available) or "(none)"
        return f"ERROR: no skill named {name}. Available: {names}"
    with open(entry["path"], errors="replace") as handle:
        return handle.read()


def _description(path: str) -> str:
    """Pull the "description:" line out of a SKILL.md's front matter.

    A deliberately forgiving read of one convention rather than a strict parse of
    a format: take the first "description:" in the opening lines, strip the
    quotes people put around it, and clip it. Anything unrecognised returns ""
    and the caller falls back to the skill's name, so a malformed header costs a
    good description — never the skill itself.
    """
    with open(path, errors="replace") as handle:
        for _ in range(FRONT_MATTER_LINES):
            line = handle.readline()
            if not line:
                break
            key, sep, value = line.partition(":")
            if sep and key.strip().lower() == "description":
                return value.strip().strip("'\"")[:MAX_DESCRIPTION_CHARS]
    return ""
