# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Resend transport for ir.mail_server.

Odoo opens an SMTP connection in _connect__ and hands the built message to
send_email. The module returns a small session object instead of a socket and
converts the message into a Resend payload, so templates, attachments, Bcc,
retries and failure handling keep working as before.

Entry points:
  _connect__   returns a ResendSession for servers marked with Use Resend API
  send_email   posts the message to the Resend API and records the tracking row
"""

import base64
import hashlib
import logging
from email.utils import formataddr, getaddresses, parseaddr

from odoo import _, api, fields, models, tools

from .compat import param_get_str
from .resend_client import ResendAPIError, build_idempotency_key

_logger = logging.getLogger(__name__)

#: Resend refuses emails above 40 MB (base64 encoded total).
_MAX_TOTAL_SIZE = 40 * 1024 * 1024
#: A single attachment is limited to 25 MB by the Resend dashboard limits.
_MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024

#: File extensions the Resend API refuses when sending (security policy).
BLOCKED_ATTACHMENT_EXTENSIONS = frozenset("""
    adp app asp bas bat cer chm cmd com cpl crt csh der exe fxp gadget hlp hta
    inf ins isp its js jse ksh lib lnk mad maf mag mam maq mar mas mat mau mav
    maw mda mdb mde mdt mdw mdz msc msh msh1 msh2 mshxml msh1xml msh2xml msi
    msp mst ops pcd pif plg prf prg reg scf scr sct shb shs sys ps1 ps1xml ps2
    ps2xml psc1 psc2 tmp url vb vbe vbs vps vsmacros vss vst vsw vxd ws wsc
    wsf wsh xnk
