# GenAI Python 2 - 국내 여행 추천 프로그램

OpenAI API와 Kakao Local API를 순서대로 연결해, 입력한 날짜에 맞는 국내
여행지 2~3곳과 각 지역의 맛집을 추천하고 결과를 JSON 및 Markdown 파일로
저장하는 CLI 프로그램입니다. 같은 날짜의 결과는 캐시로 재사용해 불필요한
외부 API 호출을 줄입니다.

## 처리 흐름

1. `--date`로 여행 날짜를 입력받고 `YYYY-MM-DD` 형식을 검사합니다.
2. 같은 날짜의 유효한 JSON 캐시가 있으면 외부 API 호출 없이 기존 리포트를
   재사용하거나 JSON으로 리포트를 복원합니다.
3. 캐시가 없으면 OpenAI API가 서로 다른 추천 지역 2~3곳과 각 지역의 날씨
   요약, 행사 후보, 추천 이유를 구조화된 JSON으로 생성합니다.
4. 반복문으로 각 추천 지역을 Kakao Local API의 맛집 검색어로 전달합니다.
5. 지역별 추천 JSON과 맛집 목록을 OpenAI API에 전달해 지역별 Markdown
   리포트를 만듭니다.
6. 원본 데이터와 리포트를 `results/` 폴더에 저장합니다.

## 요구 환경

- Python 3.10 이상
- OpenAI API 키
- Kakao Developers 애플리케이션의 REST API 키

## 설치

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Windows PowerShell에서는 가상환경을 다음과 같이 활성화합니다.

```powershell
.venv\Scripts\Activate.ps1
```

## API 키 설정

예제 파일을 복사합니다.

```bash
cp .env.example .env
```

생성한 `.env`에 실제 값을 입력합니다.

```dotenv
OPENAI_API_KEY=your_openai_api_key
OPENAI_MODEL=gpt-5.6-luna
PLACE_SEARCH_PROVIDER=kakao
KAKAO_REST_API_KEY=your_kakao_rest_api_key
```

- OpenAI 모델을 사용할 수 없다는 응답이 오면 계정에서 사용 가능한 Structured
  Outputs 지원 모델로 `OPENAI_MODEL`을 변경합니다.
- Kakao 키는 JavaScript 키나 Admin 키가 아닌 앱의 **REST API 키**를 사용합니다.
- `PLACE_SEARCH_PROVIDER`는 등록된 지도 검색 플러그인 이름이며 기본값은
  `kakao`입니다.
- `.env`는 `.gitignore`에 포함되어 있으므로 Git에 커밋하지 않습니다.
- 키를 코드, README, 실행 로그, JSON, Markdown에 복사하지 마세요.

