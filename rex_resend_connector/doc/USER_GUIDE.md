# Mail Gateway for Resend: Installation and User Guide

English | [Русский](USER_GUIDE.ru.md) | [Українська](USER_GUIDE.uk.md)

This guide explains how to install and configure the Mail Gateway for Resend for
Odoo. It is written for administrators who have never used Resend and for users who
only need to read the delivery status of an email.

Module version 1.0.0. Odoo 19.0 and 20.0. Author: POLISHCHUK & SONS LTD,
https://rex.cy.

## Contents

1. About the module
2. Before you start
3. Create a Resend account and API key
4. Add and verify a sending domain
5. Install the module in Odoo
6. Configure outgoing email
7. Send a test email
8. Configure delivery webhooks
9. Receive incoming email
10. Day to day use
11. Troubleshooting
12. Security and privacy
13. Uninstall
14. Appendix: settings reference
15. Appendix: webhook events

## 1. About the module

Odoo normally sends email through an SMTP server. That has three practical
problems: many hosting providers block the SMTP ports, SMTP reports only that the
message was accepted, and replies end up in a mailbox instead of the Odoo record.

The module sends all Odoo email through the Resend API over HTTPS instead. Odoo
continues to build the message exactly as before, including templates,
attachments, Bcc and alias domains. Resend delivers it and reports what happened:
delivered, opened, bounced, and so on. Incoming mail can also be received through
Resend and is passed to Odoo's mail gateway, so replies appear in the chatter.

The module adds a Resend section to Odoo. No other module has to be changed.

## 2. Before you start

Please check the following before installing:

* Odoo 19.0 or 20.0 is running and you can log in as an administrator.
* You have access to the DNS records of the domain you want to send from. This is
  usually the control panel of your hosting provider or registrar.
* The Odoo instance has an address reachable from the internet, for example
  https://odoo.example.com. It is needed for delivery webhooks. If Odoo runs on a
  local computer, a temporary tunnel can be used for testing.
* You have an account on https://resend.com. A free plan is enough to test.
* Decide which address will send email, for example no-reply@mg.example.com.

It is a good idea to use a separate subdomain for email, such as mg.example.com or
mail.example.com. The reputation of that subdomain is then independent from your
main website domain.

## 3. Create a Resend account and API key

1. Open https://resend.com and create an account.
2. Open the API Keys page and click Create API Key.
3. Choose the access level:
   * Sending access: enough for sending email.
   * Full access: also allows the module to manage domains and webhooks from Odoo.
   Select Full access if you want to register domains and create the webhook from
   the Odoo settings screen.
4. Copy the key. It starts with re_.
5. Keep it in a safe place. Resend shows the key only once.

The key is entered in Odoo later, in Settings > Discuss > Resend.

## 4. Add and verify a sending domain

Resend only sends from domains you have verified. The verification proves that you
control the domain and enables SPF and DKIM, which email providers use to decide
whether a message is trustworthy.

### 4.1 Add the domain

You can do this in two ways.

From Odoo:

1. Open Resend > Domains and click New.
2. Enter the domain, for example mg.example.com. Leave Region empty unless Resend
   asked you to choose one.
3. Click Register in Resend.
4. The form now shows the DNS records Resend generated.

From the Resend dashboard:

1. Open Domains and click Add Domain.
2. Enter the domain and click Add.
3. Copy the records Resend shows to the DNS section of Odoo if you want to see them
   there. Open Resend > Domains, open the domain and click Synchronise.

### 4.2 Publish the DNS records

Open the DNS control panel of your domain provider and add the records exactly as
shown. There are usually three of them:

| Type | Name | Value | Purpose |
|---|---|---|---|
| TXT | send (or the name shown by Resend) | v=spf1 include:amazonses.com ~all | SPF: allows Resend to send for the domain |
| TXT | resend._domainkey | long key starting with p= | DKIM: signs every message |
| MX | send (only for incoming mail) | feedback-smtp.<region>.amazonses.com | Returns bounces to Resend |

Resend also recommends a DMARC record. It tells receiving servers what to do when
SPF or DKIM fails and where to send reports. A common starting record is:

| Type | Name | Value |
|---|---|---|
| TXT | _dmarc | v=DMARC1; p=none; rua=mailto:dmarc@example.com |

Replace example.com with your domain and use an address you read.

Some DNS providers ask for the name without the domain part. For example, for
send.mg.example.com many panels expect only send. If the provider adds the domain
automatically, enter the short name. If it does not, enter the full name.

