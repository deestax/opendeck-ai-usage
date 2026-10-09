#!/usr/bin/env python3
"""Package already-built Linux plugins. Explicit allowlist; never reads user config."""
from pathlib import Path
import argparse
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = {
    'opendeck-claude-usage': ('local.superdao.claudeusage.sdPlugin',
        ['manifest.json', 'icon.svg', 'fetch_usage.py', 'refresh_auth.py', 'auth-refresh.json']),
    'opendeck-codex-usage': ('local.superdao.codexusage.sdPlugin', ['manifest.json', 'icon.svg']),
}


def package(output):
    output = Path(output)
    sources = [ROOT / 'target/release' / name for name in PLUGINS]
    sources += [ROOT / name / 'assets' / f for name, (_, files) in PLUGINS.items() for f in files]
    if not all(p.is_file() for p in sources):
        raise SystemExit('Missing build/assets. Run cargo build --release --workspace first.')
    output.mkdir(parents=True, exist_ok=True)
    for name, (plugin, files) in PLUGINS.items():
        # Only zip allowlisted members, even if output contains older files.
        members = []
        folder = output / plugin
        folder.mkdir(exist_ok=True)
        binary = folder / (name + '-linux')
        shutil.copy2(ROOT / 'target/release' / name, binary)
        binary.chmod(0o755)
        members.append(binary)
        for filename in files:
            dest = folder / filename
            shutil.copy2(ROOT / name / 'assets' / filename, dest)
            members.append(dest)
        with zipfile.ZipFile(output / (plugin + '.zip'), 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in members:
                archive.write(path, plugin + '/' + path.name)
    print('Packaged Claude and Codex plugins in', output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    package(parser.parse_args().output)
