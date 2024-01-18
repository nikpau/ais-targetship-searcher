from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, List, Union
from more_itertools import pairwise
import multiprocessing as mp
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from ..logger import logger
from ..structs import (
    BoundingBox, TimePosition,
    UNIX_TIMESTAMP
)
from . import split
from ..decode.filedescriptor import (
    BaseColumns, Msg12318Columns, Msg5Columns
)
from .targetship import TargetShip, AISMessage, InterpolationError
from ..utils import DataLoader

# Exceptions
class FileLoadingError(Exception):
    pass

# Type aliases
MMSI = int
Targets = dict[MMSI,TargetShip]

def _identity(x):
    return x


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
    
    def __init__(self, 
                 dynamic_paths: Union[Path,List[Path]],
                 frame: BoundingBox,
                 static_paths: Union[Path,List[Path]],
                 max_tgt_ships: int = 200,
                 preprocessor: Callable[[pd.DataFrame],pd.DataFrame] = _identity,
                 decoded: bool = True,
                 high_accuracy: bool = False) -> None:
       
        """ 
        frame: BoundingBox object setting the search space
                for the taget ship extraction process.
                AIS records outside this BoundingBox are 
                not eligible for TargetShip construction.
        dynamic_paths: path or list of paths to a csv file 
                       containing AIS messages of type 1,2,3 and 18.
        static_paths: path or list of paths to a csv file 
                      containing AIS messages of type 5.
        max_tgt_ships: maximum number of target ships to retrun

        preproceccor: Preprocessor function for input data. 
                Defaults to the identity.
        decoded: boolean flag indicating whether the input data
                is already decoded or not. If `decoded` is False,
                the input data is decoded on the fly. Please note
                that this process is magnitudes slower than using
                decoded data.
        high_accuracy: boolean flag indicating whether Vincenty's
                formula should be used for distance calculations.
                If `high_accuracy` is False, the Haversine formula
                is used. Defaults to False.
        """

        if not isinstance(dynamic_paths,list):
            dynamic_paths = [dynamic_paths]
            dynamic_paths = [Path(p) for p in dynamic_paths]
        else:
            dynamic_paths = [Path(p) for p in dynamic_paths]

        if not isinstance(static_paths,list):
            static_paths = [static_paths]
            static_paths = [Path(p) for p in static_paths]
        else:
            static_paths = static_paths
            static_paths = [Path(p) for p in static_paths]
            
        self.loader = DataLoader(
            dynamic_paths,
            static_paths,
            preprocessor
        )

        # Spatial bounding box of current AIS message space
        self.FRAME = frame
        
        self.high_accuracy = high_accuracy
        
        self.spatial_filter = (
            f"{Msg12318Columns.LON.value} > {frame.LONMIN} and "
            f"{Msg12318Columns.LON.value} < {frame.LONMAX} and "
            f"{Msg12318Columns.LAT.value} > {frame.LATMIN} and "
            f"{Msg12318Columns.LAT.value} < {frame.LATMAX}"
        )

        # Maximum number of target ships to extract
        self.max_tgt_ships = max_tgt_ships
        
        # Maximum temporal deviation of target
        # ships from provided time in `init()`.
        # This is used internally to catch
        # all messages around the queried time,
        # for building and interpolating trajectories.
        self.time_delta = 30 # in minutes

        # List of cell-indicies of all 
        # currently buffered cells
        self._buffered_cell_idx = []
        
        # Flag indicating whether the input data
        # is already decoded or not
        self.decoded = decoded
        
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
        
        # Indicator whether the input data is in memory
        self._is_bufferd = False
        
    def get_ships(self, 
                  tpos: TimePosition, 
                  search_radius: float = 20, # in nautical miles
                  interpolation: str = "linear") -> Targets:
        """
        Returns a list of target ships
        present in the neighborhood of the given position. 
        
        tpos: TimePosition object of agent for which 
                neighbors shall be found
        
        search_radius: Radius around agent in which taget vessel 
                search is rolled out [nm]

        overlap_tpos: boolean flag indicating whether
                we only want to return vessels, whose track overlaps
                with the queried timestamp. If `overlap_tpos` is False,
                all vessels whose track is within the time delta of the
                queried timestamp are returned.
        """
        assert interpolation in ["linear","spline","auto"], \
            "Interpolation method must be either 'linear', 'spline' or 'auto'"
        if not self._is_bufferd:
            self._buffer_data()
        # Get neighbors
        neigbors = self._get_neighbors(tpos,search_radius)
        tgts = self.construct_target_vessels(neigbors,tpos,True,njobs=1)
        # Contruct Splines for all target ships
        tgts = self._interpolate_trajectories(tgts,mode=interpolation)
        return tgts
    
    def _buffer_data(self) -> None:
        """
        Load all AIS messages into memory.
        """
        self.loader.load()
        self._is_bufferd = True
    
    def get_all_ships(self,
                      njobs: int = 4, 
                      skip_tsplit: bool = False) -> Targets:
        """
        Returns a dictionary of all target ships in the
        frame of the search agent.
        """
        assert njobs > 1, \
            ("Single process mode requires `tpos` and `neighbors` to be set.\n"
            "For live vessel tracking use the get_ships() method.")
        return self.construct_target_vessels(
            None,None,False,njobs,skip_tsplit
        )
    
    def _interpolate_trajectories(self, 
                           tgts: Targets,
                           mode: str = "auto") -> Targets:
        """
        Interpolate all target ships' trajectories.
        """
        for mmsi,tgt in list(tgts.items()):
            try:
                tgt.interpolate(mode) # Construct splines
            except InterpolationError as e:
                logger.warn(e)
                del tgts[mmsi]
        return tgts
    
    def _build_kd_tree(self, data: pd.DataFrame) -> cKDTree:
        """
        Build a kd-tree object from the `Lat` and `Lon` 
        columns of a pandas dataframe.
        """
        assert Msg12318Columns.LAT.value in data and Msg12318Columns.LON.value in data, \
            "Input dataframe has no `lat` or `lon` columns"
        lat=data[Msg12318Columns.LAT.value].values
        lon=data[Msg12318Columns.LON.value].values
        return cKDTree(np.column_stack((lat,lon)))
            
        
    def _get_ship_type(self, 
                       static_msgs: pd.DataFrame,
                       mmsi: int) -> list[int]:
        """
        Return the ship type of a given MMSI number.

        If more than one ship type is found, the first
        one is returned and a warning is logged.
        """
        st = static_msgs[static_msgs[Msg5Columns.MMSI.value] == mmsi]\
            [Msg5Columns.SHIPTYPE.value].values
        st:np.ndarray = np.unique(st)
        if st.size > 1:
            logger.warning(
                f"More than one ship type found for MMSI {mmsi}. "
                f"Found {st}.")
            return st
        else:
            return list(st)

    def _get_ship_length(self, 
                         static_msgs: pd.DataFrame,
                         mmsi: int) -> int:
        """
        Return the ship length of a given MMSI number.

        If more than one ship length is found, the largest
        one is returned and a warning is logged.
        """
        raw = static_msgs[static_msgs[Msg5Columns.MMSI.value] == mmsi]\
            [[Msg5Columns.TO_BOW.value,Msg5Columns.TO_STERN.value]].values
        sl:np.ndarray = np.sum(raw,axis=1)
        sl = np.unique(sl)
        if sl.size > 1:
            logger.warning(
                f"More than one ship length found for MMSI {mmsi}. "
                f"Found {sl}. Returning {max(sl)}.")
            return max(sl)
        return sl

    def _get_neighbors(self, 
                       tpos: TimePosition, 
                       search_radius: float = 20) -> pd.DataFrame:
        """
        Return all AIS messages that are no more than
        `self.search_radius` [nm] away from the given position.

        Args:
            tpos: TimePosition object of postion and time for which 
                    neighbors shall be found        

        """
        filtered = self._time_filter(
            self.loader.dynamic_data,tpos.timestamp,self.time_delta
        )
        # Check if filterd result is empty
        if filtered.empty:
            logger.warning("No AIS messages found in time-filtered cell.")
            return filtered
        tree = self._build_kd_tree(filtered)
        # The conversion to degrees is only accurate at the equator.
        # Everywhere else, the distances get smaller as lines of 
        # Longitude are not parallel. Therefore, 
        # this is a conservative estimate.
        if not search_radius == np.inf:
            sr =  search_radius/60 # Convert to degrees
        else:
            sr = np.inf
        d, indices = tree.query(
            tpos.position.as_list,
            k=self.max_tgt_ships,
            distance_upper_bound=sr
        )
        # Sort out any infinite distances
        res = [indices[i] for i,j in enumerate(d) if j != float("inf")]

        return filtered.iloc[res]
    
    def _distribute(self, df: pd.DataFrame) -> list[pd.DataFrame]:
        """
        Distribute a given dataframe into smaller
        dataframes, each containing only messages
        from a single MMSI.
        """
        mmsis = df[Msg12318Columns.MMSI.value].unique()
        return [df[df[Msg12318Columns.MMSI.value] == mmsi] for mmsi in mmsis]
    
    def _impl_construct_target_vessel(self, 
                                      dyn: pd.DataFrame,
                                      stat: pd.DataFrame) -> Targets:
        """
        Construct a single TargetVessel object from a given dataframe.
        """
        MMSI = int(dyn[Msg12318Columns.MMSI.value].iloc[0])
        # Initialize TargetVessel object
        tv =  TargetShip(
            ts = None,
            mmsi=MMSI,
            ship_type=self._get_ship_type(stat,MMSI),
            length=self._get_ship_length(stat,MMSI),
            tracks=[[]]
        )
        first = True
                
        dyn = dyn.sort_values(by=BaseColumns.TIMESTAMP.value)
        
        for ts,lat,lon,sog,cog in zip(
            dyn[BaseColumns.TIMESTAMP.value], dyn[Msg12318Columns.LAT.value],  
            dyn[Msg12318Columns.LON.value],       dyn[Msg12318Columns.SPEED.value],
            dyn[Msg12318Columns.COURSE.value]):
            ts: pd.Timestamp # make type hinting happy
            
            msg = AISMessage(
                sender=MMSI,
                timestamp=int(ts.timestamp()), # Convert to unix
                lat=lat,lon=lon,
                COG=cog,SOG=sog
            )
            if first:
                tv.tracks[-1].append(msg)
                first = False
            else:
                # Check if message is a duplicate, i.e. had been 
                # received by multiple AIS stations
                if msg.timestamp == tv.tracks[-1][-1].timestamp:
                    continue
                tv.tracks[-1].append(msg)

        return {MMSI:tv}
    
    def _mp_construct_target_vessels(self,
                                     njobs: int = 4,
                                     skip_tsplit: bool = False) -> Targets:
        """
        Adaption from `_construct_target_vessels` with multiprocessing.
        The initial dataframe is split into smaller dataframes, each
        containing only messages from a single MMSI. These dataframes
        are then processed in parallel and the results are merged.
        """
        singles: list[Targets] = []
        with mp.Pool(njobs) as pool:
            for dyn, stat in self.loader.iterate_chunks():
                single_frames = self._distribute(dyn)
                res = pool.starmap(
                    self._impl_construct_target_vessel,
                    [(dframe,stat) for dframe in single_frames]
                )
                singles.extend(res)
        targets = self._join_tracks(*singles)
        if not skip_tsplit:
            targets = self._determine_split_points(targets)
        else: # Sort by timestamp
            targets = self._sort_by_timestamp(targets)
        return targets
        
    def _rm_dups(self, targets: Targets) -> Targets:
        """
        Remove duplicate timestamps from the tracks
        of the given target ships and return the
        time-sorted tracks.
        """
        for tgt in targets.values():
            tgt.tracks = [
                list(set(track)).sort(key=lambda x: x.timestamp) 
                for track in tgt.tracks
            ]
        return targets
    
    def _join_tracks(self,
                    *singles: Targets) -> Targets:
        """
        Join tracks of multiple TargetShip objects
        into a single TargetShip object.
        
        Note: The input TargetShip objects are only 
        consisting of a single track each, as they
        just have been constructed.
        """
        targets: Targets = {}
        for single in singles:
            for mmsi,tgt in single.items():
                if mmsi not in targets:
                    targets[mmsi] = tgt
                else:
                    # We check again if the first message of the
                    # second track is a duplicate of the last message
                    # of the first track. This can happen if the 
                    # duplicate message was exactly cut off by 
                    # the chunking process, and thus was not removed
                    # by the `_impl_construct_target_vessel` method.
                    if targets[mmsi].tracks[0][-1].timestamp == tgt.tracks[0][0].timestamp:
                        tgt.tracks[0].pop(0)
                    targets[mmsi].tracks[0].extend(tgt.tracks[0])
        return targets
                    
    def _determine_split_points(self,
                                targets: Targets) -> Targets:
        """
        Determine split points for all target ships.
        """
        nvessels = len(targets)
        ctr = 0
        for tgt in list(targets.values()):
            logger.info(
                f"Processing target ship {tgt.mmsi} "
                f"({ctr+1}/{nvessels})"
            )
            ctr += 1
            for track in tgt.tracks:
                track.sort(key=lambda x: x.timestamp)
                _itracks = [] # Intermediary track
                tstartidx = 0
                for i, (msg_t0,msg_t1) in enumerate(pairwise(track)):
                    if msg_t0.timestamp == msg_t1.timestamp:
                        track.remove(msg_t1)
                        continue
                    if split.is_split_point(msg_t0,msg_t1):
                        _itracks.append(track[tstartidx:i+1])
                        tstartidx = i+1
            # Only keep tracks with more than one observation
            tgt.tracks = [track for track in _itracks if len(track) > 1]
            # If no tracks are left, remove target ship
            if not tgt.tracks:
                logger.warning(
                    f"Target ship {tgt.mmsi} has no tracks left after filtering."
                )
                del targets[tgt.mmsi]
        logger.info("Splitting done.")
        return targets
                        
    def _sp_construct_target_vessels(self,
                                     neighbors: pd.DataFrame,
                                     tpos: TimePosition,
                                     overlap: bool) -> Targets:
        """
        Single-process version of `_construct_target_vessels`.
        
        Walk through the rows of `df` and construct a 
        `TargetVessel` object for every unique MMSI. 
        
        The individual AIS Messages are sorted by date
        and are added to the respective TargetVessel's track attribute.
        
        The `overlap` flag indicates whether we only want to return vessels,
        whose track overlaps with the queried timestamp. If `overlap` is False,
        all vessels whose track is within the time delta of the
        queried timestamp are returned.
        
        If the temporal or spatial difference between two AIS Messages is too large,
        the track of the target ship is split into two tracks.
        
        """
        targets: Targets = {}
        neighbors = neighbors.sort_values(by=BaseColumns.TIMESTAMP.value)
        
        for mmsi,ts,lat,lon,sog,cog in zip(
            neighbors[Msg12318Columns.MMSI.value], neighbors[BaseColumns.TIMESTAMP.value],
            neighbors[Msg12318Columns.LAT.value],  neighbors[Msg12318Columns.LON.value],
            neighbors[Msg12318Columns.SPEED.value],neighbors[Msg12318Columns.COURSE.value]):
            ts: pd.Timestamp # make type hinting happy
            
            msg = AISMessage(
                sender=mmsi,
                timestamp=ts.to_pydatetime().timestamp(), # Convert to unix
                lat=lat,lon=lon,
                COG=cog,SOG=sog
            )
            
            if mmsi not in targets:
                targets[mmsi] = TargetShip(
                    ts = tpos.timestamp if tpos is not None else None,
                    mmsi=mmsi,
                    ship_type=self._get_ship_type(self.loader.static_data,mmsi),
                    length=self._get_ship_length(self.loader.static_data,mmsi),
                    tracks=[[msg]]
                )
            else:
                # Split track if change in speed or heading is too large
                if split.is_split_point(targets[mmsi].tracks[-1][-1],msg):
                    targets[mmsi].tracks.append([])
                v = targets[mmsi]
                v.tracks[-1].append(msg)

        for tgt in targets.values():
            tgt.find_shell() # Find shell (start/end of traj) of target ship
        
        # Remove target ships with only one observation
        self._remove_single_obs(targets)
        
        if overlap:
            self._filter_overlapping(targets,tpos)
            
        return targets
    
    def construct_target_vessels(self,
                                 neighbors: pd.DataFrame = None,
                                 tpos: TimePosition = None, 
                                 overlap: bool = False, 
                                 njobs: int = 4,
                                 skip_tsplit: bool = False) -> Targets:
        """
        Construct a dictionary of TargetVessel objects
        from a given dataframe.
        """
        if njobs == 1:
            if tpos is None or neighbors is  None:
                raise ValueError(
                    "Single process mode requires `tpos` and `neighbors` to be set.\n"
                    "For extracting all target ships, use `njobs > 1`.")
            return self._sp_construct_target_vessels(neighbors,tpos,overlap)
        else:
            return self._mp_construct_target_vessels(njobs,skip_tsplit)
        
    
    def _filter_overlapping(self, 
                            targets: Targets, 
                            tpos: TimePosition) -> None:
        """
        Remove target ships whose track does not overlap
        with the queried timestamp.
        """
        for tgt in targets.values():
            to_keep = []
            for track in tgt.tracks:
                if self._overlaps_search_date(track,tpos):
                    to_keep.append(track)
            tgt.tracks = to_keep

    def _remove_single_obs(self, targets: Targets) -> Targets:
        """
        Remove tracks that only have a single observation.
        """
        for tgt in targets.values():
            to_keep = []
            for track in tgt.tracks:
                if len(track) >= 2:
                    to_keep.append(track)
            tgt.tracks = to_keep
    
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

    
    def _overlaps_search_date(self, 
                              track: list[AISMessage], 
                              tpos: TimePosition) -> bool:
        """
        Return True if the track of the given vessel
        overlaps with the queried timestamp.
        """
        return (track[0].timestamp < tpos.timestamp < track[-1].timestamp)

    def _time_filter(self, 
                     df: pd.DataFrame, 
                     date: UNIX_TIMESTAMP, 
                     delta: int) -> pd.DataFrame:
        """
        Filter a pandas dataframe to only return 
        rows whose `Timestamp` is not more than 
        `delta` minutes apart from imput `date`.
        """
        assert BaseColumns.TIMESTAMP.value in df, "No `timestamp` column found"
        timezone = df[BaseColumns.TIMESTAMP.value].iloc[0].tz.zone # Get timezone
        date = pd.to_datetime(int(date),unit="s").tz_localize(timezone)
        dt = pd.Timedelta(delta, unit="minutes")
        mask = (
            (df[BaseColumns.TIMESTAMP.value] > (date-dt)) & 
            (df[BaseColumns.TIMESTAMP.value] < (date+dt))
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