### 4.3 Verify

1. In Odoo, open Resend > Domains and open the domain.
2. Click Verify Now.
3. Wait a few minutes if the status stays Pending. DNS changes can take from a few
   minutes to a few hours to spread.
4. Click Synchronise to refresh the status.

When everything is correct, the status becomes Verified and the domain appears in
the list of verified domains in Settings > Discuss > Resend.

If verification fails, compare every record character by character. A missing
quote, a wrong name or an extra space is enough to fail. You can also open the
domain in the Resend dashboard; it shows which record is not found.

### 4.4 Receiving domain (only for incoming email)

If you want to receive replies through Resend, enable receiving for the domain or
add a second domain used only for replies, for example reply.example.com. Resend
requires an MX record for receiving. If the main domain already receives mail, for
example through Google Workspace or Microsoft 365, use a subdomain for replies; a
second MX record on the main domain would conflict with your existing mailbox.

## 5. Install the module in Odoo

### 5.1 Standard installation

1. Copy the rex_resend_connector folder into an addons directory of your Odoo server.
2. Restart the Odoo service.
3. Open Apps, click Update Apps List and search for Mail Gateway for Resend.
4. Click Install.

### 5.2 Docker installation

Add the module folder as a volume in docker-compose.yml:

    services:
      odoo:
        volumes:
          - ./rex_resend_connector:/mnt/extra-addons/rex_resend_connector

Then restart the container and install the module from Apps, or run:

    docker compose exec odoo odoo -d odoo -i rex_resend_connector --stop-after-init

### 5.3 After installation

The module adds the Resend application to the main menu. It is visible to users in
the Resend Manager group. Administrators are members of this group automatically.
Other users can be added in Settings > Users.

## 6. Configure outgoing email

Open Settings > Discuss > Resend.

### 6.1 Master switch

Tick Send emails through Resend. When the switch is off, all email goes through
SMTP again, even on servers marked as Resend servers. This is a quick way to
disable the module without uninstalling it.

### 6.2 API key

Paste the key created in section 3. It is stored as a system parameter and can only
be read by administrators.

### 6.3 From address

Enter an address on the verified domain, for example no-reply@mg.example.com.

### 6.4 Rewrite From

Odoo chooses the sender address from the user, the company or the alias. That
address is often on a domain Resend cannot use. The option controls what happens:

* Only when the sender domain is not verified in Resend (recommended): the module
  replaces the sender with the From address above only when the original domain is
  not in the verified list. The original address is kept in Reply-To, so replies
  still reach the right person.
* Always: every email is sent from the configured From address.
* Never: the module sends the address chosen by Odoo. Resend will reject messages
  from a domain it does not know.

### 6.5 Outgoing mail server

Open Settings > Technical > Outgoing Mail Servers and open the server Odoo uses.
Tick Use Resend API. The SMTP fields are not used for this server, but Odoo
requires a host and a port; you can keep the existing values.

Click Test Connection. The module checks the API key and shows the number of
domains Resend returns.

If several outgoing servers exist, tick the option only on the ones that should use
Resend. Others continue to use SMTP.

### 6.6 Advanced settings

Open the Advanced section in Settings > Discuss > Resend. The defaults work for most
installations. The reference table in section 14 explains every option.

## 7. Send a test email

### 7.1 From a document

Open any quotation, invoice or contact, click Send and choose Email. The message is
prepared by Odoo as usual and delivered through Resend.

Open Resend > Emails. The new row shows the subject, the recipients, the Resend
message id and the status. At first the status is Sent. When the webhook is
configured, it changes to Delivered, Opened or a failure status.

### 7.2 Test addresses

Resend provides addresses that simulate delivery results. They are useful to test
the status handling without sending real email:

| Address | Result |
|---|---|
| delivered@resend.dev | Delivered |
| bounced@resend.dev | Hard bounce |
| complained@resend.dev | Delivered, then marked as spam |
| suppressed@resend.dev | Suppressed, not sent |

Without a verified domain, Resend only accepts the sender onboarding@resend.dev and
only sends to your own account address or to these test addresses. Once a domain is
verified, the test addresses still work and any real recipient can be used.

Every test message counts against your Resend quota.

### 7.3 Test wizard

Open Resend > Configuration > Settings and use the test wizard from the settings
screen, or create it from the Resend menu if your installation shows it. It sends a
message through the full Odoo pipeline and reports the result.

## 8. Configure delivery webhooks

