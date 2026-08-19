import argparse
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import call, patch

import travel_planner


def make_city(city: str) -> dict:
    return {
        "city": city,
        "weather": "선선한 날씨가 예상되는 시기입니다.",
        "events": ["지역 행사 후보"],
        "reason": "여행하기 좋은 시기입니다. 야외 활동에 적합합니다.",
    }


def make_cached_data(travel_date: str = "2026-09-15") -> dict:
    recommendations = []
    for city in ("강릉", "경주"):
        recommendation = make_city(city)
        recommendation["restaurants"] = [
            {
                "name": f"{city} 식당",
                "address": f"{city} 중앙로 1",
                "category": "음식점 > 한식",
                "url": "https://place.map.kakao.com/1",
                "x": 127.0,
                "y": 37.0,
            }
        ]
        recommendations.append(recommendation)
    return {
        "schema_version": travel_planner.CACHE_SCHEMA_VERSION,
        "requested_date": travel_date,
        "recommendations": recommendations,
        "errors": [],
    }


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

    def test_parser_accepts_refresh_option(self):
        args = travel_planner.build_parser().parse_args(
            ["--date", "2026-09-15", "--refresh"]
        )

        self.assertTrue(args.refresh)


class CityNormalizationTests(unittest.TestCase):
    def test_maps_standard_names_and_removes_administrative_suffixes(self):
        self.assertEqual(
            travel_planner.normalize_city_name(
                "  부산광역시 / 해운대구 (우동)  "
            ),
            "부산 해운대 우동",
        )
        self.assertEqual(
            travel_planner.normalize_city_name("강원특별자치도 강릉시"),
            "강원 강릉",
        )

    def test_deduplicates_tokens_and_keeps_at_most_three_keywords(self):
        self.assertEqual(
            travel_planner.normalize_city_name("제주특별자치도 제주시 애월읍 한림면"),
            "제주 애월 한림",
        )

    def test_recommendation_detects_duplicates_after_normalization(self):
        with self.assertRaises(ValueError):
            travel_planner.validate_recommendation_payload(
                {
                    "recommended_cities": [
                        make_city("제주특별자치도"),
                        make_city("제주도"),
                    ]
                }
            )


