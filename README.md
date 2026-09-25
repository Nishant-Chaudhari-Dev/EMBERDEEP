# Emberdeep

A tabletop fantasy RPG with an AI game master, built on the 5th edition rules from the SRD 5.1. Play solo or with friends. Everything runs on your own computer: the model, the rules, the saves. No API keys, no accounts, no internet needed once you've downloaded a model.

You type what your character does in plain words ("I kick the brazier into the cultists", "I tell the guard we're from the tax office"), the AI decides what kind of roll that needs, a real rules engine rolls it, and the AI narrates what happened. The engine owns every number, so the story can go anywhere but the game stays fair: the model can't hand out 10,000 gold or quietly skip your death saves.

Emberdeep is not an official Dungeons & Dragons product. It uses the openly licensed SRD 5.1 rules (attribution at the bottom) and its own setting.

## What you get

- **Three ways to play.** Terminal solo, terminal hot-seat (friends share one keyboard), or a browser game that friends on your Wi-Fi join from their phones or laptops.
- **Real 5e mechanics.** Ability checks with a DC ladder, advantage and disadvantage, attacks and crits, saving throws, spell slots and upcasting, conditions, death saves, short and long rests, XP and leveling to 10.
- **Four classes and five ancestries.** Fighter, Rogue, Wizard and Cleric; Human, Elf, Dwarf, Halfling and Half-Orc, each with their signature features (Second Wind, Action Surge, Sneak Attack, Lucky, Relentless Endurance).
- **Monsters.** 18 SRD monsters, from rats to an owlbear, plus anything the GM invents on the spot, which gets balanced stats from a challenge-rating table.
- **Persistent world.** The game remembers locations, NPCs and their attitudes, quests, and story flags, with autosaves every round.

## Quick start

You need Python 3.10 or newer and one way to run a model. If you just want to see it work first, skip the model entirely:

```
python play.py --backend mock
```

The mock GM is a dumb keyword matcher that exists only for testing. Real play needs a real model, set up below.

### 1. Install a backend

**Option A: llama.cpp inside the game (default).** No separate program, no server, the model loads straight into the game process.

```
# CPU only (works everywhere, slowest)
pip install llama-cpp-python

# NVIDIA GPU, building from source (needs the CUDA toolkit and a C++ compiler)
CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python

# Apple Silicon: the plain install builds with Metal GPU support automatically
pip install llama-cpp-python
```

Building from source needs a C/C++ compiler and CMake (Visual Studio Build Tools on Windows, Xcode command line tools on macOS). If the build gives you trouble, the llama-cpp-python project publishes prebuilt wheels. Check its README for the right `--extra-index-url` for your setup (CPU, CUDA version or Metal).

**Option B: Ollama.** Install it from ollama.com, then pull a model:

```
ollama pull qwen2.5:14b
```

Ollama runs a small server on your own machine (localhost:11434). The game talks to it on your computer only. Nothing goes to the internet. You need Ollama 0.5 or newer, because the game uses its JSON-schema structured output.

### 2. Pick a model

Any recent instruct model from roughly 7B to 14B parameters works, in GGUF format for llama.cpp. Use a Q4_K_M or Q5_K_M quantization. Models that follow JSON instructions well and have been used with this design include Qwen2.5 Instruct (7B or 14B), Mistral Nemo Instruct (12B) and Llama 3.1 Instruct (8B). Newer instruct models in the same size range should do as well or better. Download the GGUF from Hugging Face (search for the model name plus "GGUF"). This is a one-time download, and after that you're fully offline.

Rough sizing, including room for the 8K context:

| Your hardware | Model size to use |
|---|---|
| GPU with 6 to 8 GB VRAM | 7B to 8B |
| GPU with 12 GB VRAM | up to 12B |
| GPU with 16 GB+ VRAM | 14B |
| Mac with 16 GB unified memory | 7B to 8B (12B is tight) |
| Mac with 32 GB+ | 14B |
| CPU only, 16 GB RAM | 7B to 8B, and be patient |