Webhooks are how Resend tells Odoo what happened to a message. Without them the
module cannot show delivery, bounce or complaint statuses. The status can still be
refreshed by the scheduled reconciliation job, but webhooks are the normal way.

### 8.1 Requirements

Odoo must be reachable from the internet at a public HTTPS address. The address is
used in the webhook URL:

    https://odoo.example.com/resend/webhook

If Odoo is behind a proxy, make sure the address is public and the proxy forwards
the request to Odoo.

### 8.2 Automatic creation

1. In Settings > Discuss > Resend, check the Webhook URL shown in the Delivery
   webhooks section.
2. If the address is not correct, set Public Base URL to the public address of the
   instance.
3. Click Create / update webhook in Resend.
4. The module creates the endpoint in Resend, subscribes it to the email events and
   stores the signing secret. The status next to the button changes to Secret
   configured.

### 8.3 Manual creation

If you prefer to create the endpoint in the Resend dashboard:

1. Open Webhooks in Resend and click Add Endpoint.
2. Paste the webhook URL shown in Odoo.
3. Select the events: email.sent, email.delivered, email.delivery_delayed,
   email.opened, email.clicked, email.bounced, email.complained, email.failed,
   email.suppressed and email.received.
4. Copy the signing secret, which starts with whsec_, and paste it in Settings >
   Discuss > Resend > Webhook Signing Secret.
5. Save.

The secret can contain several values separated by commas, which is useful while
rotating a secret.

### 8.4 Check that events arrive

Open Resend > Configuration > Webhook Events. Every call from Resend appears there
with its type and status. The payload is stored for debugging if Store Webhook
Payloads is enabled.

If the list stays empty, the instance is not reachable from the internet, the
webhook is not subscribed to the events, or the secret is wrong.

## 9. Receive incoming email

Incoming mail is optional. Configure it when replies should appear in the chatter.

### 9.1 DNS

Add the receiving MX record for the subdomain. In Resend, open the domain, enable
Receiving if it is disabled, and copy the MX record. Publish it at your DNS
provider. Resend receives mail only when its MX record has the lowest priority
value for that name.

### 9.2 Odoo alias domain

1. Open Settings > Discuss.
2. Set Alias Domain to the receiving subdomain, for example reply.example.com.
3. Save.

Odoo then uses this domain for aliases and for the Reply-To address of outgoing
email.

### 9.3 Create an alias

An alias forwards a mailbox to an Odoo model. For example, sales@reply.example.com
can create leads.

1. Open Settings > Technical > Email > Aliases and click New.
2. Enter a name, for example sales.
3. Choose the model, for example CRM Lead.
4. Save.

You can also configure aliases on the record type itself, for example in Sales
settings, depending on the module.

Every address on a receiving domain is accepted by Resend, so you do not have to
create mailboxes in Resend. Routing is done by Odoo.

### 9.4 Replies to an existing email

When a customer replies to an email sent from Odoo, the message contains a
reference to the original. The module restores that reference and Odoo attaches the
reply to the same record and thread, without any alias.

### 9.5 Check incoming mail

Open Resend > Inbound. Each received message is listed with its sender, recipient
and status:

* Processed: routed to an Odoo record.
* No Route: no alias or thread matched the recipient. Open the message to see the
  reason, create the missing alias and click Process Again.
* Pending: waiting for the Resend API. The scheduled job retries automatically.
* Failed: see the error on the record.

## 10. Day to day use

### 10.1 The Resend menu

* Emails: every message sent through Resend, with its status and event history.
* Inbound: messages received from Resend.
* Domains: sending domains, their status and DNS records.
* Configuration > Settings: configuration.
* Configuration > Webhook Events: calls received from Resend.
* Configuration > Delivery Events: delivery events per message.
* Documentation: this guide inside Odoo.

### 10.2 Reading a status

Open an email in Resend > Emails to see the status and the list of events. The same
status is shown on the standard email record in Settings > Technical > Emails and
on the related document.

If a message fails, the reason from Resend is stored in the error field and in the
failure reason of the Odoo email. Typical reasons are a domain that is not
verified, an invalid API key, a blocked attachment type or an exhausted quota.

### 10.3 Retrying a failed email

Open the message in Resend > Emails and click Retry in Odoo. The module puts the
Odoo email back in the queue. Automatic retries also run for temporary failures
such as network errors, rate limits and server errors, up to the configured limit.

### 10.4 Domains

Click Synchronise on a domain to refresh its status and DNS records. A scheduled
job does this once a day.

## 11. Troubleshooting

