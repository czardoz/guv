import errno
import os
from socket import socket as original_socket
import socket
import sys
import time
import logging
from io import IOBase

from .exceptions import IOClosed
from .support import get_errno
from .hubs import trampoline, notify_close, notify_opened

log = logging.getLogger('guv')

__all__ = ['GreenSocket', 'GreenPipe', 'shutdown_safe']

BUFFER_SIZE = 4096
CONNECT_ERR = {errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK}
CONNECT_SUCCESS = {0, errno.EISCONN}

if sys.platform[:3] == 'win':
    CONNECT_ERR.add(errno.WSAEINVAL)  # Bug 67


def socket_connect(sock, address):
    """
    Attempts to connect to the address, returns the descriptor if it succeeds,
    returns None if it needs to trampoline, and raises any exceptions.
    """
    err = sock.connect_ex(address)
    if err in CONNECT_ERR:
        return None
    if err not in CONNECT_SUCCESS:
        raise socket.error(err, errno.errorcode[err])
    return sock


def socket_checkerr(sock):
    err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    if err not in CONNECT_SUCCESS:
        raise socket.error(err, errno.errorcode[err])


def socket_accept(sock):
    """Attempt to accept() on the descriptor

    :type sock: socket.socket
    :return: (socket, address) or None if need to trampoline
    :rtype: tuple[socket, tuple[str, int]] or None
    """
    try:
        client_sock, address = sock.accept()
        #log.debug('accept: {}, {}'.format(client_sock, address))
        return client_sock, address
    except socket.error as e:
        if get_errno(e) == errno.EWOULDBLOCK:
            return None
        raise


if sys.platform[:3] == "win":
    # winsock sometimes throws ENOTCONN
    SOCKET_BLOCKING = {errno.EAGAIN, errno.EWOULDBLOCK}
    SOCKET_CLOSED = {errno.ECONNRESET, errno.ENOTCONN, errno.ESHUTDOWN}
else:
    # oddly, on linux/darwin, an unconnected socket is expected to block,
    # so we treat ENOTCONN the same as EWOULDBLOCK
    SOCKET_BLOCKING = {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOTCONN}
    SOCKET_CLOSED = {errno.ECONNRESET, errno.ESHUTDOWN, errno.EPIPE}


def set_nonblocking(sock):
    """Set `sock` to be nonblocking

    Works on many file-like objects as well as sockets. Only sockets can be nonblocking on
    Windows, however.
    """
    try:
        setblocking = sock.setblocking
    except AttributeError:
        # sock has no setblocking() method. It could be that this version of Python predates
        # socket.setblocking(). In that case, we can still set the flag "by hand" on the underlying
        # OS fileno using the fcntl module.
        try:
            import fcntl
        except ImportError:
            # Windows has no fcntl module. This might not be a socket at all, but rather a
            # file-like object with no setblocking() method. In particular, on Windows, pipes don't
            # support non-blocking I/O and therefore don't have that method, which means fcntl
            # wouldn't help even if we could load it.
            raise NotImplementedError(
                "set_nonblocking() on a file object with no setblocking() method (Windows pipes "
                "don't support non-blocking I/O)")
        # we managed to import fcntl
        fileno = sock.fileno()
        orig_flags = fcntl.fcntl(fileno, fcntl.F_GETFL)
        new_flags = orig_flags | os.O_NONBLOCK
        if new_flags != orig_flags:
            fcntl.fcntl(fileno, fcntl.F_SETFL, new_flags)
    else:
        # socket supports setblocking()
        setblocking(0)


try:
    from socket import _GLOBAL_DEFAULT_TIMEOUT
except ImportError:
    _GLOBAL_DEFAULT_TIMEOUT = object()


