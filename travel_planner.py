"""OpenAI와 Kakao Local API를 연동한 국내 여행 추천 CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence


BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "results"
KAKAO_KEYWORD_SEARCH_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
CACHE_SCHEMA_VERSION = 2

CITY_RECOMMENDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "minLength": 1},
        "weather": {"type": "string", "minLength": 1},
        "events": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "maxItems": 3,
        },
        "reason": {"type": "string", "minLength": 1},
    },
    "required": ["city", "weather", "events", "reason"],
    "additionalProperties": False,
}

RECOMMENDATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "recommended_cities": {
            "type": "array",
            "items": CITY_RECOMMENDATION_SCHEMA,
            "minItems": 2,
            "maxItems": 3,
        }
    },
    "required": ["recommended_cities"],
    "additionalProperties": False,
}

PER_CITY_REPORT_HEADINGS = (
    "#### 추천 이유",
    "#### 날씨 요약",
    "#### 행사/축제",
    "#### 맛집 추천",
    "#### 1일 일정 제안",
)

REQUIRED_REPORT_HEADINGS = (
    "## 지역별 추천",
    *PER_CITY_REPORT_HEADINGS,
    "## 오류 요약(errors)",
)


class ConfigurationError(RuntimeError):
    """필수 실행 환경이 준비되지 않은 경우."""


class RecommendationFormatError(RuntimeError):
    """LLM 추천 결과가 요구된 JSON 구조와 맞지 않는 경우."""


class ReportFormatError(RuntimeError):
    """최종 Markdown 리포트에 필수 섹션이 없는 경우."""


class CacheFormatError(RuntimeError):
    """저장된 JSON을 현재 버전의 캐시로 사용할 수 없는 경우."""


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    kakao_rest_api_key: str
    openai_model: str


def parse_travel_date(value: str) -> str:
    """YYYY-MM-DD 형식이며 실제로 존재하는 날짜인지 검사한다."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("날짜는 YYYY-MM-DD 형식이어야 합니다.")

    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("존재하지 않는 날짜입니다.") from exc
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="여행 날짜를 바탕으로 국내 여행지와 맛집을 추천합니다."
    )
    parser.add_argument(
        "-date",
        "--date",
        dest="travel_date",
        required=True,
        type=parse_travel_date,
        metavar="YYYY-MM-DD",
        help="여행 날짜 (예: 2026-09-15)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="같은 날짜의 캐시가 있어도 API를 다시 호출합니다.",
    )
    return parser


def load_settings() -> Settings:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError as exc:
        raise ConfigurationError(
            "필수 패키지가 없습니다. 'pip install -r requirements.txt'를 먼저 실행하세요."
        ) from exc

    load_dotenv(BASE_DIR / ".env")

    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    kakao_rest_api_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    openai_model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip()

    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", openai_api_key),
            ("KAKAO_REST_API_KEY", kakao_rest_api_key),
        )
        if not value
    ]
    if missing:
        joined = ", ".join(missing)
        raise ConfigurationError(
            f"필수 API 키가 설정되지 않았습니다: {joined}. "
            ".env.example을 복사해 .env를 만든 뒤 값을 설정하세요."
        )
    if not openai_model:
        raise ConfigurationError("OPENAI_MODEL 값이 비어 있습니다.")

    return Settings(openai_api_key, kakao_rest_api_key, openai_model)


def create_openai_client(api_key: str) -> Any:
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:
        raise ConfigurationError(
            "openai 패키지가 없습니다. 'pip install -r requirements.txt'를 실행하세요."
        ) from exc
    return OpenAI(api_key=api_key, timeout=30.0, max_retries=1)


