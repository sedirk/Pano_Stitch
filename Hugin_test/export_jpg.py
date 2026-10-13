"""Render original-resolution Hugin panoramas to JPEG with bounded working tiles."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import reduce
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time

from stitch import HERE, TOKEN, Runner, field, find_bin, image_lines, image_value, read_pto, set_field, write_pto

if sys.version_info[:2] == (3, 12):
    sys.path.insert(0, str(HERE.parent / '.tools/python-color'))

BACKGROUND = [30, 34, 39]
JPEG_MAX = 65500  # libjpeg's implementation limit, smaller than the format limit.


def dependencies():
    import pyvips
    import tifffile
    pyvips.cache_set_max_mem(128 * 1024**2)
    pyvips.cache_set_max(64)
    return pyvips, tifffile


def add_export_options(parser):
    parser.add_argument('--width', type=int, help='Output width; omitted = Hugin optimal source resolution. Use 4096 for preview.')
    parser.add_argument('--jpeg-quality', type=int, default=97, help='JPEG quality, 1..100 (default 97, 4:4:4)')
    parser.add_argument('--tile-size', type=int, default=4096, help='Working tile core size; does not change output resolution')
    parser.add_argument('--keep-intermediates', action='store_true', help='Keep 16-bit working strips after successful export')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=1, help='Concurrent render tiles (default 1); 2 uses more RAM')


def validate_options(width, quality, tile_size):
    if width is not None and (width < 512 or width % 2 or width > JPEG_MAX):
        raise ValueError(f'--width must be even, between 512 and {JPEG_MAX}')
    if not 1 <= quality <= 100:
        raise ValueError('--jpeg-quality must be between 1 and 100')
    if not 1024 <= tile_size <= 8192:
        raise ValueError('--tile-size must be between 1024 and 8192')


def boundaries(length, maximum):
    count = math.ceil(length / maximum)
    return [round(i * length / count) for i in range(count + 1)]


def sources_and_depth(lines, project, tifffile):
    sources, groups = [], {8: [], 16: []}
    for index, line in enumerate(image_lines(lines)):
        token = next(t[0] for t in TOKEN.finditer(line) if t[0].startswith('n"'))
        source = (project.parent / token[2:-1]).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() in ('.jpg', '.jpeg'):
            depth = 8
        elif source.suffix.lower() in ('.tif', '.tiff'):
            with tifffile.TiffFile(source) as image:
                depth = {'uint8': 8, 'uint16': 16}.get(image.pages[0].dtype.name)
            if depth is None:
                raise ValueError(f'Expected 8/16-bit source: {source}')
        else:
            raise ValueError(f'Expected JPEG or TIFF source: {source}')
        sources.append(source)
        groups[depth].append(index)
    return sources, groups


def absolute_project(lines, sources):
    result, index = [], 0
    for line in lines:
        if line.startswith('i '):
            token = next(t for t in TOKEN.finditer(line) if t[0].startswith('n"'))
            line = line[:token.start()] + f'n"{sources[index]}"' + line[token.end():]
            index += 1
        result.append(line)
    return result


def tile_project(lines, active, crop, shift_pixels, width):
    """Shift every yaw by an integer canvas offset; retain all linked images.

    Inactive depth groups stay in the PTO so lens links and masks keep their
    original indices. Nona itself selects the active images intersecting the ROI.
    Explicit -i flags fail when an image falls outside that ROI.
    """
    images = image_lines(lines)
    info = [line for line in lines if line.startswith('#-hugin ')]
    if info and len(info) != len(images):
        raise ValueError('Unexpected Hugin image metadata count')
    result = []
    for line in lines:
        if line.startswith('p '):
            line = re.sub(r'\s+S[-\d,]+', '', line)
            result.append(line + ' S' + ','.join(map(str, crop)))
        elif line.startswith('m '):
            result.append(line)
    for index, line in enumerate(images):
        meta = info[index] if info else '#-hugin '
        if index not in active and 'disabled' not in meta:
            meta += ' disabled'
        result.append(meta)
        yaw = (image_value(images, index, 'y') + 360 * shift_pixels / width + 180) % 360 - 180
        result.append(set_field(line, 'y', yaw))
    result.extend(line for line in lines if line.startswith('k '))
    return result


def mix(a, b, vertical=False):
    """Alpha-aware linear crossfade on a shared overlap, including the 360 seam."""
    import pyvips
    if (a.width, a.height, a.bands) != (b.width, b.height, b.bands):
        raise ValueError('Overlap dimensions differ')
    axis = pyvips.Image.xyz(a.width, a.height)[1 if vertical else 0]
    weight = (axis + .5) / (a.height if vertical else a.width)
    aa, ba = a[3] / 65535, b[3] / 65535
    aw, bw = (1 - weight) * aa, weight * ba
    denom = aw + bw
    rgb = (a[:3] * aw + b[:3] * bw) / (denom > 0).ifthenelse(denom, 1)
    alpha = (aa > ba).ifthenelse(aa, ba) * 65535
    return rgb.bandjoin(alpha).round('rint').cast('ushort').copy(interpretation='rgb16')


def join_row(tiles, cores, feather):
    import pyvips
    if len(tiles) == 1:
        return tiles[0]
    seams = [mix(tiles[i-1].crop(cores[i-1], 0, 2*feather, tiles[i-1].height),
                 tiles[i].crop(0, 0, 2*feather, tiles[i].height)) for i in range(len(tiles))]
    pieces = [seams[0].crop(feather, 0, feather, seams[0].height)]
    for i, tile in enumerate(tiles):
        pieces.append(tile.crop(2*feather, 0, cores[i]-2*feather, tile.height))
        if i+1 < len(tiles):
            pieces.append(seams[i+1])
    pieces.append(seams[0].crop(0, 0, feather, seams[0].height))
    # arrayjoin pads every cell to the largest width; strips need exact joins.
    return reduce(lambda a, b: a.join(b, 'horizontal'), pieces)


def join_rows(rows, bounds, feather):
    import pyvips
    if len(rows) == 1:
        return rows[0]
    pieces = []
    for i, row in enumerate(rows):
        first = 0 if i == 0 else 2*feather
        last = row.height if i == len(rows)-1 else row.height-2*feather
        pieces.append(row.crop(0, first, row.width, last-first))
        if i+1 < len(rows):
            pieces.append(mix(row.crop(0, row.height-2*feather, row.width, 2*feather),
                              rows[i+1].crop(0, 0, row.width, 2*feather), vertical=True))
    return reduce(lambda a, b: a.join(b, 'vertical'), pieces)


def save_vips(image, path):
    temporary = path.with_name(path.stem + '_partial.v')
    image.write_to_file(str(temporary))
    temporary.replace(path)


def tiff_position(path, tifffile):
    with tifffile.TiffFile(path) as image:
        tags = image.pages[0].tags
        def ratio(name, default):
            value = tags[name].value if name in tags else default
            return value[0] / value[1] if isinstance(value, tuple) else value
        return round(ratio('XPosition', 0) * ratio('XResolution', 1)), round(ratio('YPosition', 0) * ratio('YResolution', 1))


def render_tile(runner, lines, groups, width, height, x0, x1, y0, y1, fx, fy, guard, path, scratch,
                periodic=True, bounds=None):
    pyvips, tifffile = dependencies()
    # All partial-width tiles are moved to the middle of the spherical canvas.
    # This also renders the left/right boundary as one ordinary overlap.
    shift = width//2 - (x0+x1)//2 if periodic and fx else 0
    lbound, rbound, tbound, bbound = bounds or (0, width, 0, height)
    left, right = x0-fx, x1+fx
    if not periodic:
        left, right = max(lbound, left), min(rbound, right)
    top, bottom = max(tbound, y0-fy), min(bbound, y1+fy)
    rl = max(0, left+shift-guard)
    rr = min(width, right+shift+guard)
    rt, rb = max(0, top-guard), min(height, bottom+guard)
    prefix = scratch/'layer'
    for depth, active in groups.items():
        if not active:
            continue
        project = scratch/f'depth{depth}.pto'
        write_pto(project, tile_project(lines, set(active), (rl, rr, rt, rb), shift, width))
        # Temporary TIFFs are uncompressed: bounded tiles fit on disk, and this
        # avoids repeatedly spending CPU on lossless encode/decode at gigapixel scale.
        runner.run(f'{path.stem}_remap{depth}', 'nona', ['-m', 'TIFF_m', '-p', 'UINT16', '-z', 'NONE', '-o', prefix, project])
    layers = sorted(scratch.glob('layer????.tif'))
    if not layers:
        output = pyvips.Image.black(right-left, bottom-top, bands=4).cast('ushort').copy(interpretation='rgb16')
    else:
        if len(layers) == 1:
            blended = layers[0]
        else:
            blended = scratch/'blended.tif'
            args = ['--compression=NONE', '--depth=16', f'-f{rr-rl}x{rb-rt}+{rl}+{rt}']
            if periodic and not fx:
                args.append('--wrap=HORIZONTAL')
            stage = f'{path.stem}_blend'
            try:
                runner.run(stage, 'enblend', [*args, '-o', blended, '--', *layers])
            except RuntimeError:
                log = (runner.work/f'{stage}.log').read_text(encoding='utf-8')
                if not any(message in log for message in ('failed to detect any seam',
                                                         'kernel longer than line')):
                    raise
                # A tiny unique fringe can disappear in a coarse seam mask.
                # Start with the largest footprints and use full-resolution
                # masks, retaining native multiresolution color blending.
                print(f'[export] {path.stem}: retrying native seam selection', flush=True)
                def area(layer):
                    with tifffile.TiffFile(layer) as tif:
                        return tif.pages[0].imagewidth*tif.pages[0].imagelength
                ordered = sorted(layers, key=lambda layer: (-area(layer), layer.name))
                runner.run(stage+'_fallback', 'enblend', [*args,
                           '--fine-mask', '--no-optimize', '--no-pre-assemble',
                           '-o', blended, '--', *ordered])
        image = pyvips.Image.new_from_file(str(blended))
        ox, oy = tiff_position(blended, tifffile)
        if image.format != 'ushort' or image.bands != 4:
            raise ValueError('Expected 16-bit RGBA remapped output')
        canvas = pyvips.Image.black(rr-rl, rb-rt, bands=4).cast('ushort').copy(interpretation='rgb16')
        image = canvas.insert(image, ox-rl, oy-rt, expand=False)
        output = image.crop(left+shift-rl, top-rt, right-left, bottom-top)
    save_vips(output, path)
    # Free cached libvips readers before removing TIFFs on Windows.
    del output
    if layers:
        del image, canvas
    pyvips.cache_set_max(0)
    for item in [*layers, scratch/'blended.tif']:
        item.unlink(missing_ok=True)


def file_signature(paths):
    return [{'path': str(p), 'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns} for p in paths]


def export(runner, source, width=None, quality=97, tile_size=4096, keep_intermediates=False, workers=1):
    validate_options(width, quality, tile_size)
    os.environ.setdefault('VIPS_CONCURRENCY', runner.env.get('OMP_NUM_THREADS', '6'))
    pyvips, tifffile = dependencies()
    source = source.resolve()
    source_lines = read_pto(source)
    source_images = image_lines(source_lines)
    if any(abs(image_value(source_images, i, key)) > 1e-10
           for i, line in enumerate(source_images) for key in ('TrX', 'TrY', 'TrZ')
           if any(t[0].startswith(key) for t in TOKEN.finditer(line))):
        raise ValueError('Tiled spherical export requires zero camera translations')
    sources, groups = sources_and_depth(source_lines, source, tifffile)
    initial_signature = file_signature(sources)
    write_pto(runner.work/'export_source.pto', absolute_project(source_lines, sources))
    project = runner.work/'export_full.pto'
    runner.run('export_canvas', 'pano_modify', ['--projection=2', '--fov=360x180',
               '--canvas=AUTO' if width is None else f'--canvas={width}x{width//2}',
               '--crop=0,100,0,100%', '--output-cropped-tiff', '-o', project, runner.work/'export_source.pto'])
    lines = read_pto(project)
    pano = next(line for line in lines if line.startswith('p '))
    width, height = int(field(pano, 'w')), int(field(pano, 'h'))
    if max(width, height) > JPEG_MAX:
        raise ValueError(f'Native canvas {width}x{height} exceeds JPEG/libjpeg limit {JPEG_MAX}; choose --width explicitly')
    if width != 2*height:
        raise ValueError('Expected a complete 2:1 equirectangular canvas')
    xb, yb = boundaries(width, tile_size), boundaries(height, tile_size)
    fx, fy = (256 if len(xb)>2 else 0), (256 if len(yb)>2 else 0)
    guard = 256 if fx or fy else 0
    scratch, cache = runner.work/'export_scratch', runner.work/'export_strips'
    scratch.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    settings = {'version': 1, 'project_sha256': hashlib.sha256(project.read_bytes()).hexdigest(),
                'sources': initial_signature, 'width': width, 'height': height, 'tile_size': tile_size,
                'feather': [fx, fy], 'guard': guard, 'quality': quality}
    manifest = runner.work/'export_state.json'
    if manifest.exists() and json.loads(manifest.read_text(encoding='utf-8')) != settings:
        raise ValueError('Resume inputs/settings differ; use a new output directory')
    manifest.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    print(f'[export] {width} x {height}; {(len(xb)-1)*(len(yb)-1)} tiles; JPEG Q{quality} 4:4:4', flush=True)
    row_paths = []
    for row, (y0, y1) in enumerate(zip(yb, yb[1:])):
        row_path = cache/f'row_{row:03d}.v'
        row_paths.append(row_path)
        if row_path.is_file():
            cached = pyvips.Image.new_from_file(str(row_path))
            if (cached.width, cached.height, cached.bands, cached.format) != (width, min(height,y1+fy)-max(0,y0-fy), 4, 'ushort'):
                raise ValueError(f'Incomplete or invalid cached strip: {row_path}')
            del cached
            print(f'[export] Reusing complete row {row+1}/{len(yb)-1}', flush=True)
            continue
        tile_paths = [cache/f'tile_{row:03d}_{col:03d}.v' for col in range(len(xb)-1)]
        def render_column(col):
            x0, x1 = xb[col], xb[col+1]
            path = cache/f'tile_{row:03d}_{col:03d}.v'
            if not path.is_file():
                tile_scratch = scratch/path.stem
                tile_scratch.mkdir(exist_ok=True)
                # Only remove this exporter's incomplete scratch layers on retry.
                for item in [*tile_scratch.glob('layer????.tif'), tile_scratch/'blended.tif']:
                    item.unlink(missing_ok=True)
                render_tile(runner, lines, groups, width, height, x0, x1, y0, y1, fx, fy, guard, path, tile_scratch)
            print(f'[export] Tile {row*(len(xb)-1)+col+1}/{(len(xb)-1)*(len(yb)-1)} complete', flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(render_column, col) for col in range(len(tile_paths))]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        tiles = [pyvips.Image.new_from_file(str(p)) for p in tile_paths]
        strip = join_row(tiles, [b-a for a, b in zip(xb, xb[1:])], fx)
        if (strip.width, strip.height) != (width, min(height,y1+fy)-max(0,y0-fy)):
            raise ValueError('Joined strip dimensions differ from canvas coverage')
        save_vips(strip, row_path)
        del strip, tiles
        pyvips.cache_set_max(0)
        for path in tile_paths:
            path.unlink()
        print(f'[export] Row {row+1}/{len(yb)-1} saved', flush=True)
    rows = [pyvips.Image.new_from_file(str(p)) for p in row_paths]
    panorama = join_rows(rows, yb, fy)
    if (panorama.width, panorama.height) != (width, height):
        raise ValueError('Working strips do not cover the requested canvas')
    rgb = (panorama.flatten(background=[v*257 for v in BACKGROUND], max_alpha=65535) / 257).round('rint').cast('uchar').copy(interpretation='srgb')
    temporary, final = runner.work/'panorama_partial.jpg', runner.work/'panorama.jpg'
    print('[export] Streaming full-resolution JPEG', flush=True)
    rgb.jpegsave(str(temporary), Q=quality, subsample_mode='off', optimize_coding=False, interlace=False, profile='srgb')
    check = pyvips.Image.new_from_file(str(temporary), access='sequential', fail_on='warning')
    if (check.width, check.height, check.bands) != (width, height, 3):
        raise ValueError('JPEG dimensions/channels differ from the requested canvas')
    # Full decode verifies that the stream is complete, without loading it all.
    check.avg()
    del check
    pyvips.cache_set_max(0)
    temporary.replace(final)
    preview = runner.work/'preview.jpg'
    pyvips.Image.thumbnail(str(final), min(4096, width)).jpegsave(str(preview), Q=94, subsample_mode='off')
    runner.run('export_metadata', 'exiftool', ['-overwrite_original', '-EXIF:ColorSpace=sRGB', '-XMP-GPano:ProjectionType=equirectangular',
               '-XMP-GPano:UsePanoramaViewer=True', f'-XMP-GPano:FullPanoWidthPixels={width}',
               f'-XMP-GPano:FullPanoHeightPixels={height}', f'-XMP-GPano:CroppedAreaImageWidthPixels={width}',
               f'-XMP-GPano:CroppedAreaImageHeightPixels={height}', '-XMP-GPano:CroppedAreaLeftPixels=0',
               '-XMP-GPano:CroppedAreaTopPixels=0', final])
    if file_signature(sources) != initial_signature:
        raise RuntimeError('A source file changed during export')
    result = {'jpg': str(final), 'preview': str(preview), 'project': str(project),
              'width': width, 'height': height, 'pixels': width*height, 'quality': quality,
              'chroma_subsampling': '4:4:4', 'bits_per_channel': 8, 'bytes': final.stat().st_size,
              'background_rgb': BACKGROUND, 'full_decode_verified': True,
              'source_sizes_and_mtimes_unchanged': True, 'native_render': True,
              'seam_fallback_logs': [str(p) for p in sorted(runner.work.glob('*_blend_fallback.log'))],
              'method': '16-bit Nona/Enblend tiles with 256px guard and alpha-aware 512px crossfades',
              'working_strips_retained': keep_intermediates}
    del rgb, panorama, rows
    pyvips.cache_set_max(0)
    if not keep_intermediates:
        for path in row_paths:
            path.unlink()
    return result


def main(argv=None):
    from horizon import add_level_option, level_project
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path, nargs='?', default=HERE/'runs/sky_balanced_leveled_v1/leveled.pto')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume', action='store_true', help='Continue the same --output after an interrupted export')
    parser.add_argument('--hugin-bin')
    parser.add_argument('--threads', type=int, default=6)
    add_level_option(parser)
    add_export_options(parser)
    args = parser.parse_args(argv)
    validate_options(args.width, args.jpeg_quality, args.tile_size)
    if args.threads < 1 or (args.resume and args.output is None):
        parser.error('Use positive --threads; --resume requires --output')
    os.environ['VIPS_CONCURRENCY'] = str(args.threads)
    work = (args.output or HERE/'runs'/('jpg_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))).resolve()
    work.mkdir(parents=True, exist_ok=args.resume)
    runner = Runner(find_bin(args.hugin_bin), work, args.threads)
    report = {'source_project': str(args.project.resolve()), 'status': 'started', 'steps': runner.steps}
    start = time.perf_counter()
    try:
        project, report['horizon'] = level_project(runner, args.project.resolve(), work/'leveled.pto', args.level_horizon)
        report['output'] = export(runner, project, args.width, args.jpeg_quality, args.tile_size, args.keep_intermediates, args.workers)
        report['status'] = 'exported'
    except Exception as error:
        report.update(status='failed', error=str(error))
        raise
    finally:
        report['seconds'] = round(time.perf_counter()-start, 3)
        (work/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'[report] {work/"report.json"}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
