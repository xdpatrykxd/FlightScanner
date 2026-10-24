"""Initialize a private WAHA API key and a suitable gateway image in .env."""
from pathlib import Path
import platform
import secrets
import shutil

ROOT=Path(__file__).resolve().parents[1]
ENV=ROOT/'.env'
EXAMPLE=ROOT/'.env.example'


def image_for_host():
    machine=platform.machine().lower()
    if machine in {'aarch64','arm64'}:return 'devlikeapro/waha:noweb-arm'
    if machine in {'x86_64','amd64'}:return 'devlikeapro/waha:noweb'
    raise SystemExit(f'Unsupported CPU architecture: {platform.machine()} (expected x86-64 or ARM64).')


def main():
    if not ENV.exists():
        shutil.copyfile(EXAMPLE,ENV)
        print('Created .env from .env.example.')
    lines=ENV.read_text().splitlines()
    values={line.split('=',1)[0]:line.split('=',1)[1] for line in lines if '=' in line and not line.lstrip().startswith('#')}
    key=values.get('WAHA_API_KEY','').strip()
    updates={}
    if not key or key.startswith('replace-with-') or key=='generate-with-setup-script':
        updates['WAHA_API_KEY']=secrets.token_hex(32)
    image=image_for_host()
    current_image=values.get('WAHA_IMAGE','')
    if not current_image or current_image.startswith('select-with-setup-script') or current_image in {'devlikeapro/waha:noweb','devlikeapro/waha:noweb-arm'}:
        updates['WAHA_IMAGE']=image
    out=[];written=set()
    for line in lines:
        if '=' in line and not line.lstrip().startswith('#'):
            name=line.split('=',1)[0]
            if name in updates:
                out.append(f'{name}={updates[name]}');written.add(name);continue
        out.append(line)
    for name,value in updates.items():
        if name not in written:out.append(f'{name}={value}')
    ENV.write_text('\n'.join(out)+'\n')
    try:ENV.chmod(0o600)
    except OSError:pass
    print('AirScout environment is ready.')
    print(f"WAHA image selected for {platform.machine()}: {updates.get('WAHA_IMAGE',current_image)}")
    if updates.get('WAHA_API_KEY'):print('Generated a private WAHA API key and saved it in .env.')
    print('The initializer preserves existing non-placeholder values.')

if __name__=='__main__':main()
