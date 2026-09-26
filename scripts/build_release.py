"""Create a reproducible integration ZIP, deployment TAR and file manifest."""
from pathlib import Path
import hashlib
import io
import json
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'custom_components' / 'phantom_chess'
VERSION = json.loads((SOURCE / 'manifest.json').read_text())['version']
OUTPUT = ROOT.parents[1] / 'Outputs' / 'home-assistant' / f'phantom-chess-{VERSION}'
OUTPUT.mkdir(parents=True, exist_ok=True)
FILES = sorted(p for p in SOURCE.rglob('*') if p.is_file() and '__pycache__' not in p.parts
               and '.bak' not in p.name and p.suffix in {'.py', '.json', '.yaml', '.js', '.html', '.png', '.jpg', '.svg', '.css'})
manifest = []
with zipfile.ZipFile(OUTPUT / f'phantom-chess-{VERSION}.zip', 'w', zipfile.ZIP_DEFLATED) as archive, tarfile.open(OUTPUT / 'deployment.tar', 'w') as deployment:
    for p in FILES:
        data = p.read_bytes()
        name = str(p.relative_to(ROOT))
        item = zipfile.ZipInfo(name, date_time=(2026, 9, 5, 0, 0, 0))
        item.compress_type = zipfile.ZIP_DEFLATED
        item.external_attr = 0o100644 << 16
        archive.writestr(item, data)
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = len(data), 0o644, 0
        deployment.addfile(info, io.BytesIO(data))
        manifest.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
(OUTPUT / 'files.json').write_text(json.dumps(manifest, indent=2) + '\n')
archive = OUTPUT / f'phantom-chess-{VERSION}.zip'
(OUTPUT / 'SHA256SUMS').write_text(f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n')
print(OUTPUT)
print(f'{len(FILES)} files; {archive.stat().st_size} bytes')
