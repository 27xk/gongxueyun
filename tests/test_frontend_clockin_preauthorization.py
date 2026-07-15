import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


class FrontendClockInPreauthorizationTest(unittest.TestCase):
    def test_shared_page_uses_two_api_prefixes_and_past_scope(self):
        source = read("web/src/components/ClockInPreauthorizationPage.vue")

        self.assertIn("/app/clock-in/preauthorizations", source)
        self.assertIn("/users/${props.userId}/clock-in/preauthorizations", source)
        self.assertIn("scope: 'past'", source)
        self.assertIn("ClockInPreauthorizationDialog", source)
        self.assertIn("ClockInPreauthorizationList", source)
        self.assertNotIn("localStorage", source)
        self.assertNotIn("outRegisterNo", source)

    def test_list_has_flat_table_mobile_rows_and_four_states(self):
        source = read("web/src/components/ClockInPreauthorizationList.vue")

        self.assertIn("el-table", source)
        self.assertIn(':row-key="rowKey"', source)
        self.assertIn("item.target_type", source)
        self.assertIn("mobile-list", source)
        self.assertIn("待授权", source)
        self.assertIn("已授权", source)
        self.assertIn("已使用", source)
        self.assertIn("需重新授权", source)
        self.assertIn("开始预授权", source)

    def test_dialog_has_two_open_methods_and_explicit_completion(self):
        source = read("web/src/components/ClockInPreauthorizationDialog.vue")

        self.assertIn("defineModel", source)
        self.assertIn("浏览器打开", source)
        self.assertIn("支付宝打开", source)
        self.assertIn("我已完成授权", source)
        self.assertIn("noopener,noreferrer", source)
        self.assertIn("startsWith('alipays://')", source)
        self.assertIn("startsWith('https://ds.alipay.com/')", source)
        self.assertNotIn("outRegisterNo", source)
        self.assertNotIn("localStorage", source)

    def test_route_views_are_thin_shared_page_wrappers(self):
        user_source = read("web/src/views/user/UserPreauthorizations.vue")
        admin_source = read("web/src/views/UserPreauthorizations.vue")

        self.assertIn('mode="user"', user_source)
        self.assertIn('mode="admin"', admin_source)
        self.assertIn("ClockInPreauthorizationPage", user_source)
        self.assertIn("ClockInPreauthorizationPage", admin_source)


if __name__ == "__main__":
    unittest.main()
