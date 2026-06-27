import urllib.request, os
p = '/usr/local/lib/docker/cli-plugins'
os.makedirs(p, exist_ok=True)
url = 'https://github.com/docker/compose/releases/download/v2.29.2/docker-compose-linux-x86_64'
urllib.request.urlretrieve(url, f'{p}/docker-compose')
os.chmod(f'{p}/docker-compose', 0o755)
print('docker-compose installed')
