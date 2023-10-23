# Defines the Strain class

import warnings
from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
from py4DSTEM import PointList, PointListArray, tqdmnd
from py4DSTEM.braggvectors import BraggVectors
from py4DSTEM.data import Data, RealSlice
from py4DSTEM.preprocess.utils import get_maxima_2D
from py4DSTEM.process.strain.latticevectors import (
    add_indices_to_braggvectors,
    fit_lattice_vectors_all_DPs,
    get_reference_g1g2,
    get_rotated_strain_map,
    get_strain_from_reference_g1g2,
    index_bragg_directions,
)
from py4DSTEM.visualize import add_bragg_index_labels, add_pointlabels, add_vector, show

warnings.simplefilter(action="always", category=UserWarning)


class StrainMap(RealSlice, Data):
    """
    Storage and processing methods for 4D-STEM datasets.

    """

    def __init__(self, braggvectors: BraggVectors, name: Optional[str] = "strainmap"):
        """
        Accepts:
            braggvectors (BraggVectors): BraggVectors for Strain Map
            name (str): the name of the strainmap
        Returns:
            A new StrainMap instance.
        """
        assert isinstance(
            braggvectors, BraggVectors
        ), f"braggvectors must be BraggVectors, not type {type(braggvectors)}"

        # initialize as a RealSlice
        RealSlice.__init__(
            self,
            name=name,
            data=np.empty(
                (
                    6,
                    braggvectors.Rshape[0],
                    braggvectors.Rshape[1],
                )
            ),
            slicelabels=["exx", "eyy", "exy", "theta", "mask", "error"],
        )

        # set up braggvectors
        # this assigns the bvs, ensures the origin is calibrated,
        # and adds the strainmap to the bvs' tree
        self.braggvectors = braggvectors

        # initialize as Data
        Data.__init__(self)

        # set calstate
        # this property is used only to check to make sure that
        # the braggvectors being used throughout a workflow are
        # the same. The state of calibration of the vectors is noted
        # here, and then checked each time the vectors are used -
        # if they differ, an error message and instructions for
        # re-calibration are issued
        self.calstate = self.braggvectors.calstate
        assert self.calstate["center"], "braggvectors must be centered"
        if self.calstate["rotate"] == False:
            warnings.warn(
                ("Real to reciprocal space rotation not calibrated"),
                UserWarning,
            )

        # get the BVM
        # a new BVM using the current calstate is computed
        self.bvm = self.braggvectors.histogram(mode="cal")

    # braggvector properties

    @property
    def braggvectors(self):
        return self._braggvectors

    @braggvectors.setter
    def braggvectors(self, x):
        assert isinstance(
            x, BraggVectors
        ), f".braggvectors must be BraggVectors, not type {type(x)}"
        assert (
            x.calibration.origin is not None
        ), "braggvectors must have a calibrated origin"
        self._braggvectors = x
        self._braggvectors.tree(self, force=True)

    @property
    def rshape(self):
        return self._braggvectors.Rshape

    @property
    def qshape(self):
        return self._braggvectors.Qshape

    @property
    def origin(self):
        return self.calibration.get_origin_mean()


    def reset_calstate(self):
        """
        Resets the calibration state. This recomputes the BVM, and removes any computations
        this StrainMap instance has stored, which will need to be recomputed.
        """
        for attr in (
            "g0",
            "g1",
            "g2",
        ):
            if hasattr(self, attr):
                delattr(self, attr)
        self.calstate = self.braggvectors.calstate
        pass

    # Class methods

    def choose_lattice_vectors(
        self,
        index_g1 = None,
        index_g2 = None,
        index_origin = None,
        subpixel="multicorr",
        upsample_factor=16,
        sigma=0,
        minAbsoluteIntensity=0,
        minRelativeIntensity=0,
        relativeToPeak=0,
        minSpacing=0,
        edgeBoundary=1,
        maxNumPeaks=10,
        x0=None,
        y0=None,
        figsize=(14, 9),
        c_indices="lightblue",
        c0="g",
        c1="r",
        c2="r",
        c_vectors="r",
        c_vectorlabels="w",
        size_indices=15,
        width_vectors=1,
        size_vectorlabels=15,
        vis_params={},
        returncalc=False,
        returnfig=False,
    ):
        """
        Choose which lattice vectors to use for strain mapping.

        Overlays the bvm with the points detected via local 2D
        maxima detection, plus an index for each point. User selects
        3 points using the overlaid indices, which are identified as
        the origin and the termini of the lattice vectors g1 and g2.

        Parameters
        ----------
        index_g1 : int
            selected index for g1
        index_g2 :int
            selected index for g2
        index_origin : int
            selected index for the origin
        subpixel : str in ('pixel','poly','multicorr')
            See the docstring for py4DSTEM.preprocess.get_maxima_2D
        upsample_factor : int
            See the py4DSTEM.preprocess.get_maxima_2D docstring
        sigma : number
            See the py4DSTEM.preprocess.get_maxima_2D docstring
        minAbsoluteIntensity : number
            See the py4DSTEM.preprocess.get_maxima_2D docstring
        minRelativeIntensity : number
            See the py4DSTEM.preprocess.get_maxima_2D docstring
        relativeToPeak : int
            See the py4DSTEM.preprocess.get_maxima_2D docstring
        minSpacing : number
            See the py4DSTEM.preprocess.get_maxima_2D docstring
        edgeBoundary : number
            See the py4DSTEM.preprocess.get_maxima_2D docstring
        maxNumPeaks : int
            See the py4DSTEM.preprocess.get_maxima_2D docstring
        figsize : 2-tuple
            the size of the figure
        c_indices : color
            color of the maxima
        c0 : color
            color of the origin
        c1 : color
            color of g1 point
        c2 : color
            color of g2 point
        c_vectors : color
            color of the g1/g2 vectors
        c_vectorlabels : color
            color of the vector labels
        size_indices : number
            size of the indices
        width_vectors : number
            width of the vectors
        size_vectorlabels : number
            size of the vector labels
        vis_params : dict
            additional visualization parameters passed to `show`
        returncalc : bool
            toggles returning the answer
        returnfig : bool
            toggles returning the figure

        Returns
        -------
        (optional) : None or (g0,g1,g2) or (fig,(ax1,ax2)) or both of the latter
        """
        # validate inputs
        for i in (index_origin, index_g1, index_g2):
            assert(isinstance(i, (int, np.integer)) or (i is None)), "indices must be integers!"
        # check the calstate
        assert (
            self.calstate == self.braggvectors.calstate
        ), "The calibration state has changed! To resync the calibration state, use `.reset_calstate`."

        # find the maxima

        g = get_maxima_2D(
            self.bvm.data,
            subpixel=subpixel,
            upsample_factor=upsample_factor,
            sigma=sigma,
            minAbsoluteIntensity=minAbsoluteIntensity,
            minRelativeIntensity=minRelativeIntensity,
            relativeToPeak=relativeToPeak,
            minSpacing=minSpacing,
            edgeBoundary=edgeBoundary,
            maxNumPeaks=maxNumPeaks,
        )

        # guess the origin and g1 g2 vectors if indices aren't provided
        if np.any([x is None for x in (index_g1,index_g2,index_origin)]):

            # get distances and angles from calibrated origin
            g_dists = np.hypot(g['x']-self.origin[0], g['y']-self.origin[1])
            g_angles = np.angle(g['x']-self.origin[0] + 1j*(g['y']-self.origin[1]))

            # guess the origin
            if index_origin is None:
                index_origin = np.argmin(g_dists)
                g_dists[index_origin] = 2*np.max(g_dists)

            # guess g1
            if index_g1 is None:
                index_g1 = np.argmin(g_dists)
                g_dists[index_g1] = 2*np.max(g_dists)

            # guess g2
            if index_g2 is None:
                angle_scaling = np.cos(g_angles - g_angles[index_g1])**2
                index_g2 = np.argmin(g_dists*(angle_scaling+0.1))


        # get the lattice vectors
        gx, gy = g["x"], g["y"]
        g0 = gx[index_origin], gy[index_origin]
        g1x = gx[index_g1] - g0[0]
        g1y = gy[index_g1] - g0[1]
        g2x = gx[index_g2] - g0[0]
        g2y = gy[index_g2] - g0[1]
        g1, g2 = (g1x, g1y), (g2x, g2y)

        # index the lattice vectors
        _, _, braggdirections = index_bragg_directions(
            g0[0], g0[1], g["x"], g["y"], g1, g2
        )

        # make the figure
        fig, ax = plt.subplots(1, 3, figsize=figsize)
        show(self.bvm.data, figax=(fig, ax[0]), **vis_params)
        show(self.bvm.data, figax=(fig, ax[1]), **vis_params)
        self.show_bragg_indexing(
            self.bvm.data,
            bragg_directions=braggdirections,
            points=True,
            figax=(fig, ax[2]),
            size=size_indices,
            **vis_params,
        )

        # Add indices to left panel
        d = {"x": gx, "y": gy, "size": size_indices, "color": c_indices}
        d0 = {
            "x": gx[index_origin],
            "y": gy[index_origin],
            "size": size_indices,
            "color": c0,
            "fontweight": "bold",
            "labels": [str(index_origin)],
        }
        d1 = {
            "x": gx[index_g1],
            "y": gy[index_g1],
            "size": size_indices,
            "color": c1,
            "fontweight": "bold",
            "labels": [str(index_g1)],
        }
        d2 = {
            "x": gx[index_g2],
            "y": gy[index_g2],
            "size": size_indices,
            "color": c2,
            "fontweight": "bold",
            "labels": [str(index_g2)],
        }
        add_pointlabels(ax[0], d)
        add_pointlabels(ax[0], d0)
        add_pointlabels(ax[0], d1)
        add_pointlabels(ax[0], d2)

        # Add vectors to right panel
        dg1 = {
            "x0": gx[index_origin],
            "y0": gy[index_origin],
            "vx": g1[0],
            "vy": g1[1],
            "width": width_vectors,
            "color": c_vectors,
            "label": r"$g_1$",
            "labelsize": size_vectorlabels,
            "labelcolor": c_vectorlabels,
        }
        dg2 = {
            "x0": gx[index_origin],
            "y0": gy[index_origin],
            "vx": g2[0],
            "vy": g2[1],
            "width": width_vectors,
            "color": c_vectors,
            "label": r"$g_2$",
            "labelsize": size_vectorlabels,
            "labelcolor": c_vectorlabels,
        }
        add_vector(ax[1], dg1)
        add_vector(ax[1], dg2)

        # store vectors
        self.g = g
        self.g0 = g0
        self.g1 = g1
        self.g2 = g2

        # center the bragg directions and store
        braggdirections.data['qx'] -= self.origin[0]
        braggdirections.data['qy'] -= self.origin[1]
        self.braggdirections = braggdirections

        # return
        if returncalc and returnfig:
            return (g0, g1, g2), (fig, ax)
        elif returncalc:
            return (g0, g1, g2)
        elif returnfig:
            return (fig, ax)
        else:
            return

    def fit_lattice_vectors(
        self,
        max_peak_spacing=2,
        mask=None,
        plot=True,
        vis_params={},
        returncalc=False,
    ):
        """
        From an origin (x0,y0), a set of reciprocal lattice vectors gx,gy, and an pair of
        lattice vectors g1=(g1x,g1y), g2=(g2x,g2y), find the indices (h,k) of all the
        reciprocal lattice directions.

        Args:
            x0 : floagt
                x-coord of origin
            y0 : float
                y-coord of origin
            max_peak_spacing: float
                Maximum distance from the ideal lattice points
                to include a peak for indexing
            mask: bool
                Boolean mask, same shape as the pointlistarray, indicating which
                locations should be indexed. This can be used to index different regions of
                the scan with different lattices
            plot:bool
                plot results if tru
            vis_params : dict
                additional visualization parameters passed to `show`
            returncalc : bool
                if True, returns bragg_directions, bragg_vectors_indexed, g1g2_map
        """
        # check the calstate
        assert (
            self.calstate == self.braggvectors.calstate
        ), "The calibration state has changed! To resync the calibration state, use `.reset_calstate`."


        ### add indices to the bragg vectors

        # validate mask
        if mask is None:
            mask = np.ones(self.braggvectors.Rshape, dtype=bool)
        assert (
            mask.shape == self.braggvectors.Rshape
        ), "mask must have same shape as pointlistarray"
        assert mask.dtype == bool, "mask must be boolean"

        # set up new braggpeaks PLA
        indexed_braggpeaks = PointListArray(
            dtype = [
                ("qx", float),
                ("qy", float),
                ("intensity", float),
                ("h", int),
                ("k", int),
            ],
            shape=self.braggvectors.Rshape,
        )
        calstate = self.braggvectors.calstate

        # loop over all the scan positions
        for Rx, Ry in tqdmnd(mask.shape[0], mask.shape[1]):
            if mask[Rx, Ry]:
                pl = self.braggvectors.get_vectors(
                    Rx,
                    Ry,
                    center=True,
                    ellipse=calstate["ellipse"],
                    rotate=calstate["rotate"],
                    pixel=False,
                )
                for i in range(pl.data.shape[0]):
                    r = np.hypot(
                        pl.data["qx"][i]-self.braggdirections.data["qx"],
                        pl.data["qy"][i]-self.braggdirections.data["qy"]
                    )
                    ind = np.argmin(r)
                    if r[ind] <= max_peak_spacing:
                        indexed_braggpeaks[Rx, Ry].add_data_by_field(
                            (
                                pl.data["qx"][i],
                                pl.data["qy"][i],
                                pl.data["intensity"][i],
                                self.braggdirections.data["h"][ind],
                                self.braggdirections.data["k"][ind],
                            )
                        )
        self.bragg_vectors_indexed = indexed_braggpeaks


        ### fit bragg vectors
        g1g2_map = fit_lattice_vectors_all_DPs(self.bragg_vectors_indexed)
        self.g1g2_map = g1g2_map


        # return
        if returncalc:
            return self.braggdirections, self.bragg_vectors_indexed, self.g1g2_map

    def get_strain(
        self, mask=None, g_reference=None, flip_theta=False, returncalc=False, **kwargs
    ):
        """
        mask: nd.array (bool)
            Use lattice vectors from g1g2_map scan positions
            wherever mask==True. If mask is None gets median strain
            map from entire field of view. If mask is not None, gets
            reference g1 and g2 from region and then calculates strain.
        g_reference: nd.array of form [x,y]
            G_reference (tupe): reference coordinate system for
            xaxis_x and xaxis_y
        flip_theta: bool
            If True, flips rotation coordinate system
        returncal: bool
            It True, returns rotated map
        """
        # check the calstate
        assert (
            self.calstate == self.braggvectors.calstate
        ), "The calibration state has changed! To resync the calibration state, use `.reset_calstate`."

        if mask is None:
            mask = np.ones(self.g1g2_map.shape, dtype="bool")

            # strainmap_g1g2 = get_strain_from_reference_region(
            #     self.g1g2_map,
            #     mask=mask,
            # )

        #     g1_ref, g2_ref = get_reference_g1g2(self.g1g2_map, mask)
        #     strain_map = get_strain_from_reference_g1g2(self.g1g2_map, g1_ref, g2_ref)
        # else:

        g1_ref, g2_ref = get_reference_g1g2(self.g1g2_map, mask)

        strainmap_g1g2 = get_strain_from_reference_g1g2(self.g1g2_map, g1_ref, g2_ref)

        self.strainmap_g1g2 = strainmap_g1g2

        if g_reference is None:
            g_reference = np.subtract(self.g1, self.g2)

        strainmap_rotated = get_rotated_strain_map(
            self.strainmap_g1g2,
            xaxis_x=g_reference[0],
            xaxis_y=g_reference[1],
            flip_theta=flip_theta,
        )

        self.strainmap_rotated = strainmap_rotated

        from py4DSTEM.visualize import show_strain

        figsize = kwargs.pop("figsize", (14, 4))
        vrange_exx = kwargs.pop("vrange_exx", [-2.0, 2.0])
        vrange_theta = kwargs.pop("vrange_theta", [-2.0, 2.0])
        ticknumber = kwargs.pop("ticknumber", 3)
        bkgrd = kwargs.pop("bkgrd", False)
        axes_plots = kwargs.pop("axes_plots", ())

        fig, ax = show_strain(
            self.strainmap_rotated,
            vrange_exx=vrange_exx,
            vrange_theta=vrange_theta,
            ticknumber=ticknumber,
            axes_plots=axes_plots,
            bkgrd=bkgrd,
            figsize=figsize,
            **kwargs,
            returnfig=True,
        )

        if not np.all(mask == True):
            ax[0][0].imshow(mask, alpha=0.2, cmap="binary")
            ax[0][1].imshow(mask, alpha=0.2, cmap="binary")
            ax[1][0].imshow(mask, alpha=0.2, cmap="binary")
            ax[1][1].imshow(mask, alpha=0.2, cmap="binary")

        if returncalc:
            return self.strainmap_rotated

    def show_lattice_vectors(
        ar,
        x0,
        y0,
        g1,
        g2,
        color="r",
        width=1,
        labelsize=20,
        labelcolor="w",
        returnfig=False,
        **kwargs,
    ):
        """Adds the vectors g1,g2 to an image, with tail positions at (x0,y0).  g1 and g2 are 2-tuples (gx,gy)."""
        fig, ax = show(ar, returnfig=True, **kwargs)

        # Add vectors
        dg1 = {
            "x0": x0,
            "y0": y0,
            "vx": g1[0],
            "vy": g1[1],
            "width": width,
            "color": color,
            "label": r"$g_1$",
            "labelsize": labelsize,
            "labelcolor": labelcolor,
        }
        dg2 = {
            "x0": x0,
            "y0": y0,
            "vx": g2[0],
            "vy": g2[1],
            "width": width,
            "color": color,
            "label": r"$g_2$",
            "labelsize": labelsize,
            "labelcolor": labelcolor,
        }
        add_vector(ax, dg1)
        add_vector(ax, dg2)

        if returnfig:
            return fig, ax
        else:
            plt.show()
            return

    def show_bragg_indexing(
        self,
        ar,
        bragg_directions,
        voffset=5,
        hoffset=0,
        color="w",
        size=20,
        points=True,
        pointcolor="r",
        pointsize=50,
        figax=None,
        returnfig=False,
        **kwargs,
    ):
        """
        Shows an array with an overlay describing the Bragg directions

        Accepts:
            ar                  (arrray) the image
            bragg_directions    (PointList) the bragg scattering directions; must have coordinates
                                'qx','qy','h', and 'k'. Optionally may also have 'l'.
        """
        assert isinstance(bragg_directions, PointList)
        for k in ("qx", "qy", "h", "k"):
            assert k in bragg_directions.data.dtype.fields

        if figax is None:
            fig, ax = show(ar, returnfig=True, **kwargs)
        else:
            fig = figax[0]
            ax = figax[1]
            show(ar, figax=figax, **kwargs)

        d = {
            "bragg_directions": bragg_directions,
            "voffset": voffset,
            "hoffset": hoffset,
            "color": color,
            "size": size,
            "points": points,
            "pointsize": pointsize,
            "pointcolor": pointcolor,
        }
        add_bragg_index_labels(ax, d)

        if returnfig:
            return fig, ax
        else:
            return

    def copy(self, name=None):
        name = name if name is not None else self.name + "_copy"
        strainmap_copy = StrainMap(self.braggvectors)
        for attr in (
            "g",
            "g0",
            "g1",
            "g2",
            "calstate",
            "bragg_directions",
            "bragg_vectors_indexed",
            "g1g2_map",
            "strainmap_g1g2",
            "strainmap_rotated",
        ):
            if hasattr(self, attr):
                setattr(strainmap_copy, attr, getattr(self, attr))

        for k in self.metadata.keys():
            strainmap_copy.metadata = self.metadata[k].copy()
        return strainmap_copy

    # TODO IO methods

    # read
    @classmethod
    def _get_constructor_args(cls, group):
        """
        Returns a dictionary of args/values to pass to the class constructor
        """
        ar_constr_args = RealSlice._get_constructor_args(group)
        args = {
            "data": ar_constr_args["data"],
            "name": ar_constr_args["name"],
        }
        return args
