import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.geteuid() == 0, 'bootstrap intentionally refuses root')
class BootstrapTests(unittest.TestCase):
    def test_help_needs_no_git_rust_or_opendeck(self):
        result = subprocess.run(['/bin/bash', str(ROOT / 'setup.sh'), '--help'],
                                env={'PATH': '/nonexistent', 'HOME': '/tmp'}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--install-udev', result.stdout)

    def run_download_mode(self, failing=False):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bindir = base / 'bin'; bindir.mkdir()
            fixture = base / 'fixture/scripts'; fixture.mkdir(parents=True)
            (fixture / 'setup.py').write_text('import sys\nprint("SETUP_FIXTURE", sys.argv[1:])\n' + ('sys.exit(7)\n' if failing else ''))
            git = bindir / 'git'
            git.write_text('''#!/bin/bash
set -eu
if [[ $1 == clone ]]; then
  for arg in "$@"; do dest=$arg; done
  mkdir -p "$dest"
  cp -R "$SETUP_TEST_FIXTURE"/. "$dest"/
else
  printf '%s\\n' 0000000000000000000000000000000000000000
fi
''')
            git.chmod(0o755)
            env = {**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH'],
                   'TMPDIR': str(base), 'SETUP_TEST_FIXTURE': str(fixture.parent), 'HOME': str(base)}
            # /dev/fd source simulates bash <(curl ...) without any network access.
            result = subprocess.run(['/bin/bash', '-c', 'bash <(cat "$1") --yes --no-style', '_', str(ROOT / 'setup.sh')],
                                    env=env, capture_output=True, text=True)
            self.assertIn("SETUP_FIXTURE ['--yes', '--no-style']", result.stdout)
            self.assertEqual(result.returncode, 7 if failing else 0, result.stderr)
            self.assertEqual(list(base.glob('opendeck-ai-usage.*')), [])

    def test_remote_bootstrap_forwards_flags_and_cleans_clone(self):
        self.run_download_mode()

    def test_remote_bootstrap_propagates_failure_and_cleans_clone(self):
        self.run_download_mode(failing=True)


if __name__ == '__main__':
    unittest.main()
