from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any, Optional, cast
from unittest.mock import MagicMock

from dcs import Point

from game.ato import Flight
from game.ato.flightmember import FlightMember
from game.ato.flightmembers import FlightMembers
from game.ato.flightstate.killed import Killed
from game.ato.flighttype import FlightType
from game.ato.loadouts import Loadout
from game.data.units import UnitClass
from game.debriefing import AirLosses, Debriefing, GroundLosses
from game.sim.combat import CombatMemory
from game.sim.combat import resolution
from game.sim.combat.aircombat import AirCombat
from game.sim.combat.combatinitiator import CombatInitiator
from game.sim.combat.defendingsam import DefendingSam
from game.sim.simulationresults import SimulationResults
from game.squadrons.pilot import Pilot
from game.theater.player import Player
from game.utils import meters, nautical_miles


class _Aircraft:
    def __init__(self, priorities: dict[FlightType, int]) -> None:
        self.priorities = priorities

    def capable_of(self, task: FlightType) -> bool:
        return task in self.priorities

    def task_priority(self, task: FlightType) -> int:
        return self.priorities[task]


FIGHTER = _Aircraft(
    {
        FlightType.BARCAP: 550,
        FlightType.TARCAP: 550,
        FlightType.ESCORT: 550,
        FlightType.INTERCEPTION: 550,
        FlightType.SWEEP: 550,
    }
)
BOMBER = _Aircraft({FlightType.STRIKE: 550})


class _State:
    def __init__(self) -> None:
        self.alive = True

    def estimate_position(self) -> Point:
        return cast(Point, SimpleNamespace(x=0, y=0))


def make_flight(
    aircraft: _Aircraft,
    flight_type: FlightType,
    player: Player,
    count: int = 2,
    players: int = 0,
    missions_flown: int = 0,
    package: Any = None,
) -> Flight:
    flight = Flight.__new__(Flight)
    flight.squadron = cast(Any, SimpleNamespace(player=player, settings=None))
    flight.flight_type = flight_type
    flight.package = cast(Any, package or SimpleNamespace(flights=[flight]))
    flight.state = cast(Any, _State())
    roster = FlightMembers.__new__(FlightMembers)
    roster.flight = flight
    roster.members = []
    for idx in range(count):
        pilot = Pilot(f"pilot {idx}", player=idx < players)
        pilot.record.missions_flown = missions_flown
        roster.members.append(FlightMember(pilot, Loadout.empty_loadout()))
    flight.roster = roster
    # unit_type is a property returning squadron.aircraft.
    flight.squadron.aircraft = cast(Any, aircraft)
    return flight


def test_airframe_rating_uses_best_air_to_air_priority() -> None:
    assert resolution.airframe_air_to_air_rating(cast(Any, FIGHTER)) == 1.0
    assert (
        resolution.airframe_air_to_air_rating(cast(Any, BOMBER))
        == resolution.MIN_AIR_TO_AIR_RATING
    )


def test_role_affects_air_to_air_rating() -> None:
    cap = make_flight(FIGHTER, FlightType.BARCAP, Player.BLUE)
    strike = make_flight(FIGHTER, FlightType.STRIKE, Player.BLUE)
    tanker = make_flight(FIGHTER, FlightType.REFUELING, Player.BLUE)
    assert resolution.flight_air_to_air_rating(cap) == 1.0
    assert (
        resolution.flight_air_to_air_rating(strike)
        == resolution.AIR_TO_GROUND_ROLE_FACTOR
    )
    assert resolution.flight_air_to_air_rating(tanker) == 0.0


def _total_losses(blue: Flight, red: Flight, trials: int = 300) -> tuple[int, int]:
    rng = random.Random(1234)
    blue_losses = red_losses = 0
    for _ in range(trials):
        losses = resolution.resolve_air_combat([blue, red], rng)
        blue_losses += losses[blue]
        red_losses += losses[red]
    return blue_losses, red_losses


def test_fighters_beat_strikers() -> None:
    fighters = make_flight(FIGHTER, FlightType.BARCAP, Player.BLUE, count=2)
    strikers = make_flight(BOMBER, FlightType.STRIKE, Player.RED, count=2)
    blue_losses, red_losses = _total_losses(fighters, strikers)
    assert red_losses > 3 * blue_losses


