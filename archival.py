"""Strict, atomic archival pipeline. The upstream CLI remains the entry point."""
import hashlib
import base64
from io import BytesIO
import json
import math
import random
import shutil
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import cairosvg
import httpx
import pymupdf
from PIL import Image
from lxml import etree
from pypdf import PdfReader, PdfWriter


def parse_url(url):
    parts = urlsplit(url)
    path = parts.path.strip('/').split('/')
    if (parts.scheme not in ('http', 'https') or parts.hostname not in
            ('issuu.com', 'www.issuu.com') or parts.username or parts.password or
            parts.port or len(path) != 3 or path[1] != 'docs'):
        raise ValueError('Expected https://issuu.com/OWNER/docs/PUBLICATION')
    import re
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', p) for p in (path[0], path[2])):
        raise ValueError('Invalid Issuu owner or publication slug')
    return path[0], path[2]


def page_sources(metadata):
    doc = metadata.get('document', {})
    if doc.get('isPaywallPreview') or doc.get('downloadAllowed') is False:
        raise PermissionError('Publication access/download is restricted; obtain an authorized PDF.')
    pages = doc.get('pages')
    if not isinstance(pages, list) or not pages:
        raise ValueError('Cannot determine expected page count from metadata')
    result = []
    for number, page in enumerate(pages, 1):
        if page.get('isPagePaywalled'):
            raise PermissionError(f'Page {number} is paywalled; stopping without bypassing access controls')
        url = page.get('svgUrl')
        if not url or urlsplit(url).scheme != 'https':
            raise ValueError(f'Page {number} has no accessible HTTPS SVG; refusing an incomplete export')
        result.append((number, url))
    return result


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name+'.', delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.flush()
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    tmp.replace(path)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2, allow_nan=False)+'\n').encode())


