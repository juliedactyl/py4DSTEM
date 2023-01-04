# stdlib
import os
import tempfile
from time import time

# 3rd party
import numpy as np
import dill

# local
import py4DSTEM
from py4DSTEM.process.diskdetection import PointListArray


def _find_Bragg_disks_single_DP_FK(DP, probe_kernel_FT,
                                   corrPower=1,
                                   sigma=2,
                                   edgeBoundary=20,
                                   minRelativeIntensity=0.005,
                                   minAbsoluteIntensity=0,
                                   relativeToPeak=0,
                                   minPeakSpacing=60,
                                   maxNumPeaks=70,
                                   subpixel='multicorr',
                                   upsample_factor=16,
                                   filter_function=None,
                                   return_cc=False,
                                   peaks=None):
    """
    Mirror of diskdetection.find_Bragg_disks_single_DP_FK with explicit imports for
    remote execution.

    Finds the Bragg disks in DP by cross, hybrid, or phase correlation with
    probe_kernel_FT.

    After taking the cross/hybrid/phase correlation, a gaussian smoothing is applied
    with standard deviation sigma, and all local maxima are found. Detected peaks within
    edgeBoundary pixels of the diffraction plane edges are then discarded. Next, peaks
    with intensities less than minRelativeIntensity of the brightest peak in the
    correaltion are discarded. Then peaks which are within a distance of minPeakSpacing
    of their nearest neighbor peak are found, and in each such pair the peak with the
    lesser correlation intensities is removed. Finally, if the number of peaks remaining
    exceeds maxNumPeaks, only the maxNumPeaks peaks with the highest correlation
    intensity are retained.

    IMPORTANT NOTE: the argument probe_kernel_FT is related to the probe kernels
    generated by functions like get_probe_kernel() by:

        >>> probe_kernel_FT = np.conj(np.fft.fft2(probe_kernel))

    if this function is simply passed a probe kernel, the results will not be meaningful!
    To run on a single DP while passing the real space probe kernel as an argument, use
    find_Bragg_disks_single_DP().

    Args:
        DP (ndarray): a diffraction pattern
        probe_kernel_FT (ndarray): the vacuum probe template, in Fourier space. Related
            to the real space probe kernel by probe_kernel_FT = F(probe_kernel)*, where
            F indicates a Fourier Transform and * indicates complex conjugation.
        corrPower (float between 0 and 1, inclusive): the cross correlation power. A
            value of 1 corresponds to a cross correaltion, and 0 corresponds to a
            phase correlation, with intermediate values giving various hybrids.
        sigma (float): the standard deviation for the gaussian smoothing applied to
            the cross correlation
        edgeBoundary (int): minimum acceptable distance from the DP edge, in pixels
        minRelativeIntensity (float): the minimum acceptable correlation peak intensity,
            relative to the intensity of the relativeToPeak'th peak
        relativeToPeak (int): specifies the peak against which the minimum relative
            intensity is measured -- 0=brightest maximum. 1=next brightest, etc.
        minPeakSpacing (float): the minimum acceptable spacing between detected peaks
        maxNumPeaks (int): the maximum number of peaks to return
        subpixel (str): Whether to use subpixel fitting, and which algorithm to use.
            Must be in ('none','poly','multicorr').
                * 'none': performs no subpixel fitting
                * 'poly': polynomial interpolation of correlogram peaks (default)
                * 'multicorr': uses the multicorr algorithm with DFT upsampling
        upsample_factor (int): upsampling factor for subpixel fitting (only used when
            subpixel='multicorr')
        filter_function (callable): filtering function to apply to each diffraction
            pattern before peakfinding.  Must be a function of only one argument (the
            diffraction pattern) and return the filtered diffraction pattern. The shape
            of the returned DP must match the shape of the probe kernel (but does not
            need to match the shape of the input diffraction pattern, e.g. the filter
            can be used to bin the diffraction pattern). If using distributed disk
            detection, the function must be able to be pickled with by dill.
        return_cc (bool): if True, return the cross correlation
        peaks (PointList): For internal use. If peaks is None, the PointList of peak
            positions is created here. If peaks is not None, it is the PointList that
            detected peaks are added to, and must have the appropriate coords
            ('qx','qy','intensity').

    Returns:
        (PointList) the Bragg peak positions and correlation intensities
    """
    assert subpixel in ['none', 'poly', 'multicorr'], \
        "Unrecognized subpixel option {}, subpixel must be 'none', 'poly', or 'multicorr'".format(subpixel)

    import numpy
    import scipy.ndimage.filters
    import py4DSTEM.process.utils.multicorr

    # apply filter function:
    DP = DP if filter_function is None else filter_function(DP)

    if subpixel == 'none':
        cc = py4DSTEM.process.utils.get_cross_correlation_fk(DP, probe_kernel_FT, corrPower)
        cc = numpy.maximum(cc, 0)
        maxima_x, maxima_y, maxima_int = py4DSTEM.process.utils.get_maxima_2D(
            cc,
            sigma=sigma,
            edgeBoundary=edgeBoundary,
            minRelativeIntensity=minRelativeIntensity,
            minAbsoluteIntensity=minAbsoluteIntensity,
            relativeToPeak=relativeToPeak,
            minSpacing=minPeakSpacing,
            maxNumPeaks=maxNumPeaks,
            subpixel=False)
    elif subpixel == 'poly':
        cc = py4DSTEM.process.utils.get_cross_correlation_fk(DP, probe_kernel_FT, corrPower)
        cc = numpy.maximum(cc, 0)
        maxima_x, maxima_y, maxima_int = py4DSTEM.process.utils.get_maxima_2D(
            cc, sigma=sigma,
            edgeBoundary=edgeBoundary,
            minRelativeIntensity=minRelativeIntensity,
            minAbsoluteIntensity=minAbsoluteIntensity,
            relativeToPeak=relativeToPeak,
            minSpacing=minPeakSpacing,
            maxNumPeaks=maxNumPeaks,
            subpixel=True)
    else:
        # Multicorr subpixel:
        m = numpy.fft.fft2(DP) * probe_kernel_FT
        ccc = numpy.abs(m) ** corrPower * numpy.exp(1j * numpy.angle(m))

        cc = numpy.maximum(numpy.real(numpy.fft.ifft2(ccc)), 0)

        maxima_x, maxima_y, maxima_int = py4DSTEM.process.utils.get_maxima_2D(
            cc, sigma=sigma,
            edgeBoundary=edgeBoundary,
            minRelativeIntensity=minRelativeIntensity,
            minAbsoluteIntensity=minAbsoluteIntensity,
            relativeToPeak=relativeToPeak,
            minSpacing=minPeakSpacing,
            maxNumPeaks=maxNumPeaks,
            subpixel=True)

        # Use the DFT upsample to refine the detected peaks (but not the intensity)
        for ipeak in range(len(maxima_x)):
            xyShift = numpy.array((maxima_x[ipeak], maxima_y[ipeak]))
            # we actually have to lose some precision and go down to half-pixel
            # accuracy. this could also be done by a single upsampling at factor 2
            # instead of get_maxima_2D.
            xyShift[0] = numpy.round(xyShift[0] * 2) / 2
            xyShift[1] = numpy.round(xyShift[1] * 2) / 2

            subShift = py4DSTEM.process.utils.multicorr.upsampled_correlation(ccc, upsample_factor, xyShift)
            maxima_x[ipeak] = subShift[0]
            maxima_y[ipeak] = subShift[1]

    # Make peaks PointList
    if peaks is None:
        coords = [('qx', float), ('qy', float), ('intensity', float)]
        peaks = py4DSTEM.io.classes.PointList(coordinates=coords)
    else:
        assert (isinstance(peaks, py4DSTEM.io.classes.PointList))
    peaks.add_tuple_of_nparrays((maxima_x, maxima_y, maxima_int))

    if return_cc:
        return peaks, scipy.ndimage.filters.gaussian_filter(cc, sigma)
    else:
        return peaks


