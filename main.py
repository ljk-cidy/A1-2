"""국내 여행지 추천 프로그램 - GUI 버전 (tkinter, 추가 설치 불필요)

실행:
    python travel_recommender_gui.py

필요한 패키지:
    pip install google-genai python-dotenv requests
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

# ------------------------------------------------------------------ 설정
DEFAULT_MODEL = "gemini-3.8-flash"        # .env 의 GEMINI_MODEL 로 변경 가능
RETRYABLE_CODES = {429, 500, 503}         # 재시도할 가치가 있는 오류만
MAX_WAIT_SECONDS = 90                     # 이보다 오래 기다려야 하면 재시도 포기
BASE_DIR = Path(__file__).resolve().parent
RESULT_DIR = BASE_DIR / "results"

FONT = ("맑은 고딕", 10)
FONT_BOLD = ("맑은 고딕", 10, "bold")


# ------------------------------------------------------------------ 데이터 구조
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


@dataclass
class Result:
    travel_date: str
    rec: Recommendation
    restaurants: list[dict]
    errors: list[str]
    report_md: str
    md_path: Path
    json_path: Path


class AppError(Exception):
    """사용자에게 그대로 보여줄 수 있는 오류."""


# ------------------------------------------------------------------ Gemini
def retry_delay_hint(error: Exception) -> float | None:
    match = re.search(r"(?:retryDelay['\"]?:\s*['\"]|retry in )(\d+(?:\.\d+)?)s", str(error))
    return float(match.group(1)) if match else None


def call_gemini(client, prompt, config, errors, notify, max_attempts=3):
    model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
    for attempt in range(1, max_attempts + 1):
        try:
            return client.models.generate_content(model=model, contents=prompt, config=config)
        except genai_errors.APIError as e:
            code = getattr(e, "code", None)
            errors.append(f"Gemini 오류 (code={code}, 시도 {attempt}/{max_attempts}): {e}")
            if code not in RETRYABLE_CODES or attempt == max_attempts:
                raise
            hint = retry_delay_hint(e)
            if hint is not None and hint > MAX_WAIT_SECONDS:
                raise
            wait = int(hint) + 2 if hint is not None else 3 * (2 ** attempt)
            for remaining in range(wait, 0, -1):
                notify(f"서버가 바쁩니다. {remaining}초 후 다시 시도합니다...")
                time.sleep(1)


def get_recommendation(client, travel_date, errors, notify) -> Recommendation:
    prompt = f"""
당신은 한국 여행 전문가입니다. {travel_date} 무렵에 여행하기 좋은 국내 도시 1곳을 추천하고,
하루 일정(오전/점심/오후/저녁)도 함께 제안해 주세요.

