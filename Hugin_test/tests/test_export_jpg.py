"""Full-resolution export: periodic tiling, alpha and geometry invariants."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import export_jpg
import stitch

pyvips, _ = export_jpg.dependencies()
import numpy as np


def image(array):
    return pyvips.Image.new_from_memory(array.tobytes(), array.shape[1], array.shape[0], 4, 'ushort').copy(interpretation='rgb16')


def pixels(value):
    return np.frombuffer(value.write_to_memory(), dtype=np.uint16).reshape(value.height, value.width, 4)


class ExportTests(unittest.TestCase):
    def test_final_defaults_to_native_jpeg_but_accepts_preview_width(self):
        args = stitch.parser().parse_args([])
        self.assertIsNone(args.width)
        self.assertEqual(args.jpeg_quality, 97)
        self.assertEqual(args.level_horizon, 'auto')
        self.assertEqual(stitch.parser().parse_args(['--width', '4096']).width, 4096)

    def test_irregular_tile_grid_covers_canvas_exactly(self):
        for length in (50446, 25223, 1024, 4097):
            points = export_jpg.boundaries(length, 4096)
            self.assertEqual(points[0], 0)
            self.assertEqual(points[-1], length)
            widths = [b-a for a, b in zip(points, points[1:])]
            self.assertLessEqual(max(widths), 4096)
            self.assertLessEqual(max(widths)-min(widths), 1)

    def test_periodic_two_axis_join_preserves_pixels_and_size(self):
        rng = np.random.default_rng(8)
        source = rng.integers(0, 65536, (251, 514, 4), dtype=np.uint16)
        source[:, :, 3] = 65535
        xb, yb, feather = [0, 171, 343, 514], [0, 125, 251], 16
        rows = []
        for y0, y1 in zip(yb, yb[1:]):
            tile_images = []
            for x0, x1 in zip(xb, xb[1:]):
                cols = np.arange(x0-feather, x1+feather) % source.shape[1]
                tile_images.append(image(source[max(0,y0-feather):min(251,y1+feather), cols]))
            rows.append(export_jpg.join_row(tile_images, [b-a for a,b in zip(xb,xb[1:])], feather))
        result = pixels(export_jpg.join_rows(rows, yb, feather))
        self.assertEqual(result.shape, source.shape)
        self.assertLessEqual(np.abs(result.astype(int)-source.astype(int)).max(), 1)

    def test_overlap_keeps_covered_pixels_without_black_alpha_fringe(self):
        left = np.zeros((10, 24, 4), dtype=np.uint16)
        right = left.copy()
        left[:] = [60000, 30000, 10000, 65535]
        result = pixels(export_jpg.mix(image(left), image(right)))
        np.testing.assert_array_equal(result, left)

    def test_integer_canvas_shift_resolves_yaw_links_and_preserves_color(self):
        lines = ['p f2 w2048 h1024 v360 S1,2000,20,1000 n"TIFF_m"', 'm i0',
                 '#-hugin cropFactor=2', 'i w10 h8 f0 v28 y170 p4 r2 Eev0.7 Er1.02 n"a.jpg"',
                 '#-hugin cropFactor=2 disabled', 'i w10 h8 f0 v=0 y=0 p7 r3 Eev0.4 Er1.03 n"b.jpg"']
        result = export_jpg.tile_project(lines, {1}, (300, 1300, 0, 700), 256, 2048)
        images = stitch.image_lines(result)
        self.assertEqual(float(stitch.field(images[0], 'y')), -145)
        self.assertEqual(float(stitch.field(images[1], 'y')), -145)
        self.assertIn('Eev0.7 Er1.02', images[0])
        self.assertIn('v=0', images[1])
        self.assertEqual(sum('disabled' in l for l in result), 2)
        self.assertIn('S300,1300,0,700', result[0])
        self.assertNotIn('S1,2000', result[0])

    def test_invalid_jpeg_sizes_and_quality_fail_explicitly(self):
        export_jpg.validate_options(None, 97, 4096)
        for params in ((65536, 97, 4096), (4095, 97, 4096), (4096, 101, 4096), (4096, 97, 512)):
            with self.assertRaises(ValueError):
                export_jpg.validate_options(*params)


if __name__ == '__main__':
    unittest.main()
