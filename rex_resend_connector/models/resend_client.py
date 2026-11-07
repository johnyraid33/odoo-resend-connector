# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""HTTP client for the Resend API.

The file has no Odoo imports, so it can be tested or reused on its own. It needs
only the requests library and the standard library.

Every call goes through ResendClient.request(), which sets a timeout, retries
temporary failures (network errors, 408, 425, 429 and 5xx) with backoff and
respects Retry-After, keeps the Idempotency-Key on retries, and raises
ResendAPIError with the HTTP status, the Resend error code and a retryable flag.

API reference: https://resend.com/docs/api-reference/introduction
"""

import base64
import hashlib
import hmac
import json
import logging
import random
import threading
import time

import requests

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = 'https://api.resend.com'
DEFAULT_TIMEOUT = 20

#: HTTP status codes that are worth retrying (transient failures).
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Resend error codes that indicate a transient problem even on a 4xx answer.
RETRYABLE_ERROR_CODES = frozenset({
    'rate_limit_exceeded',
    'concurrent_idempotent_requests',
    'internal_server_error',
})

#: 429 error codes that must NOT be retried: the daily/monthly quota is burnt,
#: retrying only wastes requests and makes the queue stall.
NON_RETRYABLE_RATE_LIMIT_CODES = frozenset({
    'daily_quota_exceeded',
    'monthly_quota_exceeded',
})

#: Maximum number of email addresses accepted per Resend API call.
MAX_RECIPIENTS_PER_EMAIL = 50

#: Hard limit of the Resend API for the total size of an email (base64 encoded).
MAX_EMAIL_SIZE = 40 * 1024 * 1024


class RateLimiter:
    """Process-wide token bucket matching the Resend team limit.

    Resend allows +-10 requests per second per team, shared by every API key,
    worker and database. Staying slightly below the limit avoids 429 storms
    during mass mailings; the limiter is shared by all threads of the process.
    """

    def __init__(self, rate=8.0, burst=None):
        self.rate = max(1.0, float(rate or 8.0))
        self.capacity = float(burst or max(1.0, self.rate))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
            time.sleep(min(wait, 1.0))


_LIMITERS = {}
_LIMITERS_LOCK = threading.Lock()


def get_rate_limiter(rate=None, key='default'):
    """Return the shared rate limiter for this process."""
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(key)
        if limiter is None:
            limiter = _LIMITERS[key] = RateLimiter(rate or 8.0)
        elif rate:
            limiter.rate = max(1.0, float(rate))
        return limiter


class ResendAPIError(Exception):
    """Structured error raised for any failed Resend API interaction."""

    def __init__(self, message, status_code=None, code=None, payload=None,
                 retryable=False, retry_after=None):
        self.message = message
        self.status_code = status_code
        self.code = code
        self.payload = payload or {}
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(message)

    def __str__(self):
        parts = [self.message]
        if self.code:
            parts.append('[%s]' % self.code)
        if self.status_code:
            parts.append('(HTTP %s)' % self.status_code)
        return ' '.join(parts)

    def as_user_message(self):
        """Human readable, safe to display to an Odoo user."""
        if self.status_code == 401:
            return 'Invalid or revoked Resend API key.'
        if self.status_code == 403:
            return 'Resend refuses this request (domain not verified or insufficient permissions): %s' % self.message
        if self.status_code == 422:
            return 'Resend rejected the message content: %s' % self.message
        if self.status_code == 429:
            return 'Resend rate limit exceeded, the email will be retried later.'
        if self.status_code and self.status_code >= 500:
            return 'Resend service is temporarily unavailable (%s).' % self.status_code
        return self.message


class WebhookVerificationError(Exception):
    """Raised when a Resend (Svix) webhook signature cannot be verified."""


class _RetryRequest(Exception):
    """Internal marker used to bubble a retry decision up to callers."""

    def __init__(self, payload, code, message, status_code, retry_after):
        self.payload = payload
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(message)


def build_idempotency_key(*parts):
    """Build a stable, <=255 chars idempotency key from arbitrary values."""
    raw = '\x1f'.join('' if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


class ResendClient:
    """Thin, retrying wrapper around the Resend REST API."""

    def __init__(self, api_key, base_url=None, timeout=DEFAULT_TIMEOUT,
                 max_retries=2, session=None, user_agent=None, verify=True,
                 rate_limit=None, limiter_key='default'):
        if not api_key:
            raise ResendAPIError('Missing Resend API key.', code='missing_api_key')
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip('/')
        self.timeout = timeout or DEFAULT_TIMEOUT
        self.max_retries = max(0, int(max_retries or 0))
        self.session = session or requests.Session()
        self.user_agent = user_agent or 'Odoo-Resend-Connector/1.0'
        self.verify = verify
        self.limiter = get_rate_limiter(rate_limit, limiter_key) if rate_limit != 0 else None

    # ---------------------------------------------------------------- requests
    def _headers(self, extra=None):
        headers = {
            'Authorization': 'Bearer %s' % self.api_key,
            'User-Agent': self.user_agent,
            'Accept': 'application/json',
        }
        if extra:
            headers.update(extra)
        return headers

    def request(self, method, path, json_body=None, params=None,
                idempotency_key=None, files=None, data=None):
        """Perform an API call, retrying transient failures.

        Returns the decoded JSON answer (dict), or an empty dict for 204.
        """
        attempt = 0
        while True:
            try:
                return self._request_once(
                    method, path, json_body=json_body, params=params,
                    idempotency_key=idempotency_key, files=files, data=data,
                )
            except _RetryRequest as marker:
                attempt += 1
                if attempt > self.max_retries:
                    raise ResendAPIError(
                        marker.message,
                        status_code=marker.status_code,
                        code=marker.code,
                        payload=marker.payload,
                        retryable=True,
                        retry_after=marker.retry_after,
                    )
                self._sleep_before_retry(attempt - 1, marker.retry_after)

    def _request_once(self, method, path, json_body=None, params=None,
                      idempotency_key=None, files=None, data=None):
        if self.limiter is not None:
            self.limiter.acquire()
        url = path if path.startswith('http') else '%s%s' % (self.base_url, path)
        extra_headers = {}
        if idempotency_key:
            extra_headers['Idempotency-Key'] = idempotency_key[:255]
        headers = self._headers(extra_headers)
        if json_body is not None:
            headers['Content-Type'] = 'application/json'
        started = time.monotonic()
        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                files=files,
                data=data,
                timeout=self.timeout,
                verify=self.verify,
            )
        except requests.exceptions.RequestException as exc:
            raise _RetryRequest({}, 'network_error',
                                'Could not reach the Resend API: %s' % exc,
                                None, None) from exc
        elapsed = time.monotonic() - started
        if response.status_code >= 400:
            self._raise_for_response(response, method, url)
        _logger.debug('Resend API %s %s -> %s in %.2fs', method, url,
                      response.status_code, elapsed)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {'raw': response.text}

    def _raise_for_response(self, response, method, url):
        try:
            payload = response.json()
        except ValueError:
            payload = {'message': (response.text or '')[:1000]}
        if not isinstance(payload, dict):
            payload = {'message': str(payload)[:1000]}
        code = payload.get('name') or payload.get('error_code') or payload.get('code')
        message = payload.get('message') or response.reason or 'Resend API error'
        try:
            retry_after = float(response.headers.get('Retry-After'))
        except (TypeError, ValueError):
            retry_after = None
        if code in NON_RETRYABLE_RATE_LIMIT_CODES:
            raise ResendAPIError(
                message, status_code=response.status_code, code=code,
                payload=payload, retryable=False, retry_after=retry_after)
        retryable = (
            response.status_code in RETRYABLE_STATUS_CODES
            or code in RETRYABLE_ERROR_CODES
        )
        if retryable:
            _logger.info('Retrying Resend API %s %s after HTTP %s (%s)',
                         method, url, response.status_code, code)
            raise _RetryRequest(payload, code, message, response.status_code,
                                retry_after)
        raise ResendAPIError(
            message,
            status_code=response.status_code,
            code=code,
            payload=payload,
            retryable=False,
            retry_after=retry_after,
        )

    def _sleep_before_retry(self, attempt, retry_after):
        if retry_after:
            delay = min(float(retry_after), 30.0)
        else:
            delay = min(2.0 ** attempt + random.uniform(0, 0.5), 15.0)
        time.sleep(delay)

    # --------------------------------------------------------------- endpoints
    def send_email(self, payload, idempotency_key=None):
        """POST /emails - send a single email. Returns the API answer (dict)."""
        if not payload.get('to'):
            raise ResendAPIError('No recipient.', code='missing_recipient')
        return self.request('POST', '/emails', json_body=payload,
                            idempotency_key=idempotency_key)

    def send_batch(self, payloads, idempotency_key=None):
        """POST /emails/batch - send up to 100 emails in one call."""
        if not payloads:
            raise ResendAPIError('Empty batch.', code='empty_batch')
        if len(payloads) > 100:
            raise ResendAPIError('Resend batch limit is 100 emails per call.',
                                 code='batch_too_large')
        return self.request('POST', '/emails/batch', json_body=list(payloads),
                            idempotency_key=idempotency_key)

    def get_email(self, email_id):
        return self.request('GET', '/emails/%s' % email_id)

    def list_emails(self, limit=20, after=None, before=None):
        params = {'limit': limit}
        if after:
            params['after'] = after
        if before:
            params['before'] = before
        return self.request('GET', '/emails', params=params)

    # domains
    def list_domains(self, limit=100):
        return self.request('GET', '/domains', params={'limit': limit})

    def get_domain(self, domain_id):
        return self.request('GET', '/domains/%s' % domain_id)

    def create_domain(self, name, region=None):
        body = {'name': name}
        if region:
            body['region'] = region
        return self.request('POST', '/domains', json_body=body)

    def verify_domain(self, domain_id):
        return self.request('POST', '/domains/%s/verify' % domain_id)

    def update_domain(self, domain_id, values):
        return self.request('PATCH', '/domains/%s' % domain_id,
                            json_body=values)

    def delete_domain(self, domain_id):
        return self.request('DELETE', '/domains/%s' % domain_id)

    # inbound / receiving
    def list_received_emails(self, limit=20, after=None, before=None):
        params = {'limit': limit}
        if after:
            params['after'] = after
        if before:
            params['before'] = before
        return self.request('GET', '/emails/receiving', params=params)

    def get_received_email(self, email_id, html_format=None):
        params = {}
        if html_format:
            params['html_format'] = html_format
        return self.request('GET', '/emails/receiving/%s' % email_id,
                            params=params or None)

    def list_received_attachments(self, email_id):
        return self.request('GET',
                            '/emails/receiving/%s/attachments' % email_id)

    def download_received_attachment(self, email_id, attachment_id):
        """Download one inbound attachment and return (bytes, content_type)."""
        url = '%s/emails/receiving/%s/attachments/%s' % (
            self.base_url, email_id, attachment_id)
        headers = self._headers()
        try:
            response = self.session.get(url, headers=headers,
                                        timeout=self.timeout, verify=self.verify)
        except requests.exceptions.RequestException as exc:
            raise ResendAPIError('Could not download attachment: %s' % exc,
                                 retryable=True) from exc
        if response.status_code >= 400:
            raise ResendAPIError(
                'Could not download inbound attachment (HTTP %s)' % response.status_code,
                status_code=response.status_code, retryable=True)
        return response.content, response.headers.get('Content-Type')

    # webhooks management (optional convenience)
    def list_webhooks(self):
        return self.request('GET', '/webhooks')

    def get_webhook(self, webhook_id):
        return self.request('GET', '/webhooks/%s' % webhook_id)

    def update_webhook(self, webhook_id, values):
        return self.request('PATCH', '/webhooks/%s' % webhook_id,
                            json_body=values)

    def delete_webhook(self, webhook_id):
        return self.request('DELETE', '/webhooks/%s' % webhook_id)

    # suppressions
    def list_suppressions(self, limit=100):
        return self.request('GET', '/suppressions', params={'limit': limit})

    def add_suppression(self, email, reason='manual'):
        return self.request('POST', '/suppressions',
                            json_body={'email': email, 'reason': reason})

    def download_url(self, url):
        """Download a pre-signed URL (raw .eml, attachment) without auth."""
        try:
            response = self.session.get(url, timeout=self.timeout,
                                        verify=self.verify)
        except requests.exceptions.RequestException as exc:
            raise ResendAPIError('Could not download the file: %s' % exc,
                                 retryable=True) from exc
        if response.status_code >= 400:
            raise ResendAPIError(
                'Could not download the file (HTTP %s)' % response.status_code,
                status_code=response.status_code, retryable=True)
        return response.content

    def create_webhook(self, endpoint, events):
        return self.request('POST', '/webhooks',
                            json_body={'endpoint': endpoint, 'events': events})


# ---------------------------------------------------------------------------
# Webhook signature verification (Svix)
# ---------------------------------------------------------------------------
def get_header(headers, name):
    """Case insensitive header lookup working with dicts and Odoo headers."""
    if hasattr(headers, 'get'):
        value = headers.get(name)
        if value is not None:
            return value
    lowered = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == lowered:
            return value
    return None


def verify_webhook_signature(body, headers, secrets, tolerance=300, now=None):
    """Verify a Resend webhook signature.

    Resend signs webhooks with Svix: the signed content is
    "<svix-id>.<svix-timestamp>.<raw body>", HMAC-SHA256 with the base64 decoded
    webhook secret (the whsec_ prefix is not part of the key), base64 encoded.
    The svix-signature header contains one or more space separated
    "v1,<signature>" entries.

    :param body: raw request body (bytes)
    :param headers: request headers (dict-like, case insensitive lookup)
    :param secrets: one secret or a list of secrets (base64, with or without the
        whsec_ prefix)
    :param tolerance: maximum accepted age difference in seconds
    :return: the parsed JSON payload
    :raise WebhookVerificationError: when the signature or the payload is invalid
    """
    if isinstance(body, str):
        body = body.encode('utf-8')
    if not body:
        raise WebhookVerificationError('Empty webhook body.')
    msg_id = get_header(headers, 'svix-id')
    timestamp = get_header(headers, 'svix-timestamp')
    signature_header = get_header(headers, 'svix-signature')
    if not msg_id or not timestamp or not signature_header:
        raise WebhookVerificationError(
            'Missing svix-id, svix-timestamp or svix-signature header.')
    try:
        timestamp_int = int(timestamp)
    except (TypeError, ValueError):
        raise WebhookVerificationError('Invalid svix-timestamp header.')
    if now is None:
        now = time.time()
    if tolerance and abs(now - timestamp_int) > tolerance:
        raise WebhookVerificationError(
            'Webhook timestamp is outside the accepted tolerance '
            '(possible replay attack).')

    if isinstance(secrets, str):
        secrets = [secrets]
    secrets = [secret for secret in (secrets or []) if secret]
    if not secrets:
        raise WebhookVerificationError('No webhook signing secret configured.')

    signed_content = ('%s.%s.' % (msg_id, timestamp)).encode('utf-8') + body
    provided = []
    for part in str(signature_header).split():
        if part.startswith('v1,'):
            provided.append(part[3:])
        elif part.startswith('v1a,'):
            provided.append(part[4:])
    if not provided:
        raise WebhookVerificationError('No v1 signature in svix-signature header.')

    for secret in secrets:
        key = secret.strip()
        if key.startswith('whsec_'):
            key = key[len('whsec_'):]
        try:
            key_bytes = base64.b64decode(key, validate=True)
        except (ValueError, TypeError):
            raise WebhookVerificationError('Webhook secret is not valid base64.')
        expected = base64.b64encode(
            hmac.new(key_bytes, signed_content, hashlib.sha256).digest()
        ).decode('ascii')
        for candidate in provided:
            if hmac.compare_digest(expected, candidate):
                try:
                    parsed = json.loads(body.decode('utf-8'))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise WebhookVerificationError(
                        'Webhook body is not valid JSON: %s' % exc)
                if not isinstance(parsed, dict):
                    raise WebhookVerificationError('Webhook body is not an object.')
                parsed['_svix_id'] = msg_id
                parsed['_svix_timestamp'] = timestamp_int
                return parsed
    raise WebhookVerificationError('Invalid webhook signature.')