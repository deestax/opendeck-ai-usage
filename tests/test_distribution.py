import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install
import package


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.plugins = self.base / 'config/plugins'
        self.root = self.base / 'source'
        for name, (_, assets) in package.PLUGINS.items():
            for filename in assets:
                path = self.root / name / 'assets' / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}' if filename.endswith('.json') else 'fixture')
        for name in [*package.PLUGINS, 'opendeck-style']:
            path = self.root / 'target/release' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(install, 'ROOT', self.root))
        self.stack.enter_context(patch.object(package, 'ROOT', self.root))
        self.stack.enter_context(patch.dict(os.environ, {'XDG_STATE_HOME': str(self.base / 'state')}))
        self.run = self.stack.enter_context(patch.object(install.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def main(self, *args):
        install.main(['--plugins-dir', str(self.plugins), *args])

    def upstream(self):
        folder = self.plugins / install.WRAPPED[0]
        folder.mkdir(parents=True)
        binary = folder / 'upstream-binary'
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
        manifest = {'CodePathLin': binary.name, 'Version': 'fixture', 'Actions': []}
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        return folder, manifest

    def test_dry_run_writes_nothing(self):
        self.main('--dry-run')
        self.assertFalse(self.plugins.exists())
        self.assertFalse((self.base / 'state').exists())

    def test_refuses_running_opendeck(self):
        self.run.return_value.returncode = 0
        with self.assertRaises(SystemExit):
            self.main()
        self.assertFalse(self.plugins.exists())

    def test_install_preserves_profiles_and_requires_explicit_replace(self):
        profile = self.plugins.parent / 'profiles/private.json'
        profile.parent.mkdir(parents=True)
        profile.write_text('private fixture, not to be changed')
        self.main()
        self.assertEqual(profile.read_text(), 'private fixture, not to be changed')
        for name, (plugin, assets) in package.PLUGINS.items():
            self.assertTrue(os.access(self.plugins / plugin / (name + '-linux'), os.X_OK))
            self.assertTrue(all((self.plugins / plugin / asset).exists() for asset in assets))
        with self.assertRaises(SystemExit):
            self.main()
        existing = self.plugins / 'local.superdao.claudeusage.sdPlugin/icon.svg'
        existing.write_text('old icon')
        self.main('--replace')
        backups = list((self.base / 'state/opendeck-ai-usage').iterdir())
        self.assertTrue(any((b / 'local.superdao.claudeusage.sdPlugin/icon.svg').exists()
                            and (b / 'local.superdao.claudeusage.sdPlugin/icon.svg').read_text() == 'old icon'
                            for b in backups))
        self.assertTrue(all(b.stat().st_mode & 0o777 == 0o700 for b in backups))

    def test_optional_style_keeps_vendor_binary_and_settings(self):
        folder, before = self.upstream()
        original = (folder / 'upstream-binary').read_bytes()
        (folder / 'private-settings.json').write_text('fixture settings')
        self.main('--style')
        self.assertEqual((folder / 'upstream-binary').read_bytes(), original)
        self.assertEqual((folder / 'private-settings.json').read_text(), 'fixture settings')
        after = json.loads((folder / 'manifest.json').read_text())
        self.assertEqual(after['Actions'], before['Actions'])
        self.assertEqual(after['CodePathLin'], 'ai-usage-style.sh')
        # Actually run the shell wrapper with fixture executables; no provider calls.
        self.run.assert_called_once()
        process = subprocess.Popen([str(folder / 'ai-usage-style.sh'), '-port', '1'])
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertNotIn(str(Path.home()), (folder / 'ai-usage-style.sh').read_text())
        self.main('--replace', '--style')
        self.assertEqual(json.loads((folder / 'ai-usage-style.json').read_text()), {'original': 'upstream-binary'})
        self.assertEqual((folder / 'upstream-binary').read_bytes(), original)
        (folder / 'ai-usage-style.json').unlink()
        with self.assertRaises(SystemExit):
            self.main('--replace', '--style')

    def test_style_missing_dependency_fails_before_writes(self):
        with self.assertRaises(SystemExit):
            self.main('--style')
        self.assertFalse(self.plugins.exists())

    def test_package_allowlist_ignores_unrelated_private_files(self):
        out = self.base / 'dist'
        folder = out / 'local.superdao.claudeusage.sdPlugin'
        folder.mkdir(parents=True)
        (folder / '.credentials.json').write_text('must not be packaged')
        package.package(out)
        for name, (plugin, assets) in package.PLUGINS.items():
            with zipfile.ZipFile(out / (plugin + '.zip')) as archive:
                self.assertEqual(set(archive.namelist()), {plugin + '/' + f for f in [*assets, name + '-linux']})
                mode = archive.getinfo(plugin + '/' + name + '-linux').external_attr >> 16
                self.assertTrue(mode & 0o111)


if __name__ == '__main__':
    unittest.main()
