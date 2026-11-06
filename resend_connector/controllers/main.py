# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
HTTP endpoints: the signed Resend webhook, a probe and the documentation page.
"""

import logging
import time

from psycopg2 import errors as pg_errors

from odoo import http
from odoo.http import request

from ..models.compat import param_get_str
from ..models.resend_client import ResendAPIError, WebhookVerificationError

_logger = logging.getLogger(__name__)

#: Hard cap on the webhook body size (webhook payloads are small; inbound
#: content is fetched through the API, never through the webhook).
MAX_WEBHOOK_BODY = 5 * 1024 * 1024


class ResendController(http.Controller):

    # ----------------------------------------------------------------- webhook
    @http.route('/resend/webhook', type='http', auth='none', methods=['POST'],
                csrf=False, save_session=False)
    def resend_webhook(self, **kwargs):
        """Receive a Resend webhook (Svix signed) and dispatch it.

        Responds 200 as soon as the event is safely stored. Processing happens
        right away when possible; inbound emails that cannot be fetched now are
        retried by the cron job, so a Resend retry storm is never triggered by a
        transient API problem.
        """
        raw_body = request.httprequest.get_data(cache=False)
        if len(raw_body) > MAX_WEBHOOK_BODY:
            _logger.warning('Resend webhook rejected: body too large (%s bytes)',
                            len(raw_body))
            return request.make_json_response({'ok': False, 'error': 'body too large'},
                                              status=413)
        env = request.env
        try:
            payload = env['resend.connector']._verify_webhook(
                raw_body, request.httprequest.headers)
        except WebhookVerificationError as exc:
            _logger.warning('Resend webhook rejected: %s', exc)
            return request.make_json_response(
                {'ok': False, 'error': 'invalid signature'}, status=401)
        except Exception:  # noqa: BLE001 - never leak a traceback to the caller
            _logger.exception('Resend webhook: unexpected verification error')
            return request.make_json_response(
                {'ok': False, 'error': 'invalid request'}, status=400)

        try:
            event, created = env['resend.webhook.event'].sudo()._store_event(payload)
        except Exception:  # noqa: BLE001
            _logger.exception('Resend webhook: could not store the event')
            return request.make_json_response({'ok': False}, status=500)
        if not created and event.state in ('processed', 'ignored'):
            return request.make_json_response({'ok': True, 'duplicate': True})

        # Persist the event first: a crash while processing must not lose it.
        env.cr.commit()
        event_id = event.id
        concurrency_errors = (
            pg_errors.SerializationFailure,
            pg_errors.DeadlockDetected,
            pg_errors.LockNotAvailable,
        )
        for attempt in range(4):
            event = env['resend.webhook.event'].sudo().browse(event_id)
            try:
                event.with_context(resend_payload=payload)._dispatch()
                env.cr.commit()
                return request.make_json_response({'ok': True})
            except concurrency_errors as exc:
                # Resend sends several events for the same email at once: the
                # competing transaction usually commits in milliseconds, so retry
                # immediately against fresh state instead of losing the event.
                env.cr.rollback()
                event.write({'state': 'queued', 'error': str(exc)[:2000]})
                env.cr.commit()
                _logger.info('Resend webhook %s: concurrent update, retry %s',
                             event.svix_id, attempt + 1)
                time.sleep(0.1 * (attempt + 1))
            except ResendAPIError as exc:
                # Transient Resend/API problem: the cron will retry, acknowledge
                # now so that Resend does not disable the endpoint.
                _logger.warning('Resend webhook: deferred processing of %s: %s',
                                event.svix_id, exc)
                env.cr.rollback()
                event.write({'state': 'queued', 'error': str(exc)[:2000]})
                env.cr.commit()
                return request.make_json_response({'ok': True})
            except Exception as exc:  # noqa: BLE001
                _logger.exception('Resend webhook: processing of %s failed', event.svix_id)
                env.cr.rollback()
                event.write({'state': 'failed', 'error': str(exc)[:2000]})
                env.cr.commit()
                return request.make_json_response({'ok': True})
        # Still conflicting: ask Resend to retry later (5xx is retried).
        return request.make_json_response(
            {'ok': False, 'error': 'temporarily busy'}, status=503)

    @http.route('/resend/webhook', type='http', auth='none', methods=['GET'],
                csrf=False, save_session=False)
    def resend_webhook_probe(self, **kwargs):
        """Non-mutating answer for connectivity checks of the webhook URL."""
        return request.make_json_response({'ok': True, 'service': 'resend-webhook'})

    @http.route('/resend/health', type='http', auth='none', methods=['GET'],
                csrf=False, save_session=False)
    def resend_health(self, **kwargs):
        """Public ping used to check that the webhook URL is reachable."""
        from odoo import fields
        return request.make_json_response({
            'ok': True,
            'service': 'odoo-resend-connector',
            'utc': fields.Datetime.to_string(fields.Datetime.now()),
        })

    # ----------------------------------------------------------- documentation
    @http.route('/resend/documentation', type='http', auth='user', website=False)
    def resend_documentation(self, **kwargs):
        connector = request.env['resend.connector']
        config = connector._get_config()
        base_url = param_get_str(request.env['ir.config_parameter'].sudo(), 'web.base.url')
        values = {
            'config': config,
            'webhook_url': connector._get_webhook_url(),
            'base_url': base_url,
            'domains': request.env['resend.domain'].sudo().search([]),
            'is_admin': request.env.user.has_group('base.group_system'),
        }
        return request.render('resend_connector.documentation_page', values)