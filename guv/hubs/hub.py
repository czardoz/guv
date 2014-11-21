import logging
import importlib

from .. import patcher

_threading = patcher.original('threading')
_threadlocal = _threading.local()

log = logging.getLogger('guv')


def notify_close(fd):
    """Register for any waiting listeners to be notified on the next run loop that a particular file
    descriptor has been explicitly closed
    """
    hub = get_hub()
    hub.notify_close(fd)


def notify_opened(fd):
    """
    Some file descriptors may be closed 'silently' - that is, by the garbage collector, by an
    external library, etc. When the OS returns a file descriptor from an open call (or something
    similar), this may be the only indication we have that the FD has been closed and then recycled.
    We let the hub know that the old file descriptor is dead; any stuck listeners will be disabled
    and notified in turn.
    """
    hub = get_hub()
    hub.mark_as_reopened(fd)


def get_default_hub():
    """Get default hub implementation
    """
    names = ['pyuv', 'epoll']

    for name in names:
        try:
            module = importlib.import_module('guv.hubs.{}'.format(name))
            log.debug('Using event loop backend: {}'.format(name))
            return module
        except ImportError:
            # try the next possible hub
            pass


def use_hub(mod=None):
    """Use the module :var:`mod`, containing a class called Hub, as the event hub
    """
    if not mod:
        mod = get_default_hub()

    if hasattr(_threadlocal, 'hub'):
        del _threadlocal.hub

    if hasattr(mod, 'Hub'):
        _threadlocal.Hub = mod.Hub
    else:
        _threadlocal.Hub = mod


def get_hub():
    """Get the current event hub singleton object

    .. note :: |internal|
    """
    try:
        hub = _threadlocal.hub
    except AttributeError:
        # instantiate a Hub
        try:
            _threadlocal.Hub
        except AttributeError:
            use_hub()

        hub = _threadlocal.hub = _threadlocal.Hub()
    return hub