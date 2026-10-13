"""Partial footprints must not wrap unrelated edges or gain padding pixels."""
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import export_flat
from test_export_jpg import image,pixels


class FlatExportTests(unittest.TestCase):
    def test_finite_join_preserves_edge_pixels_and_unequal_widths(self):
        rng=np.random.default_rng(2)
        source=rng.integers(0,65536,(31,503,4),dtype=np.uint16)
        source[:,:,3]=65535
        xb=[0,168,336,503]
        feather=16
        tiles=[image(source[:,max(0,a-feather):min(503,b+feather)]) for a,b in zip(xb,xb[1:])]
        result=pixels(export_flat.join_finite(tiles,feather))
        self.assertEqual(result.shape,source.shape)
        self.assertLessEqual(np.abs(result.astype(int)-source.astype(int)).max(),1)

    def test_arbitrary_crop_does_not_force_spherical_aspect_ratio(self):
        self.assertEqual(export_flat.crop_bounds('p f1 w200000 h14000 v360 S0,200000,3500,10000'),
                         (200000,14000,(0,200000,3500,10000)))
        with self.assertRaises(ValueError):
            export_flat.crop_bounds('p f0 w1000 h800 v80 S0,1001,0,800')


if __name__=='__main__':
    unittest.main()
