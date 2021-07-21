# Defines utility functions used by other functions in the /process/ directory.

import numpy as np
from numpy.fft import fftfreq, fftshift
from scipy.ndimage.filters import gaussian_filter
from scipy.spatial import Voronoi
import math as ma
import matplotlib.pyplot as plt
from mpl_toolkits.axes_grid1 import make_axes_locatable
from mpl_toolkits.axes_grid1.anchored_artists import AnchoredSizeBar
import matplotlib.font_manager as fm

from .multicorr import upsampled_correlation

try:
    from IPython.display import clear_output
except ImportError:
    def clear_output(wait=True):
        pass

def plot(img, title='Image', savePath=None, cmap='inferno', show=True, vmax=None,
                                                        figsize=(10, 10), scale=None):
    fig, ax = plt.subplots(figsize=figsize)
    im = ax.imshow(img, interpolation='nearest', cmap=plt.cm.get_cmap(cmap), vmax=vmax)
    divider = make_axes_locatable(ax)
    cax = divider.append_axes("right", size="5%", pad=0.05)
    plt.colorbar(im, cax=cax)
    ax.set_title(title)
    fontprops = fm.FontProperties(size=18)
    if scale is not None:
        scalebar = AnchoredSizeBar(ax.transData,
                                   scale[0], scale[1], 'lower right',
                                   pad=0.1,
                                   color='white',
                                   frameon=False,
                                   size_vertical=img.shape[0] / 40,
                                   fontproperties=fontprops)

        ax.add_artist(scalebar)
    ax.grid(False)
    if savePath is not None:
        fig.savefig(savePath + '.png', dpi=600)
        fig.savefig(savePath + '.eps', dpi=600)
    if show:
        plt.show()

def electron_wavelength_angstrom(E_eV):
    m = 9.109383 * 10 ** -31
    e = 1.602177 * 10 ** -19
    c = 299792458
    h = 6.62607 * 10 ** -34

    lam = h / ma.sqrt(2 * m * e * E_eV) / ma.sqrt(1 + e * E_eV / 2 / m / c ** 2) * 10 ** 10
    return lam


def sector_mask(shape, centre, radius, angle_range=(0, 360)):
    """
    Return a boolean mask for a circular sector. The start/stop angles in
    `angle_range` should be given in clockwise order.

    Args:
        shape: 2D shape of the mask
        centre: 2D center of the circular sector
        radius: radius of the circular mask
        angle_range: angular range of the circular mask
    """
    x, y = np.ogrid[:shape[0], :shape[1]]
    cx, cy = centre
    tmin, tmax = np.deg2rad(angle_range)

    # ensure stop angle > start angle
    if tmax < tmin:
        tmax += 2 * np.pi

    # convert cartesian --> polar coordinates
    r2 = (x - cx) * (x - cx) + (y - cy) * (y - cy)
    theta = np.arctan2(x - cx, y - cy) - tmin

    # wrap angles between 0 and 2*pi
    theta %= (2 * np.pi)

    # circular mask
    circmask = r2 <= radius * radius

    # print 'radius - ', radius

    # angular mask
    anglemask = theta < (tmax - tmin)

    return circmask * anglemask


def get_qx_qy_1d(M, dx=[1, 1], fft_shifted=False):
    """
    Generates 1D Fourier coordinates for a (Nx,Ny)-shaped 2D array.
    Specifying the dx argument sets a unit size.

    Args:
        M: (2,) shape of the returned array
        dx: (2,) tuple, pixel size
        fft_shifted: True if result should be fft_shifted to have the origin in the center of the array
    """

    qxa = fftfreq(M[0], dx[0])
    qya = fftfreq(M[1], dx[1])
    if fft_shifted:
        qxa = fftshift(qxa)
        qya = fftshift(qya)
    return qxa, qya