def _process_chunk(_f, start, end, path_to_static, coords, path_to_data, cluster_path):
    import os
    import dill

    with open(path_to_static, 'rb') as infile:
        inputs = dill.load(infile)

    # Always try to memory map the data file, if possible
    if path_to_data.rsplit('.', 1)[-1].startswith('dm'):
        datacube = py4DSTEM.io.read(path_to_data, load='dmmmap')
    elif path_to_data.rsplit('.',1)[-1].startswith('gt'):
        datacube = py4DSTEM.io.read(path_to_data, load='gatan_bin')
    else:
        datacube = py4DSTEM.io.read(path_to_data)

    results = []
    for x in coords:
        results.append((x[0], x[1], _f(datacube.data[x[0], x[1], :, :], *inputs).data))

    # release memory
    datacube = None

    path_to_output = os.path.join(cluster_path, "{}_{}.data".format(start, end))
    with open(path_to_output, 'wb') as data_file:
        dill.dump(results, data_file)

    return path_to_output


def find_Bragg_disks_ipp(DP, probe,
                         corrPower=1,
                         sigma=2,
                         edgeBoundary=20,
                         minRelativeIntensity=0.005,
                         minAbsoluteIntensity=0,
                         relativeToPeak=0,
                         minPeakSpacing=60,
                         maxNumPeaks=70,
                         subpixel='poly',
                         upsample_factor=4,
                         filter_function=None,
                         ipyparallel_client_file=None,
                         data_file=None,
                         cluster_path=None):
    """
    Distributed compute using IPyParallel.

    Finds the Bragg disks in all diffraction patterns of datacube by cross, hybrid, or
    phase correlation with probe.

    Args:
        DP (ndarray): a diffraction pattern
        probe (ndarray): the vacuum probe template, in real space.
        corrPower (float between 0 and 1, inclusive): the cross correlation power. A
            value of 1 corresponds to a cross correaltion, and 0 corresponds to a
            phase correlation, with intermediate values giving various hybrids.
        sigma (float): the standard deviation for the gaussian smoothing applied to
            the cross correlation
        edgeBoundary (int): minimum acceptable distance from the DP edge, in pixels
        minRelativeIntensity (float): the minimum acceptable correlation peak intensity,
            relative to the intensity of the brightest peak
        relativeToPeak (int): specifies the peak against which the minimum relative
            intensity is measured -- 0=brightest maximum. 1=next brightest, etc.
        minPeakSpacing (float): the minimum acceptable spacing between detected peaks
        maxNumPeaks (int): the maximum number of peaks to return
        subpixel (str): Whether to use subpixel fitting, and which algorithm to use.
            Must be in ('none','poly','multicorr').
                * 'none': performs no subpixel fitting
                * 'poly': polynomial interpolation of correlogram peaks (default)
                * 'multicorr': uses the multicorr algorithm with DFT upsampling
        upsample_factor (int): upsampling factor for subpixel fitting (only used when
            subpixel='multicorr')
        filter_function (callable): filtering function to apply to each diffraction
            pattern before peakfinding.  Must be a function of only one argument (the
            diffraction pattern) and return the filtered diffraction pattern. The shape
            of the returned DP must match the shape of the probe kernel (but does not
            need to match the shape of the input diffraction pattern, e.g. the filter
            can be used to bin the diffraction pattern). If using distributed disk
            detection, the function must be able to be pickled with by dill.
        ipyparallel_client_file (str): absolute path to ipyparallel client JSON file for
            connecting to a cluster
        data_file (str): absolute path to the data file containing the datacube for
            processing remotely
        cluster_path (str): working directory for cluster processing, defaults to current
            directory

    Returns:
        (PointListArray): the Bragg peak positions and correlation intensities
    """
    import ipyparallel as ipp

    R_Nx = DP.R_Nx
    R_Ny = DP.R_Ny
    R_N = DP.R_N
    DP = None

    # Make the peaks PointListArray
    coords = [('qx', float), ('qy', float), ('intensity', float)]
    peaks = PointListArray(coordinates=coords, shape=(R_Nx, R_Ny))

    # Get the probe kernel FT
    probe_kernel_FT = np.conj(np.fft.fft2(probe))

    if ipyparallel_client_file is None:
        raise RuntimeError("ipyparallel_client_file is None, no IPyParallel cluster")
    elif data_file is None:
        raise RuntimeError("data_file is None, needs path to datacube")

    t0 = time()
    c = ipp.Client(url_file=ipyparallel_client_file, timeout=30)

    inputs_list = [
        probe_kernel_FT,
        corrPower,
        sigma,
        edgeBoundary,
        minRelativeIntensity,
        minAbsoluteIntensity,
        relativeToPeak,
        minPeakSpacing,
        maxNumPeaks,
        subpixel,
        upsample_factor,
        filter_function
        ]

    if cluster_path is None:
        cluster_path = os.getcwd()

    tmpdir = tempfile.TemporaryDirectory(dir=cluster_path)

    t_00 = time()
    # write out static inputs
    path_to_inputs = os.path.join(tmpdir.name, "inputs")
    with open(path_to_inputs, 'wb') as inputs_file:
        dill.dump(inputs_list, inputs_file)
    t_inputs_save = time() - t_00
    print("Serialize input values : {}".format(t_inputs_save))

    results = []
    t1 = time()
    total = int(R_Nx * R_Ny)
    chunkSize = int(total / len(c.ids))

    while chunkSize * len(c.ids) < total:
        chunkSize += 1

    indices = [(Rx, Ry) for Rx in range(R_Nx) for Ry in range(R_Ny)]

    start = 0
    for engine in c.ids:
        if start + chunkSize < total - 1:
            end = start + chunkSize
        else:
            end = total

        results.append(
            c[engine].apply(
                _process_chunk,
                _find_Bragg_disks_single_DP_FK,
                start,
                end,
                path_to_inputs,
                indices[start:end],
                data_file,
                tmpdir.name))

        if end == total:
            break
        else:
            start = end
    t_submit = time() - t1
    print("Submit phase : {}".format(t_submit))

    t2 = time()
    c.wait(jobs=results)
    t_wait = time() - t2
    print("Gather phase : {}".format(t_wait))

    t3 = time()
    for i in range(len(results)):
        with open(results[i].get(), 'rb') as f:
            data_chunk = dill.load(f)

        for Rx, Ry, data in data_chunk:
            peaks.get_pointlist(Rx, Ry).add_dataarray(data)
    t_copy = time() - t3
    print("Copy results : {}".format(t_copy))

    # clean up temp files
    try:
        tmpdir.cleanup()
    except OSError as e:
        print("Error when cleaning up temporary files: {}".format(e))

    t = time() - t0
    print("Analyzed {} diffraction patterns in {}h {}m {}s".format(
        R_N, int(t / 3600), int(t / 60), int(t % 60)))

    return peaks


def find_Bragg_disks_dask(DP, probe,
                          corrPower=1,
                          sigma=2,
                          edgeBoundary=20,
                          minRelativeIntensity=0.005,
                          minAbsoluteIntensity=0,
                          relativeToPeak=0,
                          minPeakSpacing=60,
                          maxNumPeaks=70,
                          subpixel='poly',
                          upsample_factor=4,
                          filter_function=None,
                          dask_client=None,
                          data_file=None,
                          cluster_path=None):
    """
    Distributed compute using Dask.

    Finds the Bragg disks in all diffraction patterns of datacube by cross, hybrid, or
    phase correlation with probe.

    Args:
        DP (ndarray): a diffraction pattern
        probe (darray): the vacuum probe template, in real space.
        corrPower (float between 0 and 1, inclusive): the cross correlation power. A
            value of 1 corresponds to a cross correaltion, and 0 corresponds to a
            phase correlation, with intermediate values giving various hybrids.
        sigma (float): the standard deviation for the gaussian smoothing applied to
            the cross correlation
        edgeBoundary (int): minimum acceptable distance from the DP edge, in pixels
        minRelativeIntensity (float): the minimum acceptable correlation peak intensity,
            relative to the intensity of the brightest peak
        relativeToPeak (int): specifies the peak against which the minimum relative
            intensity is measured -- 0=brightest maximum. 1=next brightest, etc.
        minPeakSpacing (float): the minimum acceptable spacing between detected peaks
        maxNumPeaks (int): the maximum number of peaks to return
        subpixel (str): Whether to use subpixel fitting, and which algorithm to use.
            Must be in ('none','poly','multicorr').
                * 'none': performs no subpixel fitting
                * 'poly': polynomial interpolation of correlogram peaks (default)
                * 'multicorr': uses the multicorr algorithm with DFT upsampling
        upsample_factor (int): upsampling factor for subpixel fitting (only used when
            subpixel='multicorr')
        filter_function (callable): filtering function to apply to each diffraction
            pattern before peakfinding. Must be a function of only one argument (the
            diffraction pattern) and return the filtered diffraction pattern. The shape
            of the returned DP must match the shape of the probe kernel (but does not
            need to match the shape of the input diffraction pattern, e.g. the filter
            can be used to bin the diffraction pattern). If using distributed disk
            detection, the function must be able to be pickled with by dill.
        dask_client (obj): dask client for connecting to a cluster
        data_file (str): absolute path to the data file containing the datacube for
            processing remotely
        cluster_path (str): working directory for cluster processing, defaults to current
            directory

    Returns:
        (PointListArray) the Bragg peak positions and correlation intensities
    """
    import distributed

    R_Nx = DP.R_Nx
    R_Ny = DP.R_Ny
    R_N = DP.R_N
    DP = None

    # Make the peaks PointListArray
    coords = [('qx', float), ('qy', float), ('intensity', float)]
    peaks = PointListArray(coordinates=coords, shape=(R_Nx, R_Ny))

    # Get the probe kernel FT
    probe_kernel_FT = np.conj(np.fft.fft2(probe))

    if dask_client is None:
        raise RuntimeError("dask_client is None, no Dask cluster!")
    elif data_file is None:
        raise RuntimeError("data_file is None, needs path to datacube")

    t0 = time()

    inputs_list = [
        probe_kernel_FT,
        corrPower,
        sigma,
        edgeBoundary,
        minRelativeIntensity,
        minAbsoluteIntensity,
        relativeToPeak,
        minPeakSpacing,
        maxNumPeaks,
        subpixel,
        upsample_factor,
        filter_function
        ]

    if cluster_path is None:
        cluster_path = os.getcwd()

    tmpdir = tempfile.TemporaryDirectory(dir=cluster_path)

    # write out static inputs
    path_to_inputs = os.path.join(tmpdir.name, "{}.inputs".format(dask_client.id))
    with open(path_to_inputs, 'wb') as inputs_file:
        dill.dump(inputs_list, inputs_file)
    t_inputs_save = time() - t0
    print("Serialize input values : {}".format(t_inputs_save))

    cores = len(dask_client.ncores())

    submits = []
    t1 = time()
    total = int(R_Nx * R_Ny)
    chunkSize = int(total / cores)

    while (chunkSize * cores) < total:
        chunkSize += 1

    indices = [(Rx, Ry) for Rx in range(R_Nx) for Ry in range(R_Ny)]

    start = 0
    for engine in range(cores):
        if start + chunkSize < total - 1:
            end = start + chunkSize
        else:
            end = total

        submits.append(
            dask_client.submit(
                _process_chunk,
                _find_Bragg_disks_single_DP_FK,
                start,
                end,
                path_to_inputs,
                indices[start:end],
                data_file,
                tmpdir.name))

        if end == total:
            break
        else:
            start = end
    t_submit = time() - t1
    print("Submit phase : {}".format(t_submit))

    t2 = time()
    # collect results
    for batch in distributed.as_completed(submits, with_results=True).batches():
        for future, result in batch:
            with open(result, 'rb') as f:
                data_chunk = dill.load(f)

            for Rx, Ry, data in data_chunk:
                peaks.get_pointlist(Rx, Ry).add_dataarray(data)
    t_copy = time() - t2
    print("Gather phase : {}".format(t_copy))

    # clean up temp files
    try:
        tmpdir.cleanup()
    except OSError as e:
        print("Error when cleaning up temporary files: {}".format(e))

    t = time() - t0
    print("Analyzed {} diffraction patterns in {}h {}m {}s".format(
        R_N, int(t / 3600), int(t / 60), int(t % 60)))

    return peaks
