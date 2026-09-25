"""Game state. Everything here is plain data and round-trips through JSON for save files.
The rules engine owns this state; the AI only ever proposes changes to it."""
from dataclasses import dataclass, field, asdict, fields


def _build(cls, data: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Character:
    id: str
    name: str
    player: str
    ancestry: str
    cls: str
    abilities: dict
    max_hp: int
    hp: int
    ac: int
    level: int = 1
    xp: int = 0
    skills: list = field(default_factory=list)
    saves: list = field(default_factory=list)
    weapons: list = field(default_factory=list)
    inventory: dict = field(default_factory=dict)
    gold: int = 0
    conditions: dict = field(default_factory=dict)   # name -> {"rounds", "expires", "save"}
    spells: list = field(default_factory=list)
    slots: dict = field(default_factory=dict)        # spell level (str) -> remaining
    features: dict = field(default_factory=dict)     # feature -> uses remaining
    max_features: dict = field(default_factory=dict)
    hit_dice: int = 1
    death_saves: dict = field(default_factory=lambda: {"success": 0, "fail": 0})
    dead: bool = False
    backstory: str = ""
    kind: str = "pc"

    @property
    def conscious(self) -> bool:
        return self.hp > 0 and not self.dead


@dataclass
class Monster:
    id: str
    name: str
    base: str
    ac: int
    hp: int
    max_hp: int
    attacks: list
    abilities: dict
    xp: int
    multiattack: int = 1
    traits: list = field(default_factory=list)
    description: str = ""
    conditions: dict = field(default_factory=dict)
    fled: bool = False
    npc_id: str = ""        # set when a named NPC turned hostile
    morale_checked: bool = False
    kind: str = "monster"

    @property
    def alive(self) -> bool:
        return self.hp > 0 and not self.fled

    @property
    def conscious(self) -> bool:
        return self.alive


@dataclass
class Location:
    id: str
    name: str
    description: str
    exits: list = field(default_factory=list)   # location ids
    visited: bool = True


@dataclass
class NPC:
    id: str
    name: str
    description: str
    disposition: str = "neutral"
    location: str = ""
    alive: bool = True
    notes: list = field(default_factory=list)


@dataclass
class Quest:
    id: str
    title: str
    status: str = "active"
    notes: str = ""


@dataclass
class Combat:
    order: list
    initiative: dict
    round: int = 1
    index: int = 0
    xp_pool: int = 0
    skip_once: list = field(default_factory=list)
    sneak_used: list = field(default_factory=list)

    def current(self) -> str:
        return self.order[self.index] if self.order else ""


@dataclass
class GameState:
    premise: str = ""
    solo: bool = False
    characters: dict = field(default_factory=dict)
    monsters: dict = field(default_factory=dict)
    locations: dict = field(default_factory=dict)
    npcs: dict = field(default_factory=dict)
    quests: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)
    current_location: str = ""
    day: int = 1
    hour: int = 9
    combat: Combat = None
    log: list = field(default_factory=list)       # {"role": "players"|"gm", "text": ...}
    summary: str = ""
    started: bool = False
    counter: int = 0

    def new_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GameState":
        s = _build(cls, d)
        s.characters = {k: _build(Character, v) for k, v in d.get("characters", {}).items()}
        s.monsters = {k: _build(Monster, v) for k, v in d.get("monsters", {}).items()}
        s.locations = {k: _build(Location, v) for k, v in d.get("locations", {}).items()}
        s.npcs = {k: _build(NPC, v) for k, v in d.get("npcs", {}).items()}
        s.quests = {k: _build(Quest, v) for k, v in d.get("quests", {}).items()}
        s.combat = _build(Combat, d["combat"]) if d.get("combat") else None
        return s