def make_Fourier_coords2D(Nx, Ny, pixelSize=1):
    """
    Generates Fourier coordinates for a (Nx,Ny)-shaped 2D array.
	Specifying the pixelSize argument sets a unit size.
	"""
    if hasattr(pixelSize, '__len__'):
        assert len(pixelSize) == 2, "pixelSize must either be a scalar or have length 2"
        pixelSize_x = pixelSize[0]
        pixelSize_y = pixelSize[1]
    else:
        pixelSize_x = pixelSize
        pixelSize_y = pixelSize

    qx = np.fft.fftfreq(Nx, pixelSize_x)
    qy = np.fft.fftfreq(Ny, pixelSize_y)
    qy, qx = np.meshgrid(qy, qx)
    return qx, qy


def get_shift(ar1, ar2, corrPower=1):
    """
	Determine the relative shift between a pair of identical arrays, or the shift giving
	best overlap.

	Shift determination uses the brightest pixel in the cross correlation, and is thus limited to
    pixel resolution. corrPower specifies the cross correlation power, with 1 corresponding to a
    cross correlation and 0 a phase correlation.

	Args:
		ar1,ar2 (2D ndarrays):
        corrPower (float between 0 and 1, inclusive): 1=cross correlation, 0=phase
            correlation

    Returns:
		(2-tuple): (shiftx,shifty) - the relative image shift, in pixels
    """
    cc = get_cross_correlation(ar1, ar2, corrPower)
    xshift, yshift = np.unravel_index(np.argmax(cc), ar1.shape)
    return xshift, yshift


def get_shifted_ar(ar, xshift, yshift):
    """
	Shifts array ar by the shift vector (xshift,yshift), using the Fourier shift theorem (i.e.
	with sinc interpolation).
    """
    nx, ny = np.shape(ar)
    qx, qy = make_Fourier_coords2D(nx, ny, 1)
    nx, ny = float(nx), float(ny)

    w = np.exp(-(2j * np.pi) * ((yshift * qy) + (xshift * qx)))
    shifted_ar = np.real(np.fft.ifft2((np.fft.fft2(ar)) * w))
    return shifted_ar


def get_cross_correlation(ar, kernel, corrPower=1, returnval='cc'):
    """
    Calculates the cross correlation of ar with kernel.
    corrPower specifies the correlation type, where 1 is a cross correlation, 0 is a phase
    correlation, and values in between are hybrids.

    The return value depends on the argument ``returnval``. If return=='cc' (default),
    returns the real part of the cross correlation in real space.  If return=='fourier',
    returns the output in Fourier space, before taking the inverse transform.
    """
    assert(returnval in ('cc','fourier'))
    fourierkernel = np.conj(np.fft.fft2(kernel))
    return get_cross_correlation_fk(ar, fourierkernel, corrPower=corrPower, returnval=returnval)


def get_cross_correlation_fk(ar, fourierkernel, corrPower=1, returnval='cc'):
    """
    Calculates the cross correlation of ar with fourierkernel.
    Here, fourierkernel = np.conj(np.fft.fft2(kernel)); speeds up computation when the same
    kernel is to be used for multiple cross correlations.
    corrPower specifies the correlation type, where 1 is a cross correlation, 0 is a phase
    correlation, and values in between are hybrids.

    The return value depends on the argument ``returnval``. If return=='cc' (default),
    returns the real part of the cross correlation in real space.  If return=='fourier',
    returns the output in Fourier space, before taking the inverse transform.
    """
    assert(returnval in ('cc','fourier'))
    m = np.fft.fft2(ar) * fourierkernel
    ccc = np.abs(m)**(corrPower) * np.exp(1j*np.angle(m))
    if returnval=='fourier':
        return ccc
    else:
        return np.real(np.fft.ifft2(ccc))


def get_CoM(ar):
    """
    Finds and returns the center of mass of array ar.
    """
    nx, ny = np.shape(ar)
    ry, rx = np.meshgrid(np.arange(ny), np.arange(nx))
    tot_intens = np.sum(ar)
    xCoM = np.sum(rx * ar) / tot_intens
    yCoM = np.sum(ry * ar) / tot_intens
    return xCoM, yCoM


