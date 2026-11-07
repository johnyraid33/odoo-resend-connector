# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Configuration, Resend client factory and webhook signature checks.

The models read their settings through this service, so the values are loaded
once per request and the API key handling stays in one place.
"""

import hashlib
import logging
import threading

from odoo import _, api, models

from .compat import (
    IS_ODOO_20,
    param_get_bool,
    param_get_float,
    param_get_int,
    param_get_str,
)
from .resend_client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    ResendAPIError,
    ResendClient,
    WebhookVerificationError,
    verify_webhook_signature,
)

_logger = logging.getLogger(__name__)

#: ir.config_parameter keys used by the module.
P_ENABLED = 'resend.enabled'
P_API_KEY = 'resend.api_key'
P_FROM_EMAIL = 'resend.from_email'
P_FROM_MODE = 'resend.from_mode'          # 'off' | 'unverified' | 'always'
P_WEBHOOK_SECRET = 'resend.webhook_secret'
P_BASE_URL = 'resend.base_url'
P_TIMEOUT = 'resend.timeout'
P_WEBHOOK_TOLERANCE = 'resend.webhook_tolerance'
P_WEBHOOK_ALLOW_UNSIGNED = 'resend.webhook_allow_unsigned'
P_INLINE_MODE = 'resend.inline_images_mode'   # 'cid' | 'data_uri' | 'attachment'
P_RATE_LIMIT = 'resend.rate_limit'            # requests per second (team-wide)
P_PRESERVE_MESSAGE_ID = 'resend.preserve_message_id'
P_STORE_PAYLOADS = 'resend.store_webhook_payloads'
P_SAVE_ORIGINAL = 'resend.save_inbound_original'
P_AUTO_RETRY = 'resend.auto_retry_max'
P_RETRY_DELAY = 'resend.retry_delay_minutes'
P_PUBLIC_BASE_URL = 'resend.public_base_url'
P_LAST_DOMAIN_SYNC = 'resend.last_domain_sync'
P_VERIFIED_DOMAINS = 'resend.verified_domains'

_SESSION_CACHE = {}
_SESSION_LOCK = threading.Lock()

#: Access rules applied from code: Odoo 20 stores them in ir.access (operation),
#: Odoo 19 in ir.model.access (perm_* columns), so a single CSV cannot work on
#: both versions. The rules are ([model], [group xmlid], [crud operations]).
ACCESS_RULES = [
    ('resend.email', 'rex_resend_connector.group_resend_manager', 'crud'),
    ('resend.email.event', 'rex_resend_connector.group_resend_manager', 'r'),
    ('resend.domain', 'rex_resend_connector.group_resend_manager', 'crud'),
    ('resend.domain.record', 'rex_resend_connector.group_resend_manager', 'r'),
    ('resend.webhook.event', 'rex_resend_connector.group_resend_manager', 'r'),
    ('resend.inbound.email', 'rex_resend_connector.group_resend_manager', 'crud'),
    ('resend.connector', 'base.group_user', 'r'),
    ('resend.connector', 'rex_resend_connector.group_resend_manager', 'crud'),
    ('resend.test.mail', 'base.group_user', 'crud'),
]


class ResendConnector(models.AbstractModel):
    _name = 'resend.connector'
    _description = 'Resend API Connector'
    _rec_name = 'id'

    # ------------------------------------------------------------ access rules
    @api.model
    def _ensure_access_rights(self):
        """Create the module ACLs for the running Odoo version (idempotent).

        Must be called after the security XML: the manager group is created by
        the data files.
        """
        if not self.env.ref('rex_resend_connector.group_resend_manager', raise_if_not_found=False):
            return False
        if IS_ODOO_20:
            Access = self.env['ir.access'].sudo()
        else:
            Access = self.env['ir.model.access'].sudo()
        Model = self.env['ir.model'].sudo()
        for model_name, group_xmlid, operation in ACCESS_RULES:
            group = self.env.ref(group_xmlid, raise_if_not_found=False)
            model = Model.search([('model', '=', model_name)], limit=1)
            if not group or not model:
                continue
            rule = Access.search([
                ('model_id', '=', model.id),
                ('group_id', '=', group.id),
            ], limit=1)
            if IS_ODOO_20:
                values = {'name': '%s %s' % (model_name, group.name), 'operation': operation}
            else:
                values = {
                    'name': '%s %s' % (model_name, group.name),
                    'perm_read': 'r' in operation,
                    'perm_write': 'u' in operation,
                    'perm_create': 'c' in operation,
                    'perm_unlink': 'd' in operation,
                }
            if rule:
                rule.write(values)
            else:
                Access.create(dict(values, model_id=model.id, group_id=group.id))
        return True

    # ------------------------------------------------------------------ config
    @api.model
    def _get_config(self, server=None):
        """Return the effective configuration as a plain dict.

        A specific ir.mail_server may override the API key / from address; the
        rest comes from the system parameters (Settings > Discuss > Resend).

        The result is memoized on the current environment: a single request (or
        a mass mailing batch) reads the parameters once, not once per email.
        """
        cache = getattr(self.env, '_resend_config_cache', None)
        if cache is None:
            cache = self.env._resend_config_cache = {}
        cache_key = server.id if server else 0
        if cache_key not in cache:
            cache[cache_key] = self._compute_config(server=server)
        return cache[cache_key]

    @api.model
    def _compute_config(self, server=None):
        ICP = self.env['ir.config_parameter'].sudo()
        api_key = (server.resend_api_key if server else '') or param_get_str(ICP, P_API_KEY)
        from_email = (server.resend_from_email if server else '') or param_get_str(ICP, P_FROM_EMAIL)
        return {
            'rate_limit': param_get_float(ICP, P_RATE_LIMIT, 8.0) or 8.0,
            'enabled': param_get_bool(ICP, P_ENABLED),
            'api_key': api_key,
            'from_email': from_email,
            'from_mode': param_get_str(ICP, P_FROM_MODE) or 'unverified',
            'webhook_secret': param_get_str(ICP, P_WEBHOOK_SECRET),
            'webhook_allow_unsigned': param_get_bool(ICP, P_WEBHOOK_ALLOW_UNSIGNED),
            'base_url': param_get_str(ICP, P_BASE_URL) or DEFAULT_BASE_URL,
            'timeout': param_get_int(ICP, P_TIMEOUT, DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT,
            'webhook_tolerance': param_get_int(ICP, P_WEBHOOK_TOLERANCE, 300) or 300,
            'inline_images_mode': param_get_str(ICP, P_INLINE_MODE) or 'cid',
            'preserve_message_id': param_get_bool(ICP, P_PRESERVE_MESSAGE_ID),
            'store_payloads': param_get_bool(ICP, P_STORE_PAYLOADS, default=True),
            'save_inbound_original': param_get_bool(ICP, P_SAVE_ORIGINAL),
            'auto_retry_max': param_get_int(ICP, P_AUTO_RETRY, 0),
            'retry_delay_minutes': param_get_int(ICP, P_RETRY_DELAY, 60) or 60,
            'public_base_url': param_get_str(ICP, P_PUBLIC_BASE_URL),
            'verified_domains': [d.strip().lower() for d in param_get_str(ICP, P_VERIFIED_DOMAINS).split(',') if d.strip()],
        }

    @api.model
    def _is_enabled(self, server=None):
        """Whether mail sent through *server* must go through the Resend API.

        Both conditions are required: the global master switch and the per-server
        opt-in. This keeps a simple kill switch (turn the global option off and
        every server instantly goes back to SMTP) without hidden surprises.
        """
        if server is not None and not server.resend_enabled:
            return False
        # Note: a missing API key still counts as "active" so that the send
        # fails loudly instead of silently falling back to SMTP.
        return self._get_config(server=server)['enabled']

    @api.model
    def _get_session(self, config):
        """Return a shared requests.Session per (database, base url, api key)."""
        dbname = self.env.cr.dbname
        key = (dbname, config['base_url'], hashlib.sha256(config['api_key'].encode()).hexdigest()[:16])
        with _SESSION_LOCK:
            session = _SESSION_CACHE.get(key)
            if session is None:
                import requests
                session = requests.Session()
                _SESSION_CACHE[key] = session
                if len(_SESSION_CACHE) > 16:
                    _SESSION_CACHE.pop(next(iter(_SESSION_CACHE)))
            return session

    @api.model
    def _get_client(self, server=None, config=None):
        config = config or self._get_config(server=server)
        if not config['api_key']:
            raise ResendAPIError(
                _('Resend is not configured: please set an API key in '
                  'Settings > Discuss > Resend.'),
                code='missing_api_key',
            )
        return ResendClient(
            config['api_key'],
            base_url=config['base_url'],
            timeout=config['timeout'],
            session=self._get_session(config),
            rate_limit=config['rate_limit'],
        )

    @api.model
    def _get_webhook_url(self):
        config = self._get_config()
        base = config['public_base_url'] or param_get_str(
            self.env['ir.config_parameter'].sudo(), 'web.base.url')
        return '%s/resend/webhook' % (base or '').rstrip('/')

    # ----------------------------------------------------------------- webhook
    @api.model
    def _verify_webhook(self, body, headers):
        """Verify and parse an incoming Resend webhook.

        Raises WebhookVerificationError when the signature cannot be validated.
        """
        config = self._get_config()
        secrets = [s.strip() for s in (config['webhook_secret'] or '').split(',') if s.strip()]
        if not secrets:
            if config['webhook_allow_unsigned']:
                import json
                _logger.warning(
                    'Resend webhook received without signing secret: signature '
                    'verification is disabled (developer mode only).')
                payload = json.loads(body.decode('utf-8') if isinstance(body, bytes) else body)
                payload['_svix_id'] = headers.get('svix-id') or ''
                return payload
            raise WebhookVerificationError(
                _('No Resend webhook signing secret configured.'))
        return verify_webhook_signature(
            body, headers, secrets, tolerance=config['webhook_tolerance'])

    # --------------------------------------------------------------- utilities
    @api.model
    def _get_verified_domains(self):
        return self.env['resend.domain'].sudo().search([
            ('status', '=', 'verified'),
        ]).mapped('name')

    @api.model
    def _domain_is_verified(self, email):
        """Whether the domain of an email address is verified in Resend."""
        from odoo.tools.mail import email_domain_extract, email_normalize
        domain = email_domain_extract(email_normalize(email) or email)
        if not domain:
            return False
        config = self._get_config()
        return domain in config['verified_domains']

    @api.model
    def _invalidate_config_cache(self):
        if hasattr(self.env, '_resend_config_cache'):
            del self.env._resend_config_cache

    @api.model
    def _check_connection(self):
        """Validate the API key and return a small summary (for the test button)."""
        client = self._get_client()
        domains = client.list_domains(limit=100)
        data = domains.get('data') if isinstance(domains, dict) else None
        return {
            'ok': True,
            'domains_count': len(data or []),
            'verified': [d.get('name') for d in (data or []) if d.get('status') == 'verified'],
        }