# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""
Sending domains and their DNS records.

The list comes from the Resend API. The detail endpoint is used to read the DNS
records and the sending and receiving capabilities of each domain.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .compat import param_set

_logger = logging.getLogger(__name__)


class ResendDomain(models.Model):
    _name = 'resend.domain'
    _description = 'Resend Domain'
    _order = 'name'
    _rec_name = 'name'

    name = fields.Char(string='Domain', required=True, index='btree', copy=False)
    resend_domain_id = fields.Char(string='Resend Domain ID', readonly=True, index='btree', copy=False)
    status = fields.Selection([
        ('not_started', 'Not Started'),
        ('pending', 'Pending'),
        ('verified', 'Verified'),
        ('temporary_failure', 'Temporary Failure'),
        ('failure', 'Failed'),
    ], string='Status', default='pending', readonly=True, index=True)
    region = fields.Char(string='Region', readonly=True)
    sending_enabled = fields.Boolean(string='Sending Enabled', readonly=True)
    receiving_enabled = fields.Boolean(string='Receiving Enabled', readonly=True)
    api_created_at = fields.Datetime(string='Created at Resend', readonly=True)
    last_sync = fields.Datetime(string='Last Sync', readonly=True)
    record_ids = fields.One2many(
        'resend.domain.record', 'domain_id', string='DNS Records', readonly=True)
    is_primary = fields.Boolean(
        string='Used for From', compute='_compute_is_primary',
        help='This domain matches the configured Resend From address.')
    company_id = fields.Many2one(
        'res.company', string='Company', default=lambda self: self.env.company)

    _name_uniq = models.Constraint(
        'UNIQUE(name)', 'This domain is already registered.')

    @api.depends('name')
    def _compute_is_primary(self):
        for record in self:
            from_email = self.env['resend.connector']._get_config()['from_email']
            record.is_primary = bool(from_email and from_email.lower().endswith('@' + record.name.lower()))

    # ------------------------------------------------------------------- sync
    @api.model
    def _sync_all(self):
        """Synchronise the domain list and their DNS records from Resend."""
        connector = self.env['resend.connector']
        client = connector._get_client()
        answer = client.list_domains(limit=100)
        domains = answer.get('data') if isinstance(answer, dict) else (answer or [])
        seen = self.browse()
        Domain = self.sudo()
        for item in domains:
            resend_id = item.get('id')
            if not resend_id:
                continue
            # The list endpoint omits the DNS records and capabilities: fetch the
            # detail for the domains that matter (a handful per account).
            try:
                detail = client.get_domain(resend_id)
                if isinstance(detail, dict):
                    item = dict(item, **detail)
            except Exception:  # noqa: BLE001 - keep the list data on API hiccup
                _logger.info('Resend: could not fetch domain detail %s', resend_id,
                             exc_info=True)
            values = self._prepare_domain_values(item)
            domain = Domain.search([('resend_domain_id', '=', resend_id)], limit=1)
            if not domain:
                domain = Domain.search([('name', '=', item.get('name'))], limit=1)
            if domain:
                domain.write(values)
            else:
                domain = Domain.create(values)
            domain._sync_records(item.get('records') or [])
            seen |= domain
        param_set(self.env['ir.config_parameter'].sudo(), 
            'resend.verified_domains',
            ','.join(seen.filtered(lambda d: d.status == 'verified').mapped('name')),
        )
        param_set(self.env['ir.config_parameter'].sudo(), 
            'resend.last_domain_sync', fields.Datetime.to_string(fields.Datetime.now()))
        self.env['resend.connector']._invalidate_config_cache()
        return seen

    @api.model
    def _prepare_domain_values(self, item):
        status = item.get('status') or 'pending'
        if status not in ('not_started', 'pending', 'verified', 'temporary_failure', 'failure'):
            status = 'pending'
        created_at = item.get('created_at') or item.get('createdAt')
        capabilities = item.get('capabilities') or {}
        return {
            'name': item.get('name'),
            'resend_domain_id': item.get('id'),
            'status': status,
            'region': item.get('region') or False,
            'sending_enabled': capabilities.get('sending') == 'enabled',
            'receiving_enabled': capabilities.get('receiving') == 'enabled',
            'api_created_at': self._parse_date(created_at),
            'last_sync': fields.Datetime.now(),
        }

    @api.model
    def _parse_date(self, value):
        if not value:
            return False
        try:
            return fields.Datetime.to_datetime(value)
        except (TypeError, ValueError):
            pass
        try:
            from dateutil import parser as date_parser
            parsed = date_parser.parse(value)
            return parsed.replace(tzinfo=None)
        except (TypeError, ValueError, OverflowError):
            return False

    def _sync_records(self, records):
        Record = self.env['resend.domain.record'].sudo()
        for domain in self:
            existing = {r.resend_record_id: r for r in domain.record_ids}
            for item in records:
                # The public API may not expose a per-record id: fall back to the
                # record label + name, which is unique within a domain.
                resend_record_id = (
                    item.get('id') or item.get('record_id')
                    or '%s-%s' % (item.get('record') or item.get('type') or '', item.get('name') or '')
                )
                values = {
                    'domain_id': domain.id,
                    'resend_record_id': resend_record_id,
                    'name': item.get('name') or '',
                    'record_type': item.get('type') or '',
                    'value': item.get('value') or '',
                    'ttl': str(item.get('ttl') or ''),
                    'status': item.get('status') or '',
                    'priority': item.get('priority') or 0,
                }
                record = existing.pop(resend_record_id, None)
                if record:
                    record.write(values)
                else:
                    Record.create(values)
            # Remove records that disappeared from the API answer.
            if existing:
                Record.browse([r.id for r in existing.values()]).unlink()

    # ----------------------------------------------------------------- actions
    def action_create_remote(self):
        """Register the domain in Resend and fetch its DNS records."""
        self.ensure_one()
        if self.resend_domain_id:
            return self.action_sync()
        client = self.env['resend.connector']._get_client()
        try:
            answer = client.create_domain(self.name, region=self.region or None)
        except Exception as exc:  # noqa: BLE001
            raise UserError(_('Could not create the domain in Resend: %(error)s',
                              error=exc)) from exc
        self.write({
            'resend_domain_id': (answer or {}).get('id'),
            'status': (answer or {}).get('status') or 'pending',
            'last_sync': fields.Datetime.now(),
        })
        self._sync_records((answer or {}).get('records') or [])
        self._sync_all()
        return True

    def action_verify(self):
        connector = self.env['resend.connector']
        client = connector._get_client()
        for domain in self:
            if not domain.resend_domain_id:
                raise UserError(_('This domain was not synchronised from Resend yet.'))
            try:
                client.verify_domain(domain.resend_domain_id)
            except Exception as exc:  # noqa: BLE001
                raise UserError(_('Could not verify the domain %(domain)s: %(error)s',
                                  domain=domain.name, error=exc)) from exc
        self._sync_all()
        return True

    def action_sync(self):
        self._sync_all()
        return True

    @api.model
    def _cron_sync_domains(self):
        try:
            self._sync_all()
        except Exception:  # noqa: BLE001
            _logger.warning('Resend: domain synchronisation failed', exc_info=True)


class ResendDomainRecord(models.Model):
    _name = 'resend.domain.record'
    _description = 'Resend DNS Record'
    _order = 'record_type, name'

    domain_id = fields.Many2one('resend.domain', required=True, ondelete='cascade', index=True)
    resend_record_id = fields.Char(string='Record ID')
    name = fields.Char(string='Name')
    record_type = fields.Char(string='Type')
    value = fields.Text(string='Value')
    ttl = fields.Char(string='TTL')
    status = fields.Char(string='Status')
    priority = fields.Integer(string='Priority')