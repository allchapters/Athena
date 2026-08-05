"""Day 1 — the agentic loop: think, act, observe, repeat.

Concept this file teaches: an agent is not a prompt, it is a *loop*. The model
asks for tool calls, the harness runs them, the results go back, and the cycle
repeats until the model answers with words instead of calls.

Design rules:
  * The loop owns control flow and nothing else — no prompts, no network.
  * A tool failure is data, not a crash: errors come back as readable results.
  * Every decision is observable (on_event) and interceptable (before_tool).
  * The loop always terminates, and always terminates with an answer.
"""

from . import provider


def run_loop(model, system, messages, tools, on_event, before_tool,
             max_turns=80, before_turn=None) -> str:
    """Drive the model until it stops asking for tools; return its final text.

    `tools` maps name -> Tool, where a Tool exposes .spec (a {"schema": ...}
    dict) and .run (called with the model's args as keyword arguments).
    `on_event(kind, payload)` fires with "assistant", "tool_start", "tool_end".
    `before_tool(call)` returns None to allow or a reason string to block.
    `before_turn(messages)` returns the list to send, replacing it in place —
    unused today; day 3 plugs compaction in here.
    """
    for _ in range(max_turns):
        if before_turn:
            # In place, so the caller's own list reflects the compaction.
            messages[:] = before_turn(messages)
        reply = provider.complete(model, system, messages, [t.spec for t in tools.values()])
        messages.append({
            "role": "assistant", "text": reply["text"], "tool_calls": reply["tool_calls"],
        })
        on_event("assistant", reply)
        if not reply["tool_calls"]:
            return reply["text"]
        # Order matters: the model may have written call 2 expecting call 1 ran.
        for call in reply["tool_calls"]:
            on_event("tool_start", call)
            result = _execute(tools, call, before_tool)
            messages.append({"role": "tool", "name": call["name"], "text": str(result)})
            on_event("tool_end", {"call": call, "result": result})

    # Out of turns. Ask once more with no tools available, so the only move the
    # model has left is to answer — the loop can never return empty-handed.
    messages.append({"role": "user", "text": "Turn limit reached; wrap up now."})
    final = provider.complete(model, system, messages, None)
    messages.append({"role": "assistant", "text": final["text"], "tool_calls": []})
    on_event("assistant", final)
    return final["text"]


def _execute(tools, call, before_tool) -> str:
    """Run one tool call, converting every failure into readable text.

    Nothing here raises. A model that sees "ERROR: KeyError: 'count'" can fix
    its arguments and retry; a model killed by that exception cannot.
    """
    reason = before_tool(call)
    if reason:
        return f"BLOCKED: {reason}"
    tool = tools.get(call["name"])
    if tool is None:
        # Models do invent tool names. Say so plainly and let it try again.
        return f"ERROR: unknown tool {call['name']}"
    try:
        return tool.run(**(call.get("args") or {}))
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
