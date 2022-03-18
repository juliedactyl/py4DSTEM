# Utility functions for the crystal module of py4DSTEM

import numpy as np
from dataclasses import dataclass


@dataclass
class Orientation:
    """
    A class for storing output orientations, generated by fitting a Crystal 
    class orientation plan or Bloch wave pattern matching to a PointList.
    """
    num_matches: int
    matrix = None
    family = None
    corr = None
    inds = None
    mirror = None
    angles = None

    def __post_init__(self):
        # initialize empty arrays
        self.matrix = np.zeros((self.num_matches,3,3))
        self.family = np.zeros((self.num_matches,3,3))
        self.corr   = np.zeros((self.num_matches))
        self.inds   = np.zeros((self.num_matches,2), dtype='int')
        self.mirror = np.zeros((self.num_matches), dtype='bool')
        self.angles = np.zeros((self.num_matches,3))


@dataclass
class OrientationMap:
    """
    A class for storing output orientations, generated by fitting a Crystal class orientation plan or
    Bloch wave pattern matching to a PointListArray.

    """
    num_x: int
    num_y: int
    num_matches: int
    matrix = None
    family = None
    corr = None
    inds = None
    angles = None
    # basis_zone_axis = None
    # basis_in_plane = None
    # map_fiber = None

    def __post_init__(self):
        # initialize empty arrays
        self.matrix = np.zeros((self.num_x,self.num_y,self.num_matches,3,3))
        self.family = np.zeros((self.num_x,self.num_y,self.num_matches,3,3))
        self.corr   = np.zeros((self.num_x,self.num_y,self.num_matches))
        self.inds   = np.zeros((self.num_x,self.num_y,self.num_matches,2), dtype='int')
        self.mirror = np.zeros((self.num_x,self.num_y,self.num_matches), dtype='bool')
        self.angles = np.zeros((self.num_x,self.num_y,self.num_matches,3))

    def set_orientation(self,orientation,ind_x,ind_y):
        # Add an orientation to the orientation map
        self.matrix[ind_x,ind_y] = orientation.matrix
        self.family[ind_x,ind_y] = orientation.family
        self.corr[ind_x,ind_y] = orientation.corr
        self.inds[ind_x,ind_y] = orientation.inds
        self.mirror[ind_x,ind_y] = orientation.mirror
        self.angles[ind_x,ind_y] = orientation.angles

    def get_orientation(self,ind_x,ind_y):
        # Return an orientation from the orientation map
        orientation = Orientation(num_matches=self.num_matches)
        orientation.matrix = self.matrix[ind_x,ind_y]
        orientation.family = self.family[ind_x,ind_y]
        orientation.corr = self.corr[ind_x,ind_y]
        orientation.inds = self.inds[ind_x,ind_y]
        orientation.mirror = self.mirror[ind_x,ind_y]
        orientation.angles = self.angles[ind_x,ind_y]
        return orientation

    # def set_basis_zone_axis(self,basis,ind_match):
    #     if basis_zone_axis is None:
    #         self.basis_zone_axis = np.zeros((self.num_x,self.num_y,self.num_matches,3))
    #     self.basis_zone_axis[:,:,ind_match,:] = basis

    # def get_basis_zone_axis(self,ind_match):
    #     return self.basis_zone_axis[:,:,ind_match,:]






def axisEqual3D(ax):
    extents = np.array([getattr(ax, "get_{}lim".format(dim))() for dim in "xyz"])
    sz = extents[:, 1] - extents[:, 0]
    centers = np.mean(extents, axis=1)
    maxsize = max(abs(sz))
    r = maxsize / 2
    for ctr, dim in zip(centers, "xyz"):
        getattr(ax, "set_{}lim".format(dim))(ctr - r, ctr + r)

