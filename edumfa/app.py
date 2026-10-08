# License:  AGPLv3
# This file is part of eduMFA. eduMFA is a fork of privacyIDEA which was forked from LinOTP.
# Copyright (c) 2024 eduMFA Project-Team
# Previous authors by privacyIDEA project:
#
# 2014 Cornelius Kölbel, info@privacyidea.org
#
# (c) Cornelius Kölbel
#
# This code is free software; you can redistribute it and/or
# modify it under the terms of the GNU AFFERO GENERAL PUBLIC LICENSE
# License as published by the Free Software Foundation; either
# version 3 of the License, or any later version.
#
# This code is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU AFFERO GENERAL PUBLIC LICENSE for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
import logging
import logging.config
import os
import os.path
import sys
from pathlib import Path

import yaml
from flask import Flask, Response, request
from flask_babel import Babel
from flask_migrate import Migrate

# we need this import to add the before/after request function to the blueprints
import edumfa.api.before_after
from edumfa.api.application import application_blueprint
from edumfa.api.audit import audit_blueprint
from edumfa.api.auth import jwtauth
from edumfa.api.caconnector import caconnector_blueprint
from edumfa.api.clienttype import client_blueprint
from edumfa.api.edumfaserver import edumfaserver_blueprint
from edumfa.api.event import eventhandling_blueprint
from edumfa.api.health import health_blueprint
from edumfa.api.machine import machine_blueprint
from edumfa.api.machineresolver import machineresolver_blueprint
from edumfa.api.monitoring import monitoring_blueprint, stats_blueprint
from edumfa.api.periodictask import periodictask_blueprint
from edumfa.api.policy import policy_blueprint
from edumfa.api.radiusserver import radiusserver_blueprint
from edumfa.api.realm import defaultrealm_blueprint, realm_blueprint
from edumfa.api.recover import recover_blueprint
from edumfa.api.register import register_blueprint
from edumfa.api.resolver import resolver_blueprint
from edumfa.api.serviceid import serviceid_blueprint
from edumfa.api.smsgateway import smsgateway_blueprint
from edumfa.api.smtpserver import smtpserver_blueprint
from edumfa.api.system import system_blueprint
from edumfa.api.token import token_blueprint
from edumfa.api.tokengroup import tokengroup_blueprint
from edumfa.api.ttype import ttype_blueprint
from edumfa.api.user import user_blueprint
from edumfa.api.validate import validate_blueprint
from edumfa.config import config
from edumfa.lib import queue
from edumfa.lib.crypto import init_hsm
from edumfa.lib.log import DEFAULT_LOGGING_CONFIG
from edumfa.lib.tracing import instrument_network_tracing
from edumfa.models import db
from edumfa.webui.certificate import cert_blueprint
from edumfa.webui.login import get_accepted_language, login_blueprint

ENV_KEY = "EDUMFA_CONFIGFILE"


class PiResponseClass(Response):
    """Custom Response class overwriting the flask.Response.
    To avoid caching problems with the json property in the Response class,
    the property is overwritten using a non-caching approach.
    """

    @property
    def json(self):
        """This will contain the parsed JSON data if the mimetype indicates
        JSON (:mimetype:`application/json`, see :meth:`is_json`), otherwise it
        will be ``None``.
        Caching of the json data is disabled.
        """
        return self.get_json()

    default_mimetype = "application/json"


def get_locale():
    return get_accepted_language(request)


# Map the config value of EDUMFA_LDAP_LOGGING_DETAIL to ldap3's detail levels.
_LDAP3_DETAIL_LEVELS = {
    "off": 0,
    "error": 10,
    "basic": 20,
    "protocol": 30,
    "network": 40,
    "extended": 50,
}


