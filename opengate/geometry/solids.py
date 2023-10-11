from box import Box

from ..base import GateObject
from ..utility import g4_units
from ..exception import fatal, warning
import opengate_core as g4


class SolidBase(GateObject):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.g4_solid = None

    def close(self):
        self.release_g4_references()

    def release_g4_references(self):
        self.g4_solid = None

    def get_solid_info(self):
        """Computes the properties of the solid associated with this volume."""
        # Note: This method only works in derived classes which implement the build_solid method.
        solid = self.build_solid()
        if solid is None:
            fatal(
                "Cannot compute solid info for this volume {self.name}. Unable to build the solid."
            )
        r = Box()
        r.cubic_volume = solid.GetCubicVolume()
        r.surface_area = solid.GetSurfaceArea()
        pMin = g4.G4ThreeVector()
        pMax = g4.G4ThreeVector()
        solid.BoundingLimits(pMin, pMax)
        r.bounding_limits = [pMin, pMax]
        return r

    def bounding_limits(self):
        """
        Return the min and max 3D points of the bounding box of the given volume
        """
        pMin, pMax = self.get_solid_info().bounding_limits
        return pMin, pMax

    def bounding_box_size(self):
        """
        Return the size of the bounding box of the given volume
        """
        pMin, pMax = self.bounding_limits()
        return [pMax[0] - pMin[0], pMax[1] - pMin[1], pMax[2] - pMin[2]]

    def build_solid(self):
        s = (
            "Error message for developers: "
            f"You need to override 'build_solid' method in class {type(self).__name__}"
        )
        fatal(s)


class BoxSolid(SolidBase):
    user_info_defaults = {
        "size": (
            [10 * g4_units.cm, 10 * g4_units.cm, 10 * g4_units.cm],
            {"doc": "3 component list of side lengths of the box."},
        )
    }

    def build_solid(self):
        return g4.G4Box(
            self.name, self.size[0] / 2.0, self.size[1] / 2.0, self.size[2] / 2.0
        )


class HexagonSolid(SolidBase):
    """
    This is the special case of a six-sided polyhedron.
    https://geant4-userdoc.web.cern.ch/UsersGuides/ForApplicationDeveloper/html/Detector/Geometry/geomSolids.html

    """

    user_info_defaults = {
        "height": (
            5 * g4_units.cm,
            {"doc": "Height of the hexagon volume."},
        ),
        "radius": (
            0.15 * g4_units.cm,
            {"doc": "Radius from the center to corners."},
        ),
    }

    def build_solid(self):
        deg = g4_units.deg
        phi_start = 0 * deg
        phi_total = 360 * deg
        num_side = 6
        num_zplanes = 2
        zplane = [-self.height / 2, self.height / 2]
        radius_inner = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        radius_outer = [self.radius] * num_side

        return g4.G4Polyhedra(
            self.name,
            phi_start,
            phi_total,
            num_side,
            num_zplanes,
            zplane,
            radius_inner,
            radius_outer,
        )


class ConsSolid(SolidBase):
    """Cone section.
    http://geant4-userdoc.web.cern.ch/geant4-userdoc/UsersGuides/ForApplicationDeveloper/html/Detector/Geometry/geomSolids.html

    """

    user_info_defaults = {
        "rmin1": (
            5 * g4_units.mm,
            {"doc": "Inner radius at the lower end."},
        ),
        "rmin2": (
            20 * g4_units.mm,
            {"doc": "Inner radius at the upper end."},
        ),
        "rmax1": (
            10 * g4_units.mm,
            {"doc": "Outer radius at the lower end."},
        ),
        "rmax2": (
            25 * g4_units.mm,
            {"doc": "Outer radius at the upper end."},
        ),
        "dz": (40 * g4_units.mm, {"doc": "Half length in Z."}),
        "sphi": (
            0 * g4_units.deg,
            {"doc": "Starting angle of the segment in radians."},
        ),
        "dphi": (
            45 * g4_units.deg,
            {"doc": "The angle of the segment in radians."},
        ),
    }

    def build_solid(self):
        return g4.G4Cons(
            self.name,
            self.rmin1,
            self.rmax1,
            self.rmin2,
            self.rmax2,
            self.dz,
            self.sphi,
            self.dphi,
        )


