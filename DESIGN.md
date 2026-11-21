# Emberdeep design notes

## The core idea

Local 7B to 14B models are decent storytellers and decent classifiers. They're bad bookkeepers: they forget HP, invent spell slots, and let players talk their way out of dying. So Emberdeep splits the job in two.

The **rules engine** (`engine/`) is plain deterministic Python. It owns all state and every number: dice, HP, AC, spell slots, inventory, conditions, initiative, XP, the world map, NPCs, quests and flags. It never calls the model.

The **game master** (`gm/`) is the model, and it does exactly two kinds of work, both through schema-constrained JSON:

1. **Adjudicate.** It turns a player's free text into a structured rules decision. "I try to talk the guard into letting us through" becomes a Persuasion check against DC 15, with advantage because the party bribed him earlier.
2. **Narrate.** It takes what the engine actually resolved and writes the story, and it can also propose consequences ("the guard hands over the key", "the bridge collapses, 2d6 to whoever was on it"). The engine validates, clamps and applies them.

The player can attempt anything, because the model maps any intent onto a small, complete set of mechanics. And the model can't break the game, because it only ever proposes. The engine decides.

```
 player text ──► GM.adjudicate ──► [action JSON] ──► Engine.resolve ──► dice results
                  (model call 1)                        (no model)          │
                                                                            ▼
 story + state ◄── Engine.apply_effects ◄── [effects JSON] ◄── GM.narrate ◄─┘
                   (validate + clamp)                          (model call 2)
```

## Turn flow

`session.py` drives both clients, and it's where the table rules live.

**Exploration.** Every conscious character declares an action. When all of them have declared, or someone forces it with `/go`, the round resolves as a batch: one adjudicate call covering every declaration, the engine resolves each action in order, and then one narrate call covers the whole round. So a four-player round costs two model calls, not eight, and the narration reads as one scene instead of four disconnected ones. Only one rest per round counts, so four people typing "we make camp" doesn't give four long rests.

**Combat.** Strict initiative. On a player's turn: one adjudicate call, the engine resolves it, and then monsters act until the next player's turn comes up. Monsters don't use the model at all. They follow deterministic tactics: pick a conscious, visible target, use their attacks, and make a Wisdom save to avoid fleeing once they drop to a quarter of their HP (unless they're fearless). Then one narrate call covers the player's action plus everything the monsters did. If initiative puts monsters ahead of the whole party, their opening moves get a narration of their own.

If an exploration action starts a fight (attacking an NPC, or the GM spawning an ambush), combat begins mid-round. Everyone who already acted that round skips their first combat turn, so a declared action isn't counted twice.