def _enable_ldap3_logging(app):
    """
    Wire up the ``ldap3`` library logger so its debug messages are visible.

    ldap3 attaches only a ``NullHandler`` to its ``ldap3`` logger, so by default
    its messages are discarded. This is intentional on ldap3's side, but makes
    it hard to diagnose slow or failing LDAP lookups.

    If ``EDUMFA_LDAP_LOGGING`` is set to a truthy value in the config file, the
    ``ldap3`` logger is set to the level from ``EDUMFA_LDAP_LOGGING_LEVEL``
    (default ``DEBUG``) and attached to the handlers that the ``edumfa`` logger
    uses. The verbosity of the ldap3 output can be controlled with
    ``EDUMFA_LDAP_LOGGING_DETAIL`` (one of off/error/basic/protocol/network/
    extended, default ``extended``).

    This function never raises: if ldap3 is unavailable or cannot be
    reconfigured, a warning is logged and startup continues.
    """
    if not app.config.get("EDUMFA_LDAP_LOGGING"):
        return

    try:
        from ldap3.utils import log as ldap3_log
    except Exception as exx:  # pragma: no cover - ldap3 is a hard dependency
        logging.getLogger(__name__).warning(f"Could not enable ldap3 logging: {exx!r}")
        return

    level_name = str(app.config.get("EDUMFA_LDAP_LOGGING_LEVEL", "DEBUG")).upper()
    level = getattr(logging, level_name, logging.DEBUG)

    detail_name = str(app.config.get("EDUMFA_LDAP_LOGGING_DETAIL", "extended")).lower()
    detail = _LDAP3_DETAIL_LEVELS.get(detail_name, ldap3_log.EXTENDED)

    try:
        ldap3_log.set_library_log_activation_level(level)
        ldap3_log.set_library_log_detail_level(detail)
    except ValueError as exx:  # pragma: no cover - defensive
        logging.getLogger(__name__).warning(
            f"Could not set ldap3 log level/detail: {exx!r}"
        )

    ldap_logger = logging.getLogger("ldap3")
    ldap_logger.setLevel(level)
    # Attach the handlers of the eduMFA logger so ldap3 messages end up in the
    # same destination (file/console) as the rest of the application logs.
    for handler in logging.getLogger("edumfa").handlers:
        ldap_logger.addHandler(handler)
    ldap_logger.propagate = False
    logging.getLogger(__name__).info(
        f"Enabled ldap3 logging (level={logging.getLevelName(level)!r}, "
        f"detail={detail_name!r})."
    )


