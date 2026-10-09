#!/usr/bin/env python3
"""Install local Linux plugins, optionally style upstream plugins. Never edits profiles."""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import uuid

from package import PLUGINS, ROOT

WRAPPED = ('com.imdevinc.openroutercredits.sdPlugin',
           'me.amankhanna.oasystem.sdPlugin', 'me.amankhanna.oampris.sdPlugin')


def style_plan(plugins, arch):
    result = []
    for name in WRAPPED:
        folder = plugins / name
        path = folder / 'manifest.json'
        if not path.exists():
            if name == WRAPPED[0]:
                raise ValueError('Install the upstream OpenRouter Credits plugin before --style.')
            continue  # System Information / Linux Media are optional.
        manifest = json.loads(path.read_text())
        original = manifest.get('CodePaths', {}).get(arch) or manifest.get('CodePathLin')
        if original == 'ai-usage-style.sh':
            record_path = folder / 'ai-usage-style.json'
            if not record_path.is_file():
                raise ValueError(f'{name} has a legacy wrapper without provenance; restore its manifest before --style.')
            record = json.loads(record_path.read_text())
            original = record.get('original')
        if not isinstance(original, str) or Path(original).is_absolute() or '..' in Path(original).parts:
            raise ValueError(f'Invalid upstream executable path for {name}.')
        if original in ('ai-usage-style', 'ai-usage-style.sh'):
            raise ValueError(f'Recursive wrapper for {name}.')
        if not original or not (folder / original).is_file():
            raise ValueError(f'Missing upstream executable for {name} ({arch}).')
        if not os.access(folder / original, os.X_OK):
            raise ValueError(f'Upstream executable is not executable: {name}.')
        if (folder / original).suffix == '.sh':
            raise ValueError(f'{name} already uses a wrapper; refusing to chain wrappers.')
        result.append((folder, manifest, original))
    return result


def main(argv=None):
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugins-dir', type=Path, default=config / 'opendeck/plugins')
    parser.add_argument('--replace', action='store_true', help='back up and update existing Claude/Codex plugins')
    parser.add_argument('--style', action='store_true', help='wrap upstream OpenRouter/system/media display on N4 Default profiles')
    parser.add_argument('--dry-run', action='store_true', help='validate and print the plan without writing')
    args = parser.parse_args(argv)
    if sys.platform != 'linux':
        parser.error('Only native Linux is currently supported.')
    plugins = args.plugins_dir.expanduser().resolve()
    machines = {'x86_64': 'x86_64', 'aarch64': 'aarch64'}
    machine = machines.get(platform.machine())
    if not machine:
        parser.error('Unsupported architecture; supported source-build targets are x86_64 and aarch64 Linux.')
    arch = machine + '-unknown-linux-gnu'
    for name, (plugin, assets) in PLUGINS.items():
        if (plugins / plugin).exists() and not args.replace:
            parser.error(f'{plugin} exists; use --replace to back it up and update.')
        for source in [ROOT / 'target/release' / name, *(ROOT / name / 'assets' / f for f in assets)]:
            if not source.is_file():
                parser.error('Missing build/assets. Run cargo build --release --workspace first.')
    try:
        plan = style_plan(plugins, arch) if args.style else []
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if args.style and not (ROOT / 'target/release/opendeck-style').is_file():
        parser.error('Missing style binary. Run cargo build --release --workspace first.')
    for _, (plugin, _) in PLUGINS.items():
        print('Install:', plugins / plugin)
    for folder, _, _ in plan:
        print('Style:', folder.name, '(Default profile, all N4-family device IDs)')
    print('Existing profiles, key assignments and login files will NOT be changed by installation.')
    if args.dry_run:
        return
    try:
        running = subprocess.run(['pgrep', '-x', 'opendeck'], stdout=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        parser.error('pgrep is required to check that OpenDeck is stopped (install procps/procps-ng).')
    if running.returncode != 1:
        parser.error('Quit OpenDeck from its tray menu before installing (or check pgrep).')
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    backup = state / 'opendeck-ai-usage' / (datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    # All existing affected folders are backed up before the first mutation.
    affected = [plugins / plugin for plugin, _ in PLUGINS.values()] + [folder for folder, _, _ in plan]
    for folder in affected:
        if folder.exists():
            shutil.copytree(folder, backup / folder.name)
    print('Private backup (do not publish):', backup)
    for name, (plugin, assets) in PLUGINS.items():
        folder = plugins / plugin
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / 'target/release' / name, folder / (name + '-linux'))
        (folder / (name + '-linux')).chmod(0o755)
        for filename in assets:
            shutil.copy2(ROOT / name / 'assets' / filename, folder / filename)
    for folder, manifest, original in plan:
        # A private bridge copy per plugin keeps launch paths relocatable.
        shutil.copy2(ROOT / 'target/release/opendeck-style', folder / 'ai-usage-style')
        (folder / 'ai-usage-style').chmod(0o755)
        launcher = folder / 'ai-usage-style.sh'
        launcher.write_text('#!/bin/sh\nset -eu\ncd -- "$(dirname -- "$0")"\n'
                            'exec ./ai-usage-style "$PWD"/' + shlex.quote(original) + ' "$@"\n')
        launcher.chmod(0o755)
        (folder / 'ai-usage-style.json').write_text(json.dumps({'original': original}) + '\n')
        manifest['CodePathLin'] = launcher.name
        manifest.setdefault('CodePaths', {})[arch] = launcher.name
        (folder / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Installed. Restart OpenDeck and drag Claude / Codex actions onto your profile.')


if __name__ == '__main__':
    main()
