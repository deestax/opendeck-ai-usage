#!/usr/bin/env python3
"""Guided source build + verified third-party plugin installation. Python stdlib only."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile

import install
from package import PLUGINS, ROOT

ARCH = 'x86_64-unknown-linux-gnu'
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
RULES = Path('/etc/udev/rules.d/40-opendeck-akp05.rules')


def confirm(question):
    try:
        with open('/dev/tty', 'r+') as tty:
            tty.write(question + ' [y/N] ')
            tty.flush()
            return tty.readline().strip().lower() in ('y', 'yes')
    except OSError:
        raise RuntimeError('No interactive terminal. Review --dry-run, then use --yes (and --replace if needed).') from None


def stopped():
    result = subprocess.run(['pgrep', '-x', 'opendeck'], stdout=subprocess.DEVNULL, check=False)
    if result.returncode != 1:
        raise RuntimeError('Quit OpenDeck from its tray menu first. Setup will not stop it or edit a running profile.')


def prerequisites():
    if os.geteuid() == 0:
        raise RuntimeError('Run as your desktop user, NOT with sudo.')
    if sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Guided setup supports native x86-64 Linux only; see README for manual builds.')
    if sys.version_info < (3, 11):
        raise RuntimeError('Python 3.11 or newer is required.')
    if os.environ.get('FLATPAK_ID') or Path('/.flatpak-info').exists():
        raise RuntimeError('Flatpak/sandboxed setup is not supported; use native OpenDeck.')
    missing = [name for name in ('git', 'cargo', 'rustc', 'cc', 'pgrep', 'opendeck') if not shutil.which(name)]
    if missing:
        raise RuntimeError('Missing prerequisites: ' + ', '.join(missing) + '\n'
            'Install native OpenDeck >=2.5: https://github.com/nekename/OpenDeck#installation\n'
            'Install Rust stable: https://rustup.rs/\n'
            'Build tools: Debian/Ubuntu: sudo apt install git build-essential python3 procps\n'
            'Fedora: sudo dnf install git gcc python3 procps-ng\n'
            'Arch: sudo pacman -S --needed git base-devel python procps-ng\n'
            'Omarchy: omarchy pkg add git base-devel python procps-ng\n'
            'Setup does not install OS packages or run remote system installers automatically.')


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith('https://'):
            raise RuntimeError('Refusing a non-HTTPS download redirect.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(spec, path):
    if not spec['url'].startswith('https://'):
        raise RuntimeError('Downloads must use HTTPS.')
    digest = hashlib.sha256()
    total = 0
    req = urllib.request.Request(spec['url'], headers={'User-Agent': 'opendeck-ai-usage-setup'})
    with urllib.request.build_opener(HTTPSOnly()).open(req, timeout=60) as response, path.open('wb') as out:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise RuntimeError('Download exceeds size limit.')
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != spec['sha256']:
        raise RuntimeError('Checksum mismatch; refusing to install ' + spec['url'])


def unpack_plugin(archive, dest, folder):
    """Never trust zip paths/types, even for a checksum-pinned archive."""
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise RuntimeError('Plugin archive exceeds expansion limit.')
        seen = set()
        for info in infos:
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if (path.is_absolute() or '..' in path.parts or '\\' in info.filename
                    or not path.parts or path.parts[0] != folder
                    or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or str(path) in seen):
                raise RuntimeError('Unsafe or duplicate archive member: ' + info.filename)
            seen.add(str(path))
        for info in infos:
            target = dest / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as source, target.open('xb') as out:
                    shutil.copyfileobj(source, out)
                target.chmod(0o755 if (info.external_attr >> 16) & 0o111 else 0o644)
    # Some upstream ZIPs lose Unix executable bits. Restore only the validated
    # Linux entrypoint after extraction/checksum verification, not arbitrary files.
    validate_plugin(dest / folder, make_executable=True)


def reject_symlinks(folder):
    if folder.is_symlink() or any(path.is_symlink() for path in folder.rglob('*')):
        raise RuntimeError('Refusing symlinks in a plugin being modified: ' + folder.name)


def validate_plugin(folder, make_executable=False):
    if folder.is_symlink():
        raise RuntimeError(f'Refusing symlinked plugin directory: {folder.name}')
    manifest = json.loads((folder / 'manifest.json').read_text())
    binary = manifest.get('CodePaths', {}).get(ARCH) or manifest.get('CodePathLin')
    if (not isinstance(binary, str) or not binary or Path(binary).is_absolute()
            or '..' in Path(binary).parts):
        raise RuntimeError(f'Invalid Linux executable in {folder.name}.')
    path = folder / binary
    if make_executable and path.is_file() and not path.is_symlink():
        path.chmod(0o755)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f'Missing/non-executable Linux binary in {folder.name}; repair it before setup.')
    return manifest


def commit_plugins(stage, plugins, names, state):
    """Precopy everything, then swap folders; restore all old folders on failure."""
    stopped()  # Recheck after potentially lengthy build/downloads.
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    backup = state / ('setup-' + stamp)
    backup.mkdir(mode=0o700, parents=True)
    for name in names:
        folder = plugins / name
        if folder.exists() or folder.is_symlink():
            reject_symlinks(folder)
            shutil.copytree(folder, backup / name, symlinks=True)
    (backup / 'receipt.json').write_text(json.dumps({'plugins': str(plugins), 'affected': names}, indent=2) + '\n')
    print('Private backup (do not share):', backup, flush=True)
    plugins.mkdir(parents=True, exist_ok=True)
    transaction = Path(tempfile.mkdtemp(prefix='.ai-usage-setup-', dir=plugins))
    changed = []
    try:
        for name in names:
            shutil.copytree(stage / name, transaction / 'new' / name, symlinks=True)
        (transaction / 'old').mkdir()
        for name in names:
            dest = plugins / name
            old = transaction / 'old' / name
            if dest.exists():
                dest.rename(old)
            changed.append(name)
            (transaction / 'new' / name).rename(dest)
    except BaseException:
        try:
            for name in reversed(changed):
                dest, old = plugins / name, transaction / 'old' / name
                if dest.exists():
                    shutil.rmtree(dest)
                if old.exists():
                    old.rename(dest)
        except BaseException:
            print('Rollback incomplete! Recovery copies:', transaction, 'and', backup, file=sys.stderr)
            raise
        print('Plugin installation rolled back. Backup:', backup, file=sys.stderr)
        # Keep transaction for inspection on failure; never discard recovery data.
        raise
    else:
        shutil.rmtree(transaction)
    return backup


def install_rules(file, backup):
    """Called only with separate --install-udev authorization."""
    if RULES.exists():
        if hashlib.sha256(RULES.read_bytes()).digest() == hashlib.sha256(file.read_bytes()).digest():
            print('Ajazz udev rules already match; no privileged changes needed.')
            return
        shutil.copy2(RULES, backup / RULES.name)
    subprocess.run(['sudo', 'install', '-m', '0644', str(file), str(RULES)], check=True)
    subprocess.run(['sudo', 'udevadm', 'control', '--reload-rules'], check=True)
    print('Ajazz udev rules installed. Unplug/reconnect your deck.')


def next_steps():
    print('''
Plugin installation complete. Remaining personal setup:
  1. Restart OpenDeck; select Ajazz AKP05E and a profile named Default.
  2. Sign in yourself: claude auth login; codex login (ChatGPT subscription).
     Install missing CLIs from https://code.claude.com/docs/en/setup and
     https://github.com/openai/codex. Ensure they are on OpenDeck's PATH.
  3. Drag Claude > Usage and resets, Codex > Usage, OpenRouter > Credits onto keys.
  4. Enter YOUR OpenRouter management API key in the Credits property inspector.
     Do not share profile exports: they can contain that key.
Claude automatic token renewal is restricted to the reviewed CLI binary;
other builds require Claude Code to renew its own login. See docs/authentication.md.
Profiles/dials/logins were not changed. System/media actions remain optional.
''')


def main(argv=None):
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugins-dir', type=Path, default=config / 'opendeck/plugins')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--replace', action='store_true')
    parser.add_argument('--no-style', action='store_true')
    parser.add_argument('--install-udev', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    prerequisites()
    plugins = args.plugins_dir.expanduser().resolve()
    if '/.var/app/' in str(plugins):
        raise RuntimeError('Flatpak configuration is unsupported. Use native OpenDeck.')
    lock = json.loads((ROOT / 'scripts/upstreams.json').read_text())
    native = [folder for folder, _ in PLUGINS.values()]
    existing = [name for name in native if (plugins / name).exists()]
    missing = []
    for spec in lock['plugins']:
        folder = plugins / spec['folder']
        if folder.exists():
            validate_plugin(folder)
            print('Keep existing upstream:', spec['name'])
        else:
            missing.append(spec)
            print('Install verified upstream:', spec['name'], spec['version'])
    styled = []
    if not args.no_style:
        # Validate currently installed wrappers before any changes. A missing OR
        # plugin is planned from the verified download below.
        if (plugins / install.WRAPPED[0]).exists():
            styled = [folder.name for folder, _, _ in install.style_plan(plugins, ARCH)]
        else:
            styled = [name for name in install.WRAPPED if (plugins / name).exists()]
        print('Apply matching style on N4 Default profiles (original executables preserved).')
    print('Build/install: Claude + Codex from this checkout, cargo --locked; destination:', plugins)
    print('Existing Claude/Codex needing replacement consent:', ', '.join(existing) or 'none')
    print('Profiles, dials, CLI logins and upstream versions: preserved.')
    print('Ajazz udev:', 'install using sudo (explicitly requested)' if args.install_udev else 'no system changes; rules still required for hardware access')
    if args.dry_run:
        print('Dry run complete. No build, dependency download or installation performed.')
        return
    stopped()
    if existing and not args.replace:
        if args.yes or not confirm('Back up and replace the existing Claude/Codex plugins listed above?'):
            raise RuntimeError('Existing plugins kept. Rerun with --replace to authorize replacement.')
    if args.install_udev and not all(shutil.which(c) for c in ('sudo', 'udevadm')):
        raise RuntimeError('--install-udev requires sudo and udevadm.')
    if not args.yes and not confirm('Proceed with the installation plan above?'):
        print('Cancelled; nothing installed.')
        return
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'opendeck-ai-usage'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'setup.lock').open('w') as lockfile:
        try:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another guided setup is running.') from None
        with tempfile.TemporaryDirectory(prefix='opendeck-ai-usage-stage-') as workdir:
            work = Path(workdir)
            stage = work / 'plugins'
            stage.mkdir()
            for name in dict.fromkeys([*existing, *styled]):
                reject_symlinks(plugins / name)
                shutil.copytree(plugins / name, stage / name, symlinks=True)
            for spec in missing:
                archive = work / (spec['folder'] + '.zip')
                print('Downloading/checking:', spec['name'], flush=True)
                download(spec, archive)
                unpack_plugin(archive, stage, spec['folder'])
            # Existing OR is needed for --style even when unchanged.
            if not args.no_style:
                for name in install.WRAPPED:
                    if (plugins / name).exists() and not (stage / name).exists():
                        reject_symlinks(plugins / name)
                        shutil.copytree(plugins / name, stage / name, symlinks=True)
            rules = work / RULES.name
            if args.install_udev:
                download(lock['udev'], rules)
            print('Building usage plugins (first build may take several minutes)...', flush=True)
            env = {**os.environ, 'CARGO_TARGET_DIR': str(ROOT / 'target')}
            subprocess.run(['cargo', 'build', '--release', '--workspace', '--locked'], cwd=ROOT, env=env, check=True)
            command = [sys.executable, str(ROOT / 'scripts/install.py'), '--plugins-dir', str(stage), '--replace']
            if not args.no_style:
                command.append('--style')
            subprocess.run(command, env={**env, 'XDG_STATE_HOME': str(work / 'state')}, check=True)
            names = sorted(folder.name for folder in stage.iterdir())
            backup = commit_plugins(stage, plugins, names, state)
            if args.install_udev:
                try:
                    install_rules(rules, backup)
                except (OSError, subprocess.CalledProcessError) as error:
                    print('Plugins installed, but udev setup failed. See driver instructions and backup:', backup, file=sys.stderr)
                    raise RuntimeError('Finish udev setup before using the device.') from error
            else:
                print('If the deck is not detected, rerun with --install-udev or follow https://github.com/ambiso/opendeck-akp05#installation')
    next_steps()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print('Setup stopped:', error, file=sys.stderr)
        sys.exit(1)
