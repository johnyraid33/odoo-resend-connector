# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Resend fields shown on the standard email record.

The source of truth is resend.email; these fields are a copy, so the standard
mail.mail views can filter and sort without a join. When one mail.mail is split
into several deliveries, the record shows the least favourable state.
"""

from odoo import fields, models


def delivery_message_id(deliveries):
    """First Resend id of a set of deliveries (they share one Odoo email)."""
    for delivery in deliveries:
        if delivery.resend_message_id:
            return delivery.resend_message_id
    return False


class MailMail(models.Model):
    _inherit = 'mail.mail'

    resend_email_id = fields.Many2one(
        'resend.email', string='Resend Email', index='btree_not_null',
        ondelete='set null', copy=False, readonly=True)
    resend_message_id = fields.Char(
        string='Resend Message-ID', readonly=True, copy=False, index='btree_not_null')
    resend_status = fields.Selection([
        ('sending', 'Sending'),
        ('scheduled', 'Scheduled'),
        ('sent', 'Sent'),
        ('accepted', 'Accepted'),
        ('delayed', 'Delivery Delayed'),
        ('delivered', 'Delivered'),
        ('opened', 'Opened'),
        ('clicked', 'Clicked'),
        ('bounced', 'Bounced'),
        ('complained', 'Complained (Spam)'),
        ('failed', 'Failed'),
    ], string='Resend Status', readonly=True, copy=False, index=True)
    resend_status_date = fields.Datetime(
        string='Resend Status Date', readonly=True, copy=False)
    resend_last_event_type = fields.Char(
        string='Resend Last Event', readonly=True, copy=False)
    resend_error = fields.Text(string='Resend Error', readonly=True, copy=False)
    resend_retry_count = fields.Integer(
        string='Resend Retries', readonly=True, copy=False)
    resend_email_ids = fields.One2many(
        'resend.email', 'mail_mail_id', string='Resend Deliveries', readonly=True)
    resend_email_count = fields.Integer(
        string='Deliveries', compute='_compute_resend_email_count')

    def _compute_resend_email_count(self):
        for mail in self:
            mail.resend_email_count = len(mail.resend_email_ids)

    def _resend_sync_from_tracking(self):
        """Mirror the worst delivery state onto the mail.mail record.

        A single mail.mail can be delivered several times (Odoo splits
        notifications per recipient), so the record shows the least favourable
        state of all its deliveries.
        """
        for mail in self:
            deliveries = mail.resend_email_ids.sudo()
            if not deliveries:
                continue
            tracking = max(deliveries, key=lambda row: row._state_rank(row.state))
            mail.sudo().write({
                'resend_message_id': mail.resend_message_id or delivery_message_id(deliveries),
                'resend_status': tracking.state,
                'resend_status_date': tracking.status_date,
                'resend_last_event_type': tracking.last_event_type,
                'resend_error': tracking.error,
                'resend_retry_count': sum(deliveries.mapped('retry_count')),
            })

    def action_open_resend_email(self):
        self.ensure_one()
        if not self.resend_email_id:
            return False
        return {
            'type': 'ir.actions.act_window',
            'name': self.resend_email_id.sudo().display_name,
            'res_model': 'resend.email',
            'res_id': self.resend_email_id.id,
            'view_mode': 'form',
            'target': 'current',
        }