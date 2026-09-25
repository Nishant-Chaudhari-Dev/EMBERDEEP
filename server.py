"""Host a game in the browser, for yourself or for friends on the same network.

    python server.py                          # new game, settings from config.json
    python server.py --continue               # resume saves/autosave.json
    python server.py --load saves/mygame.json
    python server.py --backend mock           # no model, just to try the interface
    python server.py --host 127.0.0.1         # only this computer can connect

Everyone on the same Wi-Fi opens the printed address. Nothing leaves your network: the model runs
on this machine and this server only talks to browsers on your LAN. Standard library only.

All game work (including every model call) runs on one worker thread, so the model never sees
two requests at once and the game state is only ever changed from one place."""
import collections
import json
import queue
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import ROOT, build_llm, load_config
from engine.dice import Dice
from gm.llm import LLMError
from session import SAVE_DIR, Session

WEB = ROOT / "web"
BACKLOG = 250                 # public events replayed to anyone who (re)connects
PRIVATE_BACKLOG = 40
MAX_BODY = 32_000
MAX_TEXT = 1200
READ_ONLY = {"help", "h", "?", "sheet", "party", "quests", "map", "roll"}   # answered instantly, no model


class Hub:
    """Owns the session, the connected browsers and the job queue."""

    def __init__(self, llm, state=None, seed=None, debug=False):
        self.lock = threading.Lock()
        self.backlog = collections.deque(maxlen=BACKLOG)
        self.private = collections.defaultdict(lambda: collections.deque(maxlen=PRIVATE_BACKLOG))
        self.subs = {}              # sub id -> (pid, queue)
        self.players = {}           # pid -> {"name": str, "char_id": str | None}
        self.last_turn = None
        self.thinking = ""
        self.jobs = queue.Queue()
        self.session = Session(llm, state=state, emit=self.on_event, seed=seed, debug=debug)
        self.last_state = {"kind": "state", "text": "", "t": time.time(), "data": self.session.engine.snapshot()}
        threading.Thread(target=self._work, name="gm-worker", daemon=True).start()

    # ------------------------------------------------------------------ events
    def on_event(self, ev):
        k = ev.get("kind")
        with self.lock:
            if k == "state":
                self.last_state = ev
            elif k == "turn":
                self.last_turn = ev
                if ev.get("char_id"):          # combat turn markers are part of the story
                    self.backlog.append(ev)
            elif k == "thinking":
                self.thinking = ev.get("text", "")
            elif k == "lobby":
                pass                            # sent fresh in every hello, no need to replay
            else:
                if k in ("narration", "error", "gameover"):
                    self.thinking = ""
                self.backlog.append(ev)
            if k == "turn":
                self.thinking = ""
            targets = [q for _, q in self.subs.values()]
        for q in targets:
            q.put(ev)

    def send_to(self, pid, text, kind="private"):
        ev = {"kind": kind, "text": text, "t": time.time()}
        with self.lock:
            self.private[pid].append(ev)
            targets = [q for p, q in self.subs.values() if p == pid]
        for q in targets:
            q.put(ev)

    def lobby(self):
        s = self.session.s
        online = collections.Counter(p for p, _ in self.subs.values())
        claimed = {p["char_id"] for p in self.players.values() if p["char_id"]}
        players = [{"name": p["name"], "online": online[pid] > 0,
                    "char": s.characters[p["char_id"]].name if p["char_id"] in s.characters else ""}
                   for pid, p in self.players.items()]
        unclaimed = [{"id": c.id, "name": c.name, "player": c.player} for c in s.characters.values() if c.id not in claimed]
        return {"kind": "lobby", "t": time.time(), "data": {"players": players, "started": s.started, "unclaimed": unclaimed}}

    def push_lobby(self):
        with self.lock:
            ev = self.lobby()
        self.on_event(ev)

    def hello(self, pid):
        with self.lock:
            p = self.players.get(pid)
            events = sorted(list(self.backlog) + list(self.private.get(pid, [])), key=lambda e: e.get("t", 0))
            return {"kind": "hello", "t": time.time(), "you": ({"name": p["name"], "char_id": p["char_id"]} if p else None),
                    "backlog": events, "state": self.last_state.get("data"), "turn": self.last_turn,
                    "lobby": self.lobby()["data"], "thinking": self.thinking}

    def subscribe(self, pid):
        sid, q = uuid.uuid4().hex, queue.Queue()
        with self.lock:
            self.subs[sid] = (pid, q)
        self.push_lobby()
        return sid, q

    def unsubscribe(self, sid):
        with self.lock:
            self.subs.pop(sid, None)
        self.push_lobby()

    # ------------------------------------------------------------------ worker
    def run(self, fn, *args, wait=False, timeout=600):
        box = {"done": threading.Event()}
        self.jobs.put((fn, args, box))
        if not wait:
            return None
        if not box["done"].wait(timeout):
            raise TimeoutError("The game master is taking too long.")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _work(self):
        while True:
            fn, args, box = self.jobs.get()
            try:
                box["result"] = fn(*args)
            except Exception as e:   # keep the worker alive no matter what
                box["error"] = e
                self.on_event({"kind": "error", "text": f"Something broke: {e!r}. The game state is safe; try again.",
                               "t": time.time()})
            finally:
                box["done"].set()

    # ------------------------------------------------------------------ actions
    def join(self, name, pid=None):
        name = " ".join(str(name or "").split())[:24]
        with self.lock:
            if pid and pid in self.players:
                if name:
                    self.players[pid]["name"] = name
                p = self.players[pid]
            else:
                if not name:
                    raise ValueError("Pick a name.")
                online = {q for q, _ in self.subs.values()}
                same = [q for q, x in self.players.items() if x["name"].lower() == name.lower() and q not in online]
                if same:   # same person on a new device or a cleared browser: pick up where they were
                    pid = same[0]
                else:
                    pid = uuid.uuid4().hex
                    self.players[pid] = {"name": name, "char_id": None}
                p = self.players[pid]
            if not p["char_id"]:   # returning after a restart: reclaim the character with your player name
                claimed = {x["char_id"] for x in self.players.values() if x["char_id"]}
                for c in self.session.s.characters.values():
                    if c.player.lower() == p["name"].lower() and c.id not in claimed:
                        p["char_id"] = c.id
                        break
            out = {"pid": pid, "name": p["name"], "char_id": p["char_id"]}
        self.push_lobby()
        return out

    def claim(self, pid, char_id):
        with self.lock:
            if pid not in self.players:
                raise ValueError("Join first.")
            if char_id not in self.session.s.characters:
                raise ValueError("No such character.")
            if any(p["char_id"] == char_id for q, p in self.players.items() if q != pid):
                raise ValueError("Someone else is playing that character.")
            self.players[pid]["char_id"] = char_id
        self.push_lobby()
        return {"char_id": char_id}

    def options(self):
        c = self.session.engine.c
        return {
            "ancestries": [{"id": k, "name": v["name"], "traits": v.get("traits", []), "bonus": v.get("bonus", {})}
                           for k, v in c.ancestries.items()],
            "classes": [{"id": k, "name": v["name"], "hit_die": v["hit_die"], "caster": bool(v.get("caster")),
                         "blurb": v.get("blurb", ""), "priority": v["priority"]} for k, v in c.classes.items()],
            "started": self.session.s.started,
        }

    def create(self, pid, form):
        p = self.players.get(pid)
        if not p:
            raise ValueError("Join first.")
        cur = self.session.s.characters.get(p["char_id"] or "")
        if cur and not cur.dead:
            raise ValueError("You already have a character.")
        c = self.session.engine.c
        anc, cls = str(form.get("ancestry", "")), str(form.get("cls", ""))
        if anc not in c.ancestries or cls not in c.classes:
            raise ValueError("Pick an ancestry and a class.")
        name = " ".join(str(form.get("name", "")).split())[:30]
        if not name:
            raise ValueError("Your character needs a name.")
        scores, note = None, "Standard array: 15, 14, 13, 12, 10, 8."
        if form.get("method") == "roll":
            d = Dice()
            rolls = sorted((d.roll("4d6kh3").total for _ in range(6)), reverse=True)
            scores = dict(zip(c.classes[cls]["priority"], rolls))
            note = "You rolled " + ", ".join(map(str, rolls)) + " (highest goes to your class's key ability)."

        def make():
            pc = self.session.add_character(p["name"], name, anc, cls, scores, str(form.get("backstory", ""))[:500])
            if not self.session.s.started:
                self.session.push_state()
            return pc.id, self.session.sheet(pc)

        char_id, sheet = self.run(make, wait=True)
        with self.lock:
            p["char_id"] = char_id
        self.send_to(pid, note + "\n" + sheet)
        self.push_lobby()
        return {"char_id": char_id}

    def begin(self, pid, premise):
        if pid not in self.players:
            raise ValueError("Join first.")
        if self.session.s.started:
            raise ValueError("The adventure is already underway.")
        if not self.session.s.characters:
            raise ValueError("Make at least one character first.")
        self.run(self._begin, str(premise or "")[:MAX_TEXT])
        return {"ok": True}

    def _begin(self, premise):
        self.session.begin(premise)
        self.push_lobby()

    def act(self, pid, text):
        p = self.players.get(pid)
        if not p:
            raise ValueError("Join first.")
        text = str(text or "").strip()[:MAX_TEXT]
        if not text:
            return {}
        char_id = p["char_id"]
        if text.startswith("/"):
            cmd = text[1:].split(" ", 1)[0].lower()
            if cmd in READ_ONLY:
                return {"reply": self.session.command(char_id, text)}
        elif not char_id:
            return {"reply": "Make or pick a character first."}

        def job():
            reply = self.session.submit(char_id, text)
            if reply:
                self.send_to(pid, reply)

        self.run(job)
        return {"queued": True}

    def resolve(self, pid):
        if pid not in self.players:
            raise ValueError("Join first.")
        self.run(self.session.force_round)
        return {"ok": True}

    def resume_if_needed(self):
        """After loading a save: let enemies finish their turns if it's theirs, then ask the players."""
        s = self.session
        if not s.s.started:
            return

        def go():
            with s.lock:
                if s.s.combat and not s.engine.current_pc():
                    s._rolls(s.engine.run_until_pc())
                s.push_state()
                s.prompt()
        self.run(go)


