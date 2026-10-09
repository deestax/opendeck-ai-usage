#!/usr/bin/env python3
"""Usage GET plus coordinated token renewal. Never print credentials/errors."""
import json
import urllib.request
import urllib.error
from refresh_auth import (AuthError, NoRedirect, config_dir, read_credentials,
                          renew, retry_after)


def fetch():
    try:
        config = config_dir()
        _, oauth, _ = read_credentials(config)
        oauth, refreshed = renew(config, oauth)
        result = request_usage(oauth['accessToken'])
        if result.get('unauthorized') and not refreshed:
            oauth, refreshed = renew(config, oauth, force=True)
            result = request_usage(oauth['accessToken'])
        if result.pop('unauthorized', False):
            result['error'] = 'Claude authorization rejected'
        if refreshed:
            result['auth_refreshed'] = True
        return result
    except AuthError as error:
        return error.result
    except (OSError, ValueError, TypeError):
        return {'error': 'Usage unavailable'}


def request_usage(token):
    request = urllib.request.Request(
        'https://api.anthropic.com/api/oauth/usage',
        headers={'Authorization': 'Bearer ' + token,
                 'anthropic-beta': 'oauth-2025-04-20',
                 'anthropic-version': '2023-06-01',
                 'Accept': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            # Bound malformed/oversized responses. Only usage data goes to the parent.
            raw = response.read(262145)
            if len(raw) > 262144:
                return {'error': 'Invalid usage response'}
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {'error': 'Invalid usage response'}
            return {'data': {key: data[key] for key in
                             ('five_hour', 'seven_day', 'limits', 'spend', 'extra_usage')
                             if key in data}}
    except urllib.error.HTTPError as error:
        result = {'error': {401: 'Claude authorization rejected', 403: 'Usage access denied',
                            429: 'Rate limited'}.get(error.code, 'Usage unavailable')}
        if error.code == 401:
            result['unauthorized'] = True
        if error.code == 429:
            result.update(rate_limited=True, retry_after=retry_after(error.headers))
        error.close()
        return result
    except (OSError, ValueError):
        return {'error': 'Usage unavailable'}


if __name__ == '__main__':
    print(json.dumps(fetch()))