def test_numbers_matter() -> None:
    small = make_flight(FIGHTER, FlightType.BARCAP, Player.BLUE, count=2)
    large = make_flight(FIGHTER, FlightType.BARCAP, Player.RED, count=4)
    small_losses, large_losses = _total_losses(small, large)
    # The larger force loses fewer aircraft in absolute terms, and a far smaller
    # fraction of its force.
    assert small_losses > large_losses
    assert small_losses / 2 > 2 * (large_losses / 4)


def test_experience_matters() -> None:
    rookies = make_flight(FIGHTER, FlightType.BARCAP, Player.BLUE, count=4)
    veterans = make_flight(
        FIGHTER, FlightType.BARCAP, Player.RED, count=4, missions_flown=10
    )
    rookie_losses, veteran_losses = _total_losses(rookies, veterans)
    assert rookie_losses > veteran_losses


def test_unarmed_flights_cannot_kill() -> None:
    awacs = make_flight(FIGHTER, FlightType.AEWC, Player.BLUE, count=1)
    cap = make_flight(FIGHTER, FlightType.BARCAP, Player.RED, count=2)
    awacs_losses, cap_losses = _total_losses(awacs, cap, trials=50)
    assert cap_losses == 0
    assert awacs_losses > 0


def test_air_combat_losses_never_exceed_flight_size() -> None:
    rng = random.Random(1)
    blue = make_flight(FIGHTER, FlightType.BARCAP, Player.BLUE, count=1)
    red = make_flight(FIGHTER, FlightType.BARCAP, Player.RED, count=8)
    for _ in range(200):
        losses = resolution.resolve_air_combat([blue, red], rng)
        assert 0 <= losses[blue] <= 1
        assert 0 <= losses[red] <= 8


def _sam(radar: bool, threat_range: float) -> Any:
    return SimpleNamespace(
        has_live_radar_sam=radar, max_threat_range=lambda: meters(threat_range)
    )


def test_sam_threat_depends_on_type_and_range() -> None:
    long_range = _sam(True, nautical_miles(40).meters)
    short_range = _sam(True, 10000)
    manpads = _sam(False, 5000)
    assert (
        resolution.air_defense_pk(long_range)
        > resolution.air_defense_pk(short_range)
        > resolution.air_defense_pk(manpads)
    )


def test_more_sams_are_more_dangerous_and_capped() -> None:
    flight = make_flight(BOMBER, FlightType.STRIKE, Player.BLUE)
    sam = _sam(True, 30000)
    one = resolution.sam_loss_probability(flight, [sam])
    three = resolution.sam_loss_probability(flight, [sam] * 3)
    many = resolution.sam_loss_probability(flight, [sam] * 50)
    assert one < three
    assert many == resolution.MAX_SAM_PK


def test_sead_support_suppresses_radar_sams() -> None:
    package = SimpleNamespace(flights=[])
    striker = make_flight(BOMBER, FlightType.STRIKE, Player.BLUE, package=package)
    package.flights.append(striker)
    sam = _sam(True, 30000)
    manpads = _sam(False, 5000)
    unescorted = resolution.sam_loss_probability(striker, [sam])
    unescorted_ir = resolution.sam_loss_probability(striker, [manpads])

    sead = make_flight(FIGHTER, FlightType.SEAD_ESCORT, Player.BLUE, package=package)
    package.flights.append(sead)
    assert resolution.sam_loss_probability(striker, [sam]) < unescorted
    # IR SAMs and AAA are unaffected by SEAD.
    assert resolution.sam_loss_probability(striker, [manpads]) == unescorted_ir
    # The SEAD flight itself is better protected still.
    assert resolution.sam_loss_probability(
        sead, [sam]
    ) < resolution.sam_loss_probability(striker, [sam])

    # Dead SEAD flights provide no support.
    cast(Any, sead.state).alive = False
    assert resolution.sam_loss_probability(striker, [sam]) == unescorted


