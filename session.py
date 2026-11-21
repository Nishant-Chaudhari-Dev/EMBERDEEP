"""A running game. Both the terminal and the LAN server drive the game through this class.

Flow
  Exploration: every conscious character declares an action; the round resolves when all have
               declared (or someone forces it). All actions are adjudicated and narrated together.
  Combat:      strict initiative order. Monsters act automatically between player turns.
UIs receive a stream of events: {"kind": narration|roll|system|turn|state|thinking|error|gameover, ...}"""
import json
import threading
import time
from pathlib import Path

from engine import rules as R
from engine.dice import Dice
from engine.engine import Engine
from engine.models import GameState
from gm.llm import LLMError
from gm.master import GameMaster
from gm.prompts import DEFAULT_PREMISE

SAVE_DIR = Path(__file__).resolve().parent / "saves"
KEEP_RECENT = 10
SUMMARIZE_AT = 24

HELP = """Type what your character does, in plain words. Anything goes: talk, sneak, climb, bargain, cast, improvise.
Commands:
  /sheet           your character sheet        /party     everyone at a glance
  /quests          quest log                   /map       where you are and known places
  /roll 2d6+1      roll any dice               /ask ...   out-of-character question to the GM
  /pass            do nothing this round/turn  /go        resolve the round now (don't wait)
  /save [name]     save the game               /help      this list"""


