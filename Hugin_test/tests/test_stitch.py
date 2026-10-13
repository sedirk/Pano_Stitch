"""Regression tests for actual failure modes of the old stitching script."""
from pathlib import Path
import math
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stitch


IMAGE = 'i w4032 h3024 f0 v27.774389 Eev12.614709 r0 p0 y0 n"D:/my photos/y0 p0 v30.JPG"'
CP = 'c n0 N1 x100 y200 X101 Y201 t0'


class PipelineTests(unittest.TestCase):
    def test_residuals_respect_hugin_half_pixel_center_at_high_latitude(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)/'sphere.pto'
            stitch.write_pto(project, ['p f2 w3600 h1800 v360', IMAGE, IMAGE])
            runner = stitch.Runner(Path(folder), Path(folder), 1)
            points = [dict(n=0, N=1, x=1, y=1, X=2, Y=2)]
            # Longitude 0 / .1 degrees, both at latitude 60 degrees.
            with patch.object(runner, 'run', return_value='1799.5 299.5\n1800.5 299.5\n'):
                actual = stitch.residuals(runner, 'test', project, points)[0]
            focal = 4032/(2*math.tan(math.radians(27.774389)/2))
            expected = 2*math.asin(math.cos(math.radians(60))*math.sin(math.radians(.1)/2))*focal
            self.assertAlmostEqual(actual, expected, places=8)

    def test_hfov_edit_never_changes_exposure_or_filename(self):
        edited = stitch.set_field(IMAGE, "v", 29.5)
        self.assertEqual(stitch.field(edited, "v"), "29.5")
        self.assertIn("Eev12.614709", edited)
        self.assertTrue(edited.endswith('n"D:/my photos/y0 p0 v30.JPG"'))

    def test_integer_negative_scientific_and_linked_fields(self):
        for token in ("y0", "y-15", "y+0.4", "y1e-3", "y=0"):
            line = f'i {token} p0 r0 n"photo.JPG"'
            edited = stitch.set_field(line, "y", -32.5)
            self.assertEqual(stitch.field(edited, "y"), "-32.5")
        with self.assertRaises(ValueError):
            stitch.set_field("i p0", "y", 1)

    def test_priors_accept_negative_integers_but_not_missing_data(self):
        self.assertIsNone(stitch.pose_from_metadata({}))
        self.assertIsNone(stitch.pose_from_metadata({"GimbalYawDegree": "nan", "GimbalPitchDegree": 0, "GimbalRollDegree": 0}))
        rows = [{"GimbalYawDegree": -179, "GimbalPitchDegree": -15, "GimbalRollDegree": .5},
                {"GimbalYawDegree": 179, "GimbalPitchDegree": 0, "GimbalRollDegree": .5}]
        lines = stitch.initialize_poses([IMAGE, IMAGE], rows, True)
        self.assertEqual(float(stitch.field(lines[1], "y")), -2)
        self.assertEqual(float(stitch.field(lines[0], "p")), -15)
        self.assertEqual(stitch.field(lines[0], "Eev"), "12.614709")
        with self.assertRaises(ValueError):
            stitch.initialize_poses([IMAGE], [{}], True)

    def test_disconnected_images_require_priors_and_each_group_is_anchored(self):
        lines = [IMAGE]*5 + [CP, 'c n2 N3 x1 y1 X2 Y2 t0',
                           '#hugin_optimizerMasterSwitch 1', '#hugin_optimizerPhotoMasterSwitch 21']
        with self.assertRaises(ValueError):
            stitch.configure_optimizer(lines, False)
        configured, graph = stitch.configure_optimizer(lines, True)
        variables = [line for line in configured if line.startswith("v ")]
        self.assertEqual(variables, ["v y1 p1 r1", "v y3 p3 r3"])
        self.assertEqual(graph["isolated_images"], [4])
        self.assertEqual(graph["component_count"], 3)
        self.assertIn('#hugin_optimizerMasterSwitch 0', configured)
        self.assertIn('#hugin_optimizerPhotoMasterSwitch 0', configured)
        self.assertNotIn('#hugin_optimizerMasterSwitch 1', configured)

    def test_no_points_is_an_error_even_with_metadata(self):
        with self.assertRaises(ValueError):
            stitch.configure_optimizer([IMAGE, IMAGE], True)

    def test_linked_hfov_and_link_cycle(self):
        self.assertAlmostEqual(stitch.image_value([IMAGE, IMAGE.replace("v27.774389", "v=0")], 1, "v"), 27.774389)
        with self.assertRaises(ValueError):
            stitch.image_value([IMAGE.replace("v27.774389", "v=0")], 0, "v")

    def test_failed_subprocess_stops_and_preserves_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = stitch.Runner(Path(folder), Path(folder), 1)
            failed = subprocess.CompletedProcess(["cpfind"], 2, "", "no matches")
            with patch("stitch.subprocess.run", return_value=failed):
                with self.assertRaises(RuntimeError):
                    runner.run("match", "cpfind", [])
            self.assertEqual(runner.steps[0]["returncode"], 2)
            self.assertIn("no matches", (Path(folder)/"match.log").read_text())

    def test_pose_guard_detects_divergence_across_wrap_boundary(self):
        before = stitch.set_field(IMAGE, "y", 179)
        stitch.guard_geometry([before], [stitch.set_field(IMAGE, "y", -179)], True, 5)
        with self.assertRaises(ValueError):
            stitch.guard_geometry([IMAGE], [stitch.set_field(IMAGE, "p", 20)], True, 5)
        with self.assertRaises(ValueError):
            stitch.guard_geometry([IMAGE], [stitch.set_field(IMAGE, "r", 180)], True, 5)


if __name__ == "__main__":
    unittest.main()
