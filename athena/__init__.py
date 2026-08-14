"""Athena — the smallest agent harness that still does the real things.

Ten files are the agent and four are how you reach it. No dependencies. The
package is the tour: read it in the order it was built, and nothing is left over.

    provider.py   one boundary to the model, and Gemini's wire format behind it
    loop.py       think, act, observe, repeat — the agent is a loop
    tools.py      the hands, with their schemas derived from their signatures
    security.py   one pure decision per call: allow, refuse, or ask
    context.py    the window is a budget, so compact the old middle
    memory.py     a built system prompt, and ATHENA.md for what outlives a run
    skills.py     advertise a name, load the instructions only when needed
    session.py    append-only transcripts, repaired on load
    subagent.py   delegate, and spend someone else's context window
    harness.py    the object that composes all of the above
    cli.py        the terminal it is used from
    fleet.py      the same harness, many directories, in parallel

The public surface is deliberately four names plus one function: everything else
is available by importing the module it lives in, and nothing here re-exports for
the sake of it.

    from athena import Harness
    print(Harness(workdir="build").run("write hello.py and run it"))
"""

from .fleet import run_fleet
from .harness import Harness
from .security import Policy
from .tools import Tool, tool

__all__ = ["Harness", "Policy", "Tool", "tool", "run_fleet"]

__version__ = "1.0.0"