def get_maximal_points(ar):
    """
    For 2D array ar, returns an array of bools of the same shape which is True for all entries with
    values larger than all 8 of their nearest neighbors.
    """
    return (ar > np.roll(ar, (-1, 0), axis=(0, 1))) & (ar > np.roll(ar, (1, 0), axis=(0, 1))) & \
           (ar > np.roll(ar, (0, -1), axis=(0, 1))) & (ar > np.roll(ar, (0, 1), axis=(0, 1))) & \
           (ar > np.roll(ar, (-1, -1), axis=(0, 1))) & (ar > np.roll(ar, (-1, 1), axis=(0, 1))) & \
           (ar > np.roll(ar, (1, -1), axis=(0, 1))) & (ar > np.roll(ar, (1, 1), axis=(0, 1)))


def get_maxima_2D(ar, sigma=0, edgeBoundary=0, minSpacing=0, minRelativeIntensity=0,
                  relativeToPeak=0, maxNumPeaks=0, subpixel='poly', ar_FT=None, upsample_factor=16):
    """
    Finds the indices where the 2D array ar is a local maximum.
    Optional parameters allow blurring of the array and filtering of the output;
    setting each of these to 0 (default) turns off these functions.

    Args:
        ar (ndarray): a 2D array
        sigma (float): guassian blur std to applyu to ar before finding the maxima
        edgeBoundary (int): ignore maxima within edgeBoundary of the array edge
        minSpacing (float): if two maxima are found within minSpacing, the dimmer one
            is removed
        minRelativeIntensity (float): maxima dimmer than minRelativeIntensity compared
            to the relativeToPeak'th brightest maximum are removed
        relativeToPeak (int): 0=brightest maximum. 1=next brightest, etc.
        maxNumPeaks (int): return only the first maxNumPeaks maxima
        subpixel (str): Whether to use subpixel fitting, and which algorithm to use.
            Must be in ('none','poly','multicorr').
                * 'none': performs no subpixel fitting
                * 'poly': polynomial interpolation of correlogram peaks (default)
                * 'multicorr': uses the multicorr algorithm with DFT upsampling
        ar_FT (None or complex array): if subpixel=='multicorr' the fourier transform of
            the image is required.  It may be passed here as a complex array.  Otherwise,
            if ar_FT is None, it is computed
        upsample_factor (int): required iff subpixel=='multicorr'

    Returns:
        (3-tuple): A 3-tuple containing:

            * **maxima_x** *(ndarray)*: x-coords of the local maximum, sorted by intensity.
            * **maxima_y** *(ndarray)*: y-coords of the local maximum, sorted by intensity.
            * **maxima_intensity** *(ndarray)*: intensity of the local maxima
    """
    assert subpixel in [ 'none', 'poly', 'multicorr' ], "Unrecognized subpixel option {}, subpixel must be 'none', 'poly', or 'multicorr'".format(subpixel)

    # Get maxima
    ar = gaussian_filter(ar, sigma)
    maxima_bool = get_maximal_points(ar)

    # Remove edges
    if edgeBoundary > 0:
        assert isinstance(edgeBoundary, (int, np.integer))
        maxima_bool[:edgeBoundary, :] = False
        maxima_bool[-edgeBoundary:, :] = False
        maxima_bool[:, :edgeBoundary] = False
        maxima_bool[:, -edgeBoundary:] = False
    elif subpixel is True:
        maxima_bool[:1, :] = False
        maxima_bool[-1:, :] = False
        maxima_bool[:, :1] = False
        maxima_bool[:, -1:] = False

    # Get indices, sorted by intensity
    maxima_x, maxima_y = np.nonzero(maxima_bool)
    dtype = np.dtype([('x', float), ('y', float), ('intensity', float)])
    maxima = np.zeros(len(maxima_x), dtype=dtype)
    maxima['x'] = maxima_x
    maxima['y'] = maxima_y
    maxima['intensity'] = ar[maxima_x, maxima_y]
    maxima = np.sort(maxima, order='intensity')[::-1]

    if len(maxima) > 0:
        # Remove maxima which are too close
        if minSpacing > 0:
            deletemask = np.zeros(len(maxima), dtype=bool)
            for i in range(len(maxima)):
                if deletemask[i] == False:
                    tooClose = ((maxima['x'] - maxima['x'][i]) ** 2 + \
                                (maxima['y'] - maxima['y'][i]) ** 2) < minSpacing ** 2
                    tooClose[:i + 1] = False
                    deletemask[tooClose] = True
            maxima = np.delete(maxima, np.nonzero(deletemask)[0])

        # Remove maxima which are too dim
        if (minRelativeIntensity > 0) & (len(maxima) > relativeToPeak):
            assert isinstance(relativeToPeak, (int, np.integer))
            deletemask = maxima['intensity'] / maxima['intensity'][relativeToPeak] < minRelativeIntensity
            maxima = np.delete(maxima, np.nonzero(deletemask)[0])

        # Remove maxima in excess of maxNumPeaks
        if maxNumPeaks > 0:
            assert isinstance(maxNumPeaks, (int, np.integer))
            if len(maxima) > maxNumPeaks:
                maxima = maxima[:maxNumPeaks]

        # Subpixel fitting 
        # For all subpixel fitting, first fit 1D parabolas in x and y to 3 points (maximum, +/- 1 pixel)
        if subpixel != 'none':
            for i in range(len(maxima)):
                Ix1_ = ar[int(maxima['x'][i]) - 1, int(maxima['y'][i])]
                Ix0 = ar[int(maxima['x'][i]), int(maxima['y'][i])]
                Ix1 = ar[int(maxima['x'][i]) + 1, int(maxima['y'][i])]
                Iy1_ = ar[int(maxima['x'][i]), int(maxima['y'][i]) - 1]
                Iy0 = ar[int(maxima['x'][i]), int(maxima['y'][i])]
                Iy1 = ar[int(maxima['x'][i]), int(maxima['y'][i]) + 1]
                deltax = (Ix1 - Ix1_) / (4 * Ix0 - 2 * Ix1 - 2 * Ix1_)
                deltay = (Iy1 - Iy1_) / (4 * Iy0 - 2 * Iy1 - 2 * Iy1_)
                maxima['x'][i] += deltax
                maxima['y'][i] += deltay
                maxima['intensity'][i] = linear_interpolation_2D(ar, maxima['x'][i], maxima['y'][i])
        # Further refinement with fourier upsampling
        if subpixel == 'multicorr':
            if ar_FT is None:
                ar_FT = np.fft.fft2(ar)
            for ipeak in range(len(maxima['x'])):
                xyShift = np.array((maxima['x'][ipeak],maxima['y'][ipeak]))
                # we actually have to lose some precision and go down to half-pixel
                # accuracy. this could also be done by a single upsampling at factor 2
                # instead of get_maxima_2D.
                xyShift[0] = np.round(xyShift[0] * 2) / 2
                xyShift[1] = np.round(xyShift[1] * 2) / 2

                subShift = upsampled_correlation(ar_FT,upsample_factor,xyShift)
                maxima['x'][ipeak]=subShift[0]
                maxima['y'][ipeak]=subShift[1]

    return maxima['x'], maxima['y'], maxima['intensity']


