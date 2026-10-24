"""Private GitHub Release transport. No provider data or plaintext accepted."""
import re
import httpx
from src.archive.crypto import unseal


async def upload(repo,token,blob,key,transport=None):
    # Authenticate content before any network request.
    data=unseal(blob,key)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise ValueError('Invalid archive repository')
    if not token:raise ValueError('Archive token missing')
    async with httpx.AsyncClient(transport=transport,timeout=30,follow_redirects=False,
        headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}) as client:
        response=await client.get('https://api.github.com/repos/'+repo)
        if response.status_code!=200 or response.json().get('private') is not True:raise ValueError('Archive target must be a verified private repository')
        import hashlib
        tag='owned-audit-'+data['created_at'].replace(':','-').replace('+','-')+'-'+hashlib.sha256(blob).hexdigest()[:12]
        response=await client.post('https://api.github.com/repos/'+repo+'/releases',json={
            'tag_name':tag,'name':tag,'body':'Encrypted allowlisted audit projection. Not a full operational restore.','draft':True})
        if response.status_code!=201:raise ValueError('Could not create new archive Release')
        release_id=response.json()['id']
        # Fixed GitHub upload host; never follow an untrusted response URL with the token.
        response=await client.post(f'https://uploads.github.com/repos/{repo}/releases/{release_id}/assets',
            params={'name':'owned-audit.enc.json'},headers={'Content-Type':'application/octet-stream'},content=blob)
        if response.status_code!=201:raise ValueError('Encrypted asset upload failed; draft left for inspection')
        response=await client.patch(f'https://api.github.com/repos/{repo}/releases/{release_id}',json={'draft':False})
        if response.status_code!=200:raise ValueError('Archive Release publication failed')
    return tag
