# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""Installation hook: default configuration and access rules."""

import logging

from .models.compat import param_get_str, param_set

_logger = logging.getLogger(__name__)

# Applied only when the parameter is empty, so an existing setup is kept.
DEFAULT_PARAMETERS = {
    'resend.from_mode': 'unverified',
    'resend.inline_images_mode': 'cid',
    'resend.preserve_message_id': 'True',
    'resend.store_webhook_payloads': 'True',
    'resend.rate_limit': '8',
    'resend.webhook_tolerance': '300',
    'resend.auto_retry_max': '3',
    'resend.retry_delay_minutes': '15',
}


def post_init_hook(env):
    ICP = env['ir.config_parameter'].sudo()
    for key, value in DEFAULT_PARAMETERS.items():
        if not param_get_str(ICP, key):
            param_set(ICP, key, value)
    # The manager group is created by the data files, which load before the hook.
    env['resend.connector']._ensure_access_rights()
    _logger.info('Resend connector: default configuration initialised.')
