"""End-to-end checks against the local test server.

Run inside an Odoo shell after starting mock_resend.py. The script sends mail
through the normal pipeline, posts signed webhooks and imports an incoming
message, then reports failures.
"""
from odoo.addons.rex_resend_connector.models.compat import param_get_str, param_set

import base64
import json
import os
import time
import urllib.request

MOCK = 'http://host.docker.internal:8099'
WEB = os.environ.get('RESEND_WEB', 'http://127.0.0.1:8069')
FAILS = []
RUN = str(int(time.time()))


def check(name, condition, extra=''):
    status = 'PASS' if condition else 'FAIL'
    print('[%s] %s %s' % (status, name, extra))
    if not condition:
        FAILS.append(name)


def http_get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode())


def http_post_json(url, payload, headers=None):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers=headers or {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def sign(secret, body, msg_id='msg_test_1', timestamp=None):
    timestamp = timestamp or str(int(time.time()))
    key = base64.b64decode(secret[len('whsec_'):])
    signed = ('%s.%s.' % (msg_id, timestamp)).encode() + body
    signature = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {
        'Content-Type': 'application/json',
        'svix-id': msg_id,
        'svix-timestamp': timestamp,
        'svix-signature': 'v1,' + signature,
    }


import hashlib
import hmac

urllib.request.urlopen(MOCK + '/_reset', timeout=10).read()
ICP = env['ir.config_parameter'].sudo()
connector = env['resend.connector']

# ---------------------------------------------------------------- configuration
param_set(ICP, 'resend.enabled', 'True')
param_set(ICP, 'resend.api_key', 're_test_key')
param_set(ICP, 'resend.base_url', MOCK)
param_set(ICP, 'resend.from_email', 'no-reply@example.com')
param_set(ICP, 'resend.from_mode', 'off')
param_set(ICP, 'resend.preserve_message_id', 'True')
param_set(ICP, 'resend.rate_limit', '20')
connector._invalidate_config_cache()
config = connector._get_config()
env.cr.commit()
check('config loaded', config['enabled'] and config['api_key'] == 're_test_key', config['base_url'])

# ---------------------------------------------------------------- domain sync
domains = env['resend.domain']._sync_all()
check('domains synced', len(domains) == 1 and domains.name == 'example.com', domains.mapped('name'))
check('domain verified', domains.status == 'verified')
check('dns records synced', len(domains.record_ids) == 2, [r.record_type for r in domains.record_ids])
check('verified domains cache', 'example.com' in connector._get_config()['verified_domains'])

# ---------------------------------------------------------------- outgoing
server = env['ir.mail_server'].create({
    'name': 'Resend Test Server',
    'smtp_host': 'smtp.invalid',
    'smtp_port': 25,
    'smtp_authentication': 'login',
    'smtp_user': 'dummy',
    'smtp_pass': 'dummy',
    'resend_enabled': True,
})
msg = env['mail.message'].create({
    'subject': 'Invoice INV/2026/0001',
    'body': '<p>Please find your invoice</p>',
    'message_type': 'email_outgoing',
    'email_from': 'Acme Sales <sales@example.com>',
})
attachment = env['ir.attachment'].create({
    'name': 'invoice.pdf', 'raw': base64.b64encode(b'%PDF-1.4 fake content'),
    'mimetype': 'application/pdf',
})
mail = env['mail.mail'].create({
    'mail_message_id': msg.id,
    'email_to': 'customer@example.org',
    'email_cc': 'cc@example.org',
    'subject': 'Invoice INV/2026/0001',
    'body_html': '<p>Please find your invoice <img src="cid:logo"/></p>',
    'mail_server_id': server.id,
    'attachment_ids': [(6, 0, attachment.ids)],
})
mail.send()
check('outgoing state sent', mail.state == 'sent', mail.failure_reason or '')
check('tracking row created', bool(mail.resend_email_id), mail.resend_email_id.id)
check('resend id stored', bool(mail.resend_message_id), mail.resend_message_id)
check('tracking status sent', mail.resend_status == 'sent', mail.resend_status)
emails = http_get(MOCK + '/_emails')['data']
check('mock received one email', len(emails) == 1, len(emails))
payload = emails[0]['payload'] if emails else {}
check('payload from', payload.get('from') == 'Acme Sales <sales@example.com>', payload.get('from'))
check('payload to', payload.get('to') == ['customer@example.org'], payload.get('to'))
check('payload cc', payload.get('cc') == ['cc@example.org'], payload.get('cc'))

check('payload subject', payload.get('subject') == 'Invoice INV/2026/0001')
check('payload html', 'Please find your invoice' in (payload.get('html') or ''))
check('payload text alternative', bool(payload.get('text')))
check('payload attachment', (payload.get('attachments') or [{}])[0].get('filename') == 'invoice.pdf')
check('payload message-id header', (payload.get('headers') or {}).get('Message-ID') == mail.message_id,
      (payload.get('headers') or {}).get('Message-ID'))
check('payload tags', any(t.get('name') == 'odoo_mail_id' for t in payload.get('tags') or []))
check('idempotency key', bool(emails[0]['idempotency_key']), emails[0]['idempotency_key'])
check('user agent set', 'Odoo-Resend-Connector' in (emails[0]['user_agent'] or ''))
check('mail.mail message id unchanged', mail.message_id == msg.message_id)

# Bcc is not a mail.mail field in Odoo 20: exercise the transport directly.
IrMailServer = env['ir.mail_server']
msg_bcc = IrMailServer._build_email__(
    email_from='Acme <sales@example.com>', email_to=['customer@example.org'],
    subject='Bcc test', body='<p>bcc</p>', subtype='html',
    email_bcc=['secret@example.org'], message_id='<bcc-test@example.com>')
IrMailServer.send_email(msg_bcc, mail_server_id=server.id)
payload_bcc = http_get(MOCK + '/_emails')['data'][-1]['payload']
check('bcc forwarded to resend', payload_bcc.get('bcc') == ['secret@example.org'], payload_bcc.get('bcc'))
check('bcc removed from headers', 'Bcc' not in (payload_bcc.get('headers') or {}))
check('direct send tracked', bool(env['resend.email'].search([('rfc_message_id', '=', '<bcc-test@example.com>')])))

# ---------------------------------------------------------------- from rewrite
param_set(ICP, 'resend.from_mode', 'always')
param_set(ICP, 'resend.from_email', 'no-reply@example.com')
connector._invalidate_config_cache()
msg2 = env['mail.message'].create({'subject': 'Rewrite test', 'body': '<p>x</p>', 'message_type': 'email_outgoing', 'email_from': 'Acme Sales <sales@example.com>'})
mail2 = env['mail.mail'].create({
    'mail_message_id': msg2.id, 'email_to': 'customer@example.org',
    'subject': 'Rewrite test', 'body_html': '<p>x</p>', 'mail_server_id': server.id,
})
mail2.send()
payload2 = http_get(MOCK + '/_emails')['data'][-1]['payload']
check('from rewritten', payload2['from'].endswith('<no-reply@example.com>'), payload2['from'])
check('reply-to preserved', 'sales@example.com' in (payload2.get('reply_to') or ''), payload2.get('reply_to'))
param_set(ICP, 'resend.from_mode', 'off')
connector._invalidate_config_cache()

# ---------------------------------------------------------------- webhook: states
secret = 'whsec_' + base64.b64encode(b'test-webhook-secret-32-bytes-ok').decode()
param_set(ICP, 'resend.webhook_secret', secret)
connector._invalidate_config_cache()
env.cr.commit()


def webhook(event_type, email_id, extra=None, msg_id='evt_%s' % time.time(), secret_value=None):
    data = {'email_id': email_id, 'message_id': '<resend@example.com>',
            'from': 'Acme <sales@example.com>', 'to': ['customer@example.org'],
            'subject': 'Invoice', 'created_at': '2026-11-06T10:00:00.000Z'}
    data.update(extra or {})
    body = json.dumps({'type': event_type, 'created_at': '2026-11-06T10:00:01.000Z', 'data': data}).encode()
    headers = sign(secret_value or secret, body, msg_id=msg_id)
    return http_post_json(WEB + '/resend/webhook', json.loads(body), headers)


status, text = webhook('email.sent', mail.resend_message_id, msg_id='evt_sent_%s' % time.time())
check('webhook sent accepted', status == 200, text)
status, text = webhook('email.delivered', mail.resend_message_id, msg_id='evt_delivered_%s' % time.time())
check('webhook delivered accepted', status == 200, text)
env.cr.commit(); env.invalidate_all(); mail = env['mail.mail'].browse(mail.id)
check('status delivered', mail.resend_status == 'delivered', mail.resend_status)
status, text = webhook('email.opened', mail.resend_message_id, msg_id='evt_opened_%s' % time.time())
env.cr.commit(); env.invalidate_all(); mail = env['mail.mail'].browse(mail.id)
check('status opened', mail.resend_status == 'opened', mail.resend_status)
status, text = webhook('email.delivered', mail.resend_message_id, msg_id='evt_delivered2_%s' % time.time())
env.cr.commit(); env.invalidate_all(); mail = env['mail.mail'].browse(mail.id)
check('late delivered does not roll back', mail.resend_status == 'opened', mail.resend_status)

# duplicate svix-id
status1, _ = webhook('email.clicked', mail.resend_message_id, msg_id='evt_dup_1')
status2, text2 = webhook('email.clicked', mail.resend_message_id, msg_id='evt_dup_1')
check('duplicate webhook idempotent', status1 == 200 and status2 == 200 and 'duplicate' in text2, text2)

# bounce
env['mail.notification'].create({
    'mail_message_id': mail.mail_message_id.id,
    'mail_mail_id': mail.id, 'notification_type': 'email',
    'notification_status': 'sent',
    'res_partner_id': env.user.partner_id.id,
})
env.cr.commit()
status, text = webhook('email.bounced', mail.resend_message_id,
                       {'bounce': {'type': 'Permanent', 'message': '550 5.1.1 unknown user'}},
                       msg_id='evt_bounce_' + RUN)
env.cr.commit(); env.invalidate_all(); mail = env['mail.mail'].browse(mail.id)
check('bounce sets exception', mail.state == 'exception', mail.state)
check('bounce status', mail.resend_status == 'bounced', mail.resend_status)
check('bounce reason', '550' in (mail.resend_error or ''), mail.resend_error)
env.cr.commit(); env.invalidate_all()
notifications = env['mail.notification'].search([('mail_mail_id', '=', mail.id)])
check('notification bounced', any(n.notification_status == 'bounce' for n in notifications),
      [(n.notification_status, n.failure_type) for n in notifications])

# invalid signature
status, text = webhook('email.delivered', mail.resend_message_id,
                       msg_id='evt_badsig', secret_value='whsec_' + base64.b64encode(b'wrong-secret').decode())
check('invalid signature rejected', status == 401, status)

# ---------------------------------------------------------------- inbound
partner = env['res.partner'].create({'name': 'Inbound Tester', 'email': 'reply@example.org'})
original = partner.message_post(body='<p>Original message</p>', subject='Quotation S00042',
                                message_type='comment', subtype_xmlid='mail.mt_comment')
reply_raw = (
    'From: Inbound Tester <reply@example.org>\r\n'
    'To: sales@reply.example.com\r\n'
    'Subject: Re: Quotation S00042\r\n'
    'Date: Thu, 06 Nov 2026 10:00:00 +0000\r\n'
    'Message-Id: <reply-%s@example.org>\r\n'
    'In-Reply-To: %s\r\n'
    'References: %s\r\n'
    'MIME-Version: 1.0\r\n'
    'Content-Type: text/html; charset="utf-8"\r\n'
    '\r\n'
    '<p>Thanks, this reply should land in the chatter.</p>\r\n'
) % (RUN, original.message_id, original.message_id)
receive = {
    'id': 'recv-' + RUN,
    'raw': base64.b64encode(reply_raw.encode()).decode(),
    'meta': {
        'id': 'recv-' + RUN, 'object': 'email', 'from': 'reply@example.org',
        'to': ['sales@reply.example.com'], 'cc': [], 'subject': 'Re: Quotation S00042',
        'message_id': '<reply-%s@example.org>' % RUN, 'html': '<p>Thanks...</p>',
        'headers': {'from': 'Inbound Tester <reply@example.org>',
                    'in-reply-to': original.message_id, 'references': original.message_id},
        'raw': {'download_url': MOCK + '/raw/recv-' + RUN, 'expires_at': '2099-01-01T00:00:00.000Z'},
        'attachments': [],
    },
}
http_post_json(MOCK + '/_receive', receive)
env.cr.commit()
status, text = webhook('email.received', 'recv-' + RUN, {
    'message_id': '<reply-%s@example.org>' % RUN, 'from': 'reply@example.org',
    'to': ['sales@reply.example.com'], 'subject': 'Re: Quotation S00042',
}, msg_id='evt_received_' + RUN)
check('inbound webhook accepted', status == 200, text)
env.cr.commit()
inbound = env['resend.inbound.email'].search([('resend_email_id', '=', 'recv-' + RUN)], limit=1)
check('inbound record created', bool(inbound), inbound.state)
check('inbound processed', inbound.state == 'processed', inbound.error)
env.cr.commit(); env.invalidate_all()
inbound = env['resend.inbound.email'].search([('resend_email_id', '=', 'recv-' + RUN)], limit=1)
check('inbound linked to message', bool(inbound.mail_message_id), inbound.mail_message_id.id)
env.cr.commit()
replies = env['mail.message'].search([('model', '=', 'res.partner'), ('res_id', '=', partner.id),
                                      ('message_id', '=', '<reply-%s@example.org>' % RUN)])
check('reply routed in chatter', len(replies) == 1, len(replies))

# ---------------------------------------------------------------- API error handling
param_set(ICP, 'resend.base_url', MOCK + '/missing')
connector._invalidate_config_cache()
msg_err = env['mail.message'].create({'subject': 'Failure test', 'body': '<p>x</p>', 'message_type': 'email_outgoing', 'email_from': 'Acme Sales <sales@example.com>'})
mail_err = env['mail.mail'].create({
    'mail_message_id': msg_err.id, 'email_to': 'customer@example.org',
    'subject': 'Failure test', 'body_html': '<p>x</p>', 'mail_server_id': server.id,
})
mail_err.send()
check('api error marks exception', mail_err.state == 'exception', mail_err.state)
check('api error recorded', 'Resend' in (mail_err.failure_reason or ''), (mail_err.failure_reason or '')[:120])
check('tracking failure row', mail_err.resend_email_id.state == 'failed', mail_err.resend_email_id.state)
param_set(ICP, 'resend.base_url', MOCK)
connector._invalidate_config_cache()

# ---------------------------------------------------------------- webhook creation
answer = env['res.config.settings'].create({}).action_resend_create_webhook()
hooks = http_get(MOCK + '/webhooks')['data']
check('webhook created in resend', any(h['endpoint'].endswith('/resend/webhook') for h in hooks), len(hooks))
check('webhook secret stored', bool(param_get_str(ICP, 'resend.webhook_secret')))

param_set(ICP, 'resend.base_url', '')
param_set(ICP, 'resend.enabled', 'False')
connector._invalidate_config_cache()
env.cr.commit()
print('TOTAL FAILURES:', len(FAILS), FAILS)