Bigger models narrate noticeably better and misread fewer actions, so pick the largest one that fits comfortably. Reasoning ("thinking") models work, and their thinking gets stripped out, but they're slower and add little here. Plain instruct models are the better choice.

Put the file at `models/model.gguf`, or point to it with `--model path/to/file.gguf`.

**Speed, honestly:** each turn is two model calls. On a decent GPU that's a few seconds. On CPU only, expect anywhere from tens of seconds to a couple of minutes per turn with an 8B model, depending on your processor.

### 3. Play

**Solo or hot-seat in the terminal:**

```
python play.py                                        # llama.cpp with models/model.gguf
python play.py --model models/qwen2.5-14b-instruct-q4_k_m.gguf
python play.py --backend ollama --model qwen2.5:14b
```

**In the browser, with friends on the same network:**

```
python server.py
python server.py --backend ollama --model qwen2.5:14b
```

The server prints two addresses. Open the localhost one yourself, and give friends the other one (for example `http://192.168.1.23:8765`). Everyone picks a name, makes a character, and anyone can press start. People can drop in mid-game, and if someone's browser disconnects they reconnect to the same character by rejoining under the same name. If friends can't connect, allow Python through your firewall for private networks.

The server has no passwords. Anyone on your network who has the address can join. Use it on networks you trust, don't port-forward it to the internet, and use `--host 127.0.0.1` if you only want this computer to connect.

## How a session works

Outside combat, everyone declares what they're doing, and the round resolves once all conscious characters have declared. If someone is taking forever, anyone can type `/go` (or press "Resolve now" in the browser) to resolve without them. All the actions get adjudicated and narrated together, so the table moves as one scene.

In combat it's strict initiative order. On your turn, describe anything: attack, cast, dodge, shove someone off a ledge, try to talk the bandits down. Monsters act on their own between player turns. If a monster drops low on HP, it may break and flee.

Commands work anywhere:

| Command | What it does |
|---|---|
| `/sheet` or `/sheet Name` | full character sheet |
| `/party` | everyone at a glance |
| `/quests` | quest log |
| `/map` | where you are, routes, places you know |
| `/roll 2d6+1` | roll any dice, visible to the table |
| `/ask ...` | out-of-character question to the GM ("is the ledge climbable?") |
| `/pass` | do nothing this round or turn |
| `/go` | resolve the round now instead of waiting |
| `/save name` | save to `saves/name.json` |
| `/quit` | save and exit (terminal) |

**Saves.** The game autosaves to `saves/autosave.json` after every round. Starting a brand-new game moves the previous autosave to `autosave_previous.json` rather than overwriting it. To resume:

```
python play.py                        # menu offers "Continue"
python play.py --load saves/mygame.json
python server.py --continue           # resume the autosave in the browser
python server.py --load saves/mygame.json
```

## House rules

A few rules differ from the book, to make an AI-run game fun and hard to break:

- **Defeat isn't game over.** If the whole party drops, you wake up later at 1 HP, and the GM decides what that cost you: captured, robbed, dragged somewhere, or rescued by someone who'll want a favor.
- **Solo safety net.** A lone hero gets "last stand": once per long rest, a hit that would drop you leaves you at 1 HP instead. It switches off when a second player joins.
- **Encounter budget.** The GM can't flood you with monsters. Spawns are capped by an XP budget based on party level.
- **Capped consequences.** Narrated damage, gold, XP and item rewards are capped relative to party level.
- **Starting a fight.** Attacking a friendly or neutral NPC turns them into a combatant with matching stats, and starts initiative.
- **Sneak Attack.** Needs advantage or another conscious ally in the fight, since there's no grid to track "adjacent".
- **Area spells.** Damage is rolled once, and each target saves separately.
- **Ability score increases.** At levels 4 and 8 you get +2 to your class's key ability, applied automatically.

## Configuration

