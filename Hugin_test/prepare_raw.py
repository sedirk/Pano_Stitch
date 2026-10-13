"""Prepare consistent full-resolution RAW sources and smaller alignment images."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

from stitch import HERE, find_bin

sys.path.insert(0, str(HERE.parent / '.tools/python-color'))


def inspect(folder, work):
    from PIL import Image, ImageDraw, ImageOps
    files = sorted(p.resolve() for p in folder.iterdir() if p.suffix.lower() == '.arw')
    if len(files) < 2:
        raise ValueError('At least two ARW files are required')
    tags = ['FileName', 'Model', 'LensModel', 'ImageWidth', 'ImageHeight', 'Orientation',
            'FocalLength', 'FocalLengthIn35mmFormat', 'ExposureTime', 'FNumber', 'ISO',
            'WhiteBalance', 'WB_RGGBLevels', 'DateTimeOriginal', 'JpgFromRawStart', 'JpgFromRawLength']
    command = [str(find_bin(None)/'exiftool.exe'), '-json', '-n', '-charset', 'filename=UTF8', '-@', '-']
    env = dict(os.environ, LC_ALL='C', LC_CTYPE='C', LANG='C')
    # UTF-8 argument files bypass the Windows ANSI command-line conversion.
    arguments = '\n'.join([*['-'+tag for tag in tags], *map(str, files)])+'\n'
    metadata = json.loads(subprocess.run(command, input=arguments, encoding='utf-8',
                                        capture_output=True, check=True, env=env).stdout)
    work.mkdir(parents=True, exist_ok=True)
    thumbs = work/'embedded'
    thumbs.mkdir(exist_ok=True)
    sheet = Image.new('RGB', (6*320, ((len(files)+5)//6)*240), '#181a20')
    draw = ImageDraw.Draw(sheet)
    for i, (source, row) in enumerate(zip(files, metadata)):
        row['SourceFile'] = str(source)
        row['source_size'] = source.stat().st_size
        row['source_mtime_ns'] = source.stat().st_mtime_ns
        with source.open('rb') as stream:
            stream.seek(row['JpgFromRawStart'])
            jpeg = stream.read(row['JpgFromRawLength'])
        with Image.open(io.BytesIO(jpeg)) as photo:
            row['embedded_dimensions'] = list(photo.size)
            photo = ImageOps.exif_transpose(photo)
            photo.thumbnail((1600, 1600))
            photo.save(thumbs/(source.stem+'.jpg'), quality=93, subsampling=0)
            photo.thumbnail((310, 208))
            x, y = (i%6)*320, (i//6)*240
            sheet.paste(photo, (x+(320-photo.width)//2, y))
            draw.text((x+6, y+211), f'{i:02d} {source.stem}  {row["FocalLength"]}mm  1/{round(1/row["ExposureTime"])}', fill='white')
    sheet.save(work/'contact_sheet.jpg', quality=93)
    (work/'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'images':len(files), 'focal_lengths':sorted(set(r['FocalLength'] for r in metadata)),
                      'exposure_range':[min(r['ExposureTime'] for r in metadata),max(r['ExposureTime'] for r in metadata)],
                      'orientations':sorted(set(r['Orientation'] for r in metadata)),
                      'contact_sheet':str(work/'contact_sheet.jpg')}, ensure_ascii=False), flush=True)
    return metadata


def develop(work, indices=None):
    os.environ.setdefault('OMP_NUM_THREADS', '6')
    import rawpy
    import numpy as np
    import tifffile
    from PIL import Image, ImageCms
    metadata = json.loads((work/'metadata.json').read_text(encoding='utf-8'))
    # LibRaw uses R,G,B,G2 order; Sony's tag is R,G,G2,B.
    wbs = [list(map(float, row['WB_RGGBLevels'].split())) for row in metadata]
    median = np.median(wbs, axis=0)
    wb = [float(median[i]/median[1]) for i in (0,1,3,2)]
    policy = {'version':1, 'rawpy':rawpy.__version__, 'wb_rgbg':wb, 'auto_bright':False,
              'bright':1.0, 'gamma':[2.4,12.92], 'color':'sRGB', 'bits':16, 'demosaic':'AHD'}
    full, match = work/'developed', work/'alignment'
    full.mkdir(exist_ok=True)
    match.mkdir(exist_ok=True)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    state = work/'raw_policy.json'
    if state.exists() and json.loads(state.read_text()) != policy:
        raise ValueError('RAW development settings changed; use a new folder')
    state.write_text(json.dumps(policy, indent=2), encoding='utf-8')
    for i, row in enumerate(metadata):
        if indices is not None and i not in indices:
            continue
        source = Path(row['SourceFile'])
        target, small = full/(source.stem+'.tif'), match/(source.stem+'.jpg')
        record_path = full/(source.stem+'.json')
        if target.exists() and small.exists() and record_path.exists():
            record = json.loads(record_path.read_text())
            if [source.stat().st_size, source.stat().st_mtime_ns] != [row['source_size'],row['source_mtime_ns']]:
                raise ValueError('RAW changed since inspection')
            print(f'[raw] Reusing {i+1}/{len(metadata)} {source.name}', flush=True)
            continue
        print(f'[raw] Developing {i+1}/{len(metadata)} {source.name}', flush=True)
        with source.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        with source.open('rb') as stream, rawpy.imread(stream) as raw:
            rgb = raw.postprocess(user_wb=wb, use_auto_wb=False, use_camera_wb=False,
                                  output_bps=16, output_color=rawpy.ColorSpace.sRGB,
                                  no_auto_bright=True, bright=1.0, gamma=(2.4,12.92),
                                  demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD)
        temp = target.with_suffix('.partial.tif')
        tifffile.imwrite(temp, rgb, photometric='rgb', metadata=None,
                         extratags=[(34675, 'B', len(profile), profile, False)])
        temp.replace(target)
        # No per-image tonal adjustment: color matching is performed on overlaps.
        preview = Image.fromarray((rgb[::3,::3].astype(np.uint32)//257).astype(np.uint8))
        exif = Image.Exif()
        for tag, value in ((271,'SONY'),(272,row['Model']),(274,1)):
            exif[tag] = value
        exif[34665] = {37386:row['FocalLength'], 41989:int(row['FocalLengthIn35mmFormat']),
                       33434:row['ExposureTime'], 33437:row['FNumber'],34855:row['ISO'],
                       42036:row['LensModel']}
        preview.save(small, quality=97, subsampling=0, icc_profile=profile, exif=exif)
        record = {'source':str(source), 'sha256':digest, 'width':rgb.shape[1], 'height':rgb.shape[0],
                  'alignment_width':preview.width, 'alignment_height':preview.height,
                  'source_size':row['source_size'], 'source_mtime_ns':row['source_mtime_ns'],
                  'output':str(target), 'alignment':str(small), 'policy':policy}
        record_path.write_text(json.dumps(record, indent=2), encoding='utf-8')
        del rgb, preview


def sync_metadata(work):
    """Copy selected optical tags into derived JPEGs without re-encoding pixels."""
    rows = json.loads((work/'metadata.json').read_text(encoding='utf-8'))
    commands = []
    for row in rows:
        target = work/'alignment'/(Path(row['SourceFile']).stem+'.jpg')
        commands.extend(['-tagsFromFile', row['SourceFile'], '-Make', '-Model', '-LensModel',
                         '-FocalLength', '-FocalLengthIn35mmFormat', '-ExposureTime', '-FNumber',
                         '-ISO', '-Orientation=1', '-n', '-overwrite_original', str(target), '-execute'])
    command = [str(find_bin(None)/'exiftool.exe'), '-charset', 'filename=UTF8', '-@', '-',
               '-common_args', '-charset', 'filename=UTF8']
    result = subprocess.run(command, input='\n'.join(commands)+'\n', encoding='utf-8',
                            capture_output=True, check=True, env=dict(os.environ, LC_ALL='C',LC_CTYPE='C',LANG='C'))
    (work/'metadata_copy.log').write_text(result.stdout+'\n'+result.stderr, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--inspect-only', action='store_true')
    parser.add_argument('--indices', help='Comma-separated zero-based source indices')
    parser.add_argument('--sync-metadata', action='store_true')
    args = parser.parse_args()
    work = args.output.resolve()
    if not (work/'metadata.json').exists():
        inspect(args.folder.resolve(), work)
    if args.sync_metadata:
        sync_metadata(work)
    elif not args.inspect_only:
        develop(work, set(map(int,args.indices.split(','))) if args.indices else None)


if __name__ == '__main__':
    main()
