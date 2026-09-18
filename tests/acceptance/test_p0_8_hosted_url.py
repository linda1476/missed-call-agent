"""P0-8: the hosted deployment serves a real 60-second call through a
public URL. Requires PUBLIC_URL (and the deployment to be up); fails until
P0-8 is actually done — that is the honest signal."""

import os
import time
import urllib.request

import pytest

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")


def test_public_url_serves_60s_call():
    if not PUBLIC_URL:
        pytest.fail("PUBLIC_URL not set — deployment not configured (P0-8 todo)")

    with urllib.request.urlopen(f"{PUBLIC_URL}/health", timeout=10) as r:
        assert r.status == 200

    # A call session stays up for 60 seconds on the public URL.
    with urllib.request.urlopen(f"{PUBLIC_URL}/call/start", timeout=10) as r:
        session = __import__("json").loads(r.read())
    sid = session["session_id"]

    deadline = time.time() + 60
    while time.time() < deadline:
        with urllib.request.urlopen(
                f"{PUBLIC_URL}/call/{sid}/status", timeout=10) as r:
            body = __import__("json").loads(r.read())
        assert body["alive"], f"session {sid} dropped before 60s"
        time.sleep(5)
