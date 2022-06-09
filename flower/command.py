import os
import re
import sys
import atexit
import signal
import logging

from pprint import pformat

from logging import NullHandler

import click
from tornado.options import options
from tornado.options import parse_command_line, parse_config_file
from tornado.log import enable_pretty_logging
from celery.bin.base import CeleryCommand

from .app import Flower
from .urls import settings
from .utils import abs_path, prepend_url
from .options import DEFAULT_CONFIG_FILE, default_options

logger = logging.getLogger(__name__)
ENV_VAR_PREFIX = 'FLOWER_'


@click.command(cls=CeleryCommand,
               context_settings={
                   'ignore_unknown_options': True
               })
@click.argument("tornado_argv", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def flower(ctx, tornado_argv):
    """Web based tool for monitoring and administrating Celery clusters."""
    warn_about_celery_args_used_in_flower_command(ctx, tornado_argv)
    apply_env_options()
    apply_options(sys.argv[0], tornado_argv)

    extract_settings()
    setup_logging()

    app = ctx.obj.app
    flower = Flower(capp=app, options=options, **settings)

    atexit.register(flower.stop)

    def sigterm_handler(signal, frame):
        logger.info('SIGTERM detected, shutting down')
        sys.exit(0)

    signal.signal(signal.SIGTERM, sigterm_handler)
    print_banner(app, 'ssl_options' in settings)
    try:
        flower.start()
    except (KeyboardInterrupt, SystemExit):
        pass


def apply_env_options():
    "apply options passed through environment variables"
    env_options = filter(is_flower_envvar, os.environ)
    for env_var_name in env_options:
        name = env_var_name.replace(ENV_VAR_PREFIX, '', 1).lower()
        value = os.environ[env_var_name]
        try:
            option = options._options[name]
        except KeyError:
            option = options._options[name.replace('_', '-')]
        if option.multiple:
            value = [option.type(i) for i in value.split(',')]
        else:
            value = option.type(value)
        setattr(options, name, value)


def apply_options(prog_name, argv):
    "apply options passed through the configuration file"
    argv = list(filter(is_flower_option, argv))
    # parse the command line to get --conf option
    parse_command_line([prog_name] + argv)
    try:
        parse_config_file(os.path.abspath(options.conf), final=False)
        parse_command_line([prog_name] + argv)
    except IOError:
        if os.path.basename(options.conf) != DEFAULT_CONFIG_FILE:
            raise


def warn_about_celery_args_used_in_flower_command(ctx, flower_args):
    celery_options = [option for param in ctx.parent.command.params for option in param.opts]

    incorrectly_used_args = []
    for arg in flower_args:
        arg_name, _, _ = arg.partition("=")
        if arg_name in celery_options:
            incorrectly_used_args.append(arg_name)

    if incorrectly_used_args:
        logger.warning(
            f'You have incorrectly specified the following celery arguments after flower command:'
            f' {incorrectly_used_args}. '
            f'Please specify them after celery command instead following this template: '
            f'celery [celery args] flower [flower args].'
        )


def setup_logging():
    if options.debug and options.logging == 'info':
        options.logging = 'debug'
        enable_pretty_logging()
    else:
        logging.getLogger("tornado.access").addHandler(NullHandler())
        logging.getLogger("tornado.access").propagate = False


def extract_settings():
    settings['debug'] = options.debug

    if options.cookie_secret:
        settings['cookie_secret'] = options.cookie_secret

    if options.url_prefix:
        for name in ['login_url', 'static_url_prefix']:
            settings[name] = prepend_url(settings[name], options.url_prefix)

    if options.auth:
        # This is necessarily complex in order to try and respect documented behavior
        # If this was designed from scratch, it could be much simpler

        # The user has provided their own full regex
        if options.auth_regex:
            settings['auth_regex'] = re.compile(options.auth_regex)

        # List of emails to allow, without any regex check
        elif '|' in options.auth:
            if '.*' in options.auth:
                raise ValueError('--auth options only allows wildcard or pipe, not both')

            settings['auth_email_list'] = options.auth.split('|')

        # Wildcard (any user at a given domain)
        elif '.*' in options.auth:
            if '|' in options.auth:
                raise ValueError('--auth option only allows wildcard or pipe, not both')

            if options.auth.count('.*') != 1:
                raise ValueError('--auth option only allows exactly one wildcard, use --auth-regex instead')

            if options.auth[:3] != '.*@':
                raise ValueError('--auth with wildcard must start with the wildcard, exactly prior to the @domain.com')

            # From https://en.wikipedia.org/wiki/Email_address#Local-part, allowed chars for email
            allowed_wildcard_class = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\-]+"
            domain = re.escape(options.auth[3:])
            settings['auth_regex'] = re.compile(r'\A' + allowed_wildcard_class + '@' + domain + r'\Z')

        # Otherwise, assume the user provided exactly one valid email
        else:
            settings['auth_email_list'] = [options.auth]

        settings['oauth'] = {
            'key': options.oauth2_key or os.environ.get('FLOWER_OAUTH2_KEY'),
            'secret': options.oauth2_secret or os.environ.get('FLOWER_OAUTH2_SECRET'),
            'redirect_uri': options.oauth2_redirect_uri or os.environ.get('FLOWER_OAUTH2_REDIRECT_URI'),
        }

    if options.certfile and options.keyfile:
        settings['ssl_options'] = dict(certfile=abs_path(options.certfile),
                                       keyfile=abs_path(options.keyfile))
        if options.ca_certs:
            settings['ssl_options']['ca_certs'] = abs_path(options.ca_certs)


def is_flower_option(arg):
    name, _, _ = arg.lstrip('-').partition("=")
    name = name.replace('-', '_')
    return hasattr(options, name)


def is_flower_envvar(name):
    return name.startswith(ENV_VAR_PREFIX) and \
           name[len(ENV_VAR_PREFIX):].lower() in default_options


def print_banner(app, ssl):
    if not options.unix_socket:
        if options.url_prefix:
            prefix_str = f'/{options.url_prefix}/'
        else:
            prefix_str = ''

        logger.info(
            "Visit me at http%s://%s:%s%s", 's' if ssl else '',
            options.address or 'localhost', options.port,
            prefix_str
        )
    else:
        logger.info("Visit me via unix socket file: %s", options.unix_socket)

    logger.info('Broker: %s', app.connection().as_uri())
    logger.info(
        'Registered tasks: \n%s',
        pformat(sorted(app.tasks.keys()))
    )
    logger.debug('Settings: %s', pformat(settings))
