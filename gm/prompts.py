"""Prompts and output schemas for the AI Game Master.

Design rule: the model never does arithmetic or decides success. It (1) picks which rule applies
(adjudicate) and (2) tells the story of results the engine already computed (narrate).
Context is rebuilt from engine state every call, so the model can't 'forget' HP, items or NPCs."""
from engine import rules as R

KINDS = ["none", "check", "attack", "spell", "item", "feature", "dodge", "help", "hide", "rest", "impossible"]
OPS = ["damage", "heal", "give_item", "take_item", "gold", "xp", "condition", "remove_condition", "move",
       "add_npc", "update_npc", "quest", "flag", "spawn_enemy", "end_combat", "time"]

ADJUDICATE_SCHEMA = {
    "type": "object",
    "properties": {"actions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "actor": {"type": "string"},
            "intent": {"type": "string"},
            "kind": {"type": "string", "enum": KINDS},
            "ability": {"type": "string", "enum": R.ABILITIES + ["NONE"]},
            "skill": {"type": "string", "enum": list(R.SKILLS) + ["none"]},
            "dc": {"type": "integer"},
            "advantage": {"type": "string", "enum": ["normal", "advantage", "disadvantage"]},
            "targets": {"type": "array", "items": {"type": "string"}},
            "using": {"type": "string"},
            "slot_level": {"type": "integer"},
            "rest": {"type": "string", "enum": ["none", "short", "long"]},
        },
        "required": ["actor", "intent", "kind", "ability", "skill", "dc", "advantage", "targets", "using", "slot_level", "rest"],
    }}},
    "required": ["actions"],
}

NARRATE_SCHEMA = {
    "type": "object",
    "properties": {
        "narration": {"type": "string"},
        "effects": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": OPS},
                "target": {"type": "string"},
                "value": {"type": "string"},
                "amount": {"type": "integer"},
                "text": {"type": "string"},
            },
            "required": ["op", "target", "value", "amount", "text"],
        }},
    },
    "required": ["narration", "effects"],
}

OPENING_SCHEMA = {
    "type": "object",
    "properties": {
        "narration": {"type": "string"},
        "location": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
                     "required": ["name", "description"]},
        "npcs": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "disposition": {"type": "string", "enum": ["friendly", "neutral", "hostile"]},
            "description": {"type": "string"}}, "required": ["name", "disposition", "description"]}},
        "quest": {"type": "object", "properties": {"title": {"type": "string"}, "notes": {"type": "string"}},
                  "required": ["title", "notes"]},
    },
    "required": ["narration", "location", "npcs", "quest"],
}

IDENTITY = """You are the Game Master of Emberdeep, a tabletop fantasy roleplaying game using 5e-style rules.
A separate rules engine owns every number: dice, hit points, damage, spell slots, gold and inventory.
You never roll dice, never invent mechanical results, and never contradict the engine.
You run a living, consistent world: NPCs have motives and remember the party, choices have consequences,
and players are free to attempt anything they can describe."""

ADJUDICATE = IDENTITY + """

TASK: decide how the rules engine should resolve each declared player action. Output one entry per action, in order, with actor set to the exact character name.

PRINCIPLES
- Players may attempt anything. Never refuse; find the fitting roll instead.
- Only call for a roll when the outcome is uncertain AND failure would be interesting. Otherwise use kind "none" (talking, looking around, travelling, easy tasks).
- DC ladder: 5 very easy, 10 easy, 15 medium, 20 hard, 25 very hard, 30 nearly impossible.
- Reward clever plans, teamwork and good positioning with advantage. Bad conditions give disadvantage.
- Social actions against NPCs are checks (persuasion, deception, intimidation, insight) with a DC based on the NPC's attitude and what is asked.
- Improvised magic or stunts beyond a character's listed spells are checks (for example Arcana with the casting ability).
- Physically absurd actions (punching the moon) are "impossible".
- Harming any creature, even a friendly NPC, is "attack" (weapon or unarmed) or "spell", and starts combat if not already in it.

KINDS and fields to fill
none: nothing extra | check: ability, skill (or "none"), dc | attack: targets, using = weapon name
spell: using = spell id from the character's list, targets, slot_level (0 = lowest available)
item: using = item name from inventory, targets | feature: using = second_wind or action_surge
dodge | help: targets = ally | hide: dc | rest: rest = short or long | impossible
Unused fields: ability "NONE", skill "none", dc 0, advantage "normal", targets [], using "", slot_level 0, rest "none".

EXAMPLE
Declared: 1. Brakka: I shoulder-charge the rotten door  2. Lise: I cast fire bolt at the goblin on the stairs  3. Pip: I tell the guard we're expected, flashing the mayor's seal
{"actions":[
{"actor":"Brakka","intent":"break the door","kind":"check","ability":"STR","skill":"athletics","dc":12,"advantage":"advantage","targets":[],"using":"","slot_level":0,"rest":"none"},
{"actor":"Lise","intent":"burn the goblin","kind":"spell","ability":"NONE","skill":"none","dc":0,"advantage":"normal","targets":["Goblin"],"using":"fire_bolt","slot_level":0,"rest":"none"},
{"actor":"Pip","intent":"bluff past the guard","kind":"check","ability":"CHA","skill":"deception","dc":13,"advantage":"advantage","targets":["guard"],"using":"","slot_level":0,"rest":"none"}]}
(The rotten door gave advantage; the real seal gave advantage on the bluff.)"""

