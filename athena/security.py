"""Day 2 — the policy: who decides, and how the answer is delivered.

Concept this file teaches: permission is a *pure decision*, made in one place,
before anything happens. check() reads a proposed call and returns either None
(allow) or a sentence saying no. It touches no files, runs no commands, and
prints nothing — which is why it can be read, tested, and trusted.

Design rules:
  * Cheap and total: every call is checked, including the harmless ones.
  * Reasons are for the model. It gets the refusal as a tool result, so the text
    has to explain what happened well enough for it to change course civilly.
  * Order is the design. Denies come first, so no mode can vote past them.
  * The pattern list is a floor, not the wall. Shell is infinitely obfuscatable;
    what actually contains the agent is tools.py's resolve() and, in safe mode,
    a human. Treat DENY_PATTERNS as a guard against catastrophe by accident.
"""

import re

# Tools that only observe. They need no approval because they change nothing —
# and an agent that must ask before looking is one nobody keeps switched on.
READ_TOOLS = {"read_file", "list_files", "grep"}

# Roots that no recursive delete should ever be pointed at.
_RM_ROOT = r"(/|~|\$HOME|\$\{HOME\}|/Users/[^/\s]+|/home/[^/\s]+)"

DENY_PATTERNS = [
    # rm with a recursive flag aimed at a filesystem or home root.
    rf"\brm\b[^;|&]*\s-[a-zA-Z]*[rR][a-zA-Z]*\b[^;|&]*\s{_RM_ROOT}/?\*?\s*$",
    # Privilege escalation: whatever follows, we can no longer reason about it.
    r"\bsudo\b",
    # Writing over a device or filesystem — unrecoverable, never a build step.
    r"\bmkfs\b|\bdd\s+if=",
    # Downloading a script straight into a shell: code nobody read, running now.
    r"\bcurl\b[^|]*\|\s*(sudo\s+)?\w*sh\b",
    # Force-push discards other people's commits, and no local test needs it.
    r"\bgit\s+push\b[^;|&]*(--force\b|-f\b)",
    # Redirection onto a raw disk device.
    r">\s*/dev/sd[a-z]",
]

MODES = ("read-only", "safe", "yolo")


def _refuse(call: dict, reason: str) -> bool:
    """Default approver: say no.

    The default has to be the safe answer, because the dangerous default is the
    one that gets shipped by someone who forgot to pass an approver.
    """
    return False


class Policy:
    """The rules, and whoever gets asked when the rules do not decide.

    `mode` picks how much latitude the agent has; `approver(call, reason) -> bool`
    is how "ask a human" is expressed without this file knowing what a human is —
    it could be a prompt, a web request, or a test that always agrees.
    """

    def __init__(self, mode: str = "safe", approver=None):
        if mode not in MODES:
            # Fail here, loudly. A typo'd mode that silently fell back to
            # permissive would be a security hole spelled as a convenience.
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self.mode = mode
        self.approver = approver or _refuse

    def check(self, call: dict) -> str | None:
        """Return None to allow the call, or a reason to block it.

        This is the signature run_loop's before_tool socket expects, so a Policy
        instance's bound .check *is* the gate — nothing adapts between them.
        """
        name = call.get("name", "")
        args = call.get("args") or {}

        # First, and above every mode: some commands are not the user's to
        # authorise by mistake. yolo does not reach this decision.
        if name == "bash":
            command = str(args.get("command", ""))
            for pattern in DENY_PATTERNS:
                if re.search(pattern, command):
                    return (f"the command matches a denied pattern ({pattern}) — "
                            "destructive commands are refused in every mode")

        # Reading is always allowed; yolo allows everything that survived above.
        if name in READ_TOOLS or self.mode == "yolo":
            return None

        if self.mode == "read-only":
            return f"{name} can modify things and the policy is read-only"

        # safe: the interesting case. We do not decide — we ask, and silence,
        # errors, and anything that is not a yes all mean no.
        if self.approver(call, f"allow {name}({_summarise(args)})?"):
            return None
        return f"{name} was not approved by the user"


def _summarise(args: dict) -> str:
    """Render arguments compactly for a human who has one second to decide.

    A full dump of a 400-line write_file drowns the thing being judged, so long
    values are clipped. The approver can always inspect `call` itself.
    """
    parts = []
    for key, value in args.items():
        text = str(value).replace("\n", "\\n")
        parts.append(f"{key}={text[:80]}..." if len(text) > 80 else f"{key}={text}")
    return ", ".join(parts)
