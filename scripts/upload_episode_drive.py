import os,json,hashlib,time,urllib.parse,requests
url=os.environ['SOURCE_URL'];expected=os.environ['EXPECTED_SHA256'];name=os.environ['EPISODE_NAME']
assert url.startswith('https://github.com/anurag008w/video-staging/releases/download/')
assert len(expected)==64 and all(c in '0123456789abcdef' for c in expected)
c=json.loads(os.environ['DRIVE_CREDENTIAL']);r=requests.post('https://oauth2.googleapis.com/token',data={**c,'grant_type':'refresh_token'},timeout=30);r.raise_for_status();t=r.json();h={'Authorization':'Bearer '+t['access_token']}
p='episode.mp4';r=requests.get(url,stream=True,timeout=60);r.raise_for_status();sha=hashlib.sha256();md5=hashlib.md5();size=0
with open(p,'wb') as f:
 for chunk in r.iter_content(1024*1024):
  f.write(chunk);sha.update(chunk);md5.update(chunk);size+=len(chunk)
assert sha.hexdigest()==expected,'Downloaded SHA mismatch'
api='https://www.googleapis.com/drive/v3/files'
q="trashed=false and mimeType='application/vnd.google-apps.folder' and name='HSC Anime Episodes'"
r=requests.get(api,headers=h,params={'q':q,'fields':'files(id,name)'},timeout=30);r.raise_for_status();fs=r.json()['files']
if fs:folder=fs[0]['id']
else:
 r=requests.post(api,headers=h,json={'name':'HSC Anime Episodes','mimeType':'application/vnd.google-apps.folder'},timeout=30);r.raise_for_status();folder=r.json()['id']
# Idempotent: reuse only an exact same-name file with matching MD5 and size.
r=requests.get(api,headers=h,params={'q':"trashed=false and '"+folder+"' in parents",'fields':'files(id,name,size,md5Checksum,webViewLink)'},timeout=30);r.raise_for_status();matches=[f for f in r.json()['files'] if f['name']==name and f.get('md5Checksum')==md5.hexdigest() and int(f.get('size',0))==size]
if matches:f=matches[0]
else:
 r=requests.post('https://www.googleapis.com/upload/drive/v3/files',headers={**h,'X-Upload-Content-Type':'video/mp4','X-Upload-Content-Length':str(size)},params={'uploadType':'resumable','fields':'id,name,size,md5Checksum,webViewLink'},json={'name':name,'parents':[folder]},timeout=30);r.raise_for_status();upload=r.headers['Location']
 with open(p,'rb') as data:r=requests.put(upload,headers={'Content-Type':'video/mp4','Content-Length':str(size)},data=data,timeout=180)
 r.raise_for_status();f=r.json()
assert int(f['size'])==size and f['md5Checksum']==md5.hexdigest(),'Drive size/checksum mismatch'
r=requests.get(api+'/'+f['id'],headers=h,params={'fields':'id,name,size,md5Checksum,webViewLink,permissions(type,role,emailAddress)'},timeout=30);r.raise_for_status();f=r.json();assert not any(p['type'] in ['anyone','domain'] for p in f.get('permissions',[]))
f.pop('permissions',None)
report={'source_url':url,'sha256':sha.hexdigest(),'bytes':size,'md5':md5.hexdigest(),'drive':f,'folder_id':folder}
open('upload-result.json','w').write(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as out:out.write('Verified original episode uploaded privately: '+f['webViewLink']+'\n\nSHA-256: `'+sha.hexdigest()+'`\n')
