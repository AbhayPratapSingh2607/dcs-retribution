"""Models used to auto-resolve front line combat when fast forwarding.

The ground war is normally fought in DCS between the combat groups planned for each
front line at the start of the turn (see GroundPlanner). Fast forwarding skips the
start of the mission, so without this the front line units would sit idle for
however much time was skipped. Instead, each front line fights a combat round at a
fixed interval of simulated time, and AI CAS flights that reach their IP attack the
enemy's front line units.

Losses reduce the size of the planned combat groups, so the generated mission only
spawns the survivors, and are logged in the simulation results so they are
committed with the debriefing (reducing the control point's armor and counting
toward the front line battle outcome).

Like the air combat model, every tuning knob is a module level constant:

* Each unit with a ground attack value takes a shot each round at a random enemy
  unit. Units closer to the front (tanks, recon) are picked more often than units
  in the rear (artillery, logistics).
* Kill probability depends on the shooter's attack value and quality, the target's
  toughness, both sides' stances, and how intense the fighting is. Intensity is set
  by the more aggressive of the two stances: two defensive forces mostly watch each
  other.
* Unit quality is derived from the unit's price relative to other units of the same
  role and from the year it was introduced, so a modern MBT beats an old one.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, TYPE_CHECKING

from game.ground_forces.ai_ground_planner import CombatGroup, CombatGroupRole
from game.ground_forces.combat_stance import CombatStance
from .resolution import (
    BASE_STRIKE_PK,
    MAX_STRIKE_PK,
    STRIKE_ATTACKS_PER_AIRCRAFT,
    experience_factor,
    strike_rating,
)

if TYPE_CHECKING:
    from game.ato import Flight
    from game.theater import ControlPoint
    from ..simulationresults import SimulationResults

#: Simulated time between front line combat rounds.
GROUND_COMBAT_ROUND = timedelta(minutes=10)
#: Kill probability per round of a 1.0 attack, 1.0 quality unit against a 1.0
#: toughness target, with both sides aggressive. Two equal aggressive tank forces
#: lose roughly 15% per hour.
BASE_GROUND_PK = 0.04
MAX_GROUND_PK = 0.5

#: How much damage a unit of each role does to other ground units.
ROLE_ATTACK = {
    CombatGroupRole.TANK: 1.0,
    CombatGroupRole.IFV: 0.7,
    CombatGroupRole.ATGM: 0.9,
    CombatGroupRole.APC: 0.35,
    CombatGroupRole.RECON: 0.3,
    CombatGroupRole.ARTILLERY: 0.6,
    CombatGroupRole.INFANTRY: 0.3,
    CombatGroupRole.SHORAD: 0.15,
    CombatGroupRole.LOGI: 0.0,
}
#: How hard a unit of each role is to destroy.
ROLE_TOUGHNESS = {
    CombatGroupRole.TANK: 1.6,
    CombatGroupRole.IFV: 1.1,
    CombatGroupRole.ATGM: 0.8,
    CombatGroupRole.APC: 0.9,
    CombatGroupRole.RECON: 0.8,
    CombatGroupRole.ARTILLERY: 0.9,
    CombatGroupRole.INFANTRY: 0.6,
    CombatGroupRole.SHORAD: 0.9,
    CombatGroupRole.LOGI: 0.7,
}
#: How likely a unit of each role is to be targeted, reflecting how far from the
#: front line it is deployed (see DISTANCE_FROM_FRONTLINE).
ROLE_EXPOSURE = {
    CombatGroupRole.TANK: 1.0,
    CombatGroupRole.RECON: 1.0,
    CombatGroupRole.IFV: 0.9,
    CombatGroupRole.APC: 0.9,
    CombatGroupRole.INFANTRY: 0.9,
    CombatGroupRole.ATGM: 0.6,
    CombatGroupRole.SHORAD: 0.4,
    CombatGroupRole.ARTILLERY: 0.15,
    CombatGroupRole.LOGI: 0.1,
}
#: Price of a typical unit of each role. Units cheaper than this are worse, more
#: expensive units are better.
ROLE_REFERENCE_PRICE = {
    CombatGroupRole.TANK: 20,
    CombatGroupRole.IFV: 10,
    CombatGroupRole.ATGM: 13,
    CombatGroupRole.APC: 6,
    CombatGroupRole.RECON: 6,
    CombatGroupRole.ARTILLERY: 25,
    CombatGroupRole.INFANTRY: 1,
    CombatGroupRole.SHORAD: 20,
    CombatGroupRole.LOGI: 3,
}
MIN_QUALITY = 0.5
MAX_QUALITY = 1.6
#: Units introduced in this year have an era factor of 1.0. Each year newer or older
#: changes it by ERA_FACTOR_PER_YEAR, within the min and max.
REFERENCE_ERA = 1980
ERA_FACTOR_PER_YEAR = 0.01
MIN_ERA_FACTOR = 0.7
MAX_ERA_FACTOR = 1.3

#: How hard the fighting is when this is the more aggressive of the two stances.
STANCE_INTENSITY = {
    CombatStance.RETREAT: 0.2,
    CombatStance.DEFENSIVE: 0.4,
    CombatStance.AMBUSH: 0.5,
    CombatStance.AGGRESSIVE: 1.0,
    CombatStance.ELIMINATION: 1.2,
    CombatStance.BREAKTHROUGH: 1.3,
}
#: Damage multiplier for a side with this stance.
STANCE_OFFENSE = {
    CombatStance.RETREAT: 0.3,
    CombatStance.DEFENSIVE: 0.8,
    CombatStance.AMBUSH: 1.1,
    CombatStance.AGGRESSIVE: 1.0,
    CombatStance.ELIMINATION: 1.15,
    CombatStance.BREAKTHROUGH: 1.2,
}
#: Multiplier for damage taken by a side with this stance. Attacking forces leave
#: cover; defending forces are dug in.
STANCE_EXPOSURE = {
    CombatStance.RETREAT: 0.8,
    CombatStance.DEFENSIVE: 0.7,
    CombatStance.AMBUSH: 0.6,
    CombatStance.AGGRESSIVE: 1.0,
    CombatStance.ELIMINATION: 1.1,
    CombatStance.BREAKTHROUGH: 1.3,
}

#: Per-aircraft loss probability for each SHORAD or AAA unit defending the front.
FRONT_LINE_AIR_DEFENSE_PK = 0.03
MAX_FRONT_LINE_AIR_DEFENSE_PK = 0.4


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def era_factor(year_introduced: object) -> float:
    match = re.search(r"\d{4}", str(year_introduced))
    if match is None:
        return 1.0
    delta = int(match.group()) - REFERENCE_ERA
    return _clamp(1 + delta * ERA_FACTOR_PER_YEAR, MIN_ERA_FACTOR, MAX_ERA_FACTOR)


def unit_quality(group: CombatGroup) -> float:
    reference = ROLE_REFERENCE_PRICE.get(group.role, 1)
    price = max(group.unit_type.price, 0)
    if price == 0:
        return MIN_QUALITY
    era = era_factor(getattr(group.unit_type, "year_introduced", None))
    return _clamp(math.sqrt(price / reference) * era, MIN_QUALITY, MAX_QUALITY)


@dataclass
class GroundForce:
    """The combat groups one side has committed to a front line."""

    groups: list[CombatGroup]
    stance: CombatStance

    @property
    def unit_count(self) -> int:
        return sum(g.size for g in self.groups)


def combat_intensity(a: CombatStance, b: CombatStance) -> float:
    return max(STANCE_INTENSITY.get(a, 1.0), STANCE_INTENSITY.get(b, 1.0))


def _units(force: GroundForce) -> list[CombatGroup]:
    """One entry per unit, referencing the unit's group."""
    units = []
    for group in force.groups:
        units.extend([group] * max(group.size, 0))
    return units


