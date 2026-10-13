"""Photometric correction and native-source promotion for a cylindrical RAW panorama."""
import argparse
import json
import math
from pathlib import Path
import re
from stitch import Runner,TOKEN,field,find_bin,image_lines,image_value,read_pto,set_field,write_pto


def promote(lines, directory):
    records=[]
    result=[]
    for line in lines:
        if line.startswith('i '):
            token=next(t for t in TOKEN.finditer(line) if t[0].startswith('n"'))
            record=json.loads((directory/(Path(token[0][2:-1]).stem+'.json')).read_text())
            sx=record['width']/float(field(line,'w'))
            sy=record['height']/float(field(line,'h'))
            line=line[:token.start()]+f'n"{record["output"]}"'+line[token.end():]
            for key,value in (('w',record['width']),('h',record['height'])):
                line=set_field(line,key,value)
            # Pixel-valued offsets and crop/mask coordinates need explicit
            # scaling; prepared RAW inputs have neither source crops nor masks.
            if re.search(r'\sS\d',line):
                raise ValueError('Promotion of source crop rectangles is not supported')
            records.append((sx,sy))
        result.append(line)
    promoted=[]
    for line in result:
        if line.startswith('i '):
            index=sum(l.startswith('i ') for l in promoted)
            sx,sy=records[index]
            for key,scale in (('d',sx),('e',sy)):
                if not field(line,key).startswith('='):
                    line=set_field(line,key,float(field(line,key))*scale)
        elif line.startswith('c '):
            a,b=int(field(line,'n')),int(field(line,'N'))
            for key,scale in (('x',records[a][0]),('y',records[a][1]),('X',records[b][0]),('Y',records[b][1])):
                line=set_field(line,key,(float(field(line,key))+.5)*scale-.5)
        elif line.startswith('k '):
            raise ValueError('Promotion of source masks is not supported')
        promoted.append(line)
    return promoted


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--developed',type=Path,required=True)
    parser.add_argument('--preview-width',type=int,default=16000)
    parser.add_argument('--brightness-stops',type=float,default=1.0)
    parser.add_argument('--vignetting',action='store_true')
    args=parser.parse_args()
    work=args.output.resolve()
    work.mkdir(parents=True,exist_ok=False)
    runner=Runner(find_bin(None),work,6)
    lines=read_pto(args.project.resolve())
    images=image_lines(lines)
    anchor=image_value(images,0,'Eev')
    configured=[]
    for line in lines:
        if line=='v' or line.startswith('v '):
            continue
        if line.startswith('i '):
            line=set_field(line,'Eev',float(field(line,'Eev'))-anchor)
        elif line.startswith('p '):
            line=set_field(line,'E',-args.brightness_stops)
        configured.append(line)
    variables=[f'v Eev{i}' for i in range(1,len(images))]
    if args.vignetting:
        roots=[i for i,l in enumerate(images) if not field(l,'Vb').startswith('=')]
        variables += [f'v Vb{i} Vc{i} Vd{i}' for i in roots]
    configured.extend([*variables,'v'])
    write_pto(work/'photo_input.pto',configured)
    runner.run('photo_optimize','vig_optimize',['-s','1','-p','600','-o',work/'photo.pto',work/'photo_input.pto'])
    photo=read_pto(work/'photo.pto')
    im=image_lines(photo)
    validation=[]
    for i in range(len(im)):
        coeff=[image_value(im,i,k) for k in ('Va','Vb','Vc','Vd')]
        factors=[sum(c*r**(2*j) for j,c in enumerate(coeff)) for r in (0,.25,.5,.75,1)]
        if min(factors)<.4 or max(factors)>1.5:
            raise ValueError(f'Unreasonable fitted vignetting in image {i}: {factors}')
        validation.append({'index':i,'ev':image_value(im,i,'Eev'),'vignette_samples':factors})
    # Camera 0 starts near the left edge of the strip. Preserve fitted pitch/roll.
    runner.run('preview_canvas','pano_modify',['--projection=1','--fov=360',
               f'--canvas={args.preview_width}x{round(args.preview_width/15)}',
               '--crop=AUTO',f'--output-exposure={-args.brightness_stops}',
               '--output-cropped-tiff','--rotate=171,0,0','-o',work/'preview.pto',work/'photo.pto'])
    # Promote the already positioned preview PTO so its orientation/crop policy
    # matches the full-resolution result.
    write_pto(work/'full_sources.pto',promote(read_pto(work/'preview.pto'),args.developed.resolve()))
    runner.run('native_canvas','pano_modify',['--canvas=AUTO','--crop=AUTO',
               '--output-cropped-tiff','-o',work/'native.pto',work/'full_sources.pto'])
    # Hugin's discrete autocrop can pick a different rectangle at small sizes.
    # Derive the preview crop from the native one so it shows identical coverage.
    native_pano=next(l for l in read_pto(work/'native.pto') if l.startswith('p '))
    from export_flat import crop_bounds
    nw,nh,crop=crop_bounds(native_pano)
    scale=args.preview_width/nw
    preview=[]
    for line in read_pto(work/'preview.pto'):
        if line.startswith('p '):
            line=set_field(line,'h',round(nh*scale))
            line=re.sub(r'\sS[\d,]+','',line)
            line+=' S'+','.join(str(round(v*scale)) for v in crop)
        preview.append(line)
    write_pto(work/'preview.pto',preview)
    report={'steps':runner.steps,'photo_parameters':validation,'preview_project':str(work/'preview.pto'),
            'native_project':str(work/'native.pto')}
    (work/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    for path in ('preview.pto','native.pto'):
        print(path,next(l for l in read_pto(work/path) if l.startswith('p ')),flush=True)


if __name__=='__main__':
    main()
