import sys
import time

from box import Box
from anytree import RenderTree, LoopError
import shutil
import opengate_core as g4
import os
from pathlib import Path
import itk

from .base import (
    GateObject,
    GateObjectSingleton,
    process_cls,
    find_all_gate_objects,
    find_paths_in_gate_object_dictionary,
)
from .definitions import __world_name__, __gate_list_objects__
from .element import new_element
from .engines import SimulationEngine
from .exception import fatal, warning
from .geometry.materials import MaterialDatabase
from .image import (
    create_image_with_volume_extent,
    create_image_with_extent,
    voxelize_volume,
    update_image_py_to_cpp,
    get_cpp_image,
)
from .utility import (
    assert_unique_element_name,
    g4_units,
    indent,
    read_mac_file_to_commands,
    ensure_directory_exists,
    ensure_filename_is_str,
)
from .logger import INFO, log
from .physics import Region, cut_particle_names
from .userinfo import UserInfo
from .serialization import dump_json, dumps_json, loads_json, load_json
from .processing import dispatch_to_subprocess

from .geometry.volumes import (
    VolumeBase,
    BoxVolume,
    SphereVolume,
    TrapVolume,
    ImageVolume,
    TubsVolume,
    PolyhedraVolume,
    HexagonVolume,
    ConsVolume,
    TrdVolume,
    BooleanVolume,
    RepeatParametrisedVolume,
    ParallelWorldVolume,
    VolumeTreeRoot,
)


def retrieve_g4_physics_constructor_class(g4_physics_constructor_class_name):
    """
    Dynamically create a class with the given PhysicList
    Only possible if the class exist in g4
    """
    # Retrieve the G4VPhysicsConstructor class
    try:
        a = getattr(sys.modules["opengate_core"], g4_physics_constructor_class_name)
        # sanity check:
        assert g4_physics_constructor_class_name == a.__name__
        return a
    except AttributeError:
        s = f"Cannot find the class {g4_physics_constructor_class_name} in opengate_core"
        fatal(s)


def create_modular_physics_list_class(g4_physics_constructor_class_name):
    """
    Create a class (not on object!) which:
    - inherit from g4.G4VModularPhysicsList
    - register a single G4 PhysicsConstructor (inherited from G4VPhysicsConstructor)
    - has the same name as this PhysicsConstructor
    """
    g4_physics_constructor_class = retrieve_g4_physics_constructor_class(
        g4_physics_constructor_class_name
    )
    # create the class with __init__ method
    cls = type(
        g4_physics_constructor_class_name,
        (g4.G4VModularPhysicsList,),
        {
            "g4_physics_constructor_class": g4_physics_constructor_class,
            "__init__": init_method,
        },
    )
    return cls


def init_method(self, verbosity):
    """
    Init method of the dynamically created physics list class.
    - call the init method of the super class (G4VModularPhysicsList)
    - Create and register the physics constructor (G4VPhysicsConstructor)
    """
    g4.G4VModularPhysicsList.__init__(self)
    self.g4_physics_constructor = self.g4_physics_constructor_class(verbosity)
    self.RegisterPhysics(self.g4_physics_constructor)


class FilterManager:
    """
    Manage all the Filters in the simulation
    """

    def __init__(self, simulation):
        self.simulation = simulation
        self.user_info_filters = {}
        self.filters = {}

    def __str__(self):
        v = [v.name for v in self.user_info_filters.values()]
        s = f'{" ".join(v)} ({len(self.user_info_filters)})'
        return s

    def dump(self):
        n = len(self.user_info_filters)
        s = f"Number of filters: {n}"
        for Filter in self.user_info_filters.values():
            if n > 1:
                a = "\n" + "-" * 20
            else:
                a = ""
            a += f"\n {Filter}"
            s += indent(2, a)
        return s

    def get_filter(self, name):
        if name not in self.filters:
            fatal(
                f"The Filter {name} is not in the current "
                f"list of Filters: {self.filters}"
            )
        return self.filters[name]

    def add_filter(self, filter_type, name):
        # check that another element with the same name does not already exist
        assert_unique_element_name(self.filters, name)
        # build it
        a = UserInfo("Filter", filter_type, name)
        # append to the list
        self.user_info_filters[name] = a
        # return the info
        return a

    def initialize(self):
        for ui in self.user_info_filters.values():
            filter = new_element(ui, self.simulation)
            log.debug(f"Filter: initialize [{ui.type_name}] {ui.name}")
            filter.Initialize(ui)
            self.filters[ui.name] = filter


