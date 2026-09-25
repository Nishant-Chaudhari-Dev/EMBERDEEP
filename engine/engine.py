"""The rules engine. It owns the game state and is the only thing allowed to change it.

The AI Game Master proposes (1) how to resolve an action and (2) story consequences.
This module rolls the dice, applies the rules, and validates/clamps every change so the
model can improvise freely without breaking balance or continuity."""
import difflib
import re

from . import rules as R
from .combat import CombatMixin
from .content import Content
from .dice import Dice
from .models import Character, GameState, Location, Monster, NPC, Quest


def result(actor, kind, text, success=None, **kw) -> dict:
    d = {"actor": actor, "kind": kind, "text": text, "success": success,
         "refund": False, "extra_action": False}
    d.update(kw)
    return d


def _norm(s: str) -> str:
    s = str(s or "").lower().strip()
    s = re.sub(r"^(the|a|an|my|our)\s+", "", s)
    return re.sub(r"[^a-z0-9' ]+", " ", s).strip()


def _parse_cr(text: str) -> float:
    """'custom:0.5', 'custom 1/2', 'custom_2', 'cr 3' -> float CR. Defaults to 1/4."""
    m = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if m and int(m.group(2)):
        return int(m.group(1)) / int(m.group(2))
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else 0.25


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def fmt_d20(raw: list, mode: str) -> str:
    if len(raw) == 1:
        return f"d20({raw[0]})"
    return f"d20 {'adv' if mode == 'advantage' else 'dis'}({raw[0]},{raw[1]})"


