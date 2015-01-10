from __future__ import unicode_literals

from aj.util import LazyModule
psutil = LazyModule('psutil') # -2MB

import locale
import logging
import os
import signal
import socket
import sys
import syslog
import traceback

import aj
import aj.plugins
from aj.http import HttpRoot, RootHttpHandler, HttpMiddlewareAggregator
from aj.gate.middleware import GateMiddleware
from aj.plugins import *
from aj.api import *
from aj.util import make_report
from aj.util.pidfile import PidFile

import gevent
import gevent.ssl
from gevent import monkey

# Gevent monkeypatch ---------------------
try:
    monkey.patch_all(select=True, thread=True, aggressive=False, subprocess=True)
except:
    monkey.patch_all(select=True, thread=True, aggressive=False)  # old gevent

from gevent.event import Event
import threading
threading.Event = Event
# ----------------------------------------

import aj.compat

from socketio.server import SocketIOServer
from socketio.handler import SocketIOHandler



def run(config=None, plugin_providers=[], product_name='ajenti', dev_mode=False, debug_mode=False):
    if config is None:
        raise TypeError('`config` can\'t be None')

    reload(sys)
    sys.setdefaultencoding('utf8')

    aj.product = product_name
    aj.debug = debug_mode
    aj.dev = dev_mode
    
    aj.init()
    aj.context = Context()
    aj.config = config
    aj.plugin_providers = plugin_providers
    logging.info('Loading config from %s' % aj.config)
    aj.config.load()


    if aj.debug:
        logging.warn('Debug mode')
    if aj.dev:
        logging.warn('Dev mode')

    try:
        locale.setlocale(locale.LC_ALL, '')
    except:
        logging.warning('Couldn\'t set default locale')

    logging.info('Ajenti Core %s' % aj.version)
    logging.info('Detected platform: %s / %s' % (aj.platform, aj.platform_string))

    # Load plugins
    PluginManager.get(aj.context).load_all_from(aj.plugin_providers)
    if len(PluginManager.get(aj.context).get_all()) == 0:
        logging.warn('No plugins were loaded!')

    if 'socket' in aj.config.data['bind']:
        addrs = socket.getaddrinfo(bind_spec[0], bind_spec[1], socket.AF_INET6, 0, socket.SOL_TCP)
        bind_spec = addrs[0][-1]
    else:
        bind_spec = (aj.config.data['bind']['host'], aj.config.data['bind']['port'])

    # Fix stupid socketio bug (it tries to do *args[0][0])
    socket.socket.__getitem__ = lambda x, y: None

    logging.info('Starting server on %s' % (bind_spec, ))
    if bind_spec[0].startswith('/'):
        if os.path.exists(bind_spec[0]):
            os.unlink(bind_spec[0])
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(bind_spec[0])
        except:
            logging.error('Could not bind to %s' % bind_spec[0])
            sys.exit(1)
        listener.listen(10)
    else:
        listener = socket.socket(socket.AF_INET6 if ':' in bind_spec[0] else socket.AF_INET, socket.SOCK_STREAM)
        if not aj.platform in ['freebsd', 'osx']:
            try:
                listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_CORK, 1)
            except:
                logging.warn('Could not set TCP_CORK')
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(bind_spec)
        except:
            logging.error('Could not bind to %s' % (bind_spec,))
            sys.exit(1)
        listener.listen(10)

    gateway = GateMiddleware.get(aj.context)
    application = HttpRoot(HttpMiddlewareAggregator([gateway])).dispatch

    ssl_args = {}
    if aj.config.data['ssl']['enable']:
        ssl_args['certfile'] = aj.config.data['ssl']['certificate_path']
        logging.info('SSL enabled: %s' % ssl_args['certfile'])

    # URL prefix support for SocketIOServer
    def handle_one_response(self):
        path = self.environ.get('PATH_INFO')
        prefix = self.environ.get('HTTP_X_URL_PREFIX', '')
        self.server.resource = (prefix + '/socket.io').strip('/')
        response = handle_one_response.original(self)
        self.server.response = 'socket.io'
        return response
        
    handle_one_response.original = SocketIOHandler.handle_one_response
    SocketIOHandler.handle_one_response = handle_one_response

    aj.server = SocketIOServer(
        listener,
        log=open(os.devnull, 'w'),
        application=application,
        policy_server=False,
        handler_class=RootHttpHandler,
        resource='socket.io',
        transports=[
            str('websocket'),
            str('flashsocket'),
            str('xhr-polling'),
            str('jsonp-polling'),
        ],
        **ssl_args
    )

    # auth.log
    try:
        syslog.openlog(
            ident=str(aj.product),
            facility=syslog.LOG_AUTH,
        )
    except:
        syslog.openlog(aj.product)


    def cleanup():
        if hasattr(cleanup, '_started'):
            return
        cleanup._started = True
        logging.info('Process %s exiting normally' % os.getpid())
        gevent.signal(signal.SIGINT, lambda: None)
        gevent.signal(signal.SIGTERM, lambda: None)
        if aj.master:
            gateway.destroy()

        p = psutil.Process(os.getpid())
        for c in p.get_children(recursive=True):
            try:
                os.killpg(c.pid, signal.SIGTERM)
                os.killpg(c.pid, signal.SIGKILL)
            except OSError:
                pass
        sys.exit(0)

    try:
        gevent.signal(signal.SIGINT, cleanup)
        gevent.signal(signal.SIGTERM, cleanup)
    except:
        pass

    aj.server.serve_forever()

    if not aj.master:
        # child process, server is stopped, wait until killed
        gevent.wait()
        #while True:
        #    gevent.sleep(3600)

    if hasattr(aj.server, 'restart_marker'):
        logging.warn('Restarting by request')
        cleanup()

        fd = 20  # Close all descriptors. Creepy thing
        while fd > 2:
            try:
                os.close(fd)
                logging.debug('Closed descriptor #%i' % fd)
            except:
                pass
            fd -= 1

        os.execv(sys.argv[0], sys.argv)
    else:
        if aj.master:
            logging.debug('Server stopped')



def handle_crash(exc):
    logging.error('Fatal crash occured')
    traceback.print_exc()
    exc.traceback = traceback.format_exc(exc)
    report_path = '/root/%s-crash.txt' % aj.product
    try:
        report = open(report_path, 'w')
    except:
        report_path = './%s-crash.txt' % aj.product
        report = open(report_path, 'w')
    report.write(make_report(exc))
    report.close()
    logging.error('Crash report written to %s' % report_path)
    #logging.error('Please submit it to https://github.com/Eugeny/ajenti/issues/new')


def start(daemonize=False, log_level=logging.INFO, **kwargs):
    if daemonize:
        aj.log.init_log_directory()
        logfile = open(aj.log.LOG_FILE, 'w+')
        context = daemon.DaemonContext(
            pidfile=PidFile('/var/run/ajenti.pid'),
            stdout=logfile,
            stderr=logfile,
            detach_process=True
        )
        with context:
            gevent.reinit()
            aj.log.init_log_rotation()
            try:
                run(**kwargs)
            except Exception as e:
                handle_crash(e)
    else:
        try:
            run(**kwargs)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            handle_crash(e)

