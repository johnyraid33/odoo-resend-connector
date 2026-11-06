# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Wizard that sends a test email through the normal Odoo pipeline.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResendTestMail(models.TransientModel):
    _name = 'resend.test.mail'
    _description = 'Send a Test Email through Resend'

    email_to = fields.Char(
        string='Recipient', required=True,
        help="Use delivered@resend.dev, bounced@resend.dev or complained@resend.dev "
             "to test the delivery webhooks without a verified domain.")
    subject = fields.Char(string='Subject', default='Resend test email', required=True)
    body_html = fields.Html(
        string='Body',
        default='<p>Congratulations, Odoo is sending email through Resend.</p>',
    )
    mail_server_id = fields.Many2one(
        'ir.mail_server', string='Outgoing Server', required=True,
        domain="[('resend_enabled', '=', True)]",
        help="The email goes through Odoo's standard pipeline: templates, "
             "attachments and delivery tracking are exercised exactly like a real send.")
    mail_mail_id = fields.Many2one('mail.mail', string='Generated Email', readonly=True)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if 'mail_server_id' in fields_list and not values.get('mail_server_id'):
            server = self.env['ir.mail_server'].sudo().search(
                [('resend_enabled', '=', True), ('active', '=', True)], limit=1)
            if server:
                values['mail_server_id'] = server.id
        return values

    def action_send(self):
        self.ensure_one()
        if not self.mail_server_id.resend_enabled:
            raise UserError(_('Select an outgoing server flagged with "Use Resend API".'))
        message = self.env['mail.message'].sudo().create({
            'subject': self.subject,
            'body': self.body_html,
            'message_type': 'email_outgoing',
            'email_from': self.env.company.email or self.env.user.email_formatted,
        })
        mail = self.env['mail.mail'].sudo().create({
            'mail_message_id': message.id,
            'email_to': self.email_to,
            'subject': self.subject,
            'body_html': self.body_html,
            'mail_server_id': self.mail_server_id.id,
        })
        mail.send()
        self.mail_mail_id = mail.id
        if mail.state == 'exception':
            raise UserError(_('The email could not be sent:\n%s',
                              mail.failure_reason or ''))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Test email sent'),
                'message': _('Resend accepted the email (status: %s). Open Resend > Emails '
                             'to follow the delivery events.', mail.resend_status or _('sent')),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }