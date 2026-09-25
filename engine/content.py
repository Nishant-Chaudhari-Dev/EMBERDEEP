"""Loads game content from JSON. Drop extra JSON files into content/mods/ to add or override
monsters, spells, classes and so on, e.g. {"monsters": {"basilisk": {...}}}."""
import json
from pathlib import Path

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
SECTIONS = ["classes", "ancestries", "weapons", "armor", "items", "monsters", "spells"]


class Content:
    def __init__(self, directory: Path = CONTENT_DIR):
        for name in SECTIONS:
            setattr(self, name, json.loads((directory / f"{name}.json").read_text(encoding="utf-8")))
        mods = directory / "mods"
        if mods.is_dir():
            for f in sorted(mods.glob("*.json")):
                data = json.loads(f.read_text(encoding="utf-8"))
                for section, entries in data.items():
                    if section in SECTIONS:
                        getattr(self, section).update(entries)