class _Unit:
    def __init__(
        self,
        unit_id: int,
        air_defense: bool = False,
        unit_class: Optional[UnitClass] = None,
    ) -> None:
        self.id = unit_id
        self.alive = True
        self.threat_range = meters(10000 if air_defense else 0)
        self.detection_range = meters(0)
        self.unit_type = (
            None if unit_class is None else SimpleNamespace(unit_class=unit_class)
        )


def test_sead_attacks_air_defenses_first() -> None:
    radars = [_Unit(i, air_defense=True) for i in range(2)]
    trucks = [_Unit(10 + i) for i in range(10)]
    target = SimpleNamespace(units=[*trucks, *radars])
    flight = make_flight(
        _Aircraft({FlightType.DEAD: 550}), FlightType.DEAD, Player.BLUE, count=1
    )
    for seed in range(20):
        order = resolution.strike_candidates(
            flight, cast(Any, target), random.Random(seed)
        )
        assert set(u.id for u in order[:2]) == {0, 1}


def test_strike_never_destroys_carriers_or_dead_units() -> None:
    carrier = _Unit(1, unit_class=UnitClass.AIRCRAFT_CARRIER)
    dead = _Unit(2)
    dead.alive = False
    escort = _Unit(3)
    target = SimpleNamespace(units=[carrier, dead, escort])
    flight = make_flight(
        _Aircraft({FlightType.STRIKE: 750}), FlightType.STRIKE, Player.BLUE, count=4
    )
    for seed in range(20):
        destroyed = resolution.resolve_strike(
            flight, cast(Any, target), random.Random(seed)
        )
        assert all(u is escort for u in destroyed)
        assert len(destroyed) <= 1


def test_strike_damage_scales_with_flight_size() -> None:
    def average_kills(count: int) -> float:
        rng = random.Random(42)
        total = 0
        for _ in range(200):
            target = SimpleNamespace(units=[_Unit(i) for i in range(30)])
            flight = make_flight(BOMBER, FlightType.STRIKE, Player.BLUE, count=count)
            total += len(resolution.resolve_strike(flight, cast(Any, target), rng))
        return total / 200

    assert average_kills(4) > 1.5 * average_kills(2) > 0


def test_non_attack_missions_do_no_damage() -> None:
    target = SimpleNamespace(units=[_Unit(i) for i in range(5)])
    flight = make_flight(FIGHTER, FlightType.ESCORT, Player.BLUE, count=4)
    assert resolution.resolve_strike(flight, cast(Any, target), random.Random()) == []


def test_kill_members_removes_ai_wingmen_first() -> None:
    flight = make_flight(BOMBER, FlightType.STRIKE, Player.BLUE, count=4, players=1)
    lead = flight.roster.members[0]
    results = SimulationResults()
    events = MagicMock()

    assert flight.kill_members(2, results, events) == 2
    assert flight.count == 2
    assert flight.alive
    assert lead in flight.roster.members
    assert [loss.pilot.name for loss in results.air_losses] == [  # type: ignore[union-attr]
        "pilot 3",
        "pilot 2",
    ]

    # Only the player is left after the next loss.
    flight.kill_members(1, results, events)
    assert flight.roster.members == [lead]


def test_kill_members_kills_flight_when_none_remain() -> None:
    flight = make_flight(BOMBER, FlightType.STRIKE, Player.BLUE, count=2)
    results = SimulationResults()
    assert flight.kill_members(5, results, MagicMock()) == 2
    assert isinstance(flight.state, Killed)
    assert len(results.air_losses) == 2


def test_kill_records_aircraft_without_pilots() -> None:
    flight = make_flight(BOMBER, FlightType.STRIKE, Player.BLUE, count=2)
    flight.roster.members[1].pilot = None
    results = SimulationResults()
    flight.kill(results, MagicMock())
    assert len(results.air_losses) == 2


