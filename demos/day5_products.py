"""Day 5 proof — three real products, built by Athena, in parallel, in two phases.

This is the day the harness stops being demonstrated and starts being used. Three
projects, three fresh directories, one run_fleet call per phase. Nothing in here
writes a line of the products: every file is produced by an Athena agent driving
the Gemini API through athena/provider.py, and this script only sets the task,
seeds the skill, and then goes looking for evidence.

The two phases are the interesting part, and they are the same mechanism twice:

  build     run_fleet with a fresh Harness per job. Each agent loads the
            design-engineering skill from its own workdir — the skill is a file
            this script writes, not code it imports, which is the whole claim of
            day 3's progressive disclosure.

  review    run_fleet again, with a factory that calls harness.resume() instead
            of starting clean. "A second run in the same session" is exactly what
            resume() means: the same transcript, the same file, appended to. The
            agent can therefore be told "review every file you produced" and know
            what that refers to.

Then the checks, and they are mechanical on purpose — a grep and a word count
cannot be persuaded by a confident final answer. Anything that falls short earns
one more pass with the shortfall named, because the bar does not move.

Run:  source Athena-key.sh && python3 demos/day5_products.py
      source Athena-key.sh && python3 demos/day5_products.py viper
"""

import os
import re
import shutil
import subprocess
import sys
import time
from html import unescape
from pathlib import Path

# demos/ is not a package, so put the repo root on the path to import athena.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from athena import Harness, run_fleet, session, skills  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "scratch" / "products"

# The quality bar, written into every project's workdir as a skill. The clauses
# are verbatim: this file is the specification the agent is held to, and the
# checks at the bottom of this script count the same things it counts.
SKILL = """---
name: design-engineering
description: The quality bar for anything built in this directory — load it before writing a line, and check your work against it before you finish.
---

# Design engineering

Every deliverable in this directory has to clear this bar:

- a real design system as CSS custom properties
- at least 9 distinct sections for a landing page
- at least 1,200 words of real copy, no lorem ipsum
- at least 4 hand-drawn inline SVG illustrations, one being a product artifact in
  the hero
- at least 3 working interactive behaviors
- responsive at 360, 768, and 1280
- semantic HTML with focus states
- a self-review pass before finishing that counts sections, words, SVGs, and
  interactions against these minimums and fixes any shortfall

Notes on how to hold to it:

- "Hand-drawn" means you author the SVG yourself — paths, circles, strokes. No
  icon fonts, no external files, no base64 blobs, no libraries.
- Real copy means sentences a customer would read. Names, places, prices,
  specifics. Never lorem ipsum, and never a paragraph that says nothing.
- A working interactive behavior is one a user can trigger and see respond, wired
  in your own JavaScript.
- The self-review is not a paragraph of reassurance. Count. Then fix what the
  count says is missing, and count again.
"""

BUILD = {
    "artisan-coffee": (
        "Build a self-contained index.html for a specialty coffee roaster in Goa, "
        "India. Load the design-engineering skill first and hold to every line of "
        "it. Give the roastery a real name, a real street address in Goa, and real "
        "prices in rupees. It must have: a sticky nav; a hero containing a "
        "hand-drawn inline SVG of the product itself (a coffee bag); six origin "
        "cards, each with a price; a three-tier subscription table with a working "
        "monthly/annual toggle that changes the prices on screen; brew-guide tabs "
        "that switch panels; an FAQ accordion that opens and closes; and a "
        "dark-mode toggle that persists the choice to localStorage and restores it "
        "on load. One file only — no external CSS, JavaScript, fonts or images. "
        "Finish with the self-review pass the skill demands: count the sections, "
        "the words of visible copy, the inline SVGs and the interactive "
        "behaviours, fix every shortfall, and report the numbers you measured."),
    "taskman": (
        "Build a Python command-line task manager. Load the design-engineering "
        "skill first — the landing-page clauses do not apply to a CLI, but its "
        "self-review clause does, and so does its standard of real, specific "
        "writing. Write taskman.py with argparse subcommands add, list, done, rm "
        "and stats; JSON persistence; aligned table output that stays aligned when "
        "a title is long; and a store path that can be overridden (a --store flag "
        "or an environment variable) so tests can point it at a temporary file. "
        "Then write test_taskman.py: a unittest suite of at least 10 cases that "
        "runs taskman.py through subprocess against a temporary store, covering "
        "each subcommand, the empty-list case, a bad id, and persistence across "
        "two invocations. Run 'python3 -m unittest test_taskman -v' yourself and "
        "do not stop until every test passes. Report the number of tests and the "
        "result you saw."),
    "viper": (
        "Build a canvas snake game as a self-contained index.html. Load the "
        "design-engineering skill first and hold to every line of it: the game is "
        "the hero of a real page, not a bare canvas on a white background. The "
        "game must have grid movement driven by requestAnimationFrame, food that "
        "respawns, a speed increase every 5 foods eaten, a visible live score, a "
        "pause that resumes, a restart, and a high score persisted to localStorage "
        "and shown on load. Around it, build the page the skill asks for — "
        "sections, real copy explaining the game and its rules and its scoring, "
        "hand-drawn inline SVG illustrations, keyboard-accessible controls with "
        "focus states. One file only, no external anything. Finish with the "
        "self-review pass: count sections, words, SVGs and interactions, fix every "
        "shortfall, and report the numbers you measured."),
}

