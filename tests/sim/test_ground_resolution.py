from __future__ import annotations

import random
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from game.ato.flighttype import FlightType
from game.debriefing import AirLosses, Debriefing, GroundLosses
from game.ground_forces.ai_ground_planner import CombatGroup, CombatGroupRole
from game.ground_forces.combat_stance import CombatStance
from game.settings.settings import CombatResolutionMethod
from game.sim.combat import groundresolution as gr
from game.sim.groundwarsimulation import GroundWarSimulation
from game.sim.simulationresults import SimulationResults
from game.theater.player import Player
from game.unitmap import FrontLineUnit
from .test_combat_resolution import _Aircraft, make_flight


def unit_type(name: str, price: int) -> Any:
    return SimpleNamespace(name=name, price=price)


MBT = unit_type("MBT", 20)
OLD_MBT = unit_type("Old MBT", 6)
TRUCK = unit_type("Truck", 3)


def group(role: CombatGroupRole, size: int, kind: Any = MBT) -> CombatGroup:
    return CombatGroup(role, kind, size)


def tanks(size: int, kind: Any = MBT) -> list[CombatGroup]:
    return [group(CombatGroupRole.TANK, size, kind)]


def average_losses(
    a_groups: list[CombatGroup],
    a_stance: CombatStance,
    b_groups: list[CombatGroup],
    b_stance: CombatStance,
    rounds: int = 6,
    trials: int = 300,
) -> tuple[float, float]:
    """Average units lost by each side over the given number of rounds."""
    rng = random.Random(7)
    a_total = b_total = 0
    for _ in range(trials):
        a = gr.GroundForce(
            [CombatGroup(g.role, g.unit_type, g.size) for g in a_groups], a_stance
        )
        b = gr.GroundForce(
            [CombatGroup(g.role, g.unit_type, g.size) for g in b_groups], b_stance
        )
        for _ in range(rounds):
            a_losses, b_losses = gr.resolve_ground_round(a, b, rng)
            a_total += gr.apply_front_line_losses(
                a.groups, a_losses, MagicMock(), SimulationResults()
            )
            b_total += gr.apply_front_line_losses(
                b.groups, b_losses, MagicMock(), SimulationResults()
            )
    return a_total / trials, b_total / trials


def test_quality_comes_from_price_relative_to_role() -> None:
    assert gr.unit_quality(group(CombatGroupRole.TANK, 1, MBT)) == 1.0
    assert gr.unit_quality(group(CombatGroupRole.TANK, 1, OLD_MBT)) < 1.0
    assert gr.unit_quality(group(CombatGroupRole.TANK, 1, unit_type("Free", 0))) == (
        gr.MIN_QUALITY
    )


def test_newer_units_are_better() -> None:
    old = group(
        CombatGroupRole.TANK, 1, SimpleNamespace(price=20, year_introduced="1960")
    )
    new = group(
        CombatGroupRole.TANK, 1, SimpleNamespace(price=20, year_introduced="2010")
    )
    unknown = group(
        CombatGroupRole.TANK, 1, SimpleNamespace(price=20, year_introduced="N/A")
    )
    assert gr.unit_quality(old) < gr.unit_quality(unknown) < gr.unit_quality(new)


def test_equal_aggressive_forces_take_moderate_losses() -> None:
    a, b = average_losses(
        tanks(10), CombatStance.AGGRESSIVE, tanks(10), CombatStance.AGGRESSIVE
    )
    # About an hour of fighting.
    assert 0.5 < a < 4
    assert abs(a - b) < 0.5


def test_numbers_matter() -> None:
    small, large = average_losses(
        tanks(5), CombatStance.AGGRESSIVE, tanks(15), CombatStance.AGGRESSIVE
    )
    assert small / 5 > 2 * (large / 15)


def test_quality_matters() -> None:
    old, modern = average_losses(
        tanks(10, OLD_MBT), CombatStance.AGGRESSIVE, tanks(10), CombatStance.AGGRESSIVE
    )
    assert old > 1.5 * modern


def test_defensive_stances_reduce_fighting() -> None:
    quiet = sum(
        average_losses(
            tanks(10), CombatStance.DEFENSIVE, tanks(10), CombatStance.DEFENSIVE
        )
    )
    heavy = sum(
        average_losses(
            tanks(10), CombatStance.AGGRESSIVE, tanks(10), CombatStance.AGGRESSIVE
        )
    )
    assert quiet < heavy / 2


def test_attacking_into_defenders_is_costly() -> None:
    attacker, defender = average_losses(
        tanks(10), CombatStance.BREAKTHROUGH, tanks(10), CombatStance.DEFENSIVE
    )
    assert attacker > defender


def test_rear_units_are_rarely_hit() -> None:
    rng = random.Random(3)
    front = group(CombatGroupRole.TANK, 10)
    rear = group(CombatGroupRole.LOGI, 10, TRUCK)
    front_lost = rear_lost = 0
    for _ in range(300):
        _, losses = gr.resolve_ground_round(
            gr.GroundForce(tanks(10), CombatStance.AGGRESSIVE),
            gr.GroundForce([front, rear], CombatStance.AGGRESSIVE),
            rng,
        )
        front_lost += losses[id(front)]
        rear_lost += losses[id(rear)]
    assert front_lost > 3 * rear_lost


