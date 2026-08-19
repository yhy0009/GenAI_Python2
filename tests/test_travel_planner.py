import argparse
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import travel_planner


class DateValidationTests(unittest.TestCase):
    def test_accepts_valid_iso_date(self):
        self.assertEqual(
            travel_planner.parse_travel_date("2026-09-15"), "2026-09-15"
        )

    def test_rejects_wrong_format(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            travel_planner.parse_travel_date("2026/09/15")

    def test_rejects_impossible_date(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            travel_planner.parse_travel_date("2026-02-30")


class RecommendationValidationTests(unittest.TestCase):
    def test_accepts_required_recommendation_shape(self):
        payload = {
            "recommended_city": " 제주 ",
            "weather": " 선선한 날씨 ",
            "events": [" 지역 행사 후보 "],
            "reason": "여행하기 좋은 시기입니다. 야외 활동에 적합합니다.",
        }

        result = travel_planner.validate_recommendation_payload(payload)

        self.assertEqual(result["recommended_city"], "제주")
        self.assertEqual(result["events"], ["지역 행사 후보"])

    def test_rejects_missing_required_key(self):
        with self.assertRaises(ValueError):
            travel_planner.validate_recommendation_payload(
                {
                    "recommended_city": "제주",
                    "weather": "선선함",
                    "events": ["행사"],
                }
            )

    def test_rejects_too_many_events(self):
        with self.assertRaises(ValueError):
            travel_planner.validate_recommendation_payload(
                {
                    "recommended_city": "제주",
                    "weather": "선선함",
                    "events": ["1", "2", "3", "4"],
                    "reason": "추천 이유",
                }
            )

    def test_retries_invalid_json_once(self):
        valid_payload = {
            "recommended_city": "제주",
            "weather": "선선함",
            "events": ["지역 행사 후보"],
            "reason": "여행하기 좋은 시기입니다. 야외 활동에 적합합니다.",
        }

        class FakeResponses:
            def __init__(self):
                self.calls = []
                self.outputs = ["not json", json.dumps(valid_payload)]

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return types.SimpleNamespace(output_text=self.outputs.pop(0))

        responses = FakeResponses()
        client = types.SimpleNamespace(responses=responses)

        result = travel_planner.generate_recommendation(
            client, "test-model", "2026-09-15"
        )

        self.assertEqual(result["recommended_city"], "제주")
        self.assertEqual(len(responses.calls), 2)
        self.assertEqual(
            responses.calls[0]["text"]["format"]["type"], "json_schema"
        )


class KakaoMappingTests(unittest.TestCase):
    def test_normalizes_kakao_place(self):
        document = {
            "place_name": "테스트 식당",
            "address_name": "제주 제주시 구주소",
            "road_address_name": "제주 제주시 새주소",
            "category_name": "음식점 > 한식",
            "place_url": "https://place.map.kakao.com/1",
            "x": "126.1",
            "y": "33.5",
        }

        result = travel_planner.normalize_kakao_place(document)

        self.assertEqual(result["name"], "테스트 식당")
        self.assertEqual(result["address"], "제주 제주시 새주소")
        self.assertEqual(result["x"], 126.1)
        self.assertEqual(result["y"], 33.5)

    def test_search_uses_rest_key_and_food_category(self):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "documents": [
                        {
                            "place_name": "테스트 식당",
                            "address_name": "제주 제주시",
                            "x": "126.1",
                            "y": "33.5",
                        }
                    ]
                }

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

        fake_requests = types.SimpleNamespace(get=fake_get)
        previous = sys.modules.get("requests")
        sys.modules["requests"] = fake_requests
        try:
            result = travel_planner.search_kakao_restaurants("제주", "secret-key")
        finally:
            if previous is None:
                sys.modules.pop("requests", None)
            else:
                sys.modules["requests"] = previous

        self.assertEqual(len(result), 1)
        self.assertEqual(captured["url"], travel_planner.KAKAO_KEYWORD_SEARCH_URL)
        self.assertEqual(captured["headers"]["Authorization"], "KakaoAK secret-key")
        self.assertEqual(captured["params"]["query"], "제주 맛집")
        self.assertEqual(captured["params"]["category_group_code"], "FD6")
        self.assertEqual(captured["params"]["size"], 5)


class FallbackReportTests(unittest.TestCase):
    def test_fallback_report_contains_all_required_sections(self):
        data = {
            "requested_date": "2026-09-15",
            "recommendation": {
                "recommended_city": "제주",
                "weather": "선선함",
                "events": ["지역 행사 후보"],
                "reason": "여행하기 좋은 시기입니다.",
            },
            "restaurants": [],
            "errors": [],
        }

        report = travel_planner.build_fallback_report(data)

        self.assertIn("데이터 없음", report)
        for heading in travel_planner.REQUIRED_REPORT_HEADINGS:
            self.assertIn(heading, report)

    def test_report_validation_rejects_missing_sections(self):
        with self.assertRaises(travel_planner.ReportFormatError):
            travel_planner.validate_report("# 제목\n\n## 추천 지역\n\n제주")


class SecurityTests(unittest.TestCase):
    def test_safe_error_message_redacts_api_keys(self):
        message = travel_planner.safe_error_message(
            RuntimeError("request failed with secret-key"), ("secret-key",)
        )

        self.assertNotIn("secret-key", message)
        self.assertIn("[REDACTED]", message)


class RunFailureTests(unittest.TestCase):
    def test_first_llm_failure_still_saves_json_and_markdown(self):
        settings = travel_planner.Settings("openai-secret", "kakao-secret", "model")

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(travel_planner, "RESULTS_DIR", Path(temp_dir)),
                patch.object(travel_planner, "create_openai_client", return_value=object()),
                patch.object(
                    travel_planner,
                    "generate_recommendation",
                    side_effect=RuntimeError("failed with openai-secret"),
                ),
            ):
                exit_code = travel_planner.run("2026-09-15", settings)

            json_path = Path(temp_dir) / "2026-09-15_travel_data.json"
            report_path = Path(temp_dir) / "2026-09-15_travel_plan.md"
            payload = json.loads(json_path.read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 1)
            self.assertTrue(report_path.exists())
            self.assertEqual(payload["errors"][0]["type"], "LLM_ERROR")
            self.assertNotIn("openai-secret", json_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
