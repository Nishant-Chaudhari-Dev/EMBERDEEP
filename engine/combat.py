"""Turn-based combat. Monsters are run by deterministic tactics (fast, fair, no AI call per
monster); the AI narrates the whole exchange afterwards in one pass."""
from . import rules as R
from .models import Combat


def _r(actor, kind, text, success=None, **kw):
    d = {"actor": actor, "kind": kind, "text": text, "success": success, "refund": False, "extra_action": False}
    d.update(kw)
    return d


class CombatMixin:
    def _init_roll(self, c):
        return self.dice.die(20) + self.mod(c, "DEX")

    def start_combat(self) -> str:
        fighters = [p for p in self.pcs() if not p.dead] + self.enemies()
        init = {c.id: self._init_roll(c) for c in fighters}
        order = sorted(init, key=lambda cid: (init[cid], self.mod(self.get(cid), "DEX")), reverse=True)
        self.s.combat = Combat(order=order, initiative=init)
        return "Roll initiative! " + ", ".join(f"{self.get(c).name} {init[c]}" for c in order)

    def join_combat(self, creature):
        cb = self.s.combat
        if creature.id in cb.order:
            return
        cb.initiative[creature.id] = self._init_roll(creature)
        pos = next((i for i, cid in enumerate(cb.order) if cb.initiative[cid] < cb.initiative[creature.id]), len(cb.order))
        cb.order.insert(pos, creature.id)
        if pos <= cb.index:
            cb.index += 1

    def next_turn(self):
        cb = self.s.combat
        if not cb:
            return
        cb.index += 1
        if cb.index >= len(cb.order):
            cb.index = 0
            cb.round += 1

    def current_pc(self):
        """The PC whose turn it is, if combat is waiting on a player."""
        if not self.s.combat:
            return None
        c = self.get(self.s.combat.current())
        return c if c is not None and c.kind == "pc" and c.conscious else None

    def run_until_pc(self) -> list:
        """Advance combat until a conscious PC must act (or combat ends). Returns results."""
        out = []
        for _ in range(300):
            cb = self.s.combat
            if not cb:
                break
            end = self.check_combat_end()
            if end:
                out.append(end)
                break
            cid = cb.current()
            c = self.get(cid)
            if c is None or (c.kind == "monster" and not c.alive) or (c.kind == "pc" and c.dead):
                self.next_turn()
                continue
            if cid in cb.skip_once:
                cb.skip_once.remove(cid)
                self.next_turn()
                continue
            self.start_turn(c)
            if c.kind == "pc":
                if c.hp == 0:
                    if "stable" not in c.conditions:
                        out.append(_r(c.name, "death_save", self.death_save(c)))
                    if not c.conscious:   # a natural 20 brings them back up in time to act this turn
                        self.next_turn()
                        continue
                stuck = next((n for n in c.conditions if R.CONDITIONS.get(n, {}).get("skip")), None)
                if stuck:
                    out.append(_r(c.name, "condition", f"{c.name} is {stuck} and cannot act."))
                    out += self.end_turn(c)
                    self.next_turn()
                    continue
                return out
            out += self.monster_turn(c)
            out += self.end_turn(c)
            self.next_turn()
        return out

    def finish_pc_turn(self, pc) -> list:
        out = self.end_turn(pc)
        self.next_turn()
        return out + self.run_until_pc()

    def monster_turn(self, m) -> list:
        stuck = next((n for n in m.conditions if R.CONDITIONS.get(n, {}).get("skip")), None)
        if stuck:
            return [_r(m.name, "condition", f"{m.name} is {stuck} and cannot act.")]
        if "fearless" not in m.traits and not m.morale_checked and m.hp <= m.max_hp // 4:
            m.morale_checked = True
            ok, _, _ = self.save(m, "WIS", 10)
            if not ok:
                m.fled = True
                self.s.combat.xp_pool += m.xp // 2
                if m.npc_id in self.s.npcs:
                    self.s.npcs[m.npc_id].notes.append("fled from a fight with the party")
                return [_r(m.name, "flee", f"{m.name} breaks and flees the fight!")]
        if "prone" in m.conditions:
            del m.conditions["prone"]   # spends movement to stand up
        out = []

        def targets():
            live = [p for p in self.pcs() if p.conscious]
            unseen = [p for p in live if "hidden" not in p.conditions and "invisible" not in p.conditions]
            return unseen or live

        pool = targets()
        if not pool:
            return out
        target = self.dice.rng.choice(pool)
        for i in range(max(1, m.multiattack)):
            if not target.conscious:
                pool = targets()
                if not pool:
                    break
                target = self.dice.rng.choice(pool)
            atk = m.attacks[i % len(m.attacks)]
            mode = "normal"
            if "pack_tactics" in m.traits and any(o.alive and o.id != m.id for o in self.enemies()):
                mode = "advantage"
            final = self.attack_mode(m, target, mode)
            hit, _, text = self.attack(m, target, atk["bonus"], atk["damage"], atk["type"], final, label=atk["name"])
            if hit and atk.get("on_hit") and target.conscious:
                text += self._on_hit(target, atk["on_hit"])
            out.append(_r(m.name, "attack", text, hit))
        return out

    def _on_hit(self, target, oh) -> str:
        ok, _, text = self.save(target, oh["save"], oh["dc"])
        line = f"; {text}"
        if oh.get("damage"):
            dmg = self.dice.roll(oh["damage"]).total
            dmg = dmg // 2 if ok and oh.get("half") else (0 if ok else dmg)
            dmg = self.adjust_damage(target, dmg, oh.get("type", "poison"))
            if dmg:
                line += f", {dmg} {oh.get('type', 'poison')} damage" + self.damage(target, dmg)
        if oh.get("condition") and not ok:
            self.add_condition(target, oh["condition"], 1, "end")
            line += f", knocked {oh['condition']}" if oh["condition"] == "prone" else f", {oh['condition']}"
        return line

    def check_combat_end(self):
        if not self.s.combat:
            return None
        if not self.enemies():
            return _r("", "combat_end", self.end_combat("victory"), True, outcome="victory")
        if not any(p.conscious for p in self.pcs()):
            return _r("", "combat_end", self.end_combat("defeat"), False, outcome="defeat")
        return None

    def end_combat(self, outcome: str) -> str:
        cb = self.s.combat
        xp = cb.xp_pool if cb else 0
        if outcome == "truce":
            xp += sum(m.xp for m in self.enemies()) // 2
        self.s.combat = None
        self.applied["foes"].update(m.name for m in self.s.monsters.values())   # so narration can't revive them as NPCs
        self.s.monsters = {}
        for p in self.s.characters.values():
            for n in ("dodging", "hidden", "helped", "prone"):
                p.conditions.pop(n, None)
        if outcome == "defeat":
            alive = [p for p in self.s.characters.values() if not p.dead]
            for p in alive:
                p.hp = 1
                p.conditions.pop("unconscious", None)
                p.death_saves = {"success": 0, "fail": 0}
            self.s.flags["last_defeat"] = f"day {self.s.day}"
            if not alive:
                return "The whole party has fallen. This is the end of the tale, unless you load a save."
            return "The party is defeated. Every survivor wakes later at 1 HP; the GM decides what happened."
        for p in self.s.characters.values():
            if p.hp == 0 and not p.dead:   # allies tend to the fallen once the fighting stops
                p.conditions["stable"] = {"rounds": 0, "expires": "end", "save": None}
                p.death_saves = {"success": 0, "fail": 0}
        ups = self.award_xp(xp)
        head = "Victory!" if outcome == "victory" else "The fight ends."
        return (f"{head} The party earns {xp} XP. " + " ".join(ups)).strip()
