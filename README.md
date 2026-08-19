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
KAKAO_REST_API_KEY=your_kakao_rest_api_key
```

- OpenAI 모델을 사용할 수 없다는 응답이 오면 계정에서 사용 가능한 Structured
  Outputs 지원 모델로 `OPENAI_MODEL`을 변경합니다.
- Kakao 키는 JavaScript 키나 Admin 키가 아닌 앱의 **REST API 키**를 사용합니다.
- `.env`는 `.gitignore`에 포함되어 있으므로 Git에 커밋하지 않습니다.
- 키를 코드, README, 실행 로그, JSON, Markdown에 복사하지 마세요.

공식 문서:

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Kakao Local API](https://developers.kakao.com/docs/ko/local/dev-guide)

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
