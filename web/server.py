"""A browser front end for Athena — watch it work, and see what it built.

Concept this file teaches: the harness already publishes everything a UI needs.
`Harness(on_event=...)` fires on every assistant turn, every tool call and every
tool result; `Policy(approver=...)` asks a question and waits for a yes. A user
interface is not a new capability, it is a *second subscriber* to the events the
terminal was already printing. So nothing in athena/ changes — this file imports
the package and listens.

The whole server is the standard library. http.server for routing, a queue per
connected browser, and Server-Sent Events to push. No framework, no build step,
no npm: a workshop attendee opens a browser and it works, which is the same
promise the CLI makes.

Design rules:
  * Listen, never reach in. Everything shown comes through on_event; if the UI
    wants something the harness does not publish, the harness is what to change.
  * One run at a time, and it says so. Two agents in one directory would edit
    each other's files with complete confidence.
  * Interruption uses the socket that exists. before_tool is consulted before
    every call, so a Stop button is a gate that starts refusing — no thread
    killing, and the transcript stays coherent.
  * Localhost only, and not configurable. This hands whoever opens the page a
    shell inside the working directory; a bind address is not a preference.
  * The preview is the same sandbox. Files are served through the same realpath
    check the tools use, because "serve the workdir" is one traversal bug away
    from "serve the disk".

Run:  python3 web/server.py --workdir ~/my-project
"""

import argparse
import json
import mimetypes
import os
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from athena import Harness, Policy  # noqa: E402
from athena.tools import IGNORE_DIRS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# How long a browser has to answer an approval before it is treated as a no. Long
# enough to read the call, short enough that a closed tab cannot wedge a run.
APPROVAL_TIMEOUT = 300

# A tool result is shown in full on demand, but the event carries a clipped copy:
# a 4000-line read_file pushed to the browser is a megabyte down a pipe nobody reads.
MAX_RESULT_CHARS = 4000

# Files the browser is never shown. The agent's own bookkeeping is not the product.
HIDDEN = {".athena", "__pycache__", ".git", *IGNORE_DIRS}


