# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Tests for webhook processing: signature, duplicate calls, delivery states and
incoming mail routing.
"""

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from ..models.compat import param_set
from ..models.resend_client import ResendAPIError, WebhookVerificationError

_SECRET = 'whsec_' + base64.b64encode(b'test-webhook-secret-32-bytes-ok').decode()


def _sign(body, secret=_SECRET, msg_id='msg-1', timestamp=None):
    timestamp = timestamp or str(int(time.time()))
    key = base64.b64decode(secret[len('whsec_'):])
    signature = base64.b64encode(hmac.new(
        key, ('%s.%s.' % (msg_id, timestamp)).encode() + body, hashlib.sha256).digest()).decode()
    return {
        'svix-id': msg_id,
        'svix-timestamp': timestamp,
        'svix-signature': 'v1,' + signature,
    }


@tagged('post_install', '-at_install')
class TestResendWebhook(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env['ir.config_parameter'].sudo()
        param_set(ICP, 'resend.enabled', 'True')
        param_set(ICP, 'resend.api_key', 're_test_key')
        param_set(ICP, 'resend.webhook_secret', _SECRET)
        param_set(ICP, 'resend.store_webhook_payloads', 'True')
        cls.env['resend.connector']._invalidate_config_cache()

    def _payload(self, event_type='email.delivered', email_id='resend-1', **data):
        return {
            'type': event_type,
            'created_at': '2026-11-06T10:00:00.000Z',
            'data': dict({'email_id': email_id, 'message_id': '<mail@example.com>',
                          'from': 'Acme <sales@example.com>', 'to': ['a@b.c']}, **data),
        }

    def _register(self, payload, msg_id='msg-1'):
        body = json.dumps(payload).encode()
        headers = _sign(body, msg_id=msg_id)
        verified = self.env['resend.connector']._verify_webhook(body, headers)
        event, created = self.env['resend.webhook.event']._store_event(verified)
        if created:
            event.with_context(resend_payload=verified)._dispatch()
        return event, created

    def _tracking(self, resend_id='resend-1'):
        return self.env['resend.email'].create({
            'resend_message_id': resend_id, 'rfc_message_id': '<mail@example.com>',
            'state': 'sent', 'from_email': 'sales@example.com',
            'to_emails': 'a@b.c', 'subject': 'S',
        })

    def test_signature_roundtrip(self):
        payload = self._payload()
        body = json.dumps(payload).encode()
        verified = self.env['resend.connector']._verify_webhook(body, _sign(body))
        self.assertEqual(verified['type'], 'email.delivered')

    def test_bad_signature_rejected(self):
        payload = self._payload()
        body = json.dumps(payload).encode()
        headers = _sign(body)
        headers['svix-signature'] = 'v1,AAAA'
        with self.assertRaises(WebhookVerificationError):
            self.env['resend.connector']._verify_webhook(body, headers)

    def test_duplicate_svix_id_ignored(self):
        tracking = self._tracking()
        event, created = self._register(self._payload('email.delivered'), msg_id='dup-1')
        self.assertTrue(created)
        self.assertEqual(tracking.state, 'delivered')
        event2, created2 = self._register(self._payload('email.opened'), msg_id='dup-1')
        self.assertFalse(created2)
        self.assertEqual(event, event2)
        self.assertEqual(tracking.state, 'delivered', 'duplicate event must not apply')

    def test_delivery_state_updates_tracking(self):
        tracking = self._tracking()
        self._register(self._payload('email.delivered'), msg_id='state-1')
        self.assertEqual(tracking.state, 'delivered')
        self._register(self._payload('email.opened'), msg_id='state-2')
        self.assertEqual(tracking.state, 'opened')
        self._register(self._payload('email.delivered'), msg_id='state-3')
        self.assertEqual(tracking.state, 'opened', 'late events must not roll back')

    def test_bounce_updates_mail_and_notification(self):
        message = self.env['mail.message'].create({'subject': 'S', 'body': '<p>x</p>'})
        mail = self.env['mail.mail'].create({
            'mail_message_id': message.id, 'email_to': 'a@b.c',
            'subject': 'S', 'body_html': '<p>x</p>',
        })
        tracking = self._tracking('resend-bounce')
        tracking.mail_mail_id = mail.id
        notification = self.env['mail.notification'].create({
            'mail_message_id': message.id, 'mail_mail_id': mail.id,
            'notification_type': 'email', 'notification_status': 'sent',
            'res_partner_id': self.env.user.partner_id.id,
        })
        self._register(self._payload(
            'email.bounced', email_id='resend-bounce',
            bounce={'type': 'Permanent', 'message': '550 5.1.1 unknown'}), msg_id='bounce-1')
        self.assertEqual(tracking.state, 'bounced')
        self.assertEqual(mail.state, 'exception')
        self.assertEqual(notification.notification_status, 'bounce')

    def test_unknown_email_is_ignored(self):
        event, created = self._register(self._payload('email.delivered', email_id='nope'),
                                        msg_id='unknown-1')
        self.assertTrue(created)
        self.assertEqual(event.state, 'ignored')

    def test_payload_is_stored(self):
        event, created = self._register(self._payload(), msg_id='stored-1')
        self.assertIn('email.delivered', event.payload)

    def test_suppressed_maps_to_failed(self):
        tracking = self._tracking('resend-suppressed')
        self._register(self._payload(
            'email.suppressed', email_id='resend-suppressed',
            suppressed={'type': 'OnAccountSuppressionList', 'message': 'suppressed'}),
            msg_id='suppressed-1')
        self.assertEqual(tracking.state, 'failed')

    def test_reference_rewriting_maps_resend_message_id(self):
        """Resend replaces the Odoo Message-Id: replies must be mapped back."""
        partner = self.env['res.partner'].create({'name': 'Ref', 'email': 'ref@example.org'})
        original = partner.message_post(body='<p>orig</p>', subject='S-ref')
        resend_id = '<0102resend@amazonses.example>'
        tracking = self.env['resend.email'].create({
            'resend_message_id': 'resend-ref-1',
            'actual_message_id': resend_id,
            'rfc_message_id': original.message_id,
            'state': 'sent', 'from_email': 'a@b.c', 'to_emails': 'x@y.z', 'subject': 'S',
        })
        inbound = self.env['resend.inbound.email'].create({
            'resend_email_id': 'inbound-ref-1', 'from_email': 'ref@example.org',
            'to_emails': 'a@b.c', 'subject': 'Re: S-ref',
        })
        raw = '\n'.join([
            'From: Ref <ref@example.org>',
            'To: a@b.c',
            'Subject: Re: S-ref',
            'Message-Id: <inbound-ref-1@example.org>',
            'In-Reply-To: %s' % resend_id,
            'References: %s' % resend_id,
            '',
            'Body',
            '',
        ])
        rewritten = inbound._rewrite_references(raw.encode())
        self.assertIn(original.message_id.encode(), rewritten)
        self.assertNotIn(resend_id.encode(), rewritten)

    def test_access_rules_exist(self):
        """ACLs are created in code (ir.access on 20, ir.model.access on 19)."""
        from ..models.compat import IS_ODOO_20
        model = self.env['ir.model'].search([('model', '=', 'resend.email')], limit=1)
        Access = self.env['ir.access'] if IS_ODOO_20 else self.env['ir.model.access']
        rules = Access.search([('model_id', '=', model.id)])
        self.assertTrue(rules, 'resend.email must have access rules')
        manager = self.env.ref('resend_connector.group_resend_manager')
        self.assertIn(manager, rules.group_id)

    def test_cron_reprocesses_failed_events(self):
        """A delivery event lost to a concurrency conflict is retried by cron."""
        tracking = self._tracking('resend-cron-1')
        event, created = self._register(self._payload(
            'email.delivered', email_id='resend-cron-1'), msg_id='cron-1')
        self.assertTrue(created)
        self.assertEqual(tracking.state, 'delivered')
        # simulate an event that failed before applying
        tracking.write({'state': 'sent'})
        event.write({'state': 'failed', 'attempts': 1})
        self.env['resend.webhook.event']._cron_process_queued(event_ids=event.ids)
        self.assertEqual(tracking.state, 'delivered')

    def test_inbound_processing(self):
        partner = self.env['res.partner'].create({'name': 'Inbound', 'email': 'in@example.org'})
        original = partner.message_post(body='<p>original</p>', subject='S1')
        raw = (
            'From: Inbound <in@example.org>\r\n'
            'To: sales@reply.example.com\r\n'
            'Subject: Re: S1\r\n'
            'Message-Id: <inbound-1@example.org>\r\n'
            'In-Reply-To: %s\r\n'
            'References: %s\r\n'
            'MIME-Version: 1.0\r\n'
            'Content-Type: text/plain; charset="utf-8"\r\n'
            '\r\n'
            'Reply body\r\n'
        ) % (original.message_id, original.message_id)
        meta = {
            'id': 'inbound-1', 'from': 'in@example.org', 'to': ['sales@reply.example.com'],
            'cc': [], 'subject': 'Re: S1', 'message_id': '<inbound-1@example.org>',
            'html': '<p>Reply body</p>',
            'headers': {'from': 'Inbound <in@example.org>',
                        'in-reply-to': original.message_id,
                        'references': original.message_id},
            'raw': {'download_url': 'https://example.invalid/raw'},
            'attachments': [],
        }
        with patch('odoo.addons.resend_connector.models.resend_client.ResendClient.get_received_email',
                   return_value=meta), \
             patch('odoo.addons.resend_connector.models.resend_client.ResendClient.download_url',
                   return_value=raw.encode()):
            inbound = self.env['resend.inbound.email'].create({
                'resend_email_id': 'inbound-1', 'from_email': 'in@example.org',
                'to_emails': 'sales@reply.example.com', 'subject': 'Re: S1',
            })
            inbound._fetch_and_process()
        self.assertEqual(inbound.state, 'processed', inbound.error)
        self.assertTrue(inbound.mail_message_id)
        reply = self.env['mail.message'].search([
            ('message_id', '=', '<inbound-1@example.org>')], limit=1)
        self.assertTrue(reply, 'the reply must be routed to the original thread')
        self.assertEqual(reply.model, 'res.partner')
        self.assertEqual(reply.res_id, partner.id)

    def test_inbound_api_error_is_retryable(self):
        with patch('odoo.addons.resend_connector.models.resend_client.ResendClient.get_received_email',
                   side_effect=ResendAPIError('down', status_code=503, retryable=True)):
            inbound = self.env['resend.inbound.email'].create({
                'resend_email_id': 'inbound-err', 'from_email': 'x@y.z',
                'to_emails': 'a@b.c', 'subject': 'X',
            })
            with self.assertRaises(ResendAPIError):
                inbound._fetch_and_process()
        self.assertEqual(inbound.state, 'pending')