def get_maxima_1D(ar, sigma=0, minSpacing=0, minRelativeIntensity=0, relativeToPeak=0):
    """
    Finds the indices where 1D array ar is a local maximum.
    Optional parameters allow blurring the array and filtering the output;
    setting each to 0 (default) turns off these functions.

    Args:
        ar (1D array):
        sigma (number): gaussian blur std to apply to ar before finding maxima
        minSpacing (number): if two maxima are found within minSpacing, the dimmer one
            is removed
        minRelativeIntensity (number): maxima dimmer than minRelativeIntensity compared
            to the relativeToPeak'th brightest maximum are removed
        relativeToPeak (int): 0=brightest maximum. 1=next brightest, etc.

    Returns:
        (array of ints): An array of indices where ar is a local maximum, sorted by intensity.
    """
    assert len(ar.shape) == 1, "ar must be 1D"
    assert isinstance(relativeToPeak, (int, np.integer)), "relativeToPeak must be an int"
    if sigma > 0:
        ar = gaussian_filter(ar, sigma)

    # Get maxima and intensity arrays
    maxima_bool = np.logical_and((ar > np.roll(ar, -1)) , (ar >= np.roll(ar, +1)))
    x = np.arange(len(ar))[maxima_bool]
    intensity = ar[maxima_bool]

    # Sort by intensity
    temp_ar = np.array([(x, inten) for inten, x in sorted(zip(intensity, x), reverse=True)])
    x, intensity = temp_ar[:, 0], temp_ar[:, 1]

    # Remove points which are too close
    if minSpacing > 0:
        deletemask = np.zeros(len(x), dtype=bool)
        for i in range(len(x)):
            if not deletemask[i]:
                delete = np.abs(x[i] - x) < minSpacing
                delete[:i + 1] = False
                deletemask = deletemask | delete
        x = np.delete(x, deletemask.nonzero()[0])
        intensity = np.delete(intensity, deletemask.nonzero()[0])

    # Remove points which are too dim
    if minRelativeIntensity > 0:
        deletemask = intensity / intensity[relativeToPeak] < minRelativeIntensity
        x = np.delete(x, deletemask.nonzero()[0])
        intensity = np.delete(intensity, deletemask.nonzero()[0])

    return x.astype(int)


