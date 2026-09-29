import argparse
import json
import os
import time
from datetime import datetime
from dotenv import load_dotenv
import requests
from google import genai
from google.genai import types

# 1. .env 파일에서 API 키 읽어오기
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY")

if not GEMINI_API_KEY:
    print("[오류] GEMINI_API_KEY가 .env 파일에 없습니다. 확인해 주세요.")
    exit(1)

# Gemini 클라이언트 초기화
client = genai.Client(api_key=GEMINI_API_KEY)

# 최신 권장 모델명으로 수정
MODEL_NAME = "gemini-3.8-flash"


# 2. 날짜 형식 검증 함수
def validate_date(date_string):
    try:
        datetime.strptime(date_string, "%Y-%m-%d")
        return date_string
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"날짜 형식 오류: '{date_string}'. YYYY-MM-DD 형식으로 입력하세요."
        )


# 3. LLM에 여행지 추천 요청 (503/트래픽 과부하 대응 재시도 로직)
def get_llm_recommendation(date_str, errors_list, retry_count=0):
    prompt = f"""
    당신은 한국 여행 전문가입니다. {date_str} 시기에 여행하기 좋은 국내 도시 1곳을 추천해 주세요.
    반드시 아래 스키마 구조의 JSON 형식으로만 응답해 주세요.

    JSON 스키마:
    {{
        "recommended_city": "도시 이름 (예: 제주, 강릉)",
        "weather": "해당 시기의 날씨 요약 (1~2문장)",
        "events": ["축제나 행사 1", "축제나 행사 2"],
        "reason": "추천 이유 (2~4문장)"
    }}
    """

    if retry_count > 0:
        sleep_time = retry_count * 3
        print(f"[안내] 구글 서버 응답 대기 중 ({sleep_time}초 대기)...")
        time.sleep(sleep_time)

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.7,
            ),
        )

        content = response.text.strip()
        return json.loads(content)

    except Exception as e:
        err_msg = f"Gemini 추천 실패 (시도 {retry_count + 1}회): {str(e)}"
        print(f"[경고] {err_msg}")
        errors_list.append(err_msg)

        if retry_count < 2:
            return get_llm_recommendation(date_str, errors_list, retry_count + 1)

        return {
            "recommended_city": "서울",
            "weather": "날씨 정보 없음",
            "events": [],
            "reason": "응답 분석 실패로 기본값을 사용합니다.",
        }


# 4. 카카오 로컬 API로 맛집 검색
def get_restaurants(city_name, errors_list):
    if not KAKAO_REST_API_KEY:
        err_msg = "카카오 API 키가 설정되지 않아 맛집 검색을 건너뜁니다."
        print(f"[경고] {err_msg}")
        errors_list.append(err_msg)
        return []

    clean_kakao_key = KAKAO_REST_API_KEY.strip().replace('"', '').replace("'", "")

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {
        "Authorization": f"KakaoAK {clean_kakao_key}"
    }
    params = {
        "query": f"{city_name} 맛집",
        "size": 5
    }

    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200:
            documents = res.json().get("documents", [])
            restaurants = []
            for doc in documents:
                restaurants.append(
                    {
                        "name": doc.get("place_name", ""),
                        "address": doc.get("road_address_name") or doc.get("address_name", ""),
                        "category": doc.get("category_name", ""),
                        "url": doc.get("place_url", ""),
                        "x": doc.get("x", ""),
                        "y": doc.get("y", "")
                    }
                )
            return restaurants
        else:
            err_msg = f"카카오 API 호출 실패 (상태코드: {res.status_code})"
            print(f"[경고] {err_msg}")
            errors_list.append(err_msg)
            return []
    except Exception as e:
        err_msg = f"카카오 API 통신 에러: {str(e)}"
        print(f"[경고] {err_msg}")
        errors_list.append(err_msg)
        return []


# 5. 최종 여행 보고서(Markdown) 생성
def generate_final_report(rec_data, restaurants, errors_list, retry_count=0):
    prompt = f"""
    아래 데이터를 바탕으로 깔끔한 여행 보고서를 마크다운으로 작성해 주세요.

    [데이터]
    - 추천 도시: {rec_data.get('recommended_city')}
    - 날씨: {rec_data.get('weather')}
    - 축제: {', '.join(rec_data.get('events', []))}
    - 추천 이유: {rec_data.get('reason')}
    - 맛집 정보: {json.dumps(restaurants, ensure_ascii=False)}

    [작성 요구사항]
    1. 제목: [추천 도시] 여행 제안 리포트
    2. 추천 이유 요약 및 날씨
    3. 행사/축제 목록
    4. 맛집 정보 (없으면 '데이터 없음' 표시)
    5. 하루 일정 추천 (오전/점심/오후/저녁)
    """

    if retry_count > 0:
        sleep_time = retry_count * 3
        print(f"[안내] 리포트 생성 서버 대기 중 ({sleep_time}초 대기)...")
        time.sleep(sleep_time)

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.7,
            ),
        )
        return response.text
    except Exception as e:
        err_msg = f"리포트 생성 에러 (시도 {retry_count + 1}회): {str(e)}"
        print(f"[경고] {err_msg}")
        errors_list.append(err_msg)

        if retry_count < 2:
            return generate_final_report(rec_data, restaurants, errors_list, retry_count + 1)

        return f"# 리포트 생성 실패\n\n내용: {str(e)}"


# 6. 메인 실행 함수
def main():
    parser = argparse.ArgumentParser(
        description="국내 여행지 추천 프로그램"
    )
    parser.add_argument(
        "-date",
        type=validate_date,
        required=True,
        help="날짜 입력 (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    travel_date = args.date
    errors_list = []

    print(f"\n[{travel_date}] 여행 추천을 시작합니다...")

    # Step 1: Gemini 추천
    print("1. AI(Gemini)에게 추천 도시 물어보는 중...")
    rec_data = get_llm_recommendation(travel_date, errors_list)
    city = rec_data.get("recommended_city", "서울")

    # Step 2: 맛집 검색
    print(f"2. [{city}] 맛집 정보 검색 중 (카카오 API)...")
    restaurants = get_restaurants(city, errors_list)

    # Step 3: 리포트 생성
    print("3. 최종 보고서 작성 중...")
    report_md = generate_final_report(rec_data, restaurants, errors_list)

    # 파일 저장
    os.makedirs("results", exist_ok=True)
    now = datetime.now().strftime("%Y%m%d_%H%M%S")

    # JSON 원본 저장
    raw_data = {
        "input_date": travel_date,
        "recommendation": rec_data,
        "restaurants": restaurants,
        "errors": errors_list,
    }
    with open(f"results/result_{now}.json", "w", encoding="utf-8") as f:
        json.dump(raw_data, f, ensure_ascii=False, indent=2)

    # 마크다운 보고서 저장
    with open(f"results/report_{now}.md", "w", encoding="utf-8") as f:
        f.write(report_md)

    print("\n" + "=" * 40)
    print("성공적으로 완수되었습니다!")
    print(f"- 원본 데이터: results/result_{now}.json")
    print(f"- 여행 리포트: results/report_{now}.md")
    print("=" * 40)


if __name__ == "__main__":
    main()