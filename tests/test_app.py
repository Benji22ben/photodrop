import io
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

SAMPLE = (Path(__file__).parent / "fixtures/sample.heic").read_bytes()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA", tmp_path / "batches")
    monkeypatch.setattr(app, "MIN_FREE_BYTES", 0)
    with TestClient(app.app) as c:
        yield c


def create(client, names=None, data=SAMPLE):
    response = client.post('/api/jobs', json={"files": [
        {"name": name, "size": len(data)} for name in (names or ['photo.heic'])]})
    assert response.status_code == 201, response.text
    return response.json()['id']


def upload(client, job_id, index=0, data=SAMPLE):
    response = client.put(f'/api/jobs/{job_id}/files/{index}', content=data)
    assert response.status_code == 200, response.text


def finish(client, job_id):
    assert client.post(f'/api/jobs/{job_id}/start').status_code == 202
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = client.get(f'/api/jobs/{job_id}').json()
        if state['state'] in {'done', 'failed'}:
            return state
        time.sleep(.03)
    pytest.fail('Batch did not finish')


def test_real_conversion_duplicate_names_and_zip_range(client):
    job_id = create(client, ['../café.heic', '../café.heic'])
    upload(client, job_id, 0)
    upload(client, job_id, 1)
    state = finish(client, job_id)
    assert state['state'] == 'done'
    response = client.get(state['downloadUrl'])
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist())) == 2
        assert all('/' not in name and name.endswith('.jpg') for name in archive.namelist())
        for name in archive.namelist():
            with Image.open(io.BytesIO(archive.read(name))) as im:
                im.load()
                assert im.format == 'JPEG' and im.size == (96, 64)
    partial = client.get(state['downloadUrl'], headers={'Range': 'bytes=0-9'})
    assert partial.status_code == 206 and partial.content == response.content[:10]
    assert not list((app.DATA / job_id / 'input').iterdir())
    assert not (app.DATA / job_id / 'output').exists()
    assert client.delete(f'/api/jobs/{job_id}').status_code == 202
    assert client.get(f'/api/jobs/{job_id}').status_code == 404
    assert not (app.DATA / job_id).exists()


def test_partial_failure_is_reported_in_zip(client):
    job_id = create(client, ['ok.heic', 'broken.heic'])
    upload(client, job_id, 0)
    upload(client, job_id, 1, b'x' * len(SAMPLE))
    state = finish(client, job_id)
    assert state['state'] == 'done'
    assert [f['status'] for f in state['files']] == ['done', 'failed']
    with zipfile.ZipFile(io.BytesIO(client.get(state['downloadUrl']).content)) as archive:
        assert len(archive.namelist()) == 2
        assert json.loads(archive.read('conversion-errors.json'))[0]['name'] == 'broken.heic'


def test_all_invalid_has_no_zip(client):
    job_id = create(client, data=b'bad')
    upload(client, job_id, data=b'bad')
    assert finish(client, job_id)['state'] == 'failed'
    assert client.get(f'/api/jobs/{job_id}/download').status_code == 409


def test_chunk_retry_and_upload_validation(client, monkeypatch):
    monkeypatch.setattr(app, 'CHUNK_BYTES', 500)
    job_id = create(client)
    url = f'/api/jobs/{job_id}/files/0'
    assert client.post(f'/api/jobs/{job_id}/start').status_code == 409
    assert client.put(url, content=b'x' * 501).status_code == 413
    assert client.get(f'/api/jobs/{job_id}').json()['files'][0]['uploaded'] == 0
    assert client.put(url, content=SAMPLE[:500]).status_code == 200
    assert client.put(url, content=SAMPLE[:500]).status_code == 409
    offset = client.get(f'/api/jobs/{job_id}').json()['files'][0]['uploaded']
    while offset < len(SAMPLE):
        result = client.put(f'{url}?offset={offset}', content=SAMPLE[offset:offset + 500])
        assert result.status_code == 200
        offset = result.json()['uploaded']
    assert finish(client, job_id)['state'] == 'done'
    assert client.post(f'/api/jobs/{job_id}/start').status_code == 202
    assert client.put(url, content=b'x').status_code == 409


def test_limits_and_cross_origin_requests(client, monkeypatch):
    assert client.post('/api/jobs', json={'files': []}).status_code == 422
    assert client.post('/api/jobs', json={'files': [{'name': 'a.jpg', 'size': 1}]}).status_code == 422
    assert client.post('/api/jobs', content=b'x' * (1024 * 1024 + 1)).status_code == 413
    assert client.post('/api/jobs', headers={'Origin': 'https://other.example'}, json={}).status_code == 403
    job_id = create(client)
    assert client.get('/api/jobs/not-a-token').status_code == 404
    assert client.put(f'/api/jobs/{job_id}/files/-1', content=b'x').status_code == 404
    assert client.put(f'/api/jobs/{job_id}/files/0?offset=-1', content=b'x').status_code == 409
    monkeypatch.setattr(app, 'MAX_JOBS', 1)
    assert client.post('/api/jobs', json={'files': [{'name': 'a.heic', 'size': 1}]}).status_code == 429
    client.delete(f'/api/jobs/{job_id}')
    monkeypatch.setattr(app, 'MAX_BATCH_BYTES', 1)
    assert client.post('/api/jobs', json={'files': [{'name': 'a.heic', 'size': 2}]}).status_code == 413


