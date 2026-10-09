"""Bounded OAuth renewal, coordinated with Claude Code 2.1.289 (Linux).

No CLI prompts/model requests. No credentials in argv/logs/cache. The installed
CLI digest must match the reviewed protocol before any refresh is attempted.
"""
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

TOKEN_URL = 'https://platform.claude.com/v1/oauth/token'
CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e'
MAX_BYTES = 262144


class AuthError(Exception):
    def __init__(self, message, rate_limited=False, retry_after=900):
        super().__init__(message)
        self.result = {'error': message}
        if rate_limited:
            self.result.update(rate_limited=True, retry_after=retry_after)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def config_dir():
    # Match Claude Code's secure-storage override, including an empty override.
    value = os.environ.get('CLAUDE_SECURESTORAGE_CONFIG_DIR')
    if value is None:
        value = os.environ.get('CLAUDE_CONFIG_DIR')
    return Path(value or Path.home() / '.claude').resolve()


def state_dir(config):
    root = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    path = root / 'opendeck-claude-usage' / hashlib.sha256(str(config).encode()).hexdigest()[:16]
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def read_credentials(config):
    path = config / '.credentials.json'
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'r') as f:
            meta = os.fstat(f.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
                raise AuthError('Check Claude credential permissions')
            raw = f.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError()
        doc = json.loads(raw)
        oauth = doc.get('claudeAiOauth', doc)
        if not isinstance(oauth, dict) or not isinstance(oauth.get('accessToken'), str) or not oauth['accessToken']:
            raise ValueError()
        return doc, oauth, [meta.st_mtime_ns, meta.st_size, meta.st_ino]
    except AuthError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise AuthError('Sign in to Claude Code') from None


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def near_expiry(oauth):
    expiry = oauth.get('expiresAt')
    return isinstance(expiry, (int, float)) and expiry <= (time.time() + 300) * 1000


def verify_protocol():
    try:
        policy = json.loads(Path(__file__).with_name('auth-refresh.json').read_text())
        if policy.get('protocol') != 'claude-code-linux-2.1.289':
            raise ValueError()
        binary = shutil.which(policy['claude_binary'])
        if not binary:
            raise ValueError()
        with open(binary, 'rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        if digest != policy['sha256']:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        raise AuthError('Auth helper update needed') from None


class DirectoryLock:
    """proper-lockfile compatible: atomic mkdir, 2s heartbeat, no lock stealing.

    Native refresh locks use stale=60s/update=5s; storage uses stale=15s.
    Never delete another owner's lock, even after sleep. Dead locks from this
    helper alone can be recovered using a PID/start-time/boot receipt.
    """
    def __init__(self, path, receipts):
        self.path = path
        self.receipt = receipts / ('lock-' + hashlib.sha256(str(path).encode()).hexdigest()[:16] + '.json')
        self.fd = None
        self.stop = threading.Event()
        self.thread = None
        self.failed = False

    @staticmethod
    def process_identity(pid):
        # comm can contain spaces; starttime is field 22.
        return [Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]]

    def acquire(self):
        try:
            try:
                self.path.mkdir(mode=0o700)
            except FileExistsError:
                self.recover_own_dead_lock()
                self.path.mkdir(mode=0o700)
            self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.inode = os.fstat(self.fd).st_ino
            self.marker = uuid.uuid4().hex.encode()
            # An xattr survives heartbeats but not rmdir/recreation. Unlike a file
            # inside the directory, it doesn't break proper-lockfile's rmdir.
            os.setxattr(self.fd, 'user.opendeck-owner', self.marker)
            atomic_json(self.receipt, {'pid': os.getpid(), 'identity': self.process_identity(os.getpid()),
                                       'inode': self.inode, 'marker': self.marker.decode()})
            self.thread = threading.Thread(target=self.heartbeat, daemon=True)
            self.thread.start()
            return self
        except OSError:
            self.release()
            raise AuthError('Auth refresh busy') from None

    def recover_own_dead_lock(self):
        try:
            receipt = json.loads(self.receipt.read_text())
            meta = self.path.lstat()
            if (not stat.S_ISDIR(meta.st_mode) or meta.st_ino != receipt['inode']
                    or os.getxattr(self.path, 'user.opendeck-owner', follow_symlinks=False).decode() != receipt['marker']):
                return
            try:
                if self.process_identity(receipt['pid']) == receipt['identity']:
                    return
            except (FileNotFoundError, ProcessLookupError):
                pass
            # Restrict cleanup to our own dead helper's unchanged, empty directory.
            if self.path.lstat().st_ino == receipt['inode']:
                self.path.rmdir()
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def check(self):
        try:
            if (self.failed or self.fd is None or self.path.lstat().st_ino != self.inode
                    or os.getxattr(self.path, 'user.opendeck-owner', follow_symlinks=False) != self.marker):
                raise OSError()
        except OSError:
            raise AuthError('Auth refresh interrupted') from None

    def heartbeat(self):
        while not self.stop.wait(2):
            try:
                self.check()
                os.utime(self.fd, None)
            except (OSError, AuthError):
                self.failed = True
                return

    def release(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)
        if self.fd is not None:
            try:
                if self.path.lstat().st_ino == self.inode:
                    self.path.rmdir()
                    try:
                        record = json.loads(self.receipt.read_text())
                        if record.get('marker') == self.marker.decode():
                            self.receipt.unlink()
                    except (OSError, ValueError, AttributeError):
                        pass
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


@contextlib.contextmanager
def credential_locks(config):
    receipts = state_dir(config)
    locks = []
    try:
        # Same order as native: new refresh lock, legacy refresh lock, storage.
        for path in [config / '.oauth_refresh.lock', Path(str(config) + '.lock'),
                     config / '.storage-write.lock']:
            locks.append(DirectoryLock(path, receipts).acquire())
        yield lambda: [lock.check() for lock in locks]
    finally:
        for lock in reversed(locks):
            lock.release()


def retry_after(headers):
    try:
        return max(900, min(86400, int(headers.get('Retry-After', 900))))
    except (TypeError, ValueError):
        return 900


def renew(config, observed, force=False):
    if not force and not near_expiry(observed):
        return observed, False
    verify_protocol()
    with credential_locks(config) as check:
        doc, oauth, stamp = read_credentials(config)
        if oauth['accessToken'] != observed['accessToken'] or (not force and not near_expiry(oauth)):
            return oauth, False
        refresh = oauth.get('refreshToken')
        if not isinstance(refresh, str) or not refresh:
            raise AuthError('Sign in to Claude Code')
        scopes = oauth.get('scopes')
        if not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) for s in scopes):
            raise AuthError('Claude scopes unavailable')
        if 'user:profile' not in scopes:
            raise AuthError('Usage access denied')
        blocked = state_dir(config) / 'auth-block.json'
        try:
            if json.loads(blocked.read_text()).get('stamp') == stamp:
                raise AuthError('Sign in to Claude Code')
        except (OSError, ValueError, AttributeError):
            pass
        client = oauth.get('clientId') or CLIENT_ID
        if client != CLIENT_ID:
            raise AuthError('Unsupported Claude login')
        request = urllib.request.Request(TOKEN_URL, method='POST',
            headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                     'User-Agent': 'opendeck-claude-usage/0.4.0'},
            data=json.dumps({'grant_type': 'refresh_token', 'refresh_token': refresh,
                             'client_id': client, 'scope': ' '.join(scopes)}).encode())
        check()
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError()
                result = json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                delay = retry_after(error.headers)
                error.close()
                raise AuthError('Rate limited', True, delay) from None
            # Only an explicit invalid_grant means the saved login needs replacement.
            try:
                rejected = json.loads(error.read(8192)).get('error') == 'invalid_grant'
            except (OSError, ValueError, AttributeError):
                rejected = False
            finally:
                error.close()
            if rejected:
                atomic_json(blocked, {'stamp': stamp})
                raise AuthError('Sign in to Claude Code') from None
            raise AuthError('Token renewal unavailable') from None
        except (OSError, ValueError):
            raise AuthError('Token renewal unavailable') from None
        # The server may have consumed the old refresh token. If validation,
        # ownership, or persistence fails now, don't replay it on the next tick.
        atomic_json(blocked, {'stamp': stamp})
        check()
        if not isinstance(result, dict):
            raise AuthError('Invalid renewal response')
        access, rotated = result.get('access_token'), result.get('refresh_token', refresh)
        lifetime = result.get('expires_in')
        granted = result.get('scope', ' '.join(scopes))
        if (not isinstance(access, str) or not access or not isinstance(rotated, str) or not rotated
                or isinstance(lifetime, bool) or not isinstance(lifetime, (int, float))
                or not math.isfinite(lifetime) or not 0 < lifetime <= 366 * 86400
                or not isinstance(granted, str) or not set(scopes).issubset(granted.split())):
            raise AuthError('Invalid renewal response')
        current_doc, current, current_stamp = read_credentials(config)
        if current_stamp != stamp or current != oauth:
            # A non-cooperating writer changed it: never overwrite the newer login.
            return current, False
        updated = {**oauth, 'accessToken': access, 'refreshToken': rotated,
                   'expiresAt': int((time.time() + lifetime) * 1000), 'scopes': granted.split()}
        refresh_lifetime = result.get('refresh_token_expires_in')
        if isinstance(refresh_lifetime, (int, float)) and not isinstance(refresh_lifetime, bool) and math.isfinite(refresh_lifetime) and refresh_lifetime > 0:
            updated['refreshTokenExpiresAt'] = int((time.time() + refresh_lifetime) * 1000)
        # If omitted, don't invent a new refresh-token expiry.
        if 'claudeAiOauth' in current_doc:
            current_doc['claudeAiOauth'] = updated
        else:
            current_doc = updated
        check()
        try:
            atomic_json(config / '.credentials.json', current_doc)
        except OSError:
            raise AuthError('Could not save renewed login') from None
        return updated, True
