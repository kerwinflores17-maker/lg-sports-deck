"""Explicit isolated developer entrypoint, not a production server."""
import os
from .app import create_app


def required(name):
    value=os.environ.get(name,'').strip()
    if not value:raise RuntimeError(f'{name} must be set for the fictional-only unified prototype')
    return value


app=create_app(required('LG_UNIFIED_TEST_DB'),required('LG_UNIFIED_TEST_MEDIA'),
               set(filter(None,(s.strip() for s in required('LG_UNIFIED_TEST_ORIGINS').split(',')))),
               secure_cookies=os.environ.get('LG_UNIFIED_INSECURE_LOOPBACK')!='1',
               ffmpeg_path=os.environ.get('LG_UNIFIED_SYNTHETIC_FFMPEG') or None,
               enable_sample_retention=os.environ.get('LG_UNIFIED_TEST_SAMPLE_RETENTION')=='1',
               enable_fictional_manual_qr=os.environ.get('LG_UNIFIED_FICTIONAL_MANUAL_QR')=='1')
