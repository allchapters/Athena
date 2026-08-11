"""Day 3 — the context engine: keeping a long run inside a finite window.

Concept this file teaches: context is a *budget*, and an agent that works long
enough will always spend it. The fix is not a bigger window — it is deciding, on
purpose, what gets forgotten. Compaction replaces the old middle of a
conversation with a written summary and keeps the recent turns verbatim, so the
agent loses transcript but not knowledge.

Design rules:
  * Measure before acting. Under budget, compact() returns the very list it was
    given — the cheap path is also the common path, and identity is how a caller
    can tell that nothing happened.
  * Recent turns stay verbatim. Summaries are for what is settled; the last few
    messages are what the model is mid-thought about, and paraphrasing them is
    how an agent loses its place.
  * Never strand a tool result. A "tool" message only means something after the
    assistant turn that called for it, so a kept slice may not start with one.
  * Failure means keep everything. If the summary comes back empty, the honest
    move is to return the history intact rather than to trade it for nothing.
  * One provider call, and it is the only thing here that is not arithmetic.
"""

from . import provider

# Four characters to a token: wrong in the third digit, right in the first, and
# free. The alternative is a network round trip per turn to count tokens we are
# only comparing against a threshold we chose by feel anyway.
CHARS_PER_TOKEN = 4

# How many messages survive verbatim. Six is about two think-act-observe cycles:
# enough that the model can still see what it just did and what came back.
KEEP_RECENT = 6

# Ceiling per message inside the rendered transcript. One 4000-line read_file
# result would otherwise crowd out fifty decisions worth more than it is.
MAX_MESSAGE_CHARS = 1200

SUMMARY_SYSTEM = (
    "You compress agent transcripts. Preserve: the original task, every file "
    "created or edited and its purpose, key decisions, unresolved errors, and "
    "what remains to be done. Be dense and factual."
)


def estimate_tokens(messages: list) -> int:
    """Estimate the size of a message list in tokens.

    str(message) on purpose: it counts the dict's keys, the tool-call names, and
    the punctuation, all of which are really sent. Being a little pessimistic is
    the right direction for a number used to decide when to make room.
    """
    return sum(len(str(message)) for message in messages) // CHARS_PER_TOKEN


def compact(model: str, messages: list, budget_tokens: int) -> list:
    """Return `messages`, or a compacted version of it that fits the budget.

    Compacts to one summary message plus the last KEEP_RECENT turns. Returns the
    original list object — not a copy — whenever it declines to act, so a caller
    can test `result is messages` to see whether compaction fired.
    """
    # A list this short has no old middle to summarise: the summary message plus
    # the kept slice would be as long as what we started with.
    if len(messages) <= KEEP_RECENT + 1 or estimate_tokens(messages) <= budget_tokens:
        return messages

    old, recent = messages[:-KEEP_RECENT], messages[-KEEP_RECENT:]

    # A leading tool result would be an answer to a question no longer in the
    # transcript, and Gemini rejects a functionResponse with no call before it.
    # Hand them back to `old` rather than deleting them — they still get read
    # into the summary, they just stop being loose parts.
    while recent and recent[0].get("role") == "tool":
        old.append(recent.pop(0))

    reply = provider.complete(model, SUMMARY_SYSTEM, [{
        "role": "user",
        "text": f"Compress this transcript:\n\n{_render(old)}",
    }])
    summary = (reply["text"] or "").strip()
    if not summary:
        # The model had nothing to say. Losing the history would be worse than
        # exceeding the budget, and the next turn will try this again.
        return messages

    return [{"role": "user", "text": f"[Conversation so far, compacted]\n{summary}"}] + recent


def _render(messages: list) -> str:
    """Flatten messages into a plain transcript for the summariser to read.

    Plain text, not JSON: the model is being asked to read this, and every brace
    and escaped newline is budget spent on syntax instead of on content. Tool
    calls are named but their arguments dropped — that a file was written is the
    fact worth keeping; the 300 lines that went into it are already on disk.
    """
    lines = []
    for message in messages:
        role = message.get("role", "?")
        # The tool's name is the only thing that says which result this is.
        label = f"{role}({message['name']})" if message.get("name") else role
        parts = []
        text = str(message.get("text") or "").strip()
        if text:
            if len(text) > MAX_MESSAGE_CHARS:
                cut = len(text) - MAX_MESSAGE_CHARS
                text = f"{text[:MAX_MESSAGE_CHARS]}... [{cut} chars omitted]"
            parts.append(text)
        names = [call["name"] for call in message.get("tool_calls") or []]
        if names:
            parts.append(f"[calls {', '.join(names)}]")
        lines.append(f"{label}: {' '.join(parts)}" if parts else f"{label}: (empty)")
    return "\n".join(lines)