class Engine(CombatMixin):
    def __init__(self, state: GameState = None, content: Content = None, dice: Dice = None):
        self.s = state or GameState()
        self.c = content or Content()
        self.dice = dice or Dice()
        self.notes = []   # system messages for the UI (initiative, level ups, rejected effects)

    # ------------------------------------------------------------------ lookup
    def pcs(self, living=True):
        return [p for p in self.s.characters.values() if not (living and p.dead)]

    def enemies(self):
        return [m for m in self.s.monsters.values() if m.alive]

    def get(self, cid):
        return self.s.characters.get(cid) or self.s.monsters.get(cid)

    def find(self, name, pcs=True, monsters=True, npcs=True):
        """Fuzzy lookup of a character, monster or NPC by what the AI or player called it."""
        key = _norm(name)
        if not key:
            return None
        pool = []
        if pcs:
            pool += [(p.name, p) for p in self.s.characters.values()]
        if monsters:
            pool += [(m.name, m) for m in self.s.monsters.values() if m.alive]
        if npcs:
            pool += [(n.name, n) for n in self.s.npcs.values() if n.alive]
        for label, obj in pool:
            if _norm(label) == key or getattr(obj, "id", None) == name:
                return obj
        for label, obj in pool:
            nl = _norm(label)
            if key in nl or nl in key or nl.split(" ")[0] == key.split(" ")[0]:
                return obj
        labels = [_norm(l) for l, _ in pool]
        close = difflib.get_close_matches(key, labels, n=1, cutoff=0.6)
        if close:
            return pool[labels.index(close[0])][1]
        return None

    def find_pc(self, name):
        return self.find(name, monsters=False, npcs=False)

    # ------------------------------------------------------------------ math
    def mod(self, c, ab):
        return R.mod(c.abilities.get(ab, 10))

    def prof(self, c):
        return R.prof_bonus(c.level) if c.kind == "pc" else 0

    def cond_modes(self, c, key):
        return [R.CONDITIONS[n][key] for n in c.conditions if key in R.CONDITIONS.get(n, {})]

    def consume(self, c, key):
        for n in list(c.conditions):
            if n in R.CONSUMED_ON_USE and key in R.CONDITIONS.get(n, {}):
                del c.conditions[n]

    def lucky(self, c):
        return c.kind == "pc" and c.ancestry == "halfling"

    def caster_ability(self, pc):
        return self.c.classes.get(pc.cls, {}).get("caster")

    # ------------------------------------------------------------------ rolls
    def check(self, c, ability, skill=None, dc=15, mode="normal"):
        skill = skill if skill in R.SKILLS else None
        if ability not in R.ABILITIES:
            ability = R.SKILLS[skill] if skill else "WIS"
        bonus = self.mod(c, ability)
        if skill and skill in getattr(c, "skills", []):
            bonus += self.prof(c)
        mode = R.combine(mode, *self.cond_modes(c, "check"))
        self.consume(c, "check")
        d, raw = self.dice.d20(mode, self.lucky(c))
        total = d + bonus
        ok = total >= dc
        label = skill.replace("_", " ").title() if skill else ability
        text = (f"{c.name} {label} ({ability}) check: {fmt_d20(raw, mode)} {bonus:+d} = {total} "
                f"vs DC {dc}: {'SUCCESS' if ok else 'FAILURE'}")
        return ok, total, text

    def save(self, c, ability, dc, mode="normal"):
        if ability in ("STR", "DEX") and any(R.CONDITIONS.get(n, {}).get("fail_str_dex") for n in c.conditions):
            return False, 0, f"{c.name} {ability} save vs DC {dc}: automatic FAILURE"
        bonus = self.mod(c, ability) + (self.prof(c) if ability in getattr(c, "saves", []) else 0)
        d, raw = self.dice.d20(mode, self.lucky(c))
        total = d + bonus
        ok = total >= dc
        return ok, total, (f"{c.name} {ability} save: {fmt_d20(raw, mode)} {bonus:+d} = {total} "
                           f"vs DC {dc}: {'SUCCESS' if ok else 'FAILURE'}")

    def attack_mode(self, attacker, target, mode="normal"):
        return R.combine(mode, *self.cond_modes(attacker, "attack"), *self.cond_modes(target, "attacked"))

    def attack(self, attacker, target, bonus, damage, dtype, mode="normal", label="attack", extra=None):
        """mode must already be final (see attack_mode). Returns (hit, damage, text)."""
        self.consume(attacker, "attack")
        d, raw = self.dice.d20(mode, self.lucky(attacker))
        crit, fumble = d == 20, d == 1
        total = d + bonus
        hit = crit or (not fumble and total >= target.ac)
        text = (f"{attacker.name} {label} vs {target.name}: {fmt_d20(raw, mode)} {bonus:+d} = {total} "
                f"vs AC {target.ac}: ")
        if not hit:
            return False, 0, text + ("MISS (natural 1)" if fumble else "MISS")
        dmg = max(1, self.dice.roll(damage, crit).total)
        if extra:
            dmg += self.dice.roll(extra, crit).total
        dmg = self.adjust_damage(target, dmg, dtype)
        text += f"{'CRITICAL HIT' if crit else 'HIT'} for {dmg} {dtype}"
        if extra:
            text += " (includes sneak attack)"
        return True, dmg, text + self.damage(target, dmg, crit)

    def adjust_damage(self, target, dmg, dtype):
        traits = getattr(target, "traits", [])
        if f"vulnerable:{dtype}" in traits:
            return dmg * 2
        if f"resistant:{dtype}" in traits or (target.kind == "pc" and target.ancestry == "dwarf" and dtype == "poison"):
            return dmg // 2
        return dmg

    # ------------------------------------------------------------------ hp
    def damage(self, t, amount, crit=False) -> str:
        if amount <= 0:
            return ""
        if t.kind == "monster":
            t.hp = max(0, t.hp - amount)
            if t.hp == 0:
                self.on_monster_down(t)
                return f" ({t.name} falls)"
            return ""
        if t.dead:
            return ""
        if t.hp == 0:
            if amount >= t.max_hp:
                t.dead = True
                return f" ({t.name} is killed)"
            return self._death_fail(t, 2 if crit else 1)
        if amount - t.hp >= t.max_hp:
            t.hp, t.dead = 0, True
            return f" ({t.name} is killed outright)"
        if amount >= t.hp:
            for f in ("last_stand", "relentless"):
                if t.features.get(f, 0) > 0:
                    t.features[f] -= 1
                    t.hp = 1
                    return f" ({t.name} refuses to fall and stays at 1 HP)"
            t.hp = 0
            t.conditions["unconscious"] = {"rounds": 0, "expires": "end", "save": None}
            t.conditions.pop("stable", None)
            t.death_saves = {"success": 0, "fail": 0}
            return f" ({t.name} drops unconscious)"
        t.hp -= amount
        return ""

    def heal(self, t, amount) -> str:
        if t.kind == "pc" and t.dead:
            return f"{t.name} is beyond ordinary healing."
        before = t.hp
        t.hp = min(t.max_hp, t.hp + max(0, amount))
        if t.kind == "pc" and before == 0 and t.hp > 0:
            t.conditions.pop("unconscious", None)
            t.conditions.pop("stable", None)
            t.death_saves = {"success": 0, "fail": 0}
        return f"{t.name} regains {t.hp - before} HP ({t.hp}/{t.max_hp})"

    def _death_fail(self, pc, n) -> str:
        pc.conditions.pop("stable", None)
        pc.death_saves["fail"] += n
        if pc.death_saves["fail"] >= 3:
            pc.dead = True
            return f" ({pc.name} dies)"
        return f" ({pc.name} suffers {n} death save failure{'s' if n > 1 else ''})"

    def death_save(self, pc) -> str:
        d, _ = self.dice.d20("normal", self.lucky(pc))
        ds = pc.death_saves
        if d == 20:
            pc.hp = 1
            pc.conditions.pop("unconscious", None)
            pc.death_saves = {"success": 0, "fail": 0}
            return f"{pc.name} death save: natural 20! {pc.name} regains 1 HP and gets back up."
        if d == 1:
            ds["fail"] += 2
        elif d >= 10:
            ds["success"] += 1
        else:
            ds["fail"] += 1
        tally = f"({ds['success']} successes, {ds['fail']} failures)"
        if ds["fail"] >= 3:
            pc.dead = True
            return f"{pc.name} death save: d20({d}). {pc.name} has died."
        if ds["success"] >= 3:
            pc.conditions["stable"] = {"rounds": 0, "expires": "end", "save": None}
            pc.death_saves = {"success": 0, "fail": 0}
            return f"{pc.name} death save: d20({d}). {pc.name} is stable but unconscious."
        return f"{pc.name} death save: d20({d}) {tally}"

    # ------------------------------------------------------------------ conditions
    def add_condition(self, t, name, rounds=0, expires="end", save=None):
        t.conditions[str(name).lower()[:30]] = {"rounds": int(rounds), "expires": expires, "save": save}

    def start_turn(self, c):
        for n in [n for n, i in c.conditions.items() if i.get("expires") == "start"]:
            del c.conditions[n]
        if self.s.combat and c.id in self.s.combat.sneak_used:
            self.s.combat.sneak_used.remove(c.id)

    def end_turn(self, c) -> list:
        out = []
        for n, info in list(c.conditions.items()):
            sv = info.get("save")
            if sv:
                ok, _, text = self.save(c, sv["ability"], sv["dc"])
                if ok:
                    del c.conditions[n]
                    out.append(result(c.name, "save", f"{text}. {c.name} is no longer {n}.", True))
                    continue
            if info.get("rounds", 0) > 0 and info.get("expires") == "end":
                info["rounds"] -= 1
                if info["rounds"] == 0:
                    del c.conditions[n]
                    out.append(result(c.name, "condition", f"{c.name} is no longer {n}."))
        return out

    # ------------------------------------------------------------------ characters
    def create_character(self, name, player, ancestry, cls, scores=None, backstory="") -> Character:
        cl, an = self.c.classes[cls], self.c.ancestries[ancestry]
        scores = dict(scores) if scores else dict(zip(cl["priority"], R.STANDARD_ARRAY))
        for ab, b in an["bonus"].items():
            scores[ab] = scores.get(ab, 10) + b
        hp = max(1, cl["hit_die"] + R.mod(scores["CON"]))
        feats = dict(cl.get("features", {}))
        feats.update(an.get("features", {}))
        if self.s.solo:
            feats["last_stand"] = 1
        pc = Character(
            id=self.s.new_id("pc"), name=name.strip()[:30] or "Nameless", player=player,
            ancestry=ancestry, cls=cls, abilities=scores, max_hp=hp, hp=hp, ac=10,
            skills=sorted(set(cl["skills"]) | set(an.get("skills", []))), saves=list(cl["saves"]),
            weapons=list(cl["weapons"]), inventory=dict(cl.get("items", {})), gold=cl.get("gold", 0),
            spells=list(cl.get("spells", [])), slots=dict(R.CASTER_SLOTS[1]) if cl.get("caster") else {},
            features=feats, max_features=dict(feats), backstory=backstory[:500],
        )
        pc.ac = self.compute_ac(pc)
        self.s.characters[pc.id] = pc
        return pc

    def compute_ac(self, pc) -> int:
        cl = self.c.classes[pc.cls]
        dex = self.mod(pc, "DEX")
        armor = self.c.armor.get(cl.get("armor") or "", None)
        ac = armor["base"] + min(dex, armor["dex_cap"]) if armor else 10 + dex
        return ac + (2 if cl.get("shield") else 0)

    def award_xp(self, total) -> list:
        living = self.pcs()
        if not living or total <= 0:
            return []
        share = total // len(living)
        out = []
        for pc in living:
            pc.xp += share
            while pc.level < R.MAX_LEVEL and R.level_for_xp(pc.xp) > pc.level:
                out.append(self.level_up(pc))
        return out

    def level_up(self, pc) -> str:
        cl = self.c.classes[pc.cls]
        pc.level += 1
        gain = max(1, cl["hit_die"] // 2 + 1 + self.mod(pc, "CON"))
        pc.max_hp += gain
        pc.hp += gain
        pc.hit_dice += 1
        if cl.get("caster"):
            pc.slots = dict(R.CASTER_SLOTS[pc.level])
        for f, uses in cl.get("level_features", {}).get(str(pc.level), {}).items():
            pc.features[f] = uses
            pc.max_features[f] = uses
        extra = ""
        if pc.level in (4, 8):
            top = cl["priority"][0]
            pc.abilities[top] = min(20, pc.abilities[top] + 2)
            pc.ac = self.compute_ac(pc)
            extra = f", {top} +2"
        return f"{pc.name} reaches level {pc.level}! (+{gain} max HP{extra})"

    # ------------------------------------------------------------------ monsters
    def encounter_budget(self) -> int:
        return max(100, 125 * sum(p.level for p in self.pcs()))

    def spawn(self, name, base="", count=1, description="") -> list:
        raw = str(base or "custom").strip().lower()
        base = _slug(raw)
        cr = None
        if base not in self.c.monsters:
            closest = difflib.get_close_matches(base, list(self.c.monsters), n=1, cutoff=0.75)
            if closest and not raw.startswith("custom"):
                base = closest[0]
            else:
                cr = _parse_cr(raw)
        if base in self.c.monsters:
            t = self.c.monsters[base]
            tpl = dict(ac=t["ac"], hp=t["hp"], attacks=t["attacks"], abilities=t["abilities"],
                       xp=t["xp"], multiattack=t.get("multiattack", 1), traits=t.get("traits", []))
            name = name or t["name"]
        else:
            ac, hp, bonus, dmg, n_atk, xp = R.CR_TEMPLATES[R.nearest_cr(cr or 0.25)]
            tpl = dict(ac=ac, hp=hp, attacks=[{"name": "attack", "bonus": bonus, "damage": dmg, "type": "bludgeoning"}],
                       abilities={a: 12 for a in R.ABILITIES}, xp=xp, multiattack=n_atk, traits=[])
            base = "custom"
        count = max(1, min(int(count or 1), 8))
        room = self.encounter_budget() - sum(x.xp for x in self.enemies())
        count = max(1, min(count, room // max(1, tpl["xp"])))
        existing = {x.name for x in self.s.monsters.values()}
        made = []
        for i in range(count):
            label = name if count == 1 and name not in existing else f"{name} {i + 1}"
            n = i + 1
            while label in existing:
                n += 1
                label = f"{name} {n}"
            existing.add(label)
            mon = Monster(id=self.s.new_id("m"), name=label[:40], base=base, hp=tpl["hp"], max_hp=tpl["hp"],
                          description=description[:200], **{k: tpl[k] for k in ("ac", "attacks", "abilities", "xp", "multiattack", "traits")})
            self.s.monsters[mon.id] = mon
            if self.s.combat:
                self.join_combat(mon)
            made.append(mon)
        return made

    def spawn_from_npc(self, npc) -> Monster:
        text = f"{npc.name} {npc.description}".lower()
        base = "commoner"
        for kw, b in (("guard", "guard"), ("soldier", "guard"), ("knight", "guard"), ("bandit", "bandit"),
                      ("thug", "thug"), ("brigand", "bandit"), ("cultist", "cultist"), ("priest", "cultist"),
                      ("orc", "orc"), ("goblin", "goblin"), ("ogre", "ogre")):
            if kw in text:
                base = b
                break
        npc.disposition = "hostile"
        t = self.c.monsters[base]
        mon = Monster(id=self.s.new_id("m"), name=npc.name, base=base, ac=t["ac"], hp=t["hp"], max_hp=t["hp"],
                      attacks=t["attacks"], abilities=t["abilities"], xp=t["xp"], multiattack=t.get("multiattack", 1),
                      traits=t.get("traits", []), description=npc.description, npc_id=npc.id)
        self.s.monsters[mon.id] = mon
        if self.s.combat:
            self.join_combat(mon)
        return mon

    def on_monster_down(self, m):
        if self.s.combat:
            self.s.combat.xp_pool += m.xp
        if m.npc_id and m.npc_id in self.s.npcs:
            self.s.npcs[m.npc_id].alive = False

    # ------------------------------------------------------------------ action resolution
    def resolve(self, a: dict) -> dict:
        """Resolve one adjudicated action. a comes from the AI adjudicator (already normalized)."""
        pc = self.find_pc(a.get("actor", ""))
        if pc is None:
            return result(a.get("actor", "?"), "none", "")
        if not pc.conscious:
            return result(pc.name, "none", f"{pc.name} is unconscious and cannot act.")
        kind = a.get("kind", "none")
        mode = a.get("advantage", "normal")
        handler = getattr(self, f"_do_{kind}", None)
        if handler is None:
            return result(pc.name, kind, "", None)
        return handler(pc, a, mode)

    def _do_check(self, pc, a, mode):
        dc = max(5, min(30, int(a.get("dc") or 15)))
        ok, _, text = self.check(pc, a.get("ability"), a.get("skill"), dc, mode)
        return result(pc.name, "check", text, ok)

    def _do_impossible(self, pc, a, mode):
        return result(pc.name, "impossible", f"{pc.name}'s attempt cannot succeed.", False)

    def _offensive_targets(self, pc, names):
        found = []
        for n in names or []:
            obj = self.find(n)
            if obj is None or obj is pc:
                continue
            if isinstance(obj, NPC):
                obj = self.spawn_from_npc(obj)
            if obj not in found:
                found.append(obj)
        if not found and self.enemies():
            found = [self.enemies()[0]]
        if found and any(o.kind == "monster" for o in found) and not self.s.combat:
            self.notes.append(self.start_combat())
            self.s.combat.skip_once.append(pc.id)
        return found

    def _do_attack(self, pc, a, mode):
        targets = self._offensive_targets(pc, a.get("targets"))
        if not targets:
            return result(pc.name, "none", f"{pc.name} finds nothing to attack.")
        t = targets[0]
        wid = self.match_weapon(pc, a.get("using", ""))
        w = self.c.weapons[wid]
        props = w["props"]
        if "ranged" in props:
            ab = "DEX"
        elif "finesse" in props:
            ab = "DEX" if self.mod(pc, "DEX") >= self.mod(pc, "STR") else "STR"
        else:
            ab = "STR"
        m = self.mod(pc, ab)
        bonus = m + self.prof(pc)
        dmg = f"{w['damage']}{m:+d}" if m else w["damage"]
        final = self.attack_mode(pc, t, mode)
        extra = None
        combat = self.s.combat
        if (pc.features.get("sneak_attack") and ("finesse" in props or "ranged" in props)
                and combat and pc.id not in combat.sneak_used and final != "disadvantage"
                and (final == "advantage" or any(o.conscious and o.id != pc.id for o in self.pcs()))):
            extra = f"{(pc.level + 1) // 2}d6"
        hit, _, text = self.attack(pc, t, bonus, dmg, w["type"], final, label=w["name"].lower(), extra=extra)
        if hit and extra:
            combat.sneak_used.append(pc.id)
        return result(pc.name, "attack", text, hit)

    def match_weapon(self, pc, text) -> str:
        t = _norm(text)
        if re.search(r"\b(punch|fist|kick|unarmed|headbutt|elbow)", t):
            return "unarmed"
        for wid in pc.weapons:
            wname = self.c.weapons[wid]["name"].lower()
            if t and (wid.replace("_", " ") in t or wname in t or any(p in wname for p in t.split() if len(p) > 3)):
                return wid
        if re.search(r"\b(shoot|arrow|bolt|bow|fire at)", t):
            for wid in pc.weapons:
                if "ranged" in self.c.weapons[wid]["props"]:
                    return wid
        if not t or t in ("weapon", "melee"):
            return pc.weapons[0] if pc.weapons else "unarmed"
        return "improvised"

    def match_spell(self, pc, text):
        key = _slug(text)
        if key in pc.spells:
            return key
        names = {_slug(self.c.spells[s]["name"]): s for s in pc.spells if s in self.c.spells}
        for n, sid in names.items():
            if key and (f"_{key}_" in f"_{n}_" or f"_{n}_" in f"_{key}_"):
                return sid
        close = difflib.get_close_matches(key, list(names), n=1, cutoff=0.8)
        return names[close[0]] if close else None

    def _do_spell(self, pc, a, mode):
        sid = self.match_spell(pc, a.get("using", ""))
        if not sid:
            known = ", ".join(self.c.spells[s]["name"] for s in pc.spells if s in self.c.spells) or "none"
            return result(pc.name, "spell", f"{pc.name} doesn't know that spell. Known: {known}.", refund=True)
        sp = self.c.spells[sid]
        lvl = sp["level"]
        t = sp["type"]
        slot = 0
        avail = sorted(int(k) for k, v in pc.slots.items() if v > 0 and int(k) >= lvl) if lvl > 0 else []
        if lvl > 0 and not avail:
            return result(pc.name, "spell", f"{pc.name} has no spell slots left for {sp['name']}. A long rest restores them.", refund=True)
        targets = []
        if t in ("attack", "auto", "save"):
            targets = self._offensive_targets(pc, a.get("targets"))
            if not targets:
                return result(pc.name, "spell", f"{pc.name} has no target for {sp['name']}.", refund=True)
        if lvl > 0:
            want = int(a.get("slot_level") or 0)
            slot = want if want in avail else avail[0]
            pc.slots[str(slot)] -= 1
        ab = self.caster_ability(pc) or "INT"
        cmod = self.mod(pc, ab)
        head = f"{pc.name} casts {sp['name']}" + (f" (level {slot} slot)" if slot else "")
        if t == "utility":
            return result(pc.name, "spell", head + ".", None)

        expr = sp.get("damage") or sp.get("amount", "0")
        if sp.get("scales"):
            tier = 1 + (pc.level >= 5) + (pc.level >= 11) + (pc.level >= 17)
            expr = re.sub(r"^(\d+)d", lambda mm: f"{int(mm.group(1)) * tier}d", expr)
        if slot > lvl and sp.get("upcast"):
            expr += "+" + "+".join([sp["upcast"]] * (slot - lvl))
        expr = expr.replace("MOD", str(cmod))

        if t == "heal":
            target = self.find_pc((a.get("targets") or [""])[0]) or pc
            amt = self.dice.roll(expr).total
            return result(pc.name, "spell", f"{head} on {target.name}: {self.heal(target, amt)}", True)

        if t == "attack":
            tg = targets[0]
            final = self.attack_mode(pc, tg, mode)
            hit, _, text = self.attack(pc, tg, self.prof(pc) + cmod, expr, sp.get("dtype", "force"), final, label=sp["name"])
            return result(pc.name, "spell", f"{head}. {text}", hit)
        if t == "auto":
            tg = targets[0]
            dmg = self.adjust_damage(tg, self.dice.roll(expr).total, sp.get("dtype", "force"))
            return result(pc.name, "spell", f"{head}: automatically hits {tg.name} for {dmg} {sp.get('dtype', 'force')}{self.damage(tg, dmg)}", True)
        # saving throw spells
        dc = 8 + self.prof(pc) + cmod
        hits = targets if sp.get("area") else targets[:1]
        rolled = self.dice.roll(expr).total if sp.get("damage") else 0
        parts = []
        for tg in hits:
            ok, _, stext = self.save(tg, sp["save"], dc)
            line = stext
            if sp.get("damage"):
                dmg = rolled // 2 if ok and sp.get("half") else (0 if ok else rolled)
                dmg = self.adjust_damage(tg, dmg, sp.get("dtype", "force"))
                line += f", takes {dmg} {sp.get('dtype')}" + self.damage(tg, dmg)
            if sp.get("condition") and not ok:
                save = {"ability": sp["save"], "dc": dc} if sp.get("save_ends") else None
                self.add_condition(tg, sp["condition"], sp.get("rounds", 1), "end", save)
                line += f", is {sp['condition']}"
            parts.append(line)
        return result(pc.name, "spell", f"{head} (save DC {dc}). " + "; ".join(parts), None)

    def _do_item(self, pc, a, mode):
        want = _norm(a.get("using", ""))
        names = list(pc.inventory)
        name = next((n for n in names if _norm(n) == want), None) or next(
            (n for n in names if want and (want in _norm(n) or _norm(n) in want)), None)
        if not name:
            close = difflib.get_close_matches(want, [_norm(n) for n in names], n=1, cutoff=0.6)
            name = names[[_norm(n) for n in names].index(close[0])] if close else None
        if not name:
            return result(pc.name, "item", f"{pc.name} doesn't have '{a.get('using', '')}'.", refund=True)
        info = self.c.items.get(name.lower())
        target = self.find_pc((a.get("targets") or [""])[0]) or pc
        if not info:
            return result(pc.name, "item", f"{pc.name} uses {name}.", None)
        text = f"{pc.name} uses {name}"
        if info["use"] == "heal":
            text += f" on {target.name}: " + self.heal(target, self.dice.roll(info["amount"]).total)
        elif info["use"] == "stabilize":
            if target.hp == 0 and not target.dead:
                target.conditions["stable"] = {"rounds": 0, "expires": "end", "save": None}
                target.death_saves = {"success": 0, "fail": 0}
                text += f": {target.name} is stabilized."
            else:
                text += f" on {target.name}, who does not need stabilizing."
        elif info["use"] == "cure":
            target.conditions.pop(info["condition"], None)
            text += f": {target.name} is no longer {info['condition']}."
        if info.get("consumed"):
            pc.inventory[name] -= 1
            if pc.inventory[name] <= 0:
                del pc.inventory[name]
        return result(pc.name, "item", text, True)

    def _do_feature(self, pc, a, mode):
        f = _slug(a.get("using", ""))
        f = "second_wind" if "second" in f or "wind" in f else ("action_surge" if "surge" in f else f)
        if pc.features.get(f, 0) <= 0:
            if f in ("second_wind", "action_surge"):
                return result(pc.name, "feature", f"{pc.name} has no uses of {f.replace('_', ' ')} left.", refund=True)
            return result(pc.name, "feature", "", None)
        pc.features[f] -= 1
        if f == "second_wind":
            amt = self.dice.roll(f"1d10+{pc.level}").total
            return result(pc.name, "feature", f"{pc.name} uses Second Wind: {self.heal(pc, amt)}", True, extra_action=True)
        if f == "action_surge":
            return result(pc.name, "feature", f"{pc.name} uses Action Surge and may act again.", True, extra_action=True)
        return result(pc.name, "feature", f"{pc.name} uses {f.replace('_', ' ')}.", True)

    def _do_dodge(self, pc, a, mode):
        self.add_condition(pc, "dodging", 0, "start")
        return result(pc.name, "dodge", f"{pc.name} takes the Dodge action (attacks against them have disadvantage).", None)

    def _do_help(self, pc, a, mode):
        ally = self.find_pc((a.get("targets") or [""])[0])
        if not ally or ally is pc:
            ally = next((p for p in self.pcs() if p is not pc and p.conscious), None)
        if not ally:
            return result(pc.name, "help", f"{pc.name} has no one to help.", None)
        self.add_condition(ally, "helped", 0, "end")
        return result(pc.name, "help", f"{pc.name} helps {ally.name} (advantage on their next roll).", True)

    def _do_hide(self, pc, a, mode):
        dc = int(a.get("dc") or 0)
        if self.enemies():
            dc = max(dc, 10 + max(self.mod(m, "WIS") for m in self.enemies()))
        ok, _, text = self.check(pc, "DEX", "stealth", max(5, dc or 12), mode)
        if ok:
            self.add_condition(pc, "hidden", 0, "end")
        return result(pc.name, "hide", text + (f". {pc.name} is hidden." if ok else ""), ok)

    def _do_rest(self, pc, a, mode):
        if self.s.combat:
            return result(pc.name, "rest", "You can't rest during combat.", refund=True)
        kind = a.get("rest") if a.get("rest") in ("short", "long") else "short"
        lines = []
        for p in self.pcs():
            if kind == "long":
                p.hp = p.max_hp
                p.slots = dict(R.CASTER_SLOTS[p.level]) if self.caster_ability(p) else {}
                p.features = dict(p.max_features)
                p.hit_dice = min(p.level, p.hit_dice + max(1, p.level // 2))
                p.conditions = {}
                p.death_saves = {"success": 0, "fail": 0}
            else:
                die = self.c.classes[p.cls]["hit_die"]
                spent = 0
                while p.hit_dice > 0 and p.max_hp - p.hp >= die // 2:
                    p.hit_dice -= 1
                    spent += 1
                    self.heal(p, max(1, self.dice.roll(f"1d{die}").total + self.mod(p, "CON")))
                for f in ("second_wind", "action_surge"):
                    if f in p.max_features:
                        p.features[f] = p.max_features[f]
                if p.hp == 0 and not p.dead:
                    p.hp = 1
                    p.conditions.pop("unconscious", None)
                lines.append(f"{p.name} spends {spent} hit dice ({p.hp}/{p.max_hp} HP)" if spent else f"{p.name} rests ({p.hp}/{p.max_hp} HP)")
        self.advance_time(8 if kind == "long" else 1)
        text = "The party takes a long rest: HP, spell slots and abilities restored." if kind == "long" else "Short rest. " + "; ".join(lines)
        return result(pc.name, "rest", text, True)

    # ------------------------------------------------------------------ world
    def advance_time(self, hours):
        self.s.hour += int(hours)
        while self.s.hour >= 24:
            self.s.hour -= 24
            self.s.day += 1

    def location(self):
        return self.s.locations.get(self.s.current_location)

    def find_location(self, name):
        key = _norm(name)
        for loc in self.s.locations.values():
            if _norm(loc.name) == key:
                return loc
        names = [_norm(l.name) for l in self.s.locations.values()]
        close = difflib.get_close_matches(key, names, n=1, cutoff=0.8)
        return list(self.s.locations.values())[names.index(close[0])] if close else None

    def add_location(self, name, description="") -> Location:
        loc = self.find_location(name)
        if loc:
            if description and not loc.description:
                loc.description = description[:400]
            return loc
        loc = Location(id=self.s.new_id("loc"), name=name.strip()[:60], description=description[:400])
        self.s.locations[loc.id] = loc
        return loc

    def move_party(self, name, description="") -> str:
        here = self.location()
        loc = self.add_location(name, description)
        if here and loc.id != here.id:
            if loc.id not in here.exits:
                here.exits.append(loc.id)
            if here.id not in loc.exits:
                loc.exits.append(here.id)
        self.s.current_location = loc.id
        loc.visited = True
        return f"The party is now at {loc.name}."

    def add_npc(self, name, disposition="neutral", description="") -> NPC:
        existing = self.find(name, pcs=False, monsters=False)
        if isinstance(existing, NPC) and _norm(existing.name) == _norm(name):
            return existing
        npc = NPC(id=self.s.new_id("npc"), name=name.strip()[:40], description=description[:300],
                  disposition=disposition if disposition in ("friendly", "neutral", "hostile") else "neutral",
                  location=self.s.current_location)
        self.s.npcs[npc.id] = npc
        return npc

    # ------------------------------------------------------------------ AI-proposed effects
    def apply_effects(self, effects: list) -> list:
        """Validate and apply story consequences proposed by the narrator. Returns UI notes."""
        out = []
        for e in (effects or [])[:12]:
            try:
                msg = self._apply(e)
            except Exception as exc:  # a malformed effect never crashes the game
                msg = None
                self.notes.append(f"(ignored malformed effect {e.get('op')}: {exc})")
            if msg:
                out.append(msg)
        return out

    def _apply(self, e) -> str:
        op = e.get("op", "")
        tgt, val, amt, txt = str(e.get("target", "")), str(e.get("value", "")), int(e.get("amount") or 0), str(e.get("text", ""))
        lvl = max(1, round(sum(p.level for p in self.pcs()) / max(1, len(self.pcs()))))
        if op in ("damage", "heal"):
            t = self.find(tgt, npcs=False)
            if not t:
                return None
            if op == "damage":
                amt = max(1, min(amt, 6 * lvl + 4))
                return f"{t.name} takes {amt} damage{f' ({txt})' if txt else ''}{self.damage(t, amt)}"
            return self.heal(t, max(1, amt))
        if op in ("give_item", "take_item"):
            pc = self.find_pc(tgt) or (self.pcs()[0] if self.pcs() else None)
            if not pc or not val:
                return None
            item = val.strip()[:60]
            q = max(1, min(amt or 1, 20))
            if op == "give_item":
                pc.inventory[item] = pc.inventory.get(item, 0) + q
                return f"{pc.name} gains {item}" + (f" x{q}" if q > 1 else "")
            match = next((n for n in pc.inventory if _norm(n) == _norm(item)), None)
            if match:
                pc.inventory[match] -= q
                if pc.inventory[match] <= 0:
                    del pc.inventory[match]
                return f"{pc.name} loses {match}"
            return None
        if op == "gold":
            pc = self.find_pc(tgt) or (self.pcs()[0] if self.pcs() else None)
            if not pc:
                return None
            amt = max(-pc.gold, min(amt, 100 * lvl))
            pc.gold += amt
            return f"{pc.name} {'gains' if amt >= 0 else 'spends'} {abs(amt)} gp ({pc.gold} gp)" if amt else None
        if op == "xp":
            amt = max(0, min(amt, 100 * sum(p.level for p in self.pcs())))
            ups = self.award_xp(amt)
            return (f"The party earns {amt} XP. " + " ".join(ups)).strip() if amt else None
        if op == "condition":
            t = self.find(tgt, npcs=False)
            if not t or not val:
                return None
            self.add_condition(t, val, max(0, min(amt, 100)))
            return f"{t.name} is {val.lower()}"
        if op == "remove_condition":
            t = self.find(tgt, npcs=False)
            if t and t.conditions.pop(val.lower(), None) is not None:
                return f"{t.name} is no longer {val.lower()}"
            return None
        if op == "move":
            return self.move_party(val or tgt, txt) if (val or tgt) else None
        if op == "add_npc":
            if not tgt:
                return None
            npc = self.add_npc(tgt, val, txt)
            return f"Met {npc.name}"
        if op == "update_npc":
            npc = self.find(tgt, pcs=False, monsters=False)
            if not isinstance(npc, NPC):
                return None
            if val == "dead":
                npc.alive = False
            elif val in ("friendly", "neutral", "hostile"):
                npc.disposition = val
            if txt:
                npc.notes = (npc.notes + [txt[:120]])[-5:]
            return None
        if op == "quest":
            title = (tgt or txt)[:80]
            if not title:
                return None
            q = next((q for q in self.s.quests.values() if _norm(q.title) == _norm(title)), None)
            status = val if val in ("active", "done", "failed") else "active"
            if not q:
                q = Quest(id=self.s.new_id("q"), title=title, status=status, notes=txt[:300])
                self.s.quests[q.id] = q
                return f"New quest: {q.title}"
            changed = q.status != status
            q.status = status
            if txt:
                q.notes = txt[:300]
            return f"Quest {status}: {q.title}" if changed else None
        if op == "flag":
            if tgt and len(self.s.flags) < 80:
                self.s.flags[_slug(tgt)[:40]] = val[:80]
            return None
        if op == "spawn_enemy":
            made = self.spawn(tgt or "Foe", val, amt or 1, txt)
            if not self.s.combat:
                self.notes.append(self.start_combat())
            return "Enemies appear: " + ", ".join(m.name for m in made)
        if op == "end_combat" and self.s.combat:
            return self.end_combat("truce")
        if op == "time":
            self.advance_time(max(0, min(amt, 72)))
            return None
        return None

    # ------------------------------------------------------------------ snapshot for UIs
    def snapshot(self) -> dict:
        s = self.s
        loc = self.location()
        cur = s.combat.current() if s.combat else ""
        return {
            "day": s.day, "hour": s.hour,
            "location": {"name": loc.name, "description": loc.description} if loc else None,
            "party": [{"id": p.id, "name": p.name, "player": p.player, "label": f"{self.c.ancestries[p.ancestry]['name']} {self.c.classes[p.cls]['name']} {p.level}",
                       "hp": p.hp, "max_hp": p.max_hp, "ac": p.ac, "xp": p.xp, "gold": p.gold, "dead": p.dead,
                       "conditions": list(p.conditions), "slots": p.slots, "features": p.features,
                       "inventory": p.inventory, "active": p.id == cur} for p in s.characters.values()],
            "enemies": [{"name": m.name, "state": R.health_word(m.hp, m.max_hp), "active": m.id == cur,
                         "conditions": list(m.conditions)} for m in s.monsters.values() if m.alive],
            "combat": {"round": s.combat.round, "current": cur} if s.combat else None,
            "quests": [{"title": q.title, "status": q.status} for q in s.quests.values()],
        }