**Invalid actions.** If the engine rejects an action (a spell the character doesn't know, no slots left, no valid target, resting in combat), it gets refunded: nothing is spent, and in combat the player keeps their turn. The model's mistakes cost the player nothing.

## Model calls and schemas

All schemas live in `gm/prompts.py`. Backends enforce them at decode time. llama.cpp uses `response_format` with a JSON schema, which compiles to a grammar. Ollama uses `format` with the schema. So even small models produce parseable output nearly every time. `parse_json` also strips `<think>` blocks and code fences, just in case. If the output still won't parse, the GM retries once with a correction message, and then falls back safely (a neutral action, or a short "the GM stumbled" narration).

**Adjudicate** returns `{"actions": [...]}`, one entry per declared action. Each entry has the actor, a one-line intent, a `kind` from a fixed list (none, check, attack, spell, item, feature, dodge, help, hide, rest, impossible), and the fields that kind needs: ability, skill and DC for checks; targets and weapon for attacks; spell id, targets and slot level for spells; the item or feature being used; the rest type. The prompt includes a DC ladder (5 very easy through 30 nearly impossible) and few-shot examples. `GameMaster._normalize` then repairs whatever the model got wrong: unknown kinds become "none", unknown skills are dropped (the ability is inferred from the skill when missing), a check without a DC gets 15, and each result is matched back to its character by name, or by position if the name is off. The engine then clamps DCs to 5-30 and fuzzy-matches targets, weapons and spells against what actually exists.

**Narrate** returns `{"narration": "...", "effects": [...]}`. Each effect is `{op, target, value, amount, text}`, and `op` is one of: damage, heal, give_item, take_item, gold, xp, condition, remove_condition, move, add_npc, update_npc, quest, flag, spawn_enemy, end_combat, time. This lets the story change the world (a new location, a new NPC, a quest update, a trap) without the model ever touching state directly.

**Opening** creates the first scene from the premise: narration, a starting location, up to four NPCs, and a hook quest.

There are two more free-text calls, with no schema: **summarize**, which compresses old history, and **ask**, which answers out-of-character questions for `/ask`.

## Context and memory

Every call gets its context rebuilt fresh from the game state. Nothing depends on the model remembering an earlier call. The context holds:

- the party (exact HP and AC, weapons, spells and slots, features, conditions)
- the current scene: location, exits, NPCs who are present or were mentioned, with their disposition and notes
- active quests and story flags
- the combat order, if there's a fight
- a rolling story summary, plus the last 8 log entries

Once the log reaches 24 entries, everything except the newest 10 is folded into the summary by one summarize call. Everything that really matters (who's alive, who's hostile, what's in your pack, which quests are open) lives in structured state, not in the summary. So a long campaign loses flavor details at worst, never facts. That keeps prompts small, which matters a lot for speed on local hardware: 8K of context is plenty.

## Guardrails

Everything the model proposes goes through the engine first:

| Proposal | Limit |
|---|---|
| Damage from narration | at most 6 x average party level + 4 per target per narration, however many effects ask for it: enough to hurt, never enough to kill a healthy character outright |
| Conditions from narration | paralyzed, stunned and unconscious last at most 3 rounds, so a story beat can't lock a hero out of a fight |
| Gold | at most 100 x level per effect |
| XP | at most 100 x total party level per effect |
| Items | quantity 1 to 20, names trimmed |
| Effects per narration | 12 max, unknown ops dropped |
| Spawned enemies | total enemy XP capped by the encounter budget, max(100, 125 x total party level); one enemy is always allowed to start a fight, but reinforcements must fit the budget |
| Invented monsters | `custom:CR` gets stats from a challenge-rating table (AC, HP, attack bonus, damage, attack count) instead of numbers the model makes up |
| Attacking friendly NPCs | the NPC becomes a combatant with fitting stats (guard, bandit, cultist, commoner and so on) and initiative starts |

The rejected or clamped parts are reported to the table as system notes, so nothing changes silently.

## House rules and why

- **Defeat is a story beat, not a game over.** If the whole party drops, the survivors wake at 1 HP, and the narrator gets a note to decide the consequence (captured, robbed, rescued). Wiping out a party because a small model misjudged an encounter is no fun. Characters can still actually die from failed death saves or massive damage. The same applies outside a fight: if a trap or fall drops everyone, they come to at 1 HP 1d4 hours later.
- **Last stand in solo play.** Once per long rest, a lone hero drops to 1 HP instead of 0. Solo 5e is brutal at level 1, and nobody's there to stabilize you. It turns off as soon as a second player joins.
- **Sneak Attack** requires advantage or another conscious party member in the fight. There's no grid, so "an ally adjacent to the target" gets approximated.
- **Area spells** roll damage once, and each target saves separately, as in the book, but simpler to narrate.
- **Class skill lists are fixed** instead of chosen, and slightly generous, to keep character creation to a minute.
- **Ability score increases** are +2 to the class's key ability at levels 4 and 8, applied automatically.
- **Downed characters stabilize automatically after a victory**, which saves a round of tedious death saves once the fight is over.

## Networking

`server.py` uses only the standard library: `ThreadingHTTPServer` for requests, and Server-Sent Events for pushing updates to browsers, so no websocket library is needed. One worker thread runs every game action and every model call in order. That means:

- The model never gets two requests at once, which matters for an in-process llama.cpp model.
- Game state only ever changes from one thread.
- If players type at the same moment, their actions queue up rather than colliding.

Read-only commands (`/sheet`, `/map` and so on) skip the queue and answer instantly.

Each browser gets a player id, stored in localStorage. On connect, the server replays the last 250 public events plus that player's private messages, so a refresh or reconnect rebuilds the whole screen. Private messages (sheets, rejections, `/ask` answers) go only to the player who asked. After a server restart, players reclaim their characters by rejoining with the same name.

The web client is one HTML file with inline CSS and JS, using system fonts. It makes no requests to anything but the game server.

## Known limitations

- **Narration quality depends on the model.** A 14B model reads like a competent GM. A 7B model is serviceable but plainer and sometimes repetitive, and it will occasionally misread intent (the engine catches the mechanical side of that, but not the tone).
- **Theater of the mind.** There's no grid, positions, ranges or opportunity attacks. Movement and cover are narrative, though the GM can express them through advantage or disadvantage.
- **Content scope.** 4 classes, 5 ancestries, levels 1 to 10, and 18 monsters. Spells go up to 2nd level: higher-level slots exist and can upcast those spells, but there are no 3rd-level-plus spells yet. Adding them through `content/mods/` is the main way to extend the game.
- **Monster tactics are simple.** They focus on whoever they can see, use their listed attacks and on-hit effects, and may flee when badly hurt. There's no spellcasting monster AI.
- **One game per server,** with no accounts or passwords. It's designed for a trusted home network.
- **Speed on CPU.** Two model calls per turn is the floor for this design. Without a GPU, turns can take a minute or more.

## Extension ideas

- More classes and spells through mods (a paladin and a ranger are mostly data plus one or two feature hooks in `engine.py`).
- A simple zone map (near, far, cover) to make ranged combat and Sneak Attack more precise.
- Voice input through whisper.cpp and read-aloud narration through a local text-to-speech engine, both of which keep everything local.
- Scene illustrations from a local Stable Diffusion server, triggered on each `move` effect.
- Per-NPC memory summaries, so recurring characters remember specific conversations.
- A "GM style" setting (grim, heroic, comedic) injected into the narrate prompt.

## Tests

```
python -m unittest discover tests
```

The tests use a scripted fake model, so they check the engine and session logic without needing a real model: dice math, character creation, effect clamps, the encounter budget, save and load, round batching, a full wolf ambush, attacking NPCs, spell refunds, healing a downed ally, defeat recovery, and surviving garbage JSON.
