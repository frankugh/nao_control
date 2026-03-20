# -*- coding: utf-8 -*-
import sys
import threading

from naoqi import ALProxy


try:
    unicode
except NameError:
    unicode = str


_CONNECTION_ERROR_MARKERS = (
    "cannot connect",
    "connection failed",
    "connection refused",
    "connection reset",
    "failed to connect",
    "no connection to the remote host",
    "timed out",
    "timeout",
    "broken pipe",
    "socket",
    "transporterror",
    "transport error",
    "albroker::createbroker",
    "underlying_io_open_failed",
    "module destroyed",
)

_CACHE_LOCK = threading.RLock()
_PROXY_CACHE = {}
_ACTIVE_ENDPOINT = [None]


def _to_text(value):
    if isinstance(value, unicode):
        return value
    if isinstance(value, str):
        try:
            return value.decode("utf-8")
        except Exception:
            return unicode(repr(value))
    try:
        return unicode(value)
    except Exception:
        return u""


def _to_qi_name(value):
    if isinstance(value, unicode):
        return value.encode("utf-8")
    if isinstance(value, str):
        return value
    return str(value)


def _to_attr_name(value):
    return str(_to_text(value))


def _endpoint_tuple(nao_ip, nao_port):
    return (str(nao_ip or ""), int(nao_port or 0))


def _key_tuple(module_name, nao_ip, nao_port):
    return (_to_qi_name(module_name),) + _endpoint_tuple(nao_ip, nao_port)


def _logger_write(logger, level, message):
    if logger is not None:
        fn = getattr(logger, level, None)
        if callable(fn):
            try:
                fn(message)
                return
            except Exception:
                pass
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except Exception:
        pass


class _ProxyEntry(object):
    def __init__(self, module_name, nao_ip, nao_port):
        self.module_name = _to_qi_name(module_name)
        self.nao_ip = str(nao_ip or "")
        self.nao_port = int(nao_port or 0)
        self.proxy = None
        self.lock = threading.RLock()


class ProxyHandle(object):
    def __init__(self, module_name, nao_ip, nao_port, logger=None, allow_reconnect=False):
        self._module_name = _to_qi_name(module_name)
        self._nao_ip = str(nao_ip or "")
        self._nao_port = int(nao_port or 0)
        self._logger = logger
        self._allow_reconnect = bool(allow_reconnect)

    def __getattr__(self, attr_name):
        try:
            proxy = get_cached_proxy(self._module_name, self._nao_ip, self._nao_port, logger=self._logger)
            target = getattr(proxy, attr_name)
            if not callable(target):
                return target
        except Exception as exc:
            if (not self._allow_reconnect) or (not is_connection_error(exc)):
                raise

        def _call(*args, **kwargs):
            return call_proxy_method(
                self._module_name,
                attr_name,
                nao_ip=self._nao_ip,
                nao_port=self._nao_port,
                args=args,
                kwargs=kwargs,
                allow_reconnect=self._allow_reconnect,
                logger=self._logger,
            )

        return _call


def clear_proxy_cache():
    with _CACHE_LOCK:
        _PROXY_CACHE.clear()
        _ACTIVE_ENDPOINT[0] = None


def _ensure_active_endpoint(nao_ip, nao_port, logger=None):
    endpoint = _endpoint_tuple(nao_ip, nao_port)
    with _CACHE_LOCK:
        previous = _ACTIVE_ENDPOINT[0]
        if previous == endpoint:
            return
        cleared = len(_PROXY_CACHE)
        _PROXY_CACHE.clear()
        _ACTIVE_ENDPOINT[0] = endpoint
    if previous is not None:
        _logger_write(
            logger,
            "warning",
            "[NAO proxy] endpoint changed from %s:%s to %s:%s; cleared %s cached proxy(s)"
            % (previous[0], previous[1], endpoint[0], endpoint[1], cleared),
        )


def _get_entry(module_name, nao_ip, nao_port, logger=None):
    _ensure_active_endpoint(nao_ip, nao_port, logger=logger)
    key = _key_tuple(module_name, nao_ip, nao_port)
    with _CACHE_LOCK:
        entry = _PROXY_CACHE.get(key)
        if entry is None:
            entry = _ProxyEntry(module_name, nao_ip, nao_port)
            _PROXY_CACHE[key] = entry
        return entry


def get_cached_proxy(module_name, nao_ip, nao_port, logger=None):
    entry = _get_entry(module_name, nao_ip, nao_port, logger=logger)
    with entry.lock:
        if entry.proxy is None:
            entry.proxy = ALProxy(entry.module_name, entry.nao_ip, entry.nao_port)
        return entry.proxy


def invalidate_proxy(module_name, nao_ip, nao_port):
    key = _key_tuple(module_name, nao_ip, nao_port)
    with _CACHE_LOCK:
        entry = _PROXY_CACHE.pop(key, None)
    if entry is None:
        return
    with entry.lock:
        entry.proxy = None


def make_proxy_handle(module_name, nao_ip, nao_port, logger=None, allow_reconnect=False):
    return ProxyHandle(
        module_name,
        nao_ip,
        nao_port,
        logger=logger,
        allow_reconnect=allow_reconnect,
    )


def is_connection_error(exc):
    text = ("%s %s" % (exc.__class__.__name__, repr(exc))).lower()
    for marker in _CONNECTION_ERROR_MARKERS:
        if marker in text:
            return True
    return False


def call_proxy_method(
    module_name,
    method_name,
    nao_ip,
    nao_port,
    args=None,
    kwargs=None,
    allow_reconnect=False,
    logger=None,
):
    method_name_qi = _to_qi_name(method_name)
    args = tuple(args or ())
    kwargs = dict(kwargs or {})
    reconnect_attempted = False
    attr_name = _to_attr_name(method_name)

    for attempt in range(2 if allow_reconnect else 1):
        try:
            proxy = get_cached_proxy(module_name, nao_ip, nao_port, logger=logger)
            method = getattr(proxy, attr_name)
            result = method(*args, **kwargs)
            if reconnect_attempted:
                _logger_write(
                    logger,
                    "warning",
                    "[NAO proxy] reconnect succeeded for %s.%s on %s:%s"
                    % (_to_text(module_name), _to_text(method_name_qi), nao_ip, nao_port),
                )
            return result
        except Exception as exc:
            if allow_reconnect and attempt == 0 and is_connection_error(exc):
                reconnect_attempted = True
                _logger_write(
                    logger,
                    "warning",
                    "[NAO proxy] connection error on %s.%s; rebuilding proxy: %s"
                    % (_to_text(module_name), _to_text(method_name_qi), repr(exc)),
                )
                invalidate_proxy(module_name, nao_ip, nao_port)
                continue
            if reconnect_attempted:
                _logger_write(
                    logger,
                    "error",
                    "[NAO proxy] reconnect failed for %s.%s on %s:%s: %s"
                    % (_to_text(module_name), _to_text(method_name_qi), nao_ip, nao_port, repr(exc)),
                )
            raise
