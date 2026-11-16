"""Honor embedded WOFF fonts and Issuu's straight text paths in Cairo.

CairoSVG does not load CSS @font-face fonts itself. Register temporary renamed
copies with Fontconfig, then translate straight text paths into positioned text.
Original source files are never modified. Unsupported paths fail explicitly.
"""
import base64
import ctypes
import ctypes.util
import hashlib
import math
import re
from io import BytesIO
from pathlib import Path

import cairocffi as cairo
from fontTools.ttLib import TTFont
from lxml import etree

SVG = '{http://www.w3.org/2000/svg}'
NUMBER = r'[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?'
SEGMENT = re.compile(rf'M\s*({NUMBER})[,\s]+({NUMBER})\s*L\s*({NUMBER})[,\s]+({NUMBER})\s*')
REGISTERED = set()


def register_font(path):
    if str(path.resolve()) in REGISTERED:
        return
    library = ctypes.util.find_library('fontconfig')
    if not library:
        raise RuntimeError('Embedded SVG fonts require Fontconfig; install fontconfig or use Linux/WSL')
    fc = ctypes.CDLL(library)
    fc.FcConfigGetCurrent.restype = ctypes.c_void_p
    fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p,ctypes.c_char_p]
    fc.FcConfigAppFontAddFile.restype = ctypes.c_int
    config = fc.FcConfigGetCurrent()
    if not fc.FcConfigAppFontAddFile(config,str(path.resolve()).encode()):
        raise RuntimeError('Cannot register embedded SVG font with Fontconfig')
    REGISTERED.add(str(path.resolve()))


def point_on_path(data, offset):
    segments = list(SEGMENT.finditer(data))
    if not segments or SEGMENT.sub('', data).strip():
        raise ValueError('Unsupported SVG text path: only straight M/L segments are supported')
    remaining = offset
    for i,match in enumerate(segments):
        x,y,end_x,end_y = map(float,match.groups())
        dx,dy = end_x-x,end_y-y
        length = math.hypot(dx,dy)
        if length <= 0:
            continue
        if remaining <= length or i == len(segments)-1:
            if remaining > length+0.2:
                raise ValueError('Text start offset exceeds source path')
            return x+dx*remaining/length,y+dy*remaining/length,math.atan2(dy,dx)*180/math.pi
        remaining -= length
    raise ValueError('Empty source text path')


def prepare_svg(data, font_directory):
    root = etree.fromstring(data,etree.XMLParser(resolve_entities=False,no_network=True))
    font_directory = Path(font_directory)
    aliases = {}
    for style in root.iter(SVG+'style'):
        for block in re.findall(r'@font-face\s*\{([^}]+)\}',style.text or '',re.S):
            family = re.search(r'font-family:\s*[\"\x27]([^\"\x27]+)[\"\x27]',block)
            encoded = re.search(r'src:\s*url\(data:[^,]+;base64,([^\)]+)\)',block)
            if not family or not encoded:
                raise ValueError('Unsupported embedded font declaration')
            raw = base64.b64decode(encoded.group(1),validate=True)
            alias = 'issuu_'+hashlib.sha256(raw).hexdigest()[:24]
            path = font_directory/(alias+'.ttf')
            if not path.exists():
                font_directory.mkdir(parents=True,exist_ok=True)
                font = TTFont(BytesIO(raw))
                font.flavor = None
                for name in font['name'].names:
                    if name.nameID in (1,4,6,16):
                        name.string = alias.encode(name.getEncoding())
                font.save(str(path))
            register_font(path)
            aliases[family.group(1)] = alias
    for element in root.iter():
        family = element.get('font-family')
        if family in aliases:
            element.set('font-family',aliases[family])
    paths = {p.get('id'):p.get('d','') for p in root.iter(SVG+'path')}
    context = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32,1,1))
    for text in root.iter(SVG+'text'):
        children = list(text)
        if not any(c.tag == SVG+'textPath' for c in children):
            continue
        if len(children) != 1 or children[0].tag != SVG+'textPath':
            raise ValueError('Unsupported mixed SVG text path content')
        child = children[0]
        href = child.get('href') or child.get('{http://www.w3.org/1999/xlink}href','')
        if href[1:] not in paths or not href.startswith('#'):
            raise ValueError('SVG text path reference not found')
        offset = float(child.get('startOffset','0'))
        x,y,angle = point_on_path(paths[href[1:]],offset)
        word = ' '.join(''.join(child.itertext()).split()) or ' '
        family = text.get('font-family','sans-serif')
        if aliases and family not in aliases.values():
            raise ValueError('Text references an unavailable embedded font')
        context.select_font_face(family)
        context.set_font_size(float(text.get('font-size','12')))
        advance = context.text_extents(word)[4]
        declared_width = child.get('textLength',text.get('textLength',str(advance)))
        width = advance if declared_width == 'auto' else float(declared_width)
        scale = width/advance if advance > 0 else 1
        transform = text.get('transform','')
        text.set('transform',f'{transform} translate({x},{y}) rotate({angle}) scale({scale},1)')
        text.set('x','0')
        text.set('y','0')
        text.attrib.pop('textLength',None)
        text.remove(child)
        text.text = word
    return etree.tostring(root)
