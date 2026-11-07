from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from game.unitmap import FlyingUnit, FrontLineUnit

if TYPE_CHECKING:
    from game.ato import Flight
    from game.squadrons import Pilot
    from game.theater import Airfield
    from game.theater.theatergroup import TheaterUnit


# TODO: Serialize for bug reproducibility.
# Any bugs filed that can only be reproduced with auto-resolved combat results will not
# be reproducible since we cannot replay the auto-resolution that the player saw. We
# need to be able to serialize this data so bug repro can include the auto-resolved
# results.
@dataclass
class SimulationResults:
    air_losses: list[FlyingUnit] = field(default_factory=list)
    #: Ground object units destroyed by auto-resolved strikes. These units are
    #: killed immediately (so they are spawned dead and stop contributing to threat
    #: zones), and are recorded here so they appear in the debriefing.
    ground_object_losses: list[TheaterUnit] = field(default_factory=list)
    #: Runways cratered by auto-resolved OCA/Runway strikes. Runway damage is
    #: applied when the debriefing is committed.
    damaged_runways: list[Airfield] = field(default_factory=list)
    #: Front line units destroyed by auto-resolved ground combat or CAS. These are
    #: removed from the planned combat groups immediately (so they are not spawned)
    #: and the losses are committed with the debriefing.
    front_line_losses: list[FrontLineUnit] = field(default_factory=list)

    def kill_pilot(self, flight: Flight, pilot: Optional[Pilot]) -> None:
        self.air_losses.append(FlyingUnit(flight, pilot))

    def kill_front_line_unit(self, unit: FrontLineUnit) -> None:
        self.front_line_losses.append(unit)

    def kill_ground_object_unit(self, unit: TheaterUnit) -> None:
        self.ground_object_losses.append(unit)

    def damage_runway(self, airfield: Airfield) -> None:
        if airfield not in self.damaged_runways:
            self.damaged_runways.append(airfield)
