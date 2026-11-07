from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from game.ato import Flight
    from game.theater import TheaterGroundObject


class CombatMemory:
    """Tracks who has already fought whom during a simulation.

    Without this, a flight that survives an engagement but is still within the
    threat range of the same SAM site (or the commit range of the same enemy
    flight) would immediately enter a new combat with it on the next tick, and the
    outcome would be rolled again every minute until the flight left the area.
    Instead each SAM site engages a given flight once, and each pair of opposing
    flights fights once. Other SAM sites or fresh enemy flights can still engage.
    """

    def __init__(self) -> None:
        self._sams: dict[Flight, set[TheaterGroundObject]] = defaultdict(set)
        self._air: dict[Flight, set[Flight]] = defaultdict(set)

    def record_sam_engagement(
        self, flight: Flight, air_defenses: Iterable[TheaterGroundObject]
    ) -> None:
        self._sams[flight].update(air_defenses)

    def has_engaged_sam(self, flight: Flight, tgo: TheaterGroundObject) -> bool:
        return tgo in self._sams.get(flight, ())

    def record_air_engagement(self, flights: Iterable[Flight]) -> None:
        flights = list(flights)
        for flight in flights:
            for other in flights:
                if flight.squadron.player != other.squadron.player:
                    self._air[flight].add(other)

    def have_fought(self, flight: Flight, other: Flight) -> bool:
        return other in self._air.get(flight, ())
