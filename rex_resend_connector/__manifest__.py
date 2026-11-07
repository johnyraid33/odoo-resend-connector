# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

{
    'name': 'Resend Email Connector',
    'version': '20.0.1.0.0',
    'category': 'Productivity/Discuss',
    'summary': 'Send Odoo email through Resend, receive replies and follow delivery',
    'description': """
Resend Email Connector
======================

Send Odoo email through Resend and keep track of what happens to every message.

The module adds Resend as an outgoing mail transport. Odoo keeps building emails
the usual way (templates, chatter notifications, invoices, mass mailings,
attachments, Bcc, alias domains); the module only replaces the transport, so the
messages leave over the Resend API instead of SMTP and no mail port has to be
opened on the server.

Main features
-------------
* Sending through Resend for every email Odoo produces. The option is set per
  outgoing mail server, with a global switch to fall back to SMTP.
* A delivery status for each message: sent, delivered, delivery delayed, opened,
  clicked, bounced, complained, failed. Statuses are ranked, so a late webhook
  cannot move a message backwards.
* Incoming mail: replies and new messages are fetched from Resend and handed to
  Odoo's mail gateway, so they land in the chatter of the record they belong to
  and follow the configured aliases.
* A Resend section in Odoo with Emails, Inbound, Domains, Webhook Events,
  Delivery Events, Settings and Documentation.
* Domain management: register a domain, show its DNS records, verify it and keep
  the records in sync with Resend.
* Webhook management: create the endpoint in Resend from the settings screen,
  store the signing secret and verify every call (Svix HMAC-SHA256, replay
  protection).
* Automatic retries for transient Resend errors. A failed message stays in Odoo
  with the Resend error message attached.

Installation
------------
1. Copy the module into your addons path and update the Apps list.
2. Install "Resend Email Connector".
3. Open Settings > Discuss > Resend, enter the API key and the From address.
4. Open Settings > Technical > Outgoing Mail Servers and tick "Use Resend API" on
   the server Odoo should use.
5. Create the webhook from the settings screen. Odoo has to be reachable from the
   internet for this step.
6. Verify a domain in Resend and publish the DNS records shown on the domain form.

A complete guide, including DNS and incoming mail setup, is available in
Resend > Documentation and in the doc folder of the module.

Compatibility
-------------
Odoo 19.0 and 20.0, Community and Enterprise. The module depends on the mail
module and on the requests library, which ships with Odoo.

License: LGPL-3.
Support: info@rex.cy
    """,
    'author': 'POLISHCHUK & SONS LTD',
    'maintainer': 'POLISHCHUK & SONS LTD',
    'website': 'https://rex.cy',
    'support': 'info@rex.cy',
    'images': [
        'images/cover.png',
        'images/01_emails_list_screenshot.png',
        'images/02_email_form_screenshot.png',
        'images/03_settings_screenshot.png',
        'images/06_domain_screenshot.png',
        'images/07_inbound_screenshot.png',
    ],
    'author_email': 'info@rex.cy',
    'license': 'LGPL-3',
    'depends': ['mail'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/resend_security.xml',
        'data/ir_cron.xml',
        'wizard/resend_test_mail_views.xml',
        'views/resend_email_views.xml',
        'views/resend_email_event_views.xml',
        'views/resend_domain_views.xml',
        'views/resend_inbound_email_views.xml',
        'views/resend_webhook_event_views.xml',
        'views/ir_mail_server_views.xml',
        'views/mail_mail_views.xml',
        'views/res_config_settings_views.xml',
        'views/resend_documentation_templates.xml',
        'views/resend_menus.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
    'post_init_hook': 'post_init_hook',
}