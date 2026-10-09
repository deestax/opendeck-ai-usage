import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import setup
import install
from package import PLUGINS


def archive(path, folder):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr(folder + '/manifest.json', json.dumps({'CodePathLin': 'binary', 'Version': 'fixture'}))
        info = zipfile.ZipInfo(folder + '/binary')
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        z.writestr(info, '#!/bin/sh\nexit 0\n')


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def test_verified_download_and_bad_checksum(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        spec = {'url': 'https://example.test/plugin', 'sha256': hashlib.sha256(b'fixture').hexdigest()}
        opener = Mock()
        opener.open.return_value = response
        with patch.object(setup.urllib.request, 'build_opener', return_value=opener):
            response.read.side_effect = [b'fixture', b'']
            setup.download(spec, self.base / 'ok')
            response.read.side_effect = [b'tampered', b'']
            with self.assertRaisesRegex(RuntimeError, 'Checksum'):
                setup.download(spec, self.base / 'bad')
            with self.assertRaisesRegex(RuntimeError, 'HTTPS'):
                setup.download({**spec, 'url': 'http://example.test/plugin'}, self.base / 'bad')

    def test_refuses_insecure_redirect(self):
        with self.assertRaisesRegex(RuntimeError, 'non-HTTPS'):
            setup.HTTPSOnly().redirect_request(None, None, 302, '', {}, 'http://example.test/')

    def test_archive_permissions_and_manifest(self):
        path = self.base / 'plugin.zip'
        archive(path, 'example.sdPlugin')
        setup.unpack_plugin(path, self.base / 'extracted', 'example.sdPlugin')
        self.assertTrue(os.access(self.base / 'extracted/example.sdPlugin/binary', os.X_OK))

    def test_archive_repairs_missing_executable_bit_only_on_entrypoint(self):
        path = self.base / 'plugin.zip'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('example.sdPlugin/manifest.json', json.dumps({'CodePathLin': 'binary'}))
            z.writestr('example.sdPlugin/binary', '#!/bin/sh\nexit 0\n')
            z.writestr('example.sdPlugin/pi.html', 'inspector')
        dest = self.base / 'extracted'
        setup.unpack_plugin(path, dest, 'example.sdPlugin')
        self.assertTrue(os.access(dest / 'example.sdPlugin/binary', os.X_OK))
        self.assertFalse(os.access(dest / 'example.sdPlugin/pi.html', os.X_OK))

    def test_rejects_traversal_symlinks_wrong_roots_and_duplicates(self):
        for kind in ('traversal', 'absolute', 'backslash', 'symlink', 'other', 'duplicate'):
            with self.subTest(kind=kind):
                path = self.base / (kind + '.zip')
                names = {'traversal': 'example.sdPlugin/../../escape', 'absolute': '/escape',
                         'backslash': 'example.sdPlugin/..\\escape', 'symlink': 'example.sdPlugin/link',
                         'other': 'another.sdPlugin/file', 'duplicate': 'example.sdPlugin/file'}
                with zipfile.ZipFile(path, 'w') as z:
                    info = zipfile.ZipInfo(names[kind])
                    if kind == 'symlink':
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(info, '/etc/passwd')
                    if kind == 'duplicate':
                        import warnings
                        with warnings.catch_warnings():
                            warnings.simplefilter('ignore')
                            z.writestr(info, 'duplicate')
                with self.assertRaises(RuntimeError):
                    setup.unpack_plugin(path, self.base / kind, 'example.sdPlugin')
                self.assertFalse((self.base / kind).exists())

    def test_bounds_archive_expansion_and_download(self):
        path = self.base / 'plugin.zip'
        archive(path, 'example.sdPlugin')
        with patch.object(setup, 'MAX_EXPANDED', 1):
            with self.assertRaisesRegex(RuntimeError, 'expansion'):
                setup.unpack_plugin(path, self.base / 'extract', 'example.sdPlugin')
        response = io.BytesIO(b'oversized')
        with patch.object(setup, 'MAX_DOWNLOAD', 1), patch.object(setup.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value = response
            with self.assertRaisesRegex(RuntimeError, 'size limit'):
                setup.download({'url': 'https://example.test/file', 'sha256': ''}, self.base / 'download')

    def test_no_root_or_wrong_arch(self):
        with patch.object(setup.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(RuntimeError, 'NOT with sudo'):
                setup.prerequisites()
        with patch.object(setup.os, 'geteuid', return_value=1000), patch.object(setup.platform, 'machine', return_value='aarch64'):
            with self.assertRaisesRegex(RuntimeError, 'x86-64'):
                setup.prerequisites()

    def test_running_opendeck_is_never_killed(self):
        with patch.object(setup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            with self.assertRaisesRegex(RuntimeError, 'Quit OpenDeck'):
                setup.stopped()
        self.assertEqual(run.call_args.args[0], ['pgrep', '-x', 'opendeck'])

    def test_transaction_backup_preserves_profiles(self):
        stage, plugins, state = [self.base / x for x in ('stage', 'plugins', 'state')]
        for parent, data in [(stage, 'new'), (plugins, 'old')]:
            (parent / 'one.sdPlugin').mkdir(parents=True)
            (parent / 'one.sdPlugin/file').write_text(data)
        profile = self.base / 'profiles/private.json'
        profile.parent.mkdir()
        profile.write_text('unchanged')
        with patch.object(setup, 'stopped'):
            backup = setup.commit_plugins(stage, plugins, ['one.sdPlugin'], state)
        self.assertEqual((plugins / 'one.sdPlugin/file').read_text(), 'new')
        self.assertEqual((backup / 'one.sdPlugin/file').read_text(), 'old')
        self.assertEqual(profile.read_text(), 'unchanged')
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)

    def test_transaction_rolls_back_all_folders_after_failed_swap(self):
        stage, plugins = self.base / 'stage', self.base / 'plugins'
        for name in ('one', 'two'):
            for root, data in [(stage, 'new'), (plugins, 'old')]:
                (root / name).mkdir(parents=True)
                (root / name / 'file').write_text(data + name)
        rename = Path.rename
        def fail_second(path, target):
            if path.name == 'two' and path.parent.name == 'new':
                raise OSError('simulated failed swap')
            return rename(path, target)
        with patch.object(setup, 'stopped'), patch.object(Path, 'rename', fail_second):
            with self.assertRaisesRegex(OSError, 'simulated'):
                setup.commit_plugins(stage, plugins, ['one', 'two'], self.base / 'state')
        for name in ('one', 'two'):
            self.assertEqual((plugins / name / 'file').read_text(), 'old' + name)

    def test_symlinks_in_modified_plugin_are_refused(self):
        folder = self.base / 'plugin'
        folder.mkdir()
        (folder / 'helper').symlink_to('/some/outside/file')
        with self.assertRaisesRegex(RuntimeError, 'symlinks'):
            setup.reject_symlinks(folder)

    def test_rules_no_sudo_when_already_matching(self):
        rules = self.base / 'installed.rules'
        source = self.base / 'source.rules'
        rules.write_text('fixture'); source.write_text('fixture')
        with patch.object(setup, 'RULES', rules), patch.object(setup.subprocess, 'run') as run:
            setup.install_rules(source, self.base)
        run.assert_not_called()

    def test_rules_backup_and_exact_commands(self):
        rules = self.base / 'system/40-opendeck-akp05.rules'
        rules.parent.mkdir()
        rules.write_text('old rules')
        source = self.base / 'new.rules'; source.write_text('new rules')
        backup = self.base / 'backup'; backup.mkdir()
        with patch.object(setup, 'RULES', rules), patch.object(setup.subprocess, 'run') as run:
            setup.install_rules(source, backup)
        self.assertEqual((backup / rules.name).read_text(), 'old rules')
        self.assertEqual(run.call_args_list[0].args[0], ['sudo', 'install', '-m', '0644', str(source), str(rules)])
        self.assertEqual(run.call_args_list[1].args[0], ['sudo', 'udevadm', 'control', '--reload-rules'])


class SetupFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'source'
        self.plugins = self.base / 'config/plugins'
        (self.root / 'scripts').mkdir(parents=True)
        self.specs = [dict(name=name, folder=name, version='fixture', url='https://example.test/' + name, sha256='fixture')
                      for name in ('com.github.ambiso.opendeck-akp05.sdPlugin', install.WRAPPED[0])]
        (self.root / 'scripts/upstreams.json').write_text(json.dumps({'plugins': self.specs, 'udev': {}}))
        for name, (_, assets) in PLUGINS.items():
            for filename in assets:
                file = self.root / name / 'assets' / filename
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('{}' if filename.endswith('.json') else 'fixture')
        for name in [*PLUGINS, 'opendeck-style']:
            file = self.root / 'target/release' / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('#!/bin/sh\nexit 0\n'); file.chmod(0o755)
        self.stack = contextlib.ExitStack()
        for mod in (setup, install):
            self.stack.enter_context(patch.object(mod, 'ROOT', self.root))
        self.stack.enter_context(patch.object(setup, 'prerequisites'))
        self.stack.enter_context(patch.dict(os.environ, {'XDG_STATE_HOME': str(self.base / 'state')}))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.download = self.stack.enter_context(patch.object(setup, 'download', side_effect=lambda spec, path: archive(path, spec['folder'])))
        self.rules = self.stack.enter_context(patch.object(setup, 'install_rules'))
        self.run = self.stack.enter_context(patch.object(setup.subprocess, 'run', side_effect=self.fake_run))
        self.confirm = self.stack.enter_context(patch.object(setup, 'confirm', return_value=True))

    def tearDown(self):
        self.stack.close(); self.tmp.cleanup()

    def fake_run(self, command, **kwargs):
        if command[0] == 'pgrep':
            return subprocess.CompletedProcess(command, 1)
        if command[0] == 'cargo':
            return subprocess.CompletedProcess(command, 0)
        if command[0] == sys.executable:
            with patch.dict(os.environ, kwargs['env']):
                install.main(command[2:])
            return subprocess.CompletedProcess(command, 0)
        raise AssertionError(command)

    def main(self, *args):
        setup.main(['--plugins-dir', str(self.plugins), *args])

    def test_dry_run_no_network_build_or_writes(self):
        self.main('--dry-run')
        self.download.assert_not_called(); self.run.assert_not_called()
        self.assertFalse(self.plugins.exists()); self.assertFalse((self.base / 'state').exists())

    def test_fresh_install_then_rerun_keeps_upstreams_and_profiles(self):
        profile = self.plugins.parent / 'profiles/Default.json'
        profile.parent.mkdir(parents=True); profile.write_text('private untouched')
        self.main('--yes')
        self.assertEqual(self.download.call_count, 2)
        self.rules.assert_not_called(); self.confirm.assert_not_called()
        self.assertTrue(all((self.plugins / f).is_dir() for f, _ in PLUGINS.values()))
        vendor = self.plugins / install.WRAPPED[0] / 'binary'
        original = vendor.read_bytes()
        self.main('--yes', '--replace')
        self.assertEqual(self.download.call_count, 2)  # no redownload or upstream downgrade
        self.assertEqual(vendor.read_bytes(), original)
        self.assertEqual(profile.read_text(), 'private untouched')
        self.assertEqual(json.loads((vendor.parent / 'ai-usage-style.json').read_text())['original'], 'binary')

    def test_yes_does_not_authorize_replacement(self):
        self.main('--yes')
        self.download.reset_mock()
        with self.assertRaisesRegex(RuntimeError, '--replace'):
            self.main('--yes')
        self.download.assert_not_called(); self.confirm.assert_not_called()

    def test_build_failure_leaves_actual_plugins_untouched(self):
        self.run.side_effect = lambda cmd, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, cmd)) if cmd[0] == 'cargo' else self.fake_run(cmd, **kw)
        with self.assertRaises(subprocess.CalledProcessError):
            self.main('--yes')
        self.assertFalse(self.plugins.exists())

    def test_no_style_and_decline(self):
        self.confirm.return_value = False
        self.main()
        self.assertFalse(self.plugins.exists())
        self.download.assert_not_called()
        self.main('--yes', '--no-style')
        folder = self.plugins / install.WRAPPED[0]
        self.assertEqual(json.loads((folder / 'manifest.json').read_text())['CodePathLin'], 'binary')
        self.assertFalse((folder / 'ai-usage-style.sh').exists())


if __name__ == '__main__':
    unittest.main()