`config.json` sets the defaults, and every value can be overridden on the command line:

| Key | Flag | Meaning |
|---|---|---|
| `backend` | `--backend` | `llamacpp`, `ollama` or `mock` |
| `model` | `--model` | GGUF path (llamacpp) or model name (ollama) |
| `ctx` | `--ctx` | context window in tokens (8192 is plenty) |
| `gpu_layers` | `--gpu-layers` | `-1` puts all layers on the GPU, `0` is CPU only |
| `ollama_host` | `--ollama-host` | normally `http://127.0.0.1:11434` |
| `host` | `--host` | server bind address, `0.0.0.0` (LAN) or `127.0.0.1` (just you) |
| `port` | `--port` | server port, default 8765 |

Other flags: `--seed 42` fixes the dice for reproducible runs, and `--debug` shows raw model output and errors.

If the model runs out of memory, lower `--gpu-layers` (for example to 20) to split the model between GPU and CPU, or pick a smaller quantization.

## Modding

Any `.json` file you drop into `content/mods/` is merged over the base content at startup. It uses the same shape as the files in `content/`. A new id adds an entry, and an existing id replaces it. The GM automatically sees new monsters in its bestiary.

```json
{
  "monsters": {
    "ash_hound": {
      "name": "Ash Hound", "cr": 0.5, "xp": 100, "ac": 13, "hp": 19,
      "abilities": {"STR": 14, "DEX": 14, "CON": 13, "INT": 3, "WIS": 12, "CHA": 6},
      "attacks": [{"name": "smoldering bite", "bonus": 4, "damage": "1d8+2", "type": "fire"}],
      "traits": ["pack_tactics"]
    }
  },
  "items": {
    "ember salve": {"use": "heal", "amount": "1d4+1", "consumed": true}
  }
}
```

To learn the full formats for classes, spells (attack, save, auto-hit and heal types), weapons and armor, look at the matching files in `content/`.

## Project layout

```
play.py            terminal client (solo and hot-seat)
server.py          LAN server: standard-library HTTP + Server-Sent Events
web/index.html     browser client, one self-contained file, no external requests
session.py         a running game: turn flow, commands, saves; shared by both clients
config.py          settings and backend selection
engine/            the rules: dice, characters, checks, combat, effects, world state
gm/                the AI game master: backends, prompts and JSON schemas, orchestration
content/           classes, ancestries, gear, spells, monsters (+ mods/)
tests/             python -m unittest discover tests
saves/  models/
```

See `DESIGN.md` for how the pieces fit together and why.

## Troubleshooting

- **"No model file at ..."**: put your GGUF at `models/model.gguf`, or pass `--model`.
- **`ModuleNotFoundError: llama_cpp`**: install llama-cpp-python (see step 1), or use `--backend ollama`.
- **Can't reach Ollama**: make sure it's running (`ollama list` should work), and check `--ollama-host`.
- **"The GM stumbled"**: the model returned broken JSON twice in a row, so it couldn't narrate. Any dice already rolled still count. Describe your next move and carry on. If it happens often, use a bigger or newer model.
- **The GM misreads what you meant**: be a bit more concrete ("I attack the cultist on the left with my longsword"). If the engine rejects an action in combat (a spell you don't know, no slots left, no valid target), it tells you why and you keep your turn.
- **Friends can't connect**: same Wi-Fi, firewall permission for Python, and use the printed LAN address, not localhost.

## License and attribution

This work includes material taken from the System Reference Document 5.1 ("SRD 5.1") by Wizards of the Coast LLC and available at https://dnd.wizards.com/resources/systems-reference-document. The SRD 5.1 is licensed under the Creative Commons Attribution 4.0 International License available at https://creativecommons.org/licenses/by/4.0/legalcode.

Emberdeep's code, setting (Thornwick, the Emberdeep forest) and prompts are original. Dungeons & Dragons is a trademark of Wizards of the Coast. This project isn't affiliated with or endorsed by them.
