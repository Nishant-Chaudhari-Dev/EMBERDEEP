"""Play in the terminal: solo, or hot-seat with friends sharing one keyboard.
For players on separate devices, run server.py instead.

    python play.py                       # uses config.json
    python play.py --backend ollama --model qwen2.5:14b
    python play.py --backend mock        # no model, just to test"""
import os
import shutil
import sys
import textwrap

from config import build_llm, load_config
from engine.content import Content
from engine.dice import Dice
from gm.llm import LLMError
from session import SAVE_DIR, Session

if os.name == "nt":
    os.system("")  # enables ANSI colors in Windows terminals

C = {"dim": "\033[2m", "bold": "\033[1m", "green": "\033[32m", "red": "\033[31m", "cyan": "\033[36m",
     "yellow": "\033[33m", "magenta": "\033[35m", "reset": "\033[0m"}
WIDTH = min(96, shutil.get_terminal_size((96, 20)).columns - 2)


def paint(text, *styles):
    return "".join(C[s] for s in styles) + text + C["reset"]


def wrap(text, indent=""):
    return "\n".join(textwrap.fill(par, WIDTH, initial_indent=indent, subsequent_indent=indent) if par.strip() else ""
                     for par in text.split("\n"))


def render(ev):
    k, t = ev["kind"], ev.get("text", "")
    if k == "narration":
        print("\n" + wrap(t) + "\n")
    elif k == "roll":
        colour = "green" if ev.get("success") is True else "red" if ev.get("success") is False else "cyan"
        print(paint(wrap(t, "  "), colour))
    elif k == "system":
        print(paint(wrap(t, "  "), "yellow"))
    elif k == "thinking":
        print(paint("  " + t, "dim"))
    elif k in ("error", "gameover"):
        print(paint("\n" + wrap(t), "red", "bold"))


def ask(prompt, default=""):
    try:
        val = input(prompt).strip()
    except EOFError:
        sys.exit(0)
    return val or default


def choose(title, options):
    """options: list of (key, label). Returns key."""
    print(paint(title, "bold"))
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    while True:
        v = ask("> ", "1")
        if v.isdigit() and 1 <= int(v) <= len(options):
            return options[int(v) - 1][0]
        match = [k for k, label in options if label.lower().startswith(v.lower())]
        if match:
            return match[0]


def create_character(session, n):
    c = Content()
    print(paint(f"\nCharacter {n}", "magenta", "bold"))
    player = ask("Player name: ", f"Player {n}")
    name = ask("Character name: ", f"Hero {n}")
    anc = choose("Ancestry", [(k, f"{v['name']}: {', '.join(v['traits'])}") for k, v in c.ancestries.items()])
    cls = choose("Class", [(k, f"{v['name']} (d{v['hit_die']} hit die" + (", spellcaster)" if v.get("caster") else ")"))
                           for k, v in c.classes.items()])
    method = choose("Ability scores", [("array", "Standard array 15/14/13/12/10/8 (balanced)"),
                                       ("roll", "Roll 4d6, drop the lowest (swingy)")])
    scores = None
    if method == "roll":
        d = Dice()
        rolls = sorted((d.roll("4d6kh3").total for _ in range(6)), reverse=True)
        scores = dict(zip(c.classes[cls]["priority"], rolls))
        print("  Rolled " + ", ".join(map(str, rolls)) + " (highest goes to your class's key ability)")
    back = ask("One line of backstory (optional): ")
    pc = session.add_character(player, name, anc, cls, scores, back)
    print(paint(session.sheet(pc), "dim"))


def pick_save():
    saves = sorted(SAVE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if SAVE_DIR.exists() else []
    if not saves:
        print("No saves found.")
        return None
    return choose("Load which save?", [(str(p), p.stem) for p in saves[:10]])


def main():
    args = load_config("Emberdeep: terminal play")
    try:
        llm = build_llm(args)
    except LLMError as e:
        print(paint(str(e), "red"))
        sys.exit(1)

    print(paint("\n  E M B E R D E E P", "bold") + paint("   a tabletop adventure with an AI game master\n", "dim"))
    path = args.load
    if not path:
        opts = [("new", "New adventure")]
        if (SAVE_DIR / "autosave.json").exists():
            opts.append(("auto", "Continue (autosave)"))
        opts.append(("load", "Load a save"))
        pick = choose("Start", opts)
        path = str(SAVE_DIR / "autosave.json") if pick == "auto" else pick_save() if pick == "load" else None

    state = Session.load_state(path) if path else None
    session = Session(llm, state=state, emit=render, seed=args.seed, debug=args.debug)

    if not session.s.started:
        n = ask("How many players at this keyboard? [1] ", "1")
        n = max(1, min(6, int(n) if n.isdigit() else 1))
        for i in range(1, n + 1):
            create_character(session, i)
        premise = ask("\nDescribe the adventure you want, or press Enter for the default:\n> ")
        session.begin(premise)
    else:
        print(paint(f"Loaded. {session.s.summary}", "dim"))
        if session.s.log:
            print("\n" + wrap(session.s.log[-1]["text"]) + "\n")
        if session.s.combat and not session.engine.current_pc():
            session._rolls(session.engine.run_until_pc())

    print(paint("Type what you do in plain words. /help for commands, /quit to save and exit.", "dim"))
    while True:
        if all(p.dead for p in session.s.characters.values()):
            if ask("Load a save? (y/n) ", "n").lower().startswith("y"):
                p = pick_save()
                if p:
                    session = Session(llm, state=Session.load_state(p), emit=render, seed=args.seed, debug=args.debug)
                    continue
            break
        if session.s.combat:
            pc = session.engine.current_pc()
            if pc is None:
                session._rolls(session.engine.run_until_pc())
                continue
        else:
            todo = [p for p in session.expected() if p.id not in session.pending]
            if not todo:
                session.force_round()
                continue
            pc = todo[0]
        tag = paint(f"{pc.name} ({pc.hp}/{pc.max_hp} HP)", "bold") + (paint(" your turn", "magenta") if session.s.combat else "")
        text = ask(f"{tag} > ")
        if not text:
            continue
        if text.lower() in ("/quit", "/exit", "/q"):
            print(f"Saved to {session.save('autosave')}. Farewell.")
            break
        reply = session.submit(pc.id, text)
        if reply:
            print(paint(wrap(reply, "  "), "cyan"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Your last round is in saves/autosave.json.")