def linear_interpolation_1D(ar, x):
    """
    Calculates the 1D linear interpolation of array ar at position x using the two
    nearest elements.
    """
    x0, x1 = int(np.floor(x)), int(np.ceil(x))
    dx = x - x0
    return (1 - dx) * ar[x0] + dx * ar[x1]


def linear_interpolation_2D(ar, x, y):
    """
    Calculates the 2D linear interpolation of array ar at position x,y using the four
    nearest array elements.
    """
    x0, x1 = int(np.floor(x)), int(np.ceil(x))
    y0, y1 = int(np.floor(y)), int(np.ceil(y))
    dx = x - x0
    dy = y - y0
    return (1 - dx) * (1 - dy) * ar[x0, y0] + (1 - dx) * dy * ar[x0, y1] + dx * (1 - dy) * ar[x1, y0] + dx * dy * ar[
        x1, y1]


def add_to_2D_array_from_floats(ar, x, y, I):
    """
    Adds the values I to array ar, distributing the value between the four pixels nearest
    (x,y) using linear interpolation.  Inputs (x,y,I) may be floats or arrays of floats.
    """
    Nx, Ny = ar.shape
    x0, x1 = (np.floor(x)).astype(int), (np.ceil(x)).astype(int)
    y0, y1 = (np.floor(y)).astype(int), (np.ceil(y)).astype(int)
    mask = np.logical_and(np.logical_and(np.logical_and((x0>=0),(y0>=0)),(x1<Nx)),(y1<Ny))
    dx = x - x0
    dy = y - y0
    ar[x0[mask], y0[mask]] += (1 - dx[mask]) * (1 - dy[mask]) * I[mask]
    ar[x0[mask], y1[mask]] += (1 - dx[mask]) * (    dy[mask]) * I[mask]
    ar[x1[mask], y0[mask]] += (    dx[mask]) * (1 - dy[mask]) * I[mask]
    ar[x1[mask], y1[mask]] += (    dx[mask]) * (    dy[mask]) * I[mask]
    return ar