REVIEW = ("Review every file you produced against the skill bar as a demanding "
          "design director; list 12 concrete deficiencies; fix them all; verify "
          "again.")

# The third pass, used only where the mechanical checks disagree with the agent.
# It names the shortfall and forbids the two easy ways out.
FIX = ("A mechanical check of the files you produced still fails on these "
       "points:\n{problems}\n\nFix every one of them in the files you already "
       "have. Do not start over, do not lower any target, and do not remove a "
       "feature to make a count come out. Then measure each number yourself and "
       "report what you measured.")

# Generous, because a 60KB single-file page is built one edit at a time.
MAX_TURNS = 150


def _seed(workdir: Path) -> None:
    """Give a project an empty directory and the skill it will be held to.

    The skill has to be on disk *before* the Harness is built: the catalogue is
    assembled at construction, so a skill written later would be advertised to
    nobody and use_skill would not exist.
    """
    shutil.rmtree(workdir, ignore_errors=True)
    skill = workdir / skills.SKILLS_DIR / "design-engineering"
    skill.mkdir(parents=True)
    (skill / skills.SKILL_FILE).write_text(SKILL)


def _printer(label: str):
    """One terse line per event, labelled — three agents share this log."""
    def on_event(kind, payload):
        if kind == "assistant":
            for call in payload["tool_calls"]:
                args = ", ".join(f"{k}={str(v)[:30]!r}" for k, v in call["args"].items())
                print(f"[{label}] → {call['name']}({args[:110]})", flush=True)
            if payload["text"]:
                print(f"[{label}] {payload['text'][:300]}", flush=True)
        elif kind == "tool_end":
            head = str(payload["result"]).strip().splitlines() or [""]
            print(f"[{label}]   {head[0][:110]}", flush=True)
    return on_event


def _fresh(workdir: str) -> Harness:
    """Factory for the build phase: a new agent, a new transcript."""
    return Harness(workdir=workdir, on_event=_printer(os.path.basename(workdir)),
                   max_turns=MAX_TURNS)


def _resumed(workdir: str) -> Harness:
    """Factory for every later phase: the same conversation, continued.

    Raising when there is nothing to resume is deliberate. run_fleet turns it into
    {"ok": False} for that job alone, which is the honest report — a review phase
    that silently started a fresh context would produce a confident review of
    files it had never seen.
    """
    harness = Harness(workdir=workdir, on_event=_printer(os.path.basename(workdir)),
                      max_turns=MAX_TURNS)
    if not harness.resume():
        raise RuntimeError(f"no session to resume in {workdir}")
    return harness


def _phase(label: str, tasks: dict, make) -> dict:
    """Run one task per project in parallel; return name -> result dict."""
    jobs = [{"name": name, "workdir": str(ROOT / name), "task": task}
            for name, task in tasks.items()]
    print(f"\n{'=' * 70}\n== phase: {label} ({', '.join(tasks)})\n{'=' * 70}",
          flush=True)
    started = time.time()
    results = run_fleet(jobs, make, max_workers=len(jobs))
    print(f"\n[{label}] finished in {time.time() - started:.0f}s", flush=True)
    for result in results:
        status = "ok" if result["ok"] else "FAILED"
        print(f"[{label}] {result['name']}: {status} — "
              f"{result['report'][:400]}", flush=True)
    return {result["name"]: result for result in results}


