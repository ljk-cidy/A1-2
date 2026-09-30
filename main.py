"""국내 여행지 추천 프로그램 (Gemini + 카카오 로컬 API)

사용법:
    python travel_recommender.py --date 2026-10-15
    python travel_recommender.py            # 날짜를 물어봅니다
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

# 모델명은 .env 의 GEMINI_MODEL 로 바꿀 수 있습니다.
# (사용 가능한 모델명은 client.models.list() 로 확인하세요)
DEFAULT_MODEL = "gemini-3.8-flash"
RETRYABLE_CODES = {429, 500, 503}  # 재시도할 가치가 있는 오류만
MAX_WAIT_SECONDS = 90              # 이보다 오래 기다려야 하면 재시도 포기
RESULT_DIR = Path("results")


# ---------------------------------------------------------------- 데이터 구조
class Recommendation(BaseModel):
    """Gemini 가 반드시 이 구조로 답하도록 강제하는 스키마."""

    recommended_city: str = Field(description="도시 이름 (예: 제주, 강릉)")
    weather: str = Field(description="해당 시기의 일반적인 날씨 요약 1~2문장")
    events: list[str] = Field(description="확실한 축제/행사만. 없으면 빈 배열")
    reason: str = Field(description="추천 이유 2~4문장")
    morning: str = Field(description="오전 일정 추천")
    lunch: str = Field(description="점심 추천")
    afternoon: str = Field(description="오후 일정 추천")
    evening: str = Field(description="저녁 일정 추천")


# ---------------------------------------------------------------- 공통 유틸
def validate_date(text: str) -> str:
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"날짜 형식 오류: '{text}' → YYYY-MM-DD 형식으로 입력하세요. (예: 2026-10-15)"
        )
    return text


def countdown(seconds: int, message: str) -> None:
    """대기 중에도 화면이 멈춘 것처럼 보이지 않도록 남은 시간을 표시."""
    for remaining in range(seconds, 0, -1):
        print(f"\r   ⏳ {message} ({remaining}초 남음)   ", end="", flush=True)
        time.sleep(1)
    print("\r" + " " * 70 + "\r", end="", flush=True)


def retry_delay_hint(error: Exception) -> float | None:
    """오류 메시지에 들어 있는 서버 권장 대기시간(초)을 추출."""
    match = re.search(r"(?:retryDelay['\"]?:\s*['\"]|retry in )(\d+(?:\.\d+)?)s", str(error))
    return float(match.group(1)) if match else None


# ---------------------------------------------------------------- Gemini
def call_gemini(client, prompt: str, config, errors: list[str], max_attempts: int = 3):
    """재시도 로직을 한 곳에 모은 Gemini 호출 함수 (재귀 대신 반복문 사용)."""
    model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL)

    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(model=model, contents=prompt, config=config)
        except genai_errors.APIError as e:
            code = getattr(e, "code", None)
            errors.append(f"Gemini 오류 (code={code}, 시도 {attempt}/{max_attempts}): {e}")

            # 키 오류(400/401/403), 모델명 오류(404) 등은 기다려도 소용없음
            if code not in RETRYABLE_CODES or attempt == max_attempts:
                raise

            hint = retry_delay_hint(e)
            if hint is not None and hint > MAX_WAIT_SECONDS:
                raise  # 일일 할당량 소진 등: 오래 기다려야 하면 바로 안내

            wait = int(hint) + 2 if hint is not None else 3 * (2 ** attempt)  # 6, 12초 ...
            countdown(wait, "서버가 바쁩니다. 잠시 후 다시 시도합니다")


def get_recommendation(client, travel_date: str, errors: list[str]) -> Recommendation:
    prompt = f"""
당신은 한국 여행 전문가입니다. {travel_date} 무렵에 여행하기 좋은 국내 도시 1곳을 추천하고,
하루 일정(오전/점심/오후/저녁)도 함께 제안해 주세요.

주의:
- 날씨는 해당 시기의 '일반적인 기후'를 기준으로 설명하세요 (실시간 예보가 아닙니다).
- 축제/행사는 실제로 열리는 것이 확실한 경우에만 적고, 불확실하면 빈 목록으로 두세요.
"""
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=Recommendation,  # JSON 구조를 SDK 가 보장
        temperature=0.7,
    )
    response = call_gemini(client, prompt, config, errors)

    if isinstance(response.parsed, Recommendation):
        return response.parsed
    # parsed 가 비어 있으면 텍스트를 직접 검증 (파싱 실패는 '재시도 대기' 대상이 아님)
    return Recommendation.model_validate_json(response.text)


# ---------------------------------------------------------------- 카카오
def get_restaurants(city: str, errors: list[str]) -> list[dict]:
    key = (os.getenv("KAKAO_REST_API_KEY") or "").strip().strip("\"'")
    if not key:
        errors.append("KAKAO_REST_API_KEY 미설정 → 맛집 검색 생략")
        return []

    try:
        res = requests.get(
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            headers={"Authorization": f"KakaoAK {key}"},
            params={
                "query": f"{city} 맛집",
                "category_group_code": "FD6",  # 음식점만
                "size": 5,
            },
            timeout=5,
        )
        res.raise_for_status()
    except requests.HTTPError:
        hint = " (REST API 키가 맞는지 확인하세요)" if res.status_code == 401 else ""
        errors.append(f"카카오 API 호출 실패: HTTP {res.status_code}{hint}")
        return []
    except requests.RequestException as e:
        errors.append(f"카카오 API 통신 오류: {e}")
        return []

    return [
        {
            "name": d.get("place_name", ""),
            "address": d.get("road_address_name") or d.get("address_name", ""),
            "category": d.get("category_name", "").split(" > ")[-1],
            "url": d.get("place_url", ""),
            "longitude": d.get("x", ""),
            "latitude": d.get("y", ""),
        }
        for d in res.json().get("documents", [])
    ]


# ---------------------------------------------------------------- 리포트 (AI 호출 없이 로컬 생성)
def render_report(rec: Recommendation, restaurants: list[dict], travel_date: str) -> str:
    events = "\n".join(f"- {e}" for e in rec.events) or "- 확인된 행사 정보 없음"
    if restaurants:
        food = "\n".join(
            f"{i}. [{r['name']}]({r['url']}) - {r['category']}  \n   📍 {r['address']}"
            for i, r in enumerate(restaurants, 1)
        )
    else:
        food = "- 데이터 없음"

    return f"""# {rec.recommended_city} 여행 제안 리포트

