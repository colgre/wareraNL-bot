"""Strategic resources: what a country's (and its alliance's) copies give.

Since the strategic-resources rework they no longer pay money. Every citizen
gets a bonus per resource, and everyone in an alliance gets the bonus of the
whole alliance's copies. 1, 4 and 9 copies of a resource are level 1, 2 and 3;
copies in between add nothing. Without an alliance — or suspended from it — a
country only counts its own regions.

Players do not need this to find their own bonus: ``user.getUserById`` already
reports it as ``skills.<name>.strategic``. This is for pages that show it per
country or alliance (e.g. /alliances).
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

LEVEL_THRESHOLDS = (1, 4, 9)  # copies needed for level 1, 2, 3

# resource key (as in country.strategicResources.resources) ->
# (display name, skill it boosts or None, bonus per level, unit, effect label)
RESOURCES: dict[str, tuple[str, Optional[str], int, str, str]] = {
    "rareEarths": ("Rare earths", "production", 3, "", "Production"),
    "gold": ("Gold", "management", 1, "", "Management"),
    "lithium": ("Lithium", "energy", 10, "", "Energy"),
    "coal": ("Coal", "precision", 5, "", "Precision"),
    "diamonds": ("Diamonds", "health", 10, "", "Health"),
    "uranium": ("Uranium", "lootChance", 2, "", "Loot chance"),
}


def level_for_copies(copies: int) -> int:
    """0-3: how many of LEVEL_THRESHOLDS this many copies reaches."""
    return sum(1 for needed in LEVEL_THRESHOLDS if copies >= needed)


def copies_from_country(strategic_resources: Optional[Mapping]) -> dict[str, int]:
    """{resource: number of regions} from a country's strategicResources."""
    resources = (strategic_resources or {}).get("resources") or {}
    return {name: len(regions) for name, regions in resources.items() if regions}


def summarize(copies: Mapping[str, int]) -> list[dict]:
    """One row per resource (fixed order) with its level and what that gives."""
    out = []
    for key, (label, skill, per_level, unit, effect) in RESOURCES.items():
        n = int(copies.get(key, 0))
        level = level_for_copies(n)
        out.append({
            "resource": key,
            "label": label,
            "copies": n,
            "level": level,
            "skill": skill,
            "bonus": per_level * level,
            "unit": unit,
            "effect": effect,
        })
    return out


def sum_copies(per_country: Iterable[Mapping[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for copies in per_country:
        for name, n in copies.items():
            total[name] = total.get(name, 0) + n
    return total
