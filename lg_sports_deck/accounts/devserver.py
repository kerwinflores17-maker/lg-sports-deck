"""Explicitly configured development entry point; never a production server."""
import os
from .portal import create_app


def required(name):
    value=os.environ.get(name,'').strip()
    if not value:raise RuntimeError(f'{name} must be explicitly set for this fictional-only test server')
    return value


app=create_app(required('LG_ACCOUNTS_TEST_DB'),required('LG_ACCOUNTS_TEST_MEDIA'),
    set(filter(None,(x.strip() for x in required('LG_ACCOUNTS_TEST_ORIGINS').split(',')))),
    secure_cookies=os.environ.get('LG_ACCOUNTS_INSECURE_LOOPBACK')!='1',
    ffmpeg_path=os.environ.get('LG_ACCOUNTS_SYNTHETIC_FFMPEG') or None)
