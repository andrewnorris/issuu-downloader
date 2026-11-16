"""Verify completeness, dimensions, page order, content and extractable text."""
import argparse
import hashlib
import json
import math
import random
from pathlib import Path

import pymupdf


def verify(path, manifest, render_dir=None):
    path = Path(path)
    report = {'passed': False, 'expected_pages': manifest['expected_pages'],
              'actual_pages': 0, 'output_bytes': path.stat().st_size if path.exists() else 0,
              'extractable_text_pages': 0, 'pages': [], 'errors': [], 'rendered_samples': []}
    try:
        entries = manifest['pages']
        expected = manifest['expected_pages']
        if expected <= 0 or [p['page'] for p in entries] != list(range(1, expected+1)):
            raise ValueError('Manifest is incomplete, duplicated or out of numeric order')
        with pymupdf.open(path) as doc:
            if doc.is_encrypted:
                raise ValueError('Unexpected encrypted PDF')
            report['actual_pages'] = len(doc)
            if len(doc) != expected:
                report['errors'].append('Page count mismatch')
            samples = {0, len(doc)//2, len(doc)-1}
            samples.update(random.Random(25).sample(range(len(doc)), min(4, len(doc))))
            for i, page in enumerate(doc):
                warnings = []
                width, height = page.mediabox.width, page.mediabox.height
                if not all(math.isfinite(v) and v > 0 for v in (width,height)):
                    report['errors'].append(f'Page {i+1}: invalid media box')
                    continue
                entry = entries[i] if i < len(entries) else None
                if entry:
                    if abs(width-entry['width_pt']) > .05 or abs(height-entry['height_pt']) > .05:
                        report['errors'].append(f'Page {i+1}: dimensions changed')
                    ratio = entry.get('expected_aspect_ratio', entry['width_pt']/entry['height_pt'])
                    if abs(width/height/ratio-1) > .01:
                        report['errors'].append(f'Page {i+1}: aspect ratio mismatch')
                text = page.get_text().strip()
                report['extractable_text_pages'] += bool(text)
                # Low-resolution raster fingerprints include images and resources,
                # so order verification detects swapped pages with identical sizes.
                scale = min(1, 180/max(width,height))
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale,scale), alpha=False)
                fingerprint = hashlib.sha256(pix.samples).hexdigest()
                if max(pix.samples)-min(pix.samples) < 3:
                    warnings.append('Nearly uniform page; visually review against source (may be intentionally blank)')
                if entry:
                    source_path = Path(entry['pdf_path'])
                    if not source_path.exists():
                        report['errors'].append(f'Page {i+1}: source PDF missing; cannot verify order')
                    else:
                        if hashlib.sha256(source_path.read_bytes()).hexdigest() != entry['pdf_sha256']:
                            report['errors'].append(f'Page {i+1}: source PDF checksum mismatch')
                        with pymupdf.open(source_path) as source:
                            source_pix = source[0].get_pixmap(matrix=pymupdf.Matrix(scale,scale), alpha=False)
                            if hashlib.sha256(source_pix.samples).hexdigest() != fingerprint:
                                report['errors'].append(f'Page {i+1}: content/order fingerprint mismatch')
                report['pages'].append({'page': i+1, 'width_pt': width, 'height_pt': height,
                                        'text_characters': len(text), 'warnings': warnings,
                                        'render_sha256': fingerprint})
                if render_dir and (i in samples or warnings):
                    directory = Path(render_dir)
                    directory.mkdir(parents=True, exist_ok=True)
                    image = directory/f'page_{i+1:04d}.png'
                    page.get_pixmap(matrix=pymupdf.Matrix(.6,.6), alpha=False).save(image)
                    report['rendered_samples'].append(str(image.resolve()))
            report['passed'] = not report['errors']
    except Exception as error:
        report['errors'].append(str(error))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--render-dir', type=Path)
    args = parser.parse_args()
    result = verify(args.pdf, json.loads(args.manifest.read_text()), args.render_dir)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2)+'\n')
    print(f'Passed: {result["passed"]}; pages {result["actual_pages"]}/{result["expected_pages"]}; '
          f'text pages {result["extractable_text_pages"]}')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
