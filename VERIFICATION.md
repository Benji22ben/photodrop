# Verification — 9 September 2026

The app and deployment files were tested locally. The user's actual Coolify instance and Cloudflare account were not connected; the README provides the deployment steps, verified against official documentation.

| Check | Result |
| --- | --- |
| Python backend tests | 8 passed |
| JavaScript syntax / Python compilation | Passed |
| Docker build | Passed, Linux ARM64, Debian trixie |
| Production container settings | Healthy, non-root, read-only root filesystem, 2 GiB memory cap |
| Source parity | Running container's `app.py` SHA-256 matched the delivered file |
| Browser flow | Chromium drag/drop, mobile layout, partial failure, page reload, ZIP download, deletion passed; no page errors |
| Small-image batch | 300 HEIC → 300 verified 96 × 64 JPGs; one input crossed the 8 MiB upload boundary |
| Full-resolution batch | 300 HEIC → 300 verified 4032 × 3024 JPGs; 335,440,584 upload bytes; 395,208,022-byte ZIP; 52.87 seconds locally |

The full-resolution batch repeated generated artwork encoded with `heif-enc -q 80`, with a valid free-space box appended to one input to test chunking. Every JPG in the ZIP was decoded with Pillow and checked for format/dimensions; ZIP CRCs, unique names, and post-download deletion were checked too. This proves batch handling for these fixtures, not a throughput guarantee for every phone/camera format or internet connection.

Backend tests cover actual CLI conversion, duplicate/path-like Unicode filenames, mixed/all-invalid images, chunk retry offsets and size bounds, premature start, repeated start, cross-origin mutation rejection, capacity limits, disk exhaustion guard, retention/download protection, decoder timeout, cancellation, and deletion.

Screenshots from browser verification are saved under `test-results/` (ignored by Git). Run instructions for all checks are in the README.

## Mislabeled HEIF regression — 10 September 2026

The supplied `.heif` sample contains JPEG/MPO data, not HEIF. The old Docker app rejected it while converting a genuine HEIC in the same batch. The corrected Docker app validates its JPEG data and places the original bytes in the ZIP under a `.jpg` name. Verification confirmed byte-for-byte preservation, readable 1536 × 2048 pixels, a valid ZIP, and successful genuine HEIC conversion in the same batch. The original was not modified or added to Git.

The dependency lock and Docker image now include Pillow at runtime for JPEG validation. The validation subprocess shares conversion timeout/cancellation handling. Regression tests cover baseline/progressive JPEG, JPEG/MPO, EXIF preservation, fake JPEG signatures, truncated JPEGs, bounded/redacted decoder diagnostics, and terminated decoders. These changes are local; the Coolify deployment requires a rebuild with the updated files.