class SourceManager:
    """
    Manage all the sources in the simulation.
    The function prepare_generate_primaries will be called during
    the main run loop to set the current time and source.
    """

    def __init__(self, simulation):
        # Keep a pointer to the current simulation
        self.simulation = simulation
        # List of run times intervals
        self.run_timing_intervals = None
        self.current_run_interval = None
        # List of sources user info
        self.user_info_sources = {}

    def __str__(self):
        """
        str only dump the user info on a single line
        """
        v = [v.name for v in self.user_info_sources.values()]
        s = f'{" ".join(v)} ({len(self.user_info_sources)})'
        return s

    def dump_source_types(self):
        s = f""
        # FIXME: workaround to avoid circular import, will be solved when refactoring sources
        from opengate.sources.builders import source_builders

        for t in source_builders:
            s += f"{t} "
        return s

    def dump_sources(self):
        n = len(self.user_info_sources)
        s = f"Number of sources: {n}"
        for source in self.user_info_sources.values():
            a = f"\n {source}"
            s += indent(2, a)
        return s

    def get_source_info(self, name):
        if name not in self.user_info_sources:
            fatal(
                f"The source {name} is not in the current "
                f"list of sources: {self.user_info_sources}"
            )
        return self.user_info_sources[name]

    """def get_source(self, name):
        n = len(self.g4_thread_source_managers)
        if n > 0:
            gate.exception.warning(f"Cannot get source in multithread mode, use get_source_mt")
            return None
        for source in self.sources:
            if source.user_info.name == name:
                return source.g4_source
        gate.exception.fatal(
            f'The source "{name}" is not in the current '
            f"list of sources: {self.user_info_sources}"
        )

    def get_source_mt(self, name, thread):
        n = len(self.g4_thread_source_managers)
        if n == 0:
            gate.exception.warning(f"Cannot get source in mono-thread mode, use get_source")
            return None
        i = 0
        for source in self.sources:
            if source.user_info.name == name:
                if i == thread:
                    return source.g4_source
                i += 1
        gate.exception.fatal(
            f'The source "{name}" is not in the current '
            f"list of sources: {self.user_info_sources}"
        )"""

    def add_source(self, source_type, name):
        # check that another element with the same name does not already exist
        assert_unique_element_name(self.user_info_sources, name)
        # init the user info
        s = UserInfo("Source", source_type, name)
        # append to the list
        self.user_info_sources[name] = s
        # return the info
        return s


class ActorManager:
    """
    Manage all the actors in the simulation
    """

    def __init__(self, simulation):
        self.simulation = simulation
        self.user_info_actors = {}

    def __str__(self):
        v = [v.name for v in self.user_info_actors.values()]
        s = f'{" ".join(v)} ({len(self.user_info_actors)})'
        return s

    """def __getstate__(self):
        if self.simulation.verbose_getstate:
            gate.exception.warning("Getstate ActorManager")
        # needed to not pickle. Need to reset user_info_actors to avoid to store the actors
        self.user_info_actors = {}
        return self.__dict__"""

    def dump_actors(self):
        n = len(self.user_info_actors)
        s = f"Number of Actors: {n}"
        for actor in self.user_info_actors.values():
            if n > 1:
                a = "\n" + "-" * 20
            else:
                a = ""
            a += f"\n {actor}"
            s += indent(2, a)
        return s

    def dump_actor_types(self):
        s = f""
        # FIXME: workaround to avoid circular import, will be solved when refactoring actors
        from opengate.actors.actorbuilders import actor_builders

        for t in actor_builders:
            s += f"{t} "
        return s

    def get_actor_user_info(self, name):
        if name not in self.user_info_actors:
            fatal(
                f"The actor {name} is not in the current "
                f"list of actors: {self.user_info_actors}"
            )
        return self.user_info_actors[name]

    def add_actor(self, actor_type, name):
        # check that another element with the same name does not already exist
        assert_unique_element_name(self.user_info_actors, name)
        # build it
        a = UserInfo("Actor", actor_type, name)
        # append to the list
        self.user_info_actors[name] = a
        # return the info
        return a


