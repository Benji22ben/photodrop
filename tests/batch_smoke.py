"""Exercise a running server, real libheif CLI, chunking, ZIP download, and deletion."""
import argparse
import concurrent.futures
import io
import json
import struct
import time
import zipfile
from pathlib import Path

import httpx
from PIL import Image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('url')
    parser.add_argument('--count', type=int, default=300)
    parser.add_argument('--fixture', type=Path, default=Path(__file__).parent / 'fixtures/sample.heic')
    parser.add_argument('--dimensions', type=int, nargs=2, default=(96, 64), metavar=('WIDTH', 'HEIGHT'))
    args = parser.parse_args()
    sample = args.fixture.read_bytes()
    # A valid ISO-BMFF free-space box makes one image cross the actual 8 MiB chunk boundary.
    padding_size = 9 * 1024**2
    large = sample + struct.pack('>I4s', padding_size, b'free') + b'\0' * (padding_size - 8)
    start = time.monotonic()
    with httpx.Client(base_url=args.url, timeout=120) as client:
        config = client.get('/api/config').json()
        sizes = [len(large)] + [len(sample)] * (args.count - 1)
        response = client.post('/api/jobs', json={'files': [{'name': 'photo.heic', 'size': n} for n in sizes]})
        response.raise_for_status()
        job_id = response.json()['id']
        try:
            def upload(index):
                data = large if index == 0 else sample
                for offset in range(0, len(data), config['chunkBytes']):
                    response = client.put(f'/api/jobs/{job_id}/files/{index}?offset={offset}', content=data[offset:offset + config['chunkBytes']])
                    response.raise_for_status()
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(upload, range(args.count)))
            client.post(f'/api/jobs/{job_id}/start').raise_for_status()
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                state = client.get(f'/api/jobs/{job_id}').json()
                if state['state'] in {'done', 'failed'}:
                    break
                time.sleep(.3)
            assert state['state'] == 'done', state
            assert all(f['status'] == 'done' for f in state['files']), state
            result = client.get(state['downloadUrl'])
            result.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
                names = archive.namelist()
                assert len(names) == len(set(names)) == args.count
                assert archive.testzip() is None
                for name in names:
                    with Image.open(io.BytesIO(archive.read(name))) as image:
                        image.load()
                        assert image.format == 'JPEG' and image.size == tuple(args.dimensions)
            report = {'photos': args.count, 'jpgs_verified': len(names), 'upload_bytes': sum(sizes),
                      'zip_bytes': len(result.content), 'elapsed_seconds': round(time.monotonic() - start, 2),
                      'chunk_boundary_tested': True, 'dimensions': args.dimensions, 'server': args.url}
            print(json.dumps(report, indent=2))
        finally:
            client.delete(f'/api/jobs/{job_id}').raise_for_status()
        assert client.get(f'/api/jobs/{job_id}').status_code == 404


if __name__ == '__main__':
    main()