def create_app(
    config_name="development",
    config_file="/etc/edumfa/edumfa.cfg",
    silent=False,
    init_hsm=False,
    script=False,
):
    """
    First the configuration from the config.py is loaded depending on the
    config type like "production" or "development" or "testing".

    Then the environment variable EDUMFA_CONFIGFILE is checked for a
    config file, that contains additional settings, that will overwrite the
    default settings from config.py

    :param config_name: The config name like "production" or "testing"
    :type config_name: basestring
    :param config_file: The name of a config file to read configuration from
    :type config_file: basestring
    :param silent: If set to True the additional information are not printed
        to stdout
    :type silent: bool
    :param init_hsm: Whether the HSM should be initialized on app startup
    :type init_hsm: bool
    :return: The flask application
    :rtype: App object
    """
    if not silent:
        print(f"The configuration name is: {config_name}")
    if os.environ.get(ENV_KEY):
        config_file = os.environ[ENV_KEY]
    # Check if this is an eduMFA container image. This is necessary due to a
    # workaround changing way the config is located in containers.
    # This will be removed in 3.0.0.
    elif os.getenv("__EDUMFA_RUNNING_IN_CONTAINER") == "1":
        if Path("/etc/edumfa/edumfa.cfg").is_file():
            config_file = "/etc/edumfa/edumfa.cfg"
        else:
            config_file = "/opt/edumfa/edumfa_config.py"

    if not silent:
        print(f"Additional configuration will be read from the file {config_file}")
    app = Flask(__name__, static_folder="static", template_folder="static/templates")
    if config_name:
        app.config.from_object(config[config_name])

    try:
        # Try to load the given config_file.
        # If it does not exist, just ignore it.
        app.config.from_pyfile(config_file, silent=True)
    except OSError:
        sys.stderr.write("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")
        sys.stderr.write("  WARNING: edumfa create_app has no access\n")
        sys.stderr.write(f"  to {config_file}!\n")
        sys.stderr.write("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")

    # Try to load the file, that was specified in the environment variable
    # EDUMFA_CONFIG_FILE
    # If this file does not exist, we create an error!
    app.config.from_envvar(ENV_KEY, silent=True)

    # We allow to set different static folders
    app.static_folder = app.config.get("EDUMFA_STATIC_FOLDER", "static/")
    app.template_folder = app.config.get("EDUMFA_TEMPLATE_FOLDER", "static/templates/")

    app.register_blueprint(validate_blueprint, url_prefix="/validate")
    app.register_blueprint(token_blueprint, url_prefix="/token")
    app.register_blueprint(system_blueprint, url_prefix="/system")
    app.register_blueprint(resolver_blueprint, url_prefix="/resolver")
    app.register_blueprint(realm_blueprint, url_prefix="/realm")
    app.register_blueprint(defaultrealm_blueprint, url_prefix="/defaultrealm")
    app.register_blueprint(policy_blueprint, url_prefix="/policy")
    app.register_blueprint(login_blueprint, url_prefix="/")
    app.register_blueprint(jwtauth, url_prefix="/auth")
    app.register_blueprint(user_blueprint, url_prefix="/user")
    app.register_blueprint(audit_blueprint, url_prefix="/audit")
    app.register_blueprint(machineresolver_blueprint, url_prefix="/machineresolver")
    app.register_blueprint(machine_blueprint, url_prefix="/machine")
    app.register_blueprint(application_blueprint, url_prefix="/application")
    app.register_blueprint(caconnector_blueprint, url_prefix="/caconnector")
    app.register_blueprint(cert_blueprint, url_prefix="/certificate")
    app.register_blueprint(ttype_blueprint, url_prefix="/ttype")
    app.register_blueprint(register_blueprint, url_prefix="/register")
    app.register_blueprint(smtpserver_blueprint, url_prefix="/smtpserver")
    app.register_blueprint(recover_blueprint, url_prefix="/recover")
    app.register_blueprint(radiusserver_blueprint, url_prefix="/radiusserver")
    app.register_blueprint(periodictask_blueprint, url_prefix="/periodictask")
    app.register_blueprint(edumfaserver_blueprint, url_prefix="/edumfaserver")
    app.register_blueprint(eventhandling_blueprint, url_prefix="/event")
    app.register_blueprint(smsgateway_blueprint, url_prefix="/smsgateway")
    app.register_blueprint(client_blueprint, url_prefix="/client")
    app.register_blueprint(monitoring_blueprint, url_prefix="/monitoring")
    app.register_blueprint(stats_blueprint, url_prefix="/stats")
    app.register_blueprint(tokengroup_blueprint, url_prefix="/tokengroup")
    app.register_blueprint(serviceid_blueprint, url_prefix="/serviceid")
    app.register_blueprint(health_blueprint, url_prefix="/health")
    db.init_app(app)
    if not script:
        migrate = Migrate(app, db)

    app.response_class = PiResponseClass

    # Setup logging
    log_read_func = {
        "yaml": lambda x: logging.config.dictConfig(yaml.safe_load(open(x).read())),
        "cfg": lambda x: logging.config.fileConfig(x),
    }
    have_config = False
    log_exx = None
    log_config_file = app.config.get("EDUMFA_LOGCONFIG", "/etc/edumfa/logging.cfg")
    if os.path.isfile(log_config_file):
        for cnf_type in ["cfg", "yaml"]:
            try:
                log_read_func[cnf_type](log_config_file)
                if not silent:
                    print(f"Read Logging settings from {log_config_file}")
                have_config = True
                break
            except Exception as exx:
                log_exx = exx
                pass
    if not have_config:
        if log_exx:
            sys.stderr.write("Could not use EDUMFA_LOGCONFIG: " + str(log_exx) + "\n")
        if not silent:
            sys.stderr.write("Using EDUMFA_LOGLEVEL and EDUMFA_LOGFILE.\n")
        level = app.config.get("EDUMFA_LOGLEVEL", logging.INFO)
        # If there is another logfile in edumfa.cfg we use this.
        logfile = app.config.get("EDUMFA_LOGFILE", "/var/log/edumfa/edumfa.log")
        if not silent:
            sys.stderr.write(f"Using EDUMFA_LOGLEVEL {level}.\n")
            sys.stderr.write(f"Using EDUMFA_LOGFILE {logfile}.\n")
        DEFAULT_LOGGING_CONFIG["handlers"]["file"]["filename"] = logfile
        DEFAULT_LOGGING_CONFIG["handlers"]["file"]["level"] = level
        DEFAULT_LOGGING_CONFIG["loggers"]["edumfa"]["level"] = level
        logging.config.dictConfig(DEFAULT_LOGGING_CONFIG)

    # Optionally enable the debug logging of the ldap3 library. Since ldap3
    # only attaches a NullHandler, its messages are swallowed unless we wire it
    # up to the existing handlers. This is useful to diagnose slow or failing
    # LDAP lookups (see EDUMFA_LDAP_LOGGING).
    _enable_ldap3_logging(app)

    # If tracing is enabled, also instrument the low-level DNS/TCP/TLS calls so
    # that e.g. LDAP lookup latency can be split into DNS, connect, TLS and bind
    # time in the traces. This is a no-op if no tracer provider is configured.
    instrument_network_tracing()

    babel = Babel(app, locale_selector=get_locale)

    queue.register_app(app)

    if init_hsm:
        with app.app_context():
            init_hsm()

    logging.getLogger(__name__).debug(
        "Reading application from the static "
        f"folder {app.static_folder} and the template folder "
        f"{app.template_folder}"
    )

    return app
