from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Union
from more_itertools import pairwise
from math import radians, cos, sin, asin, sqrt

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
import utm

from .logger import Loader, logger
from .structs import (
    BoundingBox, Position, TimePosition, 
    Msg12318Columns, Msg5Columns,UTMBoundingBox
)
from .targetship import TargetVessel, AISMessage, InterpolationError

# Exceptions
class FileLoadingError(Exception):
    pass

# Type aliases
MMSI = int
Targets = dict[MMSI,TargetVessel]


def m2nm(m: float) -> float:
    """Convert meters to nautical miles"""
    return m/1852

def nm2m(nm: float) -> float:
    """Convert nautical miles to meters"""
    return nm*1852

def haversine(lon1, lat1, lon2, lat2, miles = True):
    """
    Calculate the great circle distance in kilometers between two points 
    on the earth (specified in decimal degrees)
    """
    # convert decimal degrees to radians 
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])

    # haversine formula 
    dlon = lon2 - lon1 
    dlat = lat2 - lat1 
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a)) 
    r = 3956 if miles else 6371 # Radius of earth in kilometers or miles
    return c * r

class SearchAgent:
    """
    Class searching for target ships
    near a user-provided location.

    To initialize the Agent, you have to specify a 
    datapath, where source AIS Messages are saved as csv files.
    You also have to specify a global frame 
    for the search agent. Outside its borders, no
    search will be commenced.
    """
    
    def __init__(
        self, 
        msg12318file: Union[Path,List[Path]],
        frame: BoundingBox,
        msg5file: Union[Path,List[Path]],
        search_radius: float = 0.5, # in nautical miles
        time_delta: int = 30, # in minutes
        max_tgt_ships: int = 200,
        preprocessor: Callable[[pd.DataFrame],pd.DataFrame] = lambda x: x
        ) -> None:
       
        """ 
        frame: BoundingBox object setting the search space
                for the taget ship extraction process.
                AIS records outside this BoundingBox are 
                not eligible for TargetShip construction.
        msg12318file: path to a csv file containing AIS messages
                    of type 1,2,3 and 18.
        search_radius: Radius around agent in which taget vessel 
                search is rolled out
        n_cells: number of cells into which the spatial extent
                of the AIS messages is divided into.
                (See also the `get_cell` function for more info)  
        max_tgt_ships: maximum number of target ships to store

        preproceccor: Preprocessor function for input data. 
                Defaults to the identity.
        """

        if not isinstance(msg12318file,list):
            self.msg12318files = [msg12318file]
        else:
            self.msg12318files = msg12318file

        if not isinstance(msg5file,list):
            self.msg5files = [msg5file]
        else:
            self.msg5files = msg5file
            
        assert len(self.msg12318files) == len(self.msg5files), \
            "Number of msg12318 files must be equal to number of msg5 files"

        # Spatial bounding box of current AIS message space
        self.FRAME = frame
        
        self.spatial_filter = (
            f"{Msg12318Columns.LON} > {frame.LONMIN} and "
            f"{Msg12318Columns.LON} < {frame.LONMAX} and "
            f"{Msg12318Columns.LAT} > {frame.LATMIN} and "
            f"{Msg12318Columns.LAT} < {frame.LATMAX}"
        )

        # Maximum number of target ships to extract
        self.max_tgt_ships = max_tgt_ships
        
        # Maximum temporal deviation of target
        # ships from provided time in `init()`
        self.time_delta = time_delta # in minutes
        
        # Search radius in [°] around agent
        self.search_radius = search_radius
        
        # Init cell manager
        if isinstance(frame,UTMBoundingBox):
            self._utm = True
            logger.info("UTM mode initialized")
        else:
            self._utm = False
            logger.info("LatLon mode initialized")

        # List of cell-indicies of all 
        # currently buffered cells
        self._buffered_cell_idx = []

        # Custom preprocessor function for input data
        self.preprocessor = preprocessor
        
        # Length of the original AIS message dataframe
        self._n_original = 0
        # Length of the filtered AIS message dataframe
        self._n_filtered = 0
        
        # Number of times, the _speed_correction function
        # was called
        self._n_speed_correction = 0
        
        # Number of times, the _position_correction function
        # was called
        self._n_position_correction = 0
        
        self._is_initialized = False

    def init(self, tpos: TimePosition)-> None:
        """
        tpos: TimePosition object for which 
                TargetShips shall be extracted from 
                AIS records.
        """
        tpos._is_utm = self._utm
        pos = tpos.position
        if not self._is_initialized:
            # Load AIS Messages for specific cell
            self.cell_data = self._load_frame_data(self.FRAME)
            self.msg5_data = self._load_msg5_data(self.FRAME)
            self._is_initialized = True
            pos = f"{pos.lat:.3f}N, {pos.lon:.3f}E" if isinstance(tpos.position,Position)\
                  else f"{pos.easting:.3f}mE, {pos.northing:.3f}mN"
            logger.info(
                "Target Ship search agent initialized at "
                f"{pos}"
            )
        else:
            logger.info("Target Ship search agent already initialized")
            return self 


    def get_ships(self, 
                  tpos: TimePosition, 
                  overlap_tpos: bool = True,
                  all_trajectories = False,
                  interpolation: str = "linear",
                  return_rejected: bool = False) -> List[TargetVessel]:
        """
        Returns a list of target ships
        present in the neighborhood of the given position. 
        
        tpos: TimePosition object of agent for which 
                neighbors shall be found

        overlap_tpos: boolean flag indicating whether
                we only want to return vessels, whose track overlaps
                with the queried timestamp. If `overlap_tpos` is False,
                all vessels whose track is within the time delta of the
                queried timestamp are returned.
        all_trajectories: boolean flag indicating whether
                we want to return all TagestVeessel objects
                without temporal, and spatial filtering.
        """
        assert interpolation in ["linear","spline","auto"], \
            "Interpolation method must be either 'linear', 'spline' or 'auto'"
        # Get neighbors
        if all_trajectories:
            tgts = self._construct_target_vessels(self.cell_data, tpos)
        else:
            neigbors = self._get_neighbors(tpos)
            tgts = self._construct_target_vessels(neigbors, tpos)

        tgts, rejected = self.split(tgts,tpos,overlap_tpos,n=2)
        # Contruct Splines for all target ships
        tgts = self._construct_splines(tgts,mode=interpolation)
        rejected = self._construct_splines(rejected,mode=interpolation)
        return tgts if not return_rejected else (tgts,rejected)
    
    def get_raw_ships(self, 
                  tpos: TimePosition, 
                  all_trajectories = False,
                  max_tgap: int = 180,
                  max_dgap: float = 10) -> Targets:
        """
        Returns a list of all target ships without any
        filtering
        
        `all_trajectories` is a boolean flag indicating whether
        we want to return all TagestVeessel objects or just those
        overlapping with the queried timestamp.
        
        `max_tgap` is the maximum time gap in seconds between two AIS Messages.
        `max_dgap` is the maximum distance gap in nautical miles between two AIS Messages.
        
        
        """
        # Get neighbors
        if all_trajectories:
            tgts = self._construct_target_vessels(
                self.cell_data, tpos,max_tgap,max_dgap)
        else:
            neigbors = self._get_neighbors(tpos)
            tgts = self._construct_target_vessels(
                neigbors, tpos,max_tgap,max_dgap)
        return tgts
    
    def _construct_splines(self, 
                           tgts: Targets,
                           mode: str = "auto") -> Targets:
        """
        Interpolate all target ship tracks
        """
        for mmsi,tgt in list(tgts.items()):
            try:
                tgt.interpolate(mode) # Construct splines
            except InterpolationError as e:
                logger.warn(e)
                del tgts[mmsi]
        return tgts
    
    def _load_msg5_data(self) -> pd.DataFrame:
        """
        Load AIS Messages from a given path or list of paths
        and return only messages that fall inside given `cell`-bounds.
        """
        snippets = []
        for file in self.msg5files:
            logger.info(f"Loading msg5 file '{file}'")
            msg5 = pd.read_csv(
                file,usecols=
                [
                    Msg5Columns.MMSI,
                    Msg5Columns.SHIPTYPE,
                    Msg5Columns.TO_BOW,
                    Msg5Columns.TO_STERN
                ]
            )
            snippets.append(msg5.query(self.spatial_filter))
        msg5 = pd.concat(snippets)
        msg5 = msg5[msg5[Msg5Columns.MMSI].isin(self.cell_data[Msg12318Columns.MMSI])]
        return msg5

    def _load_frame_data(self) -> pd.DataFrame:
        """
        Load AIS Messages from a given path or list of paths
        and return only messages that fall inside given `cell`-bounds.
        """
        snippets = []
        try:
            with Loader(self.FRAME):
                for file in self.msg12318files:
                    msg12318 = pd.read_csv(file,sep=",")
                    self._n_original += len(msg12318)
                    msg12318 = self.preprocessor(msg12318) # Apply custom filter
                    msg12318[Msg12318Columns.TIMESTAMP] = pd.to_datetime(
                        msg12318[Msg12318Columns.TIMESTAMP]).dt.tz_localize(None)
                    msg12318 = msg12318.drop_duplicates(
                        subset=[Msg12318Columns.TIMESTAMP,Msg12318Columns.MMSI], keep="first"
                    )
                    self._n_filtered += len(msg12318)
                    snippets.append(msg12318.query(self.spatial_filter))
        except Exception as e:
            logger.error(f"Error while loading cell data: {e}")
            raise FileLoadingError(e)

        return pd.concat(snippets)
            
    def _build_kd_tree(self, data: pd.DataFrame) -> cKDTree:
        """
        Build a kd-tree object from the `Lat` and `Lon` 
        columns of a pandas dataframe.

        If the object was initialized with a UTMCellManager,
        the data is converted to UTM before building the tree.
        """
        assert Msg12318Columns.LAT in data and Msg12318Columns.LON in data, \
            "Input dataframe has no `lat` or `lon` columns"
        if isinstance(self.FRAME,UTMBoundingBox): 
            eastings, northings, *_ = utm.from_latlon(
                data[Msg12318Columns.LAT].values,
                data[Msg12318Columns.LON].values
                )
            return cKDTree(np.column_stack((northings,eastings)))
        else:
            lat=data[Msg12318Columns.LAT].values
            lon=data[Msg12318Columns.LON].values
            return cKDTree(np.column_stack((lat,lon)))
            
        
    def _get_ship_type(self, mmsi: int) -> int:
        """
        Return the ship type of a given MMSI number.

        If more than one ship type is found, the first
        one is returned and a warning is logged.
        """
        st = self.msg5_data[self.msg5_data[Msg5Columns.MMSI] == mmsi]\
            [Msg5Columns.SHIPTYPE].values
        st:np.ndarray = np.unique(st)
        if st.size > 1:
            logger.warning(
                f"More than one ship type found for MMSI {mmsi}. "
                f"Found {st}. Returning {st[0]}.")
            return st[0]
        return st

    def _get_ship_length(self, mmsi: int) -> int:
        """
        Return the ship length of a given MMSI number.

        If more than one ship length is found, the first
        one is returned and a warning is logged.
        """
        raw = self.msg5_data[self.msg5_data[Msg5Columns.MMSI] == mmsi]\
            [[Msg5Columns.TO_BOW,Msg5Columns.TO_STERN]].values
        sl:np.ndarray = np.sum(raw,axis=1)
        sl = np.unique(sl)
        if sl.size > 1:
            logger.warning(
                f"More than one ship length found for MMSI {mmsi}. "
                f"Found {sl}. Returning {sl[0]}.")
            return sl[0]
        return sl

    def _get_neighbors(self, tpos: TimePosition):
        """
        Return all AIS messages that are no more than
        `self.search_radius` [nm] away from the given position.

        Args:
            tpos: TimePosition object of postion and time for which 
                    neighbors shall be found        

        """
        tpos._is_utm = self._utm
        filtered = self._time_filter(self.cell_data,tpos.timestamp,self.time_delta)
        # Check if filterd result is empty
        if filtered.empty:
            logger.warning("No AIS messages found in time-filtered cell.")
            return filtered
        tree = self._build_kd_tree(filtered)
        # The conversion to degrees is only accurate at the equator.
        # Everywhere else, the distances get smaller as lines of 
        # Longitude are not parallel. Therefore, 
        # this is a conservative estimate.
        if not self.search_radius == np.inf:
            sr = (nm2m(self.search_radius) if self._utm 
                            else self.search_radius/60) # Convert to degrees
        else:
            sr = np.inf
        d, indices = tree.query(
            list(tpos.position),
            k=self.max_tgt_ships,
            distance_upper_bound=sr
        )
        # Sort out any infinite distances
        res = [indices[i] for i,j in enumerate(d) if j != float("inf")]

        return filtered.iloc[res]

    def _construct_target_vessels(
            self, 
            df: pd.DataFrame, 
            tpos: TimePosition,
            max_tgap: int,
            max_dgap: float) -> Targets:
        """
        Walk through the rows of `df` and construct a 
        `TargetVessel` object for every unique MMSI. 
        
        The individual AIS Messages are sorted by date
        and are added to the respective TargetVessel's track attribute.
        
        `max_tgap` is the maximum time gap in seconds between two AIS Messages.
        `max_dgap` is the maximum distance gap in nautical miles between two AIS Messages.
        
        If the time or spatial difference between two AIS Messages is too large,
        the track of the target ship is split into two tracks.
        
        """
        df = df.sort_values(by=Msg12318Columns.TIMESTAMP)
        targets: Targets = {}
        
        for mmsi,ts,lat,lon,sog,cog in zip(
            df[Msg12318Columns.MMSI], df[Msg12318Columns.TIMESTAMP],
            df[Msg12318Columns.LAT],  df[Msg12318Columns.LON],
            df[Msg12318Columns.SPEED],df[Msg12318Columns.COURSE]):

            msg = AISMessage(
                sender=mmsi,
                timestamp=ts.to_pydatetime(),
                lat=lat,lon=lon,
                COG=cog,SOG=sog,
                _utm=self._utm
            )
            
            if mmsi not in targets:
                targets[mmsi] = TargetVessel(
                    ts = tpos.timestamp,
                    mmsi=mmsi,
                    ship_type=self._get_ship_type(mmsi),
                    length=self._get_ship_length(mmsi),
                    tracks=[[msg]]
                )
            else:
                if self._gap_too_large(
                    max_tgap,max_dgap,targets[mmsi].tracks[-1][-1],msg):
                    targets[mmsi].tracks.append([])
                v = targets[mmsi]
                v.tracks[-1].append(msg)

        for tgt in targets.values():
            #tgt.fill_rot() # Calculate missing 'rate of turn' values via COG
            tgt.find_shell() # Find shell (start/end of traj) of target ship
            tgt.ts_to_unix() # Convert timestamps to unix

        return targets#self._corrections(targets)

    def _gap_too_large(self, 
                       tgap: int,
                       dgap: int, 
                       msg_t0: AISMessage, 
                       msg_t1: AISMessage) -> bool:
        """
        Return True if the time and spatial difference between two AIS Messages
        is too large.
        `tgap` is the maximum time gap in seconds.
        `dgap` is the maximum distance gap in nautical miles.
        """
        return (
            (msg_t1.timestamp - msg_t0.timestamp).total_seconds() > tgap or
            haversine(msg_t0.lon,msg_t0.lat,msg_t1.lon,msg_t1.lat) > dgap
                )
    
    def _corrections(self, targets: Targets) -> Targets:
        """
        Perform corrections on the given targets.
        """
        self._speed_correction(targets)
        self._position_correction(targets)
        return targets
    
    def _break_down_velocity(self,
                             speed: float,
                             course: float) -> tuple[float,float]:
        """
        Break down a given velocity into its
        longitudinal and lateral components.
        """
        return (
            speed * np.cos(np.deg2rad(course)), # Longitudinal component
            speed * np.sin(np.deg2rad(course)) # Lateral component
        )
    
    def _speed_correction(self,
                          targets: Targets) -> Targets:
        """
        Speed correction after 10.1016/j.aap.2011.05.022
        """
        for target in targets.values():
            for msg_t0,msg_t1 in pairwise(target.tracks):
                tms = _time_mean_speed(msg_t0,msg_t1)
                lower_bound = msg_t0.SOG - (msg_t1.SOG - msg_t0.SOG)
                upper_bound = msg_t1.SOG + (msg_t1.SOG - msg_t0.SOG)
                if not lower_bound < tms < upper_bound:
                    self._n_speed_correction += 1
                    logger.warn(
                        f"Speed correction for MMSI {target.mmsi} "
                        f"at {msg_t1.timestamp}"
                    )
                    msg_t1.SOG = tms

    def _position_correction(self,
                             targets: Targets) -> Targets:
        """
        Position correction after 10.1016/j.aap.2011.05.022
        """
        for target in targets.values():
            for msg_t0,msg_t1 in pairwise(target.tracks):
                dt = (msg_t1.timestamp - msg_t0.timestamp)
                sog_lon, sog_lat = self._break_down_velocity(msg_t0.SOG,msg_t0.COG)
                lont1 = msg_t0.lon + (sog_lon *dt)
                latt1 = msg_t0.lat + (sog_lat *dt)
                
                est_pos = np.sqrt(
                    (lont1-msg_t1.lon)**2 + (latt1-msg_t1.lat)**2
                )
                if est_pos <= 0.5*(msg_t1.SOG-msg_t0.SOG)*dt:
                    self._n_position_correction += 1
                    logger.warn(
                        f"Position correction for MMSI {target.mmsi} "
                        f"at {msg_t1.timestamp}"
                    )
                    msg_t1.lon = lont1
                    msg_t1.lat = latt1
    

    def split(self, 
            targets: Targets, 
            tpos: TimePosition,
            overlap_tpos: bool = True,
            sd: float = 0.1,
            minlen: int = 100) -> tuple[list[TargetVessel],list[TargetVessel]]:
        """
        
        Also remove vessels whose track lies outside
        the queried timestamp.
        
        `overlap_tpos` is a boolean flag indicating whether
        we only want to return vessels, whose track overlaps
        with the queried timestamp. If `overlap_tpos` is False,
        all vessels whose track is within the time delta of the
        queried timestamp are returned.
        
        """
        rejected: Targets = {}
        accepted: Targets = {}


        nships = len(targets)
        n = 0 # Number of trajectories before split
        for i, (_,target_ship) in enumerate(targets.items()):
            logger.info(f"Filtering target ship {i+1}/{nships}")
            for track in target_ship.tracks:
                n += 1
                # Remove vessels whose track is shorter than `minlen`
                if self._track_length_filter(track,minlen):
                    self._copy_track(target_ship,rejected,track)
                    continue
                
                # Remove vessels whose track has a larger standard deviation
                elif self._track_sd_filter(track,sd):
                    self._copy_track(target_ship,rejected,track)
                    continue
                
                elif overlap_tpos and not self._overlaps_search_date(track,tpos):
                    self._copy_track(target_ship,rejected,track)
                    continue
                else:
                    self._copy_track(target_ship,accepted,track)
            
        # Number of target ships after filtering
        n_rejected = [len(v.tracks) for v in rejected.values()]
        logger.info(
            f"Filtered {n} trajectories. "
            f"{(n_rejected)/n*100:.2f}% rejected."
        )

        return accepted, rejected
    
    def _copy_track(self,
                    vessel: TargetVessel, 
                    target: Targets,
                    track: list[AISMessage]) -> None:
        """
        Copy a track from one TargetVessel object to another,
        and delete it from the original.
        """
        if vessel.mmsi not in target:
            target[vessel.mmsi] = deepcopy(vessel)
            target[vessel.mmsi].tracks = []
        target[vessel.mmsi].tracks.append(track)


    def _merge_tracks(self,
                      t1: TargetVessel,
                      t2: TargetVessel) -> TargetVessel:
        """
        Merge two target vessels into one.
        """
        assert t1.mmsi == t2.mmsi, "Can only merge tracks of same target vessel"
        t1.tracks.extend(t2.tracks)
        return t1

    def _track_length_filter(self, track: list[AISMessage], n: int) -> bool:
        """
        Return True if the length of the track of the given vessel
        is smaller than `n`.
        """
        return len(track) < n
    
    def _track_sd_filter(self, track: list[AISMessage], sd: float) -> bool:
        """
        Return True if the summed standard deviation of lat/lon 
        of the track of the given vessel is smallerw than `sd`.
        Unit of `sd` is [°].
        """
        sdlon = np.sqrt(np.var([v.lon for v in track]))
        sdlat = np.sqrt(np.var([v.lat for v in track]))
        return (sdlon+sdlat) < sd
    
    def _track_span_filter(self, track: list[AISMessage], span: float) -> bool:
        """
        Return True if the lateral and longitudinal span
        of the track of the given vessel is smaller than `span`.
        """
        lat_span = np.ptp([v.lat for v in track])
        lon_span = np.ptp([v.lon for v in track])
        return lat_span > span and lon_span > span
    
    def _overlaps_search_date(self, vessel: TargetVessel, tpos: TimePosition) -> bool:
        """
        Return True if the track of the given vessel
        overlaps with the queried timestamp.
        """
        return (vessel.tracks[0].timestamp < tpos.timestamp < vessel.tracks[-1].timestamp)

    def _time_filter(self, df: pd.DataFrame, date: datetime, delta: int) -> pd.DataFrame:
        """
        Filter a pandas dataframe to only return 
        rows whose `Timestamp` is not more than 
        `delta` minutes apart from imput `date`.
        """
        assert Msg12318Columns.TIMESTAMP in df, "No `timestamp` column found"
        date = pd.Timestamp(date, tz=None)
        dt = pd.Timedelta(delta, unit="minutes")
        mask = (
            (df[Msg12318Columns.TIMESTAMP] > (date-dt)) & 
            (df[Msg12318Columns.TIMESTAMP] < (date+dt))
        )
        return df.loc[mask]
    
def _time_mean_speed(msg_t0: AISMessage,msg_t1: AISMessage) -> float:
    """
    Calculate the time-mean speed between two AIS Messages.
    """
    lat_offset = (msg_t1.lat - msg_t0.lat)**2
    lon_offset = (msg_t1.lon - msg_t0.lon)**2
    time_offset = (msg_t1.timestamp - msg_t0.timestamp) # in seconds
    tms = np.sqrt(lat_offset + lon_offset) / (time_offset / 60 / 60) #[deg/h]
    return tms