
```markdown
# ✈️ CLI 기반 국내 여행지 추천 프로그램

입력한 여행 날짜(`-date`)를 바탕으로 LLM(Google Gemini API)이 해당 시기에 적합한 국내 여행지를 추천하고, 카카오 로컬 API를 통해 해당 지역의 맛집 정보를 조회하여 종합 여행 리포트(Markdown)를 자동 생성하는 Python CLI 프로그램입니다.

---

## 1. 프로젝트 구조

```text
my_travel_app/
│-- .env                  # API 키 보관함 (보안)
│-- .gitignore            # Git 버전 관리 제외 설정 파일
│-- main.py               # 메인 실행 프로그램
│-- README.md             # 프로그램 설명서
└-- results/              # 실행 결과 자동 저장 폴더
    │-- result_YYYYMMDD_HHMMSS.json  # 원본 통합 데이터 (AI 추천 + 맛집 + 에러 로그)
    └-- report_YYYYMMDD_HHMMSS.md    # 최종 마크다운 여행 리포트

```

---

## 2. 개발 및 실행 환경

* **개발 언어:** Python 3.10+
* **의존성 패키지:** `google-genai`, `python-dotenv`, `requests`

### 패키지 설치 명령어

```bash
pip install google-genai python-dotenv requests

```

---

## 3. API 키 설정 방법 (.env)

프로젝트 최상위 폴더에 `.env` 파일을 생성하고 발급받은 API 키를 작성합니다.

```env
GEMINI_API_KEY=your_gemini_api_key_here
KAKAO_REST_API_KEY=your_kakao_rest_api_key_here

```

> ⚠️ **보안 주의사항**
> `.env` 파일에는 실제 API 키 정보가 포함되어 있으므로 절대로 Git Repository에 커밋하거나 타인에게 유출하지 마세요. (프로젝트 폴더 내 `.gitignore` 등록 필수)

---

## 4. 프로그램 실행 방법

터미널(CMD, PowerShell 등)에서 아래 명령어를 실행합니다 (`-date` 옵션 필수):

```bash
python main.py -date "2026-10-15"

```

* **입력값 검증:** `YYYY-MM-DD` 형식이 올바르지 않으면 오류 메시지를 출력하고 안전하게 종료됩니다.

---

## 5. 결과물 확인 방법

프로그램 실행이 완료되면 `results/` 폴더 내에 실행 일시 기준으로 2개 파일이 자동 생성됩니다:

1. `result_YYYYMMDD_HHMMSS.json`: 1차 AI 추천 결과, 카카오 맛집 검색 결과, 에러 로그(`errors`)를 포함한 원본 데이터
2. `report_YYYYMMDD_HHMMSS.md`: 마크다운 형식으로 작성된 최종 1일 여행 추천 리포트

---

## 6. 학습 포인트 및 핵심 개념 요약

1. **REST API 및 HTTP 메서드 (GET vs POST)**
* **GET:** 서버의 데이터를 조회할 때 사용하며 요청 데이터가 URL에 포함됩니다. (예: 카카오 로컬 검색 API로 맛집 목록 조회)
* **POST:** 서버에 데이터를 제출하거나 처리 요청을 보낼 때 사용하며 요청 본문(Body)에 데이터를 실어 보냅니다. (예: Gemini API에 프롬프트 전달 및 응답 생성)


2. **LLM 출력의 구조화(JSON) 필요성**
* AI 응답을 단순 텍스트가 아닌 JSON 규격으로 파싱하면 파이썬 프로그램에서 추천 도시(`recommended_city`) 키값만 정확히 추출할 수 있으며, 이 데이터를 다음 단계인 지도/장소 검색 API의 검색어로 유기적으로 전달할 수 있습니다.


3. **외부 API 에러 처리 원칙**
* 네트워크 연결 문제, 인증 실패(401/403), 쿼터 초과(429/503), 파싱 에러 등에 대비해 `try-except` 예외처리를 구가하였습니다. 지도 API 실패 시에도 맛집 섹션을 '데이터 없음' 처리하여 전체 리포트 생성이 정상 진행되도록 구현했습니다.


4. **환경변수(.env) 보안 관리**
* API 키를 소스코드 내부(`main.py`)에 하드코딩하지 않고 외부 환경변수(`.env`)로 분리하여 소스코드 유출 시 발생할 수 있는 보안 사고 및 무단 과금을 방지합니다.



```

```