/* --------------------------------------------------
   Copyright (C): OpenGATE Collaboration
   This software is distributed under the terms
   of the GNU Lesser General  Public Licence (LGPL)
   See LICENSE.md for further details
   -------------------------------------------------- */
#include <pybind11/pybind11.h>

namespace py = pybind11;

#include "G4VProcess.hh"

// Declaration of this class is needed because other classes such as
// StepLimiter inherit from it and pybind needs to know about it.

void init_G4VProcess(py::module &m) {

  py::class_<G4VProcess, std::unique_ptr<G4VProcess, py::nodelete>>(
      m, "G4VProcess");
}