class GreenSocket:
    """Green version of socket.socket class, that is 100% API-compatible

    It also recognizes the keyword parameter, 'set_nonblocking=True'. Pass False to indicate that
    socket is already in non-blocking mode to save syscalls.
    """

    # this placeholder is to prevent __getattr__ from creating an infinite call loop
    sock = None

    def __init__(self, af_or_sock=socket.AF_INET, *args, **kwargs):
        """
        :param af_or_sock: socket address family or original socket
        :type af_or_sock: int or socket.socket
        """
        should_set_nonblocking = kwargs.pop('set_nonblocking', True)
        if isinstance(af_or_sock, int):
            # this is an address family (AF_*) constant; make a socket
            sock = original_socket(af_or_sock, *args, **kwargs)
            # notify the hub that this is a newly-opened socket.
            #log.debug('create new GreenSocket (create fresh socket), fd: {}'.format(sock.fileno()))
        else:
            # this is a socket
            sock = af_or_sock
            #log.debug('create new GreenSocket (from existing normal socket), fd: {}'
            #          .format(sock.fileno()))

        # import timeout from other socket, if it was there
        try:
            self._timeout = sock.gettimeout() or socket.getdefaulttimeout()
        except AttributeError:
            self._timeout = socket.getdefaulttimeout()

        if should_set_nonblocking:
            set_nonblocking(sock)

        #: :type: socket.socket
        self.sock = sock  # the original socket

        # when client calls setblocking(0) or settimeout(0) the socket must act non-blocking
        self.act_non_blocking = False

        # Copy some attributes from underlying real socket. This is the easiest way that i found
        # to fix https://bitbucket.org/guv/guv/issue/136 Only `getsockopt` is required to
        # fix that issue, others are just premature optimization to save __getattr__ call.
        self.bind = sock.bind
        self.close = sock.close
        self.fileno = sock.fileno
        self.getsockname = sock.getsockname
        self.getsockopt = sock.getsockopt
        self.listen = sock.listen
        self.setsockopt = sock.setsockopt
        self.shutdown = sock.shutdown
        self._closed = False

    @property
    def _sock(self):
        return self

    def _get_io_refs(self):
        return self.sock._io_refs

    def _set_io_refs(self, value):
        self.sock._io_refs = value

    _io_refs = property(_get_io_refs, _set_io_refs)

    # Forward unknown attributes to fd, cache the value for future use. I do not see any simple
    # attribute which could be changed so caching everything in self is fine. If we find such
    # attributes - only attributes having __get__ might be cached. For now - I do not want to
    # complicate it.
    def __getattr__(self, name):
        if self.sock is None:
            raise AttributeError(name)
        attr = getattr(self.sock, name)
        setattr(self, name, attr)
        return attr

    def _trampoline(self, fd, read=False, write=False, timeout=None, timeout_exc=None):
        """
        We need to trampoline via the event hub. We catch any signal back from the hub indicating
        that the operation we were waiting on was associated with a filehandle that's since been
        invalidated.
        """
        if self._closed:
            # If we did any logging, alerting to a second trampoline attempt on a closed
            # socket here would be useful.
            raise IOClosed()
        try:
            return trampoline(fd, read=read, write=write, timeout=timeout, timeout_exc=timeout_exc)
        except IOClosed:
            # this socket has been closed
            #log.debug('socket closed fd: {}'.format(self.fileno()))
            self._mark_as_closed()
            raise

    def accept(self):
        if self.act_non_blocking:
            return self.sock.accept()
        while True:
            res = socket_accept(self.sock)
            if res is not None:
                client, addr = res
                set_nonblocking(client)
                return type(self)(client), addr
            self._trampoline(self.fileno(), read=True, timeout=self.gettimeout(),
                             timeout_exc=socket.timeout('timed out'))

    def _mark_as_closed(self):
        """ Mark this socket as being closed """
        self._closed = True

    def __del__(self):
        # This is in case self.close is not assigned yet (currently the constructor does it)
        close = getattr(self, 'close', None)
        if close is not None:
            close()

    def connect(self, address):
        if self.act_non_blocking:
            return self.sock.connect(address)
        sock = self.sock
        fileno = self.fileno()
        if self.gettimeout() is None:
            while not socket_connect(sock, address):
                try:
                    self._trampoline(fileno, write=True)
                except IOClosed:
                    raise socket.error(errno.EBADFD)
                socket_checkerr(sock)
        else:
            end = time.time() + self.gettimeout()
            while True:
                if socket_connect(sock, address):
                    return
                if time.time() >= end:
                    raise socket.timeout("timed out")
                try:
                    self._trampoline(fileno, write=True, timeout=end - time.time(),
                                     timeout_exc=socket.timeout("timed out"))
                except IOClosed:
                    # ... we need some workable errno here.
                    raise socket.error(errno.EBADFD)
                socket_checkerr(sock)

    def connect_ex(self, address):
        if self.act_non_blocking:
            return self.sock.connect_ex(address)
        sock = self.sock
        fileno = self.fileno()
        if self.gettimeout() is None:
            while not socket_connect(sock, address):
                try:
                    self._trampoline(fileno, write=True)
                    socket_checkerr(sock)
                except socket.error as ex:
                    return get_errno(ex)
                except IOClosed:
                    return errno.EBADFD
        else:
            end = time.time() + self.gettimeout()
            while True:
                try:
                    if socket_connect(sock, address):
                        return 0
                    if time.time() >= end:
                        raise socket.timeout(errno.EAGAIN)
                    self._trampoline(fileno, write=True, timeout=end - time.time(),
                                     timeout_exc=socket.timeout(errno.EAGAIN))
                    socket_checkerr(sock)
                except socket.error as ex:
                    return get_errno(ex)
                except IOClosed:
                    return errno.EBADFD

    def dup(self, *args, **kw):
        sock = self.sock.dup(*args, **kw)
        newsock = type(self)(sock, set_nonblocking=False)
        newsock.settimeout(self.gettimeout())
        return newsock

    def makefile(self, *args, **kwargs):
        return original_socket.makefile(self, *args, **kwargs)

    def recv(self, buflen, flags=0):
        sock = self.sock
        if self.act_non_blocking:
            return sock.recv(buflen, flags)
        while True:
            try:
                return sock.recv(buflen, flags)
            except socket.error as e:
                if get_errno(e) in SOCKET_BLOCKING:
                    pass
                elif get_errno(e) in SOCKET_CLOSED:
                    return ''
                else:
                    raise
            try:
                self._trampoline(self.fileno(), read=True, timeout=self.gettimeout(),
                                 timeout_exc=socket.timeout("timed out"))
            except IOClosed as e:
                # Perhaps we should return '' instead?
                raise EOFError()

    def recvfrom(self, *args):
        if not self.act_non_blocking:
            self._trampoline(self.fileno(), read=True, timeout=self.gettimeout(),
                             timeout_exc=socket.timeout("timed out"))
        return self.sock.recvfrom(*args)

    def recvfrom_into(self, *args):
        if not self.act_non_blocking:
            self._trampoline(self.fileno(), read=True, timeout=self.gettimeout(),
                             timeout_exc=socket.timeout("timed out"))
        return self.sock.recvfrom_into(*args)

    def recv_into(self, *args):
        if not self.act_non_blocking:
            self._trampoline(self.fileno(), read=True, timeout=self.gettimeout(),
                             timeout_exc=socket.timeout("timed out"))
        return self.sock.recv_into(*args)

    def _old_send(self, data, flags=0):
        sock = self.sock

        if self.act_non_blocking:
            return sock.send(data, flags)

        # blocking socket behavior - sends all, blocks if the buffer is full
        total_sent = 0
        len_data = len(data)
        while True:
            try:
                total_sent += sock.send(data[total_sent:], flags)
            except socket.error as e:
                if get_errno(e) not in SOCKET_BLOCKING:
                    raise

            if total_sent == len_data:
                break

            try:
                self._trampoline(self.fileno(), write=True, timeout=self.gettimeout(),
                                 timeout_exc=socket.timeout("timed out"))
            except IOClosed:
                raise socket.error(errno.ECONNRESET, 'Connection closed by another thread')

        return total_sent

    def _new_send(self, data, flags=0):
        sock = self.sock

        if self.act_non_blocking:
            return sock.send(data, flags)

        # blocking socket behavior - sends all, blocks if the buffer is full
        total_sent = 0
        mv = memoryview(data)
        while mv:
            try:
                b_sent = sock.send(mv, flags)
                total_sent += b_sent
                mv = mv[b_sent:]
            except socket.error as e:
                if get_errno(e) not in SOCKET_BLOCKING:
                    raise

            try:
                self._trampoline(self.fileno(), write=True, timeout=self.gettimeout(),
                                 timeout_exc=socket.timeout("timed out"))
            except IOClosed:
                raise socket.error(errno.ECONNRESET, 'Connection closed by another thread')

        return total_sent

    send = _old_send

    def sendall(self, data, flags=0):
        mv = memoryview(data)
        while mv:
            b_sent = self.send(mv, flags)
            mv = mv[b_sent:]

    def sendto(self, *args):
        self._trampoline(self.fileno(), write=True)
        return self.sock.sendto(*args)

    def setblocking(self, flag):
        if flag:
            self.act_non_blocking = False
            self._timeout = None
        else:
            self.act_non_blocking = True
            self._timeout = 0.0

    def settimeout(self, howlong):
        if howlong is None or howlong == _GLOBAL_DEFAULT_TIMEOUT:
            self.setblocking(True)
            return
        try:
            f = howlong.__float__
        except AttributeError:
            raise TypeError('a float is required')
        howlong = f()
        if howlong < 0.0:
            raise ValueError('Timeout value out of range')
        if howlong == 0.0:
            self.act_non_blocking = True
            self._timeout = 0.0
        else:
            self.act_non_blocking = False
            self._timeout = howlong

    def gettimeout(self):
        return self._timeout

    if "__pypy__" in sys.builtin_module_names:
        def _reuse(self):
            getattr(self.sock, '_sock', self.sock)._reuse()

        def _drop(self):
            getattr(self.sock, '_sock', self.sock)._drop()


