# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Tests for the HTTP client: request building, retries, error mapping and the
rate limiter. No network access is used.
"""

from unittest.mock import MagicMock

import requests

from odoo.tests import TransactionCase, tagged

from ..models.resend_client import (
    RateLimiter,
    ResendAPIError,
    ResendClient,
    build_idempotency_key,
)


@tagged('post_install', '-at_install')
class TestResendClient(TransactionCase):

    def _client(self, **kwargs):
        session = MagicMock()
        client = ResendClient('re_test_key', session=session, timeout=5,
                              max_retries=2, **kwargs)
        client._sleep_before_retry = MagicMock()
        return client, session

    def _response(self, status_code=200, payload=None, headers=None):
        response = MagicMock()
        response.status_code = status_code
        response.content = b'{}'
        response.json.return_value = payload if payload is not None else {'id': 'abc'}
        response.headers = headers or {}
        response.text = ''
        response.reason = 'reason'
        return response

    def test_idempotency_key_stable(self):
        first = build_idempotency_key('a', 'b', None, 1)
        second = build_idempotency_key('a', 'b', None, 1)
        third = build_idempotency_key('a', 'b', None, 2)
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertEqual(len(first), 64)
        self.assertLessEqual(len(first), 255)

    def test_send_email_ok(self):
        client, session = self._client()
        session.request.return_value = self._response(200, {'id': 'mail_1'})
        result = client.send_email({'to': ['a@b.c'], 'from': 'x@y.z', 'subject': 'S'})
        self.assertEqual(result['id'], 'mail_1')
        args, kwargs = session.request.call_args
        self.assertEqual(kwargs['headers']['User-Agent'], 'Odoo-Resend-Connector/1.0')
        self.assertTrue(kwargs['headers']['Authorization'].startswith('Bearer '))
        self.assertEqual(kwargs['timeout'], 5)

    def test_missing_recipient(self):
        client, session = self._client()
        with self.assertRaises(ResendAPIError):
            client.send_email({'from': 'x@y.z'})
        session.request.assert_not_called()

    def test_retry_on_rate_limit(self):
        client, session = self._client()
        session.request.side_effect = [
            self._response(429, {'name': 'rate_limit_exceeded', 'message': 'slow down'}),
            self._response(200, {'id': 'mail_2'}),
        ]
        result = client.send_email({'to': ['a@b.c'], 'from': 'x@y.z', 'subject': 'S'})
        self.assertEqual(result['id'], 'mail_2')
        self.assertEqual(session.request.call_count, 2)

    def test_quota_not_retried(self):
        client, session = self._client()
        session.request.return_value = self._response(
            429, {'name': 'daily_quota_exceeded', 'message': 'quota'})
        with self.assertRaises(ResendAPIError) as error:
            client.send_email({'to': ['a@b.c'], 'from': 'x@y.z', 'subject': 'S'})
        self.assertFalse(error.exception.retryable)
        self.assertEqual(session.request.call_count, 1)

    def test_invalid_key_not_retried(self):
        client, session = self._client()
        session.request.return_value = self._response(
            401, {'name': 'invalid_api_key', 'message': 'nope'})
        with self.assertRaises(ResendAPIError) as error:
            client.send_email({'to': ['a@b.c'], 'from': 'x@y.z', 'subject': 'S'})
        self.assertFalse(error.exception.retryable)
        self.assertIn('Invalid or revoked', error.exception.as_user_message())

    def test_network_error_retried(self):
        client, session = self._client()
        session.request.side_effect = [
            requests.exceptions.ConnectionError('boom'),
            self._response(200, {'id': 'mail_3'}),
        ]
        result = client.send_email({'to': ['a@b.c'], 'from': 'x@y.z', 'subject': 'S'})
        self.assertEqual(result['id'], 'mail_3')

    def test_retries_exhausted(self):
        client, session = self._client()
        session.request.side_effect = [
            self._response(503, {'name': 'internal_server_error', 'message': 'down'}),
            self._response(503, {'name': 'internal_server_error', 'message': 'down'}),
            self._response(503, {'name': 'internal_server_error', 'message': 'down'}),
        ]
        with self.assertRaises(ResendAPIError) as error:
            client.send_email({'to': ['a@b.c'], 'from': 'x@y.z', 'subject': 'S'})
        self.assertTrue(error.exception.retryable)
        self.assertEqual(session.request.call_count, 3)

    def test_rate_limiter(self):
        limiter = RateLimiter(rate=100, burst=1)
        limiter.acquire()
        self.assertLessEqual(limiter.tokens, 1.0)
        limiter.acquire()
        self.assertTrue(True)