NARRATE = IDENTITY + """

TASK: narrate what happens, then list any NEW consequences as effects.

RULES FOR NARRATION
- RESOLVED lines are the truth and are already applied. Successes succeed, failures fail. Hits hit; misses miss.
- Do not create damage, heal or xp effects for anything in RESOLVED (including the XP for winning a fight); the engine already applied them.
- Second person, present tense, 60 to 170 words. Vivid, specific, sensory. No lists, no markdown, no dice numbers, no HP numbers.
- Give NPCs distinct voices and motives. Let the world react to what the players did.
- Never decide what player characters do, say or feel beyond what they declared.
- In combat, describe the exchange and end on whose turn is next. Out of combat, end on a clear situation that invites action. Never list options.

EFFECTS (only for consequences you introduce; empty list is fine). Unused fields: "" or 0.
damage / heal: target, amount, text = cause | give_item / take_item: target = character, value = item, amount = qty
gold: target = character, amount (negative = spend) | xp: amount (for clever non-combat achievements, 25 to 100)
condition / remove_condition: target, value = condition, amount = rounds (0 = until removed; paralyzed, stunned and unconscious last at most 3)
move: only when the party travels; value = the place's proper name (e.g. "Old Quarry"), text = one-line description if new
add_npc: people only, never enemies (spawn_enemy covers foes); target = name, value = friendly|neutral|hostile, text = who they are
update_npc: target = name, value = friendly|neutral|hostile|dead, text = what changed | quest: target = title, value = active|done|failed, text = notes
flag: target = key, value = fact to remember | time: amount = hours passed | end_combat: enemies surrender or flee
spawn_enemy: target = display name, value = bestiary id or custom:CR (e.g. custom:0.5), amount = count, text = description for custom
Register every new named NPC, place and quest with an effect so it persists. Spawning enemies starts combat."""

OPENING = IDENTITY + """

TASK: open the adventure. Using the premise and party, create the starting location, 1 to 3 NPCs with clear motives,
and one hook quest. Narration: 100 to 200 words, second person, present tense, ending on a moment that invites the players to act.
Do not start in combat. Weave in each character briefly."""

SUMMARIZE = """You maintain the campaign log for a tabletop RPG. Merge the previous summary and the new events into one updated
'story so far', max 180 words. Keep: names, promises, debts, clues, enemies made, open threads, where the party is. Drop flavor text.
Plain prose only."""

ASK = IDENTITY + """

A player is asking an out-of-character question. Answer briefly and honestly as the GM: rules, what their character would
reasonably know, or a recap. Do not advance the story or reveal secrets the characters haven't discovered.
Plain text, no markdown, at most 100 words."""

DEFAULT_PREMISE = ("A low-fantasy frontier. The village of Thornwick sits at the edge of the Emberdeep, a forest of charred "
                   "black pines that never finished burning. Something beneath the old quarry has woken.")


