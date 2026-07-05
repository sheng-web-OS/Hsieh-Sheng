#!/usr/bin/env python3
"""Minimal LINE Bot mobile capture server for Clinical Learning OS.

This server is a front-stage capture layer only. It writes structured
observations to mobile_inbox and never touches Card Registry approval,
approved_import_ready, AnkiConnect, or evidence validation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_MOBILE_INBOX = ROOT / "mobile_inbox"
LINE_REPLY_ENDPOINT = "https://api.line.me/v2/bot/message/reply"
TZ = ZoneInfo("Asia/Taipei")

MORNING_ACTIONS = {"accept_primary_focus", "backup_only", "override", "skip"}
DAYTIME_TYPES = {
    "attending_question",
    "gpt_followup",
    "patient_deidentified_learning_point",
    "evidence_check",
    "memory_seed",
    "free_text_note",
}
PROMPT_TO_DAYTIME_TYPE = {
    "attending": "attending_question",
    "gpt": "gpt_followup",
    "evidence": "evidence_check",
    "memory": "memory_seed",
    "patient": "patient_deidentified_learning_point",
    "note": "free_text_note",
}
EVENING_RESULTS = {"pass", "partial", "fail", "not_done"}
CRITICAL_MISS = {"none", "present", "unsure"}
ERROR_PATTERNS = {
    "vague_answer",
    "wrong_branch",
    "anchoring",
    "underconfidence_despite_pass",
    "high_confidence_failure",
    "missed_red_flag",
    "retrieval_failure",
    "other",
}
BACKLOG_GUILT = {"no", "slight", "yes"}
ANKI_INTENTS = {"approve_intent", "rewrite_later", "reject", "defer", "needs_verification"}
YES_NO = {"yes", "no"}

PHI_PATTERNS = [
    ("mrn_like", re.compile(r"\b(?:MRN|mrn|病歷|病歷號|chart)\s*[:#]?\s*[A-Za-z0-9-]{5,}\b")),
    ("long_numeric_identifier", re.compile(r"\b\d{6,12}\b")),
    ("bed_number", re.compile(r"\b(?:bed|BED|床|病床)\s*[:#]?\s*[A-Za-z]?\d{1,4}[A-Za-z]?\b")),
    ("birthday_like", re.compile(r"\b(?:birthday|birth|DOB|生日|出生)\s*[:：]?\s*\d{2,4}[-/年]\d{1,2}[-/月]\d{1,2}")),
    ("patient_full_date", re.compile(r"\b(?:patient|pt|病人|個案).{0,12}\d{4}[-/年]\d{1,2}[-/月]\d{1,2}")),
    ("chinese_name_like", re.compile(r"(?:姓名|名字|name)\s*[:：]\s*[\u4e00-\u9fffA-Za-z]{2,}")),
]


def now() -> datetime:
    return datetime.now(TZ)


def today_slug() -> str:
    return now().strftime("%Y_%m_%d")


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def inbox_dir() -> Path:
    path = Path(os.environ.get("MOBILE_INBOX_DIR", str(DEFAULT_MOBILE_INBOX)))
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def stable_user_hash(event: dict[str, Any]) -> str:
    source = event.get("source", {}) or {}
    seed = "|".join(str(source.get(key, "")) for key in ["type", "userId", "groupId", "roomId"])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16] if seed.strip("|") else "unknown"


def detect_phi(text: str) -> list[str]:
    return [name for name, pattern in PHI_PATTERNS if pattern.search(text or "")]


def scrub_text(text: str) -> str:
    value = str(text or "")
    for _, pattern in PHI_PATTERNS:
        value = pattern.sub("[REDACTED_PHI_LIKE]", value)
    return value.strip()


def base_record(event: dict[str, Any], raw_text: str, mode: str) -> dict[str, Any]:
    phi_flags = detect_phi(raw_text)
    return {
        "captured_at": now_iso(),
        "capture_source": "line",
        "mode": mode,
        "user_hash": stable_user_hash(event),
        "raw_text": scrub_text(raw_text),
        "phi_like_flags": phi_flags,
        "deidentification_warning": bool(phi_flags),
        "safety_note": (
            "PHI-like content was redacted. Keep LINE entries de-identified."
            if phi_flags
            else "LINE capture is observation/intent only; Codex gates still apply."
        ),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def path_for(kind: str) -> Path:
    slug = today_slug()
    names = {
        "morning": f"{slug}.morning_decision.json",
        "daytime": f"{slug}.daytime_capture.jsonl",
        "evening": f"{slug}.evening_calibration.json",
        "anki": f"{slug}.anki_intent.jsonl",
        "drift": f"{slug}.topic_drift.jsonl",
    }
    return inbox_dir() / names[kind]


def pending_prompt_path() -> Path:
    return inbox_dir() / "_pending_line_prompts.json"


def read_pending_prompts() -> dict[str, str]:
    path = pending_prompt_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_pending_prompts(payload: dict[str, str]) -> None:
    write_json(pending_prompt_path(), payload)


def set_pending_prompt(event: dict[str, Any], template: str) -> None:
    pending = read_pending_prompts()
    pending[stable_user_hash(event)] = template
    write_pending_prompts(pending)


def pop_pending_prompt(event: dict[str, Any]) -> str:
    pending = read_pending_prompts()
    user_hash = stable_user_hash(event)
    template = pending.pop(user_hash, "")
    if template:
        write_pending_prompts(pending)
    return template


def latest_morning_cockpit_json() -> Path | None:
    candidates = sorted((ROOT / "outputs" / "morning_cockpits").glob("*.morning_cockpit.json"))
    return candidates[-1] if candidates else None


def build_today_brief() -> str:
    path = latest_morning_cockpit_json()
    if not path:
        return "目前找不到 Morning Cockpit。你可以先用問主治、問 GPT、查證、記憶種子捕捉今天遇到的問題。"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "Morning Cockpit 檔案讀取失敗。先用 LINE 捕捉今天的問題，晚點再整理。"
    surface = payload.get("command_surface") or {}
    primary = surface.get("primary_focus") or {}
    decisions = surface.get("human_decisions") or []
    prompts = surface.get("micro_assessment_prompts") or []
    topic = primary.get("topic") or "今天尚未產生主軸"
    minimum = primary.get("minimum_success") or "not found"
    attending = "not found"
    verification = "not found"
    if len(decisions) > 1:
        attending = decisions[1].get("why") or decisions[1].get("default") or attending
    if len(decisions) > 2:
        verification = decisions[2].get("why") or decisions[2].get("default") or verification
    micro_lines = []
    for index, item in enumerate(prompts[:2], start=1):
        prompt = item.get("prompt", "")
        if prompt:
            micro_lines.append(f"{index}. {prompt}")
    micro_text = "\n".join(micro_lines) if micro_lines else "無"
    return (
        "今日任務\n"
        f"主軸：{topic}\n"
        f"完成：{minimum}\n\n"
        "今天只做：\n"
        f"1. 口頭練習：{micro_text}\n"
        f"2. 可問主治：{attending}\n"
        f"3. 需要查證：{verification}\n\n"
        "回覆：接受 / 備用 / 跳過"
    )


def parse_kv(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in re.findall(r"(\w+)=([^;\n]+?)(?=\s+\w+=|$)", text):
        result[key.strip().lower()] = value.strip()
    return result


def capture_morning(event: dict[str, Any], action: str, reason: str = "", raw_text: str = "") -> str:
    if action not in MORNING_ACTIONS:
        return f"未辨識 morning action：{action}"
    record = base_record(event, raw_text or action, "morning_decision")
    record.update(
        {
            "action": action,
            "override_reason": scrub_text(reason) if action == "override" else "",
            "approval_effect": "none",
        }
    )
    write_json(path_for("morning"), record)
    labels = {
        "accept_primary_focus": "已記錄：今天先照 Morning 建議的主軸走。",
        "backup_only": "已記錄：今天只把 Morning 主軸當備用方向。",
        "override": "已記錄：今天不照原本主軸，改用你剛剛寫的方向。",
        "skip": "已記錄：今天先跳過 Morning 主軸。",
    }
    return labels[action]


def capture_daytime(event: dict[str, Any], capture_type: str, text: str, raw_text: str) -> str:
    if capture_type not in DAYTIME_TYPES:
        return f"未辨識 daytime capture type：{capture_type}"
    record = base_record(event, raw_text, "daytime_capture")
    record.update(
        {
            "capture_type": capture_type,
            "content": scrub_text(text) or "not_provided",
            "patient_related": capture_type == "patient_deidentified_learning_point",
            "route_hint": {
                "attending_question": "attending_or_local_followup",
                "gpt_followup": "gpt_prompt_portal",
                "patient_deidentified_learning_point": "patient_context_unless_generalized",
                "evidence_check": "evidence_gate",
                "memory_seed": "memory_worthiness_gate",
                "free_text_note": "field_run_observation",
            }[capture_type],
        }
    )
    append_jsonl(path_for("daytime"), record)
    warning = "；已偵測並遮蔽 PHI-like 片段" if record["deidentification_warning"] else ""
    labels = {
        "attending_question": "已記錄：這是想問主治的問題",
        "gpt_followup": "已記錄：這是之後想問 GPT 的問題",
        "patient_deidentified_learning_point": "已記錄：這是去識別化的病人學習點",
        "evidence_check": "已記錄：這是需要查證或確認本院做法的問題",
        "memory_seed": "已記錄：這可能值得之後整理成記憶點",
        "free_text_note": "已記錄：這是一則一般筆記",
    }
    return f"{labels[capture_type]}{warning}"


def capture_evening(
    event: dict[str, Any],
    result: str,
    confidence: str,
    critical_miss: str,
    error_pattern: str,
    backlog_guilt: str,
    raw_text: str,
) -> str:
    if result not in EVENING_RESULTS:
        return f"未辨識 result：{result}"
    if confidence not in {"1", "2", "3", "4", "5"}:
        return "confidence 必須是 1-5"
    if critical_miss not in CRITICAL_MISS:
        return f"未辨識 critical_miss：{critical_miss}"
    if error_pattern not in ERROR_PATTERNS:
        return f"未辨識 error_pattern：{error_pattern}"
    if backlog_guilt not in BACKLOG_GUILT:
        return f"未辨識 backlog_guilt：{backlog_guilt}"
    record = base_record(event, raw_text, "evening_calibration")
    record.update(
        {
            "result": result,
            "confidence": int(confidence),
            "critical_miss": critical_miss,
            "error_pattern": error_pattern,
            "backlog_guilt": backlog_guilt,
            "creates_backlog_debt": False,
        }
    )
    write_json(path_for("evening"), record)
    result_labels = {
        "pass": "通過",
        "partial": "部分完成",
        "fail": "沒有完成",
        "not_done": "今天未做",
    }
    return f"已記錄晚間回顧：{result_labels[result]}，信心分數 {confidence}/5"


def capture_anki_intent(event: dict[str, Any], intent: str, os_id: str, note: str, raw_text: str) -> str:
    if intent not in ANKI_INTENTS:
        return f"未辨識 Anki intent：{intent}"
    record = base_record(event, raw_text, "anki_review_intent")
    record.update(
        {
            "intent": intent,
            "os_id": scrub_text(os_id) or "not_provided",
            "note": scrub_text(note) or "not_provided",
            "intent_only": True,
            "registry_status_effect": "none",
            "anki_import_effect": "none",
        }
    )
    append_jsonl(path_for("anki"), record)
    intent_labels = {
        "approve_intent": "想批准",
        "rewrite_later": "之後重寫",
        "reject": "想退回",
        "defer": "先延後",
        "needs_verification": "需要先查證",
    }
    return f"已記錄 Anki 想法：{intent_labels[intent]}。這只是意向紀錄，不會直接改卡片或匯入 Anki。"


def capture_topic_drift(event: dict[str, Any], values: dict[str, str], raw_text: str) -> str:
    topic = values.get("topic") or values.get("topic_name") or "not_provided"
    record = base_record(event, raw_text, "topic_drift_flag")
    record.update(
        {
            "topic_name": scrub_text(topic),
            "aliases_seen": scrub_text(values.get("aliases") or values.get("aliases_seen") or "not_provided"),
            "where_seen": scrub_text(values.get("where") or values.get("where_seen") or "not_provided"),
            "suspected_duplicate": values.get("duplicate", values.get("suspected_duplicate", "not_provided")),
            "suspected_wrong_route": values.get("wrong_route", values.get("suspected_wrong_route", "not_provided")),
            "needs_canonical_topic_id_later": values.get("canonical", values.get("needs_canonical_topic_id_later", "not_provided")),
        }
    )
    append_jsonl(path_for("drift"), record)
    return f"已記錄：這個主題可能有重複、跑錯地方，或需要統一命名：{record['topic_name']}"


def parse_text_command(event: dict[str, Any], text: str) -> str:
    raw = text
    value = text.strip()
    lower = value.lower()
    if lower in {"menu", "help", "開始", "選單"}:
        return "menu"
    if lower in {"today", "今日任務", "今天任務", "今天要做什麼", "任務"}:
        return build_today_brief()
    if value in {"接受", "接受主軸", "今天先照主軸走"}:
        return capture_morning(event, "accept_primary_focus", "", raw)
    if value in {"備用", "當備用", "backup"}:
        return capture_morning(event, "backup_only", "", raw)
    if value in {"跳過", "skip"}:
        return capture_morning(event, "skip", "", raw)
    if not lower.startswith("/"):
        pending_template = pop_pending_prompt(event)
        capture_type = PROMPT_TO_DAYTIME_TYPE.get(pending_template, "")
        if capture_type:
            return capture_daytime(event, capture_type, value, raw)
    if lower.startswith("/morning"):
        parts = value.split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else ""
        aliases = {"accept": "accept_primary_focus", "backup": "backup_only"}
        action = aliases.get(action, action)
        reason = parts[2] if len(parts) > 2 else ""
        return capture_morning(event, action, reason, raw)
    daytime_aliases = {
        "/attending": "attending_question",
        "/gpt": "gpt_followup",
        "/patient": "patient_deidentified_learning_point",
        "/evidence": "evidence_check",
        "/memory": "memory_seed",
        "/note": "free_text_note",
    }
    for prefix, capture_type in daytime_aliases.items():
        if lower.startswith(prefix):
            return capture_daytime(event, capture_type, value[len(prefix):].strip(), raw)
    if lower.startswith("/evening"):
        parts = value.split()
        if len(parts) < 6:
            return "格式：/evening pass 4 none vague_answer no"
        return capture_evening(event, parts[1].lower(), parts[2], parts[3].lower(), parts[4].lower(), parts[5].lower(), raw)
    if lower.startswith("/anki"):
        parts = value.split(maxsplit=3)
        if len(parts) < 2:
            return "格式：/anki rewrite_later OS_ID optional_note"
        return capture_anki_intent(
            event,
            parts[1].lower(),
            parts[2] if len(parts) > 2 else "",
            parts[3] if len(parts) > 3 else "",
            raw,
        )
    if lower.startswith("/drift"):
        return capture_topic_drift(event, parse_kv(value), raw)
    return capture_daytime(event, "free_text_note", value, raw)


def parse_postback(event: dict[str, Any]) -> str:
    data = ((event.get("postback") or {}).get("data") or "").strip()
    values = {key: vals[-1] for key, vals in urllib.parse.parse_qs(data).items() if vals}
    mode = values.get("mode", "")
    if mode == "today":
        return build_today_brief()
    if mode == "prompt":
        template = values.get("template", "")
        prompts = {
            "attending": "請直接輸入你想問主治的臨床問題。不要含姓名、病歷號、床號。",
            "gpt": "請直接輸入你晚點想丟給 GPT 追問的問題。",
            "evidence": "請直接輸入需要查 guideline、來源或本院做法的點。",
            "memory": "請直接輸入可能值得變成長期記憶的 seed。",
            "patient": "請直接輸入去識別化病人學習點。不要含姓名、病歷號、床號、生日。",
            "evening": "快速 Evening：可回覆 /evening pass 4 none other no，或 /evening partial 3 unsure wrong_branch slight",
        }
        if template in PROMPT_TO_DAYTIME_TYPE:
            set_pending_prompt(event, template)
        return prompts.get(template, "請回覆 menu 查看可用指令。")
    if mode == "morning":
        return capture_morning(event, values.get("action", ""), values.get("override_reason", ""), data)
    if mode == "evening":
        return capture_evening(
            event,
            values.get("result", "not_done"),
            values.get("confidence", "3"),
            values.get("critical_miss", "none"),
            values.get("error_pattern", "other"),
            values.get("backlog_guilt", "no"),
            data,
        )
    if mode == "anki":
        return capture_anki_intent(event, values.get("intent", ""), values.get("os_id", ""), values.get("note", ""), data)
    if mode == "drift":
        return capture_topic_drift(event, values, data)
    return "收到 postback，但 mode 不明。"


def quick_reply_item(label: str, data: str) -> dict[str, Any]:
    return {
        "type": "action",
        "action": {"type": "postback", "label": label[:20], "data": data, "displayText": label[:300]},
    }


def menu_message() -> dict[str, Any]:
    items = [
        quick_reply_item("Accept Morning", "mode=morning&action=accept_primary_focus"),
        quick_reply_item("Backup only", "mode=morning&action=backup_only"),
        quick_reply_item("Skip Morning", "mode=morning&action=skip"),
        quick_reply_item("Evening pass 4", "mode=evening&result=pass&confidence=4&critical_miss=none&error_pattern=other&backlog_guilt=no"),
        quick_reply_item("Evening partial 3", "mode=evening&result=partial&confidence=3&critical_miss=unsure&error_pattern=other&backlog_guilt=slight"),
        quick_reply_item("Rewrite later", "mode=anki&intent=rewrite_later"),
        quick_reply_item("Needs verify", "mode=anki&intent=needs_verification"),
        quick_reply_item("Topic drift", "mode=drift&topic=not_provided&duplicate=no&wrong_route=no&canonical=yes"),
    ]
    return {
        "type": "text",
        "text": (
            "Clinical OS mobile capture：\n"
            "可用 /attending /gpt /patient /evidence /memory /note /evening /anki /drift。\n"
            "請勿輸入姓名、病歷號、床號、生日或可識別日期。"
        ),
        "quickReply": {"items": items},
    }


def text_message(text: str, include_menu: bool = True) -> dict[str, Any]:
    msg = {"type": "text", "text": text[:4900]}
    if include_menu:
        msg["quickReply"] = menu_message()["quickReply"]
    return msg


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")


def reply_to_line(reply_token: str, messages: list[dict[str, Any]], token: str) -> None:
    if not reply_token or not token:
        return
    payload = json.dumps({"replyToken": reply_token, "messages": messages}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        LINE_REPLY_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        response.read()


class LineCaptureHandler(BaseHTTPRequestHandler):
    server_version = "ClinicalOSLineCapture/0.1"
    allow_unsigned_dev = False

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self.send_json({"ok": True, "service": "clinical_os_line_mobile_capture", "time": now_iso()})
            return
        if self.path.startswith("/export"):
            self.handle_export()
            return
        self.send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)

    def handle_export(self) -> None:
        export_token = os.environ.get("MOBILE_EXPORT_TOKEN", "")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        supplied_token = (query.get("token") or [""])[0]
        if not export_token:
            self.send_json({"ok": False, "error": "MOBILE_EXPORT_TOKEN is not set"}, HTTPStatus.FORBIDDEN)
            return
        if not hmac.compare_digest(export_token, supplied_token):
            self.send_json({"ok": False, "error": "invalid export token"}, HTTPStatus.FORBIDDEN)
            return
        buffer = BytesIO()
        base = inbox_dir()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(base.glob("*")):
                if path.is_file() and path.name != ".gitkeep":
                    archive.write(path, arcname=path.name)
        raw = buffer.getvalue()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="mobile_inbox_export.zip"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        try:
            if self.path.rstrip("/") != "/webhook":
                self.send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length)
            secret = os.environ.get("LINE_CHANNEL_SECRET", "")
            signature = self.headers.get("x-line-signature", "")
            if not self.allow_unsigned_dev:
                if not secret:
                    self.send_json({"ok": False, "error": "LINE_CHANNEL_SECRET is not set"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                if not verify_signature(secret, body, signature):
                    self.send_json({"ok": False, "error": "invalid LINE signature"}, HTTPStatus.FORBIDDEN)
                    return
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_json({"ok": False, "error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
                return
            token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
            processed = []
            for event in payload.get("events", []) or []:
                try:
                    response = self.handle_event(event)
                except Exception as exc:  # Keep LINE webhook healthy; record the capture error.
                    response = f"capture_failed: {type(exc).__name__}: {exc}"
                processed.append(response)
                reply_token = event.get("replyToken", "")
                if reply_token:
                    try:
                        messages = [menu_message()] if response == "menu" else [text_message(response)]
                        reply_to_line(reply_token, messages, token)
                    except Exception as exc:
                        processed.append(f"reply_failed: {type(exc).__name__}: {exc}")
            self.send_json({"ok": True, "processed": processed})
        except Exception as exc:
            self.send_json({"ok": True, "warning": f"webhook_error_suppressed: {type(exc).__name__}: {exc}"})

    def handle_event(self, event: dict[str, Any]) -> str:
        event_type = event.get("type")
        if event_type == "message" and (event.get("message") or {}).get("type") == "text":
            return parse_text_command(event, (event.get("message") or {}).get("text", ""))
        if event_type == "postback":
            return parse_postback(event)
        return "收到非文字事件；MVP 只處理文字與 postback。"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    parser.add_argument("--allow-unsigned-dev", action="store_true", help="Local testing only. Do not use for public webhook.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    LineCaptureHandler.allow_unsigned_dev = args.allow_unsigned_dev
    if not args.allow_unsigned_dev:
        secret_len = len(os.environ.get("LINE_CHANNEL_SECRET", ""))
        token_len = len(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))
        print(f"LINE_CHANNEL_SECRET length: {secret_len}")
        print(f"LINE_CHANNEL_ACCESS_TOKEN length: {token_len}")
        if secret_len == 0:
            print("ERROR: LINE_CHANNEL_SECRET is empty. Stop and export it before starting the bot.", file=sys.stderr)
            return 2
        if token_len == 0:
            print("ERROR: LINE_CHANNEL_ACCESS_TOKEN is empty. Stop and export it before starting the bot.", file=sys.stderr)
            return 2
    inbox_dir()
    server = ThreadingHTTPServer((args.host, args.port), LineCaptureHandler)
    print(f"Clinical OS LINE mobile capture bot: http://{args.host}:{args.port}/webhook")
    print(f"mobile_inbox: {inbox_dir()}")
    if args.allow_unsigned_dev:
        print("WARNING: unsigned dev mode enabled. Do not expose this server publicly.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