""".split())

#: Headers handled explicitly by the Resend payload and therefore not forwarded.
_MANAGED_HEADERS = {
    'from', 'to', 'cc', 'bcc', 'subject', 'date', 'message-id', 'reply-to',
    'return-path', 'sender', 'mime-version', 'content-type',
    'content-transfer-encoding', 'content-disposition', 'dkim-signature',
    'received', 'x-forge-to', 'x-msg-cc-add', 'x-msg-to-add',
    'x-msg-to-consolidate', 'precedence', 'errors-to',
}


class ResendSession:
    """Stand-in for the smtplib.SMTP connection used by Odoo.

    mail.mail.send() opens one connection per mail-server batch and passes it to
    ir.mail_server.send_email(). Returning this object keeps all the Odoo logic
    (from_filter, _prepare_email_message__, Bcc stripping, failure handling)
    working while guaranteeing that no SMTP socket is opened.
    """

    def __init__(self, server, config, smtp_from=None):
        self.server = server
        self.config = config
        self.smtp_from = smtp_from
        self.from_filter = server.from_filter if server else False
        self.mail_server_name = server.display_name if server else 'Resend'
        self.is_resend = True

    def quit(self):
        return True

    def close(self):
        return True

    def send_message(self, *args, **kwargs):
        raise NotImplementedError('A Resend session does not speak SMTP.')


class IrMailServer(models.Model):
    _inherit = 'ir.mail_server'

    resend_enabled = fields.Boolean(
        string='Use Resend API',
        help="Send the emails routed to this server through the Resend HTTP API "
             "instead of SMTP. The global switch (Settings > Discuss > Resend) "
             "must also be enabled.",
    )
    resend_api_key = fields.Char(
        string='Resend API Key',
        groups='base.group_system',
        help="Optional per-server API key. Leave empty to use the global one "
             "(Settings > Discuss > Resend).",
    )
    resend_from_email = fields.Char(
        string='Resend From Address',
        help="Optional per-server From address on a Resend verified domain. "
             "Leave empty to use the global setting.",
    )
    resend_last_check = fields.Datetime(string='Last Resend Check', readonly=True, copy=False)
    resend_last_error = fields.Text(string='Last Resend Error', readonly=True, copy=False)

    # ------------------------------------------------------------------ helpers
    @api.model
    def _resend_find_server(self, mail_server_id=None, host=None, smtp_from=None):
        """Resolve which ir.mail_server would be used, mirroring _connect__()."""
        if mail_server_id:
            return self.sudo().browse(mail_server_id).exists()
        if host:
            return self.browse()
        server, _email_from = self.sudo()._find_mail_server(smtp_from)
        return server

    @api.model
    def _resend_is_active(self, server):
        """True when *server* must be delivered through the Resend API."""
        if not server or not server.resend_enabled:
            return False
        return self.env['resend.connector']._is_enabled(server=server)

    @api.model
    def _resend_check_ready(self, server):
        """Return the effective config or raise a clear error when incomplete."""
        config = self.env['resend.connector']._get_config(server=server)
        if not config['api_key']:
            raise ResendAPIError(
                _('Resend is enabled for the outgoing server "%(server)s" but no '
                  'API key is configured.', server=server.display_name),
                code='missing_api_key',
            )
        return config

    # ------------------------------------------------------------- SMTP session
    def _connect__(self, host=None, port=None, user=None, password=None,
                   encryption=None, smtp_from=None, ssl_certificate=None,
                   ssl_private_key=None, smtp_debug=False, mail_server_id=None,
                   allow_archived=False):
        server = self._resend_find_server(
            mail_server_id=mail_server_id, host=host, smtp_from=smtp_from)
        if self._resend_is_active(server):
            if self._disable_send():
                return None
            config = self._resend_check_ready(server)
            return ResendSession(server, config, smtp_from=smtp_from)
        return super()._connect__(
            host=host, port=port, user=user, password=password,
            encryption=encryption, smtp_from=smtp_from,
            ssl_certificate=ssl_certificate, ssl_private_key=ssl_private_key,
            smtp_debug=smtp_debug, mail_server_id=mail_server_id,
            allow_archived=allow_archived,
        )

    # ----------------------------------------------------------------- sending
    @api.model
    def send_email(self, message, mail_server_id=None, smtp_server=None,
                   smtp_port=None, smtp_user=None, smtp_password=None,
                   smtp_encryption=None, smtp_ssl_certificate=None,
                   smtp_ssl_private_key=None, smtp_debug=False, smtp_session=None):
        session_is_resend = bool(smtp_session is not None
                                 and getattr(smtp_session, 'is_resend', False))
        if session_is_resend:
            server = smtp_session.server
        else:
            server = self._resend_find_server(
                mail_server_id=mail_server_id, host=smtp_server,
                smtp_from=message.get('From'))
        if not self._resend_is_active(server):
            return super().send_email(
                message, mail_server_id=mail_server_id,
                smtp_server=smtp_server, smtp_port=smtp_port,
                smtp_user=smtp_user, smtp_password=smtp_password,
                smtp_encryption=smtp_encryption,
                smtp_ssl_certificate=smtp_ssl_certificate,
                smtp_ssl_private_key=smtp_ssl_private_key,
                smtp_debug=smtp_debug, smtp_session=smtp_session,
            )
        if self._disable_send():
            _logger.debug('Skip Resend delivery in test mode (Message-Id %s)',
                          message.get('Message-Id'))
            return message.get('Message-Id')
        config = getattr(smtp_session, 'config', None) or self._resend_check_ready(server)
        session = smtp_session if session_is_resend else ResendSession(
            server, config, smtp_from=message.get('From'))
        smtp_from, smtp_to_list, message = self._prepare_email_message__(message, session)
        return server._resend_send_message(
            message, smtp_from, smtp_to_list, config=config)

    # ---------------------------------------------------------- payload builder
    def _resend_prepare_from(self, message, config):
        """Apply the optional From rewriting so the sender domain is verified."""
        original_from = message.get('From') or ''
        if not config['from_email'] or config['from_mode'] == 'off':
            return original_from
        rewrite = config['from_mode'] == 'always'
        if config['from_mode'] == 'unverified':
            rewrite = not self.env['resend.connector']._domain_is_verified(original_from)
        if not rewrite:
            return original_from
        display_name = parseaddr(original_from)[0]
        new_from = formataddr((display_name, config['from_email'])) if display_name else config['from_email']
        # Keep the ability for the recipient to reply to the original sender.
        if not message.get('Reply-To'):
            message['Reply-To'] = original_from
        message.replace_header('From', new_from)
        _logger.info('Resend: From rewritten from %r to %r', original_from, new_from)
        return new_from

    def _resend_extract_parts(self, message, config):
        """Extract text/html bodies, attachments and inline images."""
        text_bodies, html_bodies = [], []
        attachments, inline_images = [], []
        for part in message.walk():
            if part.is_multipart():
                continue
            content_type = part.get_content_type()
            disposition = (part.get_content_disposition() or '').lower()
            content_id = (part.get('Content-ID') or '').strip('<> \t\r\n')
            filename = part.get_filename()
            if not filename and not content_id and disposition in ('', 'inline') \
                    and content_type in ('text/plain', 'text/html'):
                raw = part.get_payload(decode=True) or b''
                charset = part.get_content_charset() or 'utf-8'
                try:
                    text = raw.decode(charset, 'replace')
                except LookupError:
                    text = raw.decode('utf-8', 'replace')
                if content_type == 'text/html':
                    html_bodies.append(text)
                else:
                    text_bodies.append(text)
                continue
            raw = part.get_payload(decode=True)
            if raw is None:
                raw = part.get_payload()
                if isinstance(raw, str):
                    raw = raw.encode('utf-8', 'replace')
                elif not isinstance(raw, bytes):
                    continue
            if not raw:
                continue
            item = {
                'filename': filename or ('%s' % (content_id or 'attachment')),
                'content_type': content_type,
                'content': raw,
                'content_id': content_id,
                'disposition': disposition,
            }
            if content_id and content_type.startswith('image/'):
                inline_images.append(item)
            else:
                attachments.append(item)
        return text_bodies, html_bodies, attachments, inline_images

    @staticmethod
    def _resend_replace_cid(html, cid, data_uri):
        if not html or not cid:
            return html
        for variant in ('cid:%s' % cid, 'cid:<%s>' % cid):
            html = html.replace(variant, data_uri)
        return html

    @staticmethod
    def _resend_send_key(rfc_message_id, recipients):
        """Stable key of one physical delivery (same key = same email retried)."""
        raw = '|'.join([rfc_message_id or '', ','.join(sorted(recipients or []))])
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    @staticmethod
    def _resend_check_extension(filename):
        extension = (filename or '').rsplit('.', 1)[-1].lower() if '.' in (filename or '') else ''
        return extension not in BLOCKED_ATTACHMENT_EXTENSIONS

    def _resend_build_payload(self, message, smtp_from, smtp_to_list, config, mail_id=None):
        """Convert an RFC2822 EmailMessage into a Resend /emails payload."""
        from_email = self._resend_prepare_from(message, config)
        to_list = [addr for _name, addr in getaddresses([message.get('To') or '']) if addr]
        cc_list = [addr for _name, addr in getaddresses([message.get('Cc') or '']) if addr]
        normalized_to_cc = {
            tools.mail.email_normalize(addr, strict=False) for addr in (to_list + cc_list)
        }
        bcc_list = [
            addr for addr in smtp_to_list
            if tools.mail.email_normalize(addr, strict=False) not in normalized_to_cc
        ]
        text_bodies, html_bodies, attachments, inline_images = self._resend_extract_parts(
            message, config)
        text_body = '\n\n'.join(t for t in text_bodies if t)
        html_body = '\n'.join(h for h in html_bodies if h)

        inline_mode = config['inline_images_mode']
        encoded_attachments = []
        if inline_mode == 'data_uri':
            # Inline the image in the HTML body (some clients ignore data URIs).
            for image in inline_images:
                data_uri = 'data:%s;base64,%s' % (
                    image['content_type'],
                    base64.b64encode(image['content']).decode('ascii'),
                )
                html_body = self._resend_replace_cid(
                    html_body, image['content_id'], data_uri)
        elif inline_mode == 'attachment':
            # Plain attachments: the cid references will not resolve, but no
            # Content-ID is advertised (useful for strict providers).
            for image in inline_images:
                image['content_id'] = ''
                encoded_attachments.append(image)
        else:
            # Default: keep cid: references and send the images as attachments
            # carrying a Content-ID, exactly what an SMTP client does.
            encoded_attachments.extend(inline_images)

        if not html_body and not text_body:
            text_body = ' '

        headers = {}
        for key, value in message.items():
            lowered = key.lower()
            if lowered in _MANAGED_HEADERS or lowered.startswith(('x-msg-', 'x-forge-')):
                continue
            headers[key] = str(value)
        if config['preserve_message_id'] and message.get('Message-Id'):
            headers['Message-ID'] = message.get('Message-Id')

        payload = {
            'from': from_email or smtp_from,
            'to': to_list,
            'subject': message.get('Subject') or '',
            'text': text_body,
        }
        if html_body:
            payload['html'] = html_body
        if cc_list:
            payload['cc'] = cc_list
        if bcc_list:
            payload['bcc'] = bcc_list
        if message.get('Reply-To'):
            payload['reply_to'] = message.get('Reply-To')
        if headers:
            payload['headers'] = headers

        # Resend tags are a cheap way to find an email back in the dashboard and
        # in webhooks (data.tags) even if the database lost the mapping.
        db_name = ''.join(c if (c.isalnum() or c in '_-') else '-' for c in self.env.cr.dbname)[:200]
        tags = [{'name': 'odoo_db', 'value': db_name}]
        if mail_id:
            tags.append({'name': 'odoo_mail_id', 'value': str(mail_id)})
        payload['tags'] = tags

        encoded = []
        total_size = 0
        for item in encoded_attachments + attachments:
            if not self._resend_check_extension(item['filename']):
                raise ResendAPIError(
                    _('The file "%(name)s" cannot be sent through Resend: this '
                      'file type is blocked by the Resend security policy.',
                      name=item['filename']),
                    code='blocked_attachment',
                )
            if len(item['content']) > _MAX_ATTACHMENT_SIZE:
                raise ResendAPIError(
                    _('The attachment "%(name)s" is too large for Resend '
                      '(maximum %(max)s MB per file).',
                      name=item['filename'], max=_MAX_ATTACHMENT_SIZE // (1024 * 1024)),
                    code='attachment_too_large',
                )
            content_b64 = base64.b64encode(item['content']).decode('ascii')
            total_size += len(content_b64)
            entry = {
                'filename': item['filename'],
                'content': content_b64,
                'content_type': item['content_type'],
            }
            if item.get('content_id'):
                entry['content_id'] = item['content_id'][:128]
            encoded.append(entry)
        if total_size > _MAX_TOTAL_SIZE:
            raise ResendAPIError(
                _('The email is too large for the Resend API (%(size).1f MB).',
                  size=total_size / 1024.0 / 1024.0),
                code='email_too_large',
            )
        if encoded:
            payload['attachments'] = encoded
        return payload

    # ------------------------------------------------------------- delivery
    def _resend_send_message(self, message, smtp_from, smtp_to_list, config=None):
        """Send one RFC2822 message through Resend and record its tracking row."""
        self.ensure_one()
        ResendEmail = self.env['resend.email'].sudo()
        config = config or self.env['resend.connector']._get_config(server=self)
        rfc_message_id = message.get('Message-Id') or ''
        mail_mail = self.env['mail.mail'].sudo().search(
            [('message_id', '=', rfc_message_id)], limit=1) if rfc_message_id \
            else self.env['mail.mail']
        # One mail.mail can be split by Odoo into several deliveries: the send
        # key identifies the physical email (message id + recipients) so that a
        # retry reuses its tracking row instead of duplicating it.
        send_key = self._resend_send_key(rfc_message_id, smtp_to_list)
        try:
            payload = self._resend_build_payload(
                message, smtp_from, smtp_to_list, config, mail_id=mail_mail.id)
        except ResendAPIError as exc:
            self._resend_record_failure(mail_mail, rfc_message_id, exc, send_key=send_key)
            raise _resend_delivery_exception(self, exc.as_user_message()) from exc
        if not payload.get('to'):
            raise AssertionError(self.NO_VALID_RECIPIENT)

        idempotency_key = None
        if mail_mail:
            idempotency_key = '%s/mail.mail/%s' % (
                (param_get_str(self.env['ir.config_parameter'].sudo(), 'database.uuid') or self.env.cr.dbname)[:8],
                mail_mail.id)
        else:
            idempotency_key = build_idempotency_key(
                rfc_message_id, payload['from'], ','.join(payload['to']))

        tracking = self._resend_get_tracking(rfc_message_id, mail_mail, send_key)
        tracking.write({
            'from_email': payload['from'],
            'to_emails': ', '.join(payload['to']),
            'cc_emails': ', '.join(payload.get('cc') or []),
            'bcc_emails': ', '.join(payload.get('bcc') or []),
            'subject': payload.get('subject') or '',
            'state': 'sending',
            'company_id': self.env.company.id,
            'mail_server_id': self.id,
            'mail_mail_id': mail_mail.id or False,
            'send_key': send_key,
        })
        client = self.env['resend.connector']._get_client(server=self, config=config)
        try:
            result = client.send_email(payload, idempotency_key=idempotency_key)
        except ResendAPIError as exc:
            self._resend_record_failure(mail_mail, rfc_message_id, exc, tracking=tracking)
            raise _resend_delivery_exception(self, exc.as_user_message()) from exc
        resend_id = (result or {}).get('id') or ''
        tracking.write({
            'resend_message_id': resend_id,
            'state': 'sent',
            'status_date': fields.Datetime.now(),
            'last_event_type': 'email.sent',
            'error': False,
            'error_code': False,
            'transient_failure': False,
        })
        if mail_mail:
            if not mail_mail.resend_email_id:
                mail_mail.sudo().write({'resend_email_id': tracking.id})
            mail_mail._resend_sync_from_tracking()
        self.sudo().write({
            'resend_last_error': False,
            'resend_last_check': fields.Datetime.now(),
        })
        _logger.info('Resend: email %s sent (Resend id %s, Message-Id %s)',
                     mail_mail.id or '-', resend_id, rfc_message_id)
        return rfc_message_id

    def _resend_get_tracking(self, rfc_message_id, mail_mail, send_key=None):
        ResendEmail = self.env['resend.email'].sudo()
        tracking = ResendEmail.search([('send_key', '=', send_key)], limit=1) \
            if send_key else ResendEmail
        if tracking:
            return tracking
        return ResendEmail.create({
            'rfc_message_id': rfc_message_id,
            'send_key': send_key,
            'mail_mail_id': mail_mail.id or False,
            'mail_server_id': self.id,
            'company_id': self.env.company.id,
            'state': 'sending',
        })

    def _resend_record_failure(self, mail_mail, rfc_message_id, exc, tracking=None, send_key=None):
        self.sudo().write({
            'resend_last_error': str(exc)[:2000],
            'resend_last_check': fields.Datetime.now(),
        })
        tracking = tracking or self._resend_get_tracking(rfc_message_id, mail_mail, send_key)
        if mail_mail and not mail_mail.resend_email_id:
            mail_mail.sudo().write({'resend_email_id': tracking.id})
        tracking.write({
            'state': 'failed',
            'status_date': fields.Datetime.now(),
            'error': exc.as_user_message(),
            'error_code': exc.code or '',
            'transient_failure': bool(exc.retryable),
            'retry_count': tracking.retry_count + 1,
            'mail_mail_id': (mail_mail or tracking.mail_mail_id).id,
        })
        if mail_mail:
            mail_mail._resend_sync_from_tracking()

    # ----------------------------------------------------------- connection test
    def test_smtp_connection(self):
        """Use the standard "Test Connection" button for Resend servers too."""
        resend_servers = self.filtered('resend_enabled')
        if resend_servers and len(resend_servers) == len(self):
            self.ensure_one()
            try:
                summary = self.env['resend.connector']._check_connection()
            except ResendAPIError as exc:
                self.sudo().write({
                    'resend_last_error': str(exc)[:2000],
                    'resend_last_check': fields.Datetime.now(),
                })
                raise _resend_user_error(self, exc) from exc
            self.sudo().write({
                'resend_last_error': False,
                'resend_last_check': fields.Datetime.now(),
            })
            message = _(
                'Resend connection OK. %(count)s domain(s) accessible, %(verified)s verified.',
                count=summary['domains_count'],
                verified=', '.join(summary['verified']) or _('none'),
            )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'message': message,
                    'type': 'success',
                    'sticky': False,
                    'next': {'type': 'ir.actions.act_window_close'},
                },
            }
        return super().test_smtp_connection()


def _resend_delivery_exception(server, user_message):
    from odoo.addons.base.models.ir_mail_server import MailDeliveryException
    return MailDeliveryException(
        _('Mail Delivery Failed'),
        str(_('Resend API error for server "%(server)s": %(message)s',
              server=server.display_name, message=user_message)),
    )


def _resend_user_error(server, exc):
    from odoo.exceptions import UserError
    return UserError(str(_('Resend API error for server "%(server)s": %(message)s',
                           server=server.display_name, message=exc.as_user_message())))