class PhysicsListManager(GateObject):
    # Names of the physics constructors that can be created dynamically
    available_g4_physics_constructors = [
        "G4EmStandardPhysics",
        "G4EmStandardPhysics_option1",
        "G4EmStandardPhysics_option2",
        "G4EmStandardPhysics_option3",
        "G4EmStandardPhysics_option4",
        "G4EmStandardPhysicsGS",
        "G4EmLowEPPhysics",
        "G4EmLivermorePhysics",
        "G4EmLivermorePolarizedPhysics",
        "G4EmPenelopePhysics",
        "G4EmDNAPhysics",
        "G4OpticalPhysics",
    ]

    special_physics_constructor_classes = {}
    special_physics_constructor_classes["G4DecayPhysics"] = g4.G4DecayPhysics
    special_physics_constructor_classes[
        "G4RadioactiveDecayPhysics"
    ] = g4.G4RadioactiveDecayPhysics
    special_physics_constructor_classes["G4OpticalPhysics"] = g4.G4OpticalPhysics
    special_physics_constructor_classes["G4EmDNAPhysics"] = g4.G4EmDNAPhysics

    def __init__(self, physics_manager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.physics_manager = physics_manager
        # declare the attribute here as None;
        # set to dict in create_physics_list_classes()
        self.created_physics_list_classes = None
        self.create_physics_list_classes()

    def __getstate__(self):
        # This is needed because cannot be pickled.
        dict_to_return = dict([(k, v) for k, v in self.__dict__.items()])
        dict_to_return["created_physics_list_classes"] = None
        return dict_to_return

    def __setstate__(self, d):
        self.__dict__ = d
        self.create_physics_list_classes()

    def create_physics_list_classes(self):
        self.created_physics_list_classes = {}
        for g4pc_name in self.available_g4_physics_constructors:
            self.created_physics_list_classes[
                g4pc_name
            ] = create_modular_physics_list_class(g4pc_name)

    def get_physics_list(self, physics_list_name):
        if physics_list_name in self.created_physics_list_classes:
            physics_list = self.created_physics_list_classes[physics_list_name](
                self.physics_manager.simulation.g4_verbose_level
            )
        else:
            g4_factory = g4.G4PhysListFactory()
            if g4_factory.IsReferencePhysList(physics_list_name):
                physics_list = g4_factory.GetReferencePhysList(physics_list_name)
            else:
                s = (
                    f"Cannot find the physic list: {physics_list_name}\n"
                    f"{self.dump_info_physics_lists()}"
                    f"Default is {self.physics_manager.user_info_defaults['physics_list_name']}\n"
                    f"Help : https://geant4-userdoc.web.cern.ch/UsersGuides/PhysicsListGuide/html/physicslistguide.html"
                )
                fatal(s)
        # add special physics constructors
        for (
            spc,
            switch,
        ) in self.physics_manager.special_physics_constructors.items():
            if switch is True:
                try:
                    physics_list.ReplacePhysics(
                        self.special_physics_constructor_classes[spc](
                            self.physics_manager.simulation.g4_verbose_level
                        )
                    )
                except KeyError:
                    fatal(
                        f"Special physics constructor named '{spc}' not found. Available constructors are: {self.special_physics_constructor_classes.keys()}."
                    )
        return physics_list

    def dump_info_physics_lists(self):
        g4_factory = g4.G4PhysListFactory()
        s = (
            "\n**** INFO about GATE physics lists ****\n"
            f"* Known Geant4 lists are: {g4_factory.AvailablePhysLists()}\n"
            f"* With EM options: {g4_factory.AvailablePhysListsEM()[1:]}\n"
            f"* Or the following simple physics lists with a single PhysicsConstructor: \n"
            f"* {self.available_g4_physics_constructors} \n"
            "**** ----------------------------- ****\n\n"
        )
        return s


class PhysicsManager(GateObject):
    """
    Everything related to the physics (lists, cuts, etc.) should be here.
    """

    user_info_defaults = {
        "physics_list_name": (
            "QGSP_BERT_EMV",
            {"doc": "Name of the Geant4 physics list. "},
        ),
        "global_production_cuts": (
            Box([("all", None)] + [(pname, None) for pname in cut_particle_names]),
            {
                "doc": "Dictionary containing the production cuts (range) for gamma, electron, positron, proton. Option 'all' overrides individual cuts."
            },
        ),
        "apply_cuts": (
            True,
            {
                "doc": "Flag to turn of cuts 'on the fly'. Still under development in Gate."
            },
        ),
        "energy_range_min": (
            None,
            {
                "doc": "Minimum energy for secondary particle production. If None, physics list default is used."
            },
        ),
        "energy_range_max": (
            None,
            {
                "doc": "Maximum energy for secondary particle production. If None, physics list default is used."
            },
        ),
        "optical_properties_file": (
            Path(os.path.dirname(__file__)) / "data" / "OpticalProperties.xml",
            {
                "doc": "Path to the xml file containing the optical material properties to be used by G4OpticalPhysics. "
                "Default: file shipped with Gate.",
                "is_input_file": True,
            },
        ),
        "user_limits_particles": (
            Box(
                [
                    ("all", False),
                    ("all_charged", True),
                    ("gamma", False),
                    ("electron", False),
                    ("positron", False),
                    ("proton", False),
                ]
            ),
            {
                "doc": "Switch on (True) or off (False) UserLimits, e.g. step limiter, for individual particles. Default: Step limiter is applied to all charged particles (in accordance with G4 default)."
            },
        ),
        "em_parameters": (
            Box(
                [
                    ("fluo", None),
                    ("auger", None),
                    ("auger_cascade", None),
                    ("pixe", None),
                    ("deexcitation_ignore_cut", None),
                ]
            ),
            {"doc": "Switches on (True) or off (False) Geant4's EM parameters."},
        ),
        "em_switches_world": (
            Box([("deex", None), ("auger", None), ("pixe", None)]),
            {
                "doc": "Switch on/off EM parameters in the world region.",
                "expose_items": False,
            },
        ),
        "special_physics_constructors": (
            Box(
                [
                    (spc, False)
                    for spc in PhysicsListManager.special_physics_constructor_classes
                ]
            ),
            {
                "doc": "Special physics constructors to be added to the physics list, e.g. G4Decay, G4OpticalPhysics. "
            },
        ),
    }

    def __init__(self, simulation, *args, **kwargs):
        super().__init__(name="physics_manager", *args, **kwargs)

        # Keep a pointer to the current simulation
        self.simulation = simulation
        self.physics_list_manager = PhysicsListManager(self, name="PhysicsListManager")

        # dictionary containing all the region objects
        # key=region_name, value=region_object
        self.regions = {}
        # Dictionary to quickly find the region to which a volume is associated.
        # This dictionary is updated by the region's associate_volume method.
        # Do not update manually!
        # key=volume_name, value=region=object
        # NB: It is well-defined because each volume has only one region.
        self.volumes_regions_lut = {}

    def reset(self):
        self.__init__(self.simulation)

    def to_dictionary(self):
        d = super().to_dictionary()
        d["regions"] = dict([(k, v.to_dictionary()) for k, v in self.regions.items()])
        return d

    def from_dictionary(self, d):
        self.reset()
        super().from_dictionary(d)
        for r in d["regions"].values():
            region = self.add_region(r["user_info"]["name"])
            region.from_dictionary(r)

    def __str__(self):
        s = ""
        for k, v in self.user_info.items():
            s += f"{k}: {v}\n"
        return s

    def __getstate__(self):
        if self.simulation.verbose_getstate:
            warning("Getstate PhysicsManager")

        dict_to_return = dict([(k, v) for k, v in self.__dict__.items()])
        dict_to_return["physics_list_manager"] = None
        return dict_to_return

    def __setstate__(self, d):
        self.__dict__ = d
        self.physics_list_manager = PhysicsListManager(self, name="PhysicsListManager")

    def _simulation_engine_closing(self):
        """This function should be called from the simulation engine
        when it is closing to make sure that G4 references are set to None.

        """
        # Region contain references to G4 objects, so they need to close
        for r in self.regions.values():
            r.close()

    def dump_available_physics_lists(self):
        return self.physics_list_manager.dump_info_physics_lists()

    def dump_info_physics_lists(self):
        return self.physics_list_manager.dump_info_physics_lists()

    def dump_production_cuts(self):
        s = "*** Production cuts for World: ***\n"
        for k, v in self.user_info.global_production_cuts.items():
            s += f"{k}: {v}\n"
        if len(self.regions.keys()) > 0:
            s += f"*** Production cuts per regions ***\n"
            for region in self.regions.values():
                s += f"In region {region.name}:\n"
                s += region.dump_production_cuts()
        else:
            s += "*** No cuts per region defined. ***\n"
        return s

    @property
    def enable_decay(self):
        """Properties to quickly enable decay.

        Note that setting enable_decay to False means that the physics list
        default is used, i.e. it does not forcefully remove
        G4DecayPhysics from the physics list.
        """

        switch1 = self.special_physics_constructors["G4DecayPhysics"]
        switch2 = self.special_physics_constructors["G4RadioactiveDecayPhysics"]
        if switch1 is True and switch2 is True:
            return True
        elif switch1 is False and switch2 is False:
            return False
        else:
            fatal(
                f"Inconsistent G4Decay constructors: G4DecayPhysics = {switch1}, G4RadioactiveDecayPhysics = {switch2}."
            )

    @enable_decay.setter
    def enable_decay(self, value):
        self.special_physics_constructors["G4DecayPhysics"] = value
        self.special_physics_constructors["G4RadioactiveDecayPhysics"] = value

    def add_region(self, name):
        if name in self.regions.keys():
            fatal("A region with this name already exists.")
        self.regions[name] = Region(name=name, physics_manager=self)
        return self.regions[name]

    def find_or_create_region(self, volume_name):
        if volume_name not in self.volumes_regions_lut.keys():
            region = self.add_region(volume_name + "_region")
            region.associate_volume(volume_name)
        else:
            region = self.volumes_regions_lut[volume_name]
        return region

    # New name, more specific
    def set_production_cut(self, volume_name, particle_name, value):
        if volume_name == self.simulation.world.name:
            self.global_production_cuts[particle_name] = value
        else:
            region = self.find_or_create_region(volume_name)
            region.production_cuts[particle_name] = value

    # set methods for the user_info parameters
    # logic: every volume with user_infos must be associated
    # with a region. If it does not yet have one, created it.
    # Outlook: These setter methods might be linked to properties
    # implemented in a future version of the Volume class
    def set_max_step_size(self, volume_name, max_step_size):
        region = self.find_or_create_region(volume_name)
        region.user_limits["max_step_size"] = max_step_size

    def set_max_track_length(self, volume_name, max_track_length):
        region = self.find_or_create_region(volume_name)
        region.user_limits["max_track_length"] = max_track_length

    def set_min_ekine(self, volume_name, min_ekine):
        region = self.find_or_create_region(volume_name)
        region.user_limits["min_ekine"] = min_ekine

    def set_max_time(self, volume_name, max_time):
        region = self.find_or_create_region(volume_name)
        region.user_limits["max_time"] = max_time

    def set_min_range(self, volume_name, min_range):
        region = self.find_or_create_region(volume_name)
        region.user_limits["min_range"] = min_range

    def set_user_limits_particles(self, particle_names):
        if not isinstance(particle_names, (list, set, tuple)):
            particle_names = list([particle_names])
        for pn in list(particle_names):
            # try to get current value to check if particle_name is eligible
            try:
                _ = self.user_info.user_limits_particles[pn]
            except KeyError:
                fatal(
                    f"Found unknown particle name '{pn}' in set_user_limits_particles(). Eligible names are "
                    + ", ".join(list(self.user_info.user_limits_particles.keys()))
                    + "."
                )
            self.user_info.user_limits_particles[pn] = True


class VolumeManager(GateObject):
    """
    Store and manage a hierarchical list of geometrical volumes and associated materials.
    This tree will be converted into Geant4 Solid/PhysicalVolume/LogicalVolumes
    """

    volume_types = {
        "BoxVolume": BoxVolume,
        "SphereVolume": SphereVolume,
        "TrapVolume": TrapVolume,
        "ImageVolume": ImageVolume,
        "TubsVolume": TubsVolume,
        "PolyhedraVolume": PolyhedraVolume,
        "HexagonVolume": HexagonVolume,
        "ConsVolume": ConsVolume,
        "TrdVolume": TrdVolume,
        "BooleanVolume": BooleanVolume,
        "RepeatParametrisedVolume": RepeatParametrisedVolume,
    }

    def __init__(self, simulation, *args, **kwargs):
        """
        Class that store geometry description.
        """
        self.simulation = simulation
        # force name to VolumeManager
        kwargs["name"] = "VolumeManager"
        super().__init__(*args, **kwargs)

        self.volume_tree_root = VolumeTreeRoot(
            volume_manager=self
        )  # abstract element used as common root for volume tree
        m = g4_units.m

        # default world volume
        self.volumes = {}
        self.volumes[__world_name__] = BoxVolume(
            volume_manager=self,
            name=__world_name__,
            size=[3 * m, 3 * m, 3 * m],
            material="G4_AIR",
            mother=None,
        )
        # attach the world to the tree root
        self.volumes[__world_name__].parent = self.volume_tree_root

        self.parallel_world_volumes = {}

        self._need_tree_update = True  # flag to store state of volume tree

        # database of materials
        self.material_database = MaterialDatabase()

    def reset(self):
        self.__init__(self.simulation)

    def __str__(self):
        s = "**** Volume manager ****\n"
        if len(self.parallel_world_volumes) > 0:
            s += f"Number of parallel worlds: {len(self.parallel_world_volumes)}\n"
            s += f"Names of the parallel worlds: {self.parallel_world_names}\n"
        s += f"Number of volumes: {len(self.volumes)}\n"
        s += "The volumes are organized in the following hierarchy:\n"
        s += self.dump_volume_tree()
        return s

    def to_dictionary(self):
        d = super().to_dictionary()
        d["volumes"] = dict([(k, v.to_dictionary()) for k, v in self.volumes.items()])
        d["parallel_world_volumes"] = list(self.parallel_world_volumes.keys())
        return d

    def from_dictionary(self, d):
        self.reset()
        super().from_dictionary(d)
        # First create all volumes
        for k, v in d["volumes"].items():
            # the world volume is always created in __init__
            if v["user_info"]["name"] != self.world_volume.name:
                self.add_volume(v["object_type"], name=v["user_info"]["name"])
        # ... then process them to make sure that any reference
        #  to a volume in the volumes dictionary is satisfied
        for k, v in d["volumes"].items():
            self.volumes[k].from_dictionary(v)

    @property
    def world_volume(self):
        return self.volumes[__world_name__]

    @property
    def all_world_volumes(self):
        """List of all world volumes, including the mass world volume."""
        world_volumes = [self.world_volume]
        world_volumes.extend(list(self.parallel_world_volumes.values()))
        return world_volumes

    @property
    def volume_names(self):
        return list(self.volumes.keys())

    @property
    def parallel_world_names(self):
        return list(self.parallel_world_volumes.keys())

    @property
    def all_volume_names(self):
        return self.volume_names + self.parallel_world_names

    def get_volume(self, volume_name):
        try:
            return self.volumes[volume_name]
        except KeyError:
            try:
                return self.parallel_world_volumes[volume_name]
            except KeyError:
                fatal(
                    f"Cannot find volume {volume_name}. "
                    f"Volumes included in this simulation are: {self.volumes.keys()}"
                )

    def update_volume_tree_if_needed(self):
        if self._need_tree_update is True:
            self.update_volume_tree()

    def update_volume_tree(self):
        for v in self.volumes.values():
            if (
                v not in self.parallel_world_volumes.values()
                and v is not self.world_volume
            ):
                try:
                    v._update_node()
                except LoopError:
                    fatal(
                        f"There seems to be a loop in the volume tree involving volume {v.name}."
                    )
        self._need_tree_update = False

    def add_volume(self, volume, name=None):
        if isinstance(volume, str):
            if name is None:
                fatal("You must provide a name for the volume.")
            new_volume = self.create_volume(volume, name)
        elif isinstance(volume, VolumeBase):
            new_volume = volume
        else:
            fatal(
                "You need to either provide a volume type and name, or a volume object."
            )

        if new_volume.name in self.all_volume_names:
            fatal(
                f"The volume name {new_volume.name} already exists. Existing volume names are: {self.volumes.keys()}"
            )
        self.volumes[new_volume.name] = new_volume
        self.volumes[new_volume.name].volume_manager = self
        self._need_tree_update = True
        # return the volume if it has not been passed as input, i.e. it was created here
        if new_volume is not volume:
            return new_volume

    def create_volume(self, volume_type, name):
        # check that another element with the same name does not already exist
        volume_type_variants = [volume_type, volume_type + "Volume"]
        for vt in volume_type_variants:
            if vt in self.volume_types.keys():
                return self.volume_types[vt](name=name)
        fatal(
            f"Unknown volume type {volume_type}. Known types are: {list(self.volume_types.keys())}."
        )

    def add_parallel_world(self, name):
        if name in self.all_volume_names:
            fatal(
                f"Cannot create the parallel world named {name} because it already exists."
            )
        # constructor needs self, i.e. the volume manager
        self.parallel_world_volumes[name] = ParallelWorldVolume(name, self)
        self._need_tree_update = True

    def _simulation_engine_closing(self):
        """
        This function should be called from the simulation engine
        when it is closing to make sure that G4 references are set to None.
        """
        self.material_database = None
        # self.volumes_user_info = None

    def add_material_database(self, filename):
        if filename in self.material_database.filenames:
            fatal(f'Database "{filename}" already exist.')
        self.material_database.read_from_file(filename)

    def find_or_build_material(self, material):
        return self.material_database.FindOrBuildMaterial(material)

    def dump_volumes(self):
        s = f"Number of volumes: {len(self.volumes)}"
        for vol in self.volumes.values():
            s += indent(2, f"\n{vol}")
        return s

    def dump_volume_tree(self):
        self.update_volume_tree_if_needed()
        s = ""
        for pre, _, node in RenderTree(self.volume_tree_root):
            # FIXME: pre should be used directly but cannot be encoded correctly in Windows
            s += len(pre) * " " + f"{node.name}\n"
        return s

    def dump_volume_types(self):
        s = f""
        for vt in self.volume_types:
            s += f"{vt} "
        return s

    def dump_material_database_names(self):
        return list(self.material_database.filenames)


def setter_hook_verbose_level(self, verbose_level):
    log.setLevel(verbose_level)
    return verbose_level


class Simulation(GateObject):
    """
    Main class that store a simulation.
    It contains:
    - a set of global user parameters (SimulationUserInfo)
    - user parameters for Volume, Source, Actors and Filters, Physics
    - a list of g4 commands that will be set to G4 engine after the initialization

    There is NO Geant4 engine here, it is only a set of parameters and options.
    """

    user_info_defaults = {
        "verbose_level": (
            INFO,
            {
                "doc": "Gate pre-run verbosity. Possible values: NONE, INFO, DEBUG.",
                "setter_hook": setter_hook_verbose_level,
            },
        ),
        "running_verbose_level": (0, {"doc": "Gate verbosity during running."}),
        "g4_verbose_level": (
            1,
            # For an unknown reason, when verbose_level == 0, there are some
            # additional print after the G4RunManager destructor. So we default at 1
            {"doc": "Geant4 verbosity."},
        ),
        "g4_verbose": (False, {"doc": "Switch on/off Geant4's verbose output."}),
        "g4_verbose_level_tracking": (
            0,
            {
                "doc": "Activate verbose tracking in Geant4 via G4 command '/tracking/verbose g4_verbose_level_tracking'."
            },
        ),
        "visu": (
            False,
            {
                "doc": "Activate visualization? Note: Use low number of primaries if you activate visualization. Default: False"
            },
        ),
        "visu_type": (
            "qt",
            {
                "doc": "The type of visualization to be used.",
                "available_values": (
                    "qt",
                    "vrml",
                    "gdml",
                    "vrml_file_only",
                    "gdml_file_only",
                ),
            },
        ),
        "visu_filename": (
            None,
            {
                "doc": "Name of the file where visualization output is stored. Only applicable for vrml and gdml.",
                "required_type": Path,
            },
        ),
        "visu_verbose": (
            False,
            {
                "doc": "Should verbose output be generated regarding the visualization?",
            },
        ),
        "visu_commands": (
            read_mac_file_to_commands("default_visu_commands.mac"),
            {
                "doc": "Geant4 commands needed to handle the visualization. ",
            },
        ),
        "visu_commands_vrml": (
            read_mac_file_to_commands("default_visu_commands_vrml.mac"),
            {
                "doc": "Geant4 commands needed to handle the VRML visualization. "
                "Only used for vrml-like visualization types. ",
            },
        ),
        "visu_commands_gdml": (
            read_mac_file_to_commands("default_visu_commands_gdml.mac"),
            {
                "doc": "Geant4 commands needed to handle the GDML visualization. "
                "Only used for vrml-like visualization types. ",
            },
        ),
        "check_volumes_overlap": (
            True,
            {
                "doc": "If true, Gate will also check whether volumes overlap. "
                "Note: Geant4 checks overlaps in any case."
            },
        ),
        "number_of_threads": (
            1,
            {
                "doc": "Number of threads on which the simulation will run. "
                "Geant4's run manager will run in MT mode if more than 1 thread is requested."
                "Requires Geant4 do be compiled with Multithread flag TRUE."
            },
        ),
        "force_multithread_mode": (
            False,
            {
                "doc": "Force Geant4 to run multihthreaded even if 'number_of_threads' = 1."
            },
        ),
        "random_engine": (
            "MixMaxRng",
            {
                "doc": "Name of the Geant4 random engine to be used. "
                "MixMaxRng is recommended for multithreaded applications."
            },
        ),
        "random_seed": (
            "auto",
            {
                "doc": "Random seed to be used by the random engine. "
                "Setting a specific value will make subsequent simulation runs to produce identical results."
            },
        ),
        "run_timing_intervals": (
            [[0 * g4_units.second, 1 * g4_units.second]],
            {
                "doc": "A list of timing intervals provided as 2-element lists of begin and end values"
            },
        ),
        "output_dir": (
            "./output",
            {
                "doc": "Directory to which any output is written, "
                "unless an absolute path is provided for a specific output."
            },
        ),
        "store_json_archive": (
            False,
            {
                "doc": "Automatically store a json file containing all parameters of the simulation after the run? "
                "Default: False"
            },
        ),
        "json_archive_filename": (
            Path("simulation.json"),
            {
                "doc": "Name of the json file containing all parameters of the simulation. "
                "It will be saved in the location specified via the parameter 'output_dir'. "
                "Default filename: simulation.json"
            },
        ),
        "store_input_files": (
            False,
            {"doc": "Store all input files used in the simulation? Default: False"},
        ),
        "g4_commands_before_init": (
            [],
            {
                "doc": "Geant4 commands which will be called before the G4 runmanager has initialized the simulation.",
                "required_type": str,
            },
        ),
        "g4_commands_after_init": (
            [],
            {
                "doc": "Geant4 commands which will be called after the G4 runmanager has initialized the simulation.",
                "required_type": str,
            },
        ),
    }

    def __init__(self, name="simulation"):
        """
        Main members are:
        - managers of volumes, physics, sources, actors and filters
        - the Geant4 objects will be only built during initialisation in SimulationEngine
        """
        super().__init__(name=name)

        # for debug only
        self.verbose_getstate = False
        self.verbose_close = False

        # main managers
        self.volume_manager = VolumeManager(self)
        self.source_manager = SourceManager(self)
        self.actor_manager = ActorManager(self)
        self.physics_manager = PhysicsManager(self)
        self.filter_manager = FilterManager(self)

        # output of the simulation (once run)
        self.output = None

        # hook functions
        self.user_hook_after_init = None
        self.user_hook_after_run = None

    def __str__(self):
        s = (
            f"Simulation name: {self.name} \n"
            f"Geometry       : {self.volume_manager}\n"
            f"Physics        : {self.physics_manager}\n"
            f"Sources        : {self.source_manager}\n"
            f"Actors         : {self.actor_manager}"
        )
        return s

    @property
    def use_multithread(self):
        return self.number_of_threads > 1 or self.force_multithread_mode

    @property
    def world(self):
        return self.volume_manager.world_volume

    def to_dictionary(self):
        d = super().to_dictionary()
        d["volume_manager"] = self.volume_manager.to_dictionary()
        d["physics_manager"] = self.physics_manager.to_dictionary()
        return d

    def from_dictionary(self, d):
        super().from_dictionary(d)
        self.volume_manager.from_dictionary(d["volume_manager"])
        self.physics_manager.from_dictionary(d["physics_manager"])

    def to_json_string(self):
        warning(
            f"******************************************************************************\n"
            f"*   WARNING: Only parts of the simulation can currently be dumped as JSON.   *\n"
            f"******************************************************************************\n"
        )
        return dumps_json(self.to_dictionary())

    def to_json_file(self, directory=None, filename=None):
        warning(
            f"******************************************************************************\n"
            f"*   WARNING: Only parts of the simulation can currently be dumped as JSON.   *\n"
            f"******************************************************************************\n"
        )
        d = self.to_dictionary()
        if filename is None:
            filename = self.json_archive_filename
        directory = self.get_output_path(directory, is_file_or_directory="d")
        with open(directory / filename, "w") as f:
            dump_json(d, f)
        # look for input files in the simulation and copy them if requested
        if self.store_input_files is True:
            self.copy_input_files(directory, dct=d)

    def from_json_string(self, json_string):
        warning(
            f"**********************************************************************************\n"
            f"*   WARNING: Only parts of the simulation can currently be reloaded from JSON.   *\n"
            f"**********************************************************************************\n"
        )
        self.from_dictionary(loads_json(json_string))

    def from_json_file(self, path):
        warning(
            f"**********************************************************************************\n"
            f"*   WARNING: Only parts of the simulation can currently be reloaded from JSON.   *\n"
            f"**********************************************************************************\n"
        )
        with open(path, "r") as f:
            self.from_dictionary(load_json(f))

    def copy_input_files(self, directory=None, dct=None):
        directory = self.get_output_path(directory, is_file_or_directory="d")
        if dct is None:
            dct = self.to_dictionary()
        input_files = []
        for go_dict in find_all_gate_objects(dct):
            input_files.extend(
                [
                    p
                    for p in find_paths_in_gate_object_dictionary(
                        go_dict, only_input_files=True
                    )
                    if p.is_file() is True
                ]
            )
        # post process the list
        for f in input_files:
            # check for image header files (mhd) and add the corresponding raw files to the list
            if f.suffix == ".mhd":
                input_files.append(f.parent.absolute() / Path(f.stem + ".raw"))
        for f in input_files:
            shutil.copy2(f, directory)

    def get_output_path(self, path=None, is_file_or_directory="file"):
        if path is None:
            # no input -> return global output directory
            p_out = Path(self.output_dir)
        else:
            # make sure type is Path
            p = Path(path)
            if not p.is_absolute():
                # prepend the global output dir if p is relative
                p_out = self.output_dir / p
            else:
                # or just keep it
                p_out = p

        # Make sure the directory exists
        if is_file_or_directory in ["file", "File", "f"]:
            n = len(p_out.parts) - 1  # last item is the filename
        elif is_file_or_directory in ["dir", "Dir", "directory", "d"]:
            n = len(p_out.parts)  # all items are part of the directory
        if len(p_out.parts) > 0:
            directory = Path(p_out.parts[0])
            for i in range(n - 1):
                directory /= p_out.parts[i + 1]
            ensure_directory_exists(directory)

        return p_out.absolute()

    def add_g4_command_after_init(self, command):
        """
        For the moment, only use it *after* runManager.Initialize
        """
        self.g4_commands_after_init.append(command)

    def add_g4_command_before_init(self, command):
        """
        For the moment, only use it *after* runManager.Initialize
        """
        self.g4_commands_before_init.append(command)

    # FIXME: will we become obsolete when refactoring the sources
    def get_source_user_info(self, name):
        return self.source_manager.get_source_info(name)

    def get_actor_user_info(self, name):
        s = self.actor_manager.get_actor_user_info(name)
        return s

    def add_volume(self, volume, name=None):
        return self.volume_manager.add_volume(volume, name)

    def add_parallel_world(self, name):
        self.volume_manager.add_parallel_world(name)

    def add_source(self, source_type, name):
        return self.source_manager.add_source(source_type, name)

    def add_actor(self, actor_type, name):
        return self.actor_manager.add_actor(actor_type, name)

    def add_filter(self, filter_type, name):
        return self.filter_manager.add_filter(filter_type, name)

    # def start(self, start_new_process=False):
    #     se = SimulationEngine(self, start_new_process=start_new_process)
    #     self.output = se.start()
    #     return self.output

    @property
    def multithreaded(self):
        return self.number_of_threads > 1 or self.force_multithread_mode

    def _run_simulation_engine(self, start_new_process):
        """Method that creates a simulation engine in a context (with ...) and runs a simulation.

        Args:
            q (:obj: queue, optional) : A queue object to which simulation output can be added if run in a subprocess.
                The dispatching function needs to extract the output from the queue.
            start_new_process (bool, optional) : A flag passed to the engine
                so it knows if it is running in a subprocess.

        Returns:
            :obj:SimulationOutput : The output of the simulation run.
        """
        with SimulationEngine(self) as se:
            se.new_process = start_new_process
            output = se.run_engine()
        return output

    def run(self, start_new_process=False):
        # if windows and MT -> fail
        if os.name == "nt" and self.multithreaded:
            fatal(
                "Error, the multi-thread option is not available for Windows now. "
                "Run the simulation with one thread."
            )

        # prepare sub process
        if start_new_process is True:
            """Important: put the
            if __name__ == '__main__':
            at the beginning of the script
            https://britishgeologicalsurvey.github.io/science/python-forking-vs-spawn/
            """

            self.output = dispatch_to_subprocess(self._run_simulation_engine, True)
        else:
            self.output = self._run_simulation_engine(False)

        # FIXME: should this not be done in a __setstate__ method?
        # put back the simulation object to all actors
        for actor in self.output.actors.values():
            actor.simulation = self
        self.output.simulation = self

        if self.store_json_archive is True:
            self.to_json_file()

    def voxelize_geometry(
        self,
        extent="auto",
        spacing=(3, 3, 3),
        margin=0,
        filename=None,
        return_path=False,
    ):
        """Create a voxelized three-dimensional representation of the simulation geometry.

        The user can specify the sub-portion (a rectangular box) of the simulation which is to be extracted.

        Args:
            extent : By default ('auto'), GATE automatically determines the sub-portion
                to contain all volumes of the simulation.
                Alternatively, extent can be either a tuple of 3-vectors indicating the two diagonally
                opposite corners of the box-shaped
                sub-portion of the geometry to be extracted, or a volume or list volumes.
                In the latter case, the box is automatically determined to contain the volume(s).
            spacing (tuple) : The voxel spacing in x-, y-, z-direction.
            margin : Width (in voxels) of the additional margin around the extracted box-shaped sub-portion
                indicated by `extent`.
            filename (str, optional) : The filename/path to which the voxelized image and labels are written.
                Suffix added automatically. Path can be relative to the global output directory of the simulation.
            return_path (bool) : Return the absolute path where the voxelixed image was written?

        Returns:
            dict, itk image, (path) : A dictionary containing the label to volume LUT; the voxelized geoemtry;
                optionally: the absolute path where the image was written, if applicable.
        """
        # collect volumes which are directly underneath the world/parallel worlds
        if extent in ("auto", "Auto"):
            self.volume_manager.update_volume_tree_if_needed()
            extent = list(self.volume_manager.world_volume.children)
            for pw in self.volume_manager.parallel_world_volumes.values():
                extent.extend(list(pw.children))

        labels, image = dispatch_to_subprocess(
            self._get_voxelized_geometry, extent, spacing, margin
        )

        if filename is not None:
            outpath = self.get_output_path(filename)

            outpath_json = outpath.parent / (outpath.stem + "_labels.json")
            outpath_mhd = outpath.parent / (outpath.stem + "_image.mhd")

            # write labels
            with open(outpath_json, "w") as outfile:
                dump_json(labels, outfile, indent=4)

            # write image
            itk.imwrite(image, ensure_filename_is_str(outpath_mhd))
        else:
            outpath_mhd = "not_applicable"

        if return_path is True:
            return labels, image, outpath_mhd
        else:
            return labels, image

    def _get_voxelized_geometry(self, extent, spacing, margin):
        """Private method which returns a voxelized image of the simulation geometry
        given the extent, spacing and margin.

        The voxelization does not check which volume is voxelized.
        Every voxel will be assigned an ID corresponding to the material at this position
        in the world.
        """

        if isinstance(extent, VolumeBase):
            image = create_image_with_volume_extent(extent, spacing, margin)
        elif isinstance(extent, __gate_list_objects__) and all(
            [isinstance(e, VolumeBase) for e in extent]
        ):
            image = create_image_with_volume_extent(extent, spacing, margin)
        elif isinstance(extent, __gate_list_objects__) and all(
            [isinstance(e, __gate_list_objects__) and len(e) == 3 for e in extent]
        ):
            image = create_image_with_extent(extent, spacing, margin)
        else:
            fatal(
                f"The input variable `extent` needs to be a tuple of 3-vectors, or a volume, "
                f"or a list of volumes. Found: {extent}."
            )

        with SimulationEngine(self) as se:
            se.initialize()
            vox = g4.GateVolumeVoxelizer()
            update_image_py_to_cpp(image, vox.fImage, False)
            vox.Voxelize()
            image = get_cpp_image(vox.fImage)
            labels = vox.fLabels

        return labels, image


process_cls(PhysicsManager)
process_cls(PhysicsListManager)
process_cls(Simulation)
