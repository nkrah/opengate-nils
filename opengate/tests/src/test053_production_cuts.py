#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import opengate as gate
import opengate_core as g4

paths = gate.get_default_test_paths(__file__)


def simulate():
    # create the simulation
    energy = 200.0
    sim = gate.Simulation()
    sim.number_of_threads = 1

    # main options
    ui = sim.user_info
    ui.g4_verbose = True
    ui.g4_verbose_level = 0
    ui.visu = False
    ui.random_engine = "MersenneTwister"

    cm = gate.g4_units("cm")
    mm = gate.g4_units("mm")
    MeV = gate.g4_units("MeV")

    requested_cuts_proton = {}
    # requested_minekine = {}

    # *** Step size in a single volume ***
    waterbox_A = sim.add_volume("Box", "waterbox_A")
    waterbox_A.size = [10 * cm, 10 * cm, 10 * cm]
    waterbox_A.translation = [0 * cm, 0 * cm, 11 * cm]
    waterbox_A.material = "G4_WATER"

    # Choose an "awkward" step size
    # which does not corrispond to any of Geant4's
    # defaults to make the assertion (below) significant
    cut_proton = 10.7 * mm
    sim.set_production_cut(waterbox_A.name, "proton", cut_proton)
    requested_cuts_proton[waterbox_A.name] = cut_proton

    # *** Step sizes in individual volumes in nested volume structure ***
    waterbox_B = sim.add_volume("Box", "waterbox_B")
    waterbox_B.size = waterbox_A.size
    waterbox_B.translation = [
        0 * cm,
        0 * cm,
        waterbox_A.translation[2] + 1.1 * waterbox_B.size[2],
    ]
    waterbox_B.material = "G4_WATER"

    previous_mother = waterbox_B
    for i in range(6):
        new_insert = sim.add_volume("Box", f"insert_B_{i}")
        new_insert.size = [0.9 * s for s in previous_mother.size]
        # new_insert.translation = waterbox_B.translation
        new_insert.material = waterbox_B.material
        new_insert.mother = previous_mother.name
        previous_mother = new_insert
        # Set step in every second insert
        if i % 2 == 0:
            cut_proton = 2.1 + i / 100.0 * mm
            sim.set_production_cut(new_insert.name, "proton", cut_proton)
            requested_cuts_proton[new_insert.name] = cut_proton

    # *** Step sizes propagated to nested volumes ***
    waterbox_C = sim.add_volume("Box", "waterbox_C")
    waterbox_C.size = waterbox_A.size
    waterbox_C.translation = [
        0 * cm,
        0 * cm,
        waterbox_B.translation[2] + 1.1 * waterbox_C.size[2],
    ]
    waterbox_C.material = "G4_WATER"

    cut_proton = 3.39 * mm
    sim.set_production_cut(
        waterbox_C.name, "proton", cut_proton, propagate_to_daughters=True
    )
    requested_cuts_proton[waterbox_C.name] = cut_proton

    previous_mother = waterbox_C
    for i in range(6):
        new_insert = sim.add_volume("Box", f"insert_C_{i}")
        new_insert.size = [0.9 * s for s in previous_mother.size]
        new_insert.material = waterbox_C.material
        new_insert.mother = previous_mother.name
        previous_mother = new_insert
        requested_cuts_proton[new_insert.name] = cut_proton

    # *** Step size set via region object ***
    region_D = sim.add_region("region_D")
    region_D.production_cuts.proton = 4.87 * mm

    for i in range(4):
        new_box = sim.add_volume("Box", f"waterbox_D{i}")
        new_box.size = [1 * mm, 1 * mm, 1 * mm]
        new_box.translation = [
            i * 2 * cm,
            0 * cm,
            waterbox_C.translation[2] + 1.1 * waterbox_C.size[2],
        ]
        new_box.material = "G4_WATER"
        region_D.associate_volume(new_box)
        requested_cuts_proton[new_box.name] = region_D.production_cuts.proton

    # Arbritrary source because we do not really need
    # the simulation, only the initialization
    source = sim.add_source("GenericSource", "Default")
    source.particle = "proton"
    source.energy.mono = energy * MeV
    source.position.radius = 1 * mm
    source.direction.type = "momentum"
    source.direction.momentum = [0, 0, 1]
    source.n = 1e1

    se = gate.SimulationEngine(sim)
    # Set the hook function user_fct_after_init
    # to the function defined below
    se.user_fct_after_init = check_user_limit
    output = se.start()

    print("Checking step limits:")
    global_cut_proton = None
    for item in output.hook_log:
        if item[0] == "world":
            global_cut_proton = item[1]["proton"]

    for item in output.hook_log:
        print(f"Volume {item[0]}:")
        value_dict = item[1]
        if item[0] == "world":
            print(f"Found cut for protons in World: {value_dict['proton']}")
        else:
            try:
                print(
                    f"Requested production cut for protons: {requested_cuts_proton[item[0]]}"
                )
                print(f"Found: {value_dict['proton']}")
                assert requested_cuts_proton[item[0]] == value_dict["proton"]
            except KeyError:
                print(
                    f"Found production cut for protons, {value_dict['proton']}, but no requested cut."
                )
                if value_dict["proton"] == global_cut_proton:
                    print(
                        "... but don't worry, this is just the global cut as expected."
                    )
                else:
                    print(
                        "... and that is strange because it does not match the global cut. "
                    )
                    raise Exception("Found unexpected production cut")
    print("Test passed")

    # return RunManager to avoid garbage collection before the other objects
    # and thus a segfault
    return se.g4_RunManager


def check_user_limit(simulation_engine):
    """Function to be called by opengate after initialization
    of the simulation, i.e. when G4 volumes and regions exist.
    The purpose is to check whether Geant4 has properly set
    the step limit in the specific region.

    The value max_step_size is stored in the attribute hook_log
    which can be accessed via the output of the simulation.

    """
    print(f"Entered hook")
    for volume_name, g4_volume in simulation_engine.volume_engine.g4_volumes.items():
        # print(volume_name, g4_volume.g4_region)
        if g4_volume.g4_region is not None:
            region_name = g4_volume.g4_region.GetName()
            print(f"In hook: found volume {volume_name} with region {region_name}")
            user_limits = g4_volume.g4_region.GetUserLimits()
            print(f"In hook: found UserLimit {user_limits}")

            pc = g4_volume.g4_region.GetProductionCuts()
            # UserLimit is None for the DefaultWorldRegion
            if pc is not None:
                cut_proton = pc.GetProductionCut("proton")
                simulation_engine.hook_log.append(
                    (
                        volume_name,
                        {"proton": cut_proton},
                    )
                )
                print(f"In hook: found production cut for protons {cut_proton}")
            else:
                print("ProductionCuts is None")


# --------------------------------------------------------------------------
if __name__ == "__main__":
    rm = simulate()