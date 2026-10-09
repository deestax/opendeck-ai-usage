# Authentication and safety

## Claude

The plugin reads the existing Linux Claude Code OAuth login from `.credentials.json`. Config directory precedence is `CLAUDE_SECURESTORAGE_CONFIG_DIR`, `CLAUDE_CONFIG_DIR`, then `~/.claude`. Credentials must be a regular file owned by the current user, without group/other permissions; symlinks are rejected.

It reads usage from `https://api.anthropic.com/api/oauth/usage`, an undocumented endpoint. It does not invoke models, buy credits or change billing. Missing quotas, caps and resets stay unknown rather than becoming fictional zeroes.

### Token renewal is a credential write

Within five minutes of expiry, or once after a usage 401, the helper can renew through `https://platform.claude.com/v1/oauth/token`. This is **not purely read-only**: successful renewal atomically replaces access/refresh/expiry fields in the original credentials file with mode `0600`, preserving unrelated records. Redirects are disabled on usage and renewal requests. Tokens never enter plugin settings, process arguments, usage caches or logs.

Automatic renewal is enabled only when `claude` on OpenDeck's PATH matches the reviewed Linux Claude Code **2.1.289** protocol and SHA-256 in `assets/auth-refresh.json`. That digest is a compatibility guard, not a credential. Different builds, architectures and upgrades fail closed at renewal with `Auth helper update needed`. The published config contains no machine-specific binary path. An already-valid access token does not require this digest check.

For an unreviewed CLI version, keep sign-in/renewal under Claude Code's control. Do not downgrade just for this helper, blindly replace the digest, or delete another client's lock. Future compatibility updates require review of the actual lock/storage protocol and tests, not only a version string change.

The helper acquires `.oauth_refresh.lock`, legacy `<config-dir>.lock`, then `.storage-write.lock`, using atomic directory creation and 2-second heartbeats. Rereading credentials under the locks avoids overwriting a concurrent renewal. It checks ownership again before committing and preserves an omitted refresh token. Explicit `invalid_grant`, or a rotation that cannot be saved, blocks replay until credentials change. Only this helper's own confirmed-dead locks can be recovered, using PID/start-time/boot identity and a random xattr marker. It never steals another client's lock after suspend. Linux filesystem user xattrs are required.

Non-secret lock receipts/rejected-credential metadata are stored under `$XDG_STATE_HOME/opendeck-claude-usage` (default `~/.local/state/`). Usage data and rate-limit deadlines are stored under `$XDG_CACHE_HOME/opendeck-claude-usage/state.json` (default `~/.cache/`). These files contain private usage/config metadata and should not be published. Cached readings are marked old until revalidated; readings older than seven days or associated with changed credentials are discarded.

## Codex

The plugin launches `codex app-server --stdio` and reads `account/rateLimits/read` over JSON-RPC. Codex manages its own existing ChatGPT-plan login. The plugin does not read or copy `auth.json`, generate content, or use an API key. The child process is stopped after the request or timeout. The CLI itself may perform its normal account maintenance.

## OpenRouter

The separately installed [upstream OpenRouter Credits plugin](https://github.com/ImDevinC/opendeck-openrouter-usage) owns the network request and authentication. It requires a **management API key**, which is privileged: protect it. The key is stored in the action's settings. Do not publish profiles, backups, property-inspector screenshots, or logs without reviewing them for secrets.

The optional display bridge forwards settings and input events only over local WebSockets. It transforms successful credit images and recognized system/media display messages on N4 `Default` profiles; error images and other profiles pass through. It does not log raw traffic or independently request OpenRouter usage.