def checksum(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def source_identity(url):
    """Issuu's v query is a viewer cache buster; revision is checked separately."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                      urlencode([(k,v) for k,v in parse_qsl(parts.query) if k != 'v']), ''))


def request(client, url, attempts=3, sleep=time.sleep):
    """Retry only transient failures. Never retry authentication/access denials."""
    for attempt in range(attempts):
        try:
            response = client.get(url, timeout=60)
            if response.status_code in (401, 403):
                raise PermissionError(f'HTTP {response.status_code}: access denied; stopping')
            if response.status_code == 429 or 500 <= response.status_code < 600:
                response.raise_for_status()
            response.raise_for_status()
            return response
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            if isinstance(error, httpx.HTTPStatusError):
                if error.response.status_code != 429 and error.response.status_code < 500:
                    raise
            if attempt == attempts - 1:
                raise
            wait = 2**attempt + random.uniform(0, 1)
            if isinstance(error, httpx.HTTPStatusError):
                retry = error.response.headers.get('Retry-After')
                if retry:
                    try:
                        retry_wait = float(retry)
                    except ValueError:
                        retry_wait = (parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds()
                    if not math.isfinite(retry_wait) or retry_wait > 60:
                        raise RuntimeError('Server requests a long Retry-After; stop and rerun later') from error
                    wait = max(wait, retry_wait)
            print(f'Transient request failure; retry {attempt+2}/{attempts} in {wait:.1f}s', flush=True)
            sleep(wait)


def validate_svg(data):
    root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    if root.tag != '{http://www.w3.org/2000/svg}svg':
        raise ValueError('Response is not SVG')
    # An archival conversion must not silently omit remote images/fonts/styles.
    if root.getroottree().docinfo.doctype:
        raise ValueError('SVG DTDs are unsupported')
    if root.getroottree().xpath('//processing-instruction()'):
        raise ValueError('SVG processing instructions are unsupported')
    import re
    for element in root.iter():
        for key, value in element.attrib.items():
            if etree.QName(key).localname == 'href' and not value.startswith(('#', 'data:')):
                raise ValueError('SVG contains an external resource; obtain a self-contained authorized source')
        css = ' '.join(element.attrib.values()) + ' ' + (element.text or '')
        if '@import' in css:
            raise ValueError('SVG contains external styles')
        for match in re.finditer(r'url\(\s*[\"\x27]?([^\)\"\x27]+)', css, re.IGNORECASE):
            if not match.group(1).strip().startswith(('#','data:')):
                raise ValueError('SVG contains an external CSS resource')
    return root


def valid_single_pdf(path):
    try:
        reader = PdfReader(path)
        return (not reader.is_encrypted and len(reader.pages) == 1 and
                float(reader.pages[0].mediabox.width) > 0 and
                float(reader.pages[0].mediabox.height) > 0)
    except Exception:
        return False


def preserve_jpeg_streams(pdf_data, svg_data):
    """Restore original JPEG bytes only where decoded pixels exactly match Cairo.

    Cairo commonly expands JPEGs to Flate-compressed RGB. Matching decoded RGB
    bytes avoids guessing which resource corresponds to which source image.
    Vector/text content and image placement remain untouched.
    """
    originals = {}
    for element in validate_svg(svg_data).iter('{http://www.w3.org/2000/svg}image'):
        href = element.get('href') or element.get('{http://www.w3.org/1999/xlink}href','')
        if href.startswith('data:image/jpeg') and ';base64,' in href:
            jpeg = base64.b64decode(href.split(';base64,',1)[1], validate=True)
            with Image.open(BytesIO(jpeg)) as image:
                # Leave profiled/CMYK images to Cairo to avoid color changes.
                if image.mode != 'RGB' or image.info.get('icc_profile'):
                    continue
                key = (image.width, image.height, hashlib.sha256(image.tobytes()).digest())
                originals[key] = jpeg
    if not originals:
        return pdf_data, []
    restored = []
    with pymupdf.open(stream=pdf_data, filetype='pdf') as doc:
        for item in doc[0].get_images(full=True):
            xref, mask = item[0], item[1]
            if mask:
                continue
            pix = pymupdf.Pixmap(doc, xref)
            if pix.n != 3:
                continue
            key = (pix.width, pix.height, hashlib.sha256(pix.samples).digest())
            jpeg = originals.get(key)
            if jpeg:
                doc[0].replace_image(xref, stream=jpeg)
                restored.append(hashlib.sha256(jpeg).hexdigest())
        return doc.tobytes(garbage=4, deflate=True), restored


def cached_page(number, url, cache, old, client, delay=0, jpeg_quality=None, image_scale=1.0):
    svg = cache / 'svgs' / f'page_{number:04d}.svg'
    pdf = cache / 'temp_pdfs' / f'page_{number:04d}.pdf'
    settings = {'jpeg_quality': jpeg_quality, 'image_scale': image_scale, 'converter_version': 3}
    valid = False
    if old and source_identity(old.get('source_url','')) == source_identity(url) and svg.exists():
        try:
            valid = checksum(svg) == old['sha256']
            if valid:
                validate_svg(svg.read_bytes())
        except Exception:
            valid = False
    if not valid:
        response = request(client, url)
        validate_svg(response.content)
        atomic_bytes(svg, response.content)
        if delay:
            time.sleep(delay)
    svg_hash = checksum(svg)
    reuse_pdf = (valid and old.get('settings') == settings and pdf.exists() and
                 old.get('pdf_sha256') == checksum(pdf) and valid_single_pdf(pdf))
    if not reuse_pdf:
        # Lossy transformations affect a working copy, never the source cache.
        source = svg
        work = cache / 'working' / svg.name
        if jpeg_quality is not None or image_scale != 1.0:
            from download_issuu import process_svg_images
            atomic_bytes(work, svg.read_bytes())
            if not process_svg_images(work, jpeg_quality, image_scale):
                raise ValueError(f'Image processing failed on page {number}')
            source = work
        source_data = source.read_bytes()
        from svg_fonts import prepare_svg
        prepared = prepare_svg(source_data, cache/'fonts')
        data = cairosvg.svg2pdf(bytestring=prepared)
        data, restored = preserve_jpeg_streams(data, source_data)
        atomic_bytes(pdf, data)
        if not valid_single_pdf(pdf):
            raise ValueError(f'Page {number} conversion is invalid')
    reader = PdfReader(pdf)
    box = reader.pages[0].mediabox
    return {'page': number, 'source_url': url, 'source_format': 'svg',
            'size_bytes': svg.stat().st_size, 'sha256': svg_hash,
            'pdf_sha256': checksum(pdf), 'settings': settings,
            'preserved_jpeg_sha256': old.get('preserved_jpeg_sha256',[]) if reuse_pdf else restored,
            'width_pt': float(box.width), 'height_pt': float(box.height),
            'svg_path': str(svg.resolve()), 'pdf_path': str(pdf.resolve())}


def merge_complete(paths, output, compress=False):
    if not paths:
        raise ValueError('No pages to merge')
    writer = PdfWriter()
    try:
        for path in paths:
            if not valid_single_pdf(path):
                raise ValueError(f'Missing or invalid page: {path}')
            writer.add_page(PdfReader(path).pages[0])
        if compress:
            for page in writer.pages:
                page.compress_content_streams()
        writer.write(output)
    finally:
        writer.close()


def run(args):
    owner, slug = parse_url(args.url)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache_dir).resolve() / f'{owner}_{slug}'
    if args.clean and cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.manifest).resolve() if args.manifest else output.with_name(output.stem.replace('_high_quality', '')+'_manifest.json')
    report_path = output.with_name(output.stem.replace('_high_quality', '')+'_validation.json')
    old = {}
    try:
        old = json.loads((cache/'manifest.json').read_text())
    except (OSError, ValueError):
        pass
    manifest = {'url': f'https://issuu.com/{owner}/docs/{slug}', 'status': 'incomplete', 'pages': []}
    candidate = output.with_name('.'+output.name+'.part')
    try:
        with httpx.Client(headers={'User-Agent': 'issuu-downloader/archival'}, follow_redirects=True) as client:
            metadata = request(client, f'https://publication.issuu.com/{owner}/{slug}/reader4.json').json()
            sources = page_sources(metadata)
            manifest['expected_pages'] = len(sources)
            doc = metadata['document']
            manifest['publication_id'] = doc.get('publicationId')
            manifest['revision_id'] = doc.get('revisionId')
            # Manifest identity prevents cross-publication or cross-revision resume.
            same = all(old.get(k) == manifest.get(k) for k in ('url','publication_id','revision_id','expected_pages'))
            previous = {p['page']: p for p in old.get('pages', [])} if same else {}
            resume_entries = previous.copy()
            print(f'Publication {owner}/{slug}: expected {len(sources)} pages', flush=True)
            for number, url in sources:
                print(f'Page {number}/{len(sources)}: validating/downloading/converting', flush=True)
                entry = cached_page(number, url, cache, previous.get(number), client,
                                    args.delay, args.jpeg_quality, args.image_scale)
                source = doc['pages'][number-1]
                if source.get('width') and source.get('height'):
                    ratio = float(source['width']) / float(source['height'])
                    entry['expected_aspect_ratio'] = ratio
                    if abs(entry['width_pt']/entry['height_pt']/ratio-1) > 0.01:
                        raise ValueError(f'Page {number} aspect ratio differs from metadata')
                manifest['pages'].append(entry)
                resume_entries[number] = entry
                atomic_json(cache/'manifest.json', {**manifest, 'pages':[
                    resume_entries[n] for n in sorted(resume_entries)]})
                atomic_json(manifest_path, manifest)
        merge_complete([Path(p['pdf_path']) for p in manifest['pages']], candidate, args.compress)
        from scripts.verify_pdf import verify
        report = verify(candidate, manifest, render_dir=output.parent/'validation_samples')
        report['output_path'] = str(output)
        atomic_json(report_path, report)
        if not report['passed']:
            raise ValueError('PDF validation failed; inspect the validation report')
        candidate.replace(output)
        manifest['status'] = 'complete'
        manifest['output_sha256'] = checksum(output)
        atomic_json(manifest_path, manifest)
        atomic_json(cache/'manifest.json', manifest)
        print(f'Complete: {report["actual_pages"]}/{manifest["expected_pages"]} pages; '
              f'{report["output_bytes"]} bytes; {report["extractable_text_pages"]} pages with extractable text; '
              f'validation passed. Output: {output}', flush=True)
        if not args.keep_cache:
            shutil.rmtree(cache)
    except BaseException as error:
        candidate.unlink(missing_ok=True)
        manifest['error'] = str(error)
        atomic_json(manifest_path, manifest)
        # Preserve detailed verifier failures rather than replacing them.
        if not report_path.exists() or 'report' not in locals():
            atomic_json(report_path, {'passed': False, 'status': 'export_failed',
                        'expected_pages': manifest.get('expected_pages'), 'actual_pages': None,
                        'error': str(error)})
        raise
