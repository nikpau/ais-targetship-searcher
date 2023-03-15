from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass, field
from datetime import datetime

Latitude  = float
Longitude = float

_DATEFORMAT = "%Y-%m-%d %H:%M:%S"

Position = namedtuple("Position", ["lat","lon"])

class _OUT_OF_BOUNDS_TYPE:
    pass
OUTOFBOUNDS = _OUT_OF_BOUNDS_TYPE()

class ShellError(Exception):
    pass

@dataclass(frozen=True)
class BoundingBox:
    LATMIN: Latitude
    LATMAX: Latitude
    LONMIN: Longitude
    LONMAX: Longitude
    
    def __repr__(self) -> str:
        return (
            "<BoundingBox("
            f"LATMIN={self.LATMIN:.3f},"
            f"LATMAX={self.LATMAX:.3f},"
            f"LONMIN={self.LONMIN:.3f},"
            f"LONMAX={self.LONMAX:.3f})>"
        )
    
@dataclass(frozen=True)
class Cell(BoundingBox):
    index: int
    
    def __repr__(self) -> str:
        return f"{super().__repr__()}|idx={self.index}"

@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def is_on_left_of(self, other: Point)-> bool:
        return self.x < other.x
        
    def is_above_of(self, other: Point) -> bool:
        return self.y > other.y
    
@dataclass
class TimePosition:
    """
    Time and position object
    """
    timestamp: datetime
    lat: Latitude
    lon: Longitude
    as_array: list[float] = field(default=list)

    def __post_init__(self)-> None:
        self.timestamp = self._validate_timestamp()
        self.as_array = [self.timestamp,self.lat,self.lon]
        
    def _validate_timestamp(self) -> datetime:
        try:
            return datetime.strptime(self.timestamp,_DATEFORMAT)
        except ValueError:
            raise ValueError(
                "Incorrect date format, should be YYYY-MM-DD HH:MM"
            )
    
    @property
    def position(self) -> Position:
        """Return position as 
        (lat,lon)-namedtuple"""
        return Position(self.lat,self.lon)

@dataclass
class AdjacentCells:
    N:  Cell | _OUT_OF_BOUNDS_TYPE
    NE: Cell | _OUT_OF_BOUNDS_TYPE
    E:  Cell | _OUT_OF_BOUNDS_TYPE
    SE: Cell | _OUT_OF_BOUNDS_TYPE
    S:  Cell | _OUT_OF_BOUNDS_TYPE
    SW: Cell | _OUT_OF_BOUNDS_TYPE
    W:  Cell | _OUT_OF_BOUNDS_TYPE
    NW: Cell | _OUT_OF_BOUNDS_TYPE


# Map from subcells to 
# adjacent cells to be
# pre-buffered.
SUB_TO_ADJ: dict[int,tuple[str,...]] = {
    1: ("W","NW","N"),
    2: ("N","NE","E"),
    3: ("S","SW","W"),
    4: ("E","SE","S")
}
