# Issuu PDF Downloader

Export **authorized, publicly accessible** Issuu SVG pages into one verified PDF.
Fork of [gbmerrall/issuu-downloader](https://github.com/gbmerrall/issuu-downloader),
MIT copyright 2025 Graeme Merrall. See LICENSE.

## Setup

Python 3.10+ and system Cairo are required. This fork was tested on Linux with
Python 3.12.14 and Cairo 1.18.4. macOS and Windows instructions are provided but
were not executed in this environment.

- **macOS:** `brew install cairo fontconfig`; install Python 3.10 or later.
- **Ubuntu/Debian:** `sudo apt-get install python3-venv libcairo2 fontconfig`
- **Fedora/RHEL:** `sudo dnf install python3 cairo fontconfig`
- **Windows:** Install Python 3.10+ and a matching Cairo runtime. One option is
  MSYS2's UCRT64 package `mingw-w64-ucrt-x86_64-cairo`; add its `bin` directory
  to PATH before launching Python. Cairo DLL and Python architectures must match.
  The original project's GTK runtime route also supplies Cairo:
  https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer

```bash
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python -c "import cairocffi; print(cairocffi.cairo_version_string())"
```

The lock file records the full tested environment. If a platform lacks a compatible
locked wheel, use `python -m pip install -r requirements.txt` and rerun tests.
PyMuPDF's licensing applies to that dependency separately from this project's MIT
license. HTTPX's SOCKS extra supports environments with configured SOCKS proxies;
no proxy rotation or network-access evasion is implemented.

## X25 export

Use only if you have permission to download/reproduce the publication. Public
viewability is not a grant of reproduction rights. This tool does not retrieve an
original publisher PDF; prefer an authorized original PDF if one is available.

```bash
python download_issuu.py "https://issuu.com/sjc_preview/docs/x25" \
  output/x25_high_quality.pdf --keep-cache --delay 2 --verify
```

The CLI normalizes viewer query strings/fragments. The output directory is created
automatically. All metadata-listed pages must have accessible SVGs. Paywall flags,
HTTP 401/403, missing pages, unsupported external SVG resources, invalid dimensions,
and validation failures stop the export. It does not bypass authentication, DRM,
paywalls, CAPTCHAs, download restrictions, or server rate limits. Metadata cannot
establish publisher permission; the operator remains responsible for that.

## Quality and completeness

Defaults preserve SVG vectors and original embedded image bytes before conversion;
there is no requested JPEG re-encoding, downscaling, rasterization, OCR, or upscaling.
When Cairo expands an embedded RGB JPEG, its decoded pixels are compared exactly
to the source; a match lets the original JPEG stream be restored without
re-encoding. Color-profiled, CMYK and masked images are left to Cairo.
Embedded WOFF fonts are decoded with FontTools and registered with Fontconfig.
Straight Issuu text paths are converted into positioned text using their source
offsets and lengths, avoiding Cairo font substitution and clipped end characters.
Curved or unsupported text-path structures fail explicitly. Fontconfig must be
available to Cairo; Linux/WSL is recommended for embedded-font publications on
Windows. Cairo may still have limitations for other complex SVG features. Source geometry determines PDF dimensions; metadata aspect
ratios are cross-checked. Viewer dimensions are not necessarily print dimensions.
Text is selectable only where source representation and Cairo support it. Outlined
letters and image-only pages do not become searchable; the report measures actual
extractable text. No print-master resolution is promised beyond exposed source data.

Every source SVG and converted page is checksummed. The merge follows metadata's
numeric page order and is checked against **all** per-page PDFs using raster
fingerprints. This avoids lexicographic page-10-before-page-2 errors and checks that
merging did not change page rendering. Only a complete validated candidate replaces
the destination; a failure preserves any previously existing destination PDF.
Compare its validation report before assuming it belongs to the latest attempt.

Conversion handles one SVG at a time; final PDF merging still requires memory
proportional to the document. It no longer repeatedly rewrites partial merged PDFs.

### Outputs

For the X25 command:

- `output/x25_high_quality.pdf`: published only after successful validation.
- `output/x25_manifest.json`: page sources, hashes, sizes, dimensions, settings,
  publication/revision identity, and completion status.
- `output/x25_validation.json`: expected/actual count, dimensions, warnings, text
  count, bytes, order verification errors, and pass/fail result.
- `output/validation_samples/`: first, middle, last, four seeded random samples,
  and any nearly uniform pages rendered for human review.

Nearly uniform pages are flagged for visual review rather than automatically
removed; intentionally blank source pages must remain. Automated checks cannot
prove perfect source-to-PDF rendering; inspect the rendered samples and any warnings.

## Options

| Option | Behavior |
|---|---|
| `--cache-dir PATH` | Default `.issuu_cache`; publication/revision-aware cache |
| `--keep-cache` | Retain source SVGs, page PDFs and internal manifest |
| `--clean` | Start fresh for this publication only |
| `--delay SECONDS` | Minimum delay after each new page download; 2 recommended |
| `--manifest PATH` | Choose manifest output path |
| `--verify`, `--strict` | Accepted; verification and strict completeness are always on |
| `--batch-size N` | Accepted for compatibility; processing is now per-page |
| `--compress` | Lossless PDF content-stream compression; no image recompression requested |
| `--jpeg-quality 1..100` | Explicit **lossy** JPEG re-encoding, even at 100 |
| `--image-scale FACTOR` | Explicit image resizing; below 1 loses resolution; above 1 invents pixels |

Lossy flags operate on working copies. Source SVGs remain unchanged so resumed
archival runs cannot accidentally reuse degraded assets. Resizing without specifying
JPEG quality uses quality 95, which is still lossy. For archival output, omit both
lossy flags. There is no page-range option; batch size does not limit total pages.

## Resume and troubleshooting

Rerun the same command to resume. Checksums, SVG structure and PDF validity are
verified before reuse. A changed conversion setting regenerates its page PDF.
A changed publication/revision forces new downloads. Interrupted `.part` output is
not presented as complete. Upstream progress/partial-PDF caches are deliberately
not trusted automatically; initial fork runs rebuild their own manifest.

Transient transport errors, HTTP 429 and 5xx receive at most three attempts with
exponential backoff and jitter. Retry-After is respected; waits over 60 seconds
stop with instructions to rerun later. HTTP 401/403 and other permanent errors are
not retried. If endpoint behavior changes or pages are unavailable, obtain an
authorized source PDF or publisher permission rather than guessing private URLs.

For Cairo import errors, verify the system runtime and architecture/PATH. For a
broken cache, rerun normally to recover checksum failures or use `--clean`. If
conversion fails on a complex SVG, retained assets identify the exact page.
Ensure sufficient disk space for source SVGs, page PDFs, candidate and final PDF.

## Verification and tests

Keep cache to allow a later independent order check against page PDFs:

```bash
python scripts/verify_pdf.py output/x25_high_quality.pdf \
  --manifest output/x25_manifest.json --report output/x25_validation.json \
  --render-dir output/validation_samples
python -m unittest discover -s tests -v
```

The offline test suite uses a small synthetic SVG, HTTPX mock responses, and local
PDFs. It tests URL validation, all-pages metadata, paywall stops, retry handling,
cache resume/recovery, interruption recovery without duplication, order checks,
PDF dimensions, text extraction, and strict missing-page failure. No copyrighted
publication pages, exported PDF, source assets, or session credentials belong in
the repository. See IMPLEMENTATION_NOTES.md for the actual X25 run results.
