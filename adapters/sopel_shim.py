# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
sopel_shim.py — Zero-Dependency In-Memory Sopel Plugin Compatibility Shim.
Injected into sys.modules['sopel'], sys.modules['sopel.module'], and sys.modules['sopel.plugin']
to execute community Sopel 7.x/8.x plugins without pip or heavy external dependencies.
"""

import fnmatch
import json
import logging
import re
import sys
import types
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("SopelShim")


class SopelConfigCore:
    """Mock Sopel core config section."""
    def __init__(self, bot_nick: str = "minibot"):
        self.nick = bot_nick
        self.user = bot_nick
        self.name = "Minibot"
        self.prefix = "!"
        self.help_prefix = "!"
        self.owner = "admin"
        self.admins = ["admin"]


class SopelConfigMock:
    """Mock Sopel config object."""
    def __init__(self, bot_nick: str = "minibot"):
        self.core = SopelConfigCore(bot_nick)

    def __getitem__(self, item):
        return getattr(self, item, None)

    def get(self, item, default=None):
        return getattr(self, item, default)


class SopelBotWrapper:
    """Mock Sopel bot instance mapping .say(), .reply(), .action(), .notice() to minibot."""
    def __init__(self, minibot_instance: Any, current_target: str, sender_nick: str):
        self._minibot = minibot_instance
        self._target = current_target
        self._sender = sender_nick
        self.nick = getattr(minibot_instance, "nick", "minibot")
        self.channels = getattr(minibot_instance, "channels", [])
        self.network = getattr(minibot_instance, "network", "")
        self.users = {}

        # Persistent bot memory dictionary
        if not hasattr(minibot_instance, "_sopel_memory") or minibot_instance._sopel_memory is None:
            minibot_instance._sopel_memory = {}
        self.memory = minibot_instance._sopel_memory

        self.config = SopelConfigMock(self.nick)

    def say(self, message: str, recipient: Optional[str] = None, max_messages: int = 1, destination: Optional[str] = None, **kwargs) -> None:
        target = destination or recipient or self._target
        self._minibot.send_msg(target, message)

    def reply(self, message: str, recipient: Optional[str] = None, reply_to: Optional[str] = None, destination: Optional[str] = None, **kwargs) -> None:
        target = destination or recipient or self._target
        reply_nick = reply_to or self._sender
        if target.startswith("#"):
            self._minibot.send_msg(target, f"{reply_nick}: {message}")
        else:
            self._minibot.send_msg(target, message)

    def action(self, message: str, recipient: Optional[str] = None) -> None:
        target = recipient or self._target
        if hasattr(self._minibot, "raw_send"):
            self._minibot.raw_send(f"PRIVMSG {target} :\x01ACTION {message}\x01")
        elif hasattr(self._minibot, "send_msg"):
            self._minibot.send_msg(target, f"\x01ACTION {message}\x01")

    def notice(self, message: str, recipient: Optional[str] = None) -> None:
        target = recipient or self._sender
        if hasattr(self._minibot, "raw_send"):
            self._minibot.raw_send(f"NOTICE {target} :{message}")
        elif hasattr(self._minibot, "send_msg"):
            self._minibot.send_msg(target, f"[NOTICE] {message}")


class SopelTrigger:
    """Mock Sopel trigger instance encapsulating IRC event payload."""
    def __init__(
        self,
        bot_nick: str,
        target: str,
        sender_nick: str,
        hostmask: str,
        text: str,
        match_obj: Optional[re.Match] = None,
        is_admin: bool = False,
        is_owner: bool = False,
    ):
        self.nick = sender_nick
        self.sender = target
        self.hostmask = hostmask or f"{sender_nick}!user@host"

        # Parse user and host from hostmask
        if "!" in self.hostmask and "@" in self.hostmask:
            ident_host = self.hostmask.split("!", 1)[1]
            self.user, self.host = ident_host.split("@", 1)
        else:
            self.user = sender_nick
            self.host = "unknown"

        self.channel = target if target.startswith("#") else ""
        self.is_privmsg = not target.startswith("#")
        self.raw = text
        self.event = "PRIVMSG"
        self.args = text.split()[1:] if text.split() else []
        self.admin = is_admin
        self.owner = is_owner

        if match_obj:
            self.match = match_obj
            self.group = match_obj.group
            self.groups = match_obj.groups
        else:
            # Emulate Sopel command regex: group(0)=full, group(1)=cmd, group(2)=args
            cmd_match = re.match(r"^!?[^\s]+\s*(.*)$", text)
            parts = text.split()
            cmd_name = parts[0].lstrip("!.") if parts else ""
            args_str = cmd_match.group(1).strip() if cmd_match else ""

            def _group(n=0):
                if n == 0:
                    return text
                elif n == 1:
                    return cmd_name
                elif n == 2:
                    return args_str
                return ""

            self.match = None
            self.group = _group
            self.groups = lambda: (cmd_name, args_str)


# Decorators for sopel.module and sopel.plugin
def commands(*names: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        if not hasattr(func, "_sopel_commands"):
            func._sopel_commands = []
        func._sopel_commands.extend(names)
        return func
    return decorator


command = commands


def rule(*patterns: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        if not hasattr(func, "_sopel_rules"):
            func._sopel_rules = []
        func._sopel_rules.extend(patterns)
        return func
    return decorator


find = rule
search = rule
url = rule


def interval(seconds: int) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_interval = seconds
        return func
    return decorator


def rate(user: int = 0, channel: int = 0, server: int = 0) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_rate = {"user": user, "channel": channel, "server": server}
        return func
    return decorator


def example(msg: str, result: Optional[str] = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_example = (msg, result)
        return func
    return decorator


def event(*events: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_events = list(events)
        return func
    return decorator


def thread(enabled: bool = True) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_thread = enabled
        return func
    return decorator


def output_prefix(prefix: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_output_prefix = prefix
        return func
    return decorator


def require_chan(*channels: str) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_require_chan = [c.lower() for c in channels]
        return func
    return decorator


def require_chanmsg(arg: Any = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_require_chanmsg = True
        return func
    if callable(arg):
        return decorator(arg)
    return decorator


def require_privmsg(arg: Any = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_require_privmsg = True
        return func
    if callable(arg):
        return decorator(arg)
    return decorator


def require_admin(arg: Any = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_require_admin = True
        return func
    if callable(arg):
        return decorator(arg)
    return decorator


def require_owner(arg: Any = None) -> Callable:
    def decorator(func: Callable) -> Callable:
        func._sopel_require_owner = True
        return func
    if callable(arg):
        return decorator(arg)
    return decorator


class module:
    commands = commands
    command = command
    rule = rule
    find = find
    search = search
    url = url
    interval = interval
    rate = rate
    example = example
    event = event
    thread = thread
    output_prefix = output_prefix
    require_chan = require_chan
    require_chanmsg = require_chanmsg
    require_privmsg = require_privmsg
    require_admin = require_admin
    require_owner = require_owner


plugin = module


def inject_sopel_shim() -> None:
    """Injects lightweight sopel, sopel.module, sopel.plugin, and sopel.tools into sys.modules."""
    sopel_mod = sys.modules.get("sopel")
    if sopel_mod is None:
        sopel_mod = types.ModuleType("sopel")
        sopel_mod.__path__ = []
        sys.modules["sopel"] = sopel_mod

    # Build sopel.module
    sopel_module_mod = sys.modules.get("sopel.module")
    if sopel_module_mod is None:
        sopel_module_mod = types.ModuleType("sopel.module")
        sopel_module_mod.__path__ = []

    for name in (
        "commands", "command", "rule", "find", "search", "url",
        "interval", "rate", "example", "event", "thread",
        "output_prefix", "require_chan", "require_chanmsg",
        "require_privmsg", "require_admin", "require_owner"
    ):
        setattr(sopel_module_mod, name, getattr(module, name))

    sopel_mod.module = sopel_module_mod
    sopel_mod.plugin = sopel_module_mod
    sys.modules["sopel.module"] = sopel_module_mod
    sys.modules["sopel.plugin"] = sopel_module_mod

    # Build sopel.tools
    if "sopel.tools" not in sys.modules:
        tools_mod = types.ModuleType("sopel.tools")
        tools_mod.SopelMemory = dict
        tools_mod.Identifier = str
        sopel_mod.tools = tools_mod
        sys.modules["sopel.tools"] = tools_mod

    # Build sopel.config
    if "sopel.config" not in sys.modules:
        cfg_mod = types.ModuleType("sopel.config")
        cfg_mod.types = types.ModuleType("sopel.config.types")
        sopel_mod.config = cfg_mod
        sys.modules["sopel.config"] = cfg_mod
        sys.modules["sopel.config.types"] = cfg_mod.types

    # Inject requests shim if system requests is missing or broken
    inject_requests_shim_if_needed()


class CaseInsensitiveDict(dict):
    """Case-insensitive dictionary for HTTP headers."""
    def __init__(self, data: Optional[Any] = None) -> None:
        super().__init__()
        if data:
            for k, v in dict(data).items():
                self[k] = v

    def __setitem__(self, key: Any, value: Any) -> None:
        super().__setitem__(str(key).lower(), value)

    def __getitem__(self, key: Any) -> Any:
        return super().__getitem__(str(key).lower())

    def __contains__(self, key: Any) -> bool:
        return super().__contains__(str(key).lower())

    def get(self, key: Any, default: Any = None) -> Any:
        return super().get(str(key).lower(), default)


class StdlibResponse:
    """Mock requests.Response backed by Python standard library."""
    def __init__(self, status_code: int = 200, headers: Optional[Dict[str, str]] = None, content: bytes = b"", url: str = "") -> None:
        self.status_code = status_code
        self.headers = CaseInsensitiveDict(headers or {})
        self.content = content or b""
        self.url = url
        self.encoding = "utf-8"
        ctype = self.headers.get("content-type", "")
        m = re.search(r'charset=([\w-]+)', ctype, re.IGNORECASE)
        if m:
            self.encoding = m.group(1).strip()

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.encoding, errors="replace")
        except Exception:
            return self.content.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self, **kwargs: Any) -> Any:
        return json.loads(self.text, **kwargs)

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            req_mod = sys.modules.get("requests")
            http_err_cls = getattr(getattr(req_mod, "exceptions", None), "HTTPError", Exception)
            raise http_err_cls(f"HTTP Error {self.status_code} for url: {self.url}")


class RequestsCodes:
    """Mock requests.codes status lookup."""
    ok = 200
    created = 201
    accepted = 202
    no_content = 204
    moved_permanently = 301
    found = 302
    not_modified = 304
    bad_request = 400
    unauthorized = 401
    forbidden = 403
    not_found = 404
    method_not_allowed = 405
    internal_server_error = 500
    bad_gateway = 502
    service_unavailable = 503
    gateway_timeout = 504

    def __getattr__(self, name: str) -> int:
        return 200


def _stdlib_request(
    method: str,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    data: Any = None,
    json: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = 10.0,
    verify: bool = True,
    **kwargs: Any
) -> StdlibResponse:
    import urllib.error
    import urllib.parse
    import urllib.request

    req_headers = {
        "User-Agent": "Minibot/1.0 (+https://github.com/Necrotyk/minibot)"
    }
    if headers:
        req_headers.update(headers)

    final_url = url
    if params:
        query_string = urllib.parse.urlencode(params)
        sep = "&" if "?" in final_url else "?"
        final_url = f"{final_url}{sep}{query_string}"

    body_bytes: Optional[bytes] = None
    if json is not None:
        import json as json_lib
        body_bytes = json_lib.dumps(json).encode("utf-8")
        if "content-type" not in [k.lower() for k in req_headers]:
            req_headers["Content-Type"] = "application/json"
    elif data is not None:
        if isinstance(data, bytes):
            body_bytes = data
        elif isinstance(data, str):
            body_bytes = data.encode("utf-8")
        elif isinstance(data, dict):
            body_bytes = urllib.parse.urlencode(data).encode("utf-8")
            if "content-type" not in [k.lower() for k in req_headers]:
                req_headers["Content-Type"] = "application/x-www-form-urlencoded"

    # Build opener with resilient SSL
    try:
        from core.guard import build_safe_opener
        opener = build_safe_opener(verify_ssl=verify)
    except Exception:
        opener = urllib.request.build_opener()

    req = urllib.request.Request(final_url, data=body_bytes, headers=req_headers, method=method.upper())

    try:
        with opener.open(req, timeout=timeout or 10.0) as resp:
            content = resp.read()
            resp_headers = dict(resp.headers.items())
            return StdlibResponse(status_code=getattr(resp, "status", 200), headers=resp_headers, content=content, url=final_url)
    except urllib.error.HTTPError as e:
        err_content = e.read() if hasattr(e, "read") else b""
        err_headers = dict(e.headers.items()) if hasattr(e, "headers") and e.headers else {}
        return StdlibResponse(status_code=e.code, headers=err_headers, content=err_content, url=final_url)
    except Exception as e:
        req_mod = sys.modules.get("requests")
        conn_err_cls = getattr(getattr(req_mod, "exceptions", None), "ConnectionError", IOError)
        raise conn_err_cls(f"Request failed: {e}") from e


def is_requests_usable() -> bool:
    """Checks if requests is importable and functional in sys.modules."""
    if "requests" in sys.modules:
        req = sys.modules["requests"]
        return hasattr(req, "get") and hasattr(req, "Response") and hasattr(req, "codes")
    try:
        import requests
        return hasattr(requests, "get") and hasattr(requests, "Response") and hasattr(requests, "codes")
    except Exception:
        return False


def inject_requests_shim_if_needed(force: bool = False) -> None:
    """Injects a standard-library based requests mock if requests is missing or broken."""
    if not force and is_requests_usable():
        return

    req_mod = types.ModuleType("requests")
    req_mod.__path__ = []

    exc_mod = types.ModuleType("requests.exceptions")

    class RequestException(IOError):
        pass

    class HTTPError(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    class Timeout(RequestException):
        pass

    class URLRequired(RequestException):
        pass

    class TooManyRedirects(RequestException):
        pass

    exc_mod.RequestException = RequestException
    exc_mod.HTTPError = HTTPError
    exc_mod.ConnectionError = ConnectionError
    exc_mod.Timeout = Timeout
    exc_mod.URLRequired = URLRequired
    exc_mod.TooManyRedirects = TooManyRedirects

    req_mod.exceptions = exc_mod
    req_mod.RequestException = RequestException
    req_mod.HTTPError = HTTPError
    req_mod.ConnectionError = ConnectionError
    req_mod.Timeout = Timeout

    class Session:
        def __init__(self) -> None:
            self.headers: Dict[str, str] = {}

        def get(self, url: str, **kwargs: Any) -> StdlibResponse:
            h = dict(self.headers)
            h.update(kwargs.pop("headers", None) or {})
            return _stdlib_request("GET", url, headers=h, **kwargs)

        def post(self, url: str, **kwargs: Any) -> StdlibResponse:
            h = dict(self.headers)
            h.update(kwargs.pop("headers", None) or {})
            return _stdlib_request("POST", url, headers=h, **kwargs)

        def head(self, url: str, **kwargs: Any) -> StdlibResponse:
            h = dict(self.headers)
            h.update(kwargs.pop("headers", None) or {})
            return _stdlib_request("HEAD", url, headers=h, **kwargs)

        def put(self, url: str, **kwargs: Any) -> StdlibResponse:
            h = dict(self.headers)
            h.update(kwargs.pop("headers", None) or {})
            return _stdlib_request("PUT", url, headers=h, **kwargs)

        def delete(self, url: str, **kwargs: Any) -> StdlibResponse:
            h = dict(self.headers)
            h.update(kwargs.pop("headers", None) or {})
            return _stdlib_request("DELETE", url, headers=h, **kwargs)

    req_mod.Session = Session
    req_mod.Response = StdlibResponse
    req_mod.codes = RequestsCodes()

    def get(url: str, **kwargs: Any) -> StdlibResponse:
        return _stdlib_request("GET", url, **kwargs)

    def post(url: str, **kwargs: Any) -> StdlibResponse:
        return _stdlib_request("POST", url, **kwargs)

    def head(url: str, **kwargs: Any) -> StdlibResponse:
        return _stdlib_request("HEAD", url, **kwargs)

    def put(url: str, **kwargs: Any) -> StdlibResponse:
        return _stdlib_request("PUT", url, **kwargs)

    def delete(url: str, **kwargs: Any) -> StdlibResponse:
        return _stdlib_request("DELETE", url, **kwargs)

    req_mod.get = get
    req_mod.post = post
    req_mod.head = head
    req_mod.put = put
    req_mod.delete = delete

    sys.modules["requests"] = req_mod
    sys.modules["requests.exceptions"] = exc_mod
