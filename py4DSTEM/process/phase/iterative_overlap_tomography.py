"""
Module for reconstructing phase objects from 4DSTEM datasets using iterative methods,
namely overlap tomography.
"""

import warnings
from typing import Mapping, Sequence, Tuple, Union

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.gridspec import GridSpec
from mpl_toolkits.axes_grid1 import ImageGrid, make_axes_locatable

try:
    import cupy as cp
except ImportError:
    cp = None

from py4DSTEM.io import DataCube
from py4DSTEM.process.phase.iterative_base_class import PhaseReconstruction
from py4DSTEM.process.phase.utils import (
    ComplexProbe,
    fft_shift,
    generate_batches,
    polar_aliases,
    polar_symbols,
    spatial_frequencies,
    fourier_rotate_real_volume,
)
from py4DSTEM.process.utils import (
    electron_wavelength_angstrom,
    get_CoM,
    get_shifted_ar,
)
from py4DSTEM.utils.tqdmnd import tqdmnd

warnings.simplefilter(action="always", category=UserWarning)


class OverlapTomographicReconstruction(PhaseReconstruction):
    """
    Overlap Tomographic Reconstruction Class.

    List of diffraction intensities dimensions  : (Rx,Ry,Qx,Qy)
    Reconstructed probe dimensions              : (Sx,Sy)
    Reconstructed object dimensions             : (Px,Py,Py)

    such that (Sx,Sy) is the region-of-interest (ROI) size of our probe
    and (Px,Py,Py) is the padded-object electrostatic potential volume,
    where x-axis is the tilt.

    Parameters
    ----------
    datacube: List of DataCubes
        Input list of 4D diffraction pattern intensities
    energy: float
        The electron energy of the wave functions in eV
    num_slices: int
        Number of slices to use in the forward model
    tilt_angles_deg: Sequence[float]
        List of tilt angles in degrees,
    semiangle_cutoff: float, optional
        Semiangle cutoff for the initial probe guess
    rolloff: float, optional
        Semiangle rolloff for the initial probe guess
    vacuum_probe_intensity: np.ndarray, optional
        Vacuum probe to use as intensity aperture for initial probe guess
    polar_parameters: dict, optional
        Mapping from aberration symbols to their corresponding values. All aberration
        magnitudes should be given in Å and angles should be given in radians.
    diffraction_intensities_shape: Tuple[int,int], optional
        Pixel dimensions (Qx',Qy') of the resampled diffraction intensities
        If None, no resampling of diffraction intenstities is performed
    reshaping_method: str, optional
        Method to use for reshaping, either 'bin, 'bilinear', or 'fourier' (default)
    probe_roi_shape, (int,int), optional
            Padded diffraction intensities shape.
            If None, no padding is performed
    object_padding_px: Tuple[int,int], optional
        Pixel dimensions to pad object with
        If None, the padding is set to half the probe ROI dimensions
    dp_mask: ndarray, optional
        Mask for datacube intensities (Qx,Qy)
    initial_object_guess: np.ndarray, optional
        Initial guess for complex-valued object of dimensions (Px,Py,Py)
        If None, initialized to 1.0
    initial_probe_guess: np.ndarray, optional
        Initial guess for complex-valued probe of dimensions (Sx,Sy). If None,
        initialized to ComplexProbe with semiangle_cutoff, energy, and aberrations
    initial_scan_positions: list of np.ndarray, optional
        Probe positions in Å for each diffraction intensity per tilt
        If None, initialized to a grid scan centered along tilt axis
    verbose: bool, optional
        If True, class methods will inherit this and print additional information
    device: str, optional
        Calculation device will be perfomed on. Must be 'cpu' or 'gpu'
    kwargs:
        Provide the aberration coefficients as keyword arguments.
    """

    def __init__(
        self,
        datacube: Sequence[DataCube],
        energy: float,
        num_slices: int,
        tilt_angles_degrees: Sequence[float],
        semiangle_cutoff: float = None,
        rolloff: float = 2.0,
        vacuum_probe_intensity: np.ndarray = None,
        polar_parameters: Mapping[str, float] = None,
        diffraction_intensities_shape: Tuple[int, int] = None,
        reshaping_method: str = "fourier",
        probe_roi_shape: Tuple[int, int] = None,
        object_padding_px: Tuple[int, int] = None,
        dp_mask: np.ndarray = None,
        initial_object_guess: np.ndarray = None,
        initial_probe_guess: np.ndarray = None,
        initial_scan_positions: Sequence[np.ndarray] = None,
        verbose: bool = True,
        device: str = "cpu",
        **kwargs,
    ):
        if device == "cpu":
            self._xp = np
            self._asnumpy = np.asarray
            from scipy.ndimage import gaussian_filter, zoom, rotate

            self._gaussian_filter = gaussian_filter
            self._zoom = zoom
            self._rotate = rotate
        elif device == "gpu":
            self._xp = cp
            self._asnumpy = cp.asnumpy
            from cupyx.scipy.ndimage import gaussian_filter, zoom, rotate

            self._gaussian_filter = gaussian_filter
            self._zoom = zoom
            self._rotate = rotate
        else:
            raise ValueError(f"device must be either 'cpu' or 'gpu', not {device}")

        for key in kwargs.keys():
            if (key not in polar_symbols) and (key not in polar_aliases.keys()):
                raise ValueError("{} not a recognized parameter".format(key))

        self._polar_parameters = dict(zip(polar_symbols, [0.0] * len(polar_symbols)))

        if polar_parameters is None:
            polar_parameters = {}

        polar_parameters.update(kwargs)
        self._set_polar_parameters(polar_parameters)

        num_tilts = len(tilt_angles_degrees)
        if initial_scan_positions is None:
            initial_scan_positions = [None] * num_tilts

        self._energy = energy
        self._num_slices = num_slices
        self._tilt_angles_deg = tilt_angles_degrees
        self._num_tilts = num_tilts
        self._semiangle_cutoff = semiangle_cutoff
        self._rolloff = rolloff
        self._vacuum_probe_intensity = vacuum_probe_intensity
        self._diffraction_intensities_shape = diffraction_intensities_shape
        self._reshaping_method = reshaping_method
        self._probe_roi_shape = probe_roi_shape
        self._object = initial_object_guess
        self._probe = initial_probe_guess
        self._scan_positions = initial_scan_positions
        self._datacube = datacube
        self._dp_mask = dp_mask
        self._verbose = verbose
        self._object_padding_px = object_padding_px
        self._preprocessed = False

    def _precompute_propagator_arrays(
        self,
        gpts: Tuple[int, int],
        sampling: Tuple[float, float],
        energy: float,
        slice_thicknesses: Sequence[float],
    ):
        """
        Precomputes propagator arrays complex wave-function will be convolved by,
        for all slice thicknesses.

        Parameters
        ----------
        gpts: Tuple[int,int]
            Wavefunction pixel dimensions
        sampling: Tuple[float,float]
            Wavefunction sampling in A
        energy: float
            The electron energy of the wave functions in eV
        slice_thicknesses: Sequence[float]
            Array of slice thicknesses in A

        Returns
        -------
        propagator_arrays: np.ndarray
            (T,Sx,Sy) shape array storing propagator arrays
        """
        xp = self._xp

        # Frequencies
        kx, ky = spatial_frequencies(gpts, sampling)
        kx = xp.asarray(kx)
        ky = xp.asarray(ky)

        # Antialias masks
        k = xp.sqrt(kx[:, None] ** 2 + ky[None] ** 2)
        kcut = 1 / max(sampling) / 2 * 2 / 3.0  # 2/3 cutoff
        antialias_mask = 0.5 * (
            1 + xp.cos(np.pi * (k - kcut + 0.1) / 0.1)
        )  # 0.1 rolloff
        antialias_mask[k > kcut] = 0.0
        antialias_mask = xp.where(k > kcut - 0.1, antialias_mask, xp.ones_like(k))

        # Propagators
        wavelength = electron_wavelength_angstrom(energy)
        num_slices = slice_thicknesses.shape[0]
        propagators = xp.empty((num_slices,) + k.shape, dtype=xp.complex64)
        for i, dz in enumerate(slice_thicknesses):
            propagators[i] = xp.exp(
                1.0j * (-(kx**2)[:, None] * np.pi * wavelength * dz)
            )
            propagators[i] *= xp.exp(
                1.0j * (-(ky**2)[None] * np.pi * wavelength * dz)
            )

        return propagators * antialias_mask

    def _propagate_array(self, array: np.ndarray, propagator_array: np.ndarray):
        """
        Propagates array by Fourier convolving array with propagator_array.

        Parameters
        ----------
        array: np.ndarray
            Wavefunction array to be convolved
        propagator_array: np.ndarray
            Propagator array to convolve array with

        Returns
        -------
        propagated_array: np.ndarray
            Fourier-convolved array
        """
        xp = self._xp

        return xp.fft.ifft2(xp.fft.fft2(array) * propagator_array)

    def _expand_or_project_sliced_object(self, array: np.ndarray, output_z):
        """
        Expands supersliced object or projects voxel-sliced object.

        Parameters
        ----------
        array: np.ndarray
            3D array to expand/project
        output_z: int
            Output_dimension to expand/project array to.
            If output_z > array.shape[0] array is expanded, else it's projected

        Returns
        -------
        expanded_or_projected_array: np.ndarray
            expanded or projected array
        """
        zoom = self._zoom
        input_z = array.shape[0]

        return (
            zoom(
                array,
                (output_z / input_z, 1, 1),
                order=0,
                mode="nearest",
                grid_mode=True,
            )
            * input_z
            / output_z
        )

    def preprocess(
        self,
        fit_function: str = "plane",
        plot_probe_overlaps: bool = True,
        rotation_real_space_degrees: float = None,
        diffraction_patterns_rotate_degrees: float = None,
        diffraction_patterns_transpose: bool = None,
        force_com_shifts: Sequence[float] = None,
        progress_bar: bool = True,
        **kwargs,
    ):
        """
        Ptychographic preprocessing step.

        Additionally, it initializes an (Px,Py, Py) array of 1.0
        and a complex probe using the specified polar parameters.

        Parameters
        ----------
        fit_function: str, optional
            2D fitting function for CoM fitting. One of 'plane','parabola','bezier_two'
        plot_probe_overlaps: bool, optional
            If True, initial probe overlaps scanned over the object will be displayed
        rotation_real_space_degrees: float (degrees), optional
            In plane rotation around z axis between x axis and tilt axis in
            real space (forced to be in xy plane)
        diffraction_patterns_rotate_degrees: float, optional
            Relative rotation angle between real and reciprocal space
        diffraction_patterns_transpose: bool, optional
            Whether diffraction intensities need to be transposed.
        force_com_shifts: list of tuple of ndarrays (CoMx, CoMy)
            Amplitudes come from diffraction patterns shifted with
            the CoM in the upper left corner for each probe unless
            shift is overwritten. One tuple per tilt.

        Returns
        --------
        self: OverlapTomographicReconstruction
            Self to accommodate chaining
        """
        xp = self._xp
        asnumpy = self._asnumpy

        # Prepopulate various arrays
        num_probes_per_tilt = [0]
        for dc in self._datacube:
            rx, ry = dc.Rshape
            num_probes_per_tilt.append(rx * ry)

        self._num_diffraction_patterns = sum(num_probes_per_tilt)
        self._cum_probes_per_tilt = np.cumsum(np.array(num_probes_per_tilt))

        self._mean_diffraction_intensity = []
        self._positions_px_all = np.empty((self._num_diffraction_patterns, 2))

        self._rotation_best_rad = np.deg2rad(diffraction_patterns_rotate_degrees)
        self._rotation_best_transpose = diffraction_patterns_transpose

        if force_com_shifts is None:
            force_com_shifts = [None] * self._num_tilts

        for tilt_index in tqdmnd(
            self._num_tilts,
            desc="Preprocessing data",
            unit="tilt",
            disable=not progress_bar,
        ):

            if tilt_index == 0:
                (
                    self._datacube[tilt_index],
                    self._vacuum_probe_intensity,
                    self._dp_mask,
                    force_com_shifts[tilt_index],
                ) = self._preprocess_datacube_and_vacuum_probe(
                    self._datacube[tilt_index],
                    diffraction_intensities_shape=self._diffraction_intensities_shape,
                    reshaping_method=self._reshaping_method,
                    probe_roi_shape=self._probe_roi_shape,
                    vacuum_probe_intensity=self._vacuum_probe_intensity,
                    dp_mask=self._dp_mask,
                    com_shifts=force_com_shifts[tilt_index],
                )

                self._amplitudes = xp.empty(
                    (self._num_diffraction_patterns,) + self._datacube[0].Qshape
                )
                self._region_of_interest_shape = np.array(
                    self._amplitudes[0].shape[-2:]
                )

            else:
                (
                    self._datacube[tilt_index],
                    _,
                    _,
                    force_com_shifts[tilt_index],
                ) = self._preprocess_datacube_and_vacuum_probe(
                    self._datacube[tilt_index],
                    diffraction_intensities_shape=self._diffraction_intensities_shape,
                    reshaping_method=self._reshaping_method,
                    probe_roi_shape=self._probe_roi_shape,
                    vacuum_probe_intensity=None,
                    dp_mask=None,
                    com_shifts=force_com_shifts[tilt_index],
                )

            intensities = self._extract_intensities_and_calibrations_from_datacube(
                self._datacube[tilt_index],
                require_calibrations=True,
            )

            (
                com_measured_x,
                com_measured_y,
                com_fitted_x,
                com_fitted_y,
                com_normalized_x,
                com_normalized_y,
            ) = self._calculate_intensities_center_of_mass(
                intensities,
                dp_mask=self._dp_mask,
                fit_function=fit_function,
                com_shifts=force_com_shifts[tilt_index],
            )

            (
                self._amplitudes[
                    self._cum_probes_per_tilt[tilt_index] : self._cum_probes_per_tilt[
                        tilt_index + 1
                    ]
                ],
                mean_diffraction_intensity_temp,
            ) = self._normalize_diffraction_intensities(
                intensities,
                com_fitted_x,
                com_fitted_y,
            )

            self._mean_diffraction_intensity.append(mean_diffraction_intensity_temp)

            del (
                intensities,
                com_measured_x,
                com_measured_y,
                com_fitted_x,
                com_fitted_y,
                com_normalized_x,
                com_normalized_y,
            )

            self._positions_px_all[
                self._cum_probes_per_tilt[tilt_index] : self._cum_probes_per_tilt[
                    tilt_index + 1
                ]
            ] = self._calculate_scan_positions_in_pixels(
                self._scan_positions[tilt_index]
            )

        # Object Initialization
        if self._object is None:
            pad_x, pad_y = self._object_padding_px
            p, q = np.max(self._positions_px_all, axis=0)
            p = np.max([np.round(p + pad_x), self._region_of_interest_shape[0]]).astype(
                "int"
            )
            q = np.max([np.round(q + pad_y), self._region_of_interest_shape[1]]).astype(
                "int"
            )
            self._object = xp.zeros((q, p, q), dtype=xp.float32)
        else:
            self._object = xp.asarray(self._object, dtype=xp.float32)

        self._object_initial = self._object.copy()
        self._object_shape = self._object.shape[-2:]
        self._num_voxels = self._object.shape[0]

        # Center Probes
        self._positions_px_all = xp.asarray(self._positions_px_all, dtype=xp.float32)

        for tilt_index in range(self._num_tilts):

            self._positions_px = self._positions_px_all[
                self._cum_probes_per_tilt[tilt_index] : self._cum_probes_per_tilt[
                    tilt_index + 1
                ]
            ]
            self._positions_px_com = xp.mean(self._positions_px, axis=0)
            self._positions_px -= (
                self._positions_px_com - xp.array(self._object_shape) / 2
            )

            self._positions_px_all[
                self._cum_probes_per_tilt[tilt_index] : self._cum_probes_per_tilt[
                    tilt_index + 1
                ]
            ] = self._positions_px.copy()

        self._positions_px_initial_all = self._positions_px_all.copy()
        self._positions_initial_all = self._positions_px_initial_all.copy()
        self._positions_initial_all[:, 0] *= self.sampling[0]
        self._positions_initial_all[:, 1] *= self.sampling[1]

        # Probe Initialization
        if self._probe is None:
            if self._vacuum_probe_intensity is not None:
                self._semiangle_cutoff = np.inf
                self._vacuum_probe_intensity = xp.asarray(self._vacuum_probe_intensity)
                probe_x0, probe_y0 = get_CoM(
                    self._vacuum_probe_intensity, device="cpu" if xp is np else "gpu"
                )
                shift_x = self._region_of_interest_shape[0] // 2 - probe_x0
                shift_y = self._region_of_interest_shape[1] // 2 - probe_y0
                self._vacuum_probe_intensity = get_shifted_ar(
                    self._vacuum_probe_intensity,
                    shift_x,
                    shift_y,
                    bilinear=True,
                    device="cpu" if xp is np else "gpu",
                )

            self._probe = (
                ComplexProbe(
                    gpts=self._region_of_interest_shape,
                    sampling=self.sampling,
                    energy=self._energy,
                    semiangle_cutoff=self._semiangle_cutoff,
                    rolloff=self._rolloff,
                    vacuum_probe_intensity=self._vacuum_probe_intensity,
                    parameters=self._polar_parameters,
                    device="cpu" if xp is np else "gpu",
                )
                .build()
                ._array
            )

        else:
            if isinstance(self._probe, ComplexProbe):
                if self._probe._gpts != self._region_of_interest_shape:
                    raise ValueError()
                if hasattr(self._probe, "_array"):
                    self._probe = self._probe._array
                else:
                    self._probe._xp = xp
                    self._probe = self._probe.build()._array
            else:
                self._probe = xp.asarray(self._probe, dtype=xp.complex64)

        # Normalize probe to match mean diffraction intensity
        probe_intensity = xp.sum(xp.abs(xp.fft.fft2(self._probe)) ** 2)
        self._probe *= np.sqrt(
            sum(self._mean_diffraction_intensity) / self._num_tilts / probe_intensity
        )

        self._probe_initial = self._probe.copy()
        self._probe_initial_fft_amplitude = xp.abs(xp.fft.fft2(self._probe_initial))

        # Precomputed propagator arrays
        self._slice_thicknesses = np.tile(
            self._object_shape[1] * self.sampling[1] / self._num_slices,
            self._num_slices,
        )
        self._propagator_arrays = self._precompute_propagator_arrays(
            self._region_of_interest_shape,
            self.sampling,
            self._energy,
            self._slice_thicknesses,
        )

        if plot_probe_overlaps:
            self._positions_px = self._positions_px_all[: self._cum_probes_per_tilt[1]]
            self._positions_px_fractional = self._positions_px - xp.round(
                self._positions_px
            )
            shifted_probes = fft_shift(self._probe, self._positions_px_fractional, xp)
            probe_intensities = xp.abs(shifted_probes) ** 2
            probe_overlap = self._sum_overlapping_patches_bincounts(probe_intensities)

            figsize = kwargs.get("figsize", (8, 4))
            cmap = kwargs.get("cmap", "Greys_r")
            kwargs.pop("figsize", None)
            kwargs.pop("cmap", None)

            extent = [
                0,
                self.sampling[1] * self._object_shape[1],
                self.sampling[0] * self._object_shape[0],
                0,
            ]

            probe_extent = [
                0,
                self.sampling[1] * self._region_of_interest_shape[1],
                self.sampling[0] * self._region_of_interest_shape[0],
                0,
            ]

            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=figsize)

            ax1.imshow(
                asnumpy(xp.abs(self._probe) ** 2),
                extent=probe_extent,
                cmap=cmap,
                **kwargs,
            )
            ax1.set_ylabel("x [A]")
            ax1.set_xlabel("y [A]")
            ax1.set_title("Initial Probe Intensity")

            ax2.imshow(
                asnumpy(probe_overlap),
                extent=extent,
                cmap=cmap,
                **kwargs,
            )
            ax2.scatter(
                self.positions[:, 1],
                self.positions[:, 0],
                s=2.5,
                color=(1, 0, 0, 1),
            )
            ax2.set_ylabel("x [A]")
            ax2.set_xlabel("y [A]")
            ax2.set_xlim((extent[0], extent[1]))
            ax2.set_ylim((extent[2], extent[3]))
            ax2.set_title("Object Field of View")

            fig.tight_layout()

        self._preprocessed = True

        return self

    def _overlap_projection(self, current_object, current_probe):
        """
        Ptychographic overlap projection method.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate

        Returns
        --------
        propagated_probes: np.ndarray
            Prop[object^n*probe^n]
        object_patches: np.ndarray
            Patched object view
        overlap: np.ndarray
            object_patches^n * propagated_probes^n
        """

        xp = self._xp

        shifted_probes = fft_shift(current_probe, self._positions_px_fractional, xp)

        complex_object = xp.exp(1j*current_object)
        object_patches = complex_object[
            :, self._vectorized_patch_indices_row, self._vectorized_patch_indices_col
        ]

        propagated_probes = xp.empty_like(object_patches)
        overlap = xp.empty_like(object_patches)
        propagated_probes[0] = shifted_probes

        for s in range(self._num_slices):
            overlap[s] = object_patches[s] * propagated_probes[s]

            if s + 1 < self._num_slices:
                propagated_probes[s+1] = self._propagate_array(
                    overlap[s] , self._propagator_arrays[s]
                )

        return propagated_probes, object_patches, overlap

    def _gradient_descent_fourier_projection(self, amplitudes, overlap):
        """
        Ptychographic fourier projection method for GD method.

        Parameters
        --------
        amplitudes: np.ndarray
            Normalized measured amplitudes
        propagated_probes: np.ndarray
            object_patches^n * propagated_probes^n

        Returns
        --------
        exit_waves:np.ndarray
            Updated exit wave difference
            Note: this function only increments the last slice
        error: float
            Reconstruction error
        """

        xp = self._xp
        fourier_overlap = xp.fft.fft2(overlap[-1])

        error = (
            xp.mean(xp.abs(amplitudes - xp.abs(fourier_overlap)) ** 2)
            / self._mean_diffraction_intensity[self._active_tilt_index]
        )

        modified_exit_wave = xp.fft.ifft2(
            amplitudes * xp.exp(1j * xp.angle(fourier_overlap))
        )

        #exit_waves = xp.zeros_like(overlap)
        #exit_waves[-1] = modified_exit_wave - overlap[-1]
        exit_waves = -overlap.copy()
        exit_waves[-1] += modified_exit_wave

        return exit_waves, error

    def _projection_sets_fourier_projection(
        self, amplitudes, propagated_probes, exit_waves, projection_a, projection_b, projection_c
    ):
        """
        Ptychographic fourier projection method for DM_AP and RAAR methods.
        Generalized projection using three parameters: a,b,c

            DM_AP(\alpha)   :   a =  -\alpha, b = 1, c = 1 + \alpha
              DM: DM_AP(1.0), AP: DM_AP(0.0)

            RAAR(\beta)     :   a = 1-2\beta, b = \beta, c = 2
              DM : RAAR(1.0)

            RRR(\gamma)     :   a = -\gamma, b = \gamma, c = 2
              DM: RRR(1.0)

            SUPERFLIP       :   a = 0, b = 1, c = 2

        Parameters
        --------
        amplitudes: np.ndarray
            Normalized measured amplitudes
        propagated_probes: np.ndarray
            object_patches^n * propagated_probes^n
        exit_waves: np.ndarray
            previously estimated exit waves
        projection_a: float
        projection_b: float
        projection_c: float

        Returns
        --------
        exit_waves:np.ndarray
            Updated exit wave difference
            Note: this function only affects the last slice
        error: float
            Reconstruction error
        """

        xp = self._xp
        projection_x = 1 - projection_a - projection_b
        projection_y = 1 - projection_c

        if exit_waves is None:
            exit_waves = propagated_probes.copy()

        fourier_overlap = xp.fft.fft2(propagated_probes[-1])
        error = (
            xp.mean(xp.abs(amplitudes**2 - xp.abs(fourier_overlap) ** 2))
            / self._mean_diffraction_intensity[self._active_tilt_index]
        )

        factor_to_be_projected = (
            projection_c * propagated_probes[-1] + projection_y * exit_waves[-1]
        )
        fourier_projected_factor = xp.fft.fft2(factor_to_be_projected)

        fourier_projected_factor = amplitudes * xp.exp(
            1j * xp.angle(fourier_projected_factor)
        )
        projected_factor = xp.fft.ifft2(fourier_projected_factor)

        exit_waves[-1] = (
            projection_x * exit_waves[-1]
            + projection_a * overlap[-1]
            + projection_b * projected_factor
        )

        return exit_waves, error

    def _forward(
        self,
        current_object,
        current_probe,
        amplitudes,
        exit_waves,
        use_projection_scheme,
        projection_a,
        projection_b,
        projection_c,
    ):
        """
        Ptychographic forward operator.
        Calls _overlap_projection() and the appropriate _fourier_projection().

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate
        amplitudes: np.ndarray
            Normalized measured amplitudes
        exit_waves: np.ndarray
            previously estimated exit waves
        use_projection_scheme: bool,
            If True, use generalized projection update
        projection_a: float
        projection_b: float
        projection_c: float

        Returns
        --------
        propagated_probes:np.ndarray
            Prop[object^n*probe^n]
        object_patches: np.ndarray
            Patched object view
        exit_waves:np.ndarray
            Updated exit_waves
        error: float
            Reconstruction error
        """

        propagated_probes, object_patches, overlap = self._overlap_projection(
            current_object, current_probe
        )
        if use_projection_scheme:
            exit_waves, error = self._projection_sets_fourier_projection(
                amplitudes,
                propagated_probes,
                exit_waves,
                projection_a,
                projection_b,
                projection_c,
            )

        else:
            exit_waves, error = self._gradient_descent_fourier_projection(
                amplitudes, overlap
            )

        return propagated_probes, object_patches, overlap, exit_waves, error

    def _gradient_descent_adjoint(
        self,
        current_object,
        current_probe,
        object_patches,
        propagated_probes,
        exit_waves,
        step_size,
        normalization_min,
        fix_probe,
    ):
        """
        Ptychographic adjoint operator for GD method.
        Computes object and probe update steps.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate
        object_patches: np.ndarray
            Patched object view
        propagated_probes:np.ndarray
            Prop[object^n*probe^n]
        exit_waves:np.ndarray
            Updated exit_waves
        step_size: float, optional
            Update step size
        normalization_min: float, optional
            Probe normalization minimum as a fraction of the maximum overlap intensity
        fix_probe: bool, optional
            If True, probe will not be updated

        Returns
        --------
        updated_object: np.ndarray
            Updated object estimate
        updated_probe: np.ndarray
            Updated probe estimate
        """
        xp = self._xp

        for s in reversed(range(self._num_slices)):
            obj = object_patches[s]
            probe = propagated_probes[s]
            exit_wave = exit_waves[s]

            probe_normalization = self._sum_overlapping_patches_bincounts(
                xp.abs(probe) ** 2
            )
            probe_normalization = 1 / xp.sqrt(
                1e-16
                + ((1 - normalization_min) * probe_normalization) ** 2
                + (normalization_min * xp.max(probe_normalization)) ** 2
            )

            current_object[s] += step_size * (
                self._sum_overlapping_patches_bincounts(
                    xp.real(
                        - 1j
                        * xp.conj(obj)
                        * xp.conj(probe) 
                        * exit_wave
                        )
                    )
                * probe_normalization
            )

            if s > 0:
                #object_normalization = xp.abs(obj) ** 2
                #object_normalization = 1 / xp.sqrt(
                #    1e-16
                #    + ((1 - normalization_min) * object_normalization) ** 2
                #    + (
                #        normalization_min
                #        * xp.max(object_normalization, axis=(-1, -2))[:, None, None]
                #    )
                #    ** 2
                #)

                probe +=  xp.conj(obj) * exit_wave # * object_normalization
                exit_waves[s - 1] += self._propagate_array(
                    probe, xp.conj(self._propagator_arrays[s - 1])
                )
            else:
                object_normalization = xp.sum(
                    (xp.abs(obj) ** 2),
                    axis=0,
                )
                object_normalization = 1 / xp.sqrt(
                    1e-16
                    + ((1 - normalization_min) * object_normalization) ** 2
                    + (normalization_min * xp.max(object_normalization)) ** 2
                )

                current_probe += (
                    step_size
                    * xp.sum(
                        xp.conj(obj) * exit_wave,
                        axis=0,
                    )
                    * object_normalization
                )

        return current_object, current_probe

    def _projection_sets_adjoint(
        self,
        current_object,
        current_probe,
        object_patches,
        propagated_probes,
        exit_waves,
        normalization_min,
        fix_probe,
    ):
        """
        Ptychographic adjoint operator for DM_AP and RAAR methods.
        Computes object and probe update steps.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate
        object_patches: np.ndarray
            Patched object view
        propagated_probes:np.ndarray
            Prop[object^n*probe^n]
        exit_waves:np.ndarray
            Updated exit_waves
        normalization_min: float, optional
            Probe normalization minimum as a fraction of the maximum overlap intensity
        fix_probe: bool, optional
            If True, probe will not be updated

        Returns
        --------
        updated_object: np.ndarray
            Updated object estimate
        updated_probe: np.ndarray
            Updated probe estimate
        """
        xp = self._xp

        for s in reversed(range(self._num_slices)):
            exit_wave = exit_waves[s]
            probe = propagated_probes[s]
            obj = object_patches[s]

            probe_normalization = self._sum_overlapping_patches_bincounts(
                xp.abs(probe) ** 2
            )
            probe_normalization = 1 / xp.sqrt(
                1e-16
                + ((1 - normalization_min) * probe_normalization) ** 2
                + (normalization_min * xp.max(probe_normalization)) ** 2
            )

            current_object[s] = xp.real(
                self._sum_overlapping_patches_bincounts(
                    -1j
                    * xp.conj(obj)
                    * xp.conj(probe)
                    * exit_wave
                )
                * probe_normalization
            )

            if not fix_probe:
                if s > 0:
                    object_normalization = xp.abs(obj) ** 2
                    object_normalization = 1 / xp.sqrt(
                        1e-16
                        + ((1 - normalization_min) * object_normalization) ** 2
                        + (
                            normalization_min
                            * xp.max(object_normalization, axis=(-1, -2))[:, None, None]
                        )
                        ** 2
                    )

                    probe = xp.conj(obj) * exit_wave * object_normalization
                    exit_waves[s - 1] = self._propagate_array(
                        probe, xp.conj(self._propagator_arrays[s - 1])
                    )
                else:
                    object_normalization = xp.sum(
                        (xp.abs(obj) ** 2),
                        axis=0,
                    )
                    object_normalization = 1 / xp.sqrt(
                        1e-16
                        + ((1 - normalization_min) * object_normalization) ** 2
                        + (normalization_min * xp.max(object_normalization)) ** 2
                    )

                    current_probe = (
                        xp.sum(
                            xp.conj(obj) * exit_wave,
                            axis=0,
                        )
                        * object_normalization
                    )

        return current_object, current_probe

    def _adjoint(
        self,
        current_object,
        current_probe,
        object_patches,
        propagated_probes,
        exit_waves,
        use_projection_scheme: bool,
        step_size: float,
        normalization_min: float,
        fix_probe: bool,
    ):
        """
        Ptychographic adjoint operator for GD method.
        Computes object and probe update steps.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate
        object_patches: np.ndarray
            Patched object view
        propagated_probes:np.ndarray
            fractionally-shifted probes
        exit_waves:np.ndarray
            Updated exit_waves
        step_size: float, optional
            Update step size
        normalization_min: float, optional
            Probe normalization minimum as a fraction of the maximum overlap intensity
        fix_probe: bool, optional
            If True, probe will not be updated

        Returns
        --------
        updated_object: np.ndarray
            Updated object estimate
        updated_probe: np.ndarray
            Updated probe estimate
        """

        if use_projection_scheme:
            current_object, current_probe = self._projection_sets_adjoint(
                current_object,
                current_probe,
                object_patches,
                propagated_probes,
                exit_waves,
                normalization_min,
                fix_probe,
            )
        else:
            current_object, current_probe = self._gradient_descent_adjoint(
                current_object,
                current_probe,
                object_patches,
                propagated_probes,
                exit_waves,
                step_size,
                normalization_min,
                fix_probe,
            )

        return current_object, current_probe

    def _object_positivity_constraint(self, current_object):
        """
        Ptychographic positivity constraint.
        Used to ensure electrostatic potential is positive.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate

        Returns
        --------
        constrained_object: np.ndarray
            Constrained object estimate
        """
        xp = self._xp
        return xp.maximum(current_object, 0.0)
    
    def _object_gaussian_constraint(self, current_object, gaussian_filter_sigma):
        """
        Ptychographic smoothness constraint.
        Used for blurring object.

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        gaussian_filter_sigma: float
            Standard deviation of gaussian kernel

        Returns
        --------
        constrained_object: np.ndarray
            Constrained object estimate
        """
        gaussian_filter = self._gaussian_filter

        current_object = gaussian_filter(current_object, gaussian_filter_sigma)

        return current_object

    def _object_butterworth_constraint(self, current_object, q_lowpass, q_highpass):
        """
        Butterworth filter

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        q_lowpass: float
            Cut-off frequency in A^-1 for low-pass butterworth filter
        q_highpass: float
            Cut-off frequency in A^-1 for high-pass butterworth filter

        Returns
        --------
        constrained_object: np.ndarray
            Constrained object estimate
        """
        xp = self._xp
        qz = xp.fft.fftfreq(current_object.shape[0], self.sampling[1])
        qx = xp.fft.fftfreq(current_object.shape[1], self.sampling[0])
        qy = xp.fft.fftfreq(current_object.shape[2], self.sampling[1])
        qza, qxa, qya = xp.meshgrid(qz, qx, qy, indexing="ij")
        qra = xp.sqrt(qza**2 + qxa**2 + qya**2 )

        env = xp.ones_like(qra)
        if q_highpass:
            env *= 1 - 1 / (1 + (qra / q_highpass) ** 4)
        if q_lowpass:
            env *= 1 / (1 + (qra / q_lowpass) ** 4)

        current_object = xp.real(xp.fft.ifftn(xp.fft.fftn(current_object) * env))
        return current_object

    def _constraints(
        self,
        current_object,
        current_probe,
        current_positions,
        fix_com,
        fix_probe_fourier_amplitude,
        fix_positions,
        global_affine_transformation,
        gaussian_filter,
        gaussian_filter_sigma,
        butterworth_filter,
        q_lowpass,
        q_highpass,
    ):
        """
        Ptychographic constraints operator.
        Calls _threshold_object_constraint() and _probe_center_of_mass_constraint()

        Parameters
        --------
        current_object: np.ndarray
            Current object estimate
        current_probe: np.ndarray
            Current probe estimate
        current_positions: np.ndarray
            Current positions estimate
        fix_com: bool
            If True, probe CoM is fixed to the center
        fix_probe_fourier_amplitude: bool
            If True, probe fourier amplitude is set to initial probe
        fix_positions: bool
            If True, positions are not updated
        gaussian_filter: bool
            If True, applies real-space gaussian filter
        gaussian_filter_sigma: float
            Standard deviation of gaussian kernel
        butterworth_filter: bool
            If True, applies fourier-space butterworth filter
        q_lowpass: float
            Cut-off frequency in A^-1 for low-pass butterworth filter
        q_highpass: float
            Cut-off frequency in A^-1 for high-pass butterworth filter

        Returns
        --------
        constrained_object: np.ndarray
            Constrained object estimate
        constrained_probe: np.ndarray
            Constrained probe estimate
        constrained_positions: np.ndarray
            Constrained positions estimate
        """

        if gaussian_filter:
            current_object = self._object_gaussian_constraint(
                current_object, gaussian_filter_sigma
            )

        if butterworth_filter:
            current_object = self._object_butterworth_constraint(
                current_object,
                q_lowpass,
                q_highpass,
            )

        current_object = self._object_positivity_constraint(current_object)

        if fix_probe_fourier_amplitude:
            current_probe = self._probe_fourier_amplitude_constraint(current_probe)

        current_probe = self._probe_finite_support_constraint(current_probe)

        if fix_com:
            current_probe = self._probe_center_of_mass_constraint(current_probe)

        if not fix_positions:
            current_positions = self._positions_center_of_mass_constraint(
                current_positions
            )

            if global_affine_transformation:
                current_positions = self._positions_affine_transformation_constraint(
                    self._positions_px_initial, current_positions
                )

        return current_object, current_probe, current_positions

    def reconstruct(
        self,
        max_iter: int = 64,
        reconstruction_method: str = "gradient-descent",
        reconstruction_parameter: float = 1.0,
        max_batch_size: int = None,
        seed_random: int = None,
        step_size: float = 0.9,
        normalization_min: float = 1.0,
        positions_step_size: float = 0.9,
        fix_com: bool = True,
        fix_probe_iter: int = 0,
        fix_probe_fourier_amplitude_iter: int = 0,
        fix_positions_iter: int = np.inf,
        global_affine_transformation: bool = True,
        probe_support_relative_radius: float = 1.0,
        probe_support_supergaussian_degree: float = 10.0,
        gaussian_filter_sigma: float = None,
        gaussian_filter_iter: int = np.inf,
        butterworth_filter_iter: int = np.inf,
        q_lowpass: float = None,
        q_highpass: float = None,
        kz_regularization_filter_iter: int = np.inf,
        kz_regularization_gamma: float = None,
        store_iterations: bool = False,
        progress_bar: bool = True,
        reset: bool = None,
    ):
        """
        Ptychographic reconstruction main method.

        Parameters
        --------
        max_iter: int, optional
            Maximum number of iterations to run
        reconstruction_method: str, optional
            Specifies which reconstruction algorithm to use, one of:
            "generalized-projection",
            "DM_AP" (or "difference-map_alternating-projections"),
            "RAAR" (or "relaxed-averaged-alternating-reflections"),
            "RRR" (or "relax-reflect-reflect"),
            "SUPERFLIP" (or "charge-flipping"), or
            "GD" (or "gradient_descent")
        reconstruction_parameter: float, optional
            Reconstruction parameter for various reconstruction methods above.
        reconstruction_parameter: float, optional
            Tuning parameter to interpolate b/w DM-AP and DM-RAAR
        max_batch_size: int, optional
            Max number of probes to update at once
        seed_random: int, optional
            Seeds the random number generator, only applicable when max_batch_size is not None
        step_size: float, optional
            Update step size
        normalization_min: float, optional
            Probe normalization minimum as a fraction of the maximum overlap intensity
        positions_step_size: float, optional
            Positions update step size
        fix_com: bool, optional
            If True, fixes center of mass of probe
        fix_probe_iter: int, optional
            Number of iterations to run with a fixed probe before updating probe estimate
        fix_probe_amplitude: int, optional
            Number of iterations to run with a fixed probe amplitude
        fix_positions_iter: int, optional
            Number of iterations to run with fixed positions before updating positions estimate
        global_affine_transformation: bool, optional
            If True, positions are assumed to be a global affine transform from initial scan
        probe_support_relative_radius: float, optional
            Radius of probe supergaussian support in scaled pixel units, between (0,1]
        probe_support_supergaussian_degree: float, optional
            Degree supergaussian support is raised to, higher is sharper cutoff
        gaussian_filter_sigma: float, optional
            Standard deviation of gaussian kernel
        gaussian_filter_iter: int, optional
            Number of iterations to run using object smoothness constraint
        butterworth_filter_iter: int, optional
            Number of iterations to run using high-pass butteworth filter
        q_lowpass: float
            Cut-off frequency in A^-1 for low-pass butterworth filter
        q_highpass: float
            Cut-off frequency in A^-1 for high-pass butterworth filter
        kz_regularization_filter_iter: int, optional
            Number of iterations to run using kz regularization filter
        kz_regularization_gamma, float, optional
            kz regularization strength
        store_iterations: bool, optional
            If True, reconstructed objects and probes are stored at each iteration
        progress_bar: bool, optional
            If True, reconstruction progress is displayed
        reset: bool, optional
            If True, previous reconstructions are ignored

        Returns
        --------
        self: MultislicePtychographicReconstruction
            Self to accommodate chaining
        """
        asnumpy = self._asnumpy
        xp = self._xp

        # Reconstruction method

        if reconstruction_method == "generalized-projection":
            if np.array(reconstruction_parameter).shape != (3,):
                raise ValueError(
                    (
                        "reconstruction_parameter must be a list of three numbers "
                        "when using `reconstriction_method`=generalized-projection."
                    )
                )

            use_projection_scheme = True
            projection_a, projection_b, projection_c = reconstruction_parameter
            step_size = None
        elif (
            reconstruction_method == "DM_AP"
            or reconstruction_method == "difference-map_alternating-projections"
        ):
            if reconstruction_parameter < 0.0 or reconstruction_parameter > 1.0:
                raise ValueError("reconstruction_parameter must be between 0-1.")

            use_projection_scheme = True
            projection_a = -reconstruction_parameter
            projection_b = 1
            projection_c = 1 + reconstruction_parameter
            step_size = None
        elif (
            reconstruction_method == "RAAR"
            or reconstruction_method == "relaxed-averaged-alternating-reflections"
        ):
            if reconstruction_parameter < 0.0 or reconstruction_parameter > 1.0:
                raise ValueError("reconstruction_parameter must be between 0-1.")

            use_projection_scheme = True
            projection_a = 1 - 2 * reconstruction_parameter
            projection_b = reconstruction_parameter
            projection_c = 2
            step_size = None
        elif (
            reconstruction_method == "RRR"
            or reconstruction_method == "relax-reflect-reflect"
        ):
            if reconstruction_parameter < 0.0 or reconstruction_parameter > 2.0:
                raise ValueError("reconstruction_parameter must be between 0-2.")

            use_projection_scheme = True
            projection_a = -reconstruction_parameter
            projection_b = reconstruction_parameter
            projection_c = 2
            step_size = None
        elif (
            reconstruction_method == "SUPERFLIP"
            or reconstruction_method == "charge-flipping"
        ):
            use_projection_scheme = True
            projection_a = 0
            projection_b = 1
            projection_c = 2
            reconstruction_parameter = None
            step_size = None
        elif (
            reconstruction_method == "GD" or reconstruction_method == "gradient-descent"
        ):
            use_projection_scheme = False
            projection_a = None
            projection_b = None
            projection_c = None
            reconstruction_parameter = None
        else:
            raise ValueError(
                (
                    "reconstruction_method must be one of 'DM_AP' (or 'difference-map_alternating-projections'), "
                    "'RAAR' (or 'relaxed-averaged-alternating-reflections'), "
                    "'RRR' (or 'relax-reflect-reflect'), "
                    "'SUPERFLIP' (or 'charge-flipping'), "
                    f"or 'GD' (or 'gradient-descent'), not  {reconstruction_method}."
                )
            )

        if self._verbose:
            if max_batch_size is not None:
                if use_projection_scheme:
                    raise ValueError(
                        (
                            "Stochastic object/probe updating is inconsistent with 'DM_AP', 'RAAR', 'RRR', and 'SUPERFLIP'. "
                            "Use reconstruction_method='GD' or set max_batch_size=None."
                        )
                    )
                else:
                    print(
                        (
                            f"Performing {max_iter} iterations using the {reconstruction_method} algorithm, "
                            f"with normalization_min: {normalization_min} and step _size: {step_size}, "
                            f"in batches of max {max_batch_size} measurements."
                        )
                    )
            else:
                if reconstruction_parameter is not None:
                    if np.array(reconstruction_parameter).shape == (3,):
                        print(
                            (
                                f"Performing {max_iter} iterations using the {reconstruction_method} algorithm, "
                                f"with normalization_min: {normalization_min} and (a,b,c): {reconstruction_parameter}."
                            )
                        )
                    else:
                        print(
                            (
                                f"Performing {max_iter} iterations using the {reconstruction_method} algorithm, "
                                f"with normalization_min: {normalization_min} and α: {reconstruction_parameter}."
                            )
                        )
                else:
                    if step_size is not None:
                        print(
                            (
                                f"Performing {max_iter} iterations using the {reconstruction_method} algorithm, "
                                f"with normalization_min: {normalization_min}."
                            )
                        )
                    else:
                        print(
                            (
                                f"Performing {max_iter} iterations using the {reconstruction_method} algorithm, "
                                f"with normalization_min: {normalization_min} and step _size: {step_size}."
                            )
                        )

        # Batching

        if max_batch_size is not None:
            xp.random.seed(seed_random)
        else:
            max_batch_size = self._num_diffraction_patterns

        # initialization
        if store_iterations and (not hasattr(self, "object_iterations") or reset):
            self.object_iterations = []
            self.probe_iterations = []
            self.error_iterations = []

        if reset:
            self._object = self._object_initial.copy()
            self._probe = self._probe_initial.copy()
            self._positions_px_all = self._positions_px_initial_all.copy()

            self._exit_waves = None

        elif reset is None:
            if hasattr(self, "error"):
                warnings.warn(
                    (
                        "Continuing reconstruction from previous result. "
                        "Use reset=True for a fresh start."
                    ),
                    UserWarning,
                )
            else:
                self._exit_waves = None

        # Probe support mask initialization
        x = xp.linspace(-1, 1, self._region_of_interest_shape[0], endpoint=False)
        y = xp.linspace(-1, 1, self._region_of_interest_shape[1], endpoint=False)
        xx, yy = xp.meshgrid(x, y, indexing="ij")
        self._probe_support_mask = xp.exp(
            -(
                (
                    (xx / probe_support_relative_radius) ** 2
                    + (yy / probe_support_relative_radius) ** 2
                )
                ** probe_support_supergaussian_degree
            )
        )

        # main loop
        for a0 in tqdmnd(
            max_iter,
            desc="Reconstructing object and probe",
            unit=" iter",
            disable=not progress_bar,
        ):

            error = 0.0

            for tilt_index in range(self._num_tilts):
                self._active_tilt_index = tilt_index

                self._object = self._rotate(
                    self._object,
                    self._tilt_angles_deg[self._active_tilt_index],
                    axes=(0, 2),
                    reshape=False,
                    order=2,
                )
                
                #self._object = fourier_rotate_real_volume(
                #    self._object,
                #    self._tilt_angles_deg[self._active_tilt_index],
                #    axes=(0, 2),
                #    xp = self._xp,
                #)
                self._object_sliced = self._expand_or_project_sliced_object(
                    self._object, self._num_slices
                )

                self._object_sliced_old = self._object_sliced.copy()

                start_tilt = self._cum_probes_per_tilt[self._active_tilt_index]
                end_tilt = self._cum_probes_per_tilt[self._active_tilt_index + 1]

                num_diffraction_patterns = end_tilt - start_tilt
                shuffled_indices = np.arange(num_diffraction_patterns)
                unshuffled_indices = np.zeros_like(shuffled_indices)

                # randomize
                if not use_projection_scheme:
                    np.random.shuffle(shuffled_indices)
                unshuffled_indices[shuffled_indices] = np.arange(
                    num_diffraction_patterns
                )

                positions_px = self._positions_px_all[start_tilt:end_tilt].copy()[
                    shuffled_indices
                ]

                initial_positions_px = self._positions_px_initial_all[
                    start_tilt:end_tilt
                ].copy()[shuffled_indices]

                for start, end in generate_batches(
                    num_diffraction_patterns, max_batch=max_batch_size
                ):
                    # batch indices
                    self._positions_px = positions_px[start:end]
                    self._positions_px_com = xp.mean(self._positions_px, axis=0)
                    self._positions_px_initial = initial_positions_px[start:end]
                    self._positions_px_fractional = self._positions_px - xp.round(
                        self._positions_px
                    )

                    (
                        self._vectorized_patch_indices_row,
                        self._vectorized_patch_indices_col,
                    ) = self._extract_vectorized_patch_indices()

                    amplitudes = self._amplitudes[start_tilt:end_tilt][
                        shuffled_indices[start:end]
                    ]


                    # forward operator
                    (
                        propagated_probes,
                        object_patches,
                        overlap,
                        self._exit_waves,
                        batch_error,
                    ) = self._forward(
                        self._object_sliced,
                        self._probe,
                        amplitudes,
                        self._exit_waves,
                        use_projection_scheme,
                        projection_a,
                        projection_b,
                        projection_c,
                    )

                    # adjoint operator
                    self._object_sliced, self._probe = self._adjoint(
                        self._object_sliced,
                        self._probe,
                        object_patches,
                        propagated_probes,
                        self._exit_waves,
                        use_projection_scheme=use_projection_scheme,
                        step_size=step_size,
                        normalization_min=normalization_min,
                        fix_probe=a0 < fix_probe_iter,
                    )

                    # position correction
                    if a0 >= fix_positions_iter:
                        positions_px[start:end] = self._position_correction(
                            self._object_sliced[-1],
                            propagated_probes[-1],
                            overlap[-1],
                            amplitudes,
                            self._positions_px,
                            positions_step_size,
                        )

                    error += batch_error
                
                self._object += self._expand_or_project_sliced_object(
                        self._object_sliced - self._object_sliced_old, self._num_voxels
                            )
                self._object = self._rotate(
                    self._object,
                    -self._tilt_angles_deg[self._active_tilt_index],
                    axes=(0, 2),
                    reshape=False,
                    order=2,
                )
                
                #self._object = fourier_rotate_real_volume(
                #    self._object,
                #    -self._tilt_angles_deg[self._active_tilt_index],
                #    axes=(0, 2),
                #    xp = self._xp,
                #)
                
                # constraints
                self._positions_px_all[start_tilt:end_tilt] = positions_px.copy()[
                    unshuffled_indices
                ]
                
                (
                    self._object,
                    self._probe,
                    self._positions_px_all[start_tilt:end_tilt],
                ) = self._constraints(
                    self._object,
                    self._probe,
                    self._positions_px_all[start_tilt:end_tilt],
                    fix_com=fix_com and a0 >= fix_probe_iter,
                    fix_probe_fourier_amplitude=a0 < fix_probe_fourier_amplitude_iter,
                    fix_positions=a0 < fix_positions_iter,
                   global_affine_transformation=global_affine_transformation,
                    gaussian_filter=a0 < gaussian_filter_iter
                    and gaussian_filter_sigma is not None,
                    gaussian_filter_sigma=gaussian_filter_sigma,
                    butterworth_filter=a0 < butterworth_filter_iter
                    and (q_lowpass is not None or q_highpass is not None),
                    q_lowpass=q_lowpass,
                    q_highpass=q_highpass,
                )

            if store_iterations:
                self.object_iterations.append(asnumpy(self._object.copy()))
                self.probe_iterations.append(asnumpy(self._probe.copy()))
                self.error_iterations.append(error.item())

        # store result
        self.object = asnumpy(self._object)
        self.probe = asnumpy(self._probe)
        self.error = error.item()

        return self

    def visualize(self):
        pass
