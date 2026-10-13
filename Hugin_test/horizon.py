"""Level a panorama with one global spherical rotation; keep relative geometry.

Auto mode estimates a plane from a well-covered near-horizontal capture ring.
This assumes that ring was captured at approximately constant elevation; it is
not skyline detection or an independent gravity measurement. Hugin's native
straightener is the fallback when the ring is unavailable or inconsistent.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import statistics

from stitch import (HERE, Runner, TOKEN, field, find_bin, graph_report, image_lines,
                    image_value, read_pto, set_field, write_pto)


def add_level_option(parser, default="auto"):
    parser.add_argument("--level-horizon", nargs="?", const="auto", default=default,
                        choices=("auto", "off", "hugin"),
                        help="Global horizon leveling (default auto); off disables, hugin uses native straightening")


def solve3(matrix, target):
    """Pivoted elimination for the three-parameter plane fit (stdlib only)."""
    rows = [list(row)+[value] for row, value in zip(matrix, target)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda i: abs(rows[i][col]))
        rows[col], rows[pivot] = rows[pivot], rows[col]
        scale = rows[col][col]
        if abs(scale) < 1e-10:
            raise ValueError("Insufficient independent viewing directions for leveling")
        rows[col] = [v/scale for v in rows[col]]
        for i in range(3):
            if i != col:
                scale = rows[i][col]
                rows[i] = [a-scale*b for a, b in zip(rows[i], rows[col])]
    return [row[3] for row in rows]


def fit_ring(yaws, pitches):
    if len(yaws) != len(pitches) or len(yaws) < 8:
        raise ValueError("Need at least eight near-horizontal ring images")
    if not all(math.isfinite(v) for v in [*yaws, *pitches]):
        raise ValueError("Non-finite camera orientations")
    ordered = sorted(y % 360 for y in yaws)
    gaps = [b-a for a, b in zip(ordered, ordered[1:]+[ordered[0]+360])]
    coverage = 360-max(gaps)
    if coverage < 240:
        raise ValueError("Near-horizontal capture ring covers less than 240 degrees")
    # Sphere coordinates: x=right, y=up, z=forward. Fit y=a*x+b*z+c.
    design, target = [], []
    for yaw, pitch in zip(yaws, pitches):
        y, p = math.radians(yaw), math.radians(pitch)
        design.append((math.cos(p)*math.sin(y), math.cos(p)*math.cos(y), 1))
        target.append(math.sin(p))
    weights = [1.0]*len(target)
    for _ in range(8):
        matrix = [[sum(w*row[i]*row[j] for row, w in zip(design, weights))
                   for j in range(3)] for i in range(3)]
        rhs = [sum(w*row[i]*v for row, v, w in zip(design, target, weights)) for i in range(3)]
        coefficients = solve3(matrix, rhs)
        residual = [v-sum(a*b for a, b in zip(row, coefficients)) for row, v in zip(design, target)]
        median = statistics.median(residual)
        sigma = max(math.radians(.02), 1.4826*statistics.median(abs(v-median) for v in residual))
        weights = [min(1, 1.5*sigma/max(abs(v), 1e-12)) for v in residual]
    a, b, c = coefficients
    deviations = sorted(abs(math.degrees(v)) for v in residual)
    pitch = -math.degrees(math.atan2(b, math.sqrt(1+a*a)))
    roll = math.degrees(math.atan(a))
    return {"indices_count": len(yaws), "yaw_coverage_deg": coverage,
            "plane_coefficients": coefficients,
            "estimated_tilt_deg": math.degrees(math.atan(math.hypot(a, b))),
            "rotation_ypr_deg": [0.0, pitch, roll],
            "p90_fit_residual_deg": deviations[math.ceil(.9*len(deviations))-1],
            "rms_fit_residual_deg": math.sqrt(statistics.mean(v*v for v in deviations))}


def find_ring(lines):
    images = image_lines(lines)
    graph = graph_report(lines)
    if not graph["control_points"]:
        raise ValueError("No image-supported component for capture-ring leveling")
    indices = [i for i in graph["components"][0] if abs(image_value(images, i, "p")) <= 10]
    fit = fit_ring([image_value(images, i, "y") for i in indices],
                   [image_value(images, i, "p") for i in indices])
    if fit["p90_fit_residual_deg"] > .35 or fit["rms_fit_residual_deg"] > .5:
        raise ValueError("Near-horizontal images do not form a consistent capture ring")
    return {**fit, "reference_indices_zero_based": indices}


def matmul(a, b):
    return [[sum(a[i][k]*b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def transpose(a):
    return list(map(list, zip(*a)))


def orientation(yaw, pitch, roll):
    # Hugin's PTO convention in a forward/right/up basis: Rz(-y) Ry(-p) Rx(r).
    y, p, r = map(math.radians, (-yaw, -pitch, roll))
    cy, sy, cp, sp, cr, sr = math.cos(y), math.sin(y), math.cos(p), math.sin(p), math.cos(r), math.sin(r)
    return matmul(matmul([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]],
                         [[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]]),
                  [[1, 0, 0], [0, cr, -sr], [0, sr, cr]])


def verify_global_rotation(before, after, max_angle=5.0):
    old, new = image_lines(before), image_lines(after)
    if not old or len(old) != len(new):
        raise ValueError("Leveling changed the image count")
    old_matrices = [orientation(*(image_value(old, i, k) for k in ("y", "p", "r"))) for i in range(len(old))]
    new_matrices = [orientation(*(image_value(new, i, k) for k in ("y", "p", "r"))) for i in range(len(new))]
    rotation = matmul(new_matrices[0], transpose(old_matrices[0]))
    cosine = (sum(rotation[i][i] for i in range(3))-1)/2
    angle = math.degrees(math.acos(max(-1, min(1, cosine))))
    if not math.isfinite(angle) or angle > max_angle:
        raise ValueError(f"Automatic leveling rotation {angle:.3f} degrees exceeds {max_angle} degrees")
    error = max(abs(a-b) for old_matrix, new_matrix in zip(old_matrices, new_matrices)
                for expected, actual in zip(matmul(rotation, old_matrix), new_matrix)
                for a, b in zip(expected, actual))
    if not math.isfinite(error) or error > 1e-7:
        raise ValueError("Leveling is not one common rotation; relative image alignment would change")
    return {"global_rotation_deg": angle, "matrix_max_error": error,
            "relative_orientations_preserved": True, "rotation_matrix": rotation}


def level_project(runner, source, output, mode="auto"):
    if mode == "off":
        return source, {"mode": mode, "status": "disabled"}
    if mode not in ("auto", "hugin"):
        raise ValueError(f"Unknown horizon mode: {mode}")
    before = read_pto(source)
    images = image_lines(before)
    for i in range(len(images)):
        for key in ("TrX", "TrY", "TrZ"):
            try:
                value = image_value(images, i, key)
            except ValueError:
                value = 0.0  # Absent translation fields mean zero in a PTO.
            if abs(value) > 1e-10:
                raise ValueError("Horizon leveling currently requires a rotation-only project")
    report = {"mode": mode, "status": "running"}
    ring = None
    if mode == "auto":
        try:
            ring = find_ring(before)
        except ValueError as error:
            report["fallback_reason"] = str(error)
    if ring is not None:
        report.update(method="robust_capture_ring", before=ring,
                      assumption="Near-horizontal reference images were captured at approximately constant elevation")
        if ring["estimated_tilt_deg"] > 5:
            raise ValueError("Estimated tilt exceeds the 5-degree automatic correction limit")
        rotation = ",".join(f"{value:.12g}" for value in ring["rotation_ypr_deg"])
        options = [f"--rotate={rotation}"]
    else:
        report["method"] = "hugin_straighten"
        options = ["--straighten"]
    # Resolve filenames for Hugin's temporary output (PTO paths may be relative).
    absolute = []
    for line in before:
        if line.startswith("i "):
            token = next(t for t in TOKEN.finditer(line) if t[0].startswith('n"'))
            name = (source.parent/Path(token[0][2:-1])).resolve()
            line = line[:token.start()]+f'n"{name}"'+line[token.end():]
        absolute.append(line)
    input_copy = runner.work/(output.stem+"_input.pto")
    native_output = runner.work/(output.stem+"_native.pto")
    write_pto(input_copy, absolute)
    runner.run(output.stem, "pano_modify", [*options, "-o", native_output, input_copy])
    rotated = image_lines(read_pto(native_output))
    if len(rotated) != len(images):
        raise ValueError("Hugin changed the image count")
    result, index = [], 0
    # Accept only native y/p/r changes. Keep canvas, lens, colors, source paths,
    # masks and control-point coordinates; --straighten can otherwise resize.
    for line in absolute:
        if line.startswith("i "):
            for key in ("y", "p", "r"):
                line = set_field(line, key, image_value(rotated, index, key))
            index += 1
        result.append(line)
    report["invariants"] = verify_global_rotation(before, result)
    if ring is not None:
        ids = ring["reference_indices_zero_based"]
        after = image_lines(result)
        report["after"] = fit_ring([image_value(after, i, "y") for i in ids],
                                    [image_value(after, i, "p") for i in ids])
        if report["after"]["estimated_tilt_deg"] > .02:
            raise ValueError("Estimated capture ring was not leveled successfully")
    write_pto(output, result)
    report.update(status="leveled", output_project=str(output))
    print(f"[horizon] {report['method']}: global rotation {report['invariants']['global_rotation_deg']:.3f} degrees", flush=True)
    return output, report


def main(argv=None):
    from export_jpg import add_export_options, export, validate_options
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, nargs="?", default=HERE/"runs/sky_balanced_v2/corrected.pto")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hugin-bin")
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--stop-after", choices=("level", "render"), default="render")
    add_level_option(parser)
    add_export_options(parser)
    args = parser.parse_args(argv)
    validate_options(args.width, args.jpeg_quality, args.tile_size)
    if args.threads < 1:
        parser.error("--threads must be positive")
    source = args.project.resolve()
    work = (args.output or HERE/"runs"/("leveled_"+datetime.now().strftime("%Y%m%d_%H%M%S_%f"))).resolve()
    work.mkdir(parents=True, exist_ok=False)
    runner = Runner(find_bin(args.hugin_bin), work, args.threads)
    report = {"source_project": str(source), "status": "started", "steps": runner.steps}
    try:
        project, report["horizon"] = level_project(runner, source, work/"leveled.pto", args.level_horizon)
        if args.stop_after == "render":
            report["output"] = export(runner, project, args.width, args.jpeg_quality,
                                      args.tile_size, args.keep_intermediates, args.workers)
            report["status"] = "rendered"
        else:
            report["status"] = "leveled" if args.level_horizon != "off" else "disabled"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        (work/"report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[report] {work/'report.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
