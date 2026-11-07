"""Models used to auto-resolve combat when fast forwarding.

These functions decide *what* happens in a combat (how many aircraft each flight
loses, which ground units a strike destroys) without applying the result. The
FrozenCombat subclasses apply the outcome to the game.

The models are intentionally simple and every tuning knob is a module level
constant so they are easy to adjust:

* Air-to-air combat is fought in a small number of rounds. In each round every
  aircraft able to fight takes a shot at a random enemy aircraft. The chance of a
  kill depends on the shooter's air-to-air capability, the target's ability to
  defend itself, and the experience of both pilots. Kills within a round are
  simultaneous.
* SAM engagements roll once per aircraft. Each threatening site adds to the
  chance of the aircraft being lost. Radar SAMs are more dangerous than IR SAMs
  and AAA, and long range radar SAMs are the most dangerous. SEAD support in the
  package suppresses radar SAMs.
* Strikes are resolved when the flight reaches its IP. Each aircraft gets a number
  of attacks against the units of the package's target, with a chance of
  destroying a unit on each attack. SEAD and DEAD flights attack emitters and
  launchers first.

Aircraft capability is derived from the task priorities in the aircraft YAML
files, which already rank how well each airframe performs each mission type.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

from game.ato.flighttype import FlightType
from game.data.units import UnitClass
from game.utils import nautical_miles

if TYPE_CHECKING:
    from game.ato import Flight
    from game.ato.flightmember import FlightMember
    from game.dcs.aircrafttype import AircraftType
    from game.theater import TheaterGroundObject
    from game.theater.theatergroup import TheaterUnit

# --- Shared -----------------------------------------------------------------

#: Task priority that maps to a capability rating of 1.0. Dedicated modern
#: fighters have an air-to-air priority of roughly 500-750.
REFERENCE_TASK_PRIORITY = 550
#: Upper bound for capability ratings so a single airframe can't be invincible.
MAX_CAPABILITY_RATING = 1.4

#: Each mission a pilot has flown improves their performance by this fraction...
EXPERIENCE_BONUS_PER_MISSION = 0.02
#: ...up to this many missions.
MAX_EXPERIENCE_MISSIONS = 10

# --- Air-to-air -------------------------------------------------------------

AIR_TO_AIR_TASKS = (
    FlightType.BARCAP,
    FlightType.TARCAP,
    FlightType.INTERCEPTION,
    FlightType.SWEEP,
    FlightType.ESCORT,
)

#: Flights that are not armed for combat at all. They can be shot down but can't
#: shoot back.
NON_COMBAT_TASKS = frozenset(
    {
        FlightType.AEWC,
        FlightType.REFUELING,
        FlightType.TRANSPORT,
        FlightType.FERRY,
        FlightType.AIR_ASSAULT,
        FlightType.PRETENSE_CARGO,
        FlightType.RECOVERY,
    }
)

#: Rating for airframes that aren't tasked for air-to-air at all (guns, maybe an
#: IR missile for self-defense).
MIN_AIR_TO_AIR_RATING = 0.15
#: Multiplier for air-to-air capable aircraft flying an air-to-ground mission.
#: They're loaded with ground attack ordnance and focused on their target.
AIR_TO_GROUND_ROLE_FACTOR = 0.4
#: Number of shooting rounds in a single engagement. Survivors of both sides
#: disengage afterward.
AIR_COMBAT_ROUNDS = 2
#: Kill probability of a rating 1.0 shooter against a defenseless target.
BASE_AIR_TO_AIR_PK = 0.4
#: How much a target's own air-to-air rating reduces the chance of it being hit.
#: A rating 1.0 target halves the shooter's kill probability.
AIR_TO_AIR_EVASION = 0.5
MAX_AIR_TO_AIR_PK = 0.9

# --- SAMs -------------------------------------------------------------------

#: Per-aircraft loss probability for a short range radar SAM site.
RADAR_SAM_BASE_PK = 0.08
#: Additional loss probability for a radar SAM at or beyond LONG_RANGE_SAM.
RADAR_SAM_RANGE_PK = 0.08
LONG_RANGE_SAM = nautical_miles(40)
#: Per-aircraft loss probability for IR SAMs and AAA.
SHORT_RANGE_AIR_DEFENSE_PK = 0.05
#: Radar SAM effectiveness multiplier when the package has SEAD support.
SEAD_SUPPORT_FACTOR = 0.5
#: Additional radar SAM effectiveness multiplier for flights that are themselves
#: tasked with suppressing air defenses (anti-radiation missiles, ECM, tactics).
SEAD_FLIGHT_FACTOR = 0.7
MAX_SAM_PK = 0.6

SEAD_TASKS = frozenset(
    {
        FlightType.SEAD,
        FlightType.SEAD_ESCORT,
        FlightType.SEAD_SWEEP,
        FlightType.DEAD,
    }
)

# --- Strikes ----------------------------------------------------------------

#: Number of attacks each aircraft makes against its target, by mission type.
#: Mission types not listed here do no damage when auto-resolved.
STRIKE_ATTACKS_PER_AIRCRAFT = {
    FlightType.STRIKE: 2,
    FlightType.BAI: 2,
    FlightType.DEAD: 2,
    FlightType.SEAD: 1,
    FlightType.SEAD_SWEEP: 1,
    FlightType.ANTISHIP: 1,
    FlightType.ARMED_RECON: 2,
    FlightType.CAS: 2,
}
#: Chance that a single attack by a rating 1.0 aircraft destroys a unit.
BASE_STRIKE_PK = 0.45
MIN_STRIKE_RATING = 0.3
#: Chance that a single rating 1.0 aircraft on an OCA/Runway mission closes the
#: runway.
BASE_RUNWAY_PK = 0.35
MAX_STRIKE_PK = 0.9

#: Units never destroyed by auto-resolved strikes. Losing a carrier removes its
#: squadrons, which would pull aircraft out from under flights that are airborne
#: in the simulation.
PROTECTED_UNIT_CLASSES = frozenset(
    {UnitClass.AIRCRAFT_CARRIER, UnitClass.HELICOPTER_CARRIER}
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def capability_rating(priority: int) -> float:
    return _clamp(priority / REFERENCE_TASK_PRIORITY, 0.0, MAX_CAPABILITY_RATING)


def experience_factor(member: FlightMember) -> float:
    if member.pilot is None:
        return 1.0
    missions = min(member.pilot.record.missions_flown, MAX_EXPERIENCE_MISSIONS)
    return 1.0 + missions * EXPERIENCE_BONUS_PER_MISSION


def airframe_air_to_air_rating(aircraft: AircraftType) -> float:
    priorities = [
        aircraft.task_priority(task)
        for task in AIR_TO_AIR_TASKS
        if aircraft.capable_of(task)
    ]
    if not priorities:
        return MIN_AIR_TO_AIR_RATING
    return max(MIN_AIR_TO_AIR_RATING, capability_rating(max(priorities)))


def flight_air_to_air_rating(flight: Flight) -> float:
    """Per-aircraft air-to-air combat rating of the flight."""
    if flight.flight_type in NON_COMBAT_TASKS:
        return 0.0
    rating = airframe_air_to_air_rating(flight.unit_type)
    if not flight.flight_type.is_air_to_air:
        rating *= AIR_TO_GROUND_ROLE_FACTOR
    return rating


@dataclass
class _Combatant:
    flight: Flight
    rating: float
    experience: float

    @property
    def attack(self) -> float:
        return self.rating * self.experience

    @property
    def defense(self) -> float:
        return min(1.0, self.rating * self.experience)


def _combatants(flights: Iterable[Flight]) -> list[_Combatant]:
    combatants = []
    for flight in flights:
        if not flight.alive:
            continue
        rating = flight_air_to_air_rating(flight)
        for member in flight.iter_members():
            combatants.append(_Combatant(flight, rating, experience_factor(member)))
    return combatants


def air_to_air_pk(shooter: _Combatant, target: _Combatant) -> float:
    pk = BASE_AIR_TO_AIR_PK * shooter.attack * (1 - AIR_TO_AIR_EVASION * target.defense)
    return _clamp(pk, 0.0, MAX_AIR_TO_AIR_PK)


def _fire_round(
    shooters: Sequence[_Combatant],
    targets: Sequence[_Combatant],
    rng: random.Random,
) -> set[int]:
    """Returns the indices of targets killed this round."""
    killed: set[int] = set()
    for shooter in shooters:
        if shooter.attack <= 0:
            continue
        idx = rng.randrange(len(targets))
        if rng.random() < air_to_air_pk(shooter, targets[idx]):
            killed.add(idx)
    return killed


def resolve_air_combat(
    flights: Iterable[Flight], rng: Optional[random.Random] = None
) -> dict[Flight, int]:
    """Returns the number of aircraft lost by each flight in an air combat."""
    if rng is None:
        rng = random.Random()
    flights = list(flights)
    blue = _combatants(f for f in flights if f.squadron.player.is_blue)
    red = _combatants(f for f in flights if not f.squadron.player.is_blue)

    losses = {flight: 0 for flight in flights}
    for _ in range(AIR_COMBAT_ROUNDS):
        if not blue or not red:
            break
        red_killed = _fire_round(blue, red, rng)
        blue_killed = _fire_round(red, blue, rng)
        for side, killed in ((blue, blue_killed), (red, red_killed)):
            for idx in killed:
                losses[side[idx].flight] += 1
        blue = [c for i, c in enumerate(blue) if i not in blue_killed]
        red = [c for i, c in enumerate(red) if i not in red_killed]
    return losses


def package_has_sead_support(flight: Flight) -> bool:
    return any(f.alive and f.flight_type in SEAD_TASKS for f in flight.package.flights)


def air_defense_pk(tgo: TheaterGroundObject) -> float:
    """Per-aircraft loss probability for a single air defense site."""
    if not tgo.has_live_radar_sam:
        return SHORT_RANGE_AIR_DEFENSE_PK
    radar_range = tgo.max_threat_range()
    range_fraction = _clamp(radar_range.meters / LONG_RANGE_SAM.meters, 0.0, 1.0)
    return RADAR_SAM_BASE_PK + RADAR_SAM_RANGE_PK * range_fraction


def sam_loss_probability(
    flight: Flight, air_defenses: Iterable[TheaterGroundObject]
) -> float:
    """Per-aircraft (before pilot experience) loss probability for a flight
    engaged by the given air defense sites."""
    sead_support = package_has_sead_support(flight)
    is_sead_flight = flight.flight_type in SEAD_TASKS
    survival = 1.0
    for tgo in air_defenses:
        pk = air_defense_pk(tgo)
        if tgo.has_live_radar_sam:
            if sead_support:
                pk *= SEAD_SUPPORT_FACTOR
            if is_sead_flight:
                pk *= SEAD_FLIGHT_FACTOR
        survival *= 1 - pk
    return _clamp(1 - survival, 0.0, MAX_SAM_PK)


def resolve_sam_engagement(
    flight: Flight,
    air_defenses: Iterable[TheaterGroundObject],
    rng: Optional[random.Random] = None,
) -> int:
    """Returns the number of aircraft the flight loses to the air defenses."""
    if rng is None:
        rng = random.Random()
    if not flight.alive:
        return 0
    pk = sam_loss_probability(flight, air_defenses)
    losses = 0
    for member in flight.iter_members():
        if rng.random() < pk / experience_factor(member):
            losses += 1
    return losses


def strike_rating(flight: Flight) -> float:
    aircraft = flight.unit_type
    if not aircraft.capable_of(flight.flight_type):
        return MIN_STRIKE_RATING
    return max(
        MIN_STRIKE_RATING, capability_rating(aircraft.task_priority(flight.flight_type))
    )


def _is_protected(unit: TheaterUnit) -> bool:
    unit_type = unit.unit_type
    return unit_type is not None and unit_type.unit_class in PROTECTED_UNIT_CLASSES


def _is_air_defense_unit(unit: TheaterUnit) -> bool:
    return bool(unit.threat_range.meters or unit.detection_range.meters)


def strike_candidates(
    flight: Flight, target: TheaterGroundObject, rng: random.Random
) -> list[TheaterUnit]:
    """Alive units of the target in the order they will be attacked."""
    candidates = [u for u in target.units if u.alive and not _is_protected(u)]
    rng.shuffle(candidates)
    if flight.flight_type in SEAD_TASKS:
        # Stable sort keeps the shuffled order within each group.
        candidates.sort(key=lambda u: not _is_air_defense_unit(u))
    return candidates


def resolve_strike(
    flight: Flight,
    target: TheaterGroundObject,
    rng: Optional[random.Random] = None,
) -> list[TheaterUnit]:
    """Returns the units of the target destroyed by the flight."""
    if rng is None:
        rng = random.Random()
    attacks = STRIKE_ATTACKS_PER_AIRCRAFT.get(flight.flight_type, 0)
    if not attacks or not flight.alive:
        return []
    candidates = strike_candidates(flight, target, rng)
    rating = strike_rating(flight)
    destroyed: list[TheaterUnit] = []
    for member in flight.iter_members():
        pk = _clamp(
            BASE_STRIKE_PK * rating * experience_factor(member), 0.0, MAX_STRIKE_PK
        )
        for _ in range(attacks):
            if not candidates:
                return destroyed
            if rng.random() < pk:
                destroyed.append(candidates.pop(0))
    return destroyed


def resolve_runway_strike(flight: Flight, rng: Optional[random.Random] = None) -> bool:
    """Returns True if the flight closes the target runway."""
    if rng is None:
        rng = random.Random()
    if flight.flight_type is not FlightType.OCA_RUNWAY or not flight.alive:
        return False
    rating = strike_rating(flight)
    for member in flight.iter_members():
        pk = _clamp(
            BASE_RUNWAY_PK * rating * experience_factor(member), 0.0, MAX_STRIKE_PK
        )
        if rng.random() < pk:
            return True
    return False
