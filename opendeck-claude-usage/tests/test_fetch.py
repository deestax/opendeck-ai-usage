import importlib.util
import io
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).parents[1] / 'assets'))
import refresh_auth as auth
import fetch_usage as module


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name)
        self.credentials = self.config / '.credentials.json'
        self.credentials.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'test-secret'}}))
        self.credentials.chmod(0o600)
        self.env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': self.tmp.name,
            'CLAUDE_SECURESTORAGE_CONFIG_DIR': self.tmp.name, 'XDG_STATE_HOME': str(self.config / 'state')})
        self.env.start()
        self.protocol = patch.object(auth, 'verify_protocol')
        self.protocol.start()

    def tearDown(self):
        self.protocol.stop()
        self.env.stop()
        self.tmp.cleanup()


class FetchTests(Fixture):
    def test_only_usage_output_and_correct_host(self):
        response = io.BytesIO(json.dumps({'five_hour': {'utilization': 3}, 'ignored': 'anything'}).encode())
        opener = Mock()
        opener.open.return_value = response
        with patch.object(module.urllib.request, 'build_opener', return_value=opener):
            result = module.fetch()
        self.assertEqual(result, {'data': {'five_hour': {'utilization': 3}}})
        self.assertNotIn('test-secret', json.dumps(result))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.anthropic.com/api/oauth/usage')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-secret')

    def test_rate_limit_backoff_and_no_response_body(self):
        error = urllib.error.HTTPError('url', 429, 'test-secret', {'Retry-After': '1200'}, io.BytesIO(b'test-secret'))
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(module.urllib.request, 'build_opener', return_value=opener):
            result = module.fetch()
        self.assertEqual(result, {'error': 'Rate limited', 'rate_limited': True, 'retry_after': 1200})
        self.assertNotIn('test-secret', json.dumps(result))

    def test_auth_errors_and_missing_credentials(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('url', 401, 'test-secret', {}, io.BytesIO())
        with patch.object(module.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(module.fetch(), {'error': 'Sign in to Claude Code'})
        self.credentials.unlink()
        self.assertEqual(module.fetch(), {'error': 'Sign in to Claude Code'})

    def test_expired_without_refresh_does_not_call_api(self):
        self.credentials.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'test-secret', 'expiresAt': 1}}))
        with patch.object(module.urllib.request, 'build_opener') as build:
            self.assertEqual(module.fetch(), {'error': 'Sign in to Claude Code'})
            build.assert_not_called()

    def test_no_redirect(self):
        self.assertIsNone(module.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_401_gets_one_renewal_not_a_loop(self):
        oauth = {'accessToken': 'renewed-secret'}
        with patch.object(module, 'renew', side_effect=[({'accessToken': 'test-secret'}, False), (oauth, True)]) as renew:
            with patch.object(module, 'request_usage', return_value={'unauthorized': True}) as get:
                self.assertEqual(module.fetch(), {'error': 'Claude authorization rejected', 'auth_refreshed': True})
        self.assertEqual(renew.call_count, 2)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(renew.call_args.kwargs['force'])

    def test_no_second_renewal_after_proactive_refresh(self):
        with patch.object(module, 'renew', return_value=({'accessToken': 'new'}, True)) as renew:
            with patch.object(module, 'request_usage', return_value={'unauthorized': True}):
                self.assertEqual(module.fetch()['error'], 'Claude authorization rejected')
        renew.assert_called_once()

    def test_oversized_response_rejected(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b' ' * 262145 + b'{}')
        with patch.object(module.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(module.fetch(), {'error': 'Invalid usage response'})


class RenewalTests(Fixture):
    def setUp(self):
        super().setUp()
        self.oauth = {'accessToken': 'old-access', 'refreshToken': 'old-refresh', 'expiresAt': 1,
                      'scopes': ['user:profile', 'user:inference'], 'subscriptionType': 'max',
                      'rateLimitTier': 'existing-tier', 'unrelated': 'keep'}
        self.doc = {'claudeAiOauth': self.oauth, 'mcpOAuth': {'server': 'keep'}}
        self.credentials.write_text(json.dumps(self.doc))
        self.result = {'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 28800,
                       'scope': 'user:profile user:inference'}

    def opener(self, result=None, side_effect=None):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(result or self.result).encode())
        if side_effect:
            opener.open.side_effect = side_effect
        return patch.object(auth.urllib.request, 'build_opener', return_value=opener), opener

    def test_atomic_renewal_preserves_unrelated_fields_and_locks(self):
        def response(request, timeout):
            for path in [self.config / '.oauth_refresh.lock', Path(str(self.config) + '.lock'), self.config / '.storage-write.lock']:
                self.assertTrue(path.is_dir())
            self.assertEqual(request.full_url, auth.TOKEN_URL)
            self.assertEqual(timeout, 15)
            body = json.loads(request.data)
            self.assertEqual(body['scope'], 'user:profile user:inference')
            self.assertNotIn('expires_in', body)
            self.assertEqual(request.get_header('User-agent'), 'opendeck-claude-usage/0.4.0')
            return io.BytesIO(json.dumps(self.result).encode())
        mocked, opener = self.opener(side_effect=response)
        with mocked:
            updated, changed = auth.renew(self.config, self.oauth)
        self.assertTrue(changed)
        saved = json.loads(self.credentials.read_text())
        self.assertEqual(saved['mcpOAuth'], self.doc['mcpOAuth'])
        self.assertEqual(saved['claudeAiOauth']['unrelated'], 'keep')
        self.assertEqual(saved['claudeAiOauth']['refreshToken'], 'new-refresh')
        self.assertEqual(saved['claudeAiOauth']['rateLimitTier'], 'existing-tier')
        self.assertGreater(updated['expiresAt'], time.time() * 1000)
        self.assertEqual(self.credentials.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.config / '.oauth_refresh.lock').exists())
        self.assertFalse(Path(str(self.config) + '.lock').exists())
        self.assertFalse((self.config / '.storage-write.lock').exists())
        for path in auth.state_dir(self.config).glob('*.json'):
            self.assertNotIn('old-refresh', path.read_text())
            self.assertNotIn('new-refresh', path.read_text())

    def test_valid_token_does_not_refresh_or_check_cli(self):
        oauth = {**self.oauth, 'expiresAt': (time.time() + 3600) * 1000}
        with patch.object(auth, 'verify_protocol') as verify:
            with patch.object(auth.urllib.request, 'build_opener') as build:
                self.assertEqual(auth.renew(self.config, oauth), (oauth, False))
        verify.assert_not_called()
        build.assert_not_called()

    def test_another_process_renewed_before_lock(self):
        sibling = {**self.oauth, 'accessToken': 'sibling-access', 'expiresAt': (time.time() + 3600) * 1000}
        self.credentials.write_text(json.dumps({'claudeAiOauth': sibling}))
        with patch.object(auth.urllib.request, 'build_opener') as build:
            self.assertEqual(auth.renew(self.config, self.oauth), (sibling, False))
        build.assert_not_called()

    def test_never_overwrite_newer_login_during_request(self):
        sibling = {**self.oauth, 'accessToken': 'sibling-access', 'refreshToken': 'sibling-refresh'}
        def response(*args, **kwargs):
            self.credentials.write_text(json.dumps({'claudeAiOauth': sibling, 'other': 42}))
            return io.BytesIO(json.dumps(self.result).encode())
        mocked, _ = self.opener(side_effect=response)
        with mocked:
            self.assertEqual(auth.renew(self.config, self.oauth), (sibling, False))
        self.assertEqual(json.loads(self.credentials.read_text())['other'], 42)

    def test_all_three_native_locks_block_requests(self):
        for path in [self.config / '.oauth_refresh.lock', Path(str(self.config) + '.lock'), self.config / '.storage-write.lock']:
            with self.subTest(path=path):
                path.mkdir()
                with patch.object(auth.urllib.request, 'build_opener') as build:
                    with self.assertRaisesRegex(auth.AuthError, 'busy'):
                        auth.renew(self.config, self.oauth)
                build.assert_not_called()
                self.assertTrue(path.is_dir())
                path.rmdir()

    def test_invalid_grant_is_suppressed_until_credentials_change(self):
        error = urllib.error.HTTPError('url', 400, 'secret', {}, io.BytesIO(b'{"error":"invalid_grant"}'))
        mocked, opener = self.opener(side_effect=error)
        original = self.credentials.read_bytes()
        with mocked:
            for _ in range(2):
                with self.assertRaisesRegex(auth.AuthError, 'Sign in'):
                    auth.renew(self.config, self.oauth)
        opener.open.assert_called_once()
        self.assertEqual(original, self.credentials.read_bytes())
        self.credentials.write_text(json.dumps(self.doc) + ' ')
        mocked, _ = self.opener()
        with mocked:
            self.assertTrue(auth.renew(self.config, self.oauth)[1])

    def test_missing_rotated_token_preserves_existing_refresh(self):
        del self.result['refresh_token']
        mocked, _ = self.opener()
        with mocked:
            updated, _ = auth.renew(self.config, self.oauth)
        self.assertEqual(updated['refreshToken'], 'old-refresh')

    def test_network_error_keeps_credentials(self):
        original = self.credentials.read_bytes()
        mocked, _ = self.opener(side_effect=OSError('secret'))
        with mocked:
            with self.assertRaisesRegex(auth.AuthError, '^Token renewal unavailable$'):
                auth.renew(self.config, self.oauth)
        self.assertEqual(original, self.credentials.read_bytes())

    def test_rate_limit_respected(self):
        error = urllib.error.HTTPError('url', 429, 'secret', {'Retry-After': '3600'}, io.BytesIO())
        mocked, _ = self.opener(side_effect=error)
        with mocked:
            with self.assertRaises(auth.AuthError) as error:
                auth.renew(self.config, self.oauth)
        self.assertEqual(error.exception.result, {'error': 'Rate limited', 'rate_limited': True, 'retry_after': 3600})

    def test_bad_response_never_overwrites(self):
        original = self.credentials.read_bytes()
        for field, value in [('expires_in', None), ('expires_in', float('nan')), ('expires_in', True),
                             ('access_token', ''), ('refresh_token', ''), ('scope', 'user:profile')]:
            with self.subTest(field=field, value=value):
                self.credentials.write_bytes(original)
                mocked, _ = self.opener({**self.result, field: value})
                with mocked:
                    with self.assertRaisesRegex(auth.AuthError, 'Invalid renewal response'):
                        auth.renew(self.config, self.oauth)
                self.assertEqual(original, self.credentials.read_bytes())

    def test_compromised_lock_never_writes(self):
        original = self.credentials.read_bytes()
        def response(*args, **kwargs):
            (self.config / '.oauth_refresh.lock').rmdir()
            (self.config / '.oauth_refresh.lock').mkdir()
            return io.BytesIO(json.dumps(self.result).encode())
        mocked, _ = self.opener(side_effect=response)
        with mocked:
            with self.assertRaisesRegex(auth.AuthError, 'interrupted'):
                auth.renew(self.config, self.oauth)
        self.assertEqual(original, self.credentials.read_bytes())
        self.assertTrue((self.config / '.oauth_refresh.lock').is_dir())

    def test_credential_permissions_and_symlink_rejected(self):
        self.credentials.chmod(0o644)
        with self.assertRaisesRegex(auth.AuthError, 'permissions'):
            auth.read_credentials(self.config)
        self.credentials.rename(self.config / 'real')
        self.credentials.symlink_to(self.config / 'real')
        with self.assertRaises(auth.AuthError):
            auth.read_credentials(self.config)

    def test_protocol_guard_fails_closed(self):
        self.protocol.stop()
        with patch.object(auth.Path, 'read_text', return_value='{}'):
            with self.assertRaisesRegex(auth.AuthError, 'update needed'):
                auth.verify_protocol()

    def test_recovery_only_of_own_dead_lock(self):
        receipts = auth.state_dir(self.config)
        path = self.config / '.oauth_refresh.lock'
        lock = auth.DirectoryLock(path, receipts)
        path.mkdir()
        os.setxattr(path, 'user.opendeck-owner', b'test-marker')
        auth.atomic_json(lock.receipt, {'pid': 999999999, 'identity': ['old-boot', '0'],
                                       'inode': path.stat().st_ino, 'marker': 'test-marker'})
        lock.acquire()
        lock.check()
        lock.release()
        self.assertFalse(path.exists())

    def test_real_child_lock_heartbeat_and_crash_recovery(self):
        receipts = auth.state_dir(self.config)
        path = self.config / '.oauth_refresh.lock'
        context = multiprocessing.get_context('fork')
        ready = context.Event()
        def worker():
            lock = auth.DirectoryLock(path, receipts).acquire()
            ready.set()
            time.sleep(30)
            lock.release()
        child = context.Process(target=worker)
        child.start()
        try:
            self.assertTrue(ready.wait(5))
            first_mtime = path.stat().st_mtime_ns
            time.sleep(2.2)
            self.assertGreater(path.stat().st_mtime_ns, first_mtime)
            with self.assertRaisesRegex(auth.AuthError, 'busy'):
                auth.DirectoryLock(path, receipts).acquire()
        finally:
            child.kill()
            child.join(timeout=5)
        recovered = auth.DirectoryLock(path, receipts).acquire()
        recovered.check()
        recovered.release()
        self.assertFalse(path.exists())

    def test_failed_commit_blocks_replaying_spent_refresh(self):
        original = self.credentials.read_bytes()
        write = auth.atomic_json
        def fail_credentials(path, value):
            if path == self.credentials:
                raise OSError('disk full')
            return write(path, value)
        mocked, opener = self.opener()
        with mocked, patch.object(auth, 'atomic_json', side_effect=fail_credentials):
            with self.assertRaisesRegex(auth.AuthError, 'Could not save'):
                auth.renew(self.config, self.oauth)
            with self.assertRaisesRegex(auth.AuthError, 'Sign in'):
                auth.renew(self.config, self.oauth)
        opener.open.assert_called_once()
        self.assertEqual(self.credentials.read_bytes(), original)

    def test_live_owner_lock_never_stolen(self):
        receipts = auth.state_dir(self.config)
        path = self.config / '.oauth_refresh.lock'
        first = auth.DirectoryLock(path, receipts).acquire()
        try:
            with self.assertRaisesRegex(auth.AuthError, 'busy'):
                auth.DirectoryLock(path, receipts).acquire()
            first.check()
        finally:
            first.release()


class PortableProtocolTests(unittest.TestCase):
    def test_resolves_reviewed_binary_on_path(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / 'claude'
            binary.write_bytes(b'reviewed fixture binary')
            policy = {'protocol': 'claude-code-linux-2.1.289', 'claude_binary': 'claude',
                      'sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}
            with patch.object(auth.Path, 'read_text', return_value=json.dumps(policy)):
                with patch.object(auth.shutil, 'which', return_value=str(binary)) as which:
                    auth.verify_protocol()
                    which.assert_called_once_with('claude')
                    binary.write_bytes(b'upgraded unreviewed binary')
                    with self.assertRaisesRegex(auth.AuthError, 'update needed'):
                        auth.verify_protocol()

    def test_missing_cli_fails_closed(self):
        policy = {'protocol': 'claude-code-linux-2.1.289', 'claude_binary': 'claude', 'sha256': 'fixture'}
        with patch.object(auth.Path, 'read_text', return_value=json.dumps(policy)):
            with patch.object(auth.shutil, 'which', return_value=None):
                with self.assertRaisesRegex(auth.AuthError, 'update needed'):
                    auth.verify_protocol()


if __name__ == '__main__':
    unittest.main()
