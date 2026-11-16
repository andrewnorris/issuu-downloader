#!/usr/bin/env python3
"""Download authorized public Issuu SVG pages and produce a verified complete PDF.

Based on gbmerrall/issuu-downloader (MIT, Graeme Merrall, 2025).
"""
import argparse
import base64
from io import BytesIO
from pathlib import Path

from lxml import etree
from PIL import Image

from archival import parse_url as parse_issuu_url, page_sources as extract_svg_urls, run


def process_svg_images(svg_path, jpeg_quality=None, image_scale=1.0):
    """Apply explicitly requested lossy processing to a disposable SVG copy."""
    if jpeg_quality is None and image_scale == 1.0:
        return True
    try:
        root = etree.fromstring(Path(svg_path).read_bytes(), etree.XMLParser(resolve_entities=False, no_network=True))
        for element in root.iter('{http://www.w3.org/2000/svg}image'):
            for attr in ('href', '{http://www.w3.org/1999/xlink}href'):
                href = element.get(attr, '')
                if not href.startswith('data:image/jpeg') or ';base64,' not in href:
                    continue
                with Image.open(BytesIO(base64.b64decode(href.split(';base64,', 1)[1], validate=True))) as image:
                    if image_scale != 1.0:
                        size = (max(1, round(image.width*image_scale)), max(1, round(image.height*image_scale)))
                        image = image.resize(size, Image.Resampling.LANCZOS)
                    output = BytesIO()
                    image.save(output, format='JPEG', quality=jpeg_quality if jpeg_quality is not None else 95)
                element.set(attr, 'data:image/jpeg;base64,'+base64.b64encode(output.getvalue()).decode('ascii'))
        Path(svg_path).write_bytes(etree.tostring(root))
        return True
    except Exception as error:
        print(f'Image processing failed: {error}', flush=True)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='https://issuu.com/OWNER/docs/PUBLICATION')
    parser.add_argument('output', help='Destination PDF; published only after complete validation')
    parser.add_argument('--cache-dir', default='.issuu_cache')
    parser.add_argument('--batch-size', type=int, default=10,
                        help='Compatibility option; pages are processed individually to bound memory')
    parser.add_argument('--keep-cache', action='store_true')
    parser.add_argument('--clean', action='store_true')
    parser.add_argument('--compress', action='store_true', help='Lossless PDF content-stream compression')
    parser.add_argument('--jpeg-quality', type=int, help='Explicit lossy JPEG re-encoding (1-100)')
    parser.add_argument('--image-scale', type=float, default=1.0, help='Explicit image scaling; default 1.0')
    parser.add_argument('--delay', type=float, default=0, help='Minimum seconds between new page downloads')
    parser.add_argument('--manifest', type=Path, help='JSON page manifest destination')
    parser.add_argument('--verify', action='store_true', help='Compatibility flag: verification is always enabled')
    parser.add_argument('--strict', action='store_true', help='Compatibility flag: completeness is always required')
    args = parser.parse_args()
    import math
    if args.batch_size <= 0:
        parser.error('--batch-size must be positive')
    if args.jpeg_quality is not None and not 1 <= args.jpeg_quality <= 100:
        parser.error('--jpeg-quality must be between 1 and 100')
    if not math.isfinite(args.image_scale) or args.image_scale <= 0:
        parser.error('--image-scale must be finite and positive')
    if not math.isfinite(args.delay) or args.delay < 0:
        parser.error('--delay must be finite and nonnegative')
    try:
        run(args)
    except KeyboardInterrupt:
        print('Interrupted; validated per-page cache retained. Rerun the same command to resume.')
        return 1
    except Exception as error:
        print(f'Export failed: {error}', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