class PolyhedraSolid(SolidBase):
    """
    https://geant4-userdoc.web.cern.ch/UsersGuides/ForApplicationDeveloper/html/Detector/Geometry/geomSolids.html

    """

    user_info_defaults = {
        "phi_start": (
            0 * g4_units.deg,
            {"doc": "Initial Phi starting angle"},
        ),
        "phi_total": (
            360 * g4_units.deg,
            {"doc": "Total Phi angle"},
        ),
        "num_side": (6, {"doc": "Number of sides."}),
        "num_zplanes": (2, {"doc": "Number Z planes."}),
        "zplane": (
            [-2.5 * g4_units.cm, 2.5 * g4_units.cm],
            {
                "doc": "Position of Z planes. Should be a list with one position per plane."
            },
        ),
        "radius_inner": (
            [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            {
                "doc": "Tangent distance to inner surface. Should be a list with one distance per side."
            },
        ),
        "radius_outer": (
            [0.15 * g4_units.cm] * 6,
            {
                "doc": "Tangent distance to outer surface. Should be a list with one distance per side."
            },
        ),
    }

    def build_solid(self):
        return g4.G4Polyhedra(
            self.name,
            self.phi_start,
            self.phi_total,
            self.num_side,
            self.num_zplanes,
            self.zplane,
            self.radius_inner,
            self.radius_outer,
        )


class SphereSolid(SolidBase):
    user_info_defaults = {
        "rmin": (0, {"doc": "Inner radius (0 means solid sphere)."}),
        "rmax": (
            1 * g4_units.mm,
            {"doc": "Outer radius of the sphere."},
        ),
        "sphi": (0, {"doc": ""}),
        "dphi": (180 * g4_units.deg, {"doc": ""}),
        "stheta": (0, {"doc": ""}),
        "dtheta": (180 * g4_units.deg, {"doc": ""}),
    }

    def build_solid(self):
        return g4.G4Sphere(
            self.name,
            self.rmin,
            self.rmax,
            self.sphi,
            self.dphi,
            self.stheta,
            self.dtheta,
        )


class TrapSolid(SolidBase):
    """
    http://geant4-userdoc.web.cern.ch/geant4-userdoc/UsersGuides/ForApplicationDeveloper/html/Detector/Geometry/geomSolids.html

    """

    user_info_defaults = {
        "dx1": (
            30 * g4_units.mm,
            {"doc": "Half x length of the side at y=-pdy1 of the face at -pdz"},
        ),
        "dx2": (
            40 * g4_units.mm,
            {"doc": "Half x length of the side at y=+pdy1 of the face at -pdz"},
        ),
        "dy1": (40 * g4_units.mm, {"doc": "Half y length at -pdz"}),
        "dy2": (16 * g4_units.mm, {"doc": "Half y length at +pdz"}),
        "dx3": (
            10 * g4_units.mm,
            {"doc": "Half x length of the side at y=-pdy2 of the face at +pdz"},
        ),
        "dx4": (
            14 * g4_units.mm,
            {"doc": "Half x length of the side at y=+pdy2 of the face at +pdz"},
        ),
        "dz": (60 * g4_units.mm, {"doc": "Half z length"}),
        "theta": (
            20 * g4_units.deg,
            {
                "doc": "Polar angle of the line joining the centres of the faces at -/+pdz"
            },
        ),
        "phi": (
            5 * g4_units.deg,
            {
                "doc": "Azimuthal angle of the line joining the centre of the face at -pdz "
                "to the centre of the face at +pdz"
            },
        ),
        "alp1": (
            10 * g4_units.deg,
            {
                "doc": "Angle with respect to the y axis from the centre of the side (lower endcap)"
            },
        ),
        "alp2": (
            10 * g4_units.deg,
            {
                "doc": "Angle with respect to the y axis from the centre of the side (upper endcap)"
            },
        ),
    }

    def build_solid(self):
        return g4.G4Trap(
            self.name,
            self.dz,
            self.theta,
            self.phi,
            self.dy1,
            self.dx1,
            self.dx2,
            self.alp1,
            self.dy2,
            self.dx3,
            self.dx4,
            self.alp2,
        )


class TrdSolid(SolidBase):
    """
    https://geant4-userdoc.web.cern.ch/UsersGuides/ForApplicationDeveloper/html/Detector/Geometry/geomSolids.html?highlight=g4trd

    dx1 Half-length along X at the surface positioned at -dz
    dx2 Half-length along X at the surface positioned at +dz
    dy1 Half-length along Y at the surface positioned at -dz
    dy2 Half-length along Y at the surface positioned at +dz
    zdz Half-length along Z axis

    """

    user_info_defaults = {
        "dx1": (
            30 * g4_units.mm,
            {"doc": "Half-length along X at the surface positioned at -dz"},
        ),
        "dx2": (
            10 * g4_units.mm,
            {"doc": "dx2 Half-length along X at the surface positioned at +dz"},
        ),
        "dy1": (
            40 * g4_units.mm,
            {"doc": "dy1 Half-length along Y at the surface positioned at -dz"},
        ),
        "dy2": (
            15 * g4_units.mm,
            {"doc": "dy2 Half-length along Y at the surface positioned at +dz"},
        ),
        "dz": (
            15 * g4_units.mm,
            {"doc": "Half-length along Z axis"},
        ),
    }

    def build_solid(self):
        return g4.G4Trd(self.name, self.dx1, self.dx2, self.dy1, self.dy2, self.dz)


class TubsSolid(SolidBase):
    """
    http://geant4-userdoc.web.cern.ch/geant4-userdoc/UsersGuides/ForApplicationDeveloper/html/Detector/Geometry/geomSolids.html

    """

    user_info_defaults = {
        "rmin": (30 * g4_units.mm, {"doc": "Inner radius"}),
        "rmax": (40 * g4_units.mm, {"doc": "Outer radius"}),
        "dz": (40 * g4_units.mm, {"doc": "Half length along Z"}),
        "sphi": (0 * g4_units.deg, {"doc": "Start angle phi"}),
        "dphi": (360 * g4_units.deg, {"doc": "Angle segment"}),
    }

    def build_solid(self):
        return g4.G4Tubs(self.name, self.rmin, self.rmax, self.dz, self.sphi, self.dphi)