"""Validate a JPEG mislabeled as HEIC/HEIF, then preserve its original bytes.

Runs in a child process so validation obeys the conversion timeout/cancellation
and does not block the API or allocate decoded pixels in the server process.
"""
import shutil
import sys
import warnings

from PIL import Image


def prepare(source, output):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source, formats=("JPEG",)) as image:
            image.load()  # Header recognition alone does not detect truncation.
    shutil.copyfile(source, output)


if __name__ == "__main__":
    try:
        prepare(sys.argv[1], sys.argv[2])
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        print(f"JPEG validation failed: {exc}", file=sys.stderr)
        sys.exit(1)
