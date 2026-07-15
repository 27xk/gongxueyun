import unittest
from unittest.mock import patch


class FakeURL:
    def __init__(self, scheme="https", hostname="app.example.com", port=None, path="/api/users"):
        self.scheme = scheme
        self.hostname = hostname
        self.port = port
        self.path = path


class FakeRequest:
    def __init__(
        self,
        *,
        method="POST",
        origin="",
        referer="",
        authorization="",
        cookies=None,
        csrf_header="",
        path="/api/users",
    ):
        self.method = method
        self.url = FakeURL(path=path)
        self.headers = {}
        if origin:
            self.headers["origin"] = origin
        if referer:
            self.headers["referer"] = referer
        if authorization:
            self.headers["authorization"] = authorization
        if csrf_header:
            self.headers["x-csrf-token"] = csrf_header
        self.cookies = cookies if cookies is not None else {"admin_auth_token": "cookie-token", "csrf_token": "csrf-token"}


class CookieCsrfGuardTest(unittest.TestCase):
    def test_cross_site_origin_is_rejected_for_cookie_authenticated_post(self):
        from server.security import should_reject_cookie_csrf

        request = FakeRequest(origin="https://evil.example.net")

        self.assertTrue(should_reject_cookie_csrf(request))

    def test_same_origin_post_is_allowed(self):
        from server.security import should_reject_cookie_csrf

        request = FakeRequest(origin="https://app.example.com", csrf_header="csrf-token")

        self.assertFalse(should_reject_cookie_csrf(request))

    def test_cookie_authenticated_post_requires_matching_csrf_token(self):
        from server.security import should_reject_cookie_csrf

        missing_header = FakeRequest(origin="https://app.example.com", csrf_header="")
        mismatch = FakeRequest(origin="https://app.example.com", csrf_header="wrong-token")
        missing_cookie = FakeRequest(
            origin="https://app.example.com",
            csrf_header="csrf-token",
            cookies={"admin_auth_token": "cookie-token"},
        )

        self.assertTrue(should_reject_cookie_csrf(missing_header))
        self.assertTrue(should_reject_cookie_csrf(mismatch))
        self.assertTrue(should_reject_cookie_csrf(missing_cookie))

    def test_auth_bootstrap_endpoint_allows_stale_auth_cookie_without_csrf_token(self):
        from server.security import should_reject_cookie_csrf

        request = FakeRequest(
            origin="https://app.example.com",
            csrf_header="",
            cookies={"app_auth_token": "stale-token"},
            path="/api/app/auth/login",
        )

        self.assertFalse(should_reject_cookie_csrf(request))

    def test_auth_bootstrap_endpoint_still_rejects_cross_site_origin(self):
        from server.security import should_reject_cookie_csrf

        request = FakeRequest(
            origin="https://evil.example.net",
            csrf_header="",
            cookies={"app_auth_token": "stale-token"},
            path="/api/app/auth/login",
        )

        self.assertTrue(should_reject_cookie_csrf(request))

    def test_cookie_authenticated_post_without_origin_still_requires_csrf_token(self):
        from server.security import should_reject_cookie_csrf

        self.assertTrue(should_reject_cookie_csrf(FakeRequest(csrf_header="")))
        self.assertFalse(should_reject_cookie_csrf(FakeRequest(csrf_header="csrf-token")))

    def test_null_origin_is_rejected_for_cookie_authenticated_post(self):
        from server.security import should_reject_cookie_csrf

        request = FakeRequest(origin="null")

        self.assertTrue(should_reject_cookie_csrf(request))

    def test_configured_frontend_origin_is_allowed(self):
        from server.security import should_reject_cookie_csrf

        request = FakeRequest(origin="https://front.example.com", csrf_header="csrf-token")

        with patch.dict("os.environ", {"FRONTEND_ORIGINS": "https://front.example.com"}, clear=False):
            self.assertFalse(should_reject_cookie_csrf(request))

    def test_bearer_authenticated_request_is_not_checked_as_cookie_csrf(self):
        from server.security import should_reject_cookie_csrf

        request = FakeRequest(origin="https://evil.example.net", authorization="Bearer token")

        self.assertFalse(should_reject_cookie_csrf(request))

    def test_safe_method_is_allowed(self):
        from server.security import should_reject_cookie_csrf

        request = FakeRequest(method="GET", origin="https://evil.example.net")

        self.assertFalse(should_reject_cookie_csrf(request))


if __name__ == "__main__":
    unittest.main()
