Mail Gateway for Resend
=======================

Send Odoo email through Resend, receive incoming mail from Resend and keep the
delivery status of every message in Odoo.

Author: POLISHCHUK & SONS LTD, https://rex.cy
License: LGPL-3.0
Odoo versions: 19.0 and 20.0

Overview
--------

Odoo prepares email as usual: templates, quotations, invoices, chatter
notifications, mass mailings, attachments, Bcc and alias domains. The module
replaces only the transport. Instead of opening an SMTP connection, the message is
sent to the Resend API over HTTPS. Delivery events reported by Resend are written
back to the Odoo record.

Incoming mail can also be received from Resend. The module downloads the original
message, restores the reference to the Odoo thread and passes it to Odoo's mail
gateway, so replies appear in the chatter.

Requirements
------------

* Odoo 19.0 or 20.0, Community or Enterprise.
* A Resend account.
* A domain verified in Resend for production sending.
* DNS access for the sending domain.
* A public HTTPS address for Odoo when delivery webhooks are used.

The module uses the requests library that ships with Odoo; no extra Python package
is required.

Installation
------------

1. Copy the rex_resend_connector folder into an addons directory.
2. Restart Odoo and update the Apps list.
3. Install "Mail Gateway for Resend".
4. Open Settings > Discuss > Resend.

With Docker, mount the module folder and run:

    docker compose exec odoo odoo -d odoo -i rex_resend_connector --stop-after-init

Configuration
-------------

Create the API key
~~~~~~~~~~~~~~~~~~

Open the API Keys page in Resend and create a key. Full access is needed to manage
domains and webhooks from Odoo. Sending access is enough to send email.

Verify the sending domain
~~~~~~~~~~~~~~~~~~~~~~~~~

1. Open Resend > Domains in Odoo and create the domain.
2. Click Register in Resend.
3. Publish the SPF and DKIM records shown on the form at your DNS provider.
4. Add a DMARC record, for example::

       v=DMARC1; p=none; rua=mailto:dmarc@example.com

5. Click Verify Now and wait for DNS propagation.

A subdomain such as mg.example.com is recommended, so the email reputation is
independent from the main domain.

Configure the server
~~~~~~~~~~~~~~~~~~~~

1. In Settings > Discuss > Resend tick "Send emails through Resend".
2. Paste the API key and set the From address on the verified domain.
3. Keep "Rewrite From" on its default value. The module then replaces a sender on an
   unverified domain and keeps the original address in Reply-To.
4. Open Settings > Technical > Outgoing Mail Servers and tick "Use Resend API" on
   the server Odoo should use.
5. Click Test Connection.

Servers without the option continue to use SMTP.

Delivery webhooks
~~~~~~~~~~~~~~~~~

Odoo must be reachable from the internet at a public HTTPS address. Check the
Webhook URL in the settings, set Public Base URL if needed, then click
"Create / update webhook in Resend". The module creates the endpoint, subscribes it
to the email events and stores the signing secret.

The events used by the module are::

    email.sent, email.delivered, email.delivery_delayed, email.opened,
    email.clicked, email.bounced, email.complained, email.failed,
    email.suppressed, email.received

Every call is verified with the Svix HMAC-SHA256 signature, a five minute tolerance
and a unique event id.

Incoming mail
-------------

1. Publish the MX record of a subdomain, for example reply.example.com.
2. Set Settings > Discuss > Alias Domain to that subdomain.
3. Create a mail alias for the address, or reply to an email sent from Odoo.
4. Messages without a matching alias are listed in Resend > Inbound.

Delivery statuses
-----------------

* Sent: accepted by the Resend API. The Odoo email is marked as sent.
* Delivered: accepted by the recipient server. The notification is marked as
  delivered.
* Delivery delayed: Resend is still retrying. Shown as a warning, it never lowers a
  later status.
* Opened and Clicked: tracking events. Informational.
* Bounced: rejected by the recipient server. The email is marked as failed and the
  notification as bounced.
* Complained: marked as spam by the recipient. The notification is cancelled.
* Failed: quota, invalid data or a blocked attachment. The email is marked as failed
  with the reason.

Bounced, complained and failed are final states. Complained takes precedence over
bounced, and bounced over failed.

Troubleshooting
---------------

* Mail Delivery Failed: open the email and read the failure reason. It contains the
  Resend error, for example domain is not verified, invalid_api_key or
  daily_quota_exceeded.
* No status updates: open Resend > Configuration > Webhook Events. An empty list
  means the webhook is missing or Odoo is not reachable from the internet.
* Webhook answers 401: the signing secret does not match or the server clock is off.
* Replies do not appear in the chatter: check the MX record, the email.received
  subscription, the alias domain and the Inbound list.
* Mass mailing is slow: Resend allows 10 requests per second per team. The module
  limits itself to 8 and retries the rest.

Security
--------

* The API key and the signing secret are system parameters readable by
  administrators only.
* Webhook calls are verified with a constant time comparison and a replay window.
* Every email carries an idempotency key, so a retry cannot deliver it twice.
* Attachments are checked against the file types and the size allowed by Resend.

Support
-------

POLISHCHUK & SONS LTD, info@rex.cy, https://rex.cy.