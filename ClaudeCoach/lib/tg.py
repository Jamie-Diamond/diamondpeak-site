#!/usr/bin/env python3
"""tg.py - hardened Telegram transport, shared by the nutrition bot.

Every guard in here was earned by a live failure in telegram/bot.py, so this is an
extraction of that hardening rather than a fresh implementation:

  - PLAIN-TEXT RETRY on a 400. Telegram rejects the whole message when legacy
    Markdown will not parse, which happens on an unbalanced *, _, [ or ` in
    generated text. Without the retry the reply is silently lost - that is the
    16 Jun bug where a generated swim debrief was never delivered. Retry once as
    plain text so a reply is never dropped; worst case the user sees a stray
    asterisk.
  - "message is not modified" on a 400 is BENIGN, not a failure. It fires when an
    edit's text already matches what is shown. Treating it as an error triggers a
    pointless plain-text retry and a bogus caller fallback.
  - 429 FLOOD CONTROL carries parameters.retry_after. Honour it once, capped, so a
    worker can never hang on a long back-off.
  - IPv4-only resolution. The VM's IPv6 route to api.telegram.org is unreliable and
    a hung connect looks identical to a dead bot.

DEBT, STATED PLAINLY: telegram/bot.py still has its own copy of this logic. It is
the live coach bot and refactoring it is not something to do casually mid-session,
so until that lands, a fix to either transport must be applied to BOTH. That is
exactly the divergence trap this project has refused everywhere else, and it is
recorded here rather than hidden.
"""

import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

SSL_CONTEXT = ssl.create_default_context()
API = "https://api.telegram.org"

_real_getaddrinfo = socket.getaddrinfo


def force_ipv4():
    """Resolve api.telegram.org over IPv4 only.

    The VM's IPv6 path to Telegram is unreliable and a hung connect is
    indistinguishable from a dead bot from the outside. Applied globally by the bot
    at startup, not per-call, because urllib gives no per-request hook."""
    def _ipv4_only(host, *args, **kwargs):
        if isinstance(host, str) and "telegram.org" in host:
            return [ai for ai in _real_getaddrinfo(host, *args, **kwargs)
                    if ai[0] == socket.AF_INET] or _real_getaddrinfo(host, *args, **kwargs)
        return _real_getaddrinfo(host, *args, **kwargs)
    socket.getaddrinfo = _ipv4_only


def post(token: str, method: str, payload: dict, log=print, timeout: int = 10) -> dict:
    """POST to the Bot API with the retry behaviour described above.

    Returns the parsed response, or {"ok": False, ...} rather than raising: a reply
    failing to send must never take down the poll loop."""
    url = f"{API}/bot{token}/{method}"
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as r:
            return json.loads(r.read())
    except Exception as exc:
        body = (exc.read().decode("utf-8", "replace")[:300]
                if hasattr(exc, "read") else "")
        code = getattr(exc, "code", None)

        if code == 400 and "not modified" in body:
            # The edit already matches what is displayed. Benign.
            return {"ok": True, "result": {}}

        if code == 429:
            retry_after = 0.0
            try:
                params = (json.loads(body) if body else {}).get("parameters") or {}
                retry_after = float(params.get("retry_after", 0)) or 0.0
            except Exception:
                retry_after = 0.0
            retry_after = min(max(retry_after or 1.0, 0.5), 5.0)
            log(f"tg {method} 429, backing off {retry_after:.1f}s then retrying once")
            time.sleep(retry_after)
            try:
                req2 = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req2, timeout=timeout,
                                            context=SSL_CONTEXT) as r:
                    return json.loads(r.read())
            except Exception as exc2:
                log(f"tg {method} failed after back-off: {exc2}")
                return {"ok": False, "error": str(exc2)}

        if code == 400 and payload.get("parse_mode"):
            # Markdown would not parse. Send it as plain text rather than lose it.
            log(f"tg {method} 400 on parse_mode, retrying as plain text: {body[:120]}")
            plain = {k: v for k, v in payload.items() if k != "parse_mode"}
            try:
                req3 = urllib.request.Request(url, data=json.dumps(plain).encode(),
                                              headers=headers)
                with urllib.request.urlopen(req3, timeout=timeout,
                                            context=SSL_CONTEXT) as r:
                    return json.loads(r.read())
            except Exception as exc3:
                log(f"tg {method} plain-text retry failed: {exc3}")
                return {"ok": False, "error": str(exc3)}

        log(f"tg {method} failed: {code} {exc} {body[:160]}")
        return {"ok": False, "error": str(exc)}


def get(token: str, method: str, params: dict, log=print, timeout: int = 65) -> dict:
    """GET, used for long polling. The default timeout exceeds the poll timeout so
    the socket does not close under a legitimately idle long poll."""
    url = f"{API}/bot{token}/{method}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=SSL_CONTEXT) as r:
            return json.loads(r.read())
    except Exception as exc:
        log(f"tg {method} get failed: {exc}")
        return {"ok": False, "result": []}


def send(token: str, chat_id, text: str, reply_markup=None, parse_mode="Markdown",
         log=print) -> dict:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return post(token, "sendMessage", payload, log=log)


def answer_callback(token: str, callback_query_id: str, text: str = "", log=print):
    return post(token, "answerCallbackQuery",
                {"callback_query_id": callback_query_id, "text": text}, log=log)


def edit_text(token: str, chat_id, message_id, text: str, reply_markup=None,
              parse_mode="Markdown", log=print):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return post(token, "editMessageText", payload, log=log)


def inline(rows) -> dict:
    """Inline keyboard from [[(label, callback_data), ...], ...]."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                for row in rows]}
