# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Tests for the outgoing path: mail.mail -> ir.mail_server -> Resend payload.
"""

import base64
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models.compat import param_set
from ..models.resend_client import ResendAPIError

_PAYLOADS = []
_ERROR = {'value': None}


def _fake_send_email(self_client, payload, idempotency_key=None):
    """Replacement for ResendClient.send_email (no network)."""
    _PAYLOADS.append({'payload': payload, 'key': idempotency_key})
    if _ERROR['value']:
        raise _ERROR['value']
    return {'id': 'resend-mock-%s' % len(_PAYLOADS)}


_SEND_EMAIL = 'odoo.addons.rex_resend_connector.models.resend_client.ResendClient.send_email'
_DISABLE_SEND = 'odoo.addons.base.models.ir_mail_server.IrMail_Server._disable_send'


@tagged('post_install', '-at_install')
class TestResendOutgoing(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env['ir.config_parameter'].sudo()
        param_set(ICP, 'resend.enabled', 'True')
        param_set(ICP, 'resend.api_key', 're_test_key')
        param_set(ICP, 'resend.from_mode', 'off')
        param_set(ICP, 'resend.preserve_message_id', 'True')
        param_set(ICP, 'resend.inline_images_mode', 'cid')
        cls.env['resend.connector']._invalidate_config_cache()
        cls.server = cls.env['ir.mail_server'].create({
            'name': 'Resend test server',
            'smtp_host': 'smtp.invalid',
            'smtp_port': 25,
            'smtp_authentication': 'login',
            'smtp_user': 'user',
            'smtp_pass': 'pass',
            'resend_enabled': True,
        })

    def setUp(self):
        super().setUp()
        _PAYLOADS.clear()
        _ERROR['value'] = None

    def _make_mail(self, **values):
        message = self.env['mail.message'].create({
            'subject': values.pop('subject', 'Test subject'),
            'body': '<p>Body</p>',
            'message_type': 'email_outgoing',
            'email_from': values.pop('email_from', 'Acme <sales@example.com>'),
        })
        values.setdefault('email_to', 'customer@example.org')
        values.setdefault('subject', 'Test subject')
        values.setdefault('body_html', '<p>Body</p>')
        mail = self.env['mail.mail'].create(dict(
            values, mail_message_id=message.id, mail_server_id=self.server.id))
        return mail, message

    def _send(self, mail):
        with patch(_DISABLE_SEND, return_value=False), \
                patch(_SEND_EMAIL, new=_fake_send_email):
            mail.send()

    def test_resend_session_is_used(self):
        with patch(_DISABLE_SEND, return_value=False):
            session = self.env['ir.mail_server']._connect__(
                mail_server_id=self.server.id, smtp_from='sales@example.com')
        self.assertTrue(getattr(session, 'is_resend', False))
        session.quit()

    def test_payload_mapping(self):
        mail, message = self._make_mail(email_cc='cc@example.org')
        self._send(mail)
        self.assertEqual(mail.state, 'sent', mail.failure_reason)
        self.assertTrue(mail.resend_message_id)
        self.assertEqual(mail.resend_status, 'sent')
        payload = _PAYLOADS[0]['payload']
        self.assertEqual(payload['to'], ['customer@example.org'])
        self.assertEqual(payload['cc'], ['cc@example.org'])
        self.assertEqual(payload['subject'], 'Test subject')
        self.assertIn('Body', payload['html'])
        self.assertIn('Body', payload['text'])
        self.assertEqual(payload['headers']['Message-ID'], message.message_id)
        self.assertEqual(payload['from'], 'Acme <sales@example.com>')
        self.assertTrue(_PAYLOADS[0]['key'])
        self.assertIn('odoo_mail_id', [tag['name'] for tag in payload['tags']])

    def test_attachment_is_base64_encoded(self):
        mail, message = self._make_mail()
        attachment = self.env['ir.attachment'].create({
            'name': 'invoice.pdf', 'mimetype': 'application/pdf',
            'raw': b'%PDF-1.4 content',
        })
        mail.attachment_ids = [(6, 0, attachment.ids)]
        self._send(mail)
        attachments = _PAYLOADS[0]['payload']['attachments']
        self.assertEqual(attachments[0]['filename'], 'invoice.pdf')
        self.assertEqual(base64.b64decode(attachments[0]['content']), b'%PDF-1.4 content')

    def test_blocked_attachment_extension(self):
        mail, message = self._make_mail()
        attachment = self.env['ir.attachment'].create({
            'name': 'malware.exe', 'mimetype': 'application/octet-stream',
            'raw': b'MZ',
        })
        mail.attachment_ids = [(6, 0, attachment.ids)]
        self._send(mail)
        self.assertEqual(mail.state, 'exception')
        self.assertIn('cannot be sent through Resend', mail.failure_reason or '')

    def test_from_rewrite_always(self):
        ICP = self.env['ir.config_parameter'].sudo()
        param_set(ICP, 'resend.from_mode', 'always')
        param_set(ICP, 'resend.from_email', 'no-reply@example.com')
        self.env['resend.connector']._invalidate_config_cache()
        try:
            mail, message = self._make_mail()
            self._send(mail)
            payload = _PAYLOADS[0]['payload']
            self.assertIn('no-reply@example.com', payload['from'])
            self.assertIn('sales@example.com', payload['reply_to'])
        finally:
            param_set(ICP, 'resend.from_mode', 'off')
            self.env['resend.connector']._invalidate_config_cache()

    def test_api_failure_marks_exception(self):
        _ERROR['value'] = ResendAPIError('domain is not verified', status_code=403,
                                         code='validation_error')
        mail, message = self._make_mail()
        self._send(mail)
        self.assertEqual(mail.state, 'exception')
        self.assertIn('Resend API error', mail.failure_reason or '')
        tracking = self.env['resend.email'].search(
            [('rfc_message_id', '=', message.message_id)], limit=1)
        self.assertEqual(tracking.state, 'failed')
        self.assertEqual(tracking.error_code, 'validation_error')

    def test_missing_api_key_does_not_fall_back_to_smtp(self):
        ICP = self.env['ir.config_parameter'].sudo()
        param_set(ICP, 'resend.api_key', '')
        self.env['resend.connector']._invalidate_config_cache()
        try:
            mail, message = self._make_mail()
            self._send(mail)
            self.assertEqual(mail.state, 'exception')
            self.assertIn('API key', mail.failure_reason or '')
        finally:
            param_set(ICP, 'resend.api_key', 're_test_key')
            self.env['resend.connector']._invalidate_config_cache()

    def test_several_deliveries_of_one_mail(self):
        """Odoo may split one mail.mail into several deliveries (per recipient)."""
        mail, message = self._make_mail()
        server = self.server
        with patch(_DISABLE_SEND, return_value=False), patch(_SEND_EMAIL, new=_fake_send_email):
            for recipient in ('first@example.org', 'second@example.org'):
                msg = server._build_email__(
                    email_from='Acme <sales@example.com>', email_to=[recipient],
                    subject='Split', body='<p>x</p>', subtype='html',
                    message_id=message.message_id)
                server.send_email(msg, mail_server_id=server.id)
        self.assertEqual(len(mail.resend_email_ids), 2)
        first = mail.resend_email_ids[0]
        first._apply_event('email.bounced', {'bounce': {'message': '550 user unknown'}})
        self.assertEqual(mail.resend_status, 'bounced')
        self.assertEqual(mail.state, 'exception')
        second = mail.resend_email_ids[1]
        second._apply_event('email.delivered', {})
        self.assertEqual(mail.resend_status, 'bounced',
                         'the worst state of all deliveries must win')
        self.assertEqual(len(mail.resend_email_ids), 2)

    def test_delivery_state_ranking(self):
        mail, message = self._make_mail()
        self._send(mail)
        tracking = mail.resend_email_id
        tracking._apply_event('email.delivered', {})
        self.assertEqual(tracking.state, 'delivered')
        tracking._apply_event('email.opened', {})
        self.assertEqual(tracking.state, 'opened')
        tracking._apply_event('email.delivered', {})
        self.assertEqual(tracking.state, 'opened', 'a late event must not roll back')
        tracking._apply_event('email.bounced', {'bounce': {'message': '550'}})
        self.assertEqual(tracking.state, 'bounced')
        tracking._apply_event('email.delivered', {})
        self.assertEqual(tracking.state, 'bounced', 'terminal state must stick')