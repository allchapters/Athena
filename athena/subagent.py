"""Day 4 — sub-agents: spending a second context window instead of your own.

Concept this file teaches: the scarcest thing an agent has is a clean context,
and the cheapest way to get one is to start another agent. "Find every place this
function is called" can cost forty tool results to answer in one sentence. Done
inline, those forty results sit in the window for the rest of the run, crowding
out the actual work. Delegated, the child spends its own window and hands back
the sentence.

That is the whole trade, and it has a price: the child cannot see this
conversation. It gets a task and nothing else, so a vague delegation produces a
confident answer to the wrong question. The tool's description says so plainly,
because that warning is the only thing standing between the model and a
sub-agent sent off to do something it has no way of understanding.

Design rules:
  * A sub-agent is a tool. The model already knows how to use tools; it does not
    need a new mechanism to know how to use another agent.
  * Recursion needs a floor. A sub-agent that can spawn sub-agents is a fork
    bomb with an API bill, so depth is passed down and checked.
  * The refusal is an instruction. "Do this task yourself" tells the model what
    to do next; "depth limit reached" alone invites it to try again.
  * This file does not know what a harness is. It takes a factory and calls it —
    which is why it can be written today and composed with a harness tomorrow.
"""

from .tools import tool


def subagent_tool(make_harness, depth: int = 0, max_depth: int = 2):
    """Build the spawn_agent tool for an agent currently at `depth`.

    `make_harness(depth)` returns a fresh harness — anything with a .run(task)
    that returns a final report. The factory takes the depth so the child builds
    its own spawn_agent one level down, and the limit propagates without any
    global state.
    """
    @tool("Delegate a self-contained task to a fresh sub-agent with its own clean "
          "context. The sub-agent cannot see this conversation, so state "
          "everything it needs in the task itself, including any file paths and "
          "what to report back. Returns the sub-agent's final report. Use this "
          "for work whose intermediate steps you do not need to keep — a wide "
          "search, an exploration, a self-contained piece of implementation.",
          task="The complete task for the sub-agent, written for someone who "
               "knows nothing about this conversation")
    def spawn_agent(task):
        """Run a child agent to completion and return only its final report."""
        if depth >= max_depth:
            # Refused as a tool result, not an exception: the model can still do
            # the work itself, and this sentence tells it to.
            return "ERROR: sub-agent depth limit reached; do this task yourself"
        child = make_harness(depth + 1)
        # Only the return value crosses back. The child's transcript stays in the
        # child — that is the entire reason for delegating.
        return child.run(task)

    return spawn_agent