def ground_pk(
    shooter: CombatGroup,
    target: CombatGroup,
    shooter_stance: CombatStance,
    target_stance: CombatStance,
    intensity: float,
) -> float:
    pk = (
        BASE_GROUND_PK
        * ROLE_ATTACK.get(shooter.role, 0.0)
        * unit_quality(shooter)
        * STANCE_OFFENSE.get(shooter_stance, 1.0)
        * intensity
        * STANCE_EXPOSURE.get(target_stance, 1.0)
        / ROLE_TOUGHNESS.get(target.role, 1.0)
    )
    return _clamp(pk, 0.0, MAX_GROUND_PK)


def _pick_target(targets: Sequence[CombatGroup], rng: random.Random) -> int:
    weights = [ROLE_EXPOSURE.get(t.role, 1.0) for t in targets]
    return rng.choices(range(len(targets)), weights=weights)[0]


def _fire(
    shooters: Sequence[CombatGroup],
    targets: Sequence[CombatGroup],
    shooter_stance: CombatStance,
    target_stance: CombatStance,
    intensity: float,
    rng: random.Random,
) -> set[int]:
    killed: set[int] = set()
    if not targets:
        return killed
    for shooter in shooters:
        if ROLE_ATTACK.get(shooter.role, 0.0) <= 0:
            continue
        idx = _pick_target(targets, rng)
        pk = ground_pk(shooter, targets[idx], shooter_stance, target_stance, intensity)
        if rng.random() < pk:
            killed.add(idx)
    return killed


