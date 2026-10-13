"""Initialize ordinary-camera rotations from calibrated, robust bearing matches."""
from collections import defaultdict
import math
import sys
from stitch import HERE, control_points, field, graph_report, image_lines, image_value, set_field

sys.path.insert(0,str(HERE.parent/'.tools/python-color'))


def initialize(lines, threshold=8.0):
    import numpy as np
    from scipy.spatial.transform import Rotation
    images=image_lines(lines)
    pairs=defaultdict(list)
    for cp in control_points(lines):
        pairs[cp['n'],cp['N']].append(cp)
    rng=np.random.default_rng(729)
    kept=set()
    edges=[]
    records=[]

    def rays(index, points):
        w,h=(image_value(images,index,k) for k in ('w','h'))
        f=w/(2*math.tan(math.radians(image_value(images,index,'v'))/2))
        # Hugin orientation basis: forward, left, up.
        v=np.column_stack([np.ones(len(points)), -(points[:,0]+.5-w/2)/f, -(points[:,1]+.5-h/2)/f])
        return v/np.linalg.norm(v,axis=1)[:,None],f

    def fit(a,b):
        u,_,vt=np.linalg.svd(b.T@a)
        d=np.diag([1,1,np.linalg.det(vt.T@u.T)])
        return vt.T@d@u.T

    for (a,b),points in sorted(pairs.items()):
        if len(points)<8:
            continue
        ar,fa=rays(a,np.array([[p['x'],p['y']] for p in points]))
        br,fb=rays(b,np.array([[p['X'],p['Y']] for p in points]))
        focal=(fa+fb)/2
        best=np.zeros(len(points),bool)
        best_error=float('inf')
        for _ in range(400):
            subset=rng.choice(len(points),3,replace=False)
            rotation=fit(ar[subset],br[subset])
            errors=np.linalg.norm(br@rotation.T-ar,axis=1)*focal
            mask=errors<threshold
            score=float(np.median(errors[mask])) if mask.any() else float('inf')
            if mask.sum()>best.sum() or (mask.sum()==best.sum() and score<best_error):
                best,best_error=mask,score
        for _ in range(3):
            if best.sum()<8:
                break
            rotation=fit(ar[best],br[best])
            errors=np.linalg.norm(br@rotation.T-ar,axis=1)*focal
            best=errors<threshold
        angle=math.degrees(Rotation.from_matrix(rotation).magnitude())
        accepted=int(best.sum())>=8 and angle<image_value(images,a,'v')+image_value(images,b,'v')
        records.append({'pair':[a,b],'points':len(points),'inliers':int(best.sum()),
                        'median_px':float(np.median(errors[best])) if best.any() else None,
                        'rotation_deg':angle,'accepted':bool(accepted)})
        if accepted:
            kept.update(p['line'] for p,valid in zip(points,best) if valid)
            edges.append((int(best.sum()),a,b,rotation))
    filtered=[l for l in lines if not l.startswith('c ') or l in kept or field(l,'t')!='0']
    graph=graph_report(filtered)
    if graph['component_count']!=1:
        rejected=[r for r in records if not r['accepted']]
        raise ValueError(f'Calibrated rotation matches are disconnected: {graph["components"]}; rejected: {rejected}')
    # Maximum spanning tree favors widely supported pairs. The subsequent
    # bundle adjustment uses every retained edge, including loop closure.
    poses={0:np.eye(3)}
    while len(poses)<len(images):
        candidate=max((e for e in edges if (e[1] in poses)!=(e[2] in poses)),key=lambda e:e[0])
        _,a,b,rotation=candidate
        if a in poses:
            poses[b]=poses[a]@rotation
        else:
            poses[a]=poses[b]@rotation.T
    result=[]
    index=0
    for line in filtered:
        if line.startswith('i '):
            z,y,x=Rotation.from_matrix(poses[index]).as_euler('ZYX',degrees=True)
            for key,value in (('y',-z),('p',-y),('r',x)):
                line=set_field(line,key,float(value))
            index+=1
        result.append(line)
    return result,{'method':'calibrated_bearing_ransac_maximum_spanning_tree',
                   'threshold_px':threshold,'pairs':records,'graph':graph}
