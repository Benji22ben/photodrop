"""Real browser drop → conversion → ZIP, plus responsive layout and page reload."""
import base64
import io
import sys
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

url = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:8000'
fixture = Path(__file__).parent / 'fixtures/sample.heic'
out = Path('test-results')
out.mkdir(exist_ok=True)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(url)
    expect(page.locator('#limits')).to_contain_text('1,000')
    page.screenshot(path=str(out / 'desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    page.screenshot(path=str(out / 'mobile.png'), full_page=True)
    page.set_viewport_size({'width': 1100, 'height': 900})
    payload = base64.b64encode(fixture.read_bytes()).decode()
    page.evaluate('''(payload) => {
      const bytes = Uint8Array.from(atob(payload), c => c.charCodeAt(0));
      const transfer = new DataTransfer();
      for (let i = 0; i < 12; i++) transfer.items.add(new File([bytes], 'photo.heic', {type:'image/heic'}));
      transfer.items.add(new File(['bad'], 'broken.heic', {type:'image/heic'}));
      document.querySelector('#dropzone').dispatchEvent(new DragEvent('drop', {bubbles:true, dataTransfer:transfer}));
    }''', payload)
    assert page.locator('#selection-count').inner_text() == '13 photos selected'
    page.click('#convert')
    expect(page.locator('#phase')).to_have_text('Your photos are ready', timeout=120000)
    assert '12 of 13 photos converted' in page.locator('#progress-detail').inner_text()
    page.reload()
    page.locator('#download').wait_for(state='visible', timeout=10000)
    with page.expect_download() as event:
        page.click('#download')
    download = event.value
    assert download.failure() is None
    with zipfile.ZipFile(download.path()) as archive:
        assert len([n for n in archive.namelist() if n.endswith('.jpg')]) == 12
        assert 'conversion-errors.json' in archive.namelist()
    page.locator('#file-details').evaluate('(el) => {el.open = true}')
    page.screenshot(path=str(out / 'completed.png'), full_page=True)
    page.click('#reset')
    expect(page.locator('#progress-panel')).to_be_hidden()
    assert not errors, errors
    browser.close()
print('Browser smoke passed: drag/drop, mobile layout, partial failure, reload, ZIP download, deletion; no page errors.')
