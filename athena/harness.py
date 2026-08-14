"""Day 5 — the harness: the one object that owns all of it.

Concept this file teaches: *composition*. Nothing here is a new capability. The
loop, the tools, the policy, the context engine, memory, skills, sessions and
sub-agents were each built already, and each was built knowing nothing about the
others. What was missing is the object that holds them — one constructor that
assembles a working agent, and two methods, resume() and run(). Everything above
this file (a CLI, a fleet, a test, someone else's program) talks only to those.

The measure of this file is how little of it there is. Every line is a wire
between two modules that were designed not to need each other; if any line here
starts making a decision of its own, that decision belongs in the module whose
subject it is.

Design rules:
  * Assemble, never reimplement. No prompts, no policy, no persistence format —
    those exist elsewhere and this file's job is to connect them.
  * A default for everything. Harness() with no arguments has to be a real
    agent, because Harness().run("...") is the first thing anyone will type.
  * The transcript is written as it happens. Persisting after the run is
    persisting exactly when it cannot help.
  * A child is ephemeral. A sub-agent shares the sandbox and the policy, and
    writes no session of its own — its errand must never become the thing
    `--resume` picks up.
"""

import os

from . import context, loop, memory, provider, session, skills
from .security import Policy
from .subagent import subagent_tool
from .tools import core_tools, tool

# How much of the task seeds the session filename. Enough to recognise the run
# in a directory listing; session._slug() clips and sanitises it from there.
SLUG_CHARS = 32


class Harness:
    """A working directory, a policy, a set of tools, and a conversation.

    The whole product, in one object. Construct it, then call run(task) as many
    times as you like — the conversation continues across calls, which is what
    makes an interactive session and a resumed one the same code path.
    """

    def __init__(self, workdir=".", model=None, policy=None, extra_tools=None,
                 system_extra="", on_event=None, budget_tokens=600_000,
                 max_turns=120, session_path=None, enable_subagents=True,
                 persist=True, _depth=0):
        # realpath first, and once. Every path the agent touches is checked
        # against this string, and the session lives under it, so a symlinked
        # ".", or /tmp on macOS, must be resolved before anything is built on it.
        self.workdir = os.path.realpath(workdir)
        os.makedirs(self.workdir, exist_ok=True)

        # Environment before default, so a workshop can move everyone to another
        # model with one export and no edit to any file.
        self.model = model or os.environ.get("ATHENA_MODEL") or provider.DEFAULT_MODEL
        # yolo here, safe in the CLI. A library call has no terminal to ask at,
        # and an approver that cannot reach a human denies everything.
        self.policy = policy or Policy("yolo")
        self.on_event = on_event
        self.budget_tokens = budget_tokens
        self.max_turns = max_turns
        self.session_path = session_path
        self.persist = persist
        self.depth = _depth

        self.messages = []
        # How many of self.messages are already on disk. The transcript is
        # append-only, so one integer is the entire bookkeeping.
        self._recorded = 0

        # Kept because a child is built the same way this one was: whatever was
        # added to the parent's toolkit is part of what "this agent" means.
        self.system_extra = system_extra
        self.extra_tools = extra_tools

        self.tools = {t.name: t for t in core_tools(self.workdir)}
        self.tools["remember"] = _remember_tool(self.workdir)
        # Only when there is something to load. A tool for reading skills in a
        # directory with no skills is a tool the model can only misuse.
        if skills.catalog(self.workdir):
            self.tools["use_skill"] = _use_skill_tool(self.workdir)
        if enable_subagents:
            self.tools["spawn_agent"] = subagent_tool(self._make_child, depth=_depth)
        # Last, so a caller can deliberately replace a core tool by name — the
        # composition point the whole object exists to offer.
        for extra in extra_tools or ():
            self.tools[extra.name] = extra

        # Assembled once, here. Anything the agent remembers *during* a run is on
        # disk immediately but reaches the prompt on the next construction — the
        # next process, or the next sub-agent — which is what makes the prompt a
        # fixed thing the whole run can be reasoned about against.
        self.system = memory.build_system_prompt(
            self.workdir, _join(skills.catalog_prompt(self.workdir), system_extra))

    def resume(self, path=None):
        """Load a previous transcript and continue writing to it.

        With no path, the most recently written session in this working directory
        — that is what "carry on where I was" means to someone typing --resume.
        Returns whether there was anything to resume, so the caller can say so.
        """
        path = path or session.latest(self.workdir)
        if not path:
            return False

        self.messages = session.load(path)
        self.session_path = path
        # load() may have *added* messages: repair answers the tool calls the
        # dead process never got to. Those are part of the history now, so count
        # what the file actually holds and let _flush() write the difference —
        # otherwise the file and the list disagree from the first turn on.
        self._recorded = _heal(path)
        self._flush()
        return bool(self.messages)

    def run(self, task):
        """Run one task to completion and return the agent's final text."""
        if self.persist and not self.session_path:
            self.session_path = session.new_session(self.workdir, task[:SLUG_CHARS])

        self.messages.append({"role": "user", "text": task})
        self._flush()

        return loop.run_loop(
            model=self.model,
            system=self.system,
            messages=self.messages,
            tools=self.tools,
            on_event=self._observe,
            before_tool=self.policy.check,
            max_turns=self.max_turns,
            # The context engine, plugged into the socket day 1 left for it.
            before_turn=lambda messages: context.compact(
                self.model, messages, self.budget_tokens),
        )

    def _observe(self, kind, payload):
        """Persist whatever just landed, then tell the caller about it.

        Persisting on the event rather than after the run is the durability
        property: the file is only ever as stale as the last thing that happened.
        Disk first, because a printer that raises must not cost the transcript.
        """
        self._flush()
        if self.on_event:
            self.on_event(kind, payload)

    def _flush(self):
        """Append every message that is not on disk yet.

        The clamp is compaction. compact() can replace the list with a summary
        plus the last few turns, at which point the count of what has been
        written is larger than the list it counts into. Clamping is the right
        answer and not a patch over one: the file is the full history, the list
        is only the window the model thinks in, and what compaction dropped from
        the window was already written down.
        """
        if not self.session_path:
            return
        self._recorded = min(self._recorded, len(self.messages))
        for message in self.messages[self._recorded:]:
            session.append(self.session_path, message)
        self._recorded = len(self.messages)

    def _make_child(self, depth):
        """Factory for spawn_agent: a fresh Harness, one level deeper.

        Same directory, same policy, same tools — a sub-agent must not be a way
        around any of the three. New messages list, which is the entire point:
        the child's context starts empty and the parent's never sees inside it.

        persist=False is the subtle one. A child writes no session file, because
        the newest file in the session directory is what the next --resume picks
        up, and resuming a sub-agent's errand instead of the user's work would be
        a data-loss bug wearing a feature's clothes.
        """
        return Harness(
            workdir=self.workdir, model=self.model, policy=self.policy,
            extra_tools=self.extra_tools, system_extra=self.system_extra,
            on_event=self.on_event, budget_tokens=self.budget_tokens,
            max_turns=self.max_turns, persist=False, _depth=depth)


