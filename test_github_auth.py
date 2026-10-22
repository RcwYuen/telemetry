import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from github_auth import GitHubDeviceAuth, oauth_request


def device_response(**overrides):
    return {
        "device_code": "private-device-code", "user_code": "ABCD-EFGH",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900, "interval": 5, **overrides,
    }


class GitHubDeviceAuthTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("github_auth.time.monotonic", return_value=100).start()
        self.request = patch("github_auth.oauth_request", return_value=device_response()).start()
        self.addCleanup(patch.stopall)
        self.auth = GitHubDeviceAuth()

    def start(self):
        return self.auth.start("PublicClient123")

    def test_start_returns_only_public_device_details(self):
        data = self.start()
        self.assertEqual(data["state"], "pending")
        self.assertEqual(data["user_code"], "ABCD-EFGH")
        self.assertEqual(data["poll_after"], 5)
        self.assertEqual(data["expires_in"], 900)
        self.assertNotIn("private-device-code", json.dumps(data))
        self.request.assert_called_once_with(
            "device/code", {"client_id": "PublicClient123", "scope": "read:user"},
        )

    def test_requires_client_id_and_disallows_duplicate_sign_in(self):
        for value in ("", "not a client id", "ghp_secret", "x" * 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.auth.start(value)
        self.request.assert_not_called()
        self.start()
        with self.assertRaisesRegex(ValueError, "already pending"):
            self.start()
        self.assertEqual(self.request.call_count, 1)

    def test_poll_uses_private_code_and_respects_exact_interval(self):
        self.start()
        self.request.return_value = {"error": "authorization_pending"}
        self.clock.return_value = 104.999
        self.auth.snapshot(poll=True)
        self.assertEqual(self.request.call_count, 1)
        self.clock.return_value = 105
        self.assertEqual(self.auth.snapshot(poll=True)["state"], "pending")
        self.request.assert_called_with("oauth/access_token", {
            "client_id": "PublicClient123", "device_code": "private-device-code",
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        self.auth.snapshot(poll=True)
        self.assertEqual(self.request.call_count, 2)

    def test_slow_down_increases_all_subsequent_poll_intervals(self):
        self.start()
        self.clock.return_value = 105
        self.request.return_value = {"error": "slow_down"}
        self.assertEqual(self.auth.snapshot(poll=True)["poll_after"], 10)
        self.clock.return_value = 114.999
        self.auth.snapshot(poll=True)
        self.assertEqual(self.request.call_count, 2)
        self.clock.return_value = 115
        self.request.return_value = {"error": "authorization_pending"}
        self.assertEqual(self.auth.snapshot(poll=True)["poll_after"], 10)

    def test_success_keeps_token_out_of_public_state_and_blocks_account_switch(self):
        self.start()
        before_revision, _ = self.auth.credentials()
        self.clock.return_value = 105
        self.request.return_value = {"access_token": "private-access-token"}
        data = self.auth.snapshot(poll=True)
        self.assertEqual(data["state"], "authorized")
        self.assertIsNone(data["user_code"])
        self.assertIsNone(data["verification_uri"])
        self.assertNotIn("private-access-token", json.dumps(data))
        revision, token = self.auth.credentials()
        self.assertGreater(revision, before_revision)
        self.assertEqual(token, "private-access-token")
        with self.assertRaisesRegex(ValueError, "Disconnect"):
            self.start()

    def test_cancel_discards_codes_and_cannot_poll_later(self):
        self.start()
        revision, _ = self.auth.credentials()
        data = self.auth.disconnect()
        self.assertEqual(data["state"], "signed_out")
        self.assertIsNone(data["user_code"])
        self.assertGreater(self.auth.credentials()[0], revision)
        self.clock.return_value = 200
        self.auth.snapshot(poll=True)
        self.assertEqual(self.request.call_count, 1)

    def test_disconnect_removes_authorized_token(self):
        self.start()
        self.clock.return_value = 105
        self.request.return_value = {"access_token": "private-token"}
        self.auth.snapshot(poll=True)
        self.auth.disconnect()
        self.assertIsNone(self.auth.credentials()[1])

    def test_pending_code_expires_without_another_network_call(self):
        self.start()
        self.clock.return_value = 1000
        data = self.auth.snapshot(poll=True)
        self.assertEqual(data["state"], "error")
        self.assertIn("expired", data["message"])
        self.assertIsNone(data["user_code"])
        self.assertEqual(self.request.call_count, 1)

    def test_expiring_access_tokens_require_new_sign_in(self):
        self.start()
        self.clock.return_value = 105
        self.request.return_value = {"access_token": "private-token", "expires_in": 10}
        self.auth.snapshot(poll=True)
        self.clock.return_value = 115
        self.assertIsNone(self.auth.credentials()[1])
        self.assertEqual(self.auth.snapshot()["state"], "error")

    def test_denial_and_github_error_descriptions_do_not_leak_response_data(self):
        self.start()
        self.clock.return_value = 105
        self.request.return_value = {
            "error": "access_denied", "error_description": "private-token",
        }
        data = self.auth.snapshot(poll=True)
        self.assertEqual(data["state"], "error")
        self.assertIn("declined", data["message"])
        self.assertNotIn("private-token", json.dumps(data))

    def test_transient_connection_error_is_visible_and_retry_is_throttled(self):
        self.start()
        self.clock.return_value = 105
        self.request.side_effect = ValueError("Cannot reach GitHub")
        data = self.auth.snapshot(poll=True)
        self.assertEqual(data["state"], "pending")
        self.assertIn("Cannot reach", data["message"])
        self.auth.snapshot(poll=True)
        self.assertEqual(self.request.call_count, 2)

    def test_invalid_start_payloads_fail_explicitly(self):
        for overrides in [
            {"verification_uri": "https://untrusted.example"},
            {"device_code": None}, {"user_code": None}, {"interval": 0},
            {"expires_in": True}, {"expires_in": -1},
            {"error": "device_flow_disabled"},
        ]:
            with self.subTest(overrides=overrides):
                self.request.return_value = device_response(**overrides)
                data = self.start()
                self.assertEqual(data["state"], "error")
                self.assertIsNone(data["user_code"])

    def test_invalid_tokens_and_expiry_fail_without_exposing_values(self):
        for payload in [
            {"access_token": "private\ninvalid"},
            {"access_token": ""}, {"access_token": "private", "expires_in": -1},
            {},
        ]:
            with self.subTest(payload=payload):
                self.auth = GitHubDeviceAuth()
                self.request.return_value = device_response()
                self.start()
                self.clock.return_value += 5
                self.request.return_value = payload
                data = self.auth.snapshot(poll=True)
                self.assertEqual(data["state"], "error")
                self.assertIsNone(self.auth.credentials()[1])
                self.assertNotIn("private", json.dumps(data))


class OAuthTransportTests(unittest.TestCase):
    @patch("github_auth.urlopen")
    def test_standard_library_posts_form_and_requests_json(self, open_url):
        open_url.return_value.__enter__.return_value.read.return_value = '{"interval":5}'
        self.assertEqual(oauth_request("device/code", {"client_id": "public"}), {"interval": 5})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://github.com/login/device/code")
        self.assertEqual(request.data, b"client_id=public")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(open_url.call_args.kwargs["timeout"], 10)

    @patch("github_auth.urlopen")
    def test_http_network_and_malformed_responses_are_safe_errors(self, open_url):
        for error in [
            HTTPError("private", 403, "private", {}, None),
            URLError("private"), TimeoutError("private"),
        ]:
            with self.subTest(error=type(error).__name__):
                open_url.side_effect = error
                with self.assertRaises(ValueError) as caught:
                    oauth_request("device/code", {})
                self.assertNotIn("private", str(caught.exception))
        open_url.side_effect = None
        for response in ("bad JSON", "[]", '{"error":[]}'):
            open_url.return_value.__enter__.return_value.read.return_value = response
            with self.subTest(response=response), self.assertRaises(ValueError):
                oauth_request("device/code", {})


if __name__ == "__main__":
    unittest.main()