# ---------------------------------------------------------------------- http
def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Emberdeep/1.0"

        def log_message(self, fmt, *args):   # quiet by default
            if hub.session.debug:
                super().log_message(fmt, *args)

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("Request too large.")
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("Bad request.")
            return data

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in ("/", "/index.html"):
                body = (WEB / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/events":
                self._stream(q.get("pid", ""))
            elif url.path == "/api/options":
                self._json(hub.options())
            elif url.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self._json({"error": "Not found."}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                d = self._body()
                pid = str(d.get("pid", ""))
                if path == "/api/join":
                    out = hub.join(d.get("name", ""), pid or None)
                elif path == "/api/claim":
                    out = hub.claim(pid, str(d.get("char_id", "")))
                elif path == "/api/character":
                    out = hub.create(pid, d)
                elif path == "/api/begin":
                    out = hub.begin(pid, d.get("premise", ""))
                elif path == "/api/act":
                    out = hub.act(pid, d.get("text", ""))
                elif path == "/api/resolve":
                    out = hub.resolve(pid)
                else:
                    return self._json({"error": "Not found."}, 404)
                self._json(out)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                self._json({"error": str(e) or "Bad request."}, 400)
            except (LLMError, TimeoutError) as e:
                self._json({"error": str(e)}, 503)

        def _stream(self, pid):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            sid, q = hub.subscribe(pid)
            try:
                self._send(hub.hello(pid))
                while True:
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._send(ev)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                hub.unsubscribe(sid)

        def _send(self, ev):
            self.wfile.write(b"data: " + json.dumps(ev).encode("utf-8") + b"\n\n")
            self.wfile.flush()

    return Handler


def lan_ip():
    """The address other devices on your network can reach. Sends no packets."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    def extra(p, cfg):
        p.add_argument("--host", default=cfg.get("host", "0.0.0.0"), help="0.0.0.0 = whole LAN, 127.0.0.1 = this computer only")
        p.add_argument("--port", type=int, default=cfg.get("port", 8765))
        p.add_argument("--continue", dest="resume", action="store_true", help="resume saves/autosave.json")

    args = load_config("Emberdeep: host a game in the browser", extra)
    path = args.load or (str(SAVE_DIR / "autosave.json") if args.resume else None)
    if path and not Path(path).exists():
        print(f"No save at {path}.")
        sys.exit(1)
    try:
        print("Loading the game master model (first load can take a minute)...")
        llm = build_llm(args)
    except LLMError as e:
        print(e)
        sys.exit(1)

    hub = Hub(llm, state=Session.load_state(path) if path else None, seed=args.seed, debug=args.debug)
    hub.resume_if_needed()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), make_handler(hub))
    except OSError as e:
        print(f"Can't open port {args.port}: {e}. Try --port 8800.")
        sys.exit(1)

    print("\n  Emberdeep is running.")
    print(f"  On this computer:     http://localhost:{args.port}")
    if args.host in ("0.0.0.0", ""):
        print(f"  Friends on your Wi-Fi: http://{lan_ip()}:{args.port}")
        print("  (If they can't connect, allow Python through your firewall for private networks.)")
    print(f"  Backend: {args.backend}" + ("" if args.backend == "mock" else f" ({args.model})"))
    if path:
        print(f"  Loaded {path}")
    elif (SAVE_DIR / "autosave.json").exists():
        print("  Tip: restart with --continue to resume your last autosave.")
    print("  Ctrl+C to stop. The game autosaves after every round.\n")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if hub.session.s.started and not hub.session.busy:   # if busy, the last round's autosave stands
            try:
                print(f"\nSaved to {hub.session.save('autosave')}")
            except OSError:
                pass
        httpd.server_close()


if __name__ == "__main__":
    main()
