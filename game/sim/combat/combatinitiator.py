from __future__ import annotations

import itertools
import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Optional, TYPE_CHECKING

from game.theater.player import Player
from .aircombat import AirCombat
from .aircraftengagementzones import AircraftEngagementZones
from .atip import AtIp
from .combatmemory import CombatMemory
from .defendingsam import DefendingSam
from .joinablecombat import JoinableCombat
from .samengagementzones import SamEngagementZones
from ..gameupdateevents import GameUpdateEvents

if TYPE_CHECKING:
    from game import Game
    from game.ato import Flight
    from .frozencombat import FrozenCombat


class CombatInitiator:
    def __init__(
        self,
        game: Game,
        combats: list[FrozenCombat],
        events: GameUpdateEvents,
        memory: Optional[CombatMemory] = None,
    ) -> None:
        self.game = game
        self.combats = combats
        self.events = events
        self.memory = memory if memory is not None else CombatMemory()

    def update_active_combats(self) -> None:
        blue_a2a = AircraftEngagementZones.from_ato(self.game.blue.ato)
        red_a2a = AircraftEngagementZones.from_ato(self.game.red.ato)
        blue_sam = SamEngagementZones.from_theater(
            self.game.theater, player=Player.BLUE
        )
        red_sam = SamEngagementZones.from_theater(self.game.theater, player=Player.RED)

        # Check each vulnerable flight to see if it has initiated combat. If any flight
        # initiates combat, a single FrozenCombat will be created for all involved
        # flights and the FlightState of each flight will be updated accordingly.
        #
        # There's some nuance to this behavior. Duplicate combats are avoided because
        # InCombat flight states are not considered vulnerable. That means that once an
        # aircraft has entered combat it will not be rechecked later in the loop or on
        # another tick.
        for flight in self.iter_flights():
            if flight.state.in_combat:
                continue

            if flight.squadron.player.is_blue:
                a2a = red_a2a
                own_a2a = blue_a2a
                sam = red_sam
            else:
                a2a = blue_a2a
                own_a2a = red_a2a
                sam = blue_sam
            self.check_flight_for_combat(flight, a2a, own_a2a, sam)

    def check_flight_for_combat(
        self,
        flight: Flight,
        a2a: AircraftEngagementZones,
        own_a2a: AircraftEngagementZones,
        sam: SamEngagementZones,
    ) -> None:
        if (joined := self.check_flight_for_joined_combat(flight)) is not None:
            logging.info(f"{flight} is joining existing combat {joined}")
            joined.join(flight)
            self.memory.record_air_engagement(joined.flights)
            own_a2a.remove_flight(flight)
            self.events.update_combat(joined)
        elif (
            combat := self.check_flight_for_new_combat(flight, a2a, sam, self.memory)
        ) is not None:
            logging.info(f"Creating new combat because {combat.because()}")
            combat.update_flight_states()
            if isinstance(combat, AirCombat):
                self.memory.record_air_engagement(combat.flights)
            elif isinstance(combat, DefendingSam):
                self.memory.record_sam_engagement(flight, combat.air_defenses)
            # Remove any preoccupied flights from the list of potential air-to-air
            # threats. This prevents BARCAPs (and other air-to-air types) from getting
            # involved in multiple combats simultaneously. Additional air-to-air
            # aircraft may join existing combats, but they will not create new combats.
            a2a.update_for_combat(combat)
            own_a2a.update_for_combat(combat)
            self.combats.append(combat)
            self.events.new_combat(combat)

    def check_flight_for_joined_combat(
        self, flight: Flight
    ) -> Optional[JoinableCombat]:
        for combat in self.combats:
            if isinstance(combat, JoinableCombat) and combat.joinable_by(flight):
                return combat
        return None

    @staticmethod
    def check_flight_for_new_combat(
        flight: Flight,
        a2a: AircraftEngagementZones,
        sam: SamEngagementZones,
        memory: Optional[CombatMemory] = None,
    ) -> Optional[FrozenCombat]:
        if memory is None:
            memory = CombatMemory()

        if not flight.state.in_flight:
            return None

        if flight.state.is_at_ip and not flight.state.avoid_further_combat:
            return AtIp(timedelta(minutes=1), flight)

        position = flight.state.estimate_position()

        if flight.state.vulnerable_to_intercept and a2a.covers(position):
            interceptors = [
                f
                for f in a2a.iter_intercepting_flights(position)
                if not memory.have_fought(flight, f)
            ]
            if interceptors:
                return AirCombat(timedelta(minutes=1), [flight, *interceptors])

        if flight.state.vulnerable_to_sam and sam.covers(position):
            air_defenses = [
                tgo
                for tgo in sam.iter_threatening_sams(position)
                if not memory.has_engaged_sam(flight, tgo)
            ]
            if air_defenses:
                return DefendingSam(timedelta(minutes=1), flight, air_defenses)

        return None

    def iter_flights(self) -> Iterator[Flight]:
        packages = itertools.chain(
            self.game.blue.ato.packages, self.game.red.ato.packages
        )
        for package in packages:
            yield from package.flights
