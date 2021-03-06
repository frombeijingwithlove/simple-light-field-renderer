"""
Utils for basic image processing 
"""

import numpy
import scipy.ndimage
import cv2
from matplotlib import pyplot

DEFAULT_LONG_EDGE_LIMIT = 600
FLANN_INDEX_LSH = 6
ROI_RATIO = 0.5
DEPTH_MAP_SHORT_EDGE_SIZE = 400
DEFAULT_SHIFT_RANGE = (-1., 1.5)    # -1 is infinity, 1.5 is empirical
DEFAULT_SUB_PIX_RATE = 1.0


def get_edges_from_triangles(triangles):
    edges = []
    for triangle in triangles:
        a, b, c = triangle
        edge0 = (a, b) if a < b else (b, a)
        edge1 = (b, c) if b < c else (c, b)
        edge2 = (a, c) if a < c else (c, a)
        edges += [edge0, edge1, edge2]
    return list(set(edges))


def limit_image_size(img, long_edge=DEFAULT_LONG_EDGE_LIMIT):
    h, w = img.shape[:2]
    fxy = long_edge / max(h, w)
    if fxy < 1:
        img = cv2.resize(img, (0, 0), fx=fxy, fy=fxy)
    return img


def calibrate_rois(rois):
    orb = cv2.ORB_create(nfeatures=200, nlevels=1)
    index_params = dict(algorithm=FLANN_INDEX_LSH, trees=5)
    search_params = dict(checks=50)
    flann = cv2.FlannBasedMatcher(index_params, search_params)
    kp_n_des_list = [orb.detectAndCompute(x, None) for x in rois]

    kp0, des0 = kp_n_des_list[0]

    mats = []
    #i = 1
    for kp, des in kp_n_des_list[1:]:
        matches = flann.knnMatch(des0, des, k=2)
        good_matches = []
        for match in matches:
            if len(match) == 2 and match[0].distance < 0.6 * match[1].distance:
                good_matches.append(match[0])
        dst_pts = numpy.float32([kp0[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
        src_pts = numpy.float32([kp[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)
        mat_affine = cv2.estimateRigidTransform(src_pts, dst_pts, False)
        mats.append(mat_affine)

        #img_matching = cv2.drawMatches(rois[0], kp0, rois[i], kp, good_matches, None, flags=2)
        #pyplot.figure(i)
        #pyplot.imshow(img_matching)
        #i += 1

    #pyplot.show()

    return mats


def calibrate_images(images):
    # check if images are same size
    h_ref, w_ref = images[0].shape[:2]
    for image in images[1:]:
        h, w = image.shape[:2]
        if h != h_ref or w != w_ref:
            print('Bad inputs!')
            return None

    cx = w_ref / 2
    cy = h_ref / 2
    short_edge = min(h_ref, w_ref)
    coeff = ROI_RATIO / 2
    x0_roi = int(cx - coeff * short_edge + 0.5)
    x1_roi = int(cx + coeff * short_edge + 0.5)
    y0_roi = int(cy - coeff * short_edge + 0.5)
    y1_roi = int(cy + coeff * short_edge + 0.5)

    calib_rois = [cv2.cvtColor(x[y0_roi:y1_roi+1, x0_roi:x1_roi+1], cv2.COLOR_BGR2GRAY) for x in images]
    affine_mats = calibrate_rois(calib_rois)

    coords = [[0., 0.]]
    for i, m in enumerate(affine_mats):
        images[i + 1] = cv2.warpAffine(images[i + 1], m, (w_ref, h_ref))
        coords.append([m[0][2], m[1][2]])

    coords = numpy.array(coords)
    mean_coord = numpy.mean(coords, axis=0)
    coords = [numpy.array(x)-mean_coord for x in coords]

    return images, coords


def variance_map(images):
    imgs = numpy.asarray(images)
    dim = len(imgs.shape)
    if dim == 4:
        return numpy.sum(numpy.var(imgs, axis=0), axis=2)
    elif dim == 3:
        return numpy.var(imgs, axis=0)
    else:
        return None


def cal_depth_map(images, coords, short_edge=DEPTH_MAP_SHORT_EDGE_SIZE, shift_range=DEFAULT_SHIFT_RANGE):
    # check if images are same size
    h_ref, w_ref = images[0].shape[:2]
    for image in images[1:]:
        h, w = image.shape[:2]
        if h != h_ref or w != w_ref:
            print('Bad inputs!')
            return None

    scale = short_edge / min(h_ref, w_ref)
    imgs = []
    if scale < 1:
        for i in range(len(images)):
            imgs.append(cv2.resize(images[i], (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR))
    else:
        scale = 1.

    dcoords = [x * scale for x in coords]
    shifts = numpy.linspace(*shift_range, 100)

    depth_map = numpy.zeros(imgs[0].shape[:2], dtype=numpy.float32)
    min_var_map = numpy.ones(imgs[0].shape[:2], dtype=numpy.float32) * 1e9

    unit_mat = numpy.array([
        [1, 0],
        [0, 1]
    ])

    h0, w0 = imgs[0].shape[:2]
    still_pixs = numpy.ones(depth_map.shape, dtype=numpy.uint8)
    focus_measures = []
    for i, shift in enumerate(shifts):
        mats = [numpy.hstack([unit_mat, shift * dcoord.reshape(2, 1)]) for dcoord in dcoords]
        shifted_imgs = [cv2.warpAffine(img, m, (w0, h0)) for img, m in zip(imgs, mats)]
        var_map = variance_map(shifted_imgs)
        prev_depth_map = depth_map.copy()
        depth_map[var_map < min_var_map] = shift
        if i > 0:
            still_pixs[depth_map != prev_depth_map] = 0

        min_var_map = numpy.min([min_var_map, var_map], axis=0)
        stacked_img = numpy.mean(shifted_imgs, axis=0)

        focus_measure = 0
        for j in range(3):
            ch_grad = cv2.Laplacian(stacked_img[:, :, j], cv2.CV_64F)
            focus_measure += ch_grad.var()

        focus_measures.append(focus_measure)

    # Try to fix some never update pixels ...
    blurred_depth_map = scipy.ndimage.median_filter(depth_map, 5)
    depth_map[still_pixs == 1] = blurred_depth_map[still_pixs == 1]
    depth_map = cv2.resize(depth_map, (w_ref, h_ref))
    return depth_map, focus_measures


def interpolate_image(images, coords, interp_coords,
                      sub_pix_rate=DEFAULT_SUB_PIX_RATE,
                      shift_range=DEFAULT_SHIFT_RANGE):

    h, w = images[0].shape[:2]
    unit_mat = numpy.array([
        [1, 0],
        [0, 1]
    ])

    interp_images = []
    for interp_coord in interp_coords:
        distances = [numpy.linalg.norm(numpy.array(interp_coord)-x) for x in coords]
        num_shifts = int(numpy.mean(distances) / sub_pix_rate + 0.5) * (shift_range[1] - shift_range[0])
        alphas = numpy.linspace(*shift_range, num_shifts)
        # assume all images are equal size
        interp_image = numpy.mean(images, axis=0)
        min_diff_map = variance_map(images)
        for alpha in alphas:
            print(alpha)
            image_stack = []
            for coord, image in zip(coords, images):
                m_shift = numpy.hstack([unit_mat, alpha * coord.reshape(2, 1)])
                shifted_img = cv2.warpAffine(image, m_shift, (w, h))
                image_stack.append(shifted_img)
            diff_map = variance_map(image_stack)
            mean_shifted_image = numpy.mean(image_stack, axis=0)

            update_positions = numpy.where(diff_map < min_diff_map)
            interp_image[update_positions] = mean_shifted_image[update_positions]
            min_diff_map[update_positions] = diff_map[update_positions]
        interp_images.append(interp_image)

        image_name = '{}_{}.jpg'.format(*interp_coord)
        cv2.imwrite(image_name, interp_image.astype(numpy.uint8))

    return interp_images


def make_refocused_images(coords, images, shift_range=DEFAULT_SHIFT_RANGE):
    # check if images are same size
    h_ref, w_ref = images[0].shape[:2]
    shifts = numpy.linspace(*shift_range, 100)

    depth_map = numpy.zeros(images[0].shape[:2], dtype=numpy.float32)
    min_var_map = numpy.ones(images[0].shape[:2], dtype=numpy.float32) * 1e9

    unit_mat = numpy.array([
        [1, 0],
        [0, 1]
    ])

    h0, w0 = images[0].shape[:2]
    still_pixs = numpy.ones(depth_map.shape, dtype=numpy.uint8)
    focus_measures = []
    for i, shift in enumerate(shifts):
        mats = [numpy.hstack([unit_mat, shift * coord.reshape(2, 1)]) for coord in coords]
        shifted_imgs = [cv2.warpAffine(img, m, (w0, h0)) for img, m in zip(images, mats)]
        var_map = variance_map(shifted_imgs)
        prev_depth_map = depth_map.copy()
        depth_map[var_map < min_var_map] = shift
        if i > 0:
            still_pixs[depth_map != prev_depth_map] = 0

        min_var_map = numpy.min([min_var_map, var_map], axis=0)
        stacked_img = numpy.mean(shifted_imgs, axis=0)

        cv2.imshow('ttt', stacked_img.astype(numpy.uint8))
        cv2.imwrite('refocused_{}.jpg'.format(shift), stacked_img.astype(numpy.uint8))
        cv2.waitKey(10)

        focus_measure = 0
        for j in range(3):
            ch_grad = cv2.Laplacian(stacked_img[:, :, j], cv2.CV_64F)
            focus_measure += ch_grad.var()

        focus_measures.append(focus_measure)

    # Try to fix some never update pixels ...
    blurred_depth_map = scipy.ndimage.median_filter(depth_map, 5)
    depth_map[still_pixs == 1] = blurred_depth_map[still_pixs == 1]
    depth_map = cv2.resize(depth_map, (w_ref, h_ref))
    return depth_map, focus_measures