def _remember_tool(workdir: str):
    """The tool behind the "Project memory" section of the system prompt."""
    @tool("Save a durable fact about this project. It is written to ATHENA.md "
          "and will be in your system prompt in every future conversation here. "
          "Use it for decisions and conventions that outlive this task, not for "
          "progress notes.",
          note="The fact to remember, as one self-contained sentence")
    def remember(note):
        return memory.remember(workdir, note)

    return remember


def _use_skill_tool(workdir: str):
    """The second half of progressive disclosure: the catalogue, then the body."""
    @tool("Load the full instructions for one of the available skills. Call "
          "this before doing work the skill covers, then follow what it says.",
          name="The name of the skill to load")
    def use_skill(name):
        return skills.read_skill(workdir, name)

    return use_skill


def _heal(path: str) -> int:
    """Drop a torn final line from `path`; return how many whole lines remain.

    A process killed mid-write leaves a line with no newline on it. session.load()
    already ignores those bytes — but *appending* after them would glue the next
    message onto the wreckage, turning one lost message into an unreadable tail.
    Resuming means continuing to write to this file, so the file has to be made
    appendable again, and cutting back to the last newline is the only honest way
    to do it: nothing durable is discarded, because a line with no newline was
    never a record of anything.
    """
    if not os.path.exists(path):
        return 0
    with open(path, "rb") as handle:
        data = handle.read()
    # 0 when there is no newline at all, which is the whole file being one torn
    # line — a session that died inside its first write.
    cut = data.rfind(b"\n") + 1
    if cut != len(data):
        os.truncate(path, cut)
    return data[:cut].count(b"\n")


def _join(*sections: str) -> str:
    """Join the non-empty sections with a blank line between them.

    Empty sections are dropped rather than rendered, so a project with no skills
    and no extra instructions produces no headings for the model to interpret.
    """
    return "\n\n".join(section for section in sections if section)
