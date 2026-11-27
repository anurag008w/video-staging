import os, json, hashlib, subprocess, re, glob, zipfile, sys, time
import requests

url = os.environ['SOURCE_URL']
kind = os.environ['KIND']                      # pdf | ncert_zip
src_sha = os.environ['SOURCE_SHA256'].lower()
pages_expected = int(os.environ.get('EXPECTED_PAGES') or 0)
folder = os.environ.get('FOLDER_ID', '')
folder_name = os.environ['FOLDER_NAME']
name = os.environ['FILE_NAME']
assert url.startswith(('https://ncert.nic.in/textbook/pdf/', 'https://books.ebalbharati.in/pdfs/'))
assert re.fullmatch(r'[0-9a-f]{64}', src_sha)
assert folder == '' or re.fullmatch(r'[A-Za-z0-9_-]{20,60}', folder)

def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()

raw = 'source.bin'
for attempt in range(60):
    subprocess.run(['curl', '-sS', '-k', '-L', '-C', '-', '--max-time', '300', '-o', raw, url])
    if os.path.exists(raw) and sha256(raw) == src_sha:
        break
    time.sleep(2)
assert sha256(raw) == src_sha, 'source SHA-256 mismatch after retries'
print('source verified, bytes', os.path.getsize(raw))

if kind == 'pdf':
    out = raw
else:
    os.makedirs('z', exist_ok=True)
    zipfile.ZipFile(raw).extractall('z')
    files = glob.glob('z/**/*.pdf', recursive=True)
    base = lambda p: os.path.basename(p).lower()
    ps = sorted(p for p in files if base(p).endswith('ps.pdf'))
    an = sorted(p for p in files if base(p).endswith('an.pdf'))
    chap = sorted((p for p in files if re.search(r'1(\d\d)\.pdf$', base(p)) and p not in ps + an),
                  key=lambda p: int(re.search(r'1(\d\d)\.pdf$', base(p)).group(1)))
    rest = sorted(p for p in files if p not in ps + an + chap)
    order = ps + chap + rest + an
    print('merge order:', [os.path.basename(p) for p in order])
    subprocess.run(['qpdf', '--empty', '--pages'] + order + ['--', 'merged.pdf'], check=True)
    out = 'merged.pdf'

q = subprocess.run(['qpdf', '--show-npages', out], capture_output=True, text=True)
if q.returncode == 0:
    pages = int(q.stdout.strip())
else:
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'pypdf'], check=True)
    from pypdf import PdfReader
    pages = len(PdfReader(out).pages)
print('pages', pages)
assert open(out, 'rb').read(5) == b'%PDF-'
if pages_expected:
    assert pages == pages_expected, f'page count {pages} != {pages_expected}'
size = os.path.getsize(out)
md5 = hashlib.md5(open(out, 'rb').read()).hexdigest()

c = json.loads(os.environ['DRIVE_CREDENTIAL'])
r = requests.post('https://oauth2.googleapis.com/token', data={**c, 'grant_type': 'refresh_token'}, timeout=30)
if r.status_code != 200:
    e = r.json() if 'json' in r.headers.get('content-type', '') else {}
    sys.exit('Drive credential rejected by Google token endpoint: HTTP %s error=%s description=%s keys_present=%s' % (r.status_code, e.get('error'), e.get('error_description'), sorted(c.keys())))
h = {'Authorization': 'Bearer ' + r.json()['access_token']}
api = 'https://www.googleapis.com/drive/v3/files'
if folder:
    r = requests.get(api + '/' + folder, headers=h, params={'fields': 'id,name,mimeType,ownedByMe,trashed'}, timeout=30)
    assert r.status_code == 200, f'destination folder not visible to credential (HTTP {r.status_code})'
    fm = r.json()
    assert fm['mimeType'] == 'application/vnd.google-apps.folder' and fm['name'] == folder_name and fm['ownedByMe'] and not fm['trashed'], 'destination folder check failed'
else:
    # drive.file mode: find or create an app-owned staging folder by name
    r = requests.get(api, headers=h, params={'q': f"trashed=false and mimeType='application/vnd.google-apps.folder' and name='{folder_name}'", 'fields': 'files(id,name,ownedByMe)'}, timeout=30)
    r.raise_for_status()
    fs = [f for f in r.json()['files'] if f.get('ownedByMe')]
    if fs:
        folder = fs[0]['id']
    else:
        r = requests.post(api, headers=h, json={'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder'}, timeout=30)
        r.raise_for_status()
        folder = r.json()['id']
print('destination folder verified/created')

r = requests.get(api, headers=h, params={'q': f"trashed=false and '{folder}' in parents", 'fields': 'files(id,name,size,md5Checksum)'}, timeout=30)
r.raise_for_status()
same = [f for f in r.json()['files'] if f['name'] == name]
dup = [f for f in same if f.get('md5Checksum') == md5 and int(f.get('size', 0)) == size]
if dup:
    f = dup[0]
    print('identical file already present, not re-uploading')
elif same:
    sys.exit('a different file with this name already exists in the folder; stopping')
else:
    r = requests.post('https://www.googleapis.com/upload/drive/v3/files', headers={**h, 'X-Upload-Content-Type': 'application/pdf', 'X-Upload-Content-Length': str(size)},
                      params={'uploadType': 'resumable', 'fields': 'id,name,size,md5Checksum'}, json={'name': name, 'parents': [folder]}, timeout=30)
    r.raise_for_status()
    with open(out, 'rb') as data:
        r = requests.put(r.headers['Location'], headers={'Content-Type': 'application/pdf', 'Content-Length': str(size)}, data=data, timeout=900)
    r.raise_for_status()
    f = r.json()
assert int(f['size']) == size and f['md5Checksum'] == md5, 'Drive size/MD5 mismatch'
r = requests.get(api + '/' + f['id'], headers=h, params={'fields': 'permissions(type,role)'}, timeout=30)
r.raise_for_status()
assert not any(p['type'] in ('anyone', 'domain') for p in r.json().get('permissions', [])), 'file is shared more widely than private'
res = {'folder_id': folder, 'file_id': f['id'], 'name': name, 'bytes': size, 'md5': md5, 'pages': pages, 'private': True}
print(json.dumps({k: v for k, v in res.items() if k not in ('folder_id', 'file_id')}, indent=1))
open('upload-result.json', 'w').write(json.dumps(res))
