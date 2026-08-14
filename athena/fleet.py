"""Day 5 — the fleet: many agents, one call, and no shared state to get wrong.

Concept this file teaches: an agent run is *mostly waiting*. Nearly all of its
wall-clock time is a socket open to a model, which means N agents cost barely
more time than one — the parallelism is free, and threads are enough to collect
it because the waiting happens with the GIL released.

What is not free is isolation. Two agents in one directory will edit each other's
files with complete confidence, so a job names its own workdir and gets its own
harness. That is the entire safety story: no locks, no queues, no coordination,
because there is nothing shared to coordinate over.

Design rules:
  * The caller builds the harness. run_fleet takes a factory, so the model,
    policy and tools are the caller's decision and this file has no opinions.
  * A failed job is a result, not an exception. Raising would discard the reports
    of every job that worked, which is the opposite of what a fleet is for.
  * Order is the input order. Results are read next to the jobs that produced
    them, and completion order is an implementation detail nobody asked for.
"""

from concurrent.futures import ThreadPoolExecutor


def run_fleet(jobs: list, make_harness, max_workers: int = 4) -> list:
    """Run every job in parallel and return one result dict per job.

    `jobs` is [{"name", "workdir", "task"}]; `make_harness(workdir)` returns
    anything with a .run(task). Each result is {"name", "ok", "report"} — the
    agent's final text when it finished, the exception rendered as one line when
    it did not.

    max_workers is a rate limit in disguise: the ceiling that matters in practice
    is the provider's, not the machine's.
    """
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_one, job, make_harness) for job in jobs]
        # .result() in submission order, so the list lines up with `jobs`. It
        # cannot raise: _run_one converts every failure into a result.
        return [future.result() for future in futures]


def _run_one(job: dict, make_harness) -> dict:
    """Build one harness, run one task, and never let a failure escape.

    The harness is built *inside* the worker, so nothing is shared between jobs —
    not a message list, not a session path, not a tool closure over the wrong
    directory. type(exc).__name__ is included because "TimeoutError: " and
    "PermissionError: " are read differently, and a bare message loses that.
    """
    try:
        harness = make_harness(job["workdir"])
        return {"name": job["name"], "ok": True, "report": harness.run(job["task"])}
    except Exception as exc:
        return {"name": job["name"], "ok": False,
                "report": f"{type(exc).__name__}: {exc}"}