def test_unarmed_force_inflicts_no_losses() -> None:
    trucks = [group(CombatGroupRole.LOGI, 10, TRUCK)]
    trucks_lost, tanks_lost = average_losses(
        trucks, CombatStance.AGGRESSIVE, tanks(5), CombatStance.AGGRESSIVE, trials=20
    )
    assert tanks_lost == 0
    assert trucks_lost > 0


def test_apply_losses_shrinks_and_removes_groups() -> None:
    a = group(CombatGroupRole.TANK, 3)
    b = group(CombatGroupRole.APC, 2)
    groups = [a, b]
    origin = cast(Any, object())
    results = SimulationResults()
    lost = gr.apply_front_line_losses(
        groups, Counter({id(a): 1, id(b): 5}), origin, results
    )
    assert lost == 3
    assert groups == [a]
    assert a.size == 2
    assert [u.unit_type for u in results.front_line_losses] == [MBT, MBT, MBT]
    assert all(u.origin is origin for u in results.front_line_losses)


def test_cas_kills_front_line_units_and_takes_shorad_losses() -> None:
    cas = make_flight(
        _Aircraft({FlightType.CAS: 550}), FlightType.CAS, Player.BLUE, count=4
    )
    no_air_defense = tanks(10)
    with_air_defense = [*tanks(10), group(CombatGroupRole.SHORAD, 1)] * 1
    with_air_defense += [group(CombatGroupRole.SHORAD, 1) for _ in range(5)]

    rng = random.Random(11)
    kills = sum(
        sum(gr.resolve_cas_attack(cas, no_air_defense, rng).values())
        for _ in range(200)
    )
    assert kills > 0
    assert sum(gr.resolve_cas_losses(cas, no_air_defense, rng) for _ in range(50)) == 0
    assert sum(gr.resolve_cas_losses(cas, with_air_defense, rng) for _ in range(50)) > 0


def test_ground_war_simulation_fights_on_schedule() -> None:
    blue_cp = SimpleNamespace(id="blue", captured=Player.BLUE, stances={})
    red_cp = SimpleNamespace(id="red", captured=Player.RED, stances={})
    blue_cp.stances[red_cp.id] = CombatStance.BREAKTHROUGH
    red_cp.stances[blue_cp.id] = CombatStance.BREAKTHROUGH
    blue_groups = tanks(30)
    red_groups = tanks(30)
    game = SimpleNamespace(
        settings=SimpleNamespace(fast_forward_resolve_ground_war=True),
        theater=SimpleNamespace(
            conflicts=lambda: [SimpleNamespace(blue_cp=blue_cp, red_cp=red_cp)]
        ),
        ground_planners={
            "blue": SimpleNamespace(units_per_cp={"red": blue_groups}),
            "red": SimpleNamespace(units_per_cp={"blue": red_groups}),
        },
    )
    results = SimulationResults()
    sim = GroundWarSimulation(cast(Any, game), results)
    sim.rng = random.Random(5)

    # Not enabled unless resolving combat.
    sim.on_game_tick(timedelta(hours=1), CombatResolutionMethod.SKIP)
    assert not results.front_line_losses

    # Nothing happens until a full round has elapsed.
    sim.on_game_tick(
        gr.GROUND_COMBAT_ROUND - timedelta(seconds=1), CombatResolutionMethod.RESOLVE
    )
    assert not results.front_line_losses

    sim.on_game_tick(timedelta(hours=2), CombatResolutionMethod.RESOLVE)
    assert results.front_line_losses
    remaining = sum(g.size for g in blue_groups) + sum(g.size for g in red_groups)
    assert remaining + len(results.front_line_losses) == 60

    game.settings.fast_forward_resolve_ground_war = False
    before = len(results.front_line_losses)
    sim.on_game_tick(timedelta(hours=2), CombatResolutionMethod.RESOLVE)
    assert len(results.front_line_losses) == before


class _ControlPoint:
    def __init__(self, captured: Player) -> None:
        self.captured = captured


def test_debriefing_includes_front_line_losses_once() -> None:
    blue_cp = cast(Any, _ControlPoint(Player.BLUE))
    red_cp = cast(Any, _ControlPoint(Player.RED))
    results = SimulationResults()
    results.kill_front_line_unit(FrontLineUnit(MBT, blue_cp))
    results.kill_front_line_unit(FrontLineUnit(MBT, red_cp))
    results.kill_front_line_unit(FrontLineUnit(MBT, red_cp))

    debriefing = Debriefing.__new__(Debriefing)
    debriefing.air_losses = AirLosses([], [])
    debriefing.ground_losses = GroundLosses()
    assert debriefing.casualty_count(red_cp) == 0
    debriefing.merge_simulation_results(results)
    debriefing.merge_simulation_results(results)

    assert len(debriefing.ground_losses.player_front_line) == 1
    assert len(debriefing.ground_losses.enemy_front_line) == 2
    # The cached casualty counts are refreshed by the merge.
    assert debriefing.casualty_count(red_cp) == 2
    assert debriefing.casualty_count(blue_cp) == 1
