"""tracer.py - 실행 단계(모델·도구 호출)를 모아 API 응답의 trace 로 돌려주고 JSONL 파일에도 남기는 콜백"""
import json
import time
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "logs" / "trace.jsonl"
MAX_LEN = 500  # trace 값 하나의 최대 글자 수


def _short(value) -> str:
    """trace 에 담을 값을 문자열로 바꾸고 길면 자른다."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_LEN else text[:MAX_LEN] + "…"


def _message_text(message) -> str:
    """메시지 content 가 블록 리스트여도 텍스트만 모은다."""
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content or ""


class TraceRecorder(BaseCallbackHandler):
    """질문 1건의 모델·도구 호출을 [{"step", "input", "output"}] 로 모은다."""

    def __init__(self, request_id: str, log_path: Path = LOG_PATH):
        self.request_id = request_id
        self.log_path = log_path
        self.steps: list[dict] = []
        self._pending: dict = {}  # run_id -> (단계 이름, 입력, 시작 시각)

    def add_step(self, step: str, input_value, output_value, latency_s: float | None = None):
        """단계 하나를 기록한다. 그래프 노드(가드레일 등)에서도 직접 부른다."""
        record = {"step": step, "input": _short(input_value), "output": _short(output_value)}
        self.steps.append(record)
        self._write(record | {"latency_s": latency_s})

    def _write(self, record: dict):
        """JSONL 파일에 한 줄 추가한다."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "request_id": self.request_id} | record
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    # ---- 모델 호출
    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        last = messages[0][-1] if messages and messages[0] else None
        self._pending[run_id] = ("llm", _message_text(last) if last else "", time.time())

    def on_llm_end(self, response, *, run_id, **kwargs):
        step, input_value, started = self._pending.pop(run_id, ("llm", "", time.time()))
        message = getattr(response.generations[0][0], "message", None)
        tool_calls = [c["name"] for c in getattr(message, "tool_calls", None) or []]
        output = _message_text(message) if message is not None else response.generations[0][0].text
        if tool_calls:
            output = f"[도구 호출: {', '.join(tool_calls)}] {output}".strip()
        self.add_step(step, input_value, output, round(time.time() - started, 2))

    # ---- 도구 호출
    def on_tool_start(self, serialized, input_str, *, run_id, inputs=None, **kwargs):
        name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        self._pending[run_id] = (name, inputs if inputs is not None else input_str, time.time())

    def on_tool_end(self, output, *, run_id, **kwargs):
        step, input_value, started = self._pending.pop(run_id, ("tool", "", time.time()))
        content = getattr(output, "content", output)
        self.add_step(step, input_value, content, round(time.time() - started, 2))

    def on_tool_error(self, error, *, run_id, **kwargs):
        step, input_value, started = self._pending.pop(run_id, ("tool", "", time.time()))
        self.add_step(step, input_value, f"오류: {error}", round(time.time() - started, 2))