def party_block(e) -> str:
    lines = []
    for p in e.s.characters.values():
        cls = e.c.classes[p.cls]["name"]
        anc = e.c.ancestries[p.ancestry]["name"]
        state = "DEAD" if p.dead else f"HP {p.hp}/{p.max_hp}"
        bits = [f"{p.name} ({anc} {cls} {p.level}, player {p.player}): {state}, AC {p.ac}"]
        bits.append("weapons " + ", ".join(e.c.weapons[w]["name"] for w in p.weapons))
        if p.spells:
            bits.append("spells " + ", ".join(p.spells))
            bits.append("slots " + (", ".join(f"L{k}:{v}" for k, v in p.slots.items()) or "none"))
        feats = [f"{k}:{v}" for k, v in p.features.items() if k not in ("last_stand", "relentless", "sneak_attack")]
        if feats:
            bits.append("features " + ", ".join(feats))
        if p.inventory:
            bits.append("items " + ", ".join(f"{k}" + (f" x{v}" if v > 1 else "") for k, v in list(p.inventory.items())[:10]))
        if p.conditions:
            bits.append("conditions " + ", ".join(p.conditions))
        if p.backstory:
            bits.append("backstory: " + p.backstory[:120])
        lines.append("- " + "; ".join(bits))
    return "\n".join(lines)


def scene_block(e, mentioned: str = "") -> str:
    s = e.s
    loc = e.location()
    out = [f"Day {s.day}, {s.hour:02d}:00."]
    if loc:
        out.append(f"Location: {loc.name}. {loc.description}")
        exits = [s.locations[x].name for x in loc.exits if x in s.locations]
        if exits:
            out.append("Known routes: " + ", ".join(exits))
    here = [n for n in s.npcs.values() if n.alive and n.location == s.current_location]
    low = mentioned.lower()
    elsewhere = [n for n in s.npcs.values() if n.alive and n not in here and n.name.split(" ")[0].lower() in low]
    for n in here + elsewhere:
        where = "" if n in here else " (not here)"
        notes = f" Notes: {'; '.join(n.notes[-3:])}" if n.notes else ""
        out.append(f"NPC {n.name}{where} [{n.disposition}]: {n.description}.{notes}")
    others = [n.name for n in s.npcs.values() if n.alive and n not in here and n not in elsewhere]
    if others:
        out.append("Other known people: " + ", ".join(others[:12]))
    active = [q for q in s.quests.values() if q.status == "active"]
    for q in active[:5]:
        out.append(f"Quest '{q.title}': {q.notes}")
    if s.flags:
        out.append("Facts: " + "; ".join(f"{k}={v}" for k, v in list(s.flags.items())[-12:]))
    return "\n".join(out)


def combat_block(e) -> str:
    cb = e.s.combat
    if not cb:
        return ""
    order = []
    for cid in cb.order:
        c = e.get(cid)
        if c is None or (c.kind == "monster" and not c.alive):
            continue
        tag = " <- current turn" if cid == cb.current() else ""
        if c.kind == "monster":
            cond = f", {', '.join(c.conditions)}" if c.conditions else ""
            order.append(f"{c.name} [{R.health_word(c.hp, c.max_hp)}{cond}]{tag}")
        else:
            order.append(f"{c.name}{tag}")
    return f"COMBAT round {cb.round}. Initiative: " + " > ".join(order)


def bestiary_hint(e) -> str:
    return "Bestiary ids: " + ", ".join(f"{k} (CR {v['cr']})" for k, v in e.c.monsters.items()) + \
           f". Encounter budget for this party: about {e.encounter_budget()} XP total."


def story_block(state, recent: int = 8) -> str:
    out = []
    if state.summary:
        out.append("STORY SO FAR: " + state.summary)
    if state.log:
        out.append("RECENT:")
        for entry in state.log[-recent:]:
            who = "Players" if entry["role"] == "players" else "GM"
            out.append(f"{who}: {entry['text'][:600]}")
    return "\n".join(out)


def declared_block(declared) -> str:
    return "DECLARED ACTIONS:\n" + "\n".join(f"{i}. {pc.name}: {text}" for i, (pc, text) in enumerate(declared, 1))


def context(e, mentioned="") -> str:
    parts = ["PARTY:\n" + party_block(e), "SCENE:\n" + scene_block(e, mentioned)]
    cb = combat_block(e)
    if cb:
        parts.append(cb)
    story = story_block(e.s)
    if story:
        parts.append(story)
    return "\n\n".join(parts)
