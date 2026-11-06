# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Webhook calls received from Resend.

Every call is stored once (the Svix id is unique) and then dispatched. Delivery
events update the tracking row, email.received imports the message into Odoo.
"""

import json
import logging

from odoo import api, fields, models

from .resend_client import ResendAPIError

_logger = logging.getLogger(__name__)

#: Events changing the delivery state of an outbound email.
DELIVERY_EVENTS = (
    'email.sent', 'email.scheduled', 'email.delivered',
    'email.delivery_delayed', 'email.opened', 'email.clicked', 'email.bounced',
    'email.complained', 'email.failed', 'email.suppressed',
)

#: Events subscribed when the webhook is created automatically. The list must
#: only contain events Resend accepts. email.accepted is a local state of the
#: module, not a webhook event of Resend.
WEBHOOK_EVENTS = list(DELIVERY_EVENTS) + ['email.received']


class ResendWebhookEvent(models.Model):
    _name = 'resend.webhook.event'
    _description = 'Resend Webhook Event'
    _order = 'id desc'

    #: Events the module subscribes to when the webhook is created automatically.
    WEBHOOK_EVENTS = WEBHOOK_EVENTS

    svix_id = fields.Char(
        string='Svix ID', required=True, index='btree', copy=False,
        help='Unique identifier of the webhook call, used to ignore Resend retries.')
    event_type = fields.Char(string='Event Type', required=True, index=True)
    event_date = fields.Datetime(string='Event Date', index=True)
    received_date = fields.Datetime(
        string='Received', default=fields.Datetime.now, readonly=True)
    state = fields.Selection([
        ('queued', 'Queued'),
        ('processed', 'Processed'),
        ('ignored', 'Ignored'),
        ('failed', 'Failed'),
    ], string='Status', default='queued', index=True)
    attempts = fields.Integer(string='Attempts', default=0, readonly=True)
    error = fields.Text(string='Error', readonly=True)
    payload = fields.Text(string='Payload', readonly=True)
    email_id = fields.Many2one(
        'resend.email', string='Outbound Email', ondelete='set null', readonly=True)
    inbound_email_id = fields.Many2one(
        'resend.inbound.email', string='Inbound Email', ondelete='set null',
        readonly=True)

    _svix_id_uniq = models.Constraint(
        'UNIQUE(svix_id)',
        'This webhook event was already processed (duplicate svix-id).')

    # ------------------------------------------------------------------ helpers
    @api.model
    def _parse_event_date(self, value):
        if not value:
            return False
        try:
            return fields.Datetime.to_datetime(value)
        except (TypeError, ValueError):
            return False

    @api.model
    def _store_event(self, payload):
        """Create (or fetch) the event row for a verified webhook payload.

        Returns a tuple (event, created). Duplicated svix-id are ignored so that
        Resend retries never apply an event twice.
        """
        svix_id = payload.get('_svix_id') or ''
        event_type = payload.get('type') or ''
        if not svix_id:
            raise ValueError('Webhook without svix-id header.')
        existing = self.sudo().search([('svix_id', '=', svix_id)], limit=1)
        if existing:
            return existing, False
        config = self.env['resend.connector']._get_config()
        store_payload = config['store_payloads'] or event_type == 'email.received'
        try:
            with self.env.cr.savepoint():
                event = self.sudo().create({
                    'svix_id': svix_id,
                    'event_type': event_type,
                    'event_date': self._parse_event_date(payload.get('created_at')),
                    'payload': json.dumps(payload, default=str)[:2000000]
                               if store_payload else False,
                })
            return event, True
        except Exception:  # noqa: BLE001 - unique violation on a concurrent retry
            existing = self.sudo().search([('svix_id', '=', svix_id)], limit=1)
            if existing:
                return existing, False
            raise

    def _dispatch(self):
        """Process the event. Returns True when the event is fully handled."""
        self = self.sudo()
        self.ensure_one()
        payload = json.loads(self.payload) if self.payload else {}
        if not payload:
            # Payloads are optional in the database (privacy / storage), the
            # controller passes the freshly verified payload through the context.
            payload = self.env.context.get('resend_payload') or {}
        data = payload.get('data') or {}
        event_type = self.event_type
        self.write({'attempts': self.attempts + 1})
        if event_type == 'email.received':
            inbound = self.env['resend.inbound.email'].sudo()._receive_from_event(data)
            self.write({'state': 'processed', 'inbound_email_id': inbound.id, 'error': False})
            return True
        if event_type in DELIVERY_EVENTS:
            return self._dispatch_delivery(event_type, data)
        self.write({'state': 'ignored'})
        return True

    def _dispatch_delivery(self, event_type, data):
        ResendEmail = self.env['resend.email'].sudo()
        resend_message_id = data.get('email_id') or ''
        email = ResendEmail._find_by_resend_id(resend_message_id)
        if not email:
            # The email may have been sent by another system using the same key,
            # or the webhook arrived before the send transaction committed.
            _logger.info('Resend webhook %s for unknown email id %s',
                         event_type, resend_message_id)
            self.write({'state': 'ignored', 'error': 'Unknown Resend email id'})
            return True
        # email.suppressed means "never attempted": record it as a failure
        # Resend does not fire email.sent for a suppressed message.
        if event_type == 'email.suppressed':
            suppressed = data.get('suppressed') or {}
            data = dict(data, message=suppressed.get('message') or 'Suppressed address',
                        error_code=suppressed.get('type') or 'suppressed')
            event_type = 'email.failed'
        email._apply_event(event_type, data)
        self.env['resend.email.event'].sudo().create({
            'email_id': email.id,
            'resend_message_id': resend_message_id,
            'event_type': event_type,
            'event_date': self.event_date or fields.Datetime.now(),
            'click_url': (data.get('click') or {}).get('link'),
            'bounce_type': (data.get('bounce') or {}).get('type'),
            'bounce_code': '; '.join((data.get('bounce') or {}).get('diagnosticCode') or []) or False,
            'bounce_message': (data.get('bounce') or {}).get('message')
                              or (data.get('failed') or {}).get('reason')
                              or (data.get('suppressed') or {}).get('message'),
            'payload': json.dumps(data, default=str)[:1000000] if self.payload else False,
            'webhook_event_id': self.id,
        })
        # Keep the RFC Message-Id Resend really used: it replaces the one built
        # by Odoo and it is what replies will reference (see _rewrite_references).
        message_id = data.get('message_id')
        if message_id:
            email.write({'actual_message_id': message_id})
        self.write({'state': 'processed', 'email_id': email.id, 'error': False})
        return True

    # ------------------------------------------------------------------- crons
    @api.model
    def _cron_process_queued(self, limit=100, event_ids=None):
        """Retry events that could not be processed by the webhook request.

        Covers inbound emails waiting for the API as well as delivery events
        that hit a concurrency conflict (Resend sends several events for the
        same email at once).
        """
        domain = [
            ('state', 'in', ('queued', 'failed')),
            ('attempts', '<', 10),
        ]
        if event_ids:
            domain.append(('id', 'in', list(event_ids)))
        events = self.sudo().search(domain, limit=limit, order='id asc')
        for event in events:
            try:
                with self.env.cr.savepoint():
                    event._dispatch()
            except Exception as exc:  # noqa: BLE001
                _logger.warning('Resend: could not process webhook event %s: %s',
                                event.svix_id, exc, exc_info=True)
                event.write({'state': 'queued', 'error': str(exc)[:2000]})
                if event.attempts >= 10:
                    event.write({'state': 'failed'})
        return True


class ResendInboundEmail(models.Model):
    _name = 'resend.inbound.email'
    _description = 'Resend Inbound Email'
    _order = 'id desc'
    _rec_name = 'subject'

    resend_email_id = fields.Char(
        string='Resend Email ID', required=True, index='btree', copy=False)
    message_id = fields.Char(string='Message-Id', index='btree')
    from_email = fields.Char(string='From')
    to_emails = fields.Text(string='To')
    cc_emails = fields.Text(string='Cc')
    subject = fields.Char(string='Subject')
    received_date = fields.Datetime(string='Received', default=fields.Datetime.now)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('processed', 'Processed'),
        ('unrouted', 'No Route'),
        ('failed', 'Failed'),
        ('duplicate', 'Duplicate'),
    ], string='Status', default='pending', index=True)
    error = fields.Text(string='Error', readonly=True)
    mail_message_id = fields.Many2one(
        'mail.message', string='Odoo Message', ondelete='set null', readonly=True)
    attachment_count = fields.Integer(string='Attachments', readonly=True)
    raw_size = fields.Integer(string='Size (bytes)', readonly=True)

    _resend_email_id_uniq = models.Constraint(
        'UNIQUE(resend_email_id)', 'This inbound email was already imported.')

    # ------------------------------------------------------------------ fetching
    def _get_client(self):
        return self.env['resend.connector']._get_client()

    def _receive_from_event(self, data, event=None):
        """Create/fetch the inbound record from a (verified) event payload."""
        email_id = (data or {}).get('email_id')
        record = self.sudo().search([('resend_email_id', '=', email_id)], limit=1)
        if record:
            if record.state == 'pending':
                record._fetch_and_process()
            return record
        record = self.sudo().create({
            'resend_email_id': email_id,
            'message_id': (data or {}).get('message_id') or False,
            'from_email': (data or {}).get('from') or '',
            'to_emails': ', '.join((data or {}).get('to') or []),
            'cc_emails': ', '.join((data or {}).get('cc') or []),
            'subject': (data or {}).get('subject') or '',
            'received_date': fields.Datetime.now(),
            'attachment_count': len((data or {}).get('attachments') or []),
        })
        record._fetch_and_process()
        return record

    def _fetch_and_process(self):
        """Fetch the raw MIME message and hand it to Odoo's mail gateway."""
        self.ensure_one()
        try:
            raw, meta = self._fetch_raw_message()
        except ResendAPIError as exc:
            self.write({'state': 'pending', 'error': str(exc)[:2000]})
            raise
        except Exception as exc:  # noqa: BLE001
            self.write({'state': 'failed', 'error': str(exc)[:2000]})
            _logger.warning('Resend: failed to fetch inbound email %s: %s',
                            self.resend_email_id, exc, exc_info=True)
            return False
        # The raw MIME is authoritative for the Message-Id (the metadata of the
        # receiving API may be missing or lag behind).
        raw_message_id = False
        try:
            from email import message_from_bytes
            raw_message_id = (message_from_bytes(raw).get('Message-ID') or '').strip() or False
        except Exception:  # noqa: BLE001
            raw_message_id = False
        effective_message_id = raw_message_id or meta.get('message_id') or self.message_id
        self.write({
            'raw_size': len(raw or b''),
            'message_id': effective_message_id,
            'from_email': meta.get('from') or self.from_email,
            'subject': meta.get('subject') or self.subject,
            'to_emails': ', '.join(meta.get('to') or []) or self.to_emails,
            'cc_emails': ', '.join(meta.get('cc') or []) or self.cc_emails,
        })
        raw = self._rewrite_references(raw)
        try:
            with self.env.cr.savepoint():
                MailThread = self.env['mail.thread'].sudo().with_context(
                    resend_inbound_email_id=self.id)
                MailThread.message_process(
                    None,
                    raw,
                    save_original=self.env['resend.connector']._get_config()['save_inbound_original'],
                )
        except ValueError as exc:
            # No alias / no thread matched the recipient: keep the email for an
            # administrator instead of failing the webhook forever.
            self.write({'state': 'unrouted', 'error': str(exc)[:2000]})
            _logger.info('Resend: inbound email %s could not be routed: %s',
                         self.resend_email_id, exc)
            return False
        except Exception as exc:  # noqa: BLE001
            self.write({'state': 'failed', 'error': str(exc)[:2000]})
            _logger.warning('Resend: inbound email %s failed: %s',
                            self.resend_email_id, exc, exc_info=True)
            raise
        message = self.env['mail.message'].sudo().search(
            [('message_id', '=', effective_message_id)], limit=1) if effective_message_id \
            else self.env['mail.message']
        self.write({'state': 'processed', 'mail_message_id': message.id or False,
                    'error': False})
        return True

    def _rewrite_references(self, raw):
        """Map Resend Message-Ids back to the Odoo ones.

        Resend always assigns its own Message-Id, so a reply carries an
        In-Reply-To / References that Odoo has never seen. The tracking rows know
        the mapping, so the raw MIME is patched in-place (byte replacement keeps
        every other header, encoding and attachment untouched).
        """
        if not raw:
            return raw
        from email import message_from_bytes
        import re
        try:
            message = message_from_bytes(raw)
        except Exception:  # noqa: BLE001
            return raw
        references = ' '.join(filter(None, [
            message.get('In-Reply-To') or '',
            message.get('References') or '',
        ]))
        ids = re.findall(r'<[^<>\s]+>', references)
        if not ids:
            return raw
        mappings = self.env['resend.email'].sudo().search([
            ('actual_message_id', 'in', ids),
            ('rfc_message_id', '!=', False),
        ])
        for tracking in mappings:
            raw = raw.replace(
                tracking.actual_message_id.encode('utf-8'),
                tracking.rfc_message_id.encode('utf-8'))
        if mappings:
            _logger.info('Resend: mapped %s reference(s) of inbound email %s',
                         len(mappings), self.resend_email_id)
        return raw

    def action_open_message(self):
        self.ensure_one()
        if not self.mail_message_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': self.mail_message_id.subject or self.subject,
            'res_model': 'mail.message',
            'res_id': self.mail_message_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_reprocess(self):
        self.ensure_one()
        self.write({'state': 'pending', 'error': False})
        self._fetch_and_process()
        return True

    def _fetch_raw_message(self):
        """Return (raw bytes, metadata). Uses the signed .eml download URL."""
        client = self._get_client()
        meta = client.get_received_email(self.resend_email_id)
        raw = None
        download_url = ((meta or {}).get('raw') or {}).get('download_url')
        if download_url:
            raw = client.download_url(download_url)
        if not raw:
            raw = self._build_message_from_metadata(client, meta)
        return raw, meta or {}

    def _build_message_from_metadata(self, client, meta):
        """Fallback when the raw .eml is not available (older emails)."""
        from email.message import EmailMessage
        from email.utils import formatdate

        headers = {str(k).lower(): v for k, v in (meta.get('headers') or {}).items()}
        message = EmailMessage()
        message['From'] = headers.get('from') or meta.get('from') or ''
        message['To'] = ', '.join(meta.get('to') or [])
        if meta.get('cc'):
            message['Cc'] = ', '.join(meta.get('cc'))
        message['Subject'] = meta.get('subject') or ''
        message['Date'] = formatdate(localtime=True)
        if meta.get('message_id'):
            message['Message-Id'] = meta['message_id']
        for name in ('in-reply-to', 'references', 'reply-to'):
            if headers.get(name):
                message[name.title()] = headers[name]
        html_body = meta.get('html')
        text_body = meta.get('text')
        if html_body:
            message.set_content(text_body or '', subtype='plain', charset='utf-8')
            message.add_alternative(html_body, subtype='html', charset='utf-8')
        else:
            message.set_content(text_body or '', subtype='plain', charset='utf-8')
        for attachment in meta.get('attachments') or []:
            try:
                content, _ctype = client.download_received_attachment(
                    self.resend_email_id, attachment.get('id'))
            except ResendAPIError:
                _logger.warning('Resend: could not download attachment %s',
                                attachment.get('id'))
                continue
            message.add_attachment(
                content,
                maintype=(attachment.get('content_type') or 'application/octet-stream').split('/')[0],
                subtype=(attachment.get('content_type') or 'application/octet-stream').split('/')[-1],
                filename=attachment.get('filename') or 'attachment',
                cid=attachment.get('content_id') or None,
            )
        return message.as_bytes()