"""Behavioral checks for the overlap color solver and original-size application."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import balance_sky as sky

np = sky.np


class SkyBalanceTests(unittest.TestCase):
    def test_non_neutral_input_cannot_be_corrected_twice(self):
        pano = 'p f2 w4096 h2048 v360 E0 R0'
        image = 'i ' + ' '.join(f'{key}{value}' for key, value in sky.NEUTRAL.items())
        sky.check_neutral([pano, image])
        with self.assertRaisesRegex(ValueError, 'non-neutral Eev'):
            sky.check_neutral([pano, image.replace('Eev0', 'Eev1')])
        with self.assertRaisesRegex(ValueError, 'panorama exposure'):
            sky.check_neutral([pano.replace('E0', 'E2'), image])
        with self.assertRaisesRegex(ValueError, '360-degree'):
            sky.check_neutral([pano.replace('v360', 'v180'), image])

    def test_mesh_application_matches_sample_interpolation_at_edges_and_interior(self):
        model = np.random.default_rng(21).uniform(-.2, .2, (4, 4, 3))
        raster = sky.mesh_rows(model, 23, 17, 0, 17)
        for x, y in ((0, 0), (22, 16), (8, 3), (22, 0), (0, 16)):
            expected = sum(model.reshape(-1, 3)[i]*w for i, w in sky.weights(x, y, (23, 17)))
            np.testing.assert_allclose(raster[y, x], expected, atol=1e-7)
        np.testing.assert_allclose(sky.mesh_rows(model, 23, 17, 5, 12), raster[5:12], atol=1e-7)

    def test_spatial_bias_is_recovered_with_fixed_anchor_and_outliers(self):
        rng = np.random.default_rng(41)
        sizes = [(201, 151)]*3
        selected = [1, 2]
        models = np.zeros((2, 4, 4, 3))
        for i in range(2):
            for y in range(4):
                for x in range(4):
                    models[i, y, x] = [.025*(i+1)+.012*x, -.02+.009*y, .012*x-.008*y]
        rows = np.zeros((900, 16))
        for n in range(len(rows)):
            a, b = ((0, 1), (1, 2), (0, 2))[n % 3]
            rows[n, :2] = [a, b]
            rows[n, 2:6] = rng.random(4)*[200, 150, 200, 150]
            color = rng.uniform(.3, .6, 3)
            for side, index in enumerate((a, b)):
                bias = np.zeros(3) if index == 0 else sum(
                    models[index-1].reshape(-1, 3)[j]*w for j, w in
                    sky.weights(*rows[n, 2+side*2:4+side*2], sizes[index]))
                rows[n, 6+side*3:9+side*3] = color - bias
        train, validation = rows[:600].copy(), rows[600:]
        train[::29, 9:12] += .12  # Small moving-cloud/outlier population.
        fitted, _ = sky.fit_mesh(train, selected, sizes)
        metrics = sky.evaluate(validation, selected, sizes, fitted)
        self.assertLess(metrics['after']['mean_rgb_levels'], .5)
        self.assertLess(metrics['after']['p95_rgb_levels'], 1)
        self.assertGreater(metrics['before']['mean_rgb_levels'], 4)
        with self.assertRaisesRegex(ValueError, 'no overlap path'):
            sky.fit_mesh(rows[rows[:, 0] == 1], selected, sizes)

    def test_sample_validation_filters_downsample_border_but_rejects_wrong_geometry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'samples.txt'
            rows = np.zeros((2, 16))
            rows[:, :2] = [0, 1]
            rows[:, 2:6] = [20, 30, 40, 50]
            rows[:, 6:12] = .5
            rows[1, 2] = -2
            np.savetxt(path, rows)
            valid, discarded = sky.load_samples(path, [(100, 100)]*2)
            self.assertEqual((len(valid), discarded), (1, 1))
            rows[1, 2] = -100
            np.savetxt(path, rows)
            with self.assertRaisesRegex(ValueError, 'dimensions'):
                sky.load_samples(path, [(100, 100)]*2)
            rows[1, 0] = 3
            np.savetxt(path, rows)
            with self.assertRaisesRegex(ValueError, 'IDs'):
                sky.load_samples(path, [(100, 100)]*2)

    def test_source_correction_keeps_dimensions_and_writes_true_16bit(self):
        with tempfile.TemporaryDirectory() as folder:
            source, dest = Path(folder)/'original.jpg', Path(folder)/'corrected.tif'
            sky.Image.new('RGB', (37, 29), (100, 120, 140)).save(source, quality=98)
            digest = sky.sha256(source)
            model = np.zeros((4, 4, 3)) + [.0101, -.0202, .0303]
            info = sky.correct_source(source, dest, model, (37, 29))
            image = sky.tifffile.imread(dest)
            self.assertEqual(image.dtype, np.dtype('uint16'))
            self.assertEqual(image.shape, (29, 37, 3))
            self.assertEqual(info['new_clipped'], 0)
            self.assertEqual(sky.sha256(source), digest)
            self.assertTrue(np.any(image % 257 != 0))  # Fractional correction survives.
            with self.assertRaisesRegex(ValueError, 'clips'):
                sky.correct_source(source, dest, np.ones((4, 4, 3)), (37, 29))

    def test_replacing_filename_leaves_every_other_pto_byte_unchanged(self):
        line = 'i w4032 h3024 p15 r0 v28 Eev0 n"D:/my photos/p0 v30.JPG"'
        cp = 'c n0 N1 x23 y42 X32 Y24 t0'
        result = sky.replace_sources([line, cp], {0: Path('D:/new sky/image.tif')})
        self.assertEqual(result[0].split(' n"')[0], line.split(' n"')[0])
        self.assertEqual(result[1], cp)


if __name__ == '__main__':
    unittest.main()
