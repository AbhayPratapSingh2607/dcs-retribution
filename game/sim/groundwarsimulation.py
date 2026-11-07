from __future__ import annotations

import logging
import random
from datetime import timedelta
from typing import Optional, TYPE_CHECKING

from game.ground_forces.combat_stance import CombatStance
from game.settings.settings import CombatResolutionMethod
from .combat.groundresolution import (
    GROUND_COMBAT_ROUND,
    GroundForce,
    apply_front_line_losses,
    resolve_ground_round,
)
from .simulationresults import SimulationResults

if TYPE_CHECKING:
    from game import Game
    from game.ground_forces.ai_ground_planner import CombatGroup
    from game.theater import ControlPoint, FrontLine


class GroundWarSimulation:
    """Fights the front line ground war while fast forwarding.

    Only active when combat is auto-resolved and front line resolution is enabled.
    Losses are applied to the combat groups planned for the turn, so the generated
    mission spawns only the survivors.
    """

    def __init__(self, game: Game, results: SimulationResults) -> None:
        self.game = game
        self.results = results
        self.time_since_last_round = timedelta()
        self.rng = random.Random()

    def enabled(self, combat_resolution_method: CombatResolutionMethod) -> bool:
        return (
            combat_resolution_method is CombatResolutionMethod.RESOLVE
            and self.game.settings.fast_forward_resolve_ground_war
        )

    def on_game_tick(
        self, duration: timedelta, combat_resolution_method: CombatResolutionMethod
    ) -> None:
        if not self.enabled(combat_resolution_method):
            return
        self.time_since_last_round += duration
        while self.time_since_last_round >= GROUND_COMBAT_ROUND:
            self.time_since_last_round -= GROUND_COMBAT_ROUND
            for front_line in self.game.theater.conflicts():
                self.resolve_front_line(front_line)

    def resolve_front_line(self, front_line: FrontLine) -> None:
        blue_cp = front_line.blue_cp
        red_cp = front_line.red_cp
        blue_groups = self.planned_groups(blue_cp, red_cp)
        red_groups = self.planned_groups(red_cp, blue_cp)
        if not blue_groups or not red_groups:
            return
        blue = GroundForce(blue_groups, self.stance(blue_cp, red_cp))
        red = GroundForce(red_groups, self.stance(red_cp, blue_cp))
        blue_losses, red_losses = resolve_ground_round(blue, red, self.rng)
        blue_lost = apply_front_line_losses(
            blue_groups, blue_losses, blue_cp, self.results
        )
        red_lost = apply_front_line_losses(red_groups, red_losses, red_cp, self.results)
        if blue_lost or red_lost:
            logging.debug(
                f"Ground combat at {front_line}: {blue_cp} lost {blue_lost} units, "
                f"{red_cp} lost {red_lost} units"
            )

    def planned_groups(
        self, cp: ControlPoint, enemy: ControlPoint
    ) -> Optional[list[CombatGroup]]:
        planner = self.game.ground_planners.get(cp.id)
        if planner is None:
            return None
        return planner.units_per_cp.get(enemy.id)

    @staticmethod
    def stance(cp: ControlPoint, enemy: ControlPoint) -> CombatStance:
        return cp.stances.get(enemy.id, CombatStance.DEFENSIVE)
