"""Correct smooth color discontinuities in sky-only JPEGs of an aligned panorama.

Relative geometry and horizon/ground sources stay fixed. A small, regularized
RGB bias grid is fitted to real overlaps, with held-out validation. A final
global horizon rotation is enabled by default; --level-horizon off disables it.
This is appearance matching in encoded JPEG RGB, not a radiometric calibration.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import random
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
# The optional project-local wheels were installed for the bundled Python 3.12.
if sys.version_info[:2] == (3, 12):
    sys.path.insert(0, str(HERE.parent / ".tools/python-color"))

import numpy as np
from PIL import Image
from scipy import sparse
from scipy.sparse.linalg import lsqr
import tifffile

from stitch import TOKEN, Runner, field, find_bin, image_lines, image_value, read_pto, write_pto

GEOMETRY_KEYS = ("w", "h", "f", "v", "r", "p", "y", "a", "b", "c", "d", "e",
                 "g", "t", "TrX", "TrY", "TrZ", "Tpy", "Tpp")
NEUTRAL = dict(Eev=0, Er=1, Eb=1, Ra=0, Rb=0, Rc=0, Rd=0, Re=0,
               Va=1, Vb=0, Vc=0, Vd=0, Vx=0, Vy=0)
GRID = 4


def filename_token(line):
    tokens = [m for m in TOKEN.finditer(line) if m[0].startswith('n"')]
    if len(tokens) != 1:
        raise ValueError("Expected one PTO filename")
    return tokens[0]


def source_paths(lines, project):
    paths = []
    for line in image_lines(lines):
        name = Path(filename_token(line)[0][2:-1])
        paths.append((project.parent / name).resolve())
    return paths


def replace_sources(lines, replacements):
    result, index = [], 0
    for line in lines:
        if line.startswith("i "):
            if index in replacements:
                token = filename_token(line)
                line = line[:token.start()] + f'n"{replacements[index]}"' + line[token.end():]
            index += 1
        result.append(line)
    return result


def geometry_signature(lines):
    images = image_lines(lines)
    geometry = [[image_value(images, i, key) for key in GEOMETRY_KEYS]
                for i in range(len(images))]
    # Unknown image flags (crop, masks, etc.) must also match when reusing samples.
    extras = [[m[0] for m in TOKEN.finditer(line)
               if not m[0].startswith('n"') and not any(
                   m[0].startswith(key) for key in (*GEOMETRY_KEYS, *NEUTRAL))]
              for line in images]
    return geometry, extras


def select_sky(lines, margin=2.0):
    """Geometric sky candidate filter; it is not semantic sky segmentation.

    Restrict automatic selection to centered, undistorted rectilinear images.
    The current aerial set has clear sky above +2 degrees elevation.
    """
    images, selected = image_lines(lines), []
    for i in range(len(images)):
        value = lambda key: image_value(images, i, key)
        if value("f") != 0 or any(abs(value(k)) > 1e-8 for k in
                                  ("a", "b", "c", "d", "e", "g", "t", "TrX", "TrY", "TrZ")):
            raise ValueError("Automatic sky selection requires undistorted centered rectilinear geometry")
        pitch, roll = math.radians(value("p")), math.radians(value("r"))
        hx = math.tan(math.radians(value("v")) / 2)
        hy = hx * value("h") / value("w")
        # The lowest corner provides a conservative boundary for this upward view.
        low = math.sin(pitch) - abs(math.cos(pitch)) * (
            abs(math.cos(roll)) * hy + abs(math.sin(roll)) * hx)
        elevation = math.degrees(math.asin(max(-1, min(1, low / math.sqrt(1+hx*hx+hy*hy)))))
        if elevation > margin:
            selected.append(i)
    if not selected or len(selected) == len(images):
        raise ValueError("Need sky images and fixed horizon/ground reference images")
    return selected


def check_neutral(lines):
    pano = next(line for line in lines if line.startswith("p "))
    if float(field(pano, "E")) != 0 or float(field(pano, "R")) != 0:
        raise ValueError("Expected neutral LDR panorama exposure")
    if int(field(pano, "f")) != 2 or float(field(pano, "v")) != 360:
        raise ValueError("This renderer requires a full 360-degree equirectangular project")
    images = image_lines(lines)
    for i in range(len(images)):
        for key, expected in NEUTRAL.items():
            if abs(image_value(images, i, key) - expected) > 1e-8:
                raise ValueError(f"Image {i}: non-neutral {key}; use the neutral preview PTO to avoid double correction")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_samples(path, sizes):
    rows = np.loadtxt(path, ndmin=2)
    if rows.shape[1] != 16 or not np.isfinite(rows).all():
        raise ValueError("Expected finite Hugin overlap samples with 16 columns")
    ids = rows[:, :2]
    if np.any(ids != np.floor(ids)) or np.any(ids < 0) or np.any(ids >= len(sizes)):
        raise ValueError("Invalid image IDs in overlap samples")
    if np.any(ids[:, 0] == ids[:, 1]) or np.any(rows[:, 6:12] < 0) or np.any(rows[:, 6:12] > 1):
        raise ValueError("Invalid color values or self-pairs")
    valid = np.ones(len(rows), dtype=bool)
    for side in (0, 1):
        limits = np.asarray(sizes)[ids[:, side].astype(int)] - 1
        xy = rows[:, 2+2*side:4+2*side]
        # At -s 3, Hugin can export edge sample centers up to 4 original pixels
        # outside the image. Discard them instead of extrapolating our mesh.
        if np.any(xy < -8) or np.any(xy > limits+8):
            raise ValueError("Sample coordinates exceed source dimensions")
        valid &= (xy >= 0).all(axis=1) & (xy <= limits).all(axis=1)
    return rows[valid], int((~valid).sum())


def split_samples(rows, selected, seed=20261013):
    selected = set(selected)
    groups = defaultdict(list)
    for i, row in enumerate(rows):
        pair = tuple(sorted(map(int, row[:2])))
        if selected.intersection(pair):
            groups[pair].append(i)
    rng, train, validation = random.Random(seed), [], []
    for group in groups.values():
        rng.shuffle(group)
        count = min(18, max(1, len(group)//3))
        train.extend(group[:count])
        validation.extend(group[count:])
    if len(validation) < 50:
        raise ValueError("Too few unused overlap samples for validation")
    return rows[train], rows[validation]


def check_anchors(rows, selected):
    graph = defaultdict(set)
    for row in rows:
        a, b = map(int, row[:2])
        graph[a].add(b)
        graph[b].add(a)
    seen, queue = set(graph).difference(selected), list(set(graph).difference(selected))
    while queue:
        for other in graph[queue.pop()] - seen:
            seen.add(other)
            queue.append(other)
    missing = set(selected) - seen
    if missing:
        raise ValueError(f"Sky images have no overlap path to a fixed reference: {sorted(missing)}")


def weights(x, y, size, grid=GRID):
    x, y = x/(size[0]-1)*(grid-1), y/(size[1]-1)*(grid-1)
    ix, iy = min(int(x), grid-2), min(int(y), grid-2)
    u, v = x-ix, y-iy
    return ((iy*grid+ix, (1-u)*(1-v)), (iy*grid+ix+1, u*(1-v)),
            ((iy+1)*grid+ix, (1-u)*v), ((iy+1)*grid+ix+1, u*v))


def design(rows, selected, sizes, grid=GRID):
    lookup = {index: k*grid*grid for k, index in enumerate(selected)}
    rr, cc, data = [], [], []
    for n, row in enumerate(rows):
        for side, sign in ((0, 1), (1, -1)):
            index = int(row[side])
            if index not in lookup:
                continue
            for cell, weight in weights(*row[2+2*side:4+2*side], sizes[index], grid):
                rr.append(n)
                cc.append(lookup[index]+cell)
                data.append(sign*weight)
    return sparse.csr_matrix((data, (rr, cc)), shape=(len(rows), len(selected)*grid*grid))


def regularizer(count, grid=GRID):
    rr, cc, data = [], [], []
    row = 0
    def add(indices, coefficients):
        nonlocal row
        rr.extend([row]*len(indices))
        cc.extend(indices)
        data.extend(coefficients)
        row += 1
    for image in range(count):
        offset = image*grid*grid
        for y in range(grid):
            for x in range(grid):
                k = offset+y*grid+x
                add([k], [.025])
                if x < grid-1:
                    add([k, k+1], [-.4, .4])
                if y < grid-1:
                    add([k, k+grid], [-.4, .4])
                if x < grid-2:
                    add([k, k+1, k+2], [2, -4, 2])
                if y < grid-2:
                    add([k, k+grid, k+2*grid], [2, -4, 2])
    return sparse.csr_matrix((data, (rr, cc)), shape=(row, count*grid*grid))


def fit_mesh(train, selected, sizes):
    check_anchors(train, selected)
    matrix = design(train, selected, sizes)
    reg = regularizer(len(selected))
    fixed_pair = ~np.isin(train[:, :2].astype(int), selected).all(axis=1)
    base_weight = np.where(fixed_pair, 5.0, 1.0)
    target = train[:, 9:12] - train[:, 6:9]
    solution, diagnostics = np.zeros((matrix.shape[1], 3)), []
    for channel in range(3):
        weight = base_weight.copy()
        for iteration in range(8):
            scale = np.sqrt(weight)
            system = sparse.vstack([matrix.multiply(scale[:, None]), reg], format="csr")
            rhs = np.r_[target[:, channel]*scale, np.zeros(reg.shape[0])]
            fit = lsqr(system, rhs, atol=1e-7, btol=1e-7, iter_lim=1200)
            if fit[1] == 7:
                raise RuntimeError("Color solver reached its iteration limit")
            solution[:, channel] = fit[0]
            residual = matrix @ fit[0] - target[:, channel]
            weight = base_weight*np.minimum(1, .012/np.maximum(abs(residual), 1e-8))
        diagnostics.append({"channel": channel, "iterations_last_solve": fit[2]})
    model = solution.reshape(len(selected), GRID, GRID, 3)
    if not np.isfinite(model).all() or np.max(abs(model)) > .45:
        raise ValueError("Excessive or non-finite color correction; inspect the source geometry and overlaps")
    return model, diagnostics


def evaluate(rows, selected, sizes, model):
    before = rows[:, 6:9] - rows[:, 9:12]
    after = before + design(rows, selected, sizes) @ model.reshape(-1, 3)
    def metrics(residual):
        error = np.abs(residual).mean(axis=1)*255
        return {"median_rgb_levels": float(np.median(error)),
                "p95_rgb_levels": float(np.percentile(error, 95)),
                "mean_rgb_levels": float(error.mean()), "samples": len(error)}
    return {"before": metrics(before), "after": metrics(after)}


def mesh_rows(model, width, height, first, last):
    """Exact endpoint interpolation, identical to the overlap design matrix."""
    grid = model.shape[0]
    x = np.linspace(0, grid-1, width, dtype=np.float32)
    y = np.arange(first, last, dtype=np.float32)/(height-1)*(grid-1)
    ix, iy = np.minimum(x.astype(int), grid-2), np.minimum(y.astype(int), grid-2)
    u, v = (x-ix)[None, :, None], (y-iy)[:, None, None]
    return ((1-v)*((1-u)*model[iy[:, None], ix] + u*model[iy[:, None], ix+1])
            + v*((1-u)*model[iy[:, None]+1, ix] + u*model[iy[:, None]+1, ix+1]))


def correct_source(source, destination, model, expected_size):
    with Image.open(source) as image:
        if image.size != tuple(expected_size) or image.mode != "RGB":
            raise ValueError(f"Expected RGB source with PTO dimensions: {source}")
        rgb = np.asarray(image).copy()
        profile = image.info.get("icc_profile")
    height, width = rgb.shape[:2]
    result = np.empty(rgb.shape, dtype=np.uint16)
    clipped, new_clipped = 0, 0
    for y in range(0, height, 128):
        last = min(height, y+128)
        original = rgb[y:last].astype(np.float32)/255
        corrected = original + mesh_rows(model, width, height, y, last)
        outside = (corrected < 0) | (corrected > 1)
        clipped += int(outside.sum())
        new_clipped += int((outside & (original > 0) & (original < 1)).sum())
        result[y:last] = np.rint(np.clip(corrected, 0, 1)*65535).astype(np.uint16)
    if new_clipped/rgb.size > .005:
        raise ValueError(f"Correction clips >0.5% previously unsaturated channels: {source}")
    tags = [(34675, 'B', len(profile), profile, False)] if profile else []
    tifffile.imwrite(destination, result, photometric="rgb", compression="deflate",
                     metadata=None, extratags=tags)
    return {"source": str(source), "corrected": str(destination), "size": [width, height],
            "channel_values": int(rgb.size), "clipped": clipped, "new_clipped": new_clipped}


def render(runner, project, selected, count, width, height):
    # Nona refuses mixed UINT8/UINT16 input in one invocation. Both groups must
    # emit the same UINT16 depth for Enblend, preserving uncorrected ground files.
    prefix = runner.work / "layer"
    for name, indices in (("remap_originals", sorted(set(range(count))-set(selected))),
                          ("remap_corrected", selected)):
        if not indices:
            continue
        args = ["-m", "TIFF_m", "-p", "UINT16", "-z", "LZW"]
        for i in indices:
            args += ["-i", str(i)]
        runner.run(name, "nona", [*args, "-o", prefix, project])
    layers = [Path(f"{prefix}{i:04d}.tif") for i in range(count)]
    if not all(path.is_file() for path in layers):
        raise RuntimeError("Missing remapped layer")
    result = runner.work / "panorama.tif"
    runner.run("blend", "enblend", ["--wrap=HORIZONTAL", "--compression=LZW", "--depth=16",
                                    f"-f{width}x{height}", "-o", result, "--", *layers])
    with tifffile.TiffFile(result) as image:
        page = image.pages[0]
        if page.shape != (height, width, 4) or page.dtype != np.dtype("uint16"):
            raise RuntimeError(f"Unexpected panorama format: {page.shape}, {page.dtype}")
    with Image.open(result) as image:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (30, 34, 39, 255))
        background.alpha_composite(rgba)
        background.convert("RGB").save(runner.work / "preview.jpg", quality=94)
        bounds = rgba.getchannel("A").getbbox()
    return {"tiff": str(result), "preview": str(runner.work / "preview.jpg"),
            "width": width, "height": height, "channels": 4, "bits_per_channel": 16,
            "coverage_bbox": bounds}


def main(argv=None):
    from horizon import add_level_option, level_project
    from export_jpg import add_export_options, export, validate_options
    parser = argparse.ArgumentParser(description=__doc__)
    add_level_option(parser)
    add_export_options(parser)
    parser.add_argument("project", type=Path, nargs="?", default=HERE/"runs/optimized_v2/11_preview.pto")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--samples-from", type=Path, help="Reuse Hugin -w samples from the same geometry and sources")
    parser.add_argument("--samples-project", type=Path, help="Required with --samples-from for geometry/source checks")
    parser.add_argument("--hugin-bin")
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--sky-margin", type=float, default=2.0, help="Minimum footprint elevation in degrees")
    parser.add_argument("--stop-after", choices=("fit", "render"), default="render")
    args = parser.parse_args(argv)
    validate_options(args.width, args.jpeg_quality, args.tile_size)
    if bool(args.samples_from) != bool(args.samples_project):
        parser.error("--samples-from and --samples-project must be supplied together")
    if args.threads < 1 or not 0 <= args.sky_margin < 90:
        parser.error("Invalid thread count or sky margin")
    project = args.project.resolve()
    work = (args.output or HERE/"runs"/("sky_"+datetime.now().strftime("%Y%m%d_%H%M%S"))).resolve()
    work.mkdir(parents=True, exist_ok=False)
    runner = Runner(find_bin(args.hugin_bin), work, args.threads)
    report = {"status": "started", "source_project": str(project), "project_sha256": sha256(project),
              "method": "4x4 bilinear additive RGB mesh; fixed horizon/ground anchors; Huber IRLS",
              "domain": "encoded JPEG RGB; appearance matching, not physical lens calibration",
              "steps": runner.steps}
    started = time.perf_counter()
    try:
        lines, samples_path = read_pto(project), work/"samples.txt"
        images, sources = image_lines(lines), source_paths(lines, project)
        sizes = [(int(image_value(images, i, "w")), int(image_value(images, i, "h")))
                 for i in range(len(images))]
        selected = select_sky(lines, args.sky_margin)
        check_neutral(lines)
        if len({p.stem.lower() for p in sources}) != len(sources) or any(p.suffix.lower() not in (".jpg", ".jpeg") for p in sources):
            raise ValueError("Expected unique JPEG input sources")
        report["selected_indices_zero_based"] = selected
        report["selected_filenames"] = [sources[i].name for i in selected]
        report["source_hashes_sha256"] = {str(path): sha256(path) for path in sources}
        # Store an absolute-path copy so later working directories are irrelevant.
        absolute = replace_sources(lines, dict(enumerate(sources)))
        write_pto(work/"source.pto", absolute)
        print(f"[sky] {len(selected)}/{len(images)} images selected; original geometry stays fixed", flush=True)
        if args.samples_from:
            cached = read_pto(args.samples_project)
            if geometry_signature(cached) != geometry_signature(lines) or source_paths(cached, args.samples_project.resolve()) != sources:
                raise ValueError("Cached sample geometry or source order differs from this project")
            shutil.copyfile(args.samples_from, samples_path)
            report["samples_source"] = str(args.samples_from.resolve())
            report["samples_source_project"] = str(args.samples_project.resolve())
            report["samples_source_project_sha256"] = sha256(args.samples_project)
        else:
            # vig_optimize exports samples before solving. Only one dummy exposure
            # variable is offered to keep this extraction fast; discard its fit.
            sampling = [line for line in absolute if line != "v" and not line.startswith((
                "v ", "#hugin_optimizerMasterSwitch", "#hugin_optimizerPhotoMasterSwitch"))]
            sampling += ["#hugin_optimizerMasterSwitch 0", "#hugin_optimizerPhotoMasterSwitch 0",
                         f"v Eev{selected[0]}", "v"]
            write_pto(work/"sampling.pto", sampling)
            runner.run("sample", "vig_optimize", ["-p", "20000", "-s", "3", "-w", samples_path,
                       "-o", work/"unused_sampling_fit.pto", work/"sampling.pto"])
        report["samples_sha256"] = sha256(samples_path)
        rows, edge_samples = load_samples(samples_path, sizes)
        report["discarded_edge_samples"] = edge_samples
        train, validation = split_samples(rows, selected)
        np.savetxt(work/"train.txt", train, fmt="%.9g")
        np.savetxt(work/"validation.txt", validation, fmt="%.9g")
        model, solver = fit_mesh(train, selected, sizes)
        report.update(train_samples=len(train), validation_samples=len(validation), split_seed=20261013,
                      solver=solver, max_absolute_bias=float(abs(model).max()))
        report["validation"] = evaluate(validation, selected, sizes, model)
        anchored = ~np.isin(validation[:, :2].astype(int), selected).all(axis=1)
        if anchored.any():
            report["anchor_validation"] = evaluate(validation[anchored], selected, sizes, model)
        before, after = report["validation"]["before"], report["validation"]["after"]
        if after["mean_rgb_levels"] >= before["mean_rgb_levels"] or after["p95_rgb_levels"] > before["p95_rgb_levels"]:
            raise ValueError("Color correction did not improve held-out overlaps")
        np.savez(work/"model.npz", indices=selected, bias=model)
        print(f"[fit] Held-out median RGB difference: {before['median_rgb_levels']:.3f} -> {after['median_rgb_levels']:.3f} /255", flush=True)
        report["status"] = "fit_validated"
        if args.stop_after == "render":
            corrected_dir = work/"corrected"
            corrected_dir.mkdir()
            replacements, clipping = {}, []
            for k, index in enumerate(selected):
                destination = corrected_dir/(sources[index].stem+".tif")
                clipping.append(correct_source(sources[index], destination, model[k], sizes[index]))
                replacements[index] = destination
                print(f"[correct] {k+1}/{len(selected)} {sources[index].name}", flush=True)
            report["source_corrections"] = clipping
            report["new_clipped_channel_fraction"] = sum(row["new_clipped"] for row in clipping)/sum(row["channel_values"] for row in clipping)
            corrected = replace_sources(absolute, replacements)
            if geometry_signature(corrected) != geometry_signature(lines):
                raise RuntimeError("Unexpected geometry change")
            output_project = work/"corrected.pto"
            write_pto(output_project, corrected)
            output_project, report["horizon"] = level_project(
                runner, output_project, work/"leveled.pto", args.level_horizon)
            report["output"] = export(runner, output_project, args.width, args.jpeg_quality,
                                      args.tile_size, args.keep_intermediates, args.workers)
            report["status"] = "rendered"
        if any(sha256(path) != report["source_hashes_sha256"][str(path)] for path in sources):
            raise RuntimeError("An input source changed during execution")
        report["originals_unchanged"] = True
        leveled = report.get("horizon", {}).get("status") == "leveled"
        report["geometry_unchanged"] = not leveled
        report["relative_geometry_unchanged"] = True
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        report["seconds"] = round(time.perf_counter()-started, 3)
        (work/"report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[report] {work/'report.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