주의:
- 날씨는 해당 시기의 '일반적인 기후'를 기준으로 설명하세요 (실시간 예보가 아닙니다).
- 축제/행사는 실제로 열리는 것이 확실한 경우에만 적고, 불확실하면 빈 목록으로 두세요.
"""
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=Recommendation,
        temperature=0.7,
    )
    response = call_gemini(client, prompt, config, errors, notify)
    if isinstance(response.parsed, Recommendation):
        return response.parsed
    return Recommendation.model_validate_json(response.text)


# ------------------------------------------------------------------ 카카오
def get_restaurants(city: str, errors: list[str]) -> list[dict]:
    key = (os.getenv("KAKAO_REST_API_KEY") or "").strip().strip("\"'")
    if not key:
        errors.append("KAKAO_REST_API_KEY 미설정 → 맛집 검색 생략")
        return []
    try:
        res = requests.get(
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            headers={"Authorization": f"KakaoAK {key}"},
            params={"query": f"{city} 맛집", "category_group_code": "FD6", "size": 5},
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


# ------------------------------------------------------------------ 리포트
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


def save_results(travel_date, rec, restaurants, errors, report_md) -> tuple[Path, Path]:
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
    md_path.write_text(report_md, encoding="utf-8")
    return md_path, json_path


# ------------------------------------------------------------------ 전체 처리 (작업 스레드에서 실행)
def run_pipeline(travel_date: str, notify) -> Result:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise AppError("GEMINI_API_KEY 가 .env 파일에 없습니다.\n.env 파일 위치와 변수 이름을 확인해 주세요.")

    errors: list[str] = []
    client = genai.Client(api_key=api_key)

    notify("[1/3] AI 가 여행지를 고르는 중...")
    rec = get_recommendation(client, travel_date, errors, notify)

    notify(f"[2/3] {rec.recommended_city} 맛집을 검색하는 중...")
    restaurants = get_restaurants(rec.recommended_city, errors)

    notify("[3/3] 리포트를 저장하는 중...")
    report_md = render_report(rec, restaurants, travel_date)
    md_path, json_path = save_results(travel_date, rec, restaurants, errors, report_md)
    return Result(travel_date, rec, restaurants, errors, report_md, md_path, json_path)


def explain_api_error(e: genai_errors.APIError) -> str:
    code = getattr(e, "code", None)
    if code in (400, 401, 403):
        return f"API 키가 올바르지 않거나 권한이 없습니다. (오류 코드 {code})\n.env 의 GEMINI_API_KEY 를 확인해 주세요."
    if code == 404:
        return "모델명이 올바르지 않습니다. (오류 코드 404)\n.env 의 GEMINI_MODEL 을 확인해 주세요."
    if code == 429:
        return "사용 한도를 초과했습니다. (오류 코드 429)\n잠시 후(또는 내일) 다시 시도해 주세요."
    return f"AI 서버가 혼잡하거나 오류가 발생했습니다. (오류 코드 {code})\n잠시 후 다시 시도해 주세요."


# ------------------------------------------------------------------ GUI
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("✈️ 국내 여행지 추천")
        self.geometry("880x720")
        self.minsize(780, 620)

        self.msg_queue: queue.Queue = queue.Queue()
        self.running = False
        self.result: Result | None = None
        self.restaurants: list[dict] = []

        self._setup_style()
        self._build_ui()
        self._check_env()
        self.bind("<Return>", lambda _e: self.start())
        self.after(100, self._poll_queue)

    # ---------------- 화면 구성
    def _setup_style(self):
        style = ttk.Style(self)
        style.configure(".", font=FONT)
        style.configure("Treeview", rowheight=30, font=FONT)
        style.configure("Treeview.Heading", font=FONT_BOLD)
        style.configure("Accent.TButton", font=FONT_BOLD, padding=(16, 6))
        style.configure("Title.TLabel", font=("맑은 고딕", 18, "bold"))
        style.configure("Sub.TLabel", foreground="#666666")

    def _build_ui(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)

        # 제목
        ttk.Label(root, text="✈️ 국내 여행지 추천", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            root,
            text="날짜를 고르면 AI 가 여행지와 일정을, 카카오가 맛집을 찾아 리포트로 정리해 드립니다.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 12))

        # 날짜 입력
        box = ttk.LabelFrame(root, text=" 여행 날짜 ", padding=12)
        box.pack(fill="x")

        today = date.today()
        self.year_var = tk.StringVar(value=str(today.year))
        self.month_var = tk.StringVar(value=str(today.month))
        self.day_var = tk.StringVar(value=str(today.day))

        row = ttk.Frame(box)
        row.pack(fill="x")
        years = [str(y) for y in range(today.year, today.year + 3)]
        for var, values, width, unit in (
            (self.year_var, years, 6, "년"),
            (self.month_var, [str(m) for m in range(1, 13)], 4, "월"),
            (self.day_var, [str(d) for d in range(1, 32)], 4, "일"),
        ):
            ttk.Combobox(row, textvariable=var, values=values, width=width, state="readonly").pack(side="left")
            ttk.Label(row, text=unit).pack(side="left", padx=(2, 10))

        ttk.Button(row, text="오늘", command=self._set_today).pack(side="left")
        self.search_btn = ttk.Button(row, text="🔍 추천받기", style="Accent.TButton", command=self.start)
        self.search_btn.pack(side="right")

        # 진행 상태
        status = ttk.Frame(root)
        status.pack(fill="x", pady=(12, 6))
        self.progress = ttk.Progressbar(status, mode="indeterminate")
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="날짜를 선택하고 [추천받기]를 눌러 주세요.")
        ttk.Label(status, textvariable=self.status_var, style="Sub.TLabel").pack(anchor="w", pady=(4, 0))

        # 결과 탭
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, pady=(6, 8))

        # 탭 1: 요약
        tab_summary = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab_summary, text="  📋 요약  ")
        self.summary_text = tk.Text(
            tab_summary, wrap="word", font=FONT, relief="flat", padx=12, pady=8, state="disabled"
        )
        self.summary_text.pack(fill="both", expand=True)
        self.summary_text.tag_configure("title", font=("맑은 고딕", 20, "bold"))
        self.summary_text.tag_configure("sub", foreground="#666666", spacing3=8)
        self.summary_text.tag_configure("heading", font=("맑은 고딕", 11, "bold"), foreground="#1a5fb4", spacing1=10)
        self.summary_text.tag_configure("body", spacing3=4, lmargin1=8, lmargin2=8)
        self.summary_text.tag_configure("warn", foreground="#b45309", spacing1=12)
        self._set_text(self.summary_text, "아직 결과가 없습니다.", "sub")

        # 탭 2: 맛집
        tab_food = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab_food, text="  🍽 맛집  ")
        columns = ("name", "category", "address")
        self.tree = ttk.Treeview(tab_food, columns=columns, show="headings", selectmode="browse")
        for col, title, width in (("name", "가게 이름", 200), ("category", "종류", 120), ("address", "주소", 380)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self._open_selected_restaurant())
        food_bar = ttk.Frame(tab_food)
        food_bar.pack(fill="x", pady=(6, 0))
        ttk.Label(food_bar, text="더블클릭하면 카카오맵 페이지가 열립니다.", style="Sub.TLabel").pack(side="left")
        ttk.Button(food_bar, text="카카오맵에서 보기", command=self._open_selected_restaurant).pack(side="right")

        # 탭 3: 리포트
        tab_report = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab_report, text="  📝 리포트  ")
        self.report_text = ScrolledText(
            tab_report, wrap="word", font=("Consolas", 10), relief="flat", padx=10, pady=8, state="disabled"
        )
        self.report_text.pack(fill="both", expand=True)

        # 하단 버튼
        footer = ttk.Frame(root)
        footer.pack(fill="x")
        self.folder_btn = ttk.Button(footer, text="📂 결과 폴더 열기", command=self._open_folder, state="disabled")
        self.folder_btn.pack(side="left")
        self.copy_btn = ttk.Button(footer, text="📋 리포트 복사", command=self._copy_report, state="disabled")
        self.copy_btn.pack(side="left", padx=6)
        ttk.Button(footer, text="종료", command=self.destroy).pack(side="right")

    # ---------------- 보조 기능
    @staticmethod
    def _set_text(widget: tk.Text, text: str, tag: str | None = None):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text, tag) if tag else widget.insert("end", text)
        widget.config(state="disabled")

    def _check_env(self):
        missing = [k for k in ("GEMINI_API_KEY", "KAKAO_REST_API_KEY") if not os.getenv(k)]
        if "GEMINI_API_KEY" in missing:
            self.status_var.set("⚠ .env 에 GEMINI_API_KEY 가 없습니다. 추천을 받으려면 먼저 설정해 주세요.")
        elif missing:
            self.status_var.set("ℹ KAKAO_REST_API_KEY 가 없어 맛집 검색은 생략됩니다.")

    def _set_today(self):
        today = date.today()
        self.year_var.set(str(today.year))
        self.month_var.set(str(today.month))
        self.day_var.set(str(today.day))

    # ---------------- 실행 흐름
    def start(self):
        if self.running:
            return
        try:
            chosen = date(int(self.year_var.get()), int(self.month_var.get()), int(self.day_var.get()))
        except ValueError:
            messagebox.showwarning("날짜 확인", "존재하지 않는 날짜입니다.\n년/월/일을 다시 확인해 주세요.")
            return
        if chosen < date.today() and not messagebox.askyesno(
            "지난 날짜", "이미 지난 날짜입니다.\n그래도 계속할까요?"
        ):
            return

        self.running = True
        self.search_btn.config(state="disabled")
        self.folder_btn.config(state="disabled")
        self.copy_btn.config(state="disabled")
        self.progress.start(12)
        self.status_var.set("시작하는 중...")
        threading.Thread(target=self._worker, args=(chosen.isoformat(),), daemon=True).start()

    def _worker(self, travel_date: str):
        """네트워크 작업은 별도 스레드에서 실행해 창이 멈추지 않게 한다."""
        put = self.msg_queue.put
        try:
            result = run_pipeline(travel_date, lambda text: put(("status", text)))
            put(("done", result))
        except AppError as e:
            put(("error", str(e)))
        except genai_errors.APIError as e:
            put(("error", explain_api_error(e)))
        except ValueError as e:  # AI 응답의 JSON/스키마 오류
            put(("error", f"AI 응답 형식이 올바르지 않습니다. 다시 시도해 주세요.\n\n({e})"))
        except Exception as e:  # noqa: BLE001
            put(("error", f"예상치 못한 오류가 발생했습니다.\n\n{type(e).__name__}: {e}"))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "error":
                    self._on_error(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _finish_run(self):
        self.running = False
        self.progress.stop()
        self.search_btn.config(state="normal")

    def _on_error(self, message: str):
        self._finish_run()
        self.status_var.set("✗ 실패했습니다.")
        messagebox.showerror("추천 실패", message)

    def _on_done(self, result: Result):
        self._finish_run()
        self.result = result
        self.restaurants = result.restaurants
        self._fill_summary(result)
        self._fill_restaurants(result.restaurants)
        self._set_text(self.report_text, result.report_md)
        self.folder_btn.config(state="normal")
        self.copy_btn.config(state="normal")
        self.notebook.select(0)

        if result.errors:
            self.status_var.set(f"⚠ 완료 (일부 문제 {len(result.errors)}건) - 저장: {result.md_path.name}")
        else:
            self.status_var.set(f"✓ 완료 - 저장: {result.md_path.name}")

    # ---------------- 결과 표시
    def _fill_summary(self, r: Result):
        rec = r.rec
        t = self.summary_text
        t.config(state="normal")
        t.delete("1.0", "end")
        t.insert("end", f"📍 {rec.recommended_city}\n", "title")
        t.insert("end", f"{r.travel_date} 여행\n", "sub")

        events = "\n".join(f"• {e}" for e in rec.events) or "확인된 행사 정보 없음"
        schedule = (
            f"오전   {rec.morning}\n점심   {rec.lunch}\n"
            f"오후   {rec.afternoon}\n저녁   {rec.evening}"
        )
        for heading, body in (
            ("🌤 날씨 (일반적인 기후 기준)", rec.weather),
            ("🎉 행사 / 축제", events),
            ("💡 추천 이유", rec.reason),
            ("🗓 하루 일정", schedule),
        ):
            t.insert("end", heading + "\n", "heading")
            t.insert("end", body + "\n", "body")

        if not r.restaurants:
            t.insert("end", "\n⚠ 맛집 정보를 가져오지 못했습니다. (자세한 내용은 결과 JSON 의 errors 참고)\n", "warn")
        t.insert("end", "\n※ AI 가 생성한 정보이므로 방문 전에 꼭 확인하세요.", "sub")
        t.config(state="disabled")

    def _fill_restaurants(self, restaurants: list[dict]):
        self.tree.delete(*self.tree.get_children())
        if not restaurants:
            self.tree.insert("", "end", iid="none", values=("데이터 없음", "", ""))
            return
        for i, r in enumerate(restaurants):
            self.tree.insert("", "end", iid=str(i), values=(r["name"], r["category"], r["address"]))

    # ---------------- 버튼 동작
    def _open_selected_restaurant(self):
        selected = self.tree.selection()
        if not selected or not selected[0].isdigit():
            messagebox.showinfo("맛집", "목록에서 가게를 먼저 선택해 주세요.")
            return
        url = self.restaurants[int(selected[0])].get("url")
        if url:
            webbrowser.open(url)

    def _open_folder(self):
        folder = RESULT_DIR
        folder.mkdir(exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder)], check=False)
        else:
            subprocess.run(["xdg-open", str(folder)], check=False)

    def _copy_report(self):
        if self.result:
            self.clipboard_clear()
            self.clipboard_append(self.result.report_md)
            self.status_var.set("📋 리포트를 클립보드에 복사했습니다.")


def main():
    load_dotenv()                         # 현재 폴더 기준
    load_dotenv(BASE_DIR / ".env")        # 스크립트 폴더 기준 (더블클릭 실행 대비)
    try:  # Windows 고해상도 화면에서 글자가 흐려지지 않게
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    App().mainloop()


if __name__ == "__main__":
    main()