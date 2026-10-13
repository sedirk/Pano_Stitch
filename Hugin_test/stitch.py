"""Checked Hugin baseline; rendering uses the full-resolution JPEG exporter."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
TOKEN = re.compile(r'(?:[^\s"]|"[^"]*")+')


def field(line: str, key: str) -> str:
    for token in TOKEN.finditer(line):
        match = re.fullmatch(re.escape(key) + r"(=\d+|" + NUMBER + r")", token[0])
        if match:
            return match[1]
    raise ValueError(f"Missing PTO field {key}: {line[:120]}")


def set_field(line: str, key: str, value: float) -> str:
    # Quoted filenames are single tokens, even when they contain " p0 ".
    pattern = re.escape(key) + r"(?:=\d+|" + NUMBER + r")"
    matches = [token for token in TOKEN.finditer(line) if re.fullmatch(pattern, token[0])]
    if len(matches) != 1:
        raise ValueError(f"Expected one {key} field, found {len(matches)}")
    match = matches[0]
    return line[:match.start()] + f"{key}{value:.12g}" + line[match.end():]


def read_pto(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8-sig").splitlines()


def write_pto(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def image_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("i ")]


def image_value(images: list[str], index: int, key: str) -> float:
    visited = set()
    while True:
        if index in visited or not 0 <= index < len(images):
            raise ValueError(f"Invalid PTO link for {key}")
        visited.add(index)
        value = field(images[index], key)
        if not value.startswith("="):
            return float(value)
        index = int(value[1:])


def control_points(lines: list[str]) -> list[dict]:
    points = []
    for line in lines:
        if not line.startswith("c ") or int(field(line, "t")) != 0:
            continue
        point = {key: float(field(line, key)) for key in ("x", "y", "X", "Y")}
        point.update(n=int(field(line, "n")), N=int(field(line, "N")), line=line)
        points.append(point)
    return points


def graph_report(lines: list[str]) -> dict:
    images = image_lines(lines)
    adjacency = [set() for _ in images]
    counts = Counter()
    for cp in control_points(lines):
        a, b = cp["n"], cp["N"]
        if not (0 <= a < len(images) and 0 <= b < len(images)) or a == b:
            raise ValueError("Invalid image index in control point")
        adjacency[a].add(b)
        adjacency[b].add(a)
        counts[tuple(sorted((a, b)))] += 1
    seen, groups = set(), []
    for index in range(len(images)):
        if index in seen:
            continue
        queue, group = [index], []
        seen.add(index)
        while queue:
            current = queue.pop()
            group.append(current)
            for other in adjacency[current] - seen:
                seen.add(other)
                queue.append(other)
        groups.append(sorted(group))
    groups.sort(key=lambda group: (-len(group), group[0]))
    return {"images": len(images), "control_points": sum(counts.values()),
            "matched_pairs": len(counts), "components": groups, "component_count": len(groups),
            "isolated_images": [i for i, neighbors in enumerate(adjacency) if not neighbors]}


def wrap_angle(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def pose_from_metadata(row: dict) -> tuple[float, float, float] | None:
    try:
        yaw, pitch, roll = (float(row[key]) for key in
                            ("GimbalYawDegree", "GimbalPitchDegree", "GimbalRollDegree"))
    except (KeyError, ValueError, TypeError):
        return None
    if not all(math.isfinite(v) for v in (yaw, pitch, roll)) or not -90 <= pitch <= 90:
        return None
    return yaw, pitch, roll


def initialize_poses(lines: list[str], metadata: list[dict], enabled: bool) -> list[str]:
    if len(image_lines(lines)) != len(metadata):
        raise ValueError("Image count differs between PTO and metadata")
    if not enabled:
        return lines.copy()
    poses = [pose_from_metadata(row) for row in metadata]
    if any(pose is None for pose in poses):
        raise ValueError("Prealignment requires complete valid yaw, pitch and roll metadata")
    origin = poses[0][0]
    result, index = [], 0
    for line in lines:
        if line.startswith("i "):
            yaw, pitch, roll = poses[index]
            for key, value in (("y", wrap_angle(yaw-origin)), ("p", pitch), ("r", roll)):
                line = set_field(line, key, value)
            index += 1
        result.append(line)
    return result


def configure_optimizer(lines: list[str], has_priors: bool, optimize_hfov: bool = False,
                        optimize_distortion: bool = False) -> tuple[list[str], dict]:
    graph = graph_report(lines)
    if graph["control_points"] == 0:
        raise ValueError("No control points: refusing to call this a registered panorama")
    if graph["component_count"] > 1 and not has_priors:
        raise ValueError("Disconnected images without complete pose priors; inspect report.json")
    variables = []
    # Every component needs a fixed anchor. Isolated images have no free variables.
    for component in graph["components"]:
        variables.extend(f"v y{i} p{i} r{i}" for i in component[1:])
    if optimize_hfov:
        images = image_lines(lines)
        lens_roots = set()
        for index in graph["components"][0]:
            seen = set()
            while field(images[index], "v").startswith("="):
                if index in seen:
                    raise ValueError("Circular HFOV link")
                seen.add(index)
                index = int(field(images[index], "v")[1:])
            lens_roots.add(index)
        variables.extend(f"v v{i}" for i in sorted(lens_roots))
    if optimize_distortion:
        images = image_lines(lines)
        roots = set()
        for index in graph['components'][0]:
            seen = set()
            while field(images[index], 'b').startswith('='):
                if index in seen:
                    raise ValueError('Circular distortion link')
                seen.add(index)
                index = int(field(images[index], 'b')[1:])
            roots.add(index)
        variables.extend(f'v b{i}' for i in sorted(roots))
    result = [line for line in lines if line != "v" and not line.startswith("v ")
              and not line.startswith(("#hugin_optimizerMasterSwitch", "#hugin_optimizerPhotoMasterSwitch"))]
    # Hugin's automatic presets otherwise overwrite the explicit v lines at load.
    result.extend(["", "#hugin_optimizerMasterSwitch 0", "#hugin_optimizerPhotoMasterSwitch 0",
                   "# Explicit geometry-only optimization", *variables, "v"])
    return result, graph


def find_bin(requested: str | None) -> Path:
    if requested or os.environ.get("HUGIN_BIN"):
        candidates = [Path(requested or os.environ["HUGIN_BIN"])]
    else:
        candidates = [HERE.parent / ".tools/hugin/Hugin/bin", Path("D:/Program Files/Hugin/bin"),
                      Path("C:/Program Files/Hugin/bin")]
        if executable := shutil.which("pto_gen"):
            candidates.insert(0, Path(executable).parent)
    required = ("pto_gen", "cpfind", "cpclean", "autooptimiser", "pano_modify", "pano_trafo", "exiftool", "hugin_executor")
    suffix = ".exe" if os.name == "nt" else ""
    for directory in candidates:
        if all((directory / (tool+suffix)).is_file() for tool in required):
            return directory.resolve()
    raise FileNotFoundError("Hugin tools not found. Set --hugin-bin to the Hugin bin directory.")


class Runner:
    def __init__(self, binary_dir: Path, work: Path, threads: int):
        self.binary_dir, self.work = binary_dir, work
        self.steps = []
        self.env = os.environ.copy()
        self.env.update(PATH=str(binary_dir)+os.pathsep+self.env.get("PATH", ""),
                        OMP_NUM_THREADS=str(threads), LC_ALL="C", LANG="C", LC_CTYPE="C")

    def run(self, stage: str, tool: str, arguments: list, stdin: str | None = None) -> str:
        executable = self.binary_dir / (tool + (".exe" if os.name == "nt" else ""))
        command = [str(executable), *map(str, arguments)]
        print(f"[{stage}] {tool}", flush=True)
        start = time.perf_counter()
        result = subprocess.run(command, input=stdin, capture_output=True, encoding="utf-8",
                                errors="replace", cwd=self.work, env=self.env)
        log = self.work / f"{stage}.log"
        log.write_text(json.dumps(command, ensure_ascii=False)+"\n\n"+result.stdout+"\n"+result.stderr,
                       encoding="utf-8")
        self.steps.append({"stage": stage, "command": command, "seconds": round(time.perf_counter()-start, 3),
                           "returncode": result.returncode, "log": str(log)})
        if result.returncode:
            raise RuntimeError(f"{tool} exited with {result.returncode}; see {log}\n{result.stderr[-1200:]}")
        return result.stdout


def residuals(runner: Runner, stage: str, project: Path, points: list[dict]) -> list[float]:
    """Native projection -> ray angle -> equivalent source pixels (fit residual).

    This metric avoids the pole inflation of equirectangular canvas distances.
    """
    if not points:
        return []
    lines = read_pto(project)
    images = image_lines(lines)
    pano = next(line for line in lines if line.startswith("p "))
    if int(field(pano, "f")) != 2:
        raise ValueError("Residual diagnostics require equirectangular output")
    width, height, fov = (float(field(pano, key)) for key in ("w", "h", "v"))
    samples = []
    for cp in points:
        samples.extend([f"{cp['n']} {cp['x']:.10f} {cp['y']:.10f}",
                        f"{cp['N']} {cp['X']:.10f} {cp['Y']:.10f}"])
    output = runner.run(stage, "pano_trafo", [project], "\n".join(samples)+"\n")
    coordinates = [tuple(map(float, line.split())) for line in output.splitlines() if line.strip()]
    if len(coordinates) != 2*len(points) or any(len(xy) != 2 for xy in coordinates):
        raise ValueError("Unexpected pano_trafo output")
    rays = []
    for x, y in coordinates:
        # pano_trafo uses pixel centers: the spherical origin is (w/2-.5,h/2-.5).
        lon = math.radians((x+.5-width/2)*fov/width)
        lat = math.radians((height/2-y-.5)*fov/width)
        rays.append((math.cos(lat)*math.sin(lon), math.sin(lat), math.cos(lat)*math.cos(lon)))
    errors = []
    for i, cp in enumerate(points):
        chord = math.sqrt(sum((a-b)**2 for a, b in zip(rays[2*i], rays[2*i+1])))
        angle = 2*math.asin(min(1, chord/2))
        focal = [image_value(images, n, "w")/(2*math.tan(math.radians(image_value(images, n, "v"))/2))
                 for n in (cp["n"], cp["N"])]
        errors.append(angle*statistics.mean(focal))
    if not all(math.isfinite(error) for error in errors):
        raise ValueError("Non-finite projection residual")
    return errors


def error_summary(errors: list[float]) -> dict:
    if not errors:
        return {"count": 0}
    ordered = sorted(errors)
    return {"count": len(errors), "median_px": round(statistics.median(errors), 4),
            "mean_px": round(statistics.mean(errors), 4),
            "p95_px": round(ordered[math.ceil(.95*len(ordered))-1], 4), "max_px": round(ordered[-1], 4)}


def filter_points(lines: list[str], keep: list[bool]) -> list[str]:
    flags = iter(keep)
    result = []
    for line in lines:
        if line.startswith("c ") and int(field(line, "t")) == 0 and not next(flags):
            continue
        result.append(line)
    return result


def guard_geometry(initial: list[str], fitted: list[str], has_priors: bool, max_change: float) -> None:
    before, after = image_lines(initial), image_lines(fitted)
    if len(before) != len(after):
        raise ValueError("Optimizer changed image count")
    for i in range(len(before)):
        fov = image_value(after, i, "v")
        if not math.isfinite(fov) or not 1 < fov < 179:
            raise ValueError(f"Invalid rectilinear HFOV for image {i}: {fov}")
        if not all(math.isfinite(image_value(after, i, key)) for key in ("y", "p", "r")):
            raise ValueError(f"Non-finite pose for image {i}")
        if has_priors:
            # Yaw alone is ill-conditioned near the poles: compare optical axes.
            y1, p1 = (math.radians(image_value(before, i, k)) for k in ("y", "p"))
            y2, p2 = (math.radians(image_value(after, i, k)) for k in ("y", "p"))
            dot = math.sin(p1)*math.sin(p2)+math.cos(p1)*math.cos(p2)*math.cos(y1-y2)
            delta = math.degrees(math.acos(max(-1, min(1, dot))))
            if delta > max_change:
                raise ValueError(f"Image {i} moved {delta:.2f} degrees from its prior (limit {max_change})")
            if abs(math.degrees(p1)) < 75 and abs(math.degrees(p2)) < 75:
                roll_change = abs(wrap_angle(image_value(after, i, "r")-image_value(before, i, "r")))
                if roll_change > max_change:
                    raise ValueError(f"Image {i} roll changed {roll_change:.2f} degrees from its prior")


def execute(args: argparse.Namespace) -> Path:
    folder = args.images.resolve()
    images = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg"))
    if len(images) < 2:
        raise ValueError("At least two JPEG images are required")
    binary_dir = find_bin(args.hugin_bin)
    work = (args.work_dir or HERE/"runs"/datetime.now().strftime("%Y%m%d_%H%M%S_%f")).resolve()
    work.mkdir(parents=True, exist_ok=False)
    runner = Runner(binary_dir, work, args.threads)
    report = {"status": "running", "input": str(folder), "profile": args.profile,
              "images": [str(p) for p in images], "warnings": [], "steps": runner.steps}
    print(f"Run directory: {work}", flush=True)
    try:
        tags = ["-json", "-n", "-charset", "filename=UTF8", "-GimbalYawDegree", "-GimbalPitchDegree",
                "-GimbalRollDegree", "-Orientation", "-ImageWidth", "-ImageHeight", "-FocalLengthIn35mmFormat"]
        rows = json.loads(runner.run("00_metadata", "exiftool", [*tags, *images]))
        by_path = {os.path.normcase(str(Path(row["SourceFile"]).resolve())): row for row in rows}
        metadata = [by_path[os.path.normcase(str(p))] for p in images]
        if any(int(row.get("Orientation", 1)) != 1 for row in metadata):
            raise ValueError("Non-normal EXIF orientation: normalize images and pose coordinates first")
        has_priors = args.pose_source != "none" and all(pose_from_metadata(row) is not None for row in metadata)
        if args.pose_source == "dji" and not has_priors:
            raise ValueError("--pose-source=dji requires complete valid attitude metadata")
        report.update(metadata=metadata, has_complete_pose_priors=has_priors)
        generated, initial = work/"01_generated.pto", work/"02_initial.pto"
        gen_args = ["--ignore-fov-rectilinear", "-o", generated]
        if args.hfov is not None:
            gen_args += ["-f", args.hfov]
        runner.run("01_generate", "pto_gen", [*gen_args, *images])
        initial_lines = initialize_poses(read_pto(generated), metadata, has_priors)
        if any(int(field(line, "f")) != 0 for line in image_lines(initial_lines)):
            raise ValueError("This prototype supports rectilinear images; fisheye needs another geometry profile")
        guard_geometry(initial_lines, initial_lines, False, args.max_pose_change)
        write_pto(initial, initial_lines)
        report["initial_hfov"] = image_value(image_lines(initial_lines), 0, "v")
        if args.stop_after == "prepare":
            report["status"] = "prepared"
            return work
        matched = work/"03_matched.pto"
        match_args = ["--prealigned" if has_priors else "--multirow", f"--ncores={args.threads}"]
        if args.profile == "optimized":
            match_args += ["--ransacmode="+("rpy" if has_priors else "hom"),
                           "--ransacdist=8", "--minmatches=12", "--sieve2size=2"]
        if args.celeste:
            match_args.append("--celeste")
        if args.matches_from:
            source = args.matches_from.resolve()
            cached_lines = read_pto(source)
            cached_images = image_lines(cached_lines)
            current_images = image_lines(initial_lines)
            if len(cached_images) != len(current_images):
                raise ValueError("Cached matches use a different image count")
            for cached, original in zip(cached_images, current_images):
                cached_name = next(token[0] for token in TOKEN.finditer(cached) if token[0].startswith('n"'))
                original_name = next(token[0] for token in TOKEN.finditer(original) if token[0].startswith('n"'))
                if cached_name != original_name or any(field(cached, key) != field(original, key) for key in ("w", "h", "f")):
                    raise ValueError("Cached matches must use the same ordered source images and image dimensions")
            write_pto(matched, initial_lines + [line for line in cached_lines if line.startswith("c ")])
            report["matches_reused_from"] = str(source)
        else:
            runner.run("03_match", "cpfind", [*match_args, "-o", matched, initial])
        report["matched_graph"] = graph_report(read_pto(matched))
        if args.stop_after == "match":
            report["status"] = "matched"
            return work
        current = matched
        if args.profile == "optimized":
            cleaned = work/"04_cleaned.pto"
            runner.run("04_clean", "cpclean", ["--pairwise-checking", "-o", cleaned, current])
            current = cleaned
        if not has_priors:
            # Calibrated bearing fits avoid flipped homography/pairwise poses
            # in narrow, low-texture sea/sky overlaps.
            from camera_alignment import initialize
            initialized, report['camera_initialization'] = initialize(read_pto(current))
            aligned = work/"04_pairwise.pto"
            write_pto(aligned, initialized)
            current = aligned
        configured, graph = configure_optimizer(read_pto(current), has_priors, args.optimize_hfov, args.optimize_distortion)
        report["cleaned_graph"] = graph
        if args.strict and graph["component_count"] != 1:
            raise ValueError("Strict mode: disconnected control-point graph")
        variables = work/"05_variables.pto"
        write_pto(variables, configured)
        fitted = work/"06_fitted.pto"
        # -n honors the variable set and retains metadata/pairwise initialization.
        runner.run("06_optimize", "autooptimiser", ["-n", "-o", fitted, variables])
        guard_geometry(initial_lines, read_pto(fitted), has_priors, args.max_pose_change)
        points = control_points(read_pto(fitted))
        errors = residuals(runner, "06_residuals", fitted, points)
        report["first_fit_residuals"] = error_summary(errors)
        current = fitted
        if args.profile == "optimized" and errors:
            median = statistics.median(errors)
            mad = statistics.median(abs(e-median) for e in errors)
            threshold = max(8.0, min(24.0, median+3*1.4826*mad))
            keep = [e <= threshold for e in errors]
            counts = Counter(tuple(sorted((cp["n"], cp["N"]))) for cp, valid in zip(points, keep) if valid)
            keep = [valid and counts[tuple(sorted((cp["n"], cp["N"])))] >= 8
                    for cp, valid in zip(points, keep)]
            configured, _ = configure_optimizer(filter_points(read_pto(fitted), keep), has_priors, args.optimize_hfov, args.optimize_distortion)
            trimmed, refined = work/"07_trimmed.pto", work/"08_refined.pto"
            write_pto(trimmed, configured)
            runner.run("08_refine", "autooptimiser", ["-n", "-o", refined, trimmed])
            current = refined
            report["pruning"] = {"threshold_equivalent_source_px": threshold, "removed": len(keep)-sum(keep)}
        final_lines = read_pto(current)
        guard_geometry(initial_lines, final_lines, has_priors, args.max_pose_change)
        final_graph = graph_report(final_lines)
        report["final_graph"] = final_graph
        if args.strict and final_graph["component_count"] != 1:
            raise ValueError("Strict mode: pruning disconnected the graph")
        if final_graph["component_count"] > 1:
            warning = (f"{final_graph['component_count']} control-point graph components; {len(final_graph['isolated_images'])} images "
                       "have no control points and retain pose priors. Component placement is not image-verified.")
            report["warnings"].append(warning)
            print("WARNING: "+warning, flush=True)
        report["final_fit_residuals"] = error_summary(residuals(runner, "09_residuals", current, control_points(final_lines)))
        report["residual_units"] = "ray angle times mean source focal length (pixels); fit residual, not independent accuracy"
        report["fitted_hfov"] = image_value(image_lines(final_lines), 0, "v")
        from horizon import level_project
        current, report["horizon"] = level_project(runner, current, work/"09_leveled.pto", args.level_horizon)
        if args.stop_after == "optimize":
            report["status"] = "optimized_with_priors" if final_graph["component_count"] > 1 else "optimized"
            return work
        render_source = current
        if args.photometric:
            if final_graph["component_count"] != 1:
                raise ValueError("Automatic photometric optimization needs a connected graph in this pipeline")
            photo = work/"10_photometric.pto"
            runner.run("10_photometric", "autooptimiser", ["-m", "-o", photo, current])
            render_source = photo
        else:
            # JPEG tones are already processed. EXIF EV is not a calibrated gain.
            tones = [set_field(line, "Eev", 0) if line.startswith("i ") else line for line in read_pto(current)]
            render_source = work/"10_source_tones.pto"
            write_pto(render_source, tones)
        render_project = work/"11_render.pto"
        runner.run("11_canvas", "pano_modify", ["--projection=2", "--fov=360x180",
                   "--canvas=AUTO" if args.width is None else f"--canvas={args.width}x{args.width//2}",
                   "--output-exposure="+("AUTO" if args.photometric else "0"),
                   "--output-type=NORMAL", "--ldr-file=TIF", "--ldr-compression=LZW", "--blender=ENBLEND",
                   "--blender-args=--wrap=HORIZONTAL", "-o", render_project, render_source])
        from export_jpg import export
        report["output"] = export(runner, render_project, args.width, args.jpeg_quality,
                                  args.tile_size, args.keep_intermediates, args.workers)
        report["status"] = "rendered_with_priors" if final_graph["component_count"] > 1 else "rendered"
        return work
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        (work/"report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    from horizon import add_level_option
    from export_jpg import add_export_options
    result = argparse.ArgumentParser(description=__doc__)
    add_level_option(result)
    add_export_options(result)
    result.add_argument("images", nargs="?", type=Path, default=HERE/"001_0546")
    result.add_argument("--hugin-bin")
    result.add_argument("--work-dir", type=Path, help="New, non-existing output directory")
    result.add_argument("--matches-from", type=Path, help="Reuse control points from a PTO with the same ordered images")
    result.add_argument("--profile", choices=("baseline", "optimized"), default="optimized")
    result.add_argument("--pose-source", choices=("auto", "none", "dji"), default="auto")
    result.add_argument("--hfov", type=float, help="Input HFOV override; default reads EXIF")
    result.add_argument("--optimize-hfov", action="store_true", help="Fit shared HFOV along with poses")
    result.add_argument("--optimize-distortion", action="store_true", help="Fit each lens group's radial b coefficient")
    result.add_argument("--max-pose-change", type=float, default=5.0)
    result.add_argument("--threads", type=int, default=min(8, os.cpu_count() or 1))
    result.add_argument("--celeste", action="store_true", help="Exclude likely clouds from features")
    result.add_argument("--photometric", action="store_true", help="Fit exposure/WB; requires connected graph")
    result.add_argument("--strict", action="store_true", help="Stop on disconnected images even with priors")
    result.add_argument("--stop-after", choices=("prepare", "match", "optimize", "render"), default="render")
    return result


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    from export_jpg import validate_options
    validate_options(options.width, options.jpeg_quality, options.tile_size)
    if options.threads < 1:
        raise SystemExit("Use at least one thread")
    if options.hfov is not None and not 1 < options.hfov < 179:
        raise SystemExit("Rectilinear HFOV must be between 1 and 179 degrees")
    if not math.isfinite(options.max_pose_change) or options.max_pose_change <= 0:
        raise SystemExit("--max-pose-change must be finite and positive")
    try:
        output = execute(options)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Done. Report: {output/'report.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
