"""
This module aims at finding all spatio-temporal
neighbors around a given (stripped) AIS Message. 

A provided geographical area will be split into 
evenly-sized grids
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import List, Union

import numpy as np
import pandas as pd
import utm
from matplotlib import pyplot as plt
from matplotlib.gridspec import GridSpec
from scipy.interpolate import UnivariateSpline, interp1d

# Settings for numerical integration
Q_SETTINGS = dict(epsabs=1e-13,epsrel=1e-13,limit=500)



# Type aliases
Latitude = float
Longitude = float

# Constants
PI = np.pi

# Exceptions

class OutofTimeBoundsError(Exception):
    pass

class InterpolationError(Exception):
    pass


class TrackSplines:
    """
    Class for performing separate univariate
    spline interpolation on a given AIS track.

    This class provides the following attributes:
        - spl_northing: Spline interpolation of northing
        - spl_easting: Spline interpolation of easting
        - spl_COG: Spline interpolation of course over ground
        - spl_SOG: Spline interpolation of speed over ground
        - spl_ROT: Spline interpolation of rate of turn
        - spl_dROT: Spline interpolation of change of rate of turn

        All splines are univariate splines, with time as the
        independent variable.
    """
    def __init__(self, track: List[AISMessage]) -> None:
        self.track = track
        self._attach_splines()

    def _attach_splines(self) -> None:
        """
        Perform spline interpolation on the
        AIS track and attach the results to
        the class as attributes.
        """
        timestamps = [msg.timestamp for msg in self.track]

        self.northing = UnivariateSpline(
            timestamps, [msg.northing for msg in self.track]
        )
        self.easting = UnivariateSpline(
            timestamps, [msg.easting for msg in self.track]
        )
        self.COG = UnivariateSpline(
            timestamps, [msg.COG for msg in self.track]
        )
        self.SOG = UnivariateSpline(
            timestamps, [msg.SOG for msg in self.track]
        )
        self.ROT = UnivariateSpline(
            timestamps, [msg.ROT for msg in self.track]
        )
        self.dROT = UnivariateSpline(
            timestamps, [msg.dROT for msg in self.track]
        )

class TrackLinear:
    """
    Linear interpolation of a given AIS track.
    """
    def __init__(self, track: List[AISMessage]) -> None:
        self.track = track
        self._attach_linear()

    def _attach_linear(self) -> None:
        """
        Perform linear interpolation on the
        AIS track and attach the results to
        the class as attributes.
        """
        timestamps = [msg.timestamp for msg in self.track]

        self.northing = interp1d(
            timestamps, [msg.northing for msg in self.track]
        )
        self.easting = interp1d(
            timestamps, [msg.easting for msg in self.track]
        )
        self.COG = interp1d(
            timestamps, [msg.COG for msg in self.track]
        )
        self.SOG = interp1d(
            timestamps, [msg.SOG for msg in self.track]
        )
        self.ROT = interp1d(
            timestamps, [msg.ROT for msg in self.track]
        )
        # Derivative of ROT is not available in linear interpolation
        # as there are too few points to perform numerical differentiation.
        # Instead, we set it to zero.
        self.dROT = interp1d(
            timestamps, [0.0 for _ in self.track]
        )


@dataclass
class AISMessage:
    """
    AIS Message object
    """
    sender: int
    timestamp: datetime
    lat: Latitude
    lon: Longitude
    COG: float # Course over ground [degrees]
    SOG: float # Speed over ground [knots]
    ROT: float # Rate of turn [degrees/minute]
    dROT: float = None # Change of ROT [degrees/minute²]
    _utm: bool = False

    def __post_init__(self) -> None:
        self.easting, self.northing, self.zone_number, self.zone_letter = utm.from_latlon(
            self.lat, self.lon
        )
        self.as_array = np.array(
            [self.northing,self.easting,self.COG,self.SOG]
        ).reshape(1,-1) if self._utm else np.array(
            [self.lat,self.lon,self.COG,self.SOG]
        ).reshape(1,-1)
        self.ROT = self._rot_handler(self.ROT)
    
    def _rot_handler(self, rot: float) -> float:
        """
        Handles the Rate of Turn (ROT) value
        """
        try: 
            rot = float(rot) 
        except: 
            return None
        
        sign = np.sign(rot)
        if abs(rot) == 127 or abs(rot) == 128:
            return None
        else:
            return sign * (rot / 4.733)**2
        

class TargetVessel:
    
    def __init__(
        self,
        ts: Union[str,datetime], 
        mmsi: int, 
        track: List[AISMessage]
        ) -> None:
        
        self.ts = ts
        self.mmsi = mmsi 
        self.track = track
        self.lininterp = False # Linear interpolation flag
    
    def interpolate(self) -> None:
        """
        Construct splines for the target vessel
        """
        try:
            if self.lininterp:
                self.interpolation = TrackLinear(self.track)
            else:
                self.interpolation = TrackSplines(self.track)
        except Exception as e:
            raise InterpolationError(
                f"Could not interpolate the target vessel trajectory:\n{e}."
            )

    def observe_at_query(self) -> np.ndarray:
        """
        Infers
            - Northing (meters),
            - Easting (meters),
            - Course over ground (COG) [degrees],
            - Speed over ground (SOG) [knots],
            - Rate of turn (ROT) [degrees/minute],
            - Change of ROT [degrees/minute²],
            
        from univariate splines for the timestamp, 
        the object was initialized with.

        """
        # Convert query timestamp to unix time
        ts = self.ts.timestamp()

        # Return the observed values from the splines
        # at the given timestamp
        # Returns a 1x6 array:
        # [northing, easting, COG, SOG, ROT, dROT]
        return np.array([
            self.interpolation.northing(ts),
            self.interpolation.easting(ts),
            # Take the modulo 360 to ensure that the
            # course over ground is in the interval [0,360]
            self.interpolation.COG(ts) % 360,
            self.interpolation.SOG(ts),
            self.interpolation.ROT(ts),
            self.interpolation.dROT(ts),
        ])

    def observe_interval(
            self, 
            start: datetime, 
            end: datetime, 
            interval: int
            ) -> np.ndarray:
        """
        Infers
            - Northing (meters),
            - Easting (meters),
            - Course over ground (COG) [degrees],
            - Speed over ground (SOG) [knots],
            - Rate of turn (ROT) [degrees/minute],
            - Change of ROT [degrees/minute²],

        from univariate splines for the track between
        the given start and end timestamps, with the
        given interval in [seconds].
        """
        # Convert query timestamps to unix time
        if isinstance(start, datetime):
            start = start.timestamp()
            end = end.timestamp()

        # Check if the interval boundary is within the
        # track's timestamps
        if start < self.lower.timestamp:
            raise OutofTimeBoundsError(
                "Start timestamp is before the track's first timestamp."
            )
        if end > self.upper.timestamp:
            raise OutofTimeBoundsError(
                "End timestamp is after the track's last timestamp."
            )
        
        # Convert interval from seconds to milliseconds
        #interval = interval * 1000

        # Create a list of timestamps between the start and end
        # timestamps, with the given interval
        timestamps = np.arange(start, end, interval)

        # Return the observed values from the splines
        # at the given timestamps
        # Returns a Nx7 array:
        # [northing, easting, COG, SOG, ROT, dROT, timestamp]
        preds: np.ndarray = np.array([
            self.interpolation.northing(timestamps),
            self.interpolation.easting(timestamps),
            # Take the modulo 360 of the COG to get the
            # heading to be between 0 and 360 degrees
            self.interpolation.COG(timestamps) % 360,
            self.interpolation.SOG(timestamps),
            self.interpolation.ROT(timestamps),
            self.interpolation.dROT(timestamps),
            timestamps
        ])
        return preds.T


    def fill_rot(self) -> None:
        """
        Fill out missing rotation data and first 
        derivative of roatation by inferring it from 
        the previous and next AIS messages' headings.
        """
        for idx, msg in enumerate(self.track):
            if idx == 0:
                continue
            # Fill out missing ROT data
            if msg.ROT is None:
                num = self.track[idx].COG - self.track[idx-1].COG 
                den = (self.track[idx].timestamp - self.track[idx-1].timestamp).seconds*60
                if den == 0:
                    msg.ROT = 0.0
                else:
                    self.track[idx].ROT = num/den

        for idx, msg in enumerate(self.track):
            if idx == 0 or idx == len(self.track)-1:
                continue
            # Calculate first derivative of ROT
            num = self.track[idx+1].ROT - self.track[idx].ROT
            den = (self.track[idx+1].timestamp - self.track[idx].timestamp).seconds*60
            if den == 0:
                msg.ROT = 0.0
            else:
                self.track[idx].dROT = num/den
    
    def find_shell(self) -> None:
        """
        Find the two AIS messages encompassing
        the objects' track elements and save them
        as attributes.
        """ 
        self.lower = self.track[0]
        self.upper = self.track[-1]

    def ts_to_unix(self) -> None:
        """
        Convert the vessel's timestamp for 
        each track element to unix time.
        """
        for msg in self.track:
            msg.timestamp = msg.timestamp.timestamp()

class TrajectoryMatcher:
    """
    Class for matching trajectories of two vessels
    """

    def __init__(self, vessel1: TargetVessel, vessel2: TargetVessel) -> None:
        self.vessel1 = vessel1
        self.vessel2 = vessel2
        if self._disjoint():
            self.disjoint_trajectories = True
        else:
            self._start()
            self._end()
            self.disjoint_trajectories = False
    
    def _start(self) -> None:
        """
        Find the starting point included in both trajectories
        """
        if self.vessel1.lower.timestamp < self.vessel2.lower.timestamp:
            self.start = self.vessel2.lower.timestamp
        else:
            self.start = self.vessel1.lower.timestamp

    def _end(self) -> None:
        """
        Find the end point included in both trajectories
        """
        if self.vessel1.upper.timestamp > self.vessel2.upper.timestamp:
            self.end = self.vessel2.upper.timestamp
        else:
            self.end = self.vessel1.upper.timestamp
    
    def _disjoint(self) -> bool:
        """
        Check if the trajectories are disjoint on the time axis
        """
        return (
            (self.vessel1.upper.timestamp < self.vessel2.lower.timestamp) or
            (self.vessel2.upper.timestamp < self.vessel1.lower.timestamp)
        )

    def observe_interval(self,interval: int) -> TrajectoryMatcher:
        """
        Retruns the trajectories of both vessels
        between the start and end points, with the
        given interval in [seconds].
        """
        if self.disjoint_trajectories:
            raise ValueError(
                "Trajectories are disjoint on the time scale."
            )
        
        obs_vessel1 = self.vessel1.observe_interval(
            self.start, self.end, interval
        )
        obs_vessel2 = self.vessel2.observe_interval(
            self.start, self.end, interval
        )

        self.obs_vessel1 = obs_vessel1
        self.obs_vessel2 = obs_vessel2
        
        return self

    def plot(self, every: int = 10, path: str = None) -> None:
        """
        Plot the trajectories of both vessels
        between the start and end points.
        """
        
        n = every
        v1color = "#d90429"
        v2color = "#2b2d42"

        # Check if obs_vessel1 and obs_vessel2 are defined
        try:
            obs_vessel1 = self.obs_vessel1
            obs_vessel2 = self.obs_vessel2
        except AttributeError:
            raise AttributeError(
                "Nothing to plot. "
                "Please run observe_interval() before plotting."
            )

        # Plot trajectories and metrics
        fig = plt.figure(layout="constrained",figsize=(16,10))
        gs = GridSpec(4, 2, figure=fig)
        
        ax1 = fig.add_subplot(gs[0:2, 0])
        ax2 = fig.add_subplot(gs[0:2, 1])
        ax3 = fig.add_subplot(gs[2:, 0])
        ax4 = fig.add_subplot(gs[2, 1])
        ax5 = fig.add_subplot(gs[3, 1])

        # Custom xticks for time
        time_tick_locs = obs_vessel1[:,6][::10]
        # Make list of HH:MM for each unix timestamp
        time_tick_labels = [datetime.fromtimestamp(t).strftime('%H:%M') for t in time_tick_locs]

        # Plot trajectories in easting-northing space
        v1p = ax1.plot(obs_vessel1[:,1], obs_vessel1[:,0],color = v1color)[0]
        v2p = ax1.plot(obs_vessel2[:,1], obs_vessel2[:,0],color=v2color)[0]
        v1s = ax1.scatter(obs_vessel1[:,1][::n], obs_vessel1[:,0][::n],color = v1color)
        v2s = ax1.scatter(obs_vessel2[:,1][::n], obs_vessel2[:,0][::n],color=v2color)
        ax1.set_title("Trajectories")
        ax1.set_xlabel("Easting [m]")
        ax1.set_ylabel("Northing [m]")
        ax1.legend(
            [(v1p,v1s),(v2p,v2s)],
            [f"Vessel {self.vessel1.mmsi}", f"Vessel {self.vessel2.mmsi}"]
        )

        # Plot easting in time-space
        v1ep = ax2.plot(obs_vessel1[:,6],obs_vessel1[:,1],color = v1color)[0]
        v1es = ax2.scatter(obs_vessel1[:,6][::n],obs_vessel1[:,1][::n],color=v1color)
        v2ep = ax2.plot(obs_vessel2[:,6],obs_vessel2[:,1],color = v2color)[0]
        v2es = ax2.scatter(obs_vessel2[:,6][::n],obs_vessel2[:,1][::n],color=v2color)

        # Original trajectories for both vessels
        v1esp = ax2.scatter(
            [m.timestamp for m in self.vessel1.track],
            [m.easting for m in self.vessel1.track],color = v1color,marker="x"
        )
        v2esp = ax2.scatter(
            [m.timestamp for m in self.vessel2.track],
            [m.easting for m in self.vessel2.track],color=v2color,marker="x"
        )
        ax2.set_xticks(time_tick_locs)
        ax2.set_xticklabels(time_tick_labels, rotation=45)
        
        ax2.set_title("Easting")
        ax2.set_xlabel("Timetamp [ms]")
        ax2.set_ylabel("Easting [m]")
        ax2.legend(
            [(v1ep,v1es),(v2ep,v2es),(v1esp),(v2esp)],
            [
                f"Vessel {self.vessel1.mmsi}", 
                f"Vessel {self.vessel2.mmsi}",
                f"Vessel {self.vessel1.mmsi} raw data", 
                f"Vessel {self.vessel2.mmsi} raw data"
            ]
        )

        # Plot northing in time-space
        v1np = ax3.plot(obs_vessel1[:,6],obs_vessel1[:,0],color=v1color)[0]
        v1ns = ax3.scatter(obs_vessel1[:,6][::n],obs_vessel1[:,0][::n],color=v1color)
        v2np = ax3.plot(obs_vessel2[:,6],obs_vessel2[:,0],color=v2color)[0]
        v2ns = ax3.scatter(obs_vessel2[:,6][::n],obs_vessel2[:,0][::n],color=v2color)

        # Original trajectories for both vessels
        v1nsp = ax3.scatter(
            [m.timestamp for m in self.vessel1.track],
            [m.northing for m in self.vessel1.track],color=v1color,marker="x"
        )
        v2nsp = ax3.scatter(
            [m.timestamp for m in self.vessel2.track],
            [m.northing for m in self.vessel2.track],color=v2color,marker="x"
        )
        ax3.set_xticks(time_tick_locs)
        ax3.set_xticklabels(time_tick_labels, rotation=45)
        
        ax3.set_title("Nothing")
        ax3.set_xlabel("Timetamp [ms]")
        ax3.set_ylabel("Nothing [m]")
        ax3.legend(
            [(v1np,v1ns),(v2np,v2ns),(v1nsp),(v2nsp)],
            [
                f"Vessel {self.vessel1.mmsi}", 
                f"Vessel {self.vessel2.mmsi}",
                f"Vessel {self.vessel1.mmsi} raw data",
                f"Vessel {self.vessel2.mmsi} raw data"
            ]
        )
        
        # Plot COG in time-space
        v1cp = ax4.plot(obs_vessel1[:,6],obs_vessel1[:,2],color=v1color)[0]
        v1cs = ax4.scatter(obs_vessel1[:,6][::n],obs_vessel1[:,2][::n],color=v1color)
        v2cp = ax4.plot(obs_vessel2[:,6],obs_vessel2[:,2],color=v2color)[0]
        v2cs = ax4.scatter(obs_vessel2[:,6][::n],obs_vessel2[:,2][::n],color=v2color)

        # Original trajectories for both vessels
        v1csp = ax4.scatter(
            [m.timestamp for m in self.vessel1.track],
            [m.COG for m in self.vessel1.track],color=v1color,marker="x"
        )
        v2csp = ax4.scatter(
            [m.timestamp for m in self.vessel2.track],
            [m.COG for m in self.vessel2.track],color=v2color,marker="x"
        )
        ax4.set_xticks(time_tick_locs)
        ax4.set_xticklabels(time_tick_labels, rotation=45)

        ax4.set_title("Course over Ground")
        ax4.set_xlabel("Timetamp [ms]")
        ax4.set_ylabel("Course over Ground [deg]")
        ax4.legend(
            [(v1cp,v1cs),(v2cp,v2cs),(v1csp),(v2csp)],
            [
                f"Vessel {self.vessel1.mmsi}", 
                f"Vessel {self.vessel2.mmsi}",
                f"Vessel {self.vessel1.mmsi} raw data",
                f"Vessel {self.vessel2.mmsi} raw data"
            ]
        )
        
        # Plot SOG in time-space
        v1sp = ax5.plot(obs_vessel1[:,6],obs_vessel1[:,3],color=v1color)[0]
        v1ss = ax5.scatter(obs_vessel1[:,6][::n],obs_vessel1[:,3][::n],color=v1color)
        v2sp = ax5.plot(obs_vessel2[:,6],obs_vessel2[:,3],color=v2color)[0]
        v2ss = ax5.scatter(obs_vessel2[:,6][::n],obs_vessel2[:,3][::n],color=v2color)

        # Original trajectories for both vessels
        v1ssp = ax5.scatter(
            [m.timestamp for m in self.vessel1.track],
            [m.SOG for m in self.vessel1.track],color=v1color,marker="x"
        )
        v2ssp = ax5.scatter(
            [m.timestamp for m in self.vessel2.track],
            [m.SOG for m in self.vessel2.track],color=v2color,marker="x"
        )
        ax5.set_xticks(time_tick_locs)
        ax5.set_xticklabels(time_tick_labels, rotation=45)

        ax5.set_title("Speed over Ground")
        ax5.set_xlabel("Timetamp [ms]")
        ax5.set_ylabel("Speed over Ground [knots]")
        ax5.legend(
            [(v1sp,v1ss),(v2sp,v2ss),(v1ssp),(v2ssp)],
            [
                f"Vessel {self.vessel1.mmsi}", 
                f"Vessel {self.vessel2.mmsi}",
                f"Vessel {self.vessel1.mmsi} raw data",
                f"Vessel {self.vessel2.mmsi} raw data"
            ]
        )
        
        plt.suptitle("Trajectories")
        plt.tight_layout()

        if path is None:
            # Make directory if it does not exist
            if not os.path.exists("~/aisout/plots"):
                os.makedirs("~/aisout/plots")
            savepath = f"~/aisout/plots/trajectories_{self.vessel1.mmsi}_{self.vessel2.mmsi}.png"
        else:
            savepath = path
        plt.savefig(
            f"{savepath}/trajectories_{self.vessel1.mmsi}_{self.vessel2.mmsi}.png",
            dpi=300
        )
        plt.close()
        


def _dtr(angle: float) -> float:
    """Transform from [0,360] to 
    [-180,180] in radians"""
    o = ((angle-180)%360)-180
    return o/180*np.pi

def _dtr2(angle: float) -> float:
    """Transform from [0,360] to 
    [-180,180] in radians and switch
    angle rotation order since python
    rotates counter clockwise while
    heading is provided clockwise"""
    o = ((angle-180)%360)-180
    return -o/180*np.pi
