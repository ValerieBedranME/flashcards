import io
import os
import unittest
from unittest.mock import MagicMock, patch

import mailer
from flask import Flask, g
from storage import init_storage


class MailerTests(unittest.TestCase):
    def test_smtp_tls_and_delivery_failure(self):
        env = {'MAIL_FROM': 'sender@example.com', 'SMTP_HOST': 'smtp.example.com',
               'SMTP_USER': 'sender@example.com', 'SMTP_PASSWORD': 'test-only'}
        with patch.dict(os.environ, env, clear=True), patch.object(mailer.smtplib, 'SMTP_SSL') as connect:
            smtp = connect.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            self.assertTrue(mailer.send_email('recipient@example.com', 'Код', '123456'))
            self.assertIn('context', connect.call_args.kwargs)
            smtp.login.assert_called_once_with('sender@example.com', 'test-only')
            message = smtp.send_message.call_args.args[0]
            self.assertEqual(message['To'], 'recipient@example.com')
            self.assertIn('123456', message.get_content())
            smtp.send_message.side_effect = OSError('provider failure')
            with self.assertLogs('mailer', level='WARNING'):
                self.assertFalse(mailer.send_email('recipient@example.com', 'Код', '123456'))

    def test_resend_success_and_missing_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(mailer.configured())
            self.assertFalse(mailer.send_email('recipient@example.com', 'Код', '123456'))
        response = io.BytesIO(b'{"id":"test-id"}')
        response.status = 200
        with patch.dict(os.environ, {'RESEND_API_KEY': 'test-only', 'MAIL_FROM': 'sender@example.com'}, clear=True), patch.object(mailer.urllib.request, 'urlopen', return_value=response) as send:
            self.assertTrue(mailer.send_email('recipient@example.com', 'Код', '123456'))
            self.assertEqual(send.call_args.args[0].full_url, 'https://api.resend.com/emails')

    def test_failed_attempt_commit_is_explicit(self):
        app = Flask(__name__)
        init_storage(app)
        db = MagicMock()

        @app.before_request
        def inject_connection():
            g.db = db

        @app.route('/api/attempt/<int:persist>')
        def attempt(persist):
            g.commit_storage_on_error = bool(persist)
            return {'error': 'invalid code'}, 400

        client = app.test_client()
        client.get('/api/attempt/1')
        db.commit.assert_called_once()
        db.rollback.assert_not_called()
        db.reset_mock()
        client.get('/api/attempt/0')
        db.rollback.assert_called_once()
        db.commit.assert_not_called()
