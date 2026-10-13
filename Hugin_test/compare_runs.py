"""Compare geometry on one common set of existing correspondences.

These points are NOT independent ground truth: this is a diagnostic comparison.
"""
import argparse
import json
from pathlib import Path

from stitch import TOKEN, Runner, control_points, error_summary, field, graph_report, image_lines, read_pto, residuals, find_bin


def image_name(line: str) -> str:
    name = next(token[0][2:-1] for token in TOKEN.finditer(line) if token[0].startswith('n"'))
    return name.replace('\\', '/').rsplit('/', 1)[-1].casefold()


def compare(original: Path, baseline: Path, optimized: Path, output: Path) -> dict:
    # A rejected fit is still useful diagnostically; label it explicitly below.
    output.mkdir(parents=True, exist_ok=True)
    projects = {"original_saved_project": original.resolve(), "corrected_baseline": baseline.resolve(),
                "optimized": optimized.resolve()}
    common_images = set(graph_report(read_pto(optimized))["components"][0])
    # Use the same old correspondences for each projection, not each run's retained set.
    points = [cp for cp in control_points(read_pto(original)) if cp["n"] in common_images and cp["N"] in common_images]
    source_images = image_lines(read_pto(original))
    for project in projects.values():
        images = image_lines(read_pto(project))
        if len(images) != len(source_images) or any(
                field(a, "w") != field(b, "w") or field(a, "h") != field(b, "h") or image_name(a) != image_name(b)
                for a, b in zip(images, source_images)):
            raise ValueError("Image dimensions/order must agree")
    runner = Runner(find_bin(None), output.resolve(), 2)
    report = {"metric": "equivalent source-pixel ray residual", "common_point_count": len(points),
              "warning": "Same existing correspondences, not independent check points or ground truth.", "runs": {}}
    for name, project in projects.items():
        lines = read_pto(project)
        graph = graph_report(lines)
        report["runs"][name] = {
            "project": str(project),
            "images": graph["images"], "control_points": graph["control_points"],
            "pairs": graph["matched_pairs"], "components": graph["component_count"],
            "isolated_images": len(graph["isolated_images"]),
            "own_points_residual": error_summary(residuals(runner, name+"_own", project, control_points(lines))),
            "common_points_residual": error_summary(residuals(runner, name+"_common", project, points)),
        }
        state_file = project.parent / "report.json"
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            report["runs"][name].update(pipeline_status=state["status"], failure=state.get("error"))
        else:
            report["runs"][name]["pipeline_status"] = "existing_project_not_rerun"
    (output/"comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("optimized", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent/"diagnostics/comparison")
    args = parser.parse_args()
    print(json.dumps(compare(args.original, args.baseline, args.optimized, args.output), ensure_ascii=False, indent=2))
