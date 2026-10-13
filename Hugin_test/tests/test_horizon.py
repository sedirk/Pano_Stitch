"""Leveling behavior: spherical rotation, capture-ring assumptions and opt-out."""
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import horizon
import stitch


def angles(matrix):
    return [math.degrees(math.atan2(-matrix[1][0], matrix[0][0])),
            math.degrees(math.asin(max(-1, min(1, matrix[2][0])))),
            math.degrees(math.atan2(matrix[2][1], matrix[2][2]))]


def fixture():
    lines = ['p f2 w4096 h2048 v360 E0 R0 n"TIFF_m"']
    rotation = horizon.orientation(0, 1.7, -1.1)
    for i in range(20):
        yaw, pitch, roll = angles(horizon.matmul(rotation, horizon.orientation(i*18, -.6, .3)))
        lines.append(f'i w4032 h3024 f0 v28 y{yaw} p{pitch} r{roll} Eev0.7 Er1.02 Eb0.98 '
                     f'TrX0 TrY0 TrZ0 n"image {i}.jpg"')
    lines.extend(f'c n{i} N{(i+1)%20} x100 y200 X120 Y220 t0' for i in range(20))
    return lines


def rotated_lines(lines, rotation):
    result = []
    images = stitch.image_lines(lines)
    index = 0
    for line in lines:
        if line.startswith('i '):
            matrix = horizon.orientation(*(stitch.image_value(images, index, k) for k in ('y', 'p', 'r')))
            for key, value in zip(('y', 'p', 'r'), angles(horizon.matmul(rotation, matrix))):
                line = stitch.set_field(line, key, value)
            index += 1
        result.append(line)
    return result


class HorizonTests(unittest.TestCase):
    def test_default_is_on_and_can_be_disabled(self):
        self.assertEqual(stitch.parser().parse_args([]).level_horizon, 'auto')
        self.assertEqual(stitch.parser().parse_args(['--level-horizon', 'off']).level_horizon, 'off')
        self.assertEqual(stitch.parser().parse_args(['--level-horizon']).level_horizon, 'auto')

    def test_tilted_ring_recovers_a_level_sphere_without_flattening_pitch(self):
        before = fixture()
        fit = horizon.find_ring(before)
        self.assertGreater(fit['estimated_tilt_deg'], 2)
        result = rotated_lines(before, horizon.orientation(*fit['rotation_ypr_deg']))
        after = horizon.find_ring(result)
        self.assertLess(after['estimated_tilt_deg'], 1e-7)
        pitches = [float(stitch.field(line, 'p')) for line in stitch.image_lines(result)]
        for pitch in pitches:
            self.assertAlmostEqual(pitch, -.6, places=8)
        horizon.verify_global_rotation(before, result)

    def test_sparse_coverage_and_non_finite_data_are_rejected(self):
        with self.assertRaisesRegex(ValueError, '240 degrees'):
            horizon.fit_ring(list(range(20)), [0]*20)
        with self.assertRaisesRegex(ValueError, 'eight'):
            horizon.fit_ring([0, 90, 180], [0, 0, 0])
        with self.assertRaisesRegex(ValueError, 'Non-finite'):
            horizon.fit_ring([i*18 for i in range(20)], [math.nan]+[0]*19)

    def test_robust_ring_fit_limits_one_bad_pose(self):
        lines = fixture()
        baseline = horizon.find_ring(lines)
        images = stitch.image_lines(lines)
        yaws = [float(stitch.field(line, 'y')) for line in images]
        pitches = [float(stitch.field(line, 'p')) for line in images]
        pitches[7] += 2
        fitted = horizon.fit_ring(yaws, pitches)
        self.assertLess(abs(fitted['estimated_tilt_deg']-baseline['estimated_tilt_deg']), .015)

    def test_per_image_warp_and_excessive_rotation_are_rejected(self):
        before = fixture()
        after = rotated_lines(before, horizon.orientation(0, .7, -.2))
        self.assertTrue(horizon.verify_global_rotation(before, after)['relative_orientations_preserved'])
        after[4] = stitch.set_field(after[4], 'p', float(stitch.field(after[4], 'p'))+.1)
        with self.assertRaisesRegex(ValueError, 'not one common rotation'):
            horizon.verify_global_rotation(before, after)
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            horizon.verify_global_rotation(before, rotated_lines(before, horizon.orientation(0, 7, 0)))

    def test_disabled_is_an_exact_noop(self):
        source = Path('unread.pto')
        result, info = horizon.level_project(None, source, Path('unwritten.pto'), 'off')
        self.assertEqual(result, source)
        self.assertEqual(info['status'], 'disabled')

    def test_native_changes_to_canvas_color_lens_and_points_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source, output = folder/'source.pto', folder/'leveled.pto'
            before = fixture()
            stitch.write_pto(source, before)
            native = rotated_lines(before, horizon.orientation(*horizon.find_ring(before)['rotation_ypr_deg']))
            native[0] = native[0].replace('w4096', 'w8192')
            native = [line.replace('v28', 'v30').replace('Eev0.7', 'Eev3') for line in native]
            native = [line for line in native if not line.startswith('c ')]
            runner = stitch.Runner(folder, folder, 1)
            def run(stage, tool, args):
                stitch.write_pto(Path(args[args.index('-o')+1]), native)
            with patch.object(runner, 'run', side_effect=run):
                _, info = horizon.level_project(runner, source, output)
            after = stitch.read_pto(output)
            self.assertEqual(after[0], before[0])
            self.assertEqual([l for l in after if l.startswith('c ')], [l for l in before if l.startswith('c ')])
            for image in stitch.image_lines(after):
                self.assertEqual(stitch.field(image, 'v'), '28')
                self.assertEqual(stitch.field(image, 'Eev'), '0.7')
                self.assertEqual(stitch.field(image, 'Er'), '1.02')
            self.assertEqual(info['status'], 'leveled')


if __name__ == '__main__':
    unittest.main()
