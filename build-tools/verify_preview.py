"""Fail closed on preview staging/bundle path boundaries (not a secret scanner)."""
import argparse
from pathlib import Path

RUNTIME_DIRS = {'config', 'db', 'downloads', 'logs', 'cookies', 'temp', 'acfunid'}
DEV_DIRS = {'.venv', 'venv', '.git', '.ssh', '__pycache__', '.pytest_cache'}
PRIVATE_NAMES = {'credentials.json', 'client_secret.json', 'cookies.txt', 'token.json',
                 'id_rsa', 'id_ed25519', 'pyvenv.cfg'}
REQUIRED = ('Y2A-Auto.exe', 'start.bat', 'README.txt', 'LICENSE',
            'ffmpeg/ffmpeg.exe', 'ffmpeg/ffprobe.exe',
            'ffmpeg/FFMPEG_GPLv3.txt', 'ffmpeg/FFMPEG_README.txt')
SOURCE_ROOTS = {'app.py', 'modules', 'templates', 'static', 'fonts', 'build-tools',
                'requirements.txt', 'LICENSE', 'ffmpeg'}


def verify(root: Path, source: bool = False):
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f'Missing directory: {root}')
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        parts = tuple(part.lower() for part in relative.parts)
        if path.is_symlink():
            raise ValueError(f'Forbidden symlink: {relative}')
        if source and relative.parts[0] not in SOURCE_ROOTS:
            raise ValueError(f'Forbidden source root: {relative}')
        # Test files may create empty runtime dirs; never ship their contents.
        app_parts = parts[1:] if parts[0] == '_internal' else parts
        forbidden = any(part in DEV_DIRS for part in parts)
        forbidden |= parts[-1] in PRIVATE_NAMES or parts[-1].startswith('.env')
        public_ca = parts == ('_internal', 'certifi', 'cacert.pem') and not source
        forbidden |= path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.key'}
        forbidden |= path.suffix.lower() == '.pem' and not public_ca
        forbidden |= len(app_parts) > 1 and app_parts[0] in RUNTIME_DIRS
        if forbidden and path.is_file():
            raise ValueError(f'Forbidden package path: {relative}')
    if not source:
        for name in REQUIRED:
            path = root / name
            if not path.is_file() or not path.stat().st_size:
                raise ValueError(f'Missing or empty required file: {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='store_true')
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    verify(args.directory, source=args.source)
    print('Preview path boundary verification passed')


if __name__ == '__main__':
    main()
