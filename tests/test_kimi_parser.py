import copy
import unittest
from datetime import datetime

from main import Provider, parse_kimi_payload


def epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


NEW_MAX_PAYLOAD = {
    "limits": [
        {
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {
                "limit": "100",
                "used": "44",
                "remaining": "56",
                "resetTime": "2026-10-16T05:24:16.591977Z",
            },
        }
    ],
    "booster_wallet": {
        "monthlyChargeLimit": 5000,
        "monthlyUsed": 100,
    },
    "usages": {
        "limit_5h": {
            "used_ratio": 0.4429,
            "reset_time": "2026-10-16T05:24:15Z",
        },
        "limit_month_total": {
            "used_ratio": 0.0177,
            "reset_time": "2026-11-16T00:00:00Z",
        },
        "limit_month_code": {
            "used_ratio": 0.0177,
            "reset_time": "2026-11-16T00:00:00Z",
        },
    },
}


OLD_PAYLOAD = {
    "usage": {
        "limit": "1000",
        "used": "250",
        "remaining": "750",
        "resetTime": "2026-10-23T00:00:00Z",
    },
    "limits": [
        {
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {
                "limit": "100",
                "used": "40",
                "remaining": "60",
                "resetTime": "2026-10-16T08:00:00Z",
            },
        }
    ],
    "user": {"membership": {"level": "LEVEL_ADVANCED"}},
}


class KimiParserTests(unittest.TestCase):
    def test_new_max_uses_precise_ratios_and_only_two_rows(self):
        windows, level = parse_kimi_payload(copy.deepcopy(NEW_MAX_PAYLOAD))

        self.assertEqual([w.label for w in windows], ["5小时额度", "月度总额度"])
        self.assertEqual(level, "")
        five_hour, monthly = windows
        self.assertEqual(five_hour.used, 44.29)
        self.assertEqual(five_hour.remaining, 55.71)
        self.assertEqual(five_hour.pct, 44.29)
        self.assertEqual(five_hour.reset_epoch, epoch("2026-10-16T05:24:15Z"))
        self.assertTrue(five_hour.is_percentage)
        self.assertEqual(monthly.used, 1.77)
        self.assertEqual(monthly.remaining, 98.23)
        self.assertEqual(monthly.reset_epoch, epoch("2026-11-16T00:00:00Z"))
        self.assertTrue(monthly.is_percentage)
        self.assertNotIn("limit_month_code", [w.label for w in windows])

        provider = Provider("Kimi", True, windows=windows)
        self.assertEqual(provider.long_term_window.label, "月度总额度")
        self.assertFalse(provider.is_exhausted)

    def test_legacy_weekly_payload_still_works(self):
        windows, level = parse_kimi_payload(copy.deepcopy(OLD_PAYLOAD))

        self.assertEqual([w.label for w in windows], ["5小时额度", "周额度"])
        self.assertEqual(level, "Advanced")
        five_hour, weekly = windows
        self.assertEqual((five_hour.used, five_hour.remaining), (40.0, 60.0))
        self.assertEqual((weekly.limit, weekly.used, weekly.remaining), (1000.0, 250.0, 750.0))
        self.assertFalse(weekly.is_percentage)

        provider = Provider("Kimi", True, windows=windows)
        self.assertEqual(provider.long_term_window.label, "周额度")
        self.assertFalse(provider.is_exhausted)

    def test_mixed_payload_deduplicates_5h_and_prefers_weekly(self):
        payload = copy.deepcopy(OLD_PAYLOAD)
        payload["usages"] = copy.deepcopy(NEW_MAX_PAYLOAD["usages"])

        windows, _ = parse_kimi_payload(payload)

        self.assertEqual([w.label for w in windows], ["5小时额度", "周额度"])
        self.assertEqual(windows[0].used, 44.29)
        self.assertEqual(windows[0].reset_epoch, epoch("2026-10-16T05:24:15Z"))

    def test_long_term_exhaustion_works_for_weekly_and_monthly(self):
        old = copy.deepcopy(OLD_PAYLOAD)
        old["usage"]["used"] = "1000"
        old["usage"]["remaining"] = "0"
        old_windows, _ = parse_kimi_payload(old)
        self.assertTrue(Provider("Kimi", True, windows=old_windows).is_exhausted)

        new = copy.deepcopy(NEW_MAX_PAYLOAD)
        new["usages"]["limit_month_total"]["used_ratio"] = 1.0
        new_windows, _ = parse_kimi_payload(new)
        provider = Provider("Kimi", True, windows=new_windows)
        self.assertEqual(provider.long_term_window.label, "月度总额度")
        self.assertTrue(provider.is_exhausted)

    def test_five_hour_only_payload_does_not_need_long_term_quota(self):
        payload = {"usages": {"limit_5h": {"used_ratio": 1.0, "reset_time": ""}}}
        windows, _ = parse_kimi_payload(payload)

        self.assertEqual([w.label for w in windows], ["5小时额度"])
        provider = Provider("Kimi", True, windows=windows)
        self.assertIsNone(provider.long_term_window)
        self.assertTrue(provider.is_exhausted)


if __name__ == "__main__":
    unittest.main()