| Problem | What to check |
|---|---|
| Email is not sent, the failure reason mentions the Resend API | Read the error on the email. Invalid API key: create a new key. Domain not verified: finish section 4. Quota exceeded: wait or upgrade the Resend plan. |
| Emails are sent but no status appears | Open Resend > Configuration > Webhook Events. If empty, configure the webhook (section 8) and make sure Odoo is reachable from the internet. |
| Webhook calls answer 401 | The signing secret in Odoo does not match the one in Resend. Copy it again. Also check the server clock; the tolerance is five minutes. |
| Replies do not appear in the chatter | Check the MX record, the email.received subscription, the Alias Domain and Resend > Inbound for messages without a route. |
| Resend rejects an attachment | Resend blocks executable file types. Rename or archive the file, or send a download link instead. |
| Mass mailing is slow | Resend allows 10 requests per second per team. The module limits itself to 8 and retries the rest, so the mailing takes longer but is not lost. |
| The webhook endpoint was disabled by Resend | Resend disables endpoints that keep failing. Fix the URL or the signature check and re-enable or recreate the endpoint in the Resend dashboard. |

The Odoo log contains more detail. Search for rex_resend_connector and resend webhook.

## 12. Security and privacy

* The API key and the signing secret are stored as system parameters. Only users in
  the Settings group can read them, and they are never written to the log.
* Every webhook call is verified with the Svix signature, using a constant time
  comparison and a five minute tolerance. Repeated calls with the same identifier
  are ignored.
* Sending uses an idempotency key per Odoo email, so a retry cannot deliver the same
  message twice.
* Attachments are checked against the list of file types Resend refuses and against
  the size limit before sending.
* Incoming messages are processed by Odoo's own mail gateway, which applies the
  usual access rules and attachment handling.
* Email content and metadata are stored by Resend according to your Resend account
  settings. Review their documentation if you have data location requirements.

## 13. Uninstall

Uninstalling removes the module models (emails, events, domains, inbound records)
and the settings. Emails already sent remain visible in the Resend dashboard. Odoo
keeps the standard mail.mail records.

Before uninstalling, switch outgoing servers back to SMTP, otherwise Odoo has no
mail server to use.

## 14. Appendix: settings reference

| Setting | Default | Description |
|---|---|---|
| Send emails through Resend | off | Master switch. When off, email uses SMTP. |
| Resend API Key | empty | API key created in Resend. |
| From Address | empty | Sender address on the verified domain. |
| Rewrite From | only when not verified | Replaces the sender when its domain is not verified in Resend. |
| Webhook URL | computed | Address Resend calls. Built from Public Base URL or the Odoo base URL. |
| Webhook Signing Secret | empty | Secret from Resend, used to verify webhook calls. |
| Inline Images | keep content id | How inline images are sent. Content id keeps cid references; data uri embeds the image in the HTML; attachment sends it as a normal file. |
| Send Odoo Message-ID | on | Adds the Odoo Message-Id as a header. Resend still assigns its own id; the module tracks both. |
| Store Webhook Payloads | on | Keeps the raw webhook JSON for debugging. |
| Keep Original Inbound Messages | off | Attaches the original message file to imported incoming emails. |
| Auto-Retry Transient Failures | 3 | Number of automatic retries for temporary errors. Zero disables them. |
| Retry Delay (minutes) | 15 | Delay between automatic retries. |
| Requests / second | 8 | Throttle for the Resend API. Resend allows 10 per team. |
| Webhook Tolerance (seconds) | 300 | Accepted clock difference for webhook signatures. |
| API Base URL | https://api.resend.com | Change only for a proxy or a test server. |
| Public Base URL | empty | Public address of the instance, used to build the webhook URL. |

## 15. Appendix: webhook events

| Event | Meaning |
|---|---|
| email.sent | Resend accepted the message |
| email.delivered | The recipient server accepted the message |
| email.delivery_delayed | Resend is retrying the delivery |
| email.opened | The recipient opened the message (tracking must be enabled) |
| email.clicked | The recipient followed a link (tracking must be enabled) |
| email.bounced | The message was rejected |
| email.complained | The recipient marked the message as spam |
| email.failed | Resend could not send the message |
| email.suppressed | The address is on the suppression list |
| email.received | Resend received a message for a receiving domain |

## Support

POLISHCHUK & SONS LTD, info@rex.cy, https://rex.cy.

When reporting a problem, include the Odoo version, the module version, the status
of the email and the error text from the email or from Resend > Configuration >
Webhook Events.