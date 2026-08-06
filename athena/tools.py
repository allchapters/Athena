"""Day 2 — tools: the hands. Everything the agent can actually do lives here.

Concept this file teaches: a tool is a *contract with two halves that must not
drift*. The schema is what the model is told; the function is what happens. Day 1
wrote both by hand and they could disagree; today the decorator derives the
schema from the function, so they cannot.

Design rules:
  * One derivation — the signature is the source of truth for what is required.
  * Every parameter is a string. JSON gives us strings anyway, and a schema that
    lies about types produces arguments the tool has to guess at.
  * One gate for paths. Every tool resolves through resolve(), so the sandbox is
    a single line to read and a single line to get wrong.
  * A refusal is a *return value*, not an exception, wherever the model can fix
    it by trying again — and an exception where it must not proceed at all.
  * Output is bounded. A tool that can return a gigabyte can destroy the context
    window the agent thinks with, so every result has a ceiling.
"""

import fnmatch
import inspect
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

# Ceilings. Each is a context-window budget, not a filesystem limit.
MAX_READ_LINES = 4000
MAX_BASH_CHARS = 12000       # kept as first 6000 + last 6000
MAX_LIST_FILES = 500
MAX_GREP_HITS = 200
MAX_GREP_LINE = 200

# Directories that are never worth walking: machine output, not source.
IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv"}


@dataclass
class Tool:
    """A name, the schema the provider is given, and the code that runs.

    Deliberately the same shape day 1's hand-written class exposed, so the loop
    never learns that tools are generated now.
    """

    name: str
    spec: dict
    run: Callable


def tool(description: str, **params: str):
    """Turn a plain function into a Tool, deriving its schema from its signature.

    `description` is what the model reads to decide *whether* to call; each
    keyword is `param="what that argument means"`, which is what the model reads
    to decide *what to pass*. Parameters with defaults are optional, because a
    default is already the author saying "this one may be left out".
    """
    def decorate(fn: Callable) -> Tool:
        required = [name for name, param in inspect.signature(fn).parameters.items()
                    if param.default is inspect.Parameter.empty]
        return Tool(name=fn.__name__, spec={"schema": {
            "name": fn.__name__,
            "description": description,
            "parameters": {
                "type": "object",
                # String everywhere. The model then sends "3", not 3, and each
                # tool coerces once, at the only place that knows the meaning.
                "properties": {param: {"type": "string", "description": text}
                               for param, text in params.items()},
                "required": required,
            },
        }}, run=fn)
    return decorate


