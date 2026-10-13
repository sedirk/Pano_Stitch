"""Supplement difficult overlaps with SIFT on contrast-enhanced working images."""
import argparse
import json
from pathlib import Path
import sys
from stitch import HERE,TOKEN,control_points,image_lines,read_pto,write_pto
sys.path.insert(0,str(HERE.parent/'.tools/python-color'))
import cv2
import numpy as np


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--pairs',required=True,help='Zero-based pairs, e.g. 25-26,26-27')
    args=parser.parse_args()
    cv2.setNumThreads(6)
    cv2.setRNGSeed(730)
    lines=read_pto(args.project)
    images=image_lines(lines)
    pairs=[tuple(map(int,p.split('-'))) for p in args.pairs.split(',')]
    features={}
    sift=cv2.SIFT_create(nfeatures=18000,contrastThreshold=.006,edgeThreshold=12)
    for index in sorted({i for pair in pairs for i in pair}):
        name=next(t[0][2:-1] for t in TOKEN.finditer(images[index]) if t[0].startswith('n"'))
        path=(args.project.parent/name).resolve()
        gray=cv2.imdecode(np.fromfile(path,dtype=np.uint8),cv2.IMREAD_GRAYSCALE)
        enhanced=cv2.createCLAHE(clipLimit=3.0,tileGridSize=(8,8)).apply(gray)
        keypoints,desc=sift.detectAndCompute(enhanced,None)
        features[index]=(keypoints,desc)
        print(f'[sift] {index}: {len(keypoints)} features',flush=True)
    result=lines.copy()
    report=[]
    matcher=cv2.BFMatcher(cv2.NORM_L2)
    for a,b in pairs:
        ka,da=features[a]
        kb,db=features[b]
        forward={m.queryIdx:m.trainIdx for m,n in matcher.knnMatch(da,db,k=2) if m.distance<.78*n.distance}
        reverse={m.queryIdx:m.trainIdx for m,n in matcher.knnMatch(db,da,k=2) if m.distance<.78*n.distance}
        good=[(i,j) for i,j in forward.items() if reverse.get(j)==i]
        pa=np.float64([ka[i].pt for i,j in good])
        pb=np.float64([kb[j].pt for i,j in good])
        if len(good)>=8:
            model,mask=cv2.findHomography(pa,pb,cv2.USAC_MAGSAC,2.5,maxIters=10000,confidence=.999)
            mask=mask.ravel().astype(bool) if model is not None else np.zeros(len(good),bool)
        else:
            mask=np.zeros(len(good),bool)
        kept=0
        bins={}
        for p,q in zip(pa[mask],pb[mask]):
            bucket=tuple((p/150).astype(int))
            if bins.get(bucket,0)>=3:
                continue
            bins[bucket]=bins.get(bucket,0)+1
            result.append(f'c n{a} N{b} x{p[0]:.8f} y{p[1]:.8f} X{q[0]:.8f} Y{q[1]:.8f} t0')
            kept+=1
        report.append({'pair':[a,b],'mutual_matches':len(good),'homography_inliers':int(mask.sum()),'added':kept})
        print(f'[sift] {a}-{b}: {kept} spatially distributed inliers',flush=True)
    write_pto(args.output,result)
    args.output.with_suffix('.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
