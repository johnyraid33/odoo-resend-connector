# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Tests for the webhook signature check, including the example from the Svix
documentation.
"""


from odoo.tests import BaseCase, tagged

from ..models.resend_client import WebhookVerificationError, verify_webhook_signature

_SECRET = 'whsec_plJ3nmyCDGBKInavdOK15jsl'
_MSG_ID = 'msg_loFOjxBNrRLzqYUf'
_TIMESTAMP = '1731705121'
_BODY = b'{"event_type":"ping","data":{"success":true}}'
_SIGNATURE = 'v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0='


@tagged('post_install', '-at_install')
class TestResendSignature(BaseCase):

    def _headers(self, signature=_SIGNATURE, timestamp=_TIMESTAMP, msg_id=_MSG_ID):
        return {
            'svix-id': msg_id,
            'svix-timestamp': timestamp,
            'svix-signature': signature,
        }

    def test_published_vector(self):
        payload = verify_webhook_signature(
            _BODY, self._headers(), _SECRET,
            tolerance=10 ** 10, now=1731705121)
        self.assertEqual(payload['event_type'], 'ping')
        self.assertEqual(payload['_svix_id'], _MSG_ID)

    def test_tampered_body_rejected(self):
        with self.assertRaises(WebhookVerificationError):
            verify_webhook_signature(
                _BODY.replace(b'true', b'false'), self._headers(), _SECRET,
                tolerance=10 ** 10, now=1731705121)

    def test_wrong_secret_rejected(self):
        with self.assertRaises(WebhookVerificationError):
            verify_webhook_signature(
                _BODY, self._headers(), 'whsec_' + 'A' * 32,
                tolerance=10 ** 10, now=1731705121)

    def test_replay_window(self):
        with self.assertRaises(WebhookVerificationError) as error:
            verify_webhook_signature(
                _BODY, self._headers(), _SECRET, tolerance=300,
                now=1731705121 + 301)
        self.assertIn('tolerance', str(error.exception))

    def test_missing_headers(self):
        with self.assertRaises(WebhookVerificationError):
            verify_webhook_signature(_BODY, {}, _SECRET)

    def test_multiple_signatures_rotation(self):
        headers = self._headers(signature='v1,AAAAinvalid ' + _SIGNATURE)
        payload = verify_webhook_signature(
            _BODY, headers, [_SECRET], tolerance=10 ** 10, now=1731705121)
        self.assertEqual(payload['data'], {'success': True})

    def test_list_of_secrets(self):
        payload = verify_webhook_signature(
            _BODY, self._headers(), ['whsec_' + 'A' * 32, _SECRET],
            tolerance=10 ** 10, now=1731705121)
        self.assertTrue(payload['data']['success'])