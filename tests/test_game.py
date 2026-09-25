"""Run with:  python -m unittest discover tests
Uses a scripted fake model so every test is deterministic and needs no GPU."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.dice import Dice
from engine.engine import Engine
from engine.models import GameState
from gm.llm import LLM
from session import Session


def act(actor, kind="none", **kw):
    a = {"actor": actor, "intent": "", "kind": kind, "ability": "NONE", "skill": "none", "dc": 0,
         "advantage": "normal", "targets": [], "using": "", "slot_level": 0, "rest": "none"}
    a.update(kw)
    return a


class ScriptedLLM(LLM):
    """Returns queued replies per task; falls back to harmless defaults."""
    def __init__(self):
        self.queue = {"adjudicate": [], "narrate": [], "opening": []}
        self.calls = []

    def push(self, task, payload):
        self.queue[task].append(payload)

    def chat(self, messages, schema=None, temperature=0.7, max_tokens=600):
        props = (schema or {}).get("properties", {})
        task = "adjudicate" if "actions" in props else "narrate" if "effects" in props else "opening" if "location" in props else "text"
        self.calls.append((task, messages[-1]["content"]))
        if task in self.queue and self.queue[task]:
            return json.dumps(self.queue[task].pop(0))
        if task == "narrate":
            return json.dumps({"narration": "Things happen.", "effects": []})
        if task == "opening":
            return json.dumps({"narration": "You arrive.", "location": {"name": "Mill", "description": "An old mill."},
                               "npcs": [{"name": "Odo", "disposition": "friendly", "description": "a miller"}],
                               "quest": {"title": "Find the flour thief", "notes": "Someone steals flour."}})
        if task == "adjudicate":
            names = [l.split(": ")[0].split(". ", 1)[1] for l in messages[-1]["content"].split("DECLARED ACTIONS:")[-1].strip().splitlines() if ". " in l]
            return json.dumps({"actions": [act(n) for n in names]})
        return "summary text"


def new_session(n_players=2, seed=1):
    llm = ScriptedLLM()
    events = []
    s = Session(llm, emit=events.append, seed=seed)
    s.add_character("Rick", "Brakka", "half_orc", "fighter")
    if n_players > 1:
        s.add_character("Ann", "Lise", "elf", "wizard")
    if n_players > 2:
        s.add_character("Mo", "Tam", "dwarf", "cleric")
    s.begin("")
    return s, llm, events


class EngineTests(unittest.TestCase):
    def test_dice(self):
        d = Dice(5)
        for _ in range(200):
            r = d.roll("2d6+3")
            self.assertTrue(5 <= r.total <= 15)
        self.assertEqual(d.roll("4d6kh3").dice.__len__(), 3)
        self.assertEqual(d.roll("7").total, 7)
        with self.assertRaises(ValueError):
            d.roll("banana")

    def test_character_math(self):
        e = Engine(GameState(), dice=Dice(1))
        f = e.create_character("A", "p", "human", "fighter")
        self.assertEqual(f.ac, 18)                  # chain mail 16 + shield 2
        self.assertEqual(f.max_hp, 10 + 2)          # d10 + CON(14 -> +2... 13+1=14)
        w = e.create_character("B", "p", "elf", "wizard")
        self.assertEqual(w.slots, {"1": 2})
        self.assertIn("perception", w.skills)       # elf keen senses

    def test_effect_clamps(self):
        e = Engine(GameState(), dice=Dice(1))
        pc = e.create_character("A", "p", "human", "fighter")
        notes = e.apply_effects([
            {"op": "gold", "target": "A", "value": "", "amount": 999999, "text": ""},
            {"op": "damage", "target": "A", "value": "", "amount": 500, "text": "a falling piano"},
            {"op": "give_item", "target": "A", "value": "strange key", "amount": 1, "text": ""},
            {"op": "nonsense", "target": "", "value": "", "amount": 0, "text": ""},
        ])
        self.assertLessEqual(pc.gold, 10 + 100)    # capped per effect
        self.assertFalse(pc.dead)                  # 500 damage clamped, can't one-shot
        self.assertIn("strange key", pc.inventory)
        self.assertEqual(len(notes), 3)

    def test_custom_monster_and_budget(self):
        e = Engine(GameState(), dice=Dice(2))
        e.create_character("A", "p", "human", "fighter")
        made = e.spawn("Glass Wasp", "custom:0.5", 6, "a buzzing shard")
        self.assertEqual(len(made), 1)             # budget 125 XP at level 1 allows one CR 1/2
        self.assertEqual(made[0].xp, 100)
        self.assertEqual(made[0].base, "custom")

    def test_save_load_roundtrip(self):
        s, _, _ = new_session()
        s.engine.spawn("Goblin", "goblin", 2)
        s.engine.start_combat()
        data = json.loads(json.dumps(s.s.to_dict()))
        from engine.models import GameState as GS
        loaded = GS.from_dict(data)
        self.assertEqual(len(loaded.monsters), 2)
        self.assertEqual(loaded.combat.order, s.s.combat.order)
        self.assertEqual(loaded.characters[next(iter(loaded.characters))].name, "Brakka")


class SessionTests(unittest.TestCase):
    def test_opening_registers_world(self):
        s, _, ev = new_session()
        self.assertTrue(s.s.started)
        self.assertEqual(s.engine.location().name, "Mill")
        self.assertEqual(len(s.s.quests), 1)
        self.assertTrue(any(e["kind"] == "narration" for e in ev))

    def test_exploration_round_waits_for_everyone(self):
        s, llm, _ = new_session()
        b, l = list(s.s.characters)
        s.submit(b, "I look around")
        self.assertEqual(sum(1 for t, _ in llm.calls if t == "adjudicate"), 0)
        s.submit(l, "I read the sign")
        self.assertEqual(sum(1 for t, _ in llm.calls if t == "adjudicate"), 1)
        self.assertEqual(s.pending, {})

    def test_ambush_then_fight_to_victory(self):
        s, llm, _ = new_session(seed=4)
        b, l = list(s.s.characters)
        llm.push("adjudicate", {"actions": [act("Brakka"), act("Lise")]})
        llm.push("narrate", {"narration": "Wolves burst from the reeds!", "effects": [
            {"op": "spawn_enemy", "target": "Wolf", "value": "wolf", "amount": 2, "text": ""}]})
        s.submit(b, "walk on")
        s.submit(l, "walk on")
        self.assertIsNotNone(s.s.combat)
        for _ in range(40):
            if not s.s.combat:
                break
            pc = s.engine.current_pc()
            kind = "spell" if pc.name == "Lise" else "attack"
            llm.push("adjudicate", {"actions": [act(pc.name, kind, using="fire_bolt" if kind == "spell" else "longsword", targets=["wolf"])]})
            s.submit(pc.id, "fight")
        self.assertIsNone(s.s.combat)
        self.assertTrue(any(p.xp > 0 for p in s.s.characters.values()) or all(p.hp == 1 for p in s.s.characters.values()))

    def test_attacking_npc_starts_combat(self):
        s, llm, _ = new_session(n_players=1)
        pc = next(iter(s.s.characters.values()))
        llm.push("adjudicate", {"actions": [act("Brakka", "attack", targets=["Odo"], using="longsword")]})
        s.submit(pc.id, "I attack Odo")
        odo = next(n for n in s.s.npcs.values() if n.name == "Odo")
        self.assertEqual(odo.disposition, "hostile")

    def test_area_spell_and_hold_person(self):
        s, llm, _ = new_session(seed=11)
        e = s.engine
        e.spawn("Bandit", "bandit", 3)
        e.start_combat()
        lise = e.find_pc("Lise")
        r = e.resolve(act("Lise", "spell", using="burning hands", targets=["Bandit 1", "Bandit 2"]))
        self.assertIn("save DC 13", r["text"])
        self.assertEqual(lise.slots["1"], 1)
        r2 = e.resolve(act("Lise", "spell", using="hold person", targets=["Bandit 3"]))
        self.assertTrue(r2["refund"])              # no level 2 slots at level 1
        r3 = e.resolve(act("Lise", "spell", using="fireball", targets=["Bandit 3"]))
        self.assertTrue(r3["refund"])              # not a known spell

    def test_cleric_heals_downed_ally(self):
        s, llm, _ = new_session(n_players=3)
        e = s.engine
        brakka, tam = e.find_pc("Brakka"), e.find_pc("Tam")
        brakka.features["relentless"] = 0
        e.damage(brakka, brakka.hp)
        self.assertEqual(brakka.hp, 0)
        self.assertIn("unconscious", brakka.conditions)
        r = e.resolve(act("Tam", "spell", using="healing word", targets=["Brakka"]))
        self.assertGreater(brakka.hp, 0)
        self.assertNotIn("unconscious", brakka.conditions)

    def test_defeat_is_not_game_over(self):
        s, llm, _ = new_session(n_players=1, seed=2)
        e = s.engine
        pc = next(iter(s.s.characters.values()))
        e.spawn("Ogre", "ogre", 1)
        e.start_combat()
        pc.features = {}
        e.damage(pc, pc.hp)
        end = e.check_combat_end()
        self.assertEqual(end["outcome"], "defeat")
        self.assertEqual(pc.hp, 1)
        self.assertFalse(pc.dead)

    def test_bad_model_output_does_not_crash(self):
        s, llm, ev = new_session(n_players=1)
        pc = next(iter(s.s.characters.values()))
        llm.chat = lambda *a, **k: "I am a teapot, not JSON"
        s.submit(pc.id, "I dance")
        self.assertTrue(any(e["kind"] == "narration" for e in ev))


if __name__ == "__main__":
    unittest.main()