def test_combat_memory() -> None:
    blue = make_flight(FIGHTER, FlightType.BARCAP, Player.BLUE)
    red = make_flight(FIGHTER, FlightType.BARCAP, Player.RED)
    other_red = make_flight(FIGHTER, FlightType.BARCAP, Player.RED)
    sam = object()
    memory = CombatMemory()
    memory.record_air_engagement([blue, red])
    memory.record_sam_engagement(blue, [cast(Any, sam)])
    assert memory.have_fought(blue, red)
    assert memory.have_fought(red, blue)
    assert not memory.have_fought(blue, other_red)
    assert not memory.have_fought(red, other_red)
    assert memory.has_engaged_sam(blue, cast(Any, sam))
    assert not memory.has_engaged_sam(red, cast(Any, sam))


class _InFlightState(_State):
    in_flight = True
    is_at_ip = False
    avoid_further_combat = False
    vulnerable_to_intercept = True
    vulnerable_to_sam = True


def _zones(*items: Any) -> Any:
    return SimpleNamespace(
        covers=lambda position: bool(items),
        iter_intercepting_flights=lambda position: iter(items),
        iter_threatening_sams=lambda position: iter(items),
    )


def test_initiator_does_not_repeat_engagements(monkeypatch: Any) -> None:
    monkeypatch.setattr(AirCombat, "__init__", _record_init)
    monkeypatch.setattr(DefendingSam, "__init__", _record_init)
    flight = make_flight(BOMBER, FlightType.STRIKE, Player.BLUE)
    flight.state = cast(Any, _InFlightState())
    interceptor = make_flight(FIGHTER, FlightType.BARCAP, Player.RED)
    sam_a, sam_b = object(), object()
    memory = CombatMemory()
    no_a2a, no_sam = _zones(), _zones()

    combat = CombatInitiator.check_flight_for_new_combat(
        flight, _zones(interceptor), no_sam, memory
    )
    assert isinstance(combat, AirCombat)
    memory.record_air_engagement([flight, interceptor])
    assert (
        CombatInitiator.check_flight_for_new_combat(
            flight, _zones(interceptor), no_sam, memory
        )
        is None
    )

    combat = CombatInitiator.check_flight_for_new_combat(
        flight, no_a2a, _zones(sam_a), memory
    )
    assert isinstance(combat, DefendingSam)
    memory.record_sam_engagement(flight, [cast(Any, sam_a)])
    assert (
        CombatInitiator.check_flight_for_new_combat(
            flight, no_a2a, _zones(sam_a), memory
        )
        is None
    )
    # A new site still engages, but only the new site.
    combat = CombatInitiator.check_flight_for_new_combat(
        flight, no_a2a, _zones(sam_a, sam_b), memory
    )
    assert isinstance(combat, DefendingSam)
    assert combat.init_args[2] == [sam_b]  # type: ignore[attr-defined]


def _record_init(self: Any, *args: Any) -> None:
    self.init_args = args


def test_debriefing_includes_auto_resolved_ground_losses() -> None:
    enemy_tgo = SimpleNamespace(is_friendly=lambda player: player is Player.RED)
    friendly_tgo = SimpleNamespace(is_friendly=lambda player: player is Player.BLUE)
    enemy_unit = SimpleNamespace(id=1, ground_object=enemy_tgo)
    friendly_unit = SimpleNamespace(id=2, ground_object=friendly_tgo)
    enemy_airfield = SimpleNamespace(captured=Player.RED)

    results = SimulationResults()
    results.kill_ground_object_unit(cast(Any, enemy_unit))
    results.kill_ground_object_unit(cast(Any, friendly_unit))
    results.damage_runway(cast(Any, enemy_airfield))
    results.damage_runway(cast(Any, enemy_airfield))

    debriefing = Debriefing.__new__(Debriefing)
    debriefing.air_losses = AirLosses([], [])
    debriefing.ground_losses = GroundLosses()
    debriefing.merge_simulation_results(results)

    assert [m.theater_unit for m in debriefing.ground_losses.enemy_ground_objects] == [
        enemy_unit
    ]
    assert [m.theater_unit for m in debriefing.ground_losses.player_ground_objects] == [
        friendly_unit
    ]
    assert debriefing.ground_losses.enemy_airfields == [enemy_airfield]

    # Merging again doesn't double count.
    debriefing.merge_simulation_results(results)
    assert len(debriefing.ground_losses.enemy_ground_objects) == 1
    assert len(debriefing.ground_losses.enemy_airfields) == 1
