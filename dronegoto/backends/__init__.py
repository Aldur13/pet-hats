"""Aircraft backends.

The mission controller talks only to `DroneBackend`, so the same safety engine
drives a simulator, a MAVLink aircraft, or (via a port of the rule table) a
DJI airframe.
"""

from .base import BackendError, DroneBackend, FlightCommand

__all__ = ["BackendError", "DroneBackend", "FlightCommand"]
