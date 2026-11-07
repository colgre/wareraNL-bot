"""Optimal damage-per-8h calculator for WarEra players.

Assumptions that are always applied (worst-case war scenario):
  - Country order bonus: +15%  (maximum)
  - MU order bonus:      +15%  (maximum)
  - Alliance bonus:      +10%
  - MU headquarters:     +20%
  - Pill:                +60%  (only for player level ≥ 15)
  - Equipment:           mid-range values for the tier assigned to the player level
  - Military rank:       fetched from API per player (0% when unavailable)

Skill distribution is optimised numerically (greedy hill-climbing) to find
the allocation of skill points that maximises total damage in 8 hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

# ── Equipment tier mid-range bonuses ────────────────────────────────────────
# Columns: weapon_attack, weapon_crit_chance, helmet_crit_dmg,
#          chest_armor, gloves_precision, pants_armor, boots_dodge
# Source: skills_equipment.txt, mid-range of stated ranges used.
_EQ: dict[str, dict[str, float]] = {
    "uncommon": {
        "attack": 55.0,
        "crit_chance": 0.080,
        "crit_dmg": 0.155,  # 11-20% → 15.5%
        "armor": 0.160,  # chest 8% + pants 8%
        "precision": 0.080,
        "dodge": 0.080,
    },
    "rare": {
        "attack": 80.0,
        "crit_chance": 0.130,
        "crit_dmg": 0.255,
        "armor": 0.260,  # 13% + 13%
        "precision": 0.130,
        "dodge": 0.130,
    },
    "epic": {
        "attack": 110.0,
        "crit_chance": 0.180,
        "crit_dmg": 0.355,
        "armor": 0.360,  # 18% + 18%
        "precision": 0.180,
        "dodge": 0.180,
    },
    "legendary": {
        "attack": 145.0,
        "crit_chance": 0.255,
        "crit_dmg": 0.455,
        "armor": 0.510,  # 25.5% + 25.5%
        "precision": 0.255,
        "dodge": 0.255,
    },
    "mythic": {
        "attack": 240.0,
        "crit_chance": 0.355,
        "crit_dmg": 0.705,
        "armor": 0.710,  # 35.5% + 35.5%
        "precision": 0.355,
        "dodge": 0.355,
    },
}


def equipment_for_level(level: int) -> dict[str, float]:
    """Return mid-range equipment bonuses for the tier assigned to *level*."""
    if level < 15:
        return _EQ["uncommon"]
    if level < 20:
        return _EQ["rare"]
    if level < 25:
        return _EQ["epic"]
    if level < 30:
        return _EQ["legendary"]
    return _EQ["mythic"]


def equipment_tier_name(level: int) -> str:
    if level < 15:
        return "Uncommon"
    if level < 20:
        return "Rare"
    if level < 25:
        return "Epic"
    if level < 30:
        return "Legendary"
    return "Mythic"


# ── Skill allocation dataclass ───────────────────────────────────────────────

_SKILL_NAMES = (
    "attack",
    "precision",
    "crit_chance",
    "crit_dmg",
    "armor",
    "dodge",
    "health",
    "hunger",
)

# Hard cap: a skill can be upgraded at most this many times
MAX_SKILL_LEVEL = 10


@dataclass
class SkillAllocation:
    attack: int = 0
    precision: int = 0
    crit_chance: int = 0
    crit_dmg: int = 0
    armor: int = 0
    dodge: int = 0
    health: int = 0
    hunger: int = 0

    def total_sp_spent(self) -> int:
        return sum(v * (v + 1) // 2 for v in self._values())

    def _values(self):
        return (
            self.attack,
            self.precision,
            self.crit_chance,
            self.crit_dmg,
            self.armor,
            self.dodge,
            self.health,
            self.hunger,
        )

    def copy(self) -> "SkillAllocation":
        return SkillAllocation(
            self.attack,
            self.precision,
            self.crit_chance,
            self.crit_dmg,
            self.armor,
            self.dodge,
            self.health,
            self.hunger,
        )


# ── Global bonus constants ───────────────────────────────────────────────────

COUNTRY_ORDER_BONUS = 0.15
MU_ORDER_BONUS = 0.15
ALLIANCE_BONUS = 0.10
MU_HQ_BONUS = 0.20
PILL_BONUS = 0.60  # level ≥ 15 only

LIGHT_AMMO_BONUS = 0.10  # level < 20
AMMO_BONUS = 0.20  # level 20–30
HEAVY_AMMO_BONUS = 0.40  # level > 30

# Product of the bonuses that are always active (excluding pill, ammo and rank)
_BASE_GLOBAL = (
    (1 + COUNTRY_ORDER_BONUS)
    * (1 + MU_ORDER_BONUS)
    * (1 + ALLIANCE_BONUS)
    * (1 + MU_HQ_BONUS)
)
# With pill (level ≥ 15)
_BASE_GLOBAL_PILL = _BASE_GLOBAL * (1 + PILL_BONUS)

FOOD_HP_PER_HUNGER = 30  # cooked fish: best available food


# ── Military rank damage bonus table ────────────────────────────────────────
# Maps rank level (integer, as returned by the API's militaryRank field) to
# the damage bonus fraction (e.g. 0.0750 = 7.5%).
# Source: skills_equipment.txt.  Level 4 is absent from the source and is
# interpolated as 1.00%.

RANK_BONUS_TABLE: dict[int, float] = {
    0: 0.0000,
    1: 0.0025,
    2: 0.0050,
    3: 0.0075,
    4: 0.0100,
    5: 0.0125,
    6: 0.0150,
    7: 0.0175,
    8: 0.0200,
    9: 0.0250,
    10: 0.0275,
    11: 0.0300,
    12: 0.0325,
    13: 0.0375,
    14: 0.0400,
    15: 0.0425,
    16: 0.0450,
    17: 0.0500,
    18: 0.0525,
    19: 0.0550,
    20: 0.0575,
    21: 0.0625,
    22: 0.0650,
    23: 0.0675,
    24: 0.0700,
    25: 0.0750,
    26: 0.0775,
    27: 0.0800,
    28: 0.0825,
    29: 0.0875,
    30: 0.0900,
    31: 0.0925,
    32: 0.0950,
    33: 0.1000,
    34: 0.1025,
    35: 0.1050,
    36: 0.1075,
    37: 0.1125,
    38: 0.1150,
    39: 0.1175,
    40: 0.1200,
    41: 0.1250,
    42: 0.1275,
    43: 0.1300,
    44: 0.1325,
    45: 0.1375,
    46: 0.1400,
    47: 0.1425,
    48: 0.1450,
    49: 0.1500,
    50: 0.1525,
    51: 0.1550,
    52: 0.1575,
    53: 0.1625,
    54: 0.1650,
    55: 0.1675,
    56: 0.1700,
    57: 0.1750,
    58: 0.1775,
    59: 0.1800,
    60: 0.1825,
    61: 0.1875,
    62: 0.1900,
    63: 0.1925,
    64: 0.1950,
    65: 0.2000,
    66: 0.2025,
    67: 0.2050,
    68: 0.2075,
    69: 0.2125,
    70: 0.2150,
    71: 0.2175,
    72: 0.2200,
    73: 0.2250,
    74: 0.2275,
    75: 0.2300,
    76: 0.2325,
    77: 0.2375,
    78: 0.2400,
    79: 0.2425,
    80: 0.2450,
    81: 0.2500,
    82: 0.2525,
    83: 0.2550,
    84: 0.2575,
    85: 0.2625,
    86: 0.2650,
    87: 0.2675,
    88: 0.2700,
    89: 0.2750,
    90: 0.2775,
    91: 0.2800,
    92: 0.2825,
    93: 0.2875,
    94: 0.2900,
    95: 0.2925,
    96: 0.2950,
    97: 0.3000,
    98: 0.3025,
    99: 0.3050,
    100: 0.3075,
    101: 0.3125,
    102: 0.3150,
    103: 0.3175,
    104: 0.3200,
    105: 0.3250,
    106: 0.3275,
    107: 0.3300,
    108: 0.3325,
    109: 0.3350,
    110: 0.3375,
    111: 0.3425,
    112: 0.3450,
    113: 0.3475,
    114: 0.3500,
    115: 0.3525,
    116: 0.3550,
    117: 0.3600,
    118: 0.3650,
    119: 0.3700,
    120: 0.3750,
}

# The game doubled the damage bonus of every military rank (captain +20% ->
# +40%, supreme commander +37.5% -> +75%); the table above is the old scale.
RANK_BONUS_MULTIPLIER = 2
RANK_BONUS_TABLE = {rank: bonus * RANK_BONUS_MULTIPLIER for rank, bonus in RANK_BONUS_TABLE.items()}

_MAX_RANK_LEVEL = max(RANK_BONUS_TABLE)


def rank_bonus_from_level(rank_level: int) -> float:
    """Convert a militaryRank integer to a damage bonus fraction.

    Clamps to the known table range; returns 0.0 for unknown/negative.
    """
    if rank_level < 0:
        return 0.0
    if rank_level > _MAX_RANK_LEVEL:
        return RANK_BONUS_TABLE[_MAX_RANK_LEVEL]
    return RANK_BONUS_TABLE.get(rank_level, 0.0)


def ammo_for_level(player_level: int) -> tuple[float, str]:
    """Return (bonus_fraction, ammo_name) for the ammo tier used at *player_level*.

    - level < 20  → lightAmmo (+10%)
    - level 20–30 → Ammo (+20%)
    - level > 30  → heavyAmmo (+40%)
    """
    if player_level < 20:
        return LIGHT_AMMO_BONUS, "lightAmmo"
    elif player_level <= 30:
        return AMMO_BONUS, "Ammo"
    else:
        return HEAVY_AMMO_BONUS, "heavyAmmo"


# ── Core damage computation ──────────────────────────────────────────────────


def compute_damage_per_8h(
    skills: SkillAllocation,
    player_level: int,
    rank_bonus: float = 0.0,
) -> float:
    """Return estimated total damage output over 8 hours.

    Args:
        skills:       Skill allocation to evaluate.
        player_level: Character level (used for equipment tier and pill eligibility).
        rank_bonus:   Military rank damage bonus as a fraction (e.g. 0.15 for 15%).
    """
    eq = equipment_for_level(player_level)

    # ── Derived combat stats (skill levels capped at MAX_SKILL_LEVEL) ────────
    atk_lvl = min(skills.attack, MAX_SKILL_LEVEL)
    pre_lvl = min(skills.precision, MAX_SKILL_LEVEL)
    cc_lvl = min(skills.crit_chance, MAX_SKILL_LEVEL)
    cd_lvl = min(skills.crit_dmg, MAX_SKILL_LEVEL)
    arm_lvl = min(skills.armor, MAX_SKILL_LEVEL)
    dod_lvl = min(skills.dodge, MAX_SKILL_LEVEL)
    hp_lvl = min(skills.health, MAX_SKILL_LEVEL)
    hng_lvl = min(skills.hunger, MAX_SKILL_LEVEL)

    attack = (100.0 + 20.0 * atk_lvl) + eq["attack"]
    precision = min(1.0, 0.50 + 0.05 * pre_lvl + eq["precision"])
    crit_chance = min(1.0, 0.10 + 0.05 * cc_lvl + eq["crit_chance"])
    # Base crit dmg bonus = 1.0 (=100% extra), each level +0.20; add equipment bonus.
    # Crit multiplier = 1 + crit_dmg_bonus
    crit_dmg_bonus = (1.0 + 0.20 * cd_lvl) + eq["crit_dmg"]
    armor = min(0.80, 0.04 * arm_lvl + eq["armor"])
    dodge = min(0.80, 0.04 * dod_lvl + eq["dodge"])
    max_hp = 50.0 + 10.0 * hp_lvl
    max_hunger = 4.0 + hng_lvl

    # ── Hits per 8 hours ─────────────────────────────────────────────────────
    # Expected HP consumed per hit action (armor reduces, dodge cancels entirely)
    hp_per_hit = max(0.001, 10.0 * (1.0 - armor) * (1.0 - dodge))
    # Available HP: start full + 8h regen (10%/h) + eat all food (cooked fish)
    # Hunger regens at 10%/h; floor to whole units since food gives integer HP.
    hunger_start = int(max_hunger)
    hunger_regen = int(max_hunger * 0.8)
    total_hp = max_hp * 1.8 + (hunger_start + hunger_regen) * FOOD_HP_PER_HUNGER
    hits = total_hp / hp_per_hit

    # ── Expected damage per hit ───────────────────────────────────────────────
    # miss (prob = 1-precision): ½ damage, cannot crit
    # hit, no crit (prob = precision × (1-crit_chance)): 1× damage
    # hit + crit  (prob = precision × crit_chance): (1 + crit_dmg_bonus)× damage
    miss_rate = 1.0 - precision
    e_per_hit = attack * (
        miss_rate * 0.5
        + precision * (1.0 - crit_chance)
        + precision * crit_chance * (1.0 + crit_dmg_bonus)
    )

    # ── Global multipliers ───────────────────────────────────────────────────
    ammo_bonus, _ = ammo_for_level(player_level)
    base = _BASE_GLOBAL_PILL if player_level >= 15 else _BASE_GLOBAL
    total_mult = base * (1.0 + rank_bonus) * (1.0 + ammo_bonus)

    return hits * e_per_hit * total_mult


# ── Balanced skill allocator ─────────────────────────────────────────────────


def optimal_skills(player_level: int) -> SkillAllocation:
    """Return a balanced skill allocation following the prescribed distribution:

    - All 6 combat skills (attack, precision, crit_chance, crit_dmg, armor,
      dodge) receive the same base level.
    - Armor and dodge receive 0, 1, or 2 extra levels when the budget allows.
    - Remaining SP are split between health and hunger (lower priority).

    Among all combinations satisfying the above shape, the one that produces
    the highest computed damage is returned.  Rank bonus is excluded — it is
    a flat multiplier that does not affect the relative ranking of allocations.
    """
    budget = 4 * player_level
    _cost = [level * (level + 1) // 2 for level in range(MAX_SKILL_LEVEL + 1)]

    best_dmg = 0.0
    best_alloc = SkillAllocation()

    for base in range(MAX_SKILL_LEVEL + 1):
        for arm_bonus in range(3):  # 0, 1, or 2 extra levels for armor + dodge
            arm_lvl = min(MAX_SKILL_LEVEL, base + arm_bonus)
            dod_lvl = min(MAX_SKILL_LEVEL, base + arm_bonus)

            combat_cost = (
                _cost[base] * 4  # attack, precision, crit_chance, crit_dmg
                + _cost[arm_lvl]
                + _cost[dod_lvl]
            )
            if combat_cost > budget:
                continue

            remaining = budget - combat_cost

            # Distribute remaining SP equally to health and hunger; give any
            # leftover level-up to health first, then hunger.
            hp_lvl = hu_lvl = 0
            for lvl in range(MAX_SKILL_LEVEL, -1, -1):
                if 2 * _cost[lvl] <= remaining:
                    hp_lvl = hu_lvl = lvl
                    break
            leftover = remaining - _cost[hp_lvl] - _cost[hu_lvl]
            if (
                hp_lvl < MAX_SKILL_LEVEL
                and (_cost[hp_lvl + 1] - _cost[hp_lvl]) <= leftover
            ):
                hp_lvl += 1
                leftover -= _cost[hp_lvl] - _cost[hp_lvl - 1]
            if (
                hu_lvl < MAX_SKILL_LEVEL
                and (_cost[hu_lvl + 1] - _cost[hu_lvl]) <= leftover
            ):
                hu_lvl += 1

            alloc = SkillAllocation(
                attack=base,
                precision=base,
                crit_chance=base,
                crit_dmg=base,
                armor=arm_lvl,
                dodge=dod_lvl,
                health=hp_lvl,
                hunger=hu_lvl,
            )
            dmg = compute_damage_per_8h(alloc, player_level)
            if dmg > best_dmg:
                best_dmg = dmg
                best_alloc = alloc

    return best_alloc


@lru_cache(maxsize=128)
def damage_for_level(player_level: int) -> float:
    """Cached: optimal damage per 8h for a player at *player_level* with 0% rank bonus."""
    if player_level <= 0:
        return 0.0
    return compute_damage_per_8h(optimal_skills(player_level), player_level, 0.0)


# ── Military rank extraction ─────────────────────────────────────────────────


def extract_rank_bonus(obj: Any) -> tuple[float, Optional[int]]:
    """Extract the military rank damage bonus from a getUserLite response dict.

    Returns (bonus_fraction, rank_level_or_None).
    bonus_fraction is e.g. 0.075 for 7.5%.  Returns (0.0, None) if not found.

    Preferred path: ``militaryRank`` integer → RANK_BONUS_TABLE lookup.
    Falls back to explicit bonus fields for backwards compatibility.
    """
    if not isinstance(obj, dict):
        return 0.0, None

    # ── Primary: militaryRank integer → table lookup ─────────────────────
    for path in (
        ("militaryRank",),
        ("rankings", "militaryRank", "value"),
        ("rankings", "militaryRank"),
        ("ranking", "militaryRank"),
        ("militaryRankLevel",),
        ("rankLevel",),
    ):
        node: Any = obj
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, int) and node >= 0:
            return rank_bonus_from_level(node), node

    # ── Fallback: explicit bonus percentage fields ────────────────────────
    for key in (
        "militaryRankBonus",
        "rankBonus",
        "militaryBonus",
        "rank_bonus",
        "rankDamageBonus",
        "militaryDamageBonus",
    ):
        v = obj.get(key)
        if isinstance(v, (int, float)):
            val = float(v)
            return (val / 100.0 if val > 1.5 else val), None

    for rk in ("rankings", "ranking"):
        rankings = obj.get(rk)
        if not isinstance(rankings, dict):
            continue
        for mk in ("military", "rank", "militaryRanking"):
            rank_obj = rankings.get(mk)
            if isinstance(rank_obj, dict):
                for bk in ("bonus", "damageBonus", "percentage", "pct"):
                    v = rank_obj.get(bk)
                    if isinstance(v, (int, float)):
                        val = float(v)
                        return (val / 100.0 if val > 1.5 else val), None

    return 0.0, None


# ── Helpers for formatting ───────────────────────────────────────────────────


def fmt_damage(dmg: float) -> str:
    """Format a large damage number for display (e.g. 1_234_567 → '1.2M')."""
    if dmg >= 1_000_000_000:
        return f"{dmg / 1_000_000_000:.2f}B"
    if dmg >= 1_000_000:
        return f"{dmg / 1_000_000:.2f}M"
    if dmg >= 1_000:
        return f"{dmg / 1_000:.1f}K"
    return f"{dmg:.0f}"


# ── Full player breakdown ────────────────────────────────────────────────────


def player_breakdown(player_level: int, rank_bonus: float = 0.0) -> dict:
    """Return a complete computation breakdown dict for a single player.

    Contains every intermediate value used to arrive at the final damage
    number so it can be rendered in a detailed Discord embed.
    """
    if player_level <= 0:
        player_level = 1

    skills = optimal_skills(player_level)
    eq = equipment_for_level(player_level)
    tier = equipment_tier_name(player_level)
    sp_budget = 4 * player_level
    sp_used = skills.total_sp_spent()

    # ── Derived stats (same logic as compute_damage_per_8h) ─────────────
    atk_lvl = min(skills.attack, MAX_SKILL_LEVEL)
    pre_lvl = min(skills.precision, MAX_SKILL_LEVEL)
    cc_lvl = min(skills.crit_chance, MAX_SKILL_LEVEL)
    cd_lvl = min(skills.crit_dmg, MAX_SKILL_LEVEL)
    arm_lvl = min(skills.armor, MAX_SKILL_LEVEL)
    dod_lvl = min(skills.dodge, MAX_SKILL_LEVEL)
    hp_lvl = min(skills.health, MAX_SKILL_LEVEL)
    hng_lvl = min(skills.hunger, MAX_SKILL_LEVEL)

    attack = (100.0 + 20.0 * atk_lvl) + eq["attack"]
    skill_base_atk = 100.0 + 20.0 * atk_lvl
    precision = min(1.0, 0.50 + 0.05 * pre_lvl + eq["precision"])
    crit_chance = min(1.0, 0.10 + 0.05 * cc_lvl + eq["crit_chance"])
    crit_dmg_bonus = (1.0 + 0.20 * cd_lvl) + eq["crit_dmg"]
    armor = min(0.80, 0.04 * arm_lvl + eq["armor"])
    dodge = min(0.80, 0.04 * dod_lvl + eq["dodge"])
    max_hp = 50.0 + 10.0 * hp_lvl
    max_hunger = 4.0 + hng_lvl

    # ── HP & hits ────────────────────────────────────────────────────────
    hp_regen = max_hp * 0.8  # 10%/h × 8h
    hunger_start = int(max_hunger)
    hunger_regen = int(max_hunger * 0.8)  # floor to whole fish
    food_hp_start = hunger_start * FOOD_HP_PER_HUNGER
    food_hp_regen = hunger_regen * FOOD_HP_PER_HUNGER
    food_hp = food_hp_start + food_hp_regen
    total_hp = max_hp + hp_regen + food_hp
    hp_per_hit = max(0.001, 10.0 * (1.0 - armor) * (1.0 - dodge))
    hits = total_hp / hp_per_hit

    # ── Per-hit damage ────────────────────────────────────────────────────
    miss_rate = 1.0 - precision
    hit_no_crit_rate = precision * (1.0 - crit_chance)
    hit_crit_rate = precision * crit_chance
    e_per_hit = attack * (
        miss_rate * 0.5 + hit_no_crit_rate + hit_crit_rate * (1.0 + crit_dmg_bonus)
    )

    # ── Multipliers ───────────────────────────────────────────────────────
    pill_active = player_level >= 15
    base_global = _BASE_GLOBAL_PILL if pill_active else _BASE_GLOBAL
    ammo_bonus, ammo_name = ammo_for_level(player_level)
    total_mult = base_global * (1.0 + rank_bonus) * (1.0 + ammo_bonus)
    total_dmg = hits * e_per_hit * total_mult

    return {
        # ── Input ──────────────────────────────────────────────────────
        "player_level": player_level,
        "rank_bonus": rank_bonus,
        "sp_budget": sp_budget,
        "sp_used": sp_used,
        # ── Skills ─────────────────────────────────────────────────────
        "skills": skills,
        # ── Equipment ──────────────────────────────────────────────────
        "equipment_tier": tier,
        "eq_attack": eq["attack"],
        "eq_crit_chance": eq["crit_chance"],
        "eq_crit_dmg": eq["crit_dmg"],
        "eq_armor": eq["armor"],
        "eq_precision": eq["precision"],
        "eq_dodge": eq["dodge"],
        # ── Derived combat stats ────────────────────────────────────────
        "attack": attack,
        "skill_base_atk": skill_base_atk,
        "precision": precision,
        "crit_chance": crit_chance,
        "crit_dmg_bonus": crit_dmg_bonus,  # the EXTRA multiplier (not 1+…)
        "armor": armor,
        "dodge": dodge,
        "max_hp": max_hp,
        "max_hunger": max_hunger,
        # ── HP & hit count ───────────────────────────────────────────────
        "hp_regen": hp_regen,
        "hunger_start": hunger_start,
        "hunger_regen": hunger_regen,
        "food_hp_start": food_hp_start,
        "food_hp_regen": food_hp_regen,
        "food_hp": food_hp,
        "total_hp": total_hp,
        "hp_per_hit": hp_per_hit,
        "hp_per_landed": 10.0 * (1.0 - armor),  # HP cost ignoring dodge
        "hits": hits,
        "n_dodges": hits * dodge,
        "n_landed": hits * (1.0 - dodge),
        # ── Per-hit probability / damage ────────────────────────────────
        "miss_rate": miss_rate,
        "hit_no_crit_rate": hit_no_crit_rate,
        "hit_crit_rate": hit_crit_rate,
        "n_misses": hits * miss_rate,
        "n_hits": hits * hit_no_crit_rate,
        "n_crits": hits * hit_crit_rate,
        "dmg_miss": attack * 0.5 * total_mult,
        "dmg_hit": attack * 1.0 * total_mult,
        "dmg_crit": attack * (1.0 + crit_dmg_bonus) * total_mult,
        "e_per_hit": e_per_hit,
        # ── Multipliers ──────────────────────────────────────────────────
        "pill_active": pill_active,
        "ammo_bonus": ammo_bonus,
        "ammo_name": ammo_name,
        "base_global_mult": base_global,
        "total_mult": total_mult,
        # ── Result ───────────────────────────────────────────────────────
        "total_dmg": total_dmg,
    }
