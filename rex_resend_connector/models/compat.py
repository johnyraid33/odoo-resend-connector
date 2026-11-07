# Copyright 2026 POLISHCHUK & SONS LTD
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html).

"""Small differences between Odoo 19 and Odoo 20 used by the module.

Two APIs changed between the two versions:

* configuration parameters: Odoo 20 has typed getters and setters
  (get_str, get_bool, get_int, get_float, set_str), Odoo 19 only has
  get_param and set_param;
* access rules: Odoo 20 stores them in ir.access with an operation string,
  Odoo 19 keeps ir.model.access with perm_read, perm_write and so on.

The access rules are created by resend.connector._ensure_access_rights, so the
same module can be installed on both versions.
"""

import odoo

MAJOR_VERSION = odoo.release.version_info[0]
IS_ODOO_20 = MAJOR_VERSION >= 20


def param_get_str(ICP, key, default=''):
    if hasattr(ICP, 'get_str'):
        return ICP.get_str(key, default) or default
    return ICP.get_param(key, default) or default


def param_get_bool(ICP, key, default=False):
    if hasattr(ICP, 'get_bool'):
        return ICP.get_bool(key, default)
    value = ICP.get_param(key, None)
    if value is None or value is False:
        return default
    return str(value).lower() in ('true', '1', 'yes')


def param_get_int(ICP, key, default=0):
    if hasattr(ICP, 'get_int'):
        return ICP.get_int(key, default)
    value = ICP.get_param(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def param_get_float(ICP, key, default=0.0):
    if hasattr(ICP, 'get_float'):
        return ICP.get_float(key, default)
    value = ICP.get_param(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def param_set(ICP, key, value):
    if hasattr(ICP, 'set_str'):
        ICP.set_str(key, value)
    else:
        ICP.set_param(key, value)