def resolve_ground_round(
    a: GroundForce, b: GroundForce, rng: Optional[random.Random] = None
) -> tuple[Counter[int], Counter[int]]:
    """Fights one round of front line combat.

    Returns the losses of each side, as a count of units lost keyed by the id() of
    the CombatGroup that lost them. Groups are keyed by id because CombatGroup is not
    hashable by value and two groups can be equal.
    """
    if rng is None:
        rng = random.Random()
    a_units = _units(a)
    b_units = _units(b)
    if not a_units or not b_units:
        return Counter(), Counter()
    intensity = combat_intensity(a.stance, b.stance)
    b_killed = _fire(a_units, b_units, a.stance, b.stance, intensity, rng)
    a_killed = _fire(b_units, a_units, b.stance, a.stance, intensity, rng)
    return (
        Counter(id(a_units[i]) for i in a_killed),
        Counter(id(b_units[i]) for i in b_killed),
    )


def front_line_air_defense_pk(defenders: Iterable[CombatGroup]) -> float:
    count = sum(g.size for g in defenders if g.role is CombatGroupRole.SHORAD)
    pk = 1 - (1 - FRONT_LINE_AIR_DEFENSE_PK) ** count
    return _clamp(pk, 0.0, MAX_FRONT_LINE_AIR_DEFENSE_PK)


def resolve_cas_losses(
    flight: Flight,
    defenders: Iterable[CombatGroup],
    rng: Optional[random.Random] = None,
) -> int:
    """Returns the number of aircraft a CAS flight loses to front line air
    defenses."""
    if rng is None:
        rng = random.Random()
    pk = front_line_air_defense_pk(defenders)
    return sum(
        1 for m in flight.iter_members() if rng.random() < pk / experience_factor(m)
    )


def resolve_cas_attack(
    flight: Flight,
    targets: Sequence[CombatGroup],
    rng: Optional[random.Random] = None,
) -> Counter[int]:
    """Returns the losses inflicted by a flight attacking front line units, keyed by
    id() of the CombatGroup."""
    if rng is None:
        rng = random.Random()
    attacks = STRIKE_ATTACKS_PER_AIRCRAFT.get(flight.flight_type, 0)
    units = _units(GroundForce(list(targets), CombatStance.DEFENSIVE))
    losses: Counter[int] = Counter()
    if not attacks or not flight.alive:
        return losses
    rating = strike_rating(flight)
    for member in flight.iter_members():
        base_pk = BASE_STRIKE_PK * rating * experience_factor(member)
        for _ in range(attacks):
            if not units:
                return losses
            idx = _pick_target(units, rng)
            pk = base_pk / ROLE_TOUGHNESS.get(units[idx].role, 1.0)
            if rng.random() < _clamp(pk, 0.0, MAX_STRIKE_PK):
                losses[id(units.pop(idx))] += 1
    return losses


def apply_front_line_losses(
    groups: list[CombatGroup],
    losses: Counter[int],
    origin: ControlPoint,
    results: SimulationResults,
) -> int:
    """Removes lost units from the planned combat groups and logs the losses.

    Groups that lose all their units are removed from the list. Returns the number of
    units lost.
    """
    from game.unitmap import FrontLineUnit

    total = 0
    for group in list(groups):
        lost = min(losses.get(id(group), 0), group.size)
        if not lost:
            continue
        group.size -= lost
        total += lost
        for _ in range(lost):
            results.kill_front_line_unit(FrontLineUnit(group.unit_type, origin))
        if group.size <= 0:
            groups.remove(group)
    return total
