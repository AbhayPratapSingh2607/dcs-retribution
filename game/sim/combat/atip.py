from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .frozencombat import FrozenCombat
from .. import GameUpdateEvents
from ...ato.flightstate import InCombat
from game.settings.settings import CombatResolutionMethod
from game.theater import Airfield, TheaterGroundObject
from .resolution import resolve_runway_strike, resolve_strike

if TYPE_CHECKING:
    from game.ato import Flight
    from ..simulationresults import SimulationResults


class AtIp(FrozenCombat):
    def __init__(self, freeze_duration: timedelta, flight: Flight) -> None:
        super().__init__(freeze_duration)
        self.flight = flight

    def because(self) -> str:
        return f"{self.flight} is at its IP"

    def describe(self) -> str:
        return f"at IP"

    def iter_flights(self) -> Iterator[Flight]:
        yield self.flight

    def resolve(
        self,
        results: SimulationResults,
        events: GameUpdateEvents,
        time: datetime,
        elapsed_time: timedelta,
        resolution_method: CombatResolutionMethod,
    ) -> None:
        assert isinstance(self.flight.state, InCombat)
        if resolution_method is not CombatResolutionMethod.SKIP:
            self.resolve_attack(results, events)
        else:
            logging.debug(
                f"{self.flight} attack on {self.flight.package.target} auto-resolved "
                "with mission failure but no losses"
            )
        self.flight.state.exit_combat(
            events, time, elapsed_time, avoid_further_combat=True
        )

    def resolve_attack(
        self, results: SimulationResults, events: GameUpdateEvents
    ) -> None:
        # Flights with players in them will be flown by those players, so don't
        # decide the outcome for them.
        if self.flight.client_count:
            logging.debug(
                f"Not auto-resolving {self.flight} attack because it has players"
            )
            return

        target = self.flight.package.target
        if isinstance(target, TheaterGroundObject):
            destroyed = resolve_strike(self.flight, target)
            for unit in destroyed:
                unit.kill(events)
                results.kill_ground_object_unit(unit)
            logging.debug(
                f"{self.flight} attack on {target} auto-resolved destroying "
                f"{len(destroyed)} units"
            )
        elif isinstance(target, Airfield):
            if resolve_runway_strike(self.flight):
                results.damage_runway(target)
                logging.debug(
                    f"{self.flight} attack on {target} auto-resolved closing the runway"
                )
            else:
                logging.debug(
                    f"{self.flight} attack on {target} auto-resolved with the runway "
                    "still open"
                )
        else:
            logging.debug(
                f"{self.flight} attack on {target} cannot be auto-resolved for this "
                "target type"
            )
