import json
import base64
import hashlib
from io import BytesIO
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import httpx
import cairosvg
import pymupdf
from PIL import Image
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from pypdf import PdfReader

import archival
from svg_fonts import prepare_svg, point_on_path
from scripts.verify_pdf import verify

SVG = (Path(__file__).parent/'fixtures/page.svg').read_bytes()


class ArchivalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_url_normalization_and_rejection(self):
        for suffix in ('', '?ff&hideShareButton=true', '#pages', '?foo=1#pages', '/'):
            self.assertEqual(archival.parse_url('https://issuu.com/sjc_preview/docs/x25'+suffix), ('sjc_preview','x25'))
        for url in ('https://issuu.com.evil.test/a/docs/b', 'https://evil.test/a/docs/b',
                    'https://issuu.com/a/docs/../x', 'https://issuu.com/a/docs/b/extra'):
            with self.assertRaises(ValueError):
                archival.parse_url(url)

    def test_metadata_never_omits_missing_or_paywalled_pages(self):
        for doc in ({'pages': []}, {'pages': [{'svgUrl':'https://test/1'}, {}]},
                    {'pages':[{'svgUrl':'https://test/1', 'isPagePaywalled':True}]},
                    {'pages':[{'svgUrl':'https://test/1'}], 'isPaywallPreview':True}):
            with self.assertRaises((ValueError,PermissionError)):
                archival.page_sources({'document':doc})
        sources = archival.page_sources({'document': {'pages':[{'svgUrl':f'https://test/{n}'} for n in range(12)]}})
        self.assertEqual([p[0] for p in sources], list(range(1,13)))

    def test_retry_after_transient_and_access_denial(self):
        statuses = iter([429,503,200])
        client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(next(statuses), headers={'Retry-After':'2'}, content=SVG)))
        waits = []
        self.assertEqual(archival.request(client,'https://test/page',sleep=waits.append).status_code,200)
        self.assertEqual(len(waits),2)
        self.assertGreaterEqual(waits[0],2)
        calls = []
        def deny(req):
            calls.append(req)
            return httpx.Response(403)
        with httpx.Client(transport=httpx.MockTransport(deny)) as client:
            with self.assertRaises(PermissionError):
                archival.request(client,'https://test/page',sleep=waits.append)
        self.assertEqual(len(calls),1)

    def test_resume_and_corrupt_cache_recovery(self):
        calls = []
        def serve(req):
            calls.append(req)
            return httpx.Response(200,content=SVG)
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            first = archival.cached_page(1,'https://test/1',self.directory,None,client)
            second = archival.cached_page(1,'https://test/1',self.directory,first,client)
            self.assertEqual(first,second)
            self.assertEqual(len(calls),1)
            Path(first['svg_path']).write_bytes(b'corrupt')
            recovered = archival.cached_page(1,'https://test/1',self.directory,first,client)
            self.assertEqual(len(calls),2)
            self.assertEqual(first['sha256'],recovered['sha256'])
            Path(first['pdf_path']).write_bytes(b'bad PDF')
            archival.cached_page(1,'https://test/1',self.directory,recovered,client)
            self.assertTrue(archival.valid_single_pdf(Path(first['pdf_path'])))
            self.assertEqual(len(calls),2)

    def test_merge_dimensions_text_and_order_fingerprints(self):
        entries = []
        with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=SVG.replace(b'Synthetic archival page',req.url.path.encode())))) as client:
            for n in range(1,13):
                entries.append(archival.cached_page(n,f'https://test/Page{n}',self.directory,None,client))
        manifest = {'expected_pages':12,'pages':entries}
        output = self.directory/'combined.pdf'
        archival.merge_complete([Path(p['pdf_path']) for p in entries],output)
        result = verify(output,manifest)
        self.assertTrue(result['passed'],result)
        self.assertEqual(result['extractable_text_pages'],12)
        reader = PdfReader(output)
        self.assertEqual(float(reader.pages[0].mediabox.width),300)
        self.assertEqual(float(reader.pages[0].mediabox.height),150)
        self.assertIn('Page10',reader.pages[9].extract_text())
        paths = [Path(p['pdf_path']) for p in entries]
        paths[1],paths[9] = paths[9],paths[1]
        archival.merge_complete(paths,output)
        self.assertFalse(verify(output,manifest)['passed'])
        with self.assertRaises(ValueError):
            archival.merge_complete([self.directory/'missing.pdf'],output)

    def test_external_resources_and_html_rejected(self):
        for data in (b'<html>Access denied</html>', SVG.replace(b'<rect', b'<image href="https://test/image"/><rect',1)):
            with self.assertRaises(ValueError):
                archival.validate_svg(data)

    def test_original_jpeg_stream_is_preserved_without_reencoding(self):
        image = Image.new('RGB',(20,10),'blue')
        stream = BytesIO()
        image.save(stream,format='JPEG',quality=90)
        jpeg = stream.getvalue()
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
               '<image width="20" height="10" href="data:image/jpeg;base64,'+
               base64.b64encode(jpeg).decode()+'"/></svg>').encode()
        output, restored = archival.preserve_jpeg_streams(cairosvg.svg2pdf(bytestring=svg),svg)
        self.assertEqual(restored,[hashlib.sha256(jpeg).hexdigest()])
        with pymupdf.open(stream=output,filetype='pdf') as doc:
            extracted = [doc.extract_image(i[0])['image'] for i in doc[0].get_images()]
            self.assertIn(jpeg,extracted)

    def test_viewer_cache_buster_does_not_force_redownload(self):
        calls = []
        def serve(req):
            calls.append(req)
            return httpx.Response(200,content=SVG)
        with httpx.Client(transport=httpx.MockTransport(serve)) as client:
            first=archival.cached_page(1,'https://test/1?v=1',self.directory,None,client)
            archival.cached_page(1,'https://test/1?v=2',self.directory,first,client)
        self.assertEqual(len(calls),1)

    def test_text_path_end_is_not_clipped(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"><path id="line" d="M20,80 L40,80M40,80 L60,80" stroke="none"/><text font-family="sans-serif" font-size="30"><textPath href="#line" startOffset="20" textLength="20">B</textPath></text></svg>'
        prepared = prepare_svg(svg,self.directory/'fonts')
        self.assertNotIn(b'<textPath',prepared)
        self.assertNotIn(b'<textPath',prepare_svg(svg.replace(b'textLength="20"',b'textLength="auto"'),self.directory/'fonts'))
        output = cairosvg.svg2pdf(bytestring=prepared)
        with pymupdf.open(stream=output,filetype='pdf') as doc:
            self.assertIn('B',doc[0].get_text())
        x,y,angle = point_on_path('M0,0 L10,10',7.0710678)
        self.assertAlmostEqual(x,5)
        self.assertAlmostEqual(y,5)
        self.assertAlmostEqual(angle,45)
        with self.assertRaises(ValueError):
            point_on_path('M0,0 C1,2 3,4 5,6',0)

    def test_embedded_woff_is_registered_and_embedded_in_pdf(self):
        builder = FontBuilder(1000,isTTF=True)
        builder.setupGlyphOrder(['.notdef','A','B'])
        builder.setupCharacterMap({65:'A',66:'B'})
        glyphs = {}
        for name in ('.notdef','A','B'):
            pen = TTGlyphPen(None)
            pen.moveTo((50,0)); pen.lineTo((300,700)); pen.lineTo((550,0)); pen.closePath()
            glyphs[name] = pen.glyph()
        builder.setupGlyf(glyphs)
        builder.setupHorizontalMetrics({n:(600,50) for n in glyphs})
        builder.setupHorizontalHeader(ascent=800,descent=-200)
        builder.setupNameTable({'familyName':'Synthetic Fixture','styleName':'Regular',
                               'uniqueFontIdentifier':'Synthetic Fixture 1','fullName':'Synthetic Fixture',
                               'psName':'SyntheticFixture'})
        builder.setupOS2(sTypoAscender=800,sTypoDescender=-200,usWinAscent=800,usWinDescent=200)
        builder.setupPost()
        builder.setupMaxp()
        builder.font.flavor='woff'
        buffer=BytesIO(); builder.font.save(buffer)
        encoded=base64.b64encode(buffer.getvalue()).decode()
        svg=('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200">'
             '<style>@font-face {font-family: "Fixture"; src: url(data:font/woff;base64,'+encoded+');}</style>'
             '<path id="line" d="M10,80 L100,80" stroke="none"/>'
             '<text font-family="Fixture" font-size="40"><textPath href="#line" startOffset="0" textLength="60">AB</textPath></text></svg>').encode()
        prepared=prepare_svg(svg,self.directory/'fonts')
        output=cairosvg.svg2pdf(bytestring=prepared)
        with pymupdf.open(stream=output,filetype='pdf') as doc:
            self.assertIn('AB',doc[0].get_text())
            self.assertTrue(all('issuu_' in f[3] for f in doc[0].get_fonts()))

    def test_pipeline_interruption_resume_does_not_duplicate_pages(self):
        output = self.directory/'out.pdf'
        args = Namespace(url='https://issuu.com/test/docs/book', output=str(output),
                         cache_dir=str(self.directory/'cache'), clean=False, manifest=None,
                         delay=0, jpeg_quality=None,image_scale=1.0,compress=False,keep_cache=True)
        metadata = {'document':{'publicationId':'test','revisionId':'v1','pages':[
            {'svgUrl':f'https://test/Page{n}','width':400,'height':200} for n in (1,2,3)]}}
        failed = False
        def serve(req):
            nonlocal failed
            if req.url.host == 'publication.issuu.com':
                return httpx.Response(200,json=metadata)
            if req.url.path == '/Page2' and not failed:
                failed = True
                return httpx.Response(404)
            return httpx.Response(200,content=SVG.replace(b'Synthetic archival page',req.url.path.encode()))
        real_client = httpx.Client
        with patch('archival.httpx.Client',side_effect=lambda **kw: real_client(transport=httpx.MockTransport(serve),**kw)):
            with self.assertRaises(httpx.HTTPStatusError):
                archival.run(args)
            self.assertFalse(output.exists())
            archival.run(args)
            self.assertEqual(len(PdfReader(output).pages),3)
            archival.run(args)
            self.assertEqual(len(PdfReader(output).pages),3)
        self.assertTrue(json.loads((self.directory/'out_validation.json').read_text())['passed'])


if __name__ == '__main__':
    unittest.main()
