#!/usr/bin/env python3
"""
Message building and SMTP sending for outreach. Sending only: Zoho's free plan has no IMAP, so
replies, bounces and unsubscribes are reported by hand with `outreach.py stop`.
"""

import html
import os
import re
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate


def new_message_id(address):
    domain = (address or "").rpartition("@")[2] or "localhost"
    return f"<{secrets.token_hex(12)}@{domain}>"


def text_to_html(text):
    """Paragraphs and line breaks only: no styling, images, tracking or links beyond what the text says."""
    paragraphs = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    return "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)


def build_message(inbox, to_name, to_email, subject, text, message_id, unsubscribe, in_reply_to=None):
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["From"] = formataddr((inbox["display_name"], inbox["address"]))
    msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
    msg["Subject"] = subject
    # mailto only: RFC 8058 one-click needs an HTTPS endpoint, and Gmail still shows the link
    msg["List-Unsubscribe"] = f"<mailto:{unsubscribe}?subject=unsubscribe>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(text)
    msg.add_alternative(text_to_html(text), subtype="html")
    return msg


def smtp_connect(inbox, env, timeout=30):
    """Logged-in SMTP session for an inbox; the password comes from the .env key named in inbox.pass_env."""
    password = env.get(inbox["pass_env"]) or ""
    if not password:
        raise RuntimeError(f"{inbox['pass_env']} is not set in .env")
    context = ssl.create_default_context()
    if int(inbox["smtp_port"]) == 465:
        server = smtplib.SMTP_SSL(inbox["smtp_host"], 465, timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(inbox["smtp_host"], int(inbox["smtp_port"]), timeout=timeout)
        server.starttls(context=context)
    server.login(inbox["username"], password)
    return server


def save_draft(msg, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(bytes(msg))
    return path
