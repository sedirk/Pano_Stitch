"""Calibrated initialization rejects false matches and preserves rotation signs."""
from pathlib import Path
import sys
import unittest
import math
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import camera_alignment
import horizon
import stitch


class CameraAlignmentTests(unittest.TestCase):
    def test_known_rotations_with_outliers_and_loop_closure(self):
        poses=[(0,0,0),(8,-1,2),(16,.5,-1)]
        matrices=[np.array(horizon.orientation(*p)) for p in poses]
        width,height,hfov=1200,800,50
        focal=width/(2*math.tan(math.radians(hfov)/2))
        rng=np.random.default_rng(2)
        world=np.column_stack([np.ones(160),rng.uniform(-.3,.0,160),rng.uniform(-.2,.2,160)])
        points=[]
        for matrix in matrices:
            local=world@matrix
            points.append(np.column_stack([width/2-.5-focal*local[:,1]/local[:,0],
                                            height/2-.5-focal*local[:,2]/local[:,0]]))
        lines=['p f2 w3600 h1800 v360']+[f'i w{width} h{height} f0 v{hfov} y0 p0 r0 n"{i}.jpg"' for i in range(3)]
        for a,b in ((0,1),(1,2),(0,2)):
            for i,(p,q) in enumerate(zip(points[a],points[b])):
                if i<20:
                    q=q+rng.uniform(-200,200,2)
                lines.append(f'c n{a} N{b} x{p[0]} y{p[1]} X{q[0]} Y{q[1]} t0')
        initialized,report=camera_alignment.initialize(lines,threshold=2)
        images=stitch.image_lines(initialized)
        self.assertEqual(report['graph']['component_count'],1)
        self.assertLess(report['graph']['control_points'],480)
        for i,expected in enumerate(matrices):
            actual=np.array(horizon.orientation(*[stitch.image_value(images,i,k) for k in ('y','p','r')]))
            np.testing.assert_allclose(actual,expected,atol=1e-6)


if __name__=='__main__':
    unittest.main()