class RecommendationValidationTests(unittest.TestCase):
    def test_accepts_two_or_three_recommendations(self):
        payload = {
            "recommended_cities": [make_city(" 강릉 "), make_city("경주")]
        }

        result = travel_planner.validate_recommendation_payload(payload)

        self.assertEqual(len(result["recommended_cities"]), 2)
        self.assertEqual(result["recommended_cities"][0]["city"], "강릉")
        self.assertEqual(
            result["recommended_cities"][0]["events"], ["지역 행사 후보"]
        )

    def test_rejects_only_one_recommendation(self):
        with self.assertRaises(ValueError):
            travel_planner.validate_recommendation_payload(
                {"recommended_cities": [make_city("제주")]}
            )

    def test_rejects_duplicate_cities(self):
        with self.assertRaises(ValueError):
            travel_planner.validate_recommendation_payload(
                {"recommended_cities": [make_city("제주"), make_city("제주")]}
            )

    def test_rejects_too_many_events(self):
        city = make_city("제주")
        city["events"] = ["1", "2", "3", "4"]
        with self.assertRaises(ValueError):
            travel_planner.validate_recommendation_payload(
                {"recommended_cities": [city, make_city("경주")]}
            )

    def test_retries_invalid_json_once(self):
        valid_payload = {
            "recommended_cities": [make_city("제주"), make_city("경주")]
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

        self.assertEqual(len(result["recommended_cities"]), 2)
        self.assertEqual(len(responses.calls), 2)
        output_format = responses.calls[0]["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertEqual(
            output_format["schema"]["properties"]["recommended_cities"]["minItems"],
            2,
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
            result = travel_planner.search_kakao_restaurants(
                "부산광역시 해운대구", "secret-key"
            )
        finally:
            if previous is None:
                sys.modules.pop("requests", None)
            else:
                sys.modules["requests"] = previous

        self.assertEqual(len(result), 1)
        self.assertEqual(captured["url"], travel_planner.KAKAO_KEYWORD_SEARCH_URL)
        self.assertEqual(captured["headers"]["Authorization"], "KakaoAK secret-key")
        self.assertEqual(captured["params"]["query"], "부산 해운대 맛집")
        self.assertEqual(captured["params"]["category_group_code"], "FD6")
        self.assertEqual(captured["params"]["size"], 5)

    def test_search_rejects_size_outside_kakao_range(self):
        with self.assertRaises(ValueError):
            travel_planner.search_kakao_restaurants("제주", "secret-key", size=16)


class PlaceSearchPluginTests(unittest.TestCase):
    def test_creates_registered_provider_through_common_interface(self):
        provider_name = "test-map"

        class TestProvider:
            provider_name = "test-map"

            def search_restaurants(self, city, *, size=5):
                return [{"name": city, "address": "", "url": None}][:size]

        travel_planner.register_place_search_provider(
            provider_name, lambda settings: TestProvider()
        )
        try:
            settings = travel_planner.Settings(
                "openai-secret", "", "model", provider_name
            )
            provider = travel_planner.create_place_search_provider(settings)

            result = provider.search_restaurants("강릉", size=1)
        finally:
            travel_planner.PLACE_SEARCH_PROVIDER_FACTORIES.pop(provider_name, None)

        self.assertEqual(provider.provider_name, provider_name)
        self.assertEqual(result[0]["name"], "강릉")

    def test_rejects_unregistered_provider(self):
        settings = travel_planner.Settings(
            "openai-secret", "", "model", "missing-map"
        )

        with self.assertRaises(travel_planner.ConfigurationError):
            travel_planner.create_place_search_provider(settings)


class FallbackReportTests(unittest.TestCase):
    def test_fallback_report_organizes_results_by_city(self):
        data = make_cached_data()
        data["recommendations"][1]["restaurants"] = []

        report = travel_planner.build_fallback_report(data)

        self.assertIn("### 1. 강릉", report)
        self.assertIn("### 2. 경주", report)
        self.assertIn("데이터 없음", report)
        self.assertEqual(report.count("#### 맛집 추천"), 2)
        for heading in travel_planner.REQUIRED_REPORT_HEADINGS:
            self.assertIn(heading, report)

    def test_report_validation_rejects_incomplete_city_sections(self):
        report = travel_planner.build_fallback_report(make_cached_data())

        with self.assertRaises(travel_planner.ReportFormatError):
            travel_planner.validate_report(report, ["강릉", "경주", "제주"])

    def test_report_validation_rejects_missing_city_name(self):
        report = travel_planner.build_fallback_report(make_cached_data())

        with self.assertRaises(travel_planner.ReportFormatError):
            travel_planner.validate_report(report, ["강릉", "부산"])


class CacheTests(unittest.TestCase):
    def test_validates_current_cache_and_rejects_legacy_shape(self):
        result = travel_planner.validate_cached_travel_data(
            make_cached_data(), "2026-09-15"
        )

        self.assertEqual(len(result["recommendations"]), 2)
        with self.assertRaises(travel_planner.CacheFormatError):
            travel_planner.validate_cached_travel_data(
                {
                    "requested_date": "2026-09-15",
                    "recommendation": {"recommended_city": "제주"},
                },
                "2026-09-15",
            )

    def test_cache_hit_reuses_report_without_loading_keys_or_calling_apis(self):
        data = make_cached_data()
        report = travel_planner.build_fallback_report(data)

        with tempfile.TemporaryDirectory() as temp_dir:
            result_dir = Path(temp_dir)
            json_path = result_dir / "2026-09-15_travel_data.json"
            report_path = result_dir / "2026-09-15_travel_plan.md"
            json_path.write_text(json.dumps(data), encoding="utf-8")
            report_path.write_text(report, encoding="utf-8")

            with (
                patch.object(travel_planner, "RESULTS_DIR", result_dir),
                patch.object(
                    travel_planner,
                    "load_settings",
                    side_effect=AssertionError("API 키를 읽으면 안 됩니다."),
                ),
                patch.object(
                    travel_planner,
                    "create_openai_client",
                    side_effect=AssertionError("OpenAI를 호출하면 안 됩니다."),
                ),
                patch.object(
                    travel_planner,
                    "search_kakao_restaurants",
                    side_effect=AssertionError("Kakao를 호출하면 안 됩니다."),
                ),
            ):
                exit_code = travel_planner.run("2026-09-15")

            self.assertEqual(exit_code, 0)
            self.assertEqual(report_path.read_text(encoding="utf-8"), report)

    def test_cache_json_regenerates_missing_report_without_api_calls(self):
        data = make_cached_data()

        with tempfile.TemporaryDirectory() as temp_dir:
            result_dir = Path(temp_dir)
            json_path = result_dir / "2026-09-15_travel_data.json"
            report_path = result_dir / "2026-09-15_travel_plan.md"
            json_path.write_text(json.dumps(data), encoding="utf-8")

            with (
                patch.object(travel_planner, "RESULTS_DIR", result_dir),
                patch.object(
                    travel_planner,
                    "load_settings",
                    side_effect=AssertionError("API 키를 읽으면 안 됩니다."),
                ),
                patch.object(
                    travel_planner,
                    "create_openai_client",
                    side_effect=AssertionError("OpenAI를 호출하면 안 됩니다."),
                ),
            ):
                exit_code = travel_planner.run("2026-09-15")

            self.assertEqual(exit_code, 0)
            self.assertTrue(report_path.exists())
            self.assertIn("### 1. 강릉", report_path.read_text(encoding="utf-8"))


class SecurityTests(unittest.TestCase):
    def test_safe_error_message_redacts_api_keys(self):
        message = travel_planner.safe_error_message(
            RuntimeError("request failed with secret-key"), ("secret-key",)
        )

        self.assertNotIn("secret-key", message)
        self.assertIn("[REDACTED]", message)


class RunTests(unittest.TestCase):
    def test_success_searches_each_city_and_saves_grouped_json(self):
        settings = travel_planner.Settings("openai-secret", "kakao-secret", "model")
        recommendation = {
            "recommended_cities": [
                make_city("강릉"),
                make_city("경주"),
                make_city("제주"),
            ]
        }

        def fake_search(city, api_key, *, size=5):
            return [
                {
                    "name": f"{city} 식당",
                    "address": f"{city} 중앙로",
                    "category": None,
                    "url": None,
                    "x": None,
                    "y": None,
                }
            ]

        def fake_report(client, model, travel_data):
            return travel_planner.build_fallback_report(travel_data)

        with tempfile.TemporaryDirectory() as temp_dir:
            result_dir = Path(temp_dir)
            with (
                patch.object(travel_planner, "RESULTS_DIR", result_dir),
                patch.object(travel_planner, "create_openai_client", return_value=object()),
                patch.object(
                    travel_planner,
                    "generate_recommendation",
                    return_value=recommendation,
                ),
                patch.object(
                    travel_planner,
                    "search_kakao_restaurants",
                    side_effect=fake_search,
                ) as search_mock,
                patch.object(
                    travel_planner,
                    "generate_final_report",
                    side_effect=fake_report,
                ),
            ):
                exit_code = travel_planner.run(
                    "2026-09-15", settings, refresh=True
                )

            payload = json.loads(
                (result_dir / "2026-09-15_travel_data.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                search_mock.call_args_list,
                [
                    call("강릉", "kakao-secret", size=5),
                    call("경주", "kakao-secret", size=5),
                    call("제주", "kakao-secret", size=5),
                ],
            )
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(len(payload["recommendations"]), 3)
            self.assertEqual(
                payload["recommendations"][1]["restaurants"][0]["name"],
                "경주 식당",
            )

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
                exit_code = travel_planner.run(
                    "2026-09-15", settings, refresh=True
                )

            json_path = Path(temp_dir) / "2026-09-15_travel_data.json"
            report_path = Path(temp_dir) / "2026-09-15_travel_plan.md"
            payload = json.loads(json_path.read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 1)
            self.assertTrue(report_path.exists())
            self.assertEqual(payload["errors"][0]["type"], "LLM_ERROR")
            self.assertNotIn("openai-secret", json_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