# ------------------------------------------------------------------- the checks

def check_artisan(workdir: Path) -> dict:
    """The full skill bar, counted, plus the features the brief named."""
    page = workdir / "index.html"
    if not page.is_file():
        return {"problems": ["index.html does not exist"], "metrics": {}}
    source = page.read_text(errors="replace")

    metrics = {
        "sections": _count_sections(source),
        "words": _visible_words(source),
        "svgs": len(re.findall(r"(?i)<svg\b", source)),
        "interactions": len(re.findall(r"addEventListener", source)),
        "bytes": len(source),
        "css_vars": len(set(re.findall(r"--[a-zA-Z0-9-]+\s*:", source))),
        "breakpoints": sorted(set(re.findall(r"@media[^{]*?(\d{3,4})px", source))),
    }
    problems = []
    # The bar itself. These are the numbers the skill told the agent to count.
    if metrics["sections"] < 9:
        problems.append(f"only {metrics['sections']} distinct sections, the bar is 9")
    if metrics["words"] < 1200:
        problems.append(f"only {metrics['words']} words of visible copy, the bar is 1,200")
    if metrics["svgs"] < 4:
        problems.append(f"only {metrics['svgs']} inline SVG illustrations, the bar is 4")
    if metrics["interactions"] < 3:
        problems.append(f"only {metrics['interactions']} event listeners — the bar is "
                        "3 working interactive behaviours")
    if metrics["css_vars"] < 10:
        problems.append(f"only {metrics['css_vars']} CSS custom properties — that is "
                        "not a design system")
    if "lorem ipsum" in source.lower():
        problems.append("the copy contains lorem ipsum")

    # The brief, feature by feature, each as a grep that cannot be argued with.
    if "localstorage" not in source.lower():
        problems.append("the dark-mode toggle does not persist to localStorage")
    if not re.search(r"(?i)accordion|<details", source):
        problems.append("no FAQ accordion")
    if not re.search(r"(?i)annual|yearly", source) or not re.search(r"(?i)month", source):
        problems.append("no monthly/annual subscription toggle")
    if not re.search(r"(?i)\btabs?\b|role=[\"']tab", source):
        problems.append("no brew-guide tabs")
    if len(re.findall(r"(?i)₹|&#8377;|Rs\.?\s*\d", source)) < 6:
        problems.append("fewer than six rupee prices — the six origin cards need prices")
    if not re.search(r":focus(-visible)?\b", source):
        problems.append("no focus states")
    problems.extend(_responsive(source))
    return {"problems": problems, "metrics": metrics}


def check_taskman(workdir: Path) -> dict:
    """Files exist, the suite runs, every case passes, and there are ten of them."""
    problems, metrics = [], {}
    for name in ("taskman.py", "test_taskman.py"):
        if not (workdir / name).is_file():
            problems.append(f"{name} does not exist")
    if problems:
        return {"problems": problems, "metrics": metrics}

    source = (workdir / "taskman.py").read_text(errors="replace")
    tests = (workdir / "test_taskman.py").read_text(errors="replace")
    metrics["subcommands"] = sorted(
        sub for sub in ("add", "list", "done", "rm", "stats")
        if re.search(rf"[\"']{sub}[\"']", source))
    metrics["cases"] = len(re.findall(r"def test_", tests))
    metrics["bytes"] = len(source) + len(tests)

    if len(metrics["subcommands"]) < 5:
        problems.append(f"subcommands found: {metrics['subcommands']} — "
                        "add, list, done, rm and stats are all required")
    if metrics["cases"] < 10:
        problems.append(f"only {metrics['cases']} test cases, the bar is 10")
    if "subprocess" not in tests:
        problems.append("the tests do not drive the CLI through subprocess")
    if "json" not in source:
        problems.append("no JSON persistence")

    # The only check that matters: run them.
    done = subprocess.run([sys.executable, "-m", "unittest", "test_taskman", "-v"],
                          cwd=workdir, capture_output=True, text=True, timeout=300)
    output = done.stdout + done.stderr
    ran = re.search(r"Ran (\d+) tests?", output)
    metrics["ran"] = int(ran.group(1)) if ran else 0
    metrics["exit"] = done.returncode
    if done.returncode != 0:
        problems.append(f"the test suite fails: {output.strip()[-400:]}")
    elif metrics["ran"] < 10:
        problems.append(f"the suite ran {metrics['ran']} tests, the bar is 10")
    return {"problems": problems, "metrics": metrics}


