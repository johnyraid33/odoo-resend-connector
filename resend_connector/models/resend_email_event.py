# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Delivery events received for a Resend email.
"""

from odoo import fields, models


class ResendEmailEvent(models.Model):
    _name = 'resend.email.event'
    _description = 'Resend Email Event'
    _order = 'event_date desc, id desc'

    email_id = fields.Many2one(
        'resend.email', string='Email', required=True, ondelete='cascade', index=True)
    resend_message_id = fields.Char(string='Resend Message-ID', index='btree_not_null')
    event_type = fields.Char(string='Event Type', required=True)
    event_date = fields.Datetime(string='Event Date', index=True)
    click_url = fields.Char(string='Clicked URL')
    bounce_type = fields.Char(string='Bounce Type')
    bounce_code = fields.Char(string='Bounce Code')
    bounce_message = fields.Text(string='Bounce Message')
    payload = fields.Text(string='Payload', help='Raw webhook payload (JSON).')
    webhook_event_id = fields.Many2one(
        'resend.webhook.event', string='Webhook Call', ondelete='set null', readonly=True)