def print_progress_bar(iteration, total, prefix='', suffix='', decimals=1,
                       length=100, fill='*'):
    """
    Call in a loop to create terminal progress bar

    Args:
        iteration (int): current iteration
        total (int): total iterations
        prefix (str, optional): prefix string
        suffix  (str, optional): suffix string
        decimals (int, optional): positive number of decimals in percent complete
        length (int, optional): character length of bar
        fill (str, optional): bar fill character
    """
    percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
    filledLength = int(length * iteration // total)
    bar = fill * filledLength + '-' * (length - filledLength)
    clear_output(wait=True)
    print('\r{} |{}| {}% {}\r'.format(prefix, bar, percent, suffix))
    # Print New Line on Complete
    if iteration == total:
        print()


def bin2D(array, factor, dtype=np.float64):
    """
    Bin a 2D ndarray by binfactor.

    Args:
        array (2D numpy array):
        factor (int): the binning factor
        dtype (numpy dtype): datatype for binned array. default is numpy default for
            np.zeros()

    Returns:
        the binned array
    """
    x, y = array.shape
    binx, biny = x // factor, y // factor
    xx, yy = binx * factor, biny * factor

    # Make a binned array on the device
    binned_ar = np.zeros((binx, biny), dtype=dtype)
    array = array.astype(dtype)

    # Collect pixel sums into new bins
    for ix in range(factor):
        for iy in range(factor):
            binned_ar += array[0 + ix:xx + ix:factor, 0 + iy:yy + iy:factor]
    return binned_ar


def get_voronoi_vertices(voronoi, nx, ny, dist=10):
    """
    From a scipy.spatial.Voronoi instance, return a list of ndarrays, where each array
    is shape (N,2) and contains the (x,y) positions of the vertices of a voronoi region.

    The problem this function solves is that in a Voronoi instance, some vertices outside
    the field of view of the tesselated region are left unspecified; only the existence
    of a point beyond the field is referenced (which may or may not be 'at infinity').
    This function specifies all points, such that the vertices and edges of the
    tesselation may be directly laid over data.

    Args:
        voronoi (scipy.spatial.Voronoi): the voronoi tesselation
        nx (int): the x field-of-view of the tesselated region
        ny (int): the y field-of-view of the tesselated region
        dist (float, optional): place new vertices by extending new voronoi edges outside
            the frame by a distance of this factor times the distance of its known vertex
            from the frame edge

    Returns:
        (list of ndarrays of shape (N,2)): the (x,y) coords of the vertices of each
        voronoi region
    """
    assert isinstance(voronoi, Voronoi), "voronoi must be a scipy.spatial.Voronoi instance"

    vertex_list = []

    # Get info about ridges containing an unknown vertex.  Include:
    #   -the index of its known vertex, in voronoi.vertices, and
    #   -the indices of its regions, in voronoi.point_region
    edgeridge_vertices_and_points = []
    for i in range(len(voronoi.ridge_vertices)):
        ridge = voronoi.ridge_vertices[i]
        if -1 in ridge:
            edgeridge_vertices_and_points.append([max(ridge),
                                                  voronoi.ridge_points[i, 0],
                                                  voronoi.ridge_points[i, 1]])
    edgeridge_vertices_and_points = np.array(edgeridge_vertices_and_points)

    # Loop over all regions
    for index in range(len(voronoi.regions)):
        # Get the vertex indices
        vertex_indices = voronoi.regions[index]
        vertices = np.array([0, 0])
        # Loop over all vertices
        for i in range(len(vertex_indices)):
            index_current = vertex_indices[i]
            if index_current != -1:
                # For known vertices, just add to a running list
                vertices = np.vstack((vertices, voronoi.vertices[index_current]))
            else:
                # For unknown vertices, get the first vertex it connects to,
                # and the two voronoi points that this ridge divides
                index_prev = vertex_indices[(i - 1) % len(vertex_indices)]
                edgeridge_index = int(np.argwhere(edgeridge_vertices_and_points[:, 0] == index_prev))
                index_vert, region0, region1 = edgeridge_vertices_and_points[edgeridge_index, :]
                x, y = voronoi.vertices[index_vert]
                # Only add new points for unknown vertices if the known index it connects to
                # is inside the frame.  Add points by finding the line segment starting at
                # the known point which is perpendicular to the segment connecting the two
                # voronoi points, and extending that line segment outside the frame.
                if (x > 0) and (x < nx) and (y > 0) and (y < ny):
                    x_r0, y_r0 = voronoi.points[region0]
                    x_r1, y_r1 = voronoi.points[region1]
                    m = -(x_r1 - x_r0) / (y_r1 - y_r0)
                    # Choose the direction to extend the ridge
                    ts = np.array([-x, -y / m, nx - x, (ny - y) / m])
                    x_t = lambda t: x + t
                    y_t = lambda t: y + m * t
                    t = ts[np.argmin(np.hypot(x - x_t(ts), y - y_t(ts)))]
                    x_new, y_new = x_t(dist * t), y_t(dist * t)
                    vertices = np.vstack((vertices, np.array([x_new, y_new])))
                else:
                    # If handling unknown points connecting to points outside the frame is
                    # desired, add here
                    pass

                # Repeat for the second vertec the unknown vertex connects to
                index_next = vertex_indices[(i + 1) % len(vertex_indices)]
                edgeridge_index = int(np.argwhere(edgeridge_vertices_and_points[:, 0] == index_next))
                index_vert, region0, region1 = edgeridge_vertices_and_points[edgeridge_index, :]
                x, y = voronoi.vertices[index_vert]
                if (x > 0) and (x < nx) and (y > 0) and (y < ny):
                    x_r0, y_r0 = voronoi.points[region0]
                    x_r1, y_r1 = voronoi.points[region1]
                    m = -(x_r1 - x_r0) / (y_r1 - y_r0)
                    # Choose the direction to extend the ridge
                    ts = np.array([-x, -y / m, nx - x, (ny - y) / m])
                    x_t = lambda t: x + t
                    y_t = lambda t: y + m * t
                    t = ts[np.argmin(np.hypot(x - x_t(ts), y - y_t(ts)))]
                    x_new, y_new = x_t(dist * t), y_t(dist * t)
                    vertices = np.vstack((vertices, np.array([x_new, y_new])))
                else:
                    pass

        # Remove regions with insufficiently many vertices
        if len(vertices) < 4:
            vertices = np.array([])
        # Remove initial dummy point
        else:
            vertices = vertices[1:, :]
        # Update vertex list with this region's vertices
        vertex_list.append(vertices)

    return vertex_list

def get_ewpc_filter_function(Q_Nx, Q_Ny):
    '''
    Returns a function for computing the exit wave power cepstrum of a diffraction
    pattern using a Hanning window. This can be passed as the filter_function in the
    Bragg disk detection functions (with the probe an array of ones) to find the lattice
    vectors by the EWPC method (but be careful as the lengths are now in realspace
    units!) See https://arxiv.org/abs/1911.00984
    '''
    h = np.hanning(Q_Nx)[:,np.newaxis] * np.hanning(Q_Ny)[np.newaxis,:]
    return lambda x: np.abs(np.fft.fftshift(np.fft.fft2(h*np.log(np.maximum(x,0.01)))))**2



def fourier_resample(array, scale, dtype=np.float32):
    """
    Resize an array along any dimension, using Fourier interpolation / extrapolation.
    Note that if you pass in a 4D array, but only provide 1 or 2 

    Args:
        array (2D/4D numpy array):
        scale (float): the scaling factor for each dimension
        dtype (numpy dtype): datatype for binned array. default is single precision float.

    Returns:
        the resized array (2D/4D numpy array)
    """
    scale = np.asarray(scale)
    array_size = array.shape

    if len(array_size) == 2:
        # image array
        new_size = (array_size * scale).astype('int64')

        if scale > 1:
            array_fft = np.fft.fft2(array)

            array_resize = np.zeros(new_size, dtype=np.complex64)

            array_resize[0:array_size[0]//2, 
                0:array_size[1]//2] = \
                array_fft[0:array_size[0]//2,
                0:array_size[1]//2]

            array_resize[
                1-array_size[0]//2+new_size[0]:new_size[0], 
                0:array_size[1]//2]  = \
                array_fft[1-array_size[0]//2+array_size[0]:array_size[0], 
                0:array_size[1]//2]

            array_resize[0:array_size[0]//2,
                1-array_size[1]//2+new_size[1]:new_size[1]] = \
                array_fft[0:array_size[0]//2, 
                1-array_size[1]//2+array_size[1]:array_size[1]]

            array_resize[
                1-array_size[0]//2+new_size[0]:new_size[0], 
                1-array_size[1]//2+new_size[1]:new_size[1]] = \
                array_fft[1-array_size[0]//2+array_size[0]:array_size[0], 
                1-array_size[1]//2+array_size[1]:array_size[1]]

            # Back to real space
            array_resize = np.real(np.fft.ifft2(array_resize)).astype(dtype)

            if len(scale) == 1:
                array_resize = array_resize * scale**2
            elif:
                array_resize = array_resize * np.prod(scale)

    elif len(array_size) == 4:
        # four dimensional array
        new_size = array_size * np.array((1, 1, scale, scale))


    return array_resize