def check_viper(workdir: Path) -> dict:
    """The game's mechanics by grep, and the page's bar by count."""
    page = workdir / "index.html"
    if not page.is_file():
        return {"problems": ["index.html does not exist"], "metrics": {}}
    source = page.read_text(errors="replace")

    metrics = {
        "sections": _count_sections(source),
        "words": _visible_words(source),
        "svgs": len(re.findall(r"(?i)<svg\b", source)),
        "interactions": len(re.findall(r"addEventListener", source)),
        "bytes": len(source),
        "css_vars": len(set(re.findall(r"--[a-zA-Z0-9-]+\s*:", source))),
    }
    problems = []
    if "requestAnimationFrame" not in source:
        problems.append("the game loop does not use requestAnimationFrame")
    if "localstorage" not in source.lower():
        problems.append("the high score is not persisted to localStorage")
    if not re.search(r"(?i)<canvas", source):
        problems.append("there is no canvas")
    if not re.search(r"(?i)paus", source):
        problems.append("no pause")
    if not re.search(r"(?i)restart|new game|play again", source):
        problems.append("no restart")
    if not re.search(r"(?i)score", source):
        problems.append("no score")
    if not re.search(r"(?i)speed|interval|tickms|step", source):
        problems.append("nothing that speeds the game up")
    if metrics["svgs"] < 4:
        problems.append(f"only {metrics['svgs']} inline SVG illustrations, the bar is 4")
    if metrics["interactions"] < 3:
        problems.append(f"only {metrics['interactions']} event listeners, the bar is 3")
    if metrics["css_vars"] < 10:
        problems.append(f"only {metrics['css_vars']} CSS custom properties — that is "
                        "not a design system")
    if not re.search(r":focus(-visible)?\b", source):
        problems.append("no focus states")
    problems.extend(_responsive(source))
    return {"problems": problems, "metrics": metrics}


CHECKS = {"artisan-coffee": check_artisan, "taskman": check_taskman,
          "viper": check_viper}


def _responsive(source: str) -> list:
    """Check "responsive at 360, 768 and 1280" by what the CSS does.

    The first version of this looked for the literal strings and passed on a
    comment that said `/* Mobile First (360px) */`, which is worth recording: a
    grep for a number is not a measurement of a layout. So this asks the question
    the bar is actually asking. A viewport meta, or none of it means anything on a
    phone. A breakpoint near 768 and one near 1280, because those two widths have
    to be *addressed*. And for 360, either an explicit small-screen query or a
    mobile-first sheet — in a sheet whose base styles are the narrow layout and
    whose min-width queries add to it, 360 is not a breakpoint, it is the default,
    and demanding a max-width:360px query would be demanding worse CSS.
    """
    problems = []
    if not re.search(r"(?i)<meta[^>]+name=[\"']viewport", source):
        problems.append("no viewport meta tag, so nothing is responsive on a phone")

    mins = {int(width) for width in re.findall(r"min-width:\s*(\d{3,4})px", source)}
    maxs = {int(width) for width in re.findall(r"max-width:\s*(\d{3,4})px", source)}
    breaks = mins | maxs
    if not any(720 <= width <= 840 for width in breaks):
        problems.append(f"no breakpoint near 768px (found {sorted(breaks)})")
    if not any(1100 <= width <= 1440 for width in breaks):
        problems.append(f"no breakpoint near 1280px (found {sorted(breaks)})")
    if not mins and not any(width <= 480 for width in maxs):
        problems.append("no small-screen layout: neither a mobile-first sheet "
                        f"nor a query at or below 480px (found {sorted(breaks)})")
    return problems


