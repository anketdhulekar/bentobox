"""The HTTP call that the Toggl and speech-to-text clients share. It carries an API key, so:

  * it never follows a redirect, because urllib would send the Authorization header along to the new host
  * it refuses plain HTTP, except to this Mac (the tests' fake servers)
  * a failure returns status -1, with no exception that could reach a log
"""
import http.client
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE = 4 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_direct = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))  # to this Mac: never a proxy
_normal = urllib.request.build_opener(_NoRedirect)


def is_safe_url(url):
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK)


def call(method, url, headers, data, timeout):
    """Returns (status, body bytes, response headers). The status is -1 if there was no answer."""
    if not is_safe_url(url):
        return -1, b"", {}
    opener = _direct if urllib.parse.urlsplit(url).hostname in LOOPBACK else _normal
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.read(MAX_RESPONSE), r.headers
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(MAX_RESPONSE), e.headers
        except OSError:
            return e.code, b"", e.headers
        finally:
            e.close()
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError):
        return -1, b"", {}