def validate_city_recommendation(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("각 지역 추천은 JSON 객체여야 합니다.")

    required_types = {
        "city": str,
        "weather": str,
        "events": list,
        "reason": str,
    }
    for key, expected_type in required_types.items():
        if key not in payload:
            raise ValueError(f"지역 추천에 필수 키가 없습니다: {key}")
        if not isinstance(payload[key], expected_type):
            raise ValueError(f"{key}의 타입이 올바르지 않습니다.")

    for key in ("city", "weather", "reason"):
        if not payload[key].strip():
            raise ValueError(f"{key} 값이 비어 있습니다.")

    events = payload["events"]
    if not 1 <= len(events) <= 3:
        raise ValueError("events는 1개 이상 3개 이하이어야 합니다.")
    if any(not isinstance(item, str) or not item.strip() for item in events):
        raise ValueError("events의 모든 항목은 비어 있지 않은 문자열이어야 합니다.")

    return {
        "city": payload["city"].strip(),
        "weather": payload["weather"].strip(),
        "events": [item.strip() for item in events],
        "reason": payload["reason"].strip(),
    }


def validate_recommendation_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("추천 결과의 최상위 값은 JSON 객체여야 합니다.")

    recommended_cities = payload.get("recommended_cities")
    if not isinstance(recommended_cities, list):
        raise ValueError("recommended_cities는 목록이어야 합니다.")
    if not 2 <= len(recommended_cities) <= 3:
        raise ValueError("recommended_cities는 2개 이상 3개 이하이어야 합니다.")

    normalized = [
        validate_city_recommendation(item) for item in recommended_cities
    ]
    city_names = [item["city"] for item in normalized]
    if len(set(city_names)) != len(city_names):
        raise ValueError("recommended_cities에 중복 지역이 있습니다.")

    return {"recommended_cities": normalized}


def generate_recommendation(client: Any, model: str, travel_date: str) -> dict[str, Any]:
    """Structured Outputs로 추천 JSON을 받고 형식 오류 시 한 번 재시도한다."""
    system_prompt = (
        "당신은 국내 여행 추천 도우미입니다. 입력 날짜에 여행하기 좋은 서로 다른 "
        "국내 도시를 2~3개 추천하세요. 날씨와 행사는 확정 정보처럼 단정하지 말고 "
        "해당 시기의 일반적 경향 또는 후보임을 명확히 표현하세요. 각 reason은 "
        "한국어 2~4문장으로 작성하세요."
    )

    last_error: Exception | None = None
    for attempt in range(2):
        retry_note = ""
        if attempt == 1:
            retry_note = (
                "\n이전 응답을 처리하지 못했습니다. 이번에는 제공된 JSON Schema를 "
                "정확히 따르고 서로 다른 지역 2~3개의 필수 값을 모두 채우세요."
            )

        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"여행 날짜: {travel_date}{retry_note}",
                },
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "travel_recommendations",
                    "strict": True,
                    "schema": RECOMMENDATION_SCHEMA,
                }
            },
        )

        try:
            parsed = json.loads(response.output_text)
            return validate_recommendation_payload(parsed)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            last_error = exc
            if attempt == 0:
                print("  - 추천 JSON 파싱 실패, 프롬프트를 보강해 1회 재시도합니다.")

    raise RecommendationFormatError(
        f"추천 JSON을 두 번 모두 파싱하지 못했습니다: {last_error}"
    )


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize_kakao_place(document: dict[str, Any]) -> dict[str, Any]:
    """Kakao 응답 필드를 과제 제출용 공통 구조로 변환한다."""
    return {
        "name": str(document.get("place_name", "")).strip(),
        "address": str(
            document.get("road_address_name") or document.get("address_name") or ""
        ).strip(),
        "category": str(document.get("category_name", "")).strip() or None,
        "url": str(document.get("place_url", "")).strip() or None,
        "x": _optional_float(document.get("x")),
        "y": _optional_float(document.get("y")),
    }