class Session:
    """One working directory, one conversation, and everyone watching it.

    Holds the harness, the event history, and the subscriber queues. A browser
    that connects late — or reloads — gets the history replayed before the live
    stream starts, so the page is never emptier than the run behind it.
    """

    def __init__(self, workdir, model=None, mode="yolo"):
        self.workdir = os.path.realpath(workdir)
        self.model = model
        self.mode = mode
        self.lock = threading.Lock()
        self.events = []
        self.clients = []
        self.running = False
        self.gate = None
        self.pending = {}       # approval id -> [threading.Event, decision]
        self.harness = self._build()

    def _build(self):
        """A fresh harness over this directory, wired to publish and to obey."""
        self.gate = Gate(Policy(self.mode, approver=self._ask), self)
        return Harness(workdir=self.workdir, model=self.model, policy=self.gate,
                       on_event=self._on_event)

    # ------------------------------------------------------------ the event bus

    def publish(self, event):
        """Record an event and push it to every connected browser."""
        with self.lock:
            self.events.append(event)
            clients = list(self.clients)
        for client in clients:
            # Never block on a slow reader: a browser that cannot keep up loses
            # events, which is better than a run that stalls behind a dead tab.
            try:
                client.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self):
        """Return a queue seeded with the history so far."""
        client = queue.Queue(maxsize=1000)
        with self.lock:
            for event in self.events:
                client.put_nowait(event)
            self.clients.append(client)
        return client

    def unsubscribe(self, client):
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)

    def _on_event(self, kind, payload):
        """Translate a harness event into something a browser can render."""
        if kind == "assistant":
            calls = [{"name": call["name"], "args": call.get("args") or {}}
                     for call in payload["tool_calls"]]
            self.publish({"type": "assistant", "text": payload["text"],
                          "calls": calls, "usage": payload.get("usage") or {}})
        elif kind == "tool_start":
            self._started = time.time()
            self.publish({"type": "tool_start", "name": payload["name"],
                          "args": payload.get("args") or {}})
        elif kind == "tool_end":
            result = str(payload["result"])
            clipped = len(result) > MAX_RESULT_CHARS
            self.publish({
                "type": "tool_end", "name": payload["call"]["name"],
                "result": result[:MAX_RESULT_CHARS], "clipped": clipped,
                "ms": int((time.time() - getattr(self, "_started", time.time())) * 1000),
            })
            # The file tree and the preview are downstream of every tool that ran.
            self.publish({"type": "files", "files": self.files()})

    # ---------------------------------------------------------------- approvals

    def _ask(self, call, reason):
        """Policy's approver: put the question on screen and wait for a click.

        Blocking the run thread is the correct behaviour and not a compromise —
        safe mode means nothing happens until a human says so, and the browser is
        just a slower terminal.
        """
        key = f"{time.time()}-{call['name']}"
        gate = threading.Event()
        self.pending[key] = [gate, False]
        self.publish({"type": "approval", "id": key, "name": call["name"],
                      "args": call.get("args") or {}, "reason": reason})
        # A timeout is a no. An unanswered question must not hold a thread open
        # forever because someone closed the tab.
        gate.wait(APPROVAL_TIMEOUT)
        allowed = self.pending.pop(key, [None, False])[1]
        self.publish({"type": "approval_done", "id": key, "allowed": allowed})
        return allowed

    def decide(self, key, allowed):
        entry = self.pending.get(key)
        if not entry:
            return False
        entry[1] = bool(allowed)
        entry[0].set()
        return True

    # --------------------------------------------------------------- the run

    def run(self, prompt):
        """Run one task on a background thread. Returns False if one is going."""
        with self.lock:
            if self.running:
                return False
            self.running = True
        self.gate.stopped = False
        threading.Thread(target=self._run, args=(prompt,), daemon=True).start()
        return True

    def _run(self, prompt):
        started = time.time()
        self.publish({"type": "user", "text": prompt})
        try:
            answer = self.harness.run(prompt)
            self.publish({"type": "done", "text": answer,
                          "ms": int((time.time() - started) * 1000),
                          "stopped": self.gate.stopped})
        except Exception as exc:
            # Anything the harness could not absorb — no key, a dead network. The
            # page has to say so, or a stopped spinner is the only symptom.
            self.publish({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            self.running = False
            self.publish({"type": "files", "files": self.files()})

    def stop(self):
        """Refuse every further tool call, which ends the run at the next one."""
        self.gate.stopped = True
        self.publish({"type": "stopping"})

    def set_mode(self, mode):
        """Swap the permission mode without ending the conversation.

        The Gate holds the Policy rather than being one, so changing modes is
        replacing the object it consults — the harness keeps the same bound
        .check, and a run in progress picks the new rules up at its next call.
        """
        if mode not in ("safe", "yolo", "read-only"):
            return False
        self.mode = mode
        self.gate.policy = Policy(mode, approver=self._ask)
        self.gate.mode = mode
        self.publish({"type": "mode", "mode": mode})
        return True

    def reset(self):
        """Start a new conversation in the same directory."""
        with self.lock:
            self.events = []
        self.harness = self._build()
        self.publish({"type": "reset"})

    def resume(self):
        """Load the newest transcript in this directory and keep going."""
        if not self.harness.resume():
            return False
        self.publish({"type": "resumed", "messages": len(self.harness.messages),
                      "path": os.path.basename(self.harness.session_path or "")})
        return True

    # ---------------------------------------------------------------- the files

    def files(self):
        """Every product file under the workdir, as {path, size}."""
        found = []
        for dirpath, dirnames, filenames in os.walk(self.workdir):
            dirnames[:] = [d for d in dirnames if d not in HIDDEN]
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                full = os.path.join(dirpath, name)
                found.append({"path": os.path.relpath(full, self.workdir),
                              "size": os.path.getsize(full)})
        return sorted(found, key=lambda entry: entry["path"])[:500]

    def resolve(self, path):
        """The sandbox, again. Serving files needs the same gate the tools use."""
        full = os.path.realpath(os.path.join(self.workdir, path.lstrip("/")))
        if full != self.workdir and not full.startswith(self.workdir + os.sep):
            raise PermissionError("outside the working directory")
        return full


class Gate:
    """A Policy with a kill switch, standing where before_tool already stands.

    The harness asks this before running any tool, so a Stop button does not have
    to kill a thread mid-write — it just starts saying no, the model is told why,
    and the run winds itself up with its transcript intact.
    """

    def __init__(self, policy, session):
        self.policy = policy
        self.session = session
        self.stopped = False
        self.mode = policy.mode

    def check(self, call):
        if self.stopped:
            return "stopped by the user — do not call any more tools, just summarise"
        return self.policy.check(call)


class Handler(BaseHTTPRequestHandler):
    """Six endpoints and a static file or two. Routing, and nothing else."""

    protocol_version = "HTTP/1.1"
    session = None      # set by main()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
        if path == "/":
            return self._file(os.path.join(HERE, "index.html"), "text/html")
        if path == "/api/state":
            return self._json({
                "workdir": self.session.workdir, "model": self.session.harness.model,
                "mode": self.session.mode, "running": self.session.running,
                "tools": sorted(self.session.harness.tools),
                "files": self.session.files(),
            })
        if path == "/api/events":
            return self._stream()
        if path == "/api/file":
            return self._contents(_unquote(params.get("path", "")))
        if path.startswith("/preview/"):
            return self._preview(_unquote(path[len("/preview/"):]))
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or "{}")
        if self.path == "/api/run":
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                return self._json({"ok": False, "error": "empty prompt"})
            return self._json({"ok": self.session.run(prompt)})
        if self.path == "/api/stop":
            self.session.stop()
            return self._json({"ok": True})
        if self.path == "/api/approve":
            return self._json({"ok": self.session.decide(body.get("id"),
                                                         body.get("allow"))})
        if self.path == "/api/reset":
            self.session.reset()
            return self._json({"ok": True})
        if self.path == "/api/resume":
            return self._json({"ok": self.session.resume()})
        if self.path == "/api/mode":
            return self._json({"ok": self.session.set_mode(body.get("mode"))})
        self.send_error(404)

    # ------------------------------------------------------------------ replies

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, full, content_type):
        try:
            with open(full, "rb") as handle:
                body = handle.read()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The preview is rebuilt constantly; a cached page would show yesterday.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _preview(self, path):
        try:
            full = self.session.resolve(path or "index.html")
        except PermissionError:
            return self.send_error(403)
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        kind = mimetypes.guess_type(full)[0] or "application/octet-stream"
        self._file(full, kind)

    def _contents(self, path):
        try:
            full = self.session.resolve(path)
            with open(full, errors="replace") as handle:
                text = handle.read(400_000)
        except (PermissionError, OSError) as exc:
            return self._json({"ok": False, "error": str(exc)})
        self._json({"ok": True, "path": path, "text": text})

    def _stream(self):
        """Server-Sent Events: one long response, one JSON object per event."""
        client = self.session.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # No Content-Length: this response ends when the browser goes away.
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                try:
                    event = client.get(timeout=15)
                    data = json.dumps(event)
                except queue.Empty:
                    # A comment line. Keeps proxies and impatient sockets awake.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass        # The tab closed. Normal, and not worth a traceback.
        finally:
            self.session.unsubscribe(client)

    def log_message(self, fmt, *args):
        """Quiet. The interesting log is the event feed, not the request log."""