def _count_sections(source: str) -> int:
    """Distinct top-level regions of the page.

    <section> plus the landmarks that are sections without being called one. An
    id on the element is what makes it navigable and therefore distinct, but not
    every real section has one, so this counts elements and trusts the markup.
    """
    return len(re.findall(r"(?i)<(section|header|footer|main|nav|article|aside)\b",
                          source))


def _visible_words(source: str) -> int:
    """Words a reader would actually see.

    Script and style bodies go first, then comments, then every tag — which also
    disposes of SVG path data, since that lives in attributes. What is left is
    the copy, and the count is the one the skill's 1,200 refers to.
    """
    text = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", source)
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return len(unescape(text).split())


# ----------------------------------------------------------------------- report

def _turns(workdir: Path) -> int:
    """Model turns spent on a project, counted from its transcript.

    Assistant messages in the session log. Sub-agent turns are not in here by
    design — a child harness is built with persist=False — so this is the
    parent's own spend.
    """
    logs = sorted(Path(workdir, session.SESSION_DIR).glob("*.jsonl"))
    if not logs:
        return 0
    return sum(1 for message in session.load(str(logs[0]))
               if message.get("role") == "assistant")


def _files(workdir: Path) -> str:
    """Every product file and its size, ignoring the agent's own bookkeeping."""
    parts = []
    ignore = {".athena", "skills", "__pycache__"}
    for path in sorted(workdir.rglob("*")):
        if path.is_file() and not ignore & set(path.parts):
            parts.append(f"{path.relative_to(workdir)} ({path.stat().st_size // 1024}KB)"
                         if path.stat().st_size >= 1024
                         else f"{path.relative_to(workdir)} ({path.stat().st_size}B)")
    return ", ".join(parts) or "(nothing)"


def _report(names, checks, passes, phases):
    """Print the results table and the numbers behind it."""
    print(f"\n{'=' * 78}\n== results\n{'=' * 78}")
    print(f"{'project':16} {'pass':6} {'turns':6} {'passes':7} contents")
    print("-" * 78)
    for name in names:
        workdir = ROOT / name
        verdict = "PASS" if not checks[name]["problems"] else "FAIL"
        print(f"{name:16} {verdict:6} {_turns(workdir):<6} {passes[name]:<7} "
              f"{_files(workdir)}")
    print("-" * 78)
    for name in names:
        print(f"\n{name}: {checks[name]['metrics']}")
        for problem in checks[name]["problems"]:
            print(f"  - unresolved: {problem}")
        for label, results in phases.items():
            result = results.get(name)
            if result and not result["ok"]:
                print(f"  - phase {label} errored: {result['report']}")


def main():
    """Seed, build, review, check, fix what fell short, then report.

    Two flags, both of which exist because a real run of this hit a stalled
    provider socket half an hour in. --keep leaves finished work alone, and
    --from review starts at the phase after the build. Neither is a shortcut:
    resuming is the harness's own answer to an interrupted run, and a driver that
    can only start from nothing is a driver that throws away three completed
    builds to redo one.
    """
    argv = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    keep = "--keep" in sys.argv
    start = "review" if "--from-review" in sys.argv else "build"

    names = [name for name in argv if name in BUILD] or list(BUILD)
    ROOT.mkdir(parents=True, exist_ok=True)
    if not keep:
        for name in names:
            _seed(ROOT / name)

    phases = {}
    if start == "build":
        phases["build"] = _phase("build", {n: BUILD[n] for n in names}, _fresh)
    phases["review"] = _phase("review", {n: REVIEW for n in names}, _resumed)

    checks = {name: CHECKS[name](ROOT / name) for name in names}
    passes = {name: 2 for name in names}

    # One more pass where the count disagrees with the agent. The bar does not
    # move; the work does.
    short = {name: FIX.format(problems="\n".join(f"- {p}" for p in
                                                 checks[name]["problems"]))
             for name in names if checks[name]["problems"]}
    if short:
        print(f"\n[shortfall] {', '.join(short)} — one more pass", flush=True)
        phases["fix"] = _phase("fix", short, _resumed)
        for name in short:
            checks[name] = CHECKS[name](ROOT / name)
            passes[name] = 3

    _report(names, checks, passes, phases)
    return 0 if all(not check["problems"] for check in checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