공식 문서:

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI Responses API - Create a response](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Kakao Local API](https://developers.kakao.com/docs/ko/local/dev-guide)

## 외부 API의 GET/POST 사용 설계

프로그램의 외부 통신은 다음 세 호출로 구성됩니다. `OpenAI` 호출은 SDK를
사용하므로 코드에는 `client.responses.create()`로 보이지만, 실제 HTTP 요청은
Responses API의 `POST /v1/responses`입니다.

| 호출 단계 | HTTP 메서드와 예시 엔드포인트 | 선택 이유 |
| --- | --- | --- |
| 1차 여행지 추천 | `POST https://api.openai.com/v1/responses` | 프롬프트와 JSON Schema를 요청 본문으로 보내고, 모델이 매번 새로운 응답 리소스를 생성하므로 `POST`가 적합합니다. 긴 프롬프트와 인증 정보를 URL 쿼리에 노출하지 않습니다. |
| 지역별 맛집 검색 | `GET https://dapi.kakao.com/v2/local/search/keyword.json?query=강릉+맛집&category_group_code=FD6&size=5` | 서버 리소스를 만들거나 변경하지 않는 읽기 전용 검색입니다. 같은 조건의 조회는 반복해도 서버 상태를 바꾸지 않으므로 `GET`을 사용하고 검색 조건은 쿼리 파라미터로 전달합니다. |
| 최종 리포트 생성 | `POST https://api.openai.com/v1/responses` | 지역별 JSON을 본문으로 전달해 새로운 Markdown 응답을 생성하는 작업이므로 `POST`를 사용합니다. |

즉, 기존 장소를 조회하는 Kakao 검색에는 `GET`, 프롬프트를 처리해 새 모델
응답을 생성하는 OpenAI 호출에는 `POST`를 사용합니다. 현재 프로그램에는 장소나
여행 리소스를 외부 서버에 생성·수정하는 별도의 `POST`, `PUT`, `DELETE` 호출은
없습니다.

## 지도 검색 플러그인 설계

### 공통 인터페이스와 입출력 스펙

`run()`은 Kakao 함수를 직접 알지 못하고 `RestaurantSearchProvider` 프로토콜의
다음 메서드만 호출합니다.

```python
class RestaurantSearchProvider(Protocol):
    provider_name: str

    def search_restaurants(
        self, city: str, *, size: int = 5
    ) -> list[dict[str, Any]]:
        ...
```

입출력 계약은 다음과 같습니다.

| 구분 | 스펙 |
| --- | --- |
| 입력 `city` | `normalize_city_name()`을 통과한 비어 있지 않은 지역 검색어 |
| 입력 `size` | 반환할 최대 장소 수, 기본값 5. 어댑터는 해당 API의 허용 범위를 검사할 수 있음 |
| 정상 출력 | 공통 장소 객체의 목록. 결과가 없으면 빈 목록 `[]` |
| 공통 장소 필드 | `name: str`, `address: str`, `category: str \| None`, `url: str \| None`, `x: float \| None`, `y: float \| None` |
| 실패 출력 | 인증·쿼터·네트워크·응답 파싱 실패는 예외로 전달하며, 상위 반복문이 해당 도시의 오류를 기록하고 다음 도시를 계속 처리 |

현재 `KakaoRestaurantSearchProvider`가 Kakao 전용 응답을 위 공통 장소 구조로
바꾸는 어댑터입니다. 플러그인 선택은 `PLACE_SEARCH_PROVIDER` 값으로 하며,
`create_place_search_provider()`가 레지스트리에서 팩토리를 찾아 인스턴스를
생성합니다.

### 새 지도 API 등록 방법

다른 지도 API로 교체할 때는 공통 인터페이스를 구현하고 팩토리를 한 번 등록합니다.

```python
class AnotherMapProvider:
    provider_name = "another-map"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search_restaurants(self, city: str, *, size: int = 5):
        documents = call_another_map_api(city=city, size=size)
        return [
            {
                "name": item["title"],
                "address": item.get("road_address", ""),
                "category": item.get("category"),
                "url": item.get("url"),
                "x": item.get("longitude"),
                "y": item.get("latitude"),
            }
            for item in documents
        ]


register_place_search_provider(
    "another-map",
    lambda settings: AnotherMapProvider(os.environ["ANOTHER_MAP_API_KEY"]),
)
```

등록 코드를 애플리케이션 시작 시 한 번 import한 뒤
`PLACE_SEARCH_PROVIDER=another-map`으로 설정하면 됩니다. 교체 시 변경 지점은
새 어댑터 구현, 제공자별 키 로딩, 레지스트리 등록 세 곳이며, 지역 반복 처리,
캐시, 리포트 생성과 `run()`의 검색 호출 코드는 변경하지 않습니다.

## 도시명 정규화 파이프라인

LLM이 `부산광역시 해운대구`, `제주특별자치도 제주시`처럼 서로 다른 수준의
행정구역을 반환해도 검색어가 일관되도록 `normalize_city_name()`에서 다음
파이프라인을 적용합니다.

1. `NFKC` 유니코드 정규화로 전각 문자 등 표현 차이를 통일합니다.
2. 앞뒤·중복 공백을 제거하고 `/`, `,`, 괄호 등의 구분자를 공백으로 바꿉니다.
3. 광역 행정구역은 `STANDARD_CITY_NAMES` 표준 명칭으로 매핑합니다.
4. 각 토큰에서 `특별시`, `광역시`, `특별자치도`, `시`, `군`, `구`, `읍`,
   `면`, `동` 등의 행정 접미사를 제거해 핵심 명사를 추출합니다. 단, 제거 후
   한 글자만 남는 `중구`, `우동` 같은 짧은 고유명은 원문을 유지합니다.
5. `제주 제주`처럼 중복된 토큰을 입력 순서대로 제거합니다.
6. 지나치게 긴 검색어를 막기 위해 광역명부터 세부 지역까지 핵심 토큰을 최대
   3개만 유지합니다.

주요 표준 명칭 매핑은 다음과 같습니다.

| 입력 명칭 | 표준 명칭 | 입력 명칭 | 표준 명칭 |
| --- | --- | --- | --- |
| `서울특별시` | `서울` | `부산광역시` | `부산` |
| `세종특별자치시` | `세종` | `강원특별자치도` | `강원` |
| `전북특별자치도` | `전북` | `제주특별자치도` | `제주` |
| `경상북도` | `경북` | `경상남도` | `경남` |
| `충청북도` | `충북` | `충청남도` | `충남` |

정규화 예시는 다음과 같습니다.

| 원본 지명 | 검색용 표준 지명 |
| --- | --- |
| `부산광역시 / 해운대구 (우동)` | `부산 해운대 우동` |
| `강원특별자치도 강릉시` | `강원 강릉` |
| `제주특별자치도 제주시 애월읍 한림면` | `제주 애월 한림` |

추천 JSON 검증 단계에서도 정규화 후 도시명을 사용하므로 `제주특별자치도`와
`제주도`처럼 표현만 다른 중복 추천을 탐지할 수 있습니다. 각 지도 어댑터도
검색 직전에 같은 함수를 적용해 직접 호출 시의 비정규화 입력을 방어합니다.

## 실행

```bash
python travel_planner.py --date "2026-09-15"
```

과제 문서의 단일 하이픈 표기도 사용할 수 있습니다.

```bash
python travel_planner.py -date "2026-09-15"
```

날짜 형식이 잘못되었거나 존재하지 않는 날짜이면 사용법을 출력하고 종료합니다.

### 캐시와 새로고침

같은 날짜로 다시 실행하면 `results/{날짜}_travel_data.json`을 검사합니다. 현재
스키마와 일치하는 JSON이 있으면 OpenAI와 Kakao API를 호출하지 않습니다. 기존
Markdown 리포트가 유효하면 그대로 사용하고, 없거나 형식이 잘못되었으면 캐시
JSON만으로 기본 리포트를 다시 만듭니다. 따라서 캐시 적중 시에는 API 키도
필요하지 않습니다.

최신 추천과 장소 정보로 강제 갱신하려면 `--refresh`를 사용합니다.

```bash
python travel_planner.py --date "2026-09-15" --refresh
```

이전에 생성된 단일 지역 형식의 JSON이나 손상된 JSON은 캐시로 사용하지 않고
새 API 요청을 수행합니다.

## 결과 확인

정상 실행 후 다음 파일이 생성됩니다.

```text
results/
├── 2026-09-15_travel_data.json
└── 2026-09-15_travel_plan.md
```

JSON에는 다음 값이 포함됩니다.

- `schema_version`: 캐시 호환성 확인용 스키마 버전
- `requested_date`: 사용자가 입력한 여행 날짜
- `recommendations`: 지역별 추천과 해당 지역의 `restaurants` 목록
- `errors`: 실행 중 발생한 오류 요약 목록(장소 검색 오류에는 해당 `city` 포함)

구조 예시는 다음과 같습니다.

```json
{
  "schema_version": 2,
  "requested_date": "2026-09-15",
  "recommendations": [
    {
      "city": "강릉",
      "weather": "해당 시기의 일반적인 날씨 참고 정보",
      "events": ["확인해 볼 지역 행사 후보"],
      "reason": "추천 이유",
      "restaurants": [
        {
          "name": "검색된 식당명",
          "address": "주소",
          "category": "음식점 > 한식",
          "url": "https://place.map.kakao.com/...",
          "x": 128.0,
          "y": 37.0
        }
      ]
    }
  ],
  "errors": []
}
```

## 오류 처리 정책

- API 키가 없으면 설정 방법을 안내하고 즉시 종료합니다.
- 첫 번째 OpenAI 응답을 파싱하지 못하면 프롬프트를 보강해 한 번만 재시도합니다.
- 한 지역의 Kakao 인증, 네트워크, 쿼터 오류가 발생해도 해당 맛집 목록만 비운 뒤
  나머지 지역 검색과 리포트 생성을 계속합니다.
- 지역별 맛집 검색 결과가 0건이면 `EMPTY_RESULT`와 해당 도시를 기록하고
  `데이터 없음`으로 표시합니다.
- 최종 OpenAI 리포트 생성이 실패하면 저장된 데이터로 기본 Markdown 리포트를 생성합니다.
- 캐시 JSON은 스키마 버전, 날짜, 추천 지역 수와 필드 구조를 검사한 뒤 사용합니다.
- 오류 메시지는 최대 길이를 제한하고 API 키 값을 제거한 뒤 저장합니다.

## 테스트

외부 API를 호출하지 않는 단위 테스트입니다.

```bash
python -m unittest discover -s tests -v
```

실제 API 통합 테스트는 `.env` 설정 후 CLI 명령으로 확인합니다. API 사용 비용이
발생할 수 있으므로 자동 테스트에서는 호출하지 않습니다.