def _unquote(text):
    """Percent-decoding, without importing a URL parser for one job."""
    out, index = [], 0
    while index < len(text):
        if text[index] == "%" and index + 2 < len(text) + 1:
            try:
                out.append(chr(int(text[index + 1:index + 3], 16)))
                index += 3
                continue
            except ValueError:
                pass
        out.append("+" if text[index] == "+" else text[index])
        index += 1
    return "".join(out).replace("+", " ")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="athena-web", description="Athena in a browser, on localhost.")
    parser.add_argument("-d", "--workdir", default=".",
                        help="the directory the agent is sandboxed to (default: .)")
    parser.add_argument("-m", "--model", help="override ATHENA_MODEL")
    parser.add_argument("--mode", default="yolo", choices=("safe", "yolo", "read-only"),
                        help="safe asks in the browser before every change "
                             "(default: yolo)")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--no-open", action="store_true",
                        help="do not open a browser window")
    args = parser.parse_args(argv)

    Handler.session = Session(args.workdir, args.model, args.mode)
    # 127.0.0.1, not 0.0.0.0, and not a flag. This serves a shell.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True

    url = f"http://127.0.0.1:{args.port}"
    print(f"\nAthena UI on {url}")
    print(f"  jail:  {Handler.session.workdir}")
    print(f"  model: {Handler.session.harness.model}")
    print(f"  mode:  {args.mode}")
    print("\nCtrl-C to stop.\n")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