def search_kakao_restaurants(
    city: str, api_key: str, *, size: int = 5
) -> list[dict[str, Any]]:
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise ConfigurationError(
            "requests 패키지가 없습니다. 'pip install -r requirements.txt'를 실행하세요."
        ) from exc

    response = requests.get(
        KAKAO_KEYWORD_SEARCH_URL,
        headers={"Authorization": f"KakaoAK {api_key}"},
        params={
            "query": f"{city} 맛집",
            "category_group_code": "FD6",
            "size": size,
            "sort": "accuracy",
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    documents = payload.get("documents", [])
    if not isinstance(documents, list):
        raise ValueError("Kakao API 응답의 documents가 목록이 아닙니다.")
    return [normalize_kakao_place(item) for item in documents[:size]]


def make_error(
    step: str, error_type: str, message: str, *, city: str | None = None
) -> dict[str, str]:
    result = {"step": step, "type": error_type, "message": message}
    if city:
        result["city"] = city
    return result


def safe_error_message(error: Exception, secrets: Sequence[str] = ()) -> str:
    message = str(error).strip() or error.__class__.__name__
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:500]


def classify_place_error(error: Exception) -> str:
    status_code = getattr(getattr(error, "response", None), "status_code", None)
    if status_code in (401, 403):
        return "AUTH_ERROR"
    if status_code == 429:
        return "QUOTA_ERROR"
    if status_code is not None:
        return "HTTP_ERROR"
    if error.__class__.__name__ in {"Timeout", "ConnectTimeout", "ReadTimeout"}:
        return "TIMEOUT_ERROR"
    return "NETWORK_OR_PARSE_ERROR"


def validate_report(report: str, city_names: Sequence[str] = ()) -> str:
    report = report.strip()
    if not report:
        raise ReportFormatError("리포트 내용이 비어 있습니다.")
    missing = [heading for heading in REQUIRED_REPORT_HEADINGS if heading not in report]
    if missing:
        raise ReportFormatError(f"리포트 필수 섹션 누락: {', '.join(missing)}")
    expected_city_sections = max(1, len(city_names))
    incomplete = [
        heading
        for heading in PER_CITY_REPORT_HEADINGS
        if report.count(heading) < expected_city_sections
    ]
    if incomplete:
        raise ReportFormatError(
            "지역별 반복 섹션 부족: " + ", ".join(incomplete)
        )
    missing_cities = [city for city in city_names if city not in report]
    if missing_cities:
        raise ReportFormatError(
            f"리포트 추천 지역 누락: {', '.join(missing_cities)}"
        )
    return report + "\n"


def generate_final_report(
    client: Any, model: str, travel_data: dict[str, Any]
) -> str:
    city_names = [item["city"] for item in travel_data["recommendations"]]
    prompt = f"""다음 JSON만 근거로 한국어 국내 여행 리포트를 작성하세요.
모든 추천 지역을 입력 순서대로 나누어 작성하고, 제공되지 않은 맛집을 새로 만들지
마세요. restaurants가 비어 있으면 해당 지역의 맛집 섹션에 '데이터 없음'을 표시하세요.
날씨와 행사는 일반적 참고 정보임을 밝혀 주세요.

반드시 다음 Markdown 구조와 섹션명을 사용하세요. 대괄호 부분은 실제 값으로 바꾸고,
지역마다 #### 하위 섹션 5개를 모두 반복하세요.
# {travel_data['requested_date']} 국내 여행 추천 리포트
## 지역별 추천
### 1. [첫 번째 지역명]
#### 추천 이유
#### 날씨 요약
#### 행사/축제
#### 맛집 추천
#### 1일 일정 제안
### 2. [두 번째 지역명]
(같은 하위 섹션 반복, 세 번째 지역이 있으면 같은 방식으로 추가)
## 오류 요약(errors)

입력 JSON:
{json.dumps(travel_data, ensure_ascii=False, indent=2)}
"""
    response = client.responses.create(model=model, input=prompt)
    return validate_report(response.output_text, city_names)


def _restaurant_markdown(item: dict[str, Any]) -> str:
    name = item.get("name") or "이름 없음"
    url = item.get("url")
    label = f"[{name}]({url})" if url else name
    return f"- {label} - {item.get('address') or '주소 없음'}"


def build_fallback_report(travel_data: dict[str, Any]) -> str:
    recommendations = travel_data.get("recommendations") or []
    errors = travel_data.get("errors") or []
    city_sections: list[str] = []

    for index, recommendation in enumerate(recommendations, start=1):
        event_lines = "\n".join(
            f"- {event}" for event in recommendation.get("events", [])
        ) or "- 정보 없음"
        restaurant_lines = "\n".join(
            _restaurant_markdown(item)
            for item in recommendation.get("restaurants", [])
        ) or "- 데이터 없음 (장소 검색 결과 0건 또는 API 호출 실패)"
        city_sections.append(
            f"""### {index}. {recommendation.get('city', '추천 결과 없음')}

#### 추천 이유

{recommendation.get('reason', '추천 결과를 생성하지 못했습니다.')}

#### 날씨 요약

{recommendation.get('weather', '정보 없음')}
날씨 정보는 해당 시기의 일반적 참고 내용이며 실제 예보가 아닙니다.

#### 행사/축제

{event_lines}

#### 맛집 추천

{restaurant_lines}

#### 1일 일정 제안

- 오전: 해당 지역의 대표 명소 방문
- 오후: 주변 문화·자연 명소 탐방
- 저녁: 검색된 맛집 이용 또는 현지에서 추가 확인"""
        )

    if not city_sections:
        city_sections.append(
            """### 추천 결과 없음

#### 추천 이유

추천 결과를 생성하지 못했습니다.

#### 날씨 요약

정보 없음

#### 행사/축제

- 정보 없음

#### 맛집 추천

- 데이터 없음

#### 1일 일정 제안

- 추천 데이터를 생성한 뒤 다시 확인해 주세요."""
        )

    error_items: list[str] = []
    for item in errors:
        city_suffix = f" / `{item['city']}`" if item.get("city") else ""
        error_items.append(
            f"- `{item.get('step', 'unknown')}`{city_suffix}"
            f" / `{item.get('type', 'ERROR')}`: {item.get('message', '')}"
        )
    error_lines = "\n".join(error_items) or "- 기록된 오류 없음"

    city_body = "\n\n".join(city_sections)
    report = f"""# {travel_data['requested_date']} 국내 여행 추천 리포트

## 지역별 추천

{city_body}

## 오류 요약(errors)

{error_lines}
"""
    city_names = [item.get("city", "") for item in recommendations]
    return validate_report(report, city_names)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def validate_cached_travel_data(payload: Any, travel_date: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CacheFormatError("최상위 값이 JSON 객체가 아닙니다.")
    if payload.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise CacheFormatError("현재 캐시 스키마 버전과 다릅니다.")
    if payload.get("requested_date") != travel_date:
        raise CacheFormatError("요청 날짜와 캐시 날짜가 다릅니다.")

    recommendations = payload.get("recommendations")
    if not isinstance(recommendations, list) or not 2 <= len(recommendations) <= 3:
        raise CacheFormatError("추천 지역이 2~3개인 캐시가 아닙니다.")

    normalized_recommendations: list[dict[str, Any]] = []
    for item in recommendations:
        try:
            normalized = validate_city_recommendation(item)
        except ValueError as exc:
            raise CacheFormatError(str(exc)) from exc
        restaurants = item.get("restaurants")
        if not isinstance(restaurants, list) or any(
            not isinstance(place, dict) for place in restaurants
        ):
            raise CacheFormatError("restaurants는 JSON 객체 목록이어야 합니다.")
        normalized["restaurants"] = restaurants
        normalized_recommendations.append(normalized)

    city_names = [item["city"] for item in normalized_recommendations]
    if len(set(city_names)) != len(city_names):
        raise CacheFormatError("추천 지역이 중복되어 있습니다.")

    errors = payload.get("errors")
    if not isinstance(errors, list) or any(not isinstance(item, dict) for item in errors):
        raise CacheFormatError("errors는 JSON 객체 목록이어야 합니다.")

    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "requested_date": travel_date,
        "recommendations": normalized_recommendations,
        "errors": errors,
    }


def load_cached_travel_data(path: Path, travel_date: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return validate_cached_travel_data(payload, travel_date)
    except (OSError, json.JSONDecodeError, CacheFormatError) as exc:
        print(f"[캐시] 기존 JSON을 사용할 수 없어 새로 조회합니다: {exc}")
        return None


def reuse_cached_result(
    travel_data: dict[str, Any], json_path: Path, report_path: Path
) -> int:
    city_names = [item["city"] for item in travel_data["recommendations"]]
    report_reused = False
    if report_path.is_file():
        try:
            validate_report(report_path.read_text(encoding="utf-8"), city_names)
            report_reused = True
        except (OSError, ReportFormatError):
            report_reused = False

    if not report_reused:
        report_path.write_text(build_fallback_report(travel_data), encoding="utf-8")

    print("[캐시] 같은 날짜의 저장 결과를 찾았습니다. 외부 API 호출을 건너뜁니다.")
    print(f"- 원본 데이터: {json_path}")
    if report_reused:
        print(f"- 최종 리포트(기존 파일 재사용): {report_path}")
    else:
        print(f"- 최종 리포트(캐시 JSON으로 재생성): {report_path}")
    return 0


def run(
    travel_date: str,
    settings: Settings | None = None,
    *,
    refresh: bool = False,
) -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{travel_date}_travel_data.json"
    report_path = RESULTS_DIR / f"{travel_date}_travel_plan.md"

    if not refresh:
        cached_data = load_cached_travel_data(json_path, travel_date)
        if cached_data is not None:
            return reuse_cached_result(cached_data, json_path, report_path)

    if settings is None:
        settings = load_settings()

    errors: list[dict[str, str]] = []
    client = create_openai_client(settings.openai_api_key)

    print("[1/3] 1차 여행 추천 생성 중(OpenAI)...")
    try:
        recommendation = generate_recommendation(
            client, settings.openai_model, travel_date
        )
    except Exception as exc:
        message = safe_error_message(exc, (settings.openai_api_key,))
        errors.append(make_error("recommendation", "LLM_ERROR", message))
        failed_data = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "requested_date": travel_date,
            "recommendations": [],
            "errors": errors,
        }
        save_json(json_path, failed_data)
        report_path.write_text(build_fallback_report(failed_data), encoding="utf-8")
        print(f"오류: 1차 추천을 생성하지 못했습니다. {message}", file=sys.stderr)
        print(f"오류 기록: {json_path}", file=sys.stderr)
        print(f"오류 리포트: {report_path}", file=sys.stderr)
        return 1

    recommended_cities = recommendation["recommended_cities"]
    print(
        "  - 추천 지역: "
        + ", ".join(item["city"] for item in recommended_cities)
    )
    print("[2/3] Kakao Local API로 지역별 맛집 검색 중...")
    recommendations: list[dict[str, Any]] = []
    for index, item in enumerate(recommended_cities, start=1):
        city = item["city"]
        print(f"  - [{index}/{len(recommended_cities)}] {city} 검색 중...")
        try:
            restaurants = search_kakao_restaurants(
                city, settings.kakao_rest_api_key
            )
            if not restaurants:
                errors.append(
                    make_error(
                        "place_search",
                        "EMPTY_RESULT",
                        f"{city} 맛집 검색 결과가 0건입니다.",
                        city=city,
                    )
                )
                print("    검색 결과 0건: 데이터 없음으로 계속 진행합니다.")
            else:
                print(f"    맛집 {len(restaurants)}곳 검색 완료")
        except Exception as exc:
            message = safe_error_message(exc, (settings.kakao_rest_api_key,))
            errors.append(
                make_error(
                    "place_search", classify_place_error(exc), message, city=city
                )
            )
            restaurants = []
            print(f"    장소 검색 실패: {message}")
            print("    해당 지역의 맛집 데이터를 비우고 계속 진행합니다.")

        recommendations.append({**item, "restaurants": restaurants})

    travel_data: dict[str, Any] = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "requested_date": travel_date,
        "recommendations": recommendations,
        "errors": errors,
    }

    print("[3/3] 최종 여행 리포트 생성 중(OpenAI)...")
    try:
        report = generate_final_report(client, settings.openai_model, travel_data)
    except Exception as exc:
        message = safe_error_message(exc, (settings.openai_api_key,))
        errors.append(make_error("report_generation", "LLM_REPORT_ERROR", message))
        report = build_fallback_report(travel_data)
        print(f"  - LLM 리포트 생성 실패: {message}")
        print("  - 저장된 데이터로 기본 Markdown 리포트를 생성합니다.")

    save_json(json_path, travel_data)
    report_path.write_text(report, encoding="utf-8")

    print("완료!")
    print(f"- 원본 데이터: {json_path}")
    print(f"- 최종 리포트: {report_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args.travel_date, refresh=args.refresh)
    except ConfigurationError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
