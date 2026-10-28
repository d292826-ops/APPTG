"""Validation of Telegram Mini App initData, without trusting client roles."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

class AuthError(Exception):
    pass

def validate_init_data(raw, token, now=None, max_age=3600):
    if not isinstance(raw, str) or not raw or len(raw) > 16384:
        raise AuthError('Откройте приложение из Telegram.')
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        values = dict(pairs)
        if len(values) != len(pairs):
            raise ValueError()
        supplied = values.pop('hash')
        check = '\n'.join(f'{key}={values[key]}' for key in sorted(values))
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError()
        stamp = int(values['auth_date'])
        current = time.time() if now is None else now
        if stamp > current + 30 or current - stamp > max_age:
            raise AuthError('Сессия истекла. Закройте и снова откройте приложение.')
        user = json.loads(values['user'])
        if type(user.get('id')) is not int or not 0 < user['id'] < 2**53:
            raise ValueError()
        for field in ('first_name', 'last_name', 'username'):
            if field in user and (not isinstance(user[field], str) or len(user[field]) > 256):
                raise ValueError()
        return user
    except AuthError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise AuthError('Не удалось подтвердить вход. Откройте приложение из Telegram.') from None
