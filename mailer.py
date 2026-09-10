"""Transactional email using Resend or authenticated SMTP over TLS."""
from email.message import EmailMessage
import json
import logging
import os
import smtplib
import ssl
import urllib.request


def configured():
    if not os.environ.get('MAIL_FROM'):
        return False
    return bool(os.environ.get('RESEND_API_KEY') or all(
        os.environ.get(key) for key in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD')
    ))


def send_email(to, subject, body):
    if not configured():
        return False
    try:
        api_key = os.environ.get('RESEND_API_KEY')
        if api_key:
            payload = json.dumps({'from': os.environ['MAIL_FROM'], 'to': [to],
                                  'subject': subject, 'text': body}).encode()
            req = urllib.request.Request('https://api.resend.com/emails', data=payload,
                headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json',
                         'User-Agent': 'Flashcards/1.0'}, method='POST')
            with urllib.request.urlopen(req, timeout=10) as response:
                return 200 <= response.status < 300 and bool(json.load(response).get('id'))
        message = EmailMessage()
        message['From'] = os.environ['MAIL_FROM']
        message['To'] = to
        message['Subject'] = subject
        message.set_content(body)
        port = int(os.environ.get('SMTP_PORT', '465'))
        context = ssl.create_default_context()
        connection = (smtplib.SMTP_SSL(os.environ['SMTP_HOST'], port, timeout=10, context=context)
                      if port == 465 else smtplib.SMTP(os.environ['SMTP_HOST'], port, timeout=10))
        with connection as smtp:
            if port != 465:
                smtp.starttls(context=context)
            smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD'])
            return not smtp.send_message(message)
    except Exception as error:
        # Provider errors may contain addresses or credentials: log only the type.
        logging.getLogger(__name__).warning('Email delivery failed (%s)', type(error).__name__)
        return False