def core_tools(workdir: str) -> list[Tool]:
    """Build the six filesystem-and-shell tools, sandboxed to `workdir`.

    They are closures on purpose: the working directory is captured once, here,
    and no tool takes it as an argument. The model therefore cannot ask for a
    different sandbox, because there is no parameter through which to ask.
    """
    # realpath once, up front: comparing a real path against a symlinked one is
    # how sandboxes leak, and /tmp is a symlink on macOS.
    root = os.path.realpath(workdir)
    os.makedirs(root, exist_ok=True)

    def resolve(path: str) -> str:
        """Return the absolute real path of `path`, or refuse to leave the box.

        This is the whole sandbox. It resolves *before* checking, so "..", a
        symlink out, and an absolute path are all the same case. It raises rather
        than returning a message: an escape is not something the model should
        retry differently, and the loop already turns the raise into a tool
        result the model can read.
        """
        full = os.path.realpath(os.path.join(root, path))
        if full != root and not full.startswith(root + os.sep):
            raise PermissionError(f"{path!r} escapes the working directory")
        return full

    def walk() -> list[tuple[str, str]]:
        """Yield (relative, absolute) for every file under root, ignores pruned."""
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            # Mutate in place — that is how os.walk is told not to descend.
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            for name in filenames:
                full = os.path.join(dirpath, name)
                found.append((os.path.relpath(full, root), full))
        return found

    @tool("Read a file. Returns the contents with line numbers.",
          path="Path to the file, relative to the working directory")
    def read_file(path):
        """Return "N<TAB>line" for each line, truncating a very long file.

        The numbers are not decoration: they are how the model refers to a place
        in the file when it explains a change, and how a human checks it.
        errors="replace" because a tool that raises on one odd byte is a tool the
        agent learns to stop using.
        """
        with open(resolve(path), errors="replace") as handle:
            lines = handle.read().splitlines()
        body = [f"{n}\t{line}" for n, line in enumerate(lines[:MAX_READ_LINES], 1)]
        if len(lines) > MAX_READ_LINES:
            body.append(f"... truncated: showed {MAX_READ_LINES} of {len(lines)} lines")
        return "\n".join(body)

    @tool("Write a file, creating parent directories and overwriting any existing file.",
          path="Path to the file, relative to the working directory",
          content="The complete new contents of the file")
    def write_file(path, content):
        """Create the file and confirm the size, so a silent no-op is visible."""
        full = resolve(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(content)
        return f"Wrote {len(content)} chars to {path}"

    @tool("Replace an exact snippet in a file. The snippet must appear exactly once.",
          path="Path to the file, relative to the working directory",
          old="The exact text to replace, copied from the file",
          new="The text to put in its place")
    def edit_file(path, old, new):
        """Edit by unique snippet, refusing anything ambiguous.

        Exactly-once is the entire safety property. Zero matches means the model
        is editing a file it imagined; several means it cannot know which one it
        meant. Both come back as text it can act on — read the file, add context
        — because a retry here is exactly the right move.
        """
        full = resolve(path)
        with open(full, errors="replace") as handle:
            text = handle.read()
        hits = text.count(old)
        if hits == 0:
            return "ERROR: snippet not found — read the file and copy it exactly"
        if hits > 1:
            return (f"ERROR: snippet appears {hits} times — "
                    "include more context to make it unique")
        with open(full, "w") as handle:
            handle.write(text.replace(old, new, 1))
        return f"Edited {path}"

    @tool("Run a shell command in the working directory and return its output.",
          command="The shell command to run",
          timeout="Seconds to allow before killing it (default 120)")
    def bash(command, timeout="120"):
        """Run a command to completion, returning what a human would have seen.

        stdout and stderr are combined because the model needs the traceback and
        the print in the order they happened. Nothing here raises: a failing
        command is the normal way to learn something, and the exit code is data.
        """
        seconds = int(float(timeout))
        try:
            done = subprocess.run(command, shell=True, cwd=root, capture_output=True,
                                  text=True, errors="replace", timeout=seconds)
        except subprocess.TimeoutExpired:
            # A hung command is the one failure with no output worth keeping.
            return f"ERROR: timed out after {seconds}s"
        output = (done.stdout + done.stderr).strip()
        if len(output) > MAX_BASH_CHARS:
            half = MAX_BASH_CHARS // 2
            cut = len(output) - MAX_BASH_CHARS
            # Head and tail, never the middle: the command echoes at the top and
            # the error that matters is almost always at the bottom.
            output = f"{output[:half]}\n... [{cut} chars truncated] ...\n{output[-half:]}"
        # An empty result still has to say something, or the model reads silence
        # as failure and runs it again.
        return output or f"(exit {done.returncode}, no output)"

    @tool("List files in the working directory, optionally filtered by a glob.",
          pattern="Glob to match, e.g. '*.py' or 'src/**/*.ts' (default all files)")
    def list_files(pattern="**/*"):
        """Return matching paths, sorted, capped.

        Sorted because a stable listing is one the model can compare against a
        later one. Capped with a count rather than trimmed silently, so a
        truncated tree never reads as a complete one.
        """
        hits = sorted(rel for rel, _ in walk() if _matches(rel, pattern))
        if len(hits) > MAX_LIST_FILES:
            hits = hits[:MAX_LIST_FILES] + [f"... and {len(hits) - MAX_LIST_FILES} more"]
        return "\n".join(hits) or f"(no files match {pattern})"

    @tool("Search file contents for a regular expression.",
          regex="Python regular expression to search for",
          pattern="Glob limiting which files to search (default all files)")
    def grep(regex, pattern="*"):
        """Return "path:lineno: text" for each match, clipped and capped.

        Line-by-line, and only ever a slice of the line: one minified bundle on a
        single line would otherwise cost more context than every other hit put
        together. An unsearchable file is skipped, not reported — the agent asked
        about contents, not about permissions.
        """
        matcher = re.compile(regex)
        hits = []
        for rel, full in sorted(walk()):
            if not _matches(rel, pattern):
                continue
            try:
                with open(full, errors="replace") as handle:
                    lines = handle.read().splitlines()
            except OSError:
                continue
            for lineno, line in enumerate(lines, 1):
                if matcher.search(line):
                    hits.append(f"{rel}:{lineno}: {line[:MAX_GREP_LINE]}")
                    if len(hits) >= MAX_GREP_HITS:
                        hits.append(f"... stopped at {MAX_GREP_HITS} matches")
                        return "\n".join(hits)
        return "\n".join(hits) or f"(no matches for {regex})"

    return [read_file, write_file, edit_file, bash, list_files, grep]


def _matches(rel: str, pattern: str) -> bool:
    """Test one relative path against a glob the way the caller meant it.

    fnmatch knows nothing about directories — its `*` crosses `/` freely — so a
    literal test is wrong in both directions. Testing the basename too lets
    "*.py" find nested files; testing with a leading "/" lets the "**/" that
    people habitually write still match a file at the top level.
    """
    return (fnmatch.fnmatch(rel, pattern)
            or fnmatch.fnmatch("/" + rel, pattern)
            or fnmatch.fnmatch(os.path.basename(rel), pattern))
