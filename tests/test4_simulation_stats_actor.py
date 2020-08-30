#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gam
import gam_g4 as g4

gam.log.setLevel(gam.DEBUG)

# create the simulation
s = gam.Simulation()
s.enable_g4_verbose(False)

# set random engine
s.set_random_engine("MersenneTwister", 123456)

# add a simple volume
waterbox = s.add_volume('Box', 'Waterbox')
cm = gam.g4_units('cm')
waterbox.size = [40 * cm, 40 * cm, 40 * cm]
waterbox.translation = [0 * cm, 0 * cm, 25 * cm]
waterbox.material = 'Water'

# physic list
# print('Phys lists :', s.get_available_physicLists())

# default source for tests
source = s.add_source('TestProtonPy2', 'Default')
MeV = gam.g4_units('MeV')
source.energy = 150 * MeV
source.diameter = 2 * cm
source.n = 2000

# add stat actor
stats = s.add_actor('SimulationStatistics', 'Stats')

# create G4 objects
s.initialize()

print(gam.info_all_sources(s))

print('Simulation seed:', s.seed)
print(s.dump_geometry_tree())

# verbose
s.g4_com('/tracking/verbose 0')
# s.g4_com("/run/verbose 2")
# s.g4_com("/event/verbose 2")
# s.g4_com("/tracking/verbose 1")

# start simulation
gam.source_log.setLevel(gam.RUN)
s.start()

a = s.actors_info.Stats.g4_actor
print(a)

assert a.run_count == 1
assert a.event_count == 2000
assert a.track_count == 25297
assert a.step_count == 107029
assert a.batch_count == 3

print(f'OSX PPS = ~3856 --> {a.pps:.0f}')

print()
print('Great, ALL done ! ')