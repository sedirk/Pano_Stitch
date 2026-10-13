"""Export an actual panorama footprint, including very wide cylindrical panoramas.

The PTO must already have its projection, canvas, crop, geometry and colors set.
Unlike the spherical exporter this never expands it to a 360 x 180 canvas.
Oversized JPEGs produce a continuous BigTIFF, native-pixel JPEG sections and an
explicitly downscaled single JPEG overview.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from functools import reduce
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from stitch import Runner, field, find_bin, read_pto, write_pto
from export_jpg import (BACKGROUND, JPEG_MAX, absolute_project, boundaries, dependencies,
                        file_signature, join_row, join_rows, mix, render_tile, save_vips, sources_and_depth)


def crop_bounds(pano):
    width, height = int(field(pano,'w')), int(field(pano,'h'))
    match = re.search(r'(?:^|\s)S(\d+),(\d+),(\d+),(\d+)', pano)
    bounds = tuple(map(int,match.groups())) if match else (0,width,0,height)
    left,right,top,bottom = bounds
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError('Invalid panorama crop')
    return width, height, bounds


def join_finite(tiles, feather):
    if len(tiles) == 1:
        return tiles[0]
    pieces = []
    for i, tile in enumerate(tiles):
        first = 0 if i == 0 else 2*feather
        last = tile.width if i == len(tiles)-1 else tile.width-2*feather
        pieces.append(tile.crop(first, 0, last-first, tile.height))
        if i+1 < len(tiles):
            pieces.append(mix(tile.crop(tile.width-2*feather,0,2*feather,tile.height),
                              tiles[i+1].crop(0,0,2*feather,tile.height)))
    return reduce(lambda a,b:a.join(b,'horizontal'), pieces)


def write_jpeg(rgb, path, quality):
    pyvips, _ = dependencies()
    temporary = path.with_name(path.stem+'_partial.jpg')
    rgb.jpegsave(str(temporary), Q=quality, subsample_mode='off',
                 optimize_coding=False, interlace=False, profile='srgb')
    check = pyvips.Image.new_from_file(str(temporary), access='sequential', fail_on='warning')
    if (check.width,check.height,check.bands) != (rgb.width,rgb.height,3):
        raise ValueError('Exported JPEG dimensions differ')
    check.avg()
    del check
    pyvips.cache_set_max(0)
    temporary.replace(path)
    return {'path':str(path), 'width':rgb.width, 'height':rgb.height, 'bytes':path.stat().st_size,
            'full_decode_verified':True}


def export(runner, source, quality=97, tile_size=4096, workers=1, keep=False):
    os.environ.setdefault('VIPS_CONCURRENCY', runner.env['OMP_NUM_THREADS'])
    pyvips, tifffile = dependencies()
    source = source.resolve()
    lines = read_pto(source)
    sources, groups = sources_and_depth(lines, source, tifffile)
    signature = file_signature(sources)
    lines = absolute_project(lines, sources)
    pano = next(l for l in lines if l.startswith('p '))
    width,height,bounds = crop_bounds(pano)
    left,right,top,bottom = bounds
    outw,outh = right-left,bottom-top
    periodic = int(field(pano,'f')) in (1,2) and abs(float(field(pano,'v'))-360)<1e-7 and left==0 and right==width
    if periodic and any(abs(float(field(l,k)))>1e-10 for l in lines if l.startswith('i ')
                        for k in ('TrX','TrY','TrZ') if re.search(r'\s'+k+r'[-+\d.]',l)):
        raise ValueError('Periodic tile yaw shifts require zero camera translation')
    project = runner.work/'render.pto'
    write_pto(project,lines)
    settings = {'version':1, 'project_sha256':hashlib.sha256(project.read_bytes()).hexdigest(),
                'sources':signature, 'tile_size':tile_size, 'quality':quality, 'periodic':periodic}
    state = runner.work/'export_state.json'
    if state.exists() and json.loads(state.read_text()) != settings:
        raise ValueError('Resume inputs differ; use a fresh output folder')
    state.write_text(json.dumps(settings,indent=2),encoding='utf-8')
    xb = [left+x for x in boundaries(outw,tile_size)]
    yb = [top+y for y in boundaries(outh,tile_size)]
    fx,fy = (256 if len(xb)>2 else 0),(256 if len(yb)>2 else 0)
    scratch,cache = runner.work/'scratch',runner.work/'strips'
    scratch.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    print(f'[flat] {outw} x {outh}, {len(xb)-1} x {len(yb)-1} tiles, periodic={periodic}',flush=True)
    row_paths = []
    for row,(y0,y1) in enumerate(zip(yb,yb[1:])):
        row_path = cache/f'row_{row:03d}.v'
        row_paths.append(row_path)
        expected = (outw,min(bottom,y1+fy)-max(top,y0-fy),4,'ushort')
        if row_path.exists():
            cached = pyvips.Image.new_from_file(str(row_path))
            if (cached.width,cached.height,cached.bands,cached.format) != expected:
                raise ValueError('Invalid cached strip')
            continue
        paths = [cache/f'tile_{row:03d}_{col:03d}.v' for col in range(len(xb)-1)]
        def render(col):
            path = paths[col]
            if not path.exists():
                temp = scratch/path.stem
                temp.mkdir(exist_ok=True)
                for p in [*temp.glob('layer????.tif'),temp/'blended.tif']:
                    p.unlink(missing_ok=True)
                render_tile(runner,lines,groups,width,height,xb[col],xb[col+1],y0,y1,fx,fy,256,
                            path,temp,periodic=periodic,bounds=bounds)
            print(f'[flat] Tile {row*(len(xb)-1)+col+1}/{(len(yb)-1)*(len(xb)-1)} complete',flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(render,range(len(paths))))
        tiles = [pyvips.Image.new_from_file(str(p)) for p in paths]
        strip = join_row(tiles,[b-a for a,b in zip(xb,xb[1:])],fx) if periodic else join_finite(tiles,fx)
        if (strip.width,strip.height,strip.bands,strip.format) != expected:
            raise ValueError('Joined strip dimensions differ')
        save_vips(strip,row_path)
        del tiles,strip
        pyvips.cache_set_max(0)
        for p in paths:
            p.unlink()
        print(f'[flat] Row {row+1}/{len(yb)-1} saved',flush=True)
    rows = [pyvips.Image.new_from_file(str(p)) for p in row_paths]
    rgba = join_rows(rows,[y-top for y in yb],fy)
    if (rgba.width,rgba.height) != (outw,outh):
        raise ValueError('Final canvas size mismatch')
    rgb16 = rgba.flatten(background=[v*257 for v in BACKGROUND],max_alpha=65535).copy(interpretation='rgb16')
    rgb = (rgb16/257).round('rint').cast('uchar').copy(interpretation='srgb')
    report = {'width':outw,'height':outh,'pixels':outw*outh, 'canvas':[width,height], 'crop':bounds,
              'projection':int(field(pano,'f')), 'hfov':float(field(pano,'v')), 'periodic':periodic,
              'quality':quality,'chroma_subsampling':'4:4:4','jpeg_sections':[]}
    if max(outw,outh) <= JPEG_MAX:
        report['jpg'] = write_jpeg(rgb,runner.work/'panorama.jpg',quality)
    else:
        print('[flat] Native dimensions exceed one JPEG; saving native sections and continuous BigTIFF',flush=True)
        xcuts,ycuts = boundaries(outw,60000),boundaries(outh,60000)
        for iy,(y0,y1) in enumerate(zip(ycuts,ycuts[1:])):
            for ix,(x0,x1) in enumerate(zip(xcuts,xcuts[1:])):
                part = write_jpeg(rgb.crop(x0,y0,x1-x0,y1-y0),runner.work/f'panorama_full_{iy+1:02d}_{ix+1:02d}.jpg',quality)
                part['offset'] = [x0,y0]
                report['jpeg_sections'].append(part)
        master = runner.work/'panorama_full_16bit.tif'
        rgb16.tiffsave(str(master),bigtiff=True,tile=True,tile_width=512,tile_height=512,
                       compression='deflate',predictor='horizontal',profile='srgb')
        check = pyvips.Image.new_from_file(str(master),access='sequential',fail_on='warning')
        check.avg()
        report['master'] = {'path':str(master),'width':check.width,'height':check.height,'bits':16,
                            'bytes':master.stat().st_size,'full_decode_verified':True}
        del check
        scale = min(65000/outw,65000/outh)
        report['jpg'] = write_jpeg(rgb.resize(scale),runner.work/'panorama_overview.jpg',quality)
        report['jpg']['scale_from_native'] = scale
    preview = runner.work/'preview.jpg'
    pyvips.Image.thumbnail(report['jpg']['path'],4096).jpegsave(str(preview),Q=94,subsample_mode='off')
    report['preview'] = str(preview)
    if signature != file_signature(sources):
        raise RuntimeError('A source image changed during export')
    report['sources_unchanged'] = True
    del rgba,rgb,rgb16,rows
    pyvips.cache_set_max(0)
    if not keep:
        for p in row_paths:
            p.unlink()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--workers',type=int,choices=(1,2),default=1)
    parser.add_argument('--threads',type=int,default=6)
    parser.add_argument('--tile-size',type=int,default=4096)
    parser.add_argument('--quality',type=int,default=97)
    parser.add_argument('--keep',action='store_true')
    args = parser.parse_args()
    if not 1024<=args.tile_size<=8192 or not 1<=args.quality<=100 or args.threads<1:
        parser.error('Invalid tile size, quality or thread count')
    work = args.output.resolve()
    work.mkdir(parents=True,exist_ok=args.resume)
    runner = Runner(find_bin(None),work,args.threads)
    report = {'status':'running','steps':runner.steps}
    start = time.perf_counter()
    try:
        report['output'] = export(runner,args.project,args.quality,args.tile_size,args.workers,args.keep)
        report['status'] = 'exported'
    except Exception as error:
        report.update(status='failed',error=str(error))
        raise
    finally:
        report['seconds'] = time.perf_counter()-start
        (work/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