def test_expiry_protects_active_downloads(client):
    job_id = create(client)
    batch = app.jobs[job_id]
    batch.touched = time.time() - app.TTL - 1
    batch.downloads = 1
    app.clean_expired()
    assert job_id in app.jobs
    assert client.delete(f'/api/jobs/{job_id}').status_code == 409
    batch.downloads = 0
    app.clean_expired()
    assert job_id not in app.jobs and not batch.path.exists()


def test_timeout_and_cancel_kill_decoder(client, tmp_path, monkeypatch):
    slow = tmp_path / 'slow-decoder'
    slow.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(60)\n')
    slow.chmod(0o755)
    monkeypatch.setattr(app, 'converter', str(slow))
    monkeypatch.setattr(app, 'CONVERSION_TIMEOUT', .1)
    job_id = create(client)
    upload(client, job_id)
    result = finish(client, job_id)
    assert result['state'] == 'failed'
    assert result['files'][0]['error'] == 'Conversion timed out.'
    monkeypatch.setattr(app, 'CONVERSION_TIMEOUT', 60)
    job_id = create(client)
    upload(client, job_id)
    client.post(f'/api/jobs/{job_id}/start')
    client.delete(f'/api/jobs/{job_id}')
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and client.get(f'/api/jobs/{job_id}').status_code != 404:
        time.sleep(.03)
    assert client.get(f'/api/jobs/{job_id}').status_code == 404
    assert not (app.DATA / job_id).exists()


def test_disk_guard(client, monkeypatch):
    monkeypatch.setattr(app, 'MIN_FREE_BYTES', 10**20)
    assert client.post('/api/jobs', json={'files': [{'name': 'a.heic', 'size': 1}]}).status_code == 507


def test_decoder_diagnostic_is_bounded_and_redacted(client, tmp_path, monkeypatch):
    failing = tmp_path / 'failing-decoder'
    failing.write_text(
        f'#!{sys.executable}\nimport sys\n'
        'sys.stderr.write("Unsupported image type: " + sys.argv[-2] + "\\n" + "x" * 200000)\n'
        'sys.stderr.flush()\nsys.exit(1)\n')
    failing.chmod(0o755)
    monkeypatch.setattr(app, 'converter', str(failing))
    job_id = create(client)
    upload(client, job_id)
    state = finish(client, job_id)
    message = state['files'][0]['error']
    assert state['state'] == 'failed'
    assert 'Unsupported image type' in message and 'exit 1' in message
    assert job_id not in message and str(app.DATA) not in message
    assert len(message) < 1200


def test_decoder_signal_does_not_blame_image(client, tmp_path, monkeypatch):
    failing = tmp_path / 'killed-decoder'
    failing.write_text(f'#!{sys.executable}\nimport os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n')
    failing.chmod(0o755)
    monkeypatch.setattr(app, 'converter', str(failing))
    job_id = create(client)
    upload(client, job_id)
    message = finish(client, job_id)['files'][0]['error']
    assert 'signal 9' in message and 'memory limit' in message


@pytest.mark.parametrize('format,progressive', [('JPEG', False), ('JPEG', True), ('MPO', False)])
def test_jpeg_with_heif_extension_preserves_bytes_and_metadata(client, monkeypatch, format, progressive):
    data = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6  # Rotation metadata must survive without pixel re-encoding.
    options = {'save_all': True, 'append_images': [Image.new('RGB', (48, 32))]} if format == 'MPO' else {}
    Image.new('RGB', (96, 64), '#264e43').save(data, format, exif=exif, progressive=progressive, **options)
    original = data.getvalue()
    job_id = create(client, ['export.heif'], data=original)
    upload(client, job_id, data=original)
    # A disguised JPEG must never be sent to the HEIC decoder.
    monkeypatch.setattr(app, 'converter', '/not-a-real-heic-decoder')
    state = finish(client, job_id)
    assert state['state'] == 'done'
    assert state['files'][0]['preservedJpeg'] is True
    with zipfile.ZipFile(io.BytesIO(client.get(state['downloadUrl']).content)) as archive:
        assert archive.namelist() == ['0001-export.jpg']
        assert archive.read('0001-export.jpg') == original


@pytest.mark.parametrize('invalid', [b'\xff\xd8\xffnot-a-jpeg', None])
def test_invalid_or_truncated_disguised_jpeg_is_rejected(client, invalid):
    if invalid is None:
        data = io.BytesIO()
        Image.new('RGB', (96, 64)).save(data, 'JPEG')
        invalid = data.getvalue()[:-10]
    job_id = create(client, ['broken.heif'], data=invalid)
    upload(client, job_id, data=invalid)
    state = finish(client, job_id)
    assert state['state'] == 'failed'
    assert 'JPEG validation failed' in state['files'][0]['error']
    assert client.get(f'/api/jobs/{job_id}/download').status_code == 409
