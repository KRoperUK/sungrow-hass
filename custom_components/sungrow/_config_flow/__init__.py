"""Per-transport step mixins for the Sungrow config flow (#354).

hassfest requires the ``ConfigFlow`` subclass to live in a file named
``config_flow.py`` (not a package). The shell class
:class:`~custom_components.sungrow.config_flow.SungrowConfigFlow` therefore
sits at :mod:`..config_flow` and assembles the mixins defined in this
subpackage:

* :mod:`._base` — shared instance state + lifecycle (``async_remove``) and
  the single canonical binding for library symbols
  (``Auth`` / ``UserAuth`` / ``async_get_clientsession``) that tests patch.
* :mod:`._helpers` — pure helper functions and the OAuth callback timeout
  constant.
* :mod:`.cloud_oauth` — developer-portal OAuth handshake steps + helpers.
* :mod:`.cloud_user` — unofficial email/password transport step.
* :mod:`.modbus_only` — cloud-free direct-Modbus setup / import / reconfigure.
* :mod:`.zeroconf` — WiNet-S mDNS discovery + confirm.
* :mod:`.reconfigure` — reauth + cloud reconfigure.
* :mod:`.options` — options-flow handler.
"""
