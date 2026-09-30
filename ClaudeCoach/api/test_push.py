"""Tests for api/push.py. Run: pytest ClaudeCoach/api/test_push.py (needs pywebpush)."""
import json
import time

import pytest

import push


@pytest.fixture()
def pdir(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(push, "KEY_FILE", tmp_path / "state" / "vapid.pem")
    monkeypatch.setattr(push, "SUBS_FILE", tmp_path / "state" / "subs.json")
    monkeypatch.setattr(push, "OFFSETS_FILE", tmp_path / "state" / "offsets.json")
    monkeypatch.setattr(push, "_own_slug", lambda email: {"kat@x.com": "kat", "jam@x.com": "jam"}.get(email))
    return tmp_path


def sub(n):
    return {"endpoint": f"https://push.example/{n}", "keys": {"p256dh": "k", "auth": "a"}}


def test_public_key_is_an_uncompressed_p256_point(pdir):
    import base64
    k = push.public_key()
    raw = base64.urlsafe_b64decode(k + "=" * (-len(k) % 4))
    assert len(raw) == 65 and raw[0] == 4
    assert push.public_key() == k                        # made once, then reused


def test_subscribe_is_per_email_and_rejects_junk(pdir):
    push.subscribe("kat@x.com", sub(1))
    push.subscribe("kat@x.com", sub(2))
    push.subscribe("jam@x.com", sub(3))
    assert push.subscribed("kat@x.com") == 2
    push.unsubscribe("jam@x.com", sub(1)["endpoint"])    # not theirs: no effect
    assert push.subscribed("kat@x.com") == 2
    push.unsubscribe("kat@x.com", sub(1)["endpoint"])
    assert push.subscribed("kat@x.com") == 1
    with pytest.raises(ValueError):
        push.subscribe("kat@x.com", {"endpoint": "x"})


def test_notify_goes_only_to_that_athletes_devices_and_drops_dead_ones(pdir, monkeypatch):
    import pywebpush
    push._vapid()
    push.subscribe("kat@x.com", sub(1))
    push.subscribe("kat@x.com", sub(2))
    push.subscribe("jam@x.com", sub(3))
    calls = []

    class Gone:
        status_code = 410

    def fake(subscription, data, **k):
        calls.append((subscription["endpoint"], json.loads(data)))
        if subscription["endpoint"].endswith("/2"):
            raise pywebpush.WebPushException("gone", response=Gone())

    monkeypatch.setattr(pywebpush, "webpush", fake)
    assert push.notify("kat", "Morning card") == 1
    assert sorted(c[0] for c in calls) == [sub(1)["endpoint"], sub(2)["endpoint"]]
    assert calls[0][1]["body"] == "Morning card"
    assert push.subscribed("kat@x.com") == 1             # the 410 one is gone
    assert push.LAST_ERRORS and "410" in push.LAST_ERRORS[0]


def test_plain_strips_markdown_and_the_footer():
    assert push.plain("*Week of 28 Sep* — easy\n_46 days to race · S5.5_") == "Week of 28 Sep — easy"
    assert push.plain("x" * 200).endswith("…") and len(push.plain("x" * 200)) == 140


def test_watcher_notifies_new_lines_only_and_skips_live_chat_ones(pdir, monkeypatch):
    cc = pdir / "cc"
    (cc / "athletes" / "kat").mkdir(parents=True)
    out = cc / "athletes" / "kat" / "web-outbox.jsonl"
    out.write_text(json.dumps({"text": "old message"}) + "\n")
    athletes = pdir / "athletes.json"
    athletes.write_text(json.dumps({"kat": {"chat_id": "2"}}))
    sent = []
    monkeypatch.setattr(push, "notify", lambda slug, body, **k: sent.append((slug, body)))
    push.start(cc, push._own_slug, athletes, interval=0.05)
    time.sleep(0.2)                                       # first sight: starts at the end
    with open(out, "a") as f:
        f.write(json.dumps({"text": "*Morning card*"}) + "\n")
        f.write(json.dumps({"text": "chart", "source": "web-turn"}) + "\n")
        f.write(json.dumps({"text": "", "photo": "x.png"}) + "\n")
    time.sleep(0.3)
    assert sent == [("kat", "Morning card"), ("kat", "📷 Photo")]