class Session:
    def __init__(self, llm, state=None, emit=None, seed=None, debug=False):
        self.engine = Engine(state or GameState(), dice=Dice(seed))
        self.table_dice = Dice()   # player /roll gets its own dice so it never shifts a seeded game's rolls
        self.gm = GameMaster(llm, self.engine, log_fn=self._debug if debug else None)
        self.emit_fn = emit or (lambda ev: None)
        self.pending = {}
        self.lock = threading.RLock()
        self.busy = False
        self.debug = debug

    @property
    def s(self):
        return self.engine.s

    # ------------------------------------------------------------------ events
    def emit(self, kind, text="", **data):
        self.emit_fn({"kind": kind, "text": text, "t": time.time(), **data})

    def _debug(self, *a):
        self.emit("system", "[debug] " + " ".join(str(x) for x in a))

    def _notes(self):
        for n in self.engine.notes:
            self.emit("system", n)
        self.engine.notes.clear()

    def _rolls(self, results):
        for r in results:
            if r.get("text"):
                self.emit("roll", r["text"], success=r.get("success"), actor=r.get("actor", ""))

    def push_state(self):
        self.emit("state", data=self.engine.snapshot())

    def prompt(self):
        if not self.s.started:
            return
        if all(p.dead for p in self.s.characters.values()):
            self.emit("gameover", "The party has fallen. Load a save to continue, or start a new tale.")
            return
        pc = self.engine.current_pc()
        if self.s.combat and pc:
            self.emit("turn", f"{pc.name}'s turn ({pc.player}).", char_id=pc.id, waiting=[pc.id])
        else:
            waiting = [p.id for p in self.expected() if p.id not in self.pending]
            names = [self.s.characters[c].name for c in waiting]
            self.emit("turn", "What do you do?" + (f" Waiting on: {', '.join(names)}" if names else ""), waiting=waiting)

    # ------------------------------------------------------------------ setup
    def add_character(self, player, name, ancestry, cls, scores=None, backstory=""):
        with self.lock:
            pc = self.engine.create_character(name, player, ancestry, cls, scores, backstory)
            if self.s.solo and len(self.s.characters) > 1:   # solo safety net ends when a friend joins
                self.s.solo = False
                for p in self.s.characters.values():
                    p.features.pop("last_stand", None)
                    p.max_features.pop("last_stand", None)
            if self.s.started:
                if self.s.combat:
                    self.engine.join_combat(pc)
                self.emit("system", f"{pc.name} the {self.engine.c.classes[cls]['name']} joins the party.")
                self.s.log.append({"role": "players", "text": f"{pc.name} ({player}) joins the party."})
                self.push_state()
                self.prompt()
            return pc

    def begin(self, premise=""):
        with self.lock:
            if self.s.started or not self.s.characters:
                return
            self.busy = True
            try:
                if len(self.s.characters) == 1:
                    self.s.solo = True
                    for p in self.s.characters.values():
                        p.features["last_stand"] = p.max_features["last_stand"] = 1
                self.s.premise = premise.strip() or DEFAULT_PREMISE
                self.emit("thinking", "The GM is setting the scene...")
                op = self.gm.opening(self.s.premise)
                loc = op.get("location") or {}
                self.engine.move_party(loc.get("name") or "The Crossroads", loc.get("description", ""))
                for n in (op.get("npcs") or [])[:4]:
                    if isinstance(n, dict) and n.get("name"):
                        self.engine.add_npc(n["name"], n.get("disposition", "neutral"), n.get("description", ""))
                q = op.get("quest") or {}
                if q.get("title"):
                    self.engine.apply_effects([{"op": "quest", "target": q["title"], "value": "active", "text": q.get("notes", "")}])
                text = op.get("narration") or "Your story begins at a crossroads, under a darkening sky."
                self.s.started = True
                self.s.log.append({"role": "gm", "text": text})
                self.emit("narration", text)
                self._retire_old_autosave()
                self.autosave()
            except LLMError as e:
                self.emit("error", str(e))
            finally:
                self.busy = False
            self.push_state()
            self.prompt()

    # ------------------------------------------------------------------ input
    def expected(self):
        if self.s.combat:
            pc = self.engine.current_pc()
            return [pc] if pc else []
        return [p for p in self.s.characters.values() if p.conscious]

    def submit(self, char_id, text) -> str:
        """Returns a private reply for the submitter (or '')."""
        text = (text or "").strip()
        if not text:
            return ""
        if text.startswith("/"):
            return self.command(char_id, text)
        with self.lock:
            if not self.s.started:
                return "The adventure hasn't started yet."
            if self.busy:
                return "The GM is still working on the last moment. One second."
            pc = self.s.characters.get(char_id)
            if not pc:
                return "No such character."
            if not pc.conscious:
                return f"{pc.name} is {'dead' if pc.dead else 'unconscious'} and can't act."
            if self.s.combat:
                cur = self.engine.current_pc()
                if not cur or cur.id != char_id:
                    return f"It's {cur.name if cur else 'not your'}'s turn."
                self.emit("player", text, actor=pc.name, char_id=pc.id)
                self._run(self._combat_action, pc, text)
                return ""
            self.pending[char_id] = text
            self.emit("player", text, actor=pc.name, char_id=pc.id)
            if all(p.id in self.pending for p in self.expected()):
                self._run(self.resolve_round)
            else:
                self.prompt()
            return ""

    def _run(self, fn, *args):
        self.busy = True
        try:
            fn(*args)
        except LLMError as e:
            self.emit("error", f"{e}  (Your action was not used. Try again.)")
        except Exception as e:
            self.emit("error", f"Something broke: {e!r}. The game state is safe; try again.")
            if self.debug:
                raise
        finally:
            self.busy = False
        self.push_state()
        self.prompt()

    def force_round(self):
        with self.lock:
            if not self.s.combat and self.pending and not self.busy:
                self._run(self.resolve_round)

    # ------------------------------------------------------------------ resolution
    def resolve_round(self):
        declared = [(p, self.pending[p.id]) for p in self.s.characters.values() if p.id in self.pending and p.conscious]
        pending_backup = dict(self.pending)
        self.pending = {}
        declared = [(p, t) for p, t in declared if t != "(waits)"]
        if not declared:
            return
        self.emit("thinking", "The GM considers your actions...")
        try:
            actions = self.gm.adjudicate(declared)
        except LLMError:
            self.pending = pending_backup
            raise
        results = []
        rested = False
        for a in actions:
            if a["kind"] == "rest":   # one rest covers the whole party
                if rested:
                    a = dict(a, kind="none")
                rested = True
            r = self.engine.resolve(a)
            self._notes()
            if r.get("refund"):
                self.emit("system", r["text"], actor=r["actor"])
                r = dict(r, text=f"{r['text']} ({r['actor']} hesitates.)")
            else:
                self._rolls([r])
            results.append(r)
        if self.s.combat:   # someone started a fight: everyone who acted already spent their turn
            for p, _ in declared:
                if p.id not in self.s.combat.skip_once:
                    self.s.combat.skip_once.append(p.id)
            more = self.engine.run_until_pc()
            self._rolls(more)
            results += more
        self._narrate(declared, results)

    def _combat_action(self, pc, text):
        self.emit("thinking", f"The GM resolves {pc.name}'s move...")
        a = self.gm.adjudicate([(pc, text)])[0]
        if a["kind"] == "rest":
            a["kind"] = "dodge"
        r = self.engine.resolve(a)
        self._notes()
        if r.get("refund"):
            self.emit("system", r["text"] + " You can still act.", actor=pc.name)
            return
        self._rolls([r])
        results = [r]
        cb = self.s.combat
        keep_turn = r.get("extra_action")
        if not keep_turn and cb and pc.id in cb.surged:   # the extra action Action Surge granted
            cb.surged.remove(pc.id)
            keep_turn = True
        if keep_turn:
            end = self.engine.check_combat_end()
            if end:
                results.append(end)
                self._rolls([end])
        else:
            more = self.engine.finish_pc_turn(pc)
            self._rolls(more)
            results += more
        self._narrate([(pc, text)], results)

    def _pass_turn(self, pc):
        more = self.engine.finish_pc_turn(pc)
        self._rolls(more)
        self._narrate([(pc, "holds position and waits")], more)

    def _narrate(self, declared, results):
        situation = ""
        outcome = next((r.get("outcome") for r in results if r.get("kind") == "combat_end"), None)
        if outcome == "defeat":
            situation = ("The party was defeated but not killed. Narrate them waking up later: captured, robbed, left for dead or "
                         "rescued, whatever fits the enemies. Use effects for lost items or a move.")
        elif outcome == "victory":
            situation = "Combat is over and the party won. Describe the aftermath and what they notice now."
        narration = self._tell(declared, results, situation)
        if self.s.combat and not self.engine.current_pc():
            more = self.engine.run_until_pc()   # an ambush: enemies faster than the party act first
            if more:
                self._rolls(more)
                narration += "\n" + self._tell([], more)
        woke = self.engine.revive_if_party_down()
        if woke:
            self.emit("system", woke)
            narration += "\n" + woke
        self.s.log.append({"role": "players", "text": " | ".join(f"{p.name}: {t}" for p, t in declared) or "(enemies act)"})
        self.s.log.append({"role": "gm", "text": narration.strip()})
        self._compress_memory()
        self.autosave()

    def _tell(self, declared, results, situation="") -> str:
        """Narrate results the engine already applied and apply the narrator's effects. If the model
        is unreachable the results still stand, so say that instead of 'your action was not used'."""
        self.emit("thinking", "The GM narrates...")
        try:
            narration, effects = self.gm.narrate(declared, results, situation)
        except LLMError as e:
            self.emit("error", f"{e}  (The rolls above already happened; only the narration is missing.)")
            return "; ".join(r["text"] for r in results if r.get("text"))
        self.emit("narration", narration)
        for note in self.engine.apply_effects(effects):
            self.emit("system", note)
        self._notes()
        return narration

    def wake_if_party_down(self) -> bool:
        """For a game loaded with everyone at 0 HP out of combat."""
        note = self.engine.revive_if_party_down()
        if note:
            self.emit("system", note)
            self.s.log.append({"role": "gm", "text": note})
        return bool(note)

    def _compress_memory(self):
        if len(self.s.log) < SUMMARIZE_AT:
            return
        old, self.s.log = self.s.log[:-KEEP_RECENT], self.s.log[-KEEP_RECENT:]
        try:
            self.s.summary = self.gm.summarize(self.s.summary, old)
        except LLMError:
            self.s.log = old + self.s.log

    # ------------------------------------------------------------------ commands
    def command(self, char_id, text) -> str:
        cmd, _, arg = text[1:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        e = self.engine
        pc = self.s.characters.get(char_id)
        if cmd in ("help", "h", "?"):
            return HELP
        if cmd == "sheet":
            target = e.find_pc(arg) if arg else pc
            return self.sheet(target) if target else "No such character."
        if cmd == "party":
            return "\n".join(f"{p.name} ({p.player}): {'DEAD' if p.dead else f'{p.hp}/{p.max_hp} HP'}, AC {p.ac}"
                             + (f", {', '.join(p.conditions)}" if p.conditions else "") for p in self.s.characters.values())
        if cmd == "quests":
            return "\n".join(f"[{q.status}] {q.title}: {q.notes}" for q in self.s.quests.values()) or "No quests yet."
        if cmd == "map":
            loc = e.location()
            if not loc:
                return "Nowhere yet."
            exits = ", ".join(self.s.locations[x].name for x in loc.exits if x in self.s.locations) or "none known"
            known = ", ".join(l.name for l in self.s.locations.values() if l.id != loc.id) or "none"
            return f"You are at {loc.name}. {loc.description}\nRoutes from here: {exits}\nOther known places: {known}"
        if cmd == "roll":
            try:
                r = self.table_dice.roll(arg or "1d20")
            except ValueError as ex:
                return str(ex)
            self.emit("roll", f"{pc.name if pc else 'Someone'} rolls {arg or '1d20'}: {r.detail} = {r.total}", actor=pc.name if pc else "")
            return ""
        if cmd == "ask":
            if not arg or not pc:
                return "Usage: /ask your question"
            try:
                return "GM: " + self.gm.ask(pc, arg)
            except LLMError as ex:
                return str(ex)
        if cmd == "pass":
            if self.s.combat:
                with self.lock:
                    cur = e.current_pc()
                    if cur and pc and cur.id == pc.id and not self.busy:
                        self._run(self._pass_turn, pc)
                        return ""
                return "It's not your turn."
            with self.lock:
                if pc:
                    self.pending[pc.id] = "(waits)"
                    if all(p.id in self.pending for p in self.expected()):
                        self._run(self.resolve_round)
                    else:
                        self.prompt()
            return ""
        if cmd in ("go", "resolve"):
            self.force_round()
            return ""
        if cmd == "save":
            return f"Saved to {self.save(arg or 'quicksave')}"
        return f"Unknown command /{cmd}. Try /help."

    def sheet(self, p) -> str:
        c = self.engine.c
        nxt = R.XP_FOR_LEVEL[p.level] if p.level < R.MAX_LEVEL else "max"
        lines = [f"{p.name}, {c.ancestries[p.ancestry]['name']} {c.classes[p.cls]['name']} {p.level} (played by {p.player})",
                 f"HP {p.hp}/{p.max_hp}  AC {p.ac}  XP {p.xp}/{nxt}  Gold {p.gold}  Hit dice {p.hit_dice}",
                 "  ".join(f"{a} {p.abilities[a]} ({R.mod(p.abilities[a]):+d})" for a in R.ABILITIES),
                 f"Proficient skills: {', '.join(s.replace('_', ' ') for s in p.skills)}.  Saves: {', '.join(p.saves)}",
                 "Weapons: " + ", ".join(f"{c.weapons[w]['name']} ({c.weapons[w]['damage']} {c.weapons[w]['type']})" for w in p.weapons)]
        if p.spells:
            lines.append("Spells: " + ", ".join(c.spells[s]["name"] + (f" (L{c.spells[s]['level']})" if c.spells[s]["level"] else "")
                                                for s in p.spells if s in c.spells))
            lines.append("Slots left: " + (", ".join(f"level {k}: {v}" for k, v in p.slots.items()) or "none"))
        if p.features:
            lines.append("Features: " + ", ".join(f"{k.replace('_', ' ')} ({v})" for k, v in p.features.items()))
        lines.append("Inventory: " + (", ".join(f"{k}" + (f" x{v}" if v > 1 else "") for k, v in p.inventory.items()) or "empty"))
        lines.append("Traits: " + ", ".join(c.ancestries[p.ancestry]["traits"]))
        if p.conditions:
            lines.append("Conditions: " + ", ".join(p.conditions))
        return "\n".join(lines)

    # ------------------------------------------------------------------ persistence
    def save(self, name="quicksave") -> str:
        SAVE_DIR.mkdir(exist_ok=True)
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_") or "quicksave"
        path = SAVE_DIR / f"{safe}.json"
        with self.lock:
            path.write_text(json.dumps(self.s.to_dict(), indent=1), encoding="utf-8")
        return str(path)

    @staticmethod
    def _retire_old_autosave():
        """A brand new game would overwrite the last autosave. Keep one generation back."""
        old = SAVE_DIR / "autosave.json"
        if old.exists():
            try:
                old.replace(SAVE_DIR / "autosave_previous.json")
            except OSError:
                pass

    def autosave(self):
        try:
            self.save("autosave")
        except OSError:
            pass

    @staticmethod
    def load_state(path) -> GameState:
        return GameState.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
