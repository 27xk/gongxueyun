import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


class FrontendAlipayVerificationTest(unittest.TestCase):
    def test_shared_dialog_only_opens_alipay_scheme_and_emits_continue(self):
        source = read("web/src/components/AlipayVerificationDialog.vue")

        self.assertIn("defineModel", source)
        self.assertIn("defineEmits(['continue'])", source)
        self.assertIn("startsWith('alipays://')", source)
        self.assertIn("前往支付宝验证", source)
        self.assertIn("验证完成，继续打卡", source)
        self.assertNotIn("outRegisterNo", source)
        self.assertNotIn("localStorage", source)

    def test_user_home_reads_run_response_and_calls_app_continue_endpoint(self):
        source = read("web/src/views/user/UserHome.vue")

        self.assertIn("AlipayVerificationDialog", source)
        self.assertIn("res.data?.results", source)
        self.assertIn("/app/clock-in/alipay/continue", source)
        self.assertIn("out_register_no", source)
        self.assertIn("target_type", source)
        self.assertNotIn("localStorage", source)

    def test_user_list_tracks_target_user_and_calls_admin_continue_endpoint(self):
        source = read("web/src/views/UserList.vue")

        self.assertIn("AlipayVerificationDialog", source)
        self.assertIn("verificationUserId.value = id", source)
        self.assertIn(
            "/users/${verificationUserId.value}/clock-in/alipay/continue",
            source,
        )
        self.assertIn("out_register_no", source)
        self.assertIn("target_type", source)
        self.assertNotIn("localStorage", source)


if __name__ == "__main__":
    unittest.main()
