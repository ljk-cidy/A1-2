import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main


class TravelProgramTests(unittest.TestCase):
    def test_cli_rejects_invalid_date_before_loading_configuration(self):
        with patch.object(main, "load_dotenv") as load, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as failure:
                main.main(["--date", "2026-02-29"])
        self.assertEqual(failure.exception.code, 2)
        load.assert_not_called()

    def test_cli_saves_both_files_when_kakao_returns_invalid_json(self):
        recommendation = main.Recommendation(
            recommended_city="경주", weather="맑음", events=[], reason="문화유산",
            morning="산책", lunch="식사", afternoon="전시", evening="야경",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(main, "RESULT_DIR", Path(directory)),
            patch.object(main, "load_dotenv"),
            patch.dict(main.os.environ, {"GEMINI_API_KEY": "test", "KAKAO_REST_API_KEY": "test"}),
            patch.object(main.genai, "Client"),
            patch.object(main, "get_recommendation", return_value=recommendation),
            patch.object(main.requests, "get") as get,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            get.return_value.json.side_effect = ValueError("invalid JSON")
            self.assertEqual(main.main(["-date", "2026-10-15"]), 0)
            json_path, = Path(directory).glob("result_*.json")
            report_path, = Path(directory).glob("report_*.md")
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["input_date"], "2026-10-15")
            self.assertEqual(saved["restaurants"], [])
            self.assertEqual(saved["errors"], ["카카오 API 응답 형식 오류"])
            self.assertIn("경주 여행 제안 리포트", report_path.read_text(encoding="utf-8"))
