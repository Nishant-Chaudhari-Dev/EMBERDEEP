"""Dice. All randomness flows through one Dice object so a game can be seeded and replayed."""
import random
import re
from dataclasses import dataclass, field

_DIE = re.compile(r"(\d*)d(\d+)(?:k([hl])(\d+))?")
MAX_DICE = 100      # per term; /roll is open to every player, so '999999999d6' must not hang the game
MAX_SIDES = 1000


@dataclass
class Roll:
    expr: str
    total: int
    detail: str
    dice: list = field(default_factory=list)


class Dice:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)

    def die(self, sides: int) -> int:
        return self.rng.randint(1, sides)

    def roll(self, expr, crit: bool = False) -> Roll:
        """Roll expressions like '2d6+3', 'd20', '4d6kh3', '1d8+1d6-1'. crit doubles dice count."""
        s = str(expr).replace(" ", "").lower()
        if not s:
            return Roll(str(expr), 0, "0")
        total, parts, kept = 0, [], []
        for tok in re.findall(r"[+-]?[^+-]+", s):
            sign = -1 if tok.startswith("-") else 1
            body = tok.lstrip("+-")
            m = _DIE.fullmatch(body)
            if m:
                n = int(m.group(1) or 1) * (2 if crit else 1)
                sides = int(m.group(2))
                if not (1 <= n <= MAX_DICE and 1 <= sides <= MAX_SIDES):
                    raise ValueError(f"Bad dice expression: {expr!r} (up to {MAX_DICE} dice of up to {MAX_SIDES} sides)")
                rolls = [self.die(sides) for _ in range(n)]
                used = rolls
                if m.group(3):
                    used = sorted(rolls, reverse=m.group(3) == "h")[: int(m.group(4))]
                total += sign * sum(used)
                kept += used
                parts.append(f"{'-' if sign < 0 else '+'}{n}d{sides}{rolls}")
            elif body.isdigit():
                total += sign * int(body)
                parts.append(f"{'-' if sign < 0 else '+'}{body}")
            else:
                raise ValueError(f"Bad dice expression: {expr!r}")
        return Roll(str(expr), total, "".join(parts).lstrip("+"), kept)

    def d20(self, mode: str = "normal", lucky: bool = False):
        """Returns (result, [raw dice]). lucky = halfling trait, rerolls natural 1s once."""
        def one():
            r = self.die(20)
            return self.die(20) if (lucky and r == 1) else r
        a = one()
        if mode == "normal":
            return a, [a]
        b = one()
        return (max(a, b) if mode == "advantage" else min(a, b)), [a, b]
