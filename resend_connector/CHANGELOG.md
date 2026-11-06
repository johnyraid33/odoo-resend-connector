# Changelog

## 1.0.0

First public release.

* Resend transport for outgoing Odoo email without an SMTP connection.
* Delivery tracking with a ranked status machine and an event log.
* Signed webhooks with replay protection and duplicate detection.
* Incoming mail through the Resend receiving API, routed by Odoo's mail gateway.
* Domain and webhook management from the settings screen.
* Settings, documentation page, Russian translation and a test email wizard.
* Scheduled jobs for queued webhooks, transient retries, domain sync, status
  reconciliation and housekeeping.
* Compatibility layer for Odoo 19.0 and 20.0.
