"""Test the module against the Resend API.

Run inside the Odoo shell:

    RESEND_API_KEY=re_xxx docker compose exec -T -e RESEND_API_KEY odoo \
        odoo shell -d odoo_resend --no-http < real_resend_test.py

Or edit the constants below.
"""
from odoo.addons.resend_connector.models.compat import param_get_str, param_set

import os
import time

API_KEY = os.environ.get('RESEND_API_KEY') or 'PUT_YOUR_KEY_HERE'
FROM = os.environ.get('RESEND_FROM') or 'onboarding@resend.dev'
TO = os.environ.get('RESEND_TO') or 'delivered@resend.dev'

ICP = env['ir.config_parameter'].sudo()
param_set(ICP, 'resend.enabled', 'True')
param_set(ICP, 'resend.api_key', API_KEY)
param_set(ICP, 'resend.base_url', 'https://api.resend.com')
param_set(ICP, 'resend.from_email', FROM)
param_set(ICP, 'resend.from_mode', 'always')
param_set(ICP, 'resend.preserve_message_id', 'True')
env['resend.connector']._invalidate_config_cache()
env.cr.commit()

connector = env['resend.connector']
client = connector._get_client()

print('1) API key check...')
try:
    domains = client.list_domains(limit=100)
    data = domains.get('data') if isinstance(domains, dict) else domains
    print('   OK - domains:', [d.get('name') for d in (data or [])])
except Exception as exc:  # noqa: BLE001
    print('   API key valid for sending, domain listing failed:', exc)

server = env['ir.mail_server'].sudo().search([('resend_enabled', '=', True)], limit=1)
if not server:
    server = env['ir.mail_server'].sudo().create({
        'name': 'Resend live', 'smtp_host': 'smtp.invalid', 'smtp_port': 25,
        'smtp_authentication': 'login', 'smtp_user': 'x', 'smtp_pass': 'x',
        'resend_enabled': True,
    })
message = env['mail.message'].sudo().create({
    'subject': 'Odoo to Resend API test %s' % int(time.time()),
    'body': '<p>Sent through the Resend API from Odoo.</p>',
    'message_type': 'email_outgoing',
    'email_from': 'Odoo Test <sales@example.com>',
})
mail = env['mail.mail'].sudo().create({
    'mail_message_id': message.id, 'email_to': TO,
    'subject': message.subject, 'body_html': message.body,
    'mail_server_id': server.id,
})
print('2) Sending through Odoo pipeline to', TO, '...')
mail.send()
env.cr.commit()
print('   state:', mail.state, '| failure:', (mail.failure_reason or '')[:300])
print('   resend id:', mail.resend_message_id)
print('   tracking:', mail.resend_status)
if mail.state != 'sent':
    raise SystemExit('SEND FAILED')

print('3) Reconciling status through the API (GET /emails/{id})...')
answer = {}
for attempt in range(10):
    time.sleep(3)
    answer = client.get_email(mail.resend_message_id)
    print('   last_event:', answer.get('last_event'), '| message_id:', answer.get('message_id'))
    if answer.get('last_event') in ('delivered', 'bounced', 'failed', 'complained'):
        break
print('   expected Message-ID from Odoo:', mail.message_id)
print('   Message-ID kept by Resend   :', answer.get('message_id'))
print('   same:', answer.get('message_id') == mail.message_id)
last_event = answer.get('last_event') or 'sent'
env['resend.email'].browse(mail.resend_email_id.id)._apply_event(
    'email.' + last_event, answer or {})
env.cr.commit()
print('4) Tracking state after reconcile:', mail.resend_status, '/', mail.resend_email_id.state)
print('DONE')