class _SocketDuckForFd:
    """Class implementing all socket methods used by _fileobject in a cooperative manner using low
    level os I/O calls
    """
    _refcount = 0

    def __init__(self, fileno):
        self._fileno = fileno
        notify_opened(fileno)
        self._closed = False

    def _trampoline(self, fd, read=False, write=False, timeout=None, timeout_exc=None):
        if self._closed:
            # don't trampoline if we're already closed.
            raise IOClosed()
        try:
            return trampoline(fd, read=read, write=write, timeout=timeout, timeout_exc=timeout_exc)
        except IOClosed:
            # our fileno has been obsoleted, defang ourselves to prevent spurious closes
            self._mark_as_closed()
            raise

    def _mark_as_closed(self):
        self._closed = True

    @property
    def _sock(self):
        return self

    def fileno(self):
        return self._fileno

    def recv(self, buflen):
        while True:
            try:
                data = os.read(self._fileno, buflen)
                return data
            except OSError as e:
                if get_errno(e) not in SOCKET_BLOCKING:
                    raise IOError(*e.args)
            self._trampoline(self.fileno(), read=True)

    def recv_into(self, buf, nbytes=0, flags=0):
        if nbytes == 0:
            nbytes = len(buf)
        data = self.recv(nbytes)
        buf[:nbytes] = data
        return len(data)

    def send(self, data):
        while True:
            try:
                return os.write(self._fileno, data)
            except OSError as e:
                if get_errno(e) not in SOCKET_BLOCKING:
                    raise IOError(*e.args)
                else:
                    trampoline(self.fileno(), write=True)

    def sendall(self, data):
        len_data = len(data)
        os_write = os.write
        fileno = self._fileno
        try:
            total_sent = os_write(fileno, data)
        except OSError as e:
            if get_errno(e) != errno.EAGAIN:
                raise IOError(*e.args)
            total_sent = 0
        while total_sent < len_data:
            self._trampoline(self.fileno(), write=True)
            try:
                total_sent += os_write(fileno, data[total_sent:])
            except OSError as e:
                if get_errno(e) != errno.EAGAIN:
                    raise IOError(*e.args)

    def __del__(self):
        self._close()

    def _close(self):
        notify_close(self._fileno)
        self._mark_as_closed()
        try:
            os.close(self._fileno)
        except:
            # os.close may fail if __init__ didn't complete
            # (i.e file dscriptor passed to popen was invalid
            pass

    def __repr__(self):
        return "%s:%d" % (self.__class__.__name__, self._fileno)

    def _reuse(self):
        self._refcount += 1

    def _drop(self):
        self._refcount -= 1
        if self._refcount == 0:
            self._close()

    # Python3
    _decref_socketios = _drop


