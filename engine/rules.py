"""Core 5e-style rules (based on SRD 5.1, CC-BY-4.0). Pure functions and tables, no state."""

ABILITIES = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]

SKILLS = {
    "acrobatics": "DEX", "animal_handling": "WIS", "arcana": "INT", "athletics": "STR",
    "deception": "CHA", "history": "INT", "insight": "WIS", "intimidation": "CHA",
    "investigation": "INT", "medicine": "WIS", "nature": "INT", "perception": "WIS",
    "performance": "CHA", "persuasion": "CHA", "religion": "INT", "sleight_of_hand": "DEX",
    "stealth": "DEX", "survival": "WIS",
}

DC_LADDER = {"very easy": 5, "easy": 10, "medium": 15, "hard": 20, "very hard": 25, "nearly impossible": 30}

# XP needed to reach level N (index = level - 1). SRD 5.1 table.
XP_FOR_LEVEL = [0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000]
MAX_LEVEL = len(XP_FOR_LEVEL)

# Full caster spell slots by level (SRD). Keys are strings so they survive JSON round trips.
CASTER_SLOTS = {
    1: {"1": 2}, 2: {"1": 3}, 3: {"1": 4, "2": 2}, 4: {"1": 4, "2": 3},
    5: {"1": 4, "2": 3, "3": 2}, 6: {"1": 4, "2": 3, "3": 3}, 7: {"1": 4, "2": 3, "3": 3, "4": 1},
    8: {"1": 4, "2": 3, "3": 3, "4": 2}, 9: {"1": 4, "2": 3, "3": 3, "4": 3, "5": 1},
    10: {"1": 4, "2": 3, "3": 3, "4": 3, "5": 2},
}

STANDARD_ARRAY = [15, 14, 13, 12, 10, 8]

# How conditions change rolls. attack/check = the creature's own rolls; attacked = rolls against it.
CONDITIONS = {
    "poisoned":    {"attack": "disadvantage", "check": "disadvantage"},
    "frightened":  {"attack": "disadvantage", "check": "disadvantage"},
    "blinded":     {"attack": "disadvantage", "attacked": "advantage"},
    "restrained":  {"attack": "disadvantage", "attacked": "advantage"},
    "prone":       {"attack": "disadvantage", "attacked": "advantage"},
    "paralyzed":   {"attacked": "advantage", "skip": True, "fail_str_dex": True},
    "stunned":     {"attacked": "advantage", "skip": True, "fail_str_dex": True},
    "unconscious": {"attacked": "advantage", "skip": True, "fail_str_dex": True},
    "invisible":   {"attack": "advantage", "attacked": "disadvantage"},
    "hidden":      {"attack": "advantage"},
    "dodging":     {"attacked": "disadvantage"},
    "helped":      {"attack": "advantage", "check": "advantage"},
}
CONSUMED_ON_USE = {"hidden", "helped"}

# Stat templates for creatures the AI invents that aren't in the bestiary, so improvised
# monsters stay balanced. (ac, hp, attack bonus, damage, attacks per turn, xp) by challenge rating.
CR_TEMPLATES = {
    0:     (10, 4,   2, "1d4",     1, 10),
    0.125: (12, 9,   3, "1d6+1",   1, 25),
    0.25:  (13, 13,  4, "1d6+2",   1, 50),
    0.5:   (13, 22,  4, "1d8+2",   1, 100),
    1:     (13, 33,  4, "2d6+2",   1, 200),
    2:     (13, 50,  5, "2d8+3",   1, 450),
    3:     (13, 65,  5, "1d10+3",  2, 700),
    4:     (14, 85,  5, "2d6+3",   2, 1100),
    5:     (15, 110, 6, "2d8+4",   2, 1800),
}


def mod(score: int) -> int:
    return (score - 10) // 2


def prof_bonus(level: int) -> int:
    return 2 + (max(1, level) - 1) // 4


def level_for_xp(xp: int) -> int:
    lvl = 1
    for i, need in enumerate(XP_FOR_LEVEL):
        if xp >= need:
            lvl = i + 1
    return lvl


def combine(*modes) -> str:
    """5e rule: any advantage plus any disadvantage cancels to a normal roll."""
    adv = "advantage" in modes
    dis = "disadvantage" in modes
    if adv and not dis:
        return "advantage"
    if dis and not adv:
        return "disadvantage"
    return "normal"


def nearest_cr(cr: float) -> float:
    return min(CR_TEMPLATES, key=lambda k: abs(k - cr))


def health_word(hp: int, max_hp: int) -> str:
    """How a creature looks, for narration without leaking exact numbers."""
    if hp <= 0:
        return "down"
    r = hp / max(1, max_hp)
    if r >= 1:
        return "unhurt"
    if r > 0.5:
        return "hurt"
    if r > 0.2:
        return "bloodied"
    return "near death"