- 여행 시기: {travel_date}

## 추천 이유
{rec.reason}

## 날씨 (일반적인 기후 기준)
{rec.weather}

## 행사 / 축제
{events}

## 맛집 (카카오 로컬 검색)
{food}

## 하루 일정 추천
| 시간대 | 추천 |
|---|---|
| 오전 | {rec.morning} |
| 점심 | {rec.lunch} |
| 오후 | {rec.afternoon} |
| 저녁 | {rec.evening} |

> ※ AI 가 생성한 정보이므로 행사 일정·영업 여부는 방문 전에 꼭 확인하세요.
"""


# ---------------------------------------------------------------- 실행
def ask_date_interactively() -> str:
    while True:
        raw = input("여행 날짜를 입력하세요 (YYYY-MM-DD, 엔터=오늘): ").strip()
        try:
            return validate_date(raw or date.today().isoformat())
        except argparse.ArgumentTypeError as e:
            print(f"  ✗ {e}")


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="국내 여행지 추천 프로그램")
    parser.add_argument("-d", "--date", type=validate_date, help="여행 날짜 (YYYY-MM-DD)")
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("✗ GEMINI_API_KEY 가 .env 파일에 없습니다. 파일을 확인해 주세요.")
        return 1

    travel_date = args.date or ask_date_interactively()
    client = genai.Client(api_key=api_key)
    errors: list[str] = []

    print(f"\n🧭 {travel_date} 여행지를 찾는 중입니다\n")

    # 1) AI 추천
    print("[1/3] AI 가 여행지를 고르는 중...")
    try:
        rec = get_recommendation(client, travel_date, errors)
    except genai_errors.APIError as e:
        code = getattr(e, "code", "?")
        print(f"\n✗ AI 추천에 실패했습니다 (오류 코드 {code}).")
        if code in (400, 401, 403):
            print("  → API 키가 올바른지 확인해 주세요.")
        elif code == 404:
            print("  → 모델명이 올바른지 확인해 주세요. (.env 의 GEMINI_MODEL)")
        elif code == 429:
            print("  → 사용 한도를 초과했습니다. 잠시 후(또는 내일) 다시 시도해 주세요.")
        else:
            print("  → 서버가 혼잡합니다. 잠시 후 다시 시도해 주세요.")
        return 1
    except ValueError as e:  # JSON/스키마 검증 실패
        print(f"\n✗ AI 응답 형식이 올바르지 않습니다. 다시 실행해 주세요.\n  ({e})")
        return 1
    print(f"      ✓ 추천 도시: {rec.recommended_city}")

    # 2) 맛집
    print(f"[2/3] {rec.recommended_city} 맛집을 검색하는 중...")
    restaurants = get_restaurants(rec.recommended_city, errors)
    print(f"      ✓ {len(restaurants)}곳" if restaurants else "      - 맛집 정보를 가져오지 못했습니다")

    # 3) 저장
    print("[3/3] 리포트를 저장하는 중...")
    RESULT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = RESULT_DIR / f"result_{stamp}.json"
    md_path = RESULT_DIR / f"report_{stamp}.md"

    json_path.write_text(
        json.dumps(
            {
                "input_date": travel_date,
                "recommendation": rec.model_dump(),
                "restaurants": restaurants,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path.write_text(render_report(rec, restaurants, travel_date), encoding="utf-8")

    # 결과 요약을 터미널에 바로 보여주기
    line = "─" * 44
    print(f"\n{line}\n 📍 {rec.recommended_city}  ({travel_date})\n{line}")
    print(f" 🌤  {rec.weather}")
    if rec.events:
        print(f" 🎉 {', '.join(rec.events)}")
    print(f" 💡 {rec.reason}")
    if restaurants:
        print("\n 🍽  맛집")
        for r in restaurants:
            print(f"    • {r['name']}  ({r['address']})")
    print(f"{line}")
    print(f" 📄 리포트 : {md_path}")
    print(f" 🗂  원본   : {json_path}")
    if errors:
        print(f" ⚠  일부 문제가 있었습니다 ({len(errors)}건) → 원본 JSON 의 errors 참고")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())