def _operation_on_closed_file(*args, **kwargs):
    raise ValueError('I/O operation on closed file')


class GreenPipe(socket.SocketIO):
    """
    GreenPipe is a cooperative replacement for file class.
    It will cooperate on pipes. It will block on regular file.
    Differneces from file class:
    - mode is r/w property. Should re r/o
    - encoding property not implemented
    - write/writelines will not raise TypeError exception when non-string data is written
      it will write str(data) instead
    - Universal new lines are not supported and newlines property not implementeded
    - file argument can be descriptor, file name or file object.
    """

    def __init__(self, f, mode='r', bufsize=-1):
        if not isinstance(f, (str, int, IOBase)):
            raise TypeError('f(ile) should be int, str, unicode or file, not %r' % f)

        if isinstance(f, str):
            f = open(f, mode, 0)

        if isinstance(f, int):
            fileno = f
            self._name = "<fd:%d>" % fileno
        else:
            fileno = os.dup(f.fileno())
            self._name = f.name
            if f.mode != mode:
                raise ValueError('file.mode %r does not match mode parameter %r' % (f.mode, mode))
            self._name = f.name
            f.close()

        super(GreenPipe, self).__init__(_SocketDuckForFd(fileno), mode)
        set_nonblocking(self)
        self.softspace = 0

    @property
    def name(self):
        return self._name

    def __repr__(self):
        return "<%s %s %r, mode %r at 0x%x>" % (
            self.closed and 'closed' or 'open',
            self.__class__.__name__,
            self.name,
            self.mode,
            (id(self) < 0) and (sys.maxint + id(self)) or id(self))

    def close(self):
        super(GreenPipe, self).close()
        for method in [
            'fileno', 'flush', 'isatty', 'next', 'read', 'readinto',
            'readline', 'readlines', 'seek', 'tell', 'truncate',
            'write', 'xreadlines', '__iter__', '__next__', 'writelines']:
            setattr(self, method, _operation_on_closed_file)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _get_readahead_len(self):
        return len(self._rbuf.getvalue())

    def _clear_readahead_buf(self):
        len = self._get_readahead_len()
        if len > 0:
            self.read(len)

    def tell(self):
        self.flush()
        try:
            return os.lseek(self.fileno(), 0, 1) - self._get_readahead_len()
        except OSError as e:
            raise IOError(*e.args)

    def seek(self, offset, whence=0):
        self.flush()
        if whence == 1 and offset == 0:  # tell synonym
            return self.tell()
        if whence == 1:  # adjust offset by what is read ahead
            offset -= self._get_readahead_len()
        try:
            rv = os.lseek(self.fileno(), offset, whence)
        except OSError as e:
            raise IOError(*e.args)
        else:
            self._clear_readahead_buf()
            return rv

    if getattr(IOBase, 'truncate', None):  # not all OSes implement truncate
        def truncate(self, size=-1):
            self.flush()
            if size == -1:
                size = self.tell()
            try:
                rv = os.ftruncate(self.fileno(), size)
            except OSError as e:
                raise IOError(*e.args)
            else:
                self.seek(size)  # move position&clear buffer
                return rv

    def isatty(self):
        try:
            return os.isatty(self.fileno())
        except OSError as e:
            raise IOError(*e.args)

# import SSL module here so we can refer to greenio.SSL.exceptionclass
try:
    from OpenSSL import SSL
except ImportError:
    # pyOpenSSL not installed, define exceptions anyway for convenience
    class SSL(object):
        class WantWriteError(Exception):
            pass

        class WantReadError(Exception):
            pass

        class ZeroReturnError(Exception):
            pass

        class SysCallError(Exception):
            pass


def shutdown_safe(sock):
    """ Shuts down the socket. This is a convenience method for
    code that wants to gracefully handle regular sockets, SSL.Connection
    sockets from PyOpenSSL and ssl.SSLSocket objects from Python 2.6
    interchangeably.  Both types of ssl socket require a shutdown() before
    close, but they have different arity on their shutdown method.

    Regular sockets don't need a shutdown before close, but it doesn't hurt.
    """
    try:
        try:
            # socket, ssl.SSLSocket
            return sock.shutdown(socket.SHUT_RDWR)
        except TypeError:
            # SSL.Connection
            return sock.shutdown()
    except socket.error as e:
        # we don't care if the socket is already closed;
        # this will often be the case in an http server context
        if get_errno(e) != errno.ENOTCONN:
            raise