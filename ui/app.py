"""MouseVision Edge — local inspection UI (FastAPI)."""

from __future__ import annotations

import os
import json
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import Body, Cookie, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mousevision.clip import clip_bounds_from_record
from mousevision.detector import WeighingState
from mousevision.driver import SessionDriver, SessionSavedEvent
from mousevision.jobs import AnalysisJobManager, JobStore
from mousevision.pipeline import load_config
from mousevision.run import create_run_dir, finish_run, load_manifest, restore_renumber_temps, write_manifest
from mousevision.source.video import VideoFileSource
from mousevision.upload_queue import UploadQueue
from ui.audit import AuditStore
from ui.auth import (
    api_token,
    check_login_rate_limit,
    clear_login_failures,
    cookie_secure,
    current_user,
    record_login_failure,
    require_active_user,
    require_api_token,
    require_role,
    require_token_or_operator,
    require_user,
    require_write_access,
    set_user_store,
)
from ui.boxes import BoxRegistry, qr_payload, strain_from_cage
from ui.records_api import (
    collect_records,
    export_csv,
    export_xlsx,
    mice_admin_view,
    overview_stats,
)
from ui.records_meta import RecordsMetaStore
from ui.registry import MouseRegistry
from ui.settings import SettingsStore
from ui.users import SESSION_COOKIE, UserStore

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_VIDEO = ROOT / "RefVideo" / "9494224d488d6e735c0f108cc5562a2d.mp4"
DEFAULT_CONFIG = ROOT / "configs" / "scale_refvideo.yaml"
DEFAULT_TEMPLATES = ROOT / "assets" / "templates"
DEFAULT_OUTPUT = Path(os.getenv("MOUSEVISION_OUTPUT_DIR", str(ROOT / "output"))).resolve()
REGISTRY_PATH = DEFAULT_OUTPUT / "mice_registry.json"
QUEUE_DB = DEFAULT_OUTPUT / "upload_queue.db"
JOB_DB = DEFAULT_OUTPUT / "jobs.db"
BOX_DB = DEFAULT_OUTPUT / "boxes.db"
META_DB = DEFAULT_OUTPUT / "records_meta.db"
USERS_DB = DEFAULT_OUTPUT / "users.db"
AUDIT_DB = DEFAULT_OUTPUT / "audit.db"
SETTINGS_PATH = DEFAULT_OUTPUT / "settings.json"
JOB_UPLOAD_ROOT = DEFAULT_OUTPUT / "job_uploads"
MAX_UPLOAD_BYTES = int(os.getenv("MOUSEVISION_MAX_UPLOAD_MB", "250")) * 1024 * 1024

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"})


STEP_ORDER = [
    ("scan", "1 扫码"),
    ("weigh", "2 放鼠称重"),
    ("stable", "3 稳定读取"),
    ("save", "4 记录保存"),
]


def _strain_from_cage(cage_id: str) -> str:
    return "C57BL/6" if cage_id.upper().startswith("C57") else "-"


def _inject_api_token(html: str) -> str:
    token = api_token()
    if not token:
        return html
    meta = f'  <meta name="mousevision-api-token" content="{token}" />\n'
    return html.replace("</head>", f"{meta}</head>", 1)


def _detect_mouse_box(
    image: np.ndarray,
    lcd: Any | None,
    *,
    gray_thr: int = 70,
    min_area: int = 800,
    x_ratio: tuple[float, float] = (0.12, 0.88),
) -> tuple[int, int, int, int] | None:
    h, w = image.shape[:2]
    y1 = 40
    y2 = lcd.y - 10 if lcd is not None else int(h * 0.55)
    x1, x2 = int(w * x_ratio[0]), int(w * x_ratio[1])
    if y2 <= y1 + 20:
        return None
    roi = image[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, gray_thr, 255, cv2.THRESH_BINARY_INV)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if cv2.contourArea(contour) < min_area:
        return None
    x, y, bw, bh = cv2.boundingRect(contour)
    return x1 + x, y1 + y, bw, bh


def _steps_from_state(state: WeighingState, has_box: bool, saved: bool) -> list[dict[str, Any]]:
    mapping = {
        WeighingState.EMPTY: 0 if not has_box else 1,
        WeighingState.ENTER: 1,
        WeighingState.WEIGHING: 2,
        WeighingState.LEAVE: 2,
        WeighingState.ANALYZE: 3,
    }
    active = 3 if saved else mapping.get(state, 0)
    if has_box and active < 1 and state == WeighingState.EMPTY:
        active = 0
    result = []
    for i, (key, label) in enumerate(STEP_ORDER):
        if saved:
            status = "done" if i < len(STEP_ORDER) - 1 else "active"
        elif i < active:
            status = "done"
        elif i == active:
            status = "active"
        else:
            status = "todo"
        result.append({"key": key, "label": label, "status": status})
    return result


def _hint(state: WeighingState, saved: bool, persist: bool) -> str:
    if not persist:
        return "只读复核中（不会写入新记录）"
    if saved:
        return "请取走小鼠，放入下一只"
    if state == WeighingState.EMPTY:
        return "请扫描箱号二维码，或点击开始回放"
    if state == WeighingState.ENTER:
        return "检测到重量上升，正在进入称重"
    if state == WeighingState.WEIGHING:
        return "称重中…可直接取走小鼠"
    if state == WeighingState.LEAVE:
        return "小鼠已离开，准备分析曲线"
    if state == WeighingState.ANALYZE:
        return "正在回溯分析并保存记录"
    return ""


@dataclass
class SessionState:
    playing: bool = False
    paused: bool = False
    cage_id: str = "C57-023"
    mouse_no: int | None = None
    run_id: str | None = None
    run_dir: str | None = None
    persist: bool = True
    token: str = ""
    state: str = "EMPTY"
    weight: float | None = None
    confidence: float = 0.0
    live_weight: float | None = None
    live_confidence: float = 0.0
    timestamp: str = ""
    rec_seconds: float = 0.0
    recording: bool = False
    mouse_detected: bool = False
    lcd_detected: bool = False
    qr_ok: bool = True
    saved: bool = False
    record: dict[str, Any] | None = None
    output_dir: str | None = None
    frame_jpeg: bytes | None = None
    curve: list[dict[str, float]] = field(default_factory=list)
    message: str = "就绪"
    fps: float = 0.0
    continuous: bool = False
    session_count: int = 0
    last_saved_index: int | None = None
    conflict: bool = False
    clip_start_ms: float | None = None
    clip_end_ms: float | None = None
    target_ordinal: int | None = None
    saved_ordinal: int | None = None
    source_video: str | None = None


class PlaybackEngine:
    def __init__(self, registry: MouseRegistry, upload_queue: UploadQueue) -> None:
        self.lock = threading.Lock()
        self.state = SessionState()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._token: str | None = None
        self.video_path = DEFAULT_VIDEO
        self.config_path = DEFAULT_CONFIG
        self.templates_dir = DEFAULT_TEMPLATES
        self.output_root = DEFAULT_OUTPUT
        self.playback_speed = 1.0
        self.registry = registry
        self.upload_queue = upload_queue
        self._config = load_config(self.config_path)

    def status(self) -> dict[str, Any]:
        with self.lock:
            s = self.state
            sm_state = WeighingState(s.state) if s.state in WeighingState.__members__ else WeighingState.EMPTY
            mouse_no = s.mouse_no
            return {
                "playing": s.playing,
                "paused": s.paused,
                "cage_id": s.cage_id,
                "box_id": s.cage_id,
                "mouse_no": mouse_no,
                "run_id": s.run_id,
                "run_dir": s.run_dir,
                "persist": s.persist,
                "token": s.token,
                "strain": _strain_from_cage(s.cage_id),
                "mouse_index": f"{mouse_no:02d}" if mouse_no is not None else "-",
                "state": s.state,
                "weight": s.weight if s.weight is not None else s.live_weight,
                "confidence": s.confidence if s.saved else s.live_confidence,
                "live_weight": s.live_weight,
                "live_confidence": s.live_confidence,
                "timestamp": s.timestamp,
                "rec_seconds": s.rec_seconds,
                "recording": s.recording,
                "mouse_detected": s.mouse_detected,
                "lcd_detected": s.lcd_detected,
                "qr_ok": s.qr_ok,
                "saved": s.saved,
                "record": s.record,
                "output_dir": s.output_dir,
                "steps": _steps_from_state(sm_state, s.qr_ok, s.saved),
                "hint": _hint(sm_state, s.saved, s.persist),
                "message": s.message,
                "curve": s.curve[-80:],
                "fps": s.fps,
                "stable": bool(
                    s.saved
                    or (s.live_weight and s.live_confidence >= 0.85 and s.state == "WEIGHING")
                ),
                "next_index": self.registry.peek_next_ordinal(s.run_id),
                "continuous": s.continuous,
                "session_count": s.session_count,
                "last_saved_index": s.last_saved_index,
                "conflict": s.conflict,
                "clip_start_ms": s.clip_start_ms,
                "clip_end_ms": s.clip_end_ms,
                "target_ordinal": s.target_ordinal,
                "saved_ordinal": s.saved_ordinal,
                "source_video": s.source_video,
            }

    def _resolve_run_video(self, source_run_id: str) -> Path | None:
        """Resolve and validate the video path recorded in a run manifest."""
        runs = self.registry.list_runs()
        match = next((r for r in runs if r["run_id"] == source_run_id), None)
        if match is None:
            return None
        manifest = load_manifest(Path(match["path"])) or {}
        raw = manifest.get("source_id")
        if not raw:
            return None
        candidates = [Path(raw)]
        if not Path(raw).is_absolute():
            candidates.extend([ROOT / raw, Path.cwd() / raw, self.video_path.parent / Path(raw).name])
        for cand in candidates:
            try:
                if cand.exists() and cand.is_file():
                    return cand.resolve()
            except OSError:
                continue
        return None

    def start(
        self,
        cage_id: str = "C57-023",
        speed: float = 1.0,
        continuous: bool = False,
        persist: bool = True,
        force: bool = False,
        run_id: str | None = None,
        ordinal: int | None = None,
    ) -> dict[str, Any]:
        cage = cage_id
        target_ordinal = ordinal

        clip_start_ms: float | None = None
        clip_end_ms: float | None = None
        source_run_id = run_id
        video_path = Path(self.video_path)
        if target_ordinal is not None and source_run_id:
            mouse = self.registry.get(int(target_ordinal), run_id=source_run_id)
            if mouse is None:
                status = self.status()
                status["ok"] = False
                status["error"] = "mouse_not_found"
                status["message"] = f"未找到 run={source_run_id} ordinal={target_ordinal}"
                return status
            record_path = self.output_root / mouse["dir"] / "record.json"
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except Exception:
                record = dict(mouse)
            clip_start_ms, clip_end_ms = clip_bounds_from_record(record)
            cage = str(record.get("cage_id") or mouse.get("cage_id") or cage)
            resolved = self._resolve_run_video(source_run_id)
            if resolved is None:
                status = self.status()
                status["ok"] = False
                status["error"] = "source_missing"
                status["message"] = f"批次 {source_run_id} 的源视频不存在或未记录"
                return status
            video_path = resolved

        with self.lock:
            alive = self._thread is not None and self._thread.is_alive()
        if alive:
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=2.0)
            with self.lock:
                still_alive = self._thread is not None and self._thread.is_alive()
            if still_alive and not force:
                with self.lock:
                    self.state.conflict = True
                    self.state.message = "上一次回放尚未结束，请先停止或稍后重试"
                status = self.status()
                status["ok"] = False
                status["error"] = "busy"
                return status

        token = uuid.uuid4().hex
        stop_event = threading.Event()
        run_dir: Path | None = None
        new_run_id = ""
        if persist:
            run_dir, manifest = create_run_dir(
                self.output_root,
                cage_id=cage,
                mode="video_batch" if continuous else "video_clip",
                source_id=str(video_path),
                device_id="scale01",
            )
            new_run_id = str(manifest["run_id"])
            self.registry.set_active_run(new_run_id, run_dir)

        self._stop = stop_event
        self._token = token
        review_msg = (
            f"只读复核 · 第 {target_ordinal:02d} 只"
            if target_ordinal is not None and not persist
            else None
        )
        with self.lock:
            self.state = SessionState(
                cage_id=cage,
                mouse_no=target_ordinal if target_ordinal is not None else (1 if continuous or persist else None),
                run_id=(new_run_id or source_run_id or None),
                run_dir=str(run_dir) if run_dir else None,
                persist=persist,
                token=token,
                qr_ok=True,
                playing=True,
                continuous=continuous,
                clip_start_ms=clip_start_ms,
                clip_end_ms=clip_end_ms,
                target_ordinal=target_ordinal,
                saved_ordinal=None,
                source_video=str(video_path),
                message=(
                    review_msg
                    or (
                        "整段回放中（新批次）"
                        if continuous and persist
                        else ("只读复核中" if not persist else "回放中 · 保存到新批次")
                    )
                ),
            )
            self.playback_speed = max(0.25, min(8.0, speed))

        self._thread = threading.Thread(
            target=self._run,
            kwargs={
                "token": token,
                "stop_event": stop_event,
                "run_dir": run_dir,
                "run_id": new_run_id,
                "cage_id": cage,
                "persist": persist,
                "continuous": continuous,
                "clip_start_ms": clip_start_ms,
                "clip_end_ms": clip_end_ms,
                "target_ordinal": target_ordinal,
                "video_path": video_path,
            },
            daemon=True,
        )
        self._thread.start()
        status = self.status()
        status["ok"] = True
        return status

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        with self.lock:
            self.state.playing = False
            self.state.recording = False
            self.state.message = "已停止"
            self.state.conflict = False
        return self.status()

    def _annotate(
        self,
        image: np.ndarray,
        *,
        weight: float | None,
        conf: float,
        mouse_box: tuple[int, int, int, int] | None,
        lcd: Any | None,
        state: str,
        rec_seconds: float,
        cage_id: str,
        mouse_no: int | None,
        persist: bool,
    ) -> np.ndarray:
        vis = image.copy()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.rectangle(vis, (16, 16), (250, 78), (20, 20, 20), -1)
        cv2.putText(vis, now, (28, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (240, 240, 240), 1, cv2.LINE_AA)
        if state in {"ENTER", "WEIGHING", "LEAVE"}:
            cv2.circle(vis, (36, 62), 6, (40, 40, 220), -1)
            cv2.putText(
                vis,
                f"REC {int(rec_seconds // 60):02d}:{int(rec_seconds % 60):02d}",
                (50, 68),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (220, 220, 255),
                1,
                cv2.LINE_AA,
            )
        if mouse_box is not None:
            x, y, bw, bh = mouse_box
            cv2.rectangle(vis, (x, y), (x + bw, y + bh), (40, 220, 120), 2)
        if lcd is not None:
            cv2.rectangle(vis, (lcd.x, lcd.y), (lcd.x + lcd.w, lcd.y + lcd.h), (0, 200, 255), 2)
        label = "N/A" if weight is None else f"{weight:.2f}g"
        badge = f"{cage_id}  #{mouse_no:02d}" if mouse_no else cage_id
        if not persist:
            badge = f"REVIEW  {badge}"
        cv2.rectangle(vis, (16, vis.shape[0] - 70), (320, vis.shape[0] - 20), (30, 30, 30), -1)
        cv2.putText(
            vis,
            f"{badge}  {label} ({conf:.2f})",
            (28, vis.shape[0] - 38),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (120, 230, 140),
            2,
            cv2.LINE_AA,
        )
        return vis

    def _run(
        self,
        *,
        token: str,
        stop_event: threading.Event,
        run_dir: Path | None,
        run_id: str,
        cage_id: str,
        persist: bool,
        continuous: bool,
        clip_start_ms: float | None = None,
        clip_end_ms: float | None = None,
        target_ordinal: int | None = None,
        video_path: Path | None = None,
    ) -> None:
        config = self._config
        stride = int(config.get("frame_stride", 2))
        mouse_cfg = config.get("mouse_detect") or {}
        gray_thr = int(mouse_cfg.get("gray_threshold", 70))
        min_area = int(mouse_cfg.get("min_area", 800))
        x_ratio_raw = mouse_cfg.get("x_ratio") or [0.12, 0.88]
        x_ratio = (float(x_ratio_raw[0]), float(x_ratio_raw[1]))

        session_start = None
        last_t = time.time()
        frames = 0
        # Clip replay always stops after the (single) session in range.
        stop_after_save = not continuous
        out_root = run_dir if run_dir is not None else self.output_root
        play_path = Path(video_path) if video_path is not None else Path(self.video_path)

        def on_saved(ev: SessionSavedEvent) -> None:
            nonlocal session_start
            ordinal = ev.session_index
            if persist and run_dir is not None:
                registered = self.registry.register(
                    run_id=run_id,
                    run_dir=run_dir,
                    cage_id=cage_id,
                    ordinal=ordinal,
                    record_id=ev.record.get("record_id"),
                    weight=ev.analysis_weight,
                    confidence=ev.analysis_confidence,
                    output_dir=ev.output_dir,
                    timestamp=ev.record.get("timestamp"),
                    device=str(config.get("device_id", "scale01")),
                )
            else:
                registered = {"index": ordinal, "ordinal": ordinal}

            session_start = None
            with self.lock:
                if self._token != token:
                    return
                s = self.state
                s.saved = persist
                s.record = ev.record
                s.output_dir = str(ev.output_dir) if persist else s.output_dir
                s.weight = ev.analysis_weight
                s.confidence = ev.analysis_confidence
                s.mouse_no = int(registered.get("ordinal", ordinal))
                s.last_saved_index = int(registered.get("ordinal", ordinal))
                s.saved_ordinal = int(registered.get("ordinal", ordinal))
                s.session_count += 1
                s.curve = [
                    {"t": p.timestamp_ms / 1000.0, "w": p.weight}
                    for p in ev.curve
                ][-80:]
                if continuous:
                    verb = "检出" if persist else "复核"
                    s.message = f"已{verb}第 {s.session_count} 只 · 继续"
                    s.saved = False if continuous else s.saved
                    s.weight = None
                    s.confidence = 0.0
                    s.curve = []
                else:
                    saved_ord = int(registered.get("ordinal", ordinal))
                    if persist and target_ordinal is not None:
                        s.message = (
                            f"来源第 {target_ordinal:02d} 只已保存为新批次第 {saved_ord:02d} 只"
                        )
                    elif persist:
                        s.message = f"已保存为第 {saved_ord:02d} 只"
                    else:
                        label = f"第 {target_ordinal:02d} 只" if target_ordinal else "本只"
                        s.message = f"{label}复核完成（未写入）"
                    s.playing = False
                    # Keep mouse_no as the ordinal in the active (new) run for photo URLs.
                    s.mouse_no = saved_ord

        driver = SessionDriver(
            config=config,
            templates_dir=self.templates_dir,
            output_root=out_root,
            cage_id=cage_id,
            run_id=run_id,
            device_id=str(config.get("device_id", "scale01")),
            persist=persist,
            on_saved=on_saved,
            upload_queue=self.upload_queue if persist else None,
        )
        source = VideoFileSource(
            play_path,
            frame_stride=stride,
            start_ms=clip_start_ms,
            end_ms=clip_end_ms,
        )
        try:
            for frame in source.frames():
                if stop_event.is_set() or self._token != token:
                    break
                t0 = time.time()
                event = driver.process_frame(frame)
                state = event.state

                if state in {WeighingState.ENTER, WeighingState.WEIGHING} and session_start is None:
                    session_start = frame.timestamp_ms
                rec_seconds = 0.0
                if session_start is not None:
                    rec_seconds = max(0.0, (frame.timestamp_ms - session_start) / 1000.0)

                mouse_box = _detect_mouse_box(
                    frame.image,
                    event.lcd,
                    gray_thr=gray_thr,
                    min_area=min_area,
                    x_ratio=x_ratio,
                )
                with self.lock:
                    mouse_no = self.state.mouse_no
                    continuous_now = self.state.continuous
                    session_count = self.state.session_count

                annotated = self._annotate(
                    frame.image,
                    weight=event.weight,
                    conf=event.confidence,
                    mouse_box=mouse_box,
                    lcd=event.lcd,
                    state=state.value,
                    rec_seconds=rec_seconds,
                    cage_id=cage_id,
                    mouse_no=mouse_no if continuous_now else (mouse_no or session_count or 1),
                    persist=persist,
                )
                ok, buf = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                jpeg = buf.tobytes() if ok else None

                frames += 1
                now = time.time()
                if now - last_t >= 0.5:
                    fps = frames / (now - last_t)
                    frames = 0
                    last_t = now
                else:
                    with self.lock:
                        fps = self.state.fps

                with self.lock:
                    if self._token != token:
                        break
                    s = self.state
                    s.state = state.value
                    s.live_weight = event.weight
                    s.live_confidence = event.confidence
                    s.lcd_detected = event.lcd is not None
                    s.mouse_detected = mouse_box is not None
                    s.recording = state in {
                        WeighingState.ENTER,
                        WeighingState.WEIGHING,
                        WeighingState.LEAVE,
                    }
                    s.rec_seconds = rec_seconds
                    s.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    s.frame_jpeg = jpeg
                    s.fps = fps
                    if event.weight is not None and not s.saved:
                        s.curve.append({"t": frame.timestamp_ms / 1000.0, "w": event.weight})
                        s.curve = s.curve[-200:]
                    if continuous_now:
                        verb = "检出" if persist else "复核"
                        s.message = f"整段{verb}中 · 已 {session_count} 只"
                    elif not s.saved:
                        s.message = "只读复核中" if not persist else "回放中"
                    playing = s.playing

                if stop_after_save and not playing:
                    break

                elapsed = time.time() - t0
                target = (stride / 30.0) / self.playback_speed
                if target > elapsed:
                    time.sleep(target - elapsed)
        finally:
            source.close()
            if persist and run_dir is not None:
                finish_run(run_dir, status="completed")
            with self.lock:
                if self._token == token:
                    self.state.playing = False
                    if self.state.continuous and self.state.session_count:
                        self.state.message = f"整段完成 · 本批次共 {self.state.session_count} 只"
                    elif not self.state.saved and not self.state.session_count:
                        self.state.message = "回放结束"


registry = MouseRegistry(REGISTRY_PATH, DEFAULT_OUTPUT)
upload_queue = UploadQueue(QUEUE_DB)
engine = PlaybackEngine(registry, upload_queue)
job_store = JobStore(JOB_DB)
box_registry = BoxRegistry(BOX_DB)
records_meta = RecordsMetaStore(str(META_DB))
user_store = UserStore(str(USERS_DB))
audit_store = AuditStore(str(AUDIT_DB))
settings_store = SettingsStore(SETTINGS_PATH)
set_user_store(user_store)


def _reserve_ordinals(cage_id: str, count: int, project_id: str) -> int:
    return box_registry.reserve_ordinal(
        cage_id,
        count=count,
        project_id=project_id,
        baseline_records=DEFAULT_OUTPUT,
    )


def _release_ordinals(cage_id: str, ordinal: int) -> None:
    box_registry.release_ordinal(cage_id, ordinal)


job_manager = AnalysisJobManager(
    job_store,
    output_root=DEFAULT_OUTPUT,
    config_path=DEFAULT_CONFIG,
    templates_dir=DEFAULT_TEMPLATES,
    reserve_ordinals=_reserve_ordinals,
    release_ordinals=_release_ordinals,
    upload_queue=upload_queue,
)


def _audit(actor: str, action: str, **kwargs: Any) -> None:
    audit_store.log(actor=actor, action=action, **kwargs)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Repair any half-applied renumber from a crash, then seed box ordinals.
    for run in registry.list_runs():
        restore_renumber_temps(Path(run["path"]))
    box_registry.sync_from_records(DEFAULT_OUTPUT)
    job_manager.start()
    try:
        yield
    finally:
        job_manager.stop()


app = FastAPI(title="MouseVision Edge UI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _entry_redirect(to: str | None) -> RedirectResponse | None:
    mapping = {
        "mobile": "/mobile",
        "pc": "/pc",
        "manage": "/mobile/manage",
    }
    if to and to in mapping:
        return RedirectResponse(mapping[to], status_code=302)
    return None


@app.get("/", response_class=HTMLResponse, response_model=None)
def index(to: str | None = Query(None)) -> HTMLResponse | RedirectResponse:
    redirect = _entry_redirect((to or "").lower())
    if redirect is not None:
        return redirect
    # Entry page must NOT inject shared API token (PC login bypass risk).
    return HTMLResponse((STATIC / "entry.html").read_text(encoding="utf-8"))


@app.get("/legacy", response_class=HTMLResponse)
def legacy_index() -> HTMLResponse:
    return HTMLResponse(_inject_api_token((STATIC / "index.html").read_text(encoding="utf-8")))


@app.get("/pc", response_class=HTMLResponse)
def pc_index() -> HTMLResponse:
    # PC admin uses session cookies only — never inject shared token into HTML.
    return HTMLResponse((STATIC / "pc" / "index.html").read_text(encoding="utf-8"))


@app.get("/pc/{path:path}", response_class=HTMLResponse)
def pc_spa(path: str) -> HTMLResponse:
    return HTMLResponse((STATIC / "pc" / "index.html").read_text(encoding="utf-8"))


@app.get("/mobile", response_class=HTMLResponse)
def mobile_index() -> HTMLResponse:
    return HTMLResponse(_inject_api_token((STATIC / "mobile.html").read_text(encoding="utf-8")))


@app.get("/mobile/{path:path}", response_class=HTMLResponse)
def mobile_spa(path: str) -> HTMLResponse:
    """SPA fallback so History-API deep links (/mobile/record, ...) work.

    `/api/*` and `/static/*` are separate routes and are not affected.
    """
    return HTMLResponse(_inject_api_token((STATIC / "mobile.html").read_text(encoding="utf-8")))


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "mousevision",
        "analysis_worker": "running",
        "active_jobs": job_store.active_count(),
    }


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    public = {k: v for k, v in job.items() if k != "video_path"}
    job_id = str(job["job_id"])
    public["status_url"] = f"/api/jobs/{job_id}"
    public["report_url"] = f"/api/jobs/{job_id}/report"
    return public


def _clean_id(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not _SAFE_ID.fullmatch(cleaned):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} 仅支持 1–64 位字母、数字、点、横线和下划线",
        )
    return cleaned


def _upload_suffix(filename: str | None, content_type: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in _VIDEO_EXTENSIONS:
        return suffix
    if content_type == "video/mp4":
        return ".mp4"
    if content_type == "video/quicktime":
        return ".mov"
    if content_type == "video/webm":
        return ".webm"
    return ".video"


@app.post("/api/jobs", dependencies=[Depends(require_api_token)])
async def api_create_job(
    cage_id: str = Form(...),
    project_id: str = Form("default"),
    requested_ordinal: int | None = Form(None),
    expected_single: bool = Form(True),
    video: UploadFile = File(...),
) -> JSONResponse:
    """Upload one video and enqueue a run-scoped analysis job."""
    cage = _clean_id(cage_id, field_name="箱号")
    project = _clean_id(project_id or "default", field_name="项目号")
    content_type = (video.content_type or "").lower()
    if content_type and not (
        content_type.startswith("video/") or content_type == "application/octet-stream"
    ):
        raise HTTPException(status_code=415, detail="仅支持视频文件")

    # Server owns ordinal allocation; ignore any client-provided value except
    # as an idempotency hint. `expected_single` reserves exactly one slot.
    reserve_count = 1 if expected_single else 1
    ordinal = box_registry.reserve_ordinal(
        cage, count=reserve_count, project_id=project
    )

    job = job_store.create_job(
        project_id=project,
        cage_id=cage,
        original_filename=video.filename,
        content_type=video.content_type,
        requested_ordinal=ordinal,
    )
    job_id = str(job["job_id"])
    upload_dir = JOB_UPLOAD_ROOT / job_id
    upload_dir.mkdir(parents=True, exist_ok=False)
    target = upload_dir / f"source{_upload_suffix(video.filename, content_type)}"
    size = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"视频超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 限制",
                    )
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="视频文件为空")
        job_store.update(
            job_id,
            video_path=str(target.resolve()),
            size_bytes=size,
            stage="uploaded",
            progress=0.04,
            message="上传完成",
        )
        queued = job_manager.submit(job_id)
        return JSONResponse(_job_payload(queued), status_code=202)
    except HTTPException as exc:
        target.unlink(missing_ok=True)
        box_registry.release_ordinal(cage, ordinal)
        job_store.update(
            job_id,
            status="failed",
            stage="upload_failed",
            progress=1.0,
            message="上传失败",
            error=str(exc.detail),
        )
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        box_registry.release_ordinal(cage, ordinal)
        job_store.update(
            job_id,
            status="failed",
            stage="upload_failed",
            progress=1.0,
            message="上传失败",
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="视频保存失败") from exc
    finally:
        await video.close()


@app.get("/api/jobs")
def api_jobs(limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]:
    items = [_job_payload(job) for job in job_store.list_jobs(limit)]
    return {"items": items, "active": job_store.active_count()}


def _elapsed_sec(iso_ts: str | None) -> float:
    if not iso_ts:
        return 0.0
    try:
        return max(0.0, (datetime.now() - datetime.fromisoformat(iso_ts)).total_seconds())
    except ValueError:
        return 0.0


@app.get("/api/jobs/queue")
def api_jobs_queue() -> dict[str, Any]:
    """Queue visibility for the mobile 'record done / waiting' screen.

    NOTE: must be registered before `/api/jobs/{job_id}` or it would be
    swallowed as job_id="queue".
    """
    avg = job_store.avg_duration_sec()
    processing_jobs = job_store.list_by_status("processing")
    processing = None
    if processing_jobs:
        p = processing_jobs[0]
        elapsed = round(_elapsed_sec(p.get("processing_started_at")), 1)
        processing = {
            "job_id": p["job_id"],
            "cage_id": p["cage_id"],
            "requested_ordinal": p.get("requested_ordinal"),
            "processing_started_at": p.get("processing_started_at"),
            "elapsed_sec": elapsed,
        }
    queued_rows = job_store.list_by_status("queued")
    queued = [
        {
            "job_id": row["job_id"],
            "cage_id": row["cage_id"],
            "requested_ordinal": row.get("requested_ordinal"),
            "position": idx + 1,
        }
        for idx, row in enumerate(queued_rows)
    ]
    return {
        "processing": processing,
        "queued": queued,
        "avg_duration_sec": avg,
    }


def _estimate_wait_sec(
    position: int, avg: float | None, processing: dict[str, Any] | None
) -> float | None:
    """等待秒数 = 当前剩余 + (position - 1) × avg (design §8.2)."""
    if avg is None:
        return None
    if processing is not None:
        current_remaining = max(0.0, avg - float(processing.get("elapsed_sec") or 0.0))
    else:
        current_remaining = 0.0
    return round(current_remaining + max(0, position - 1) * avg, 1)


@app.get("/api/jobs/{job_id}/wait")
def api_job_wait(job_id: str) -> dict[str, Any]:
    """Position + estimated wait for one queued/processing job."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    snapshot = api_jobs_queue()
    avg = snapshot["avg_duration_sec"]
    processing = snapshot["processing"]
    position = 0
    for item in snapshot["queued"]:
        if item["job_id"] == job_id:
            position = item["position"]
            break
    if job["status"] == "processing":
        return {
            "status": "processing",
            "position": 0,
            "estimated_wait_sec": _estimate_wait_sec(1, avg, processing),
        }
    return {
        "status": job["status"],
        "position": position,
        "estimated_wait_sec": _estimate_wait_sec(position, avg, processing),
    }


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> dict[str, Any]:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _job_payload(job)


@app.get("/api/jobs/{job_id}/report")
def api_job_report(job_id: str) -> dict[str, Any]:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    run_id = job.get("run_id")
    mice = registry.list_mice(run_id=str(run_id)) if run_id else []
    items = []
    weights: list[float] = []
    confidences: list[float] = []
    for mouse in mice:
        ordinal = int(mouse["ordinal"])
        weight = mouse.get("weight")
        confidence = mouse.get("confidence")
        if weight is not None:
            weights.append(float(weight))
        if confidence is not None:
            confidences.append(float(confidence))
        items.append(
            {
                **mouse,
                "photo_url": f"/api/mice/{ordinal}/photo?run_id={mouse['run_id']}",
            }
        )
    summary = {
        "record_count": len(items),
        "average_weight": round(sum(weights) / len(weights), 2) if weights else None,
        "min_weight": min(weights) if weights else None,
        "max_weight": max(weights) if weights else None,
        "average_confidence": (
            round(sum(confidences) / len(confidences), 3) if confidences else None
        ),
    }
    return {"job": _job_payload(job), "summary": summary, "items": items}


# --------------------------------------------------------------------------- #
# Boxes (cage) registry + unified per-cage record list (design §8.1 / §8.1a)
# --------------------------------------------------------------------------- #


class BoxCreate(BaseModel):
    cage_id: str
    strain: str | None = None
    notes: str = ""
    project_id: str = "default"
    mouse_no_start: int = Field(1, ge=1)
    mouse_no_pad: int = Field(2, ge=1, le=6)


class BoxUpdate(BaseModel):
    strain: str | None = None
    notes: str | None = None
    mouse_no_pad: int | None = Field(None, ge=1, le=6)


def _box_stats(cage_id: str) -> dict[str, Any]:
    jobs = job_store.list_by_cage(cage_id)
    pending = sum(
        1 for j in jobs if j["status"] in {"uploading", "queued", "processing"}
    )
    last_at = max((j.get("created_at") for j in jobs), default=None) if jobs else None
    # Count records actually on disk for this cage (deletes are reflected
    # immediately; job.record_count is only a snapshot at completion time).
    on_disk = 0
    for run in registry.list_runs():
        if (run.get("cage_id") or "-") != cage_id:
            continue
        on_disk += len(registry._mice_in_dir(Path(run["path"]), run_id=run["run_id"]))
    return {
        "record_count": on_disk,
        "pending_count": pending,
        "last_activity_at": last_at,
    }


@app.get("/api/boxes")
def api_boxes(
    strain: str | None = Query(None), limit: int = Query(100, ge=1, le=500)
) -> dict[str, Any]:
    boxes = box_registry.list(strain=strain, limit=limit)
    for box in boxes:
        box.update(_box_stats(box["cage_id"]))
    return {"items": boxes}


@app.get("/api/boxes/recent")
def api_boxes_recent(limit: int = Query(5, ge=1, le=50)) -> dict[str, Any]:
    """Home screen recent activity, ordered by last job time then created."""
    boxes = box_registry.list(limit=200)
    enriched = []
    for box in boxes:
        stats = _box_stats(box["cage_id"])
        box.update(stats)
        enriched.append(box)
    enriched.sort(
        key=lambda b: (b.get("last_activity_at") or b.get("created_at") or ""),
        reverse=True,
    )
    return {"items": enriched[:limit]}


@app.post("/api/boxes", dependencies=[Depends(require_token_or_operator)])
def api_create_box(body: BoxCreate) -> JSONResponse:
    cage = _clean_id(body.cage_id, field_name="箱号")
    project = _clean_id(body.project_id or "default", field_name="项目号")
    try:
        box = box_registry.create(
            cage_id=cage,
            strain=body.strain,
            notes=body.notes,
            project_id=project,
            mouse_no_start=body.mouse_no_start,
            mouse_no_pad=body.mouse_no_pad,
        )
    except KeyError:
        raise HTTPException(status_code=409, detail="箱号已存在")
    box.update(_box_stats(cage))
    return JSONResponse(box, status_code=201)


@app.get("/api/boxes/{cage_id}")
def api_box(cage_id: str) -> dict[str, Any]:
    box = box_registry.get(cage_id)
    if box is None:
        raise HTTPException(status_code=404, detail="箱子不存在")
    box.update(_box_stats(cage_id))
    return box


@app.patch("/api/boxes/{cage_id}", dependencies=[Depends(require_token_or_operator)])
def api_update_box(cage_id: str, body: BoxUpdate) -> dict[str, Any]:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        box = box_registry.update(cage_id, **changes)
    except KeyError:
        raise HTTPException(status_code=404, detail="箱子不存在")
    box.update(_box_stats(cage_id))
    return box


@app.post(
    "/api/boxes/{cage_id}/reserve-ordinal",
    dependencies=[Depends(require_api_token)],
)
def api_reserve_ordinal(
    cage_id: str, project_id: str = Query("default")
) -> dict[str, Any]:
    cage = _clean_id(cage_id, field_name="箱号")
    project = _clean_id(project_id or "default", field_name="项目号")
    ordinal = box_registry.reserve_ordinal(cage, project_id=project)
    return {"cage_id": cage, "requested_ordinal": ordinal}


@app.get("/api/boxes/{cage_id}/qr.svg", response_model=None)
def api_box_qr(cage_id: str):
    """Printable QR (SVG) carrying {v, project_id, cage_id} (design §3.5.4)."""
    box = box_registry.get(cage_id)
    project = box["project_id"] if box else "default"
    payload = qr_payload(cage_id, project)
    import io

    import segno

    buf = io.BytesIO()
    segno.make(payload, error="m").save(
        buf, kind="svg", scale=6, border=2, dark="#212529"
    )
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


def _placeholder_from_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": f"job-{job['job_id']}",
        "job_id": job["job_id"],
        "record_id": None,
        "requested_ordinal": job.get("requested_ordinal"),
        "actual_ordinal": None,
        "status": job["status"],
        "run_id": job.get("run_id"),
        "weight": None,
        "confidence": None,
        "photo_url": None,
        "warning": None,
        "created_at": job.get("created_at"),
    }


def _item_from_record(job: dict[str, Any], mouse: dict[str, Any]) -> dict[str, Any]:
    record_id = mouse.get("record_id")
    return {
        "item_id": f"rec-{record_id}",
        "job_id": job["job_id"],
        "record_id": record_id,
        "requested_ordinal": job.get("requested_ordinal"),
        "actual_ordinal": mouse.get("actual_ordinal", mouse.get("ordinal")),
        "status": "completed",
        "run_id": mouse.get("run_id"),
        "weight": mouse.get("weight"),
        "confidence": mouse.get("confidence"),
        "photo_url": f"/api/records/{record_id}/photo" if record_id else None,
        "warning": None,
        "created_at": job.get("created_at"),
    }


@app.get("/api/boxes/{cage_id}/records")
def api_box_records(cage_id: str) -> dict[str, Any]:
    """Unified list: merge pending jobs and completed records (design §8.1a).

    Soft-deleted records are hidden from this mobile-facing endpoint.
    """
    jobs = job_store.list_by_cage(cage_id)
    items: list[dict[str, Any]] = []
    for job in jobs:
        if job["status"] == "completed":
            mice = registry.list_mice(run_id=str(job["run_id"])) if job.get("run_id") else []
            if not mice:
                placeholder = _placeholder_from_job(job)
                placeholder["warning"] = "no_detection"
                items.append(placeholder)
                continue
            mice_sorted = sorted(mice, key=lambda m: int(m.get("ordinal") or 0))
            for mouse in mice_sorted:
                rid = mouse.get("record_id")
                if rid and records_meta.effective_status(str(rid)) == "deleted":
                    continue
                item = _item_from_record(job, mouse)
                if len(mice_sorted) > 1:
                    item["warning"] = "multi_detected"
                items.append(item)
        else:
            items.append(_placeholder_from_job(job))

    def sort_key(item: dict[str, Any]) -> tuple[int, int]:
        ordinal = item.get("actual_ordinal") or item.get("requested_ordinal") or 0
        completed_first = 0 if item["status"] == "completed" else 1
        return (int(ordinal), completed_first)

    items.sort(key=sort_key)
    return {"cage_id": cage_id, "items": items}


# --------------------------------------------------------------------------- #
# Records by record_id (design §8.3) — unambiguous across runs
# --------------------------------------------------------------------------- #


def _assert_record_readable(record_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
    mouse = registry.get_by_record_id(record_id)
    if mouse is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    status = records_meta.effective_status(record_id)
    if status == "deleted" and not include_deleted:
        raise HTTPException(status_code=404, detail="记录不存在")
    return mouse


@app.get("/api/records/{record_id}")
def api_record(
    record_id: str,
    include_deleted: bool = Query(False),
    user: dict[str, Any] | None = Depends(current_user),
) -> dict[str, Any]:
    allow_deleted = bool(include_deleted and user and user.get("role") in {"admin", "operator"})
    mouse = _assert_record_readable(record_id, include_deleted=allow_deleted)
    mouse["photo_url"] = f"/api/records/{record_id}/photo"
    mouse["label"] = f"第 {int(mouse.get('actual_ordinal', mouse['ordinal'])):02d} 只"
    raw_path = DEFAULT_OUTPUT / mouse["dir"] / "record.json"
    if raw_path.exists():
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            mouse["duration_sec"] = (
                round(max(0.0, float(raw["clip_end_ms"]) - float(raw["clip_start_ms"])) / 1000.0, 1)
                if raw.get("clip_start_ms") is not None and raw.get("clip_end_ms") is not None
                else None
            )
            mouse["clip_start_ms"] = raw.get("clip_start_ms")
            mouse["clip_end_ms"] = raw.get("clip_end_ms")
        except Exception:
            pass
    meta = records_meta.ensure(record_id)
    mouse["status"] = meta["status"]
    mouse["verified"] = bool(meta.get("verified"))
    mouse["notes"] = meta.get("notes") or ""
    mouse["strain"] = strain_from_cage(str(mouse.get("cage_id") or "-"))
    return mouse


@app.get("/api/records/{record_id}/photo", response_model=None)
def api_record_photo(
    record_id: str,
    include_deleted: bool = Query(False),
    user: dict[str, Any] | None = Depends(current_user),
):
    allow_deleted = bool(include_deleted and user and user.get("role") in {"admin", "operator"})
    mouse = _assert_record_readable(record_id, include_deleted=allow_deleted)
    path = DEFAULT_OUTPUT / mouse["dir"] / mouse.get("photo", "photo.jpg")
    if not path.exists():
        raise HTTPException(status_code=404, detail="照片不存在")
    return FileResponse(path)


@app.delete("/api/records/{record_id}", dependencies=[Depends(require_write_access)])
def api_delete_record(record_id: str, user: dict[str, Any] = Depends(require_write_access)) -> dict[str, Any]:
    mouse = registry.get_by_record_id(record_id)
    if mouse is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    actor = user.get("username", "unknown")
    records_meta.soft_delete(record_id, operator=actor)
    upload_queue.delete_by_record_id(record_id)
    _audit(
        actor,
        "record.delete",
        target_type="record",
        target_id=record_id,
        detail={"soft": True},
    )
    return {"ok": True, "record_id": record_id, "status": "deleted"}


@app.post("/api/records/{record_id}/restore", dependencies=[Depends(require_write_access)])
def api_restore_record(record_id: str, user: dict[str, Any] = Depends(require_write_access)) -> dict[str, Any]:
    if registry.get_by_record_id(record_id) is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    actor = user.get("username", "unknown")
    meta = records_meta.restore(record_id, operator=actor)
    _audit(actor, "record.restore", target_type="record", target_id=record_id)
    return {"ok": True, "meta": meta}


class RecordMetaUpdate(BaseModel):
    notes: str | None = None
    tags: str | None = None


@app.patch("/api/records/{record_id}", dependencies=[Depends(require_write_access)])
def api_update_record_meta(
    record_id: str,
    body: RecordMetaUpdate,
    user: dict[str, Any] = Depends(require_write_access),
) -> dict[str, Any]:
    if registry.get_by_record_id(record_id) is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    actor = user.get("username", "unknown")
    meta = records_meta.update(record_id, operator=actor, **changes)
    _audit(actor, "record.update", target_type="record", target_id=record_id, detail=changes)
    return meta


@app.post("/api/records/{record_id}/publish", dependencies=[Depends(require_write_access)])
def api_publish_record(record_id: str, user: dict[str, Any] = Depends(require_write_access)) -> dict[str, Any]:
    if registry.get_by_record_id(record_id) is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    actor = user.get("username", "unknown")
    meta = records_meta.publish(record_id, operator=actor)
    _audit(actor, "record.publish", target_type="record", target_id=record_id)
    return {"ok": True, "meta": meta}


@app.post("/api/records/{record_id}/unpublish", dependencies=[Depends(require_write_access)])
def api_unpublish_record(record_id: str, user: dict[str, Any] = Depends(require_write_access)) -> dict[str, Any]:
    if registry.get_by_record_id(record_id) is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    actor = user.get("username", "unknown")
    meta = records_meta.unpublish(record_id, operator=actor)
    _audit(actor, "record.unpublish", target_type="record", target_id=record_id)
    return {"ok": True, "meta": meta}


@app.post("/api/records/{record_id}/verify", dependencies=[Depends(require_write_access)])
def api_verify_record(record_id: str, user: dict[str, Any] = Depends(require_write_access)) -> dict[str, Any]:
    if registry.get_by_record_id(record_id) is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    actor = user.get("username", "unknown")
    meta = records_meta.verify(record_id, operator=actor)
    _audit(actor, "record.verify", target_type="record", target_id=record_id)
    return {"ok": True, "meta": meta}


@app.post("/api/records/{record_id}/reject", dependencies=[Depends(require_write_access)])
def api_reject_record(record_id: str, user: dict[str, Any] = Depends(require_write_access)) -> dict[str, Any]:
    if registry.get_by_record_id(record_id) is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    actor = user.get("username", "unknown")
    meta = records_meta.reject(record_id, operator=actor)
    _audit(actor, "record.reject", target_type="record", target_id=record_id)
    return {"ok": True, "meta": meta}


class BatchRecordAction(BaseModel):
    record_ids: list[str]
    action: str = Field(..., pattern="^(publish|unpublish|delete|restore|verify|reject)$")


@app.post("/api/records/batch", dependencies=[Depends(require_write_access)])
def api_batch_records(
    body: BatchRecordAction,
    user: dict[str, Any] = Depends(require_write_access),
) -> dict[str, Any]:
    actor = user.get("username", "unknown")
    handlers = {
        "publish": records_meta.publish,
        "unpublish": records_meta.unpublish,
        "delete": records_meta.soft_delete,
        "restore": records_meta.restore,
        "verify": records_meta.verify,
        "reject": records_meta.reject,
    }
    handler = handlers[body.action]
    results = []
    for rid in body.record_ids:
        if registry.get_by_record_id(rid) is None:
            results.append({"record_id": rid, "ok": False, "error": "not_found"})
            continue
        meta = handler(rid, operator=actor)
        if body.action == "delete":
            upload_queue.delete_by_record_id(rid)
        results.append({"record_id": rid, "ok": True, "meta": meta})
    _audit(
        actor,
        f"record.batch.{body.action}",
        target_type="record",
        target_id=",".join(body.record_ids[:5]),
        detail={"count": len(body.record_ids)},
    )
    return {"results": results}


@app.get("/api/records")
def api_records(
    tab: str = Query("all"),
    strain: str | None = Query(None),
    cage_id: str | None = Query(None),
    q: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_deleted: bool = Query(False),
    user: dict[str, Any] = Depends(require_active_user),
) -> dict[str, Any]:
    # tab=deleted always includes deleted; otherwise require explicit flag.
    show_deleted = tab == "deleted" or include_deleted
    items = collect_records(
        registry,
        records_meta,
        DEFAULT_OUTPUT,
        tab=tab if tab != "all" else None,
        strain=strain,
        cage_id=cage_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
        include_deleted=show_deleted,
    )
    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]
    overview = overview_stats(registry, records_meta, DEFAULT_OUTPUT)
    return {
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "stats": {
            "total_records": overview["total_records"],
            "pending_count": overview["pending_count"],
            "published_count": overview["published_count"],
            "deleted_count": overview["deleted_count"],
            "average_weight": overview["average_weight"],
        },
    }


@app.get("/api/overview")
def api_overview(user: dict[str, Any] = Depends(require_active_user)) -> dict[str, Any]:
    return overview_stats(registry, records_meta, DEFAULT_OUTPUT)


@app.get("/api/mice-admin")
def api_mice_admin(user: dict[str, Any] = Depends(require_active_user)) -> dict[str, Any]:
    return {"items": mice_admin_view(registry, records_meta, DEFAULT_OUTPUT)}


@app.get("/api/export")
def api_export(
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
    tab: str = Query("all"),
    strain: str | None = Query(None),
    cage_id: str | None = Query(None),
    q: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    user: dict[str, Any] = Depends(require_active_user),
) -> Response:
    show_deleted = tab == "deleted"
    items = collect_records(
        registry,
        records_meta,
        DEFAULT_OUTPUT,
        tab=tab if tab != "all" else None,
        strain=strain,
        cage_id=cage_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
        include_deleted=show_deleted,
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if format == "xlsx":
        content = export_xlsx(items)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"mousevision_export_{stamp}.xlsx"
    else:
        content = export_csv(items)
        media = "text/csv; charset=utf-8"
        filename = f"mousevision_export_{stamp}.csv"
    _audit(
        user.get("username", "unknown"),
        "export.download",
        target_type="export",
        target_id=format,
        detail={"count": len(items)},
    )
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def api_login(request: Request, body: LoginRequest) -> JSONResponse:
    check_login_rate_limit(request)
    user = user_store.authenticate(body.username, body.password)
    if user is None:
        record_login_failure(request)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    clear_login_failures(request)
    token = user_store.create_session(user["id"])
    _audit(user["username"], "auth.login", target_type="user", target_id=user["id"])
    resp = JSONResponse({"ok": True, "user": user})
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
        max_age=7 * 24 * 3600,
    )
    return resp


@app.post("/api/logout")
def api_logout(
    request: Request,
    mv_session: str | None = Cookie(None, alias=SESSION_COOKIE),
) -> JSONResponse:
    if mv_session:
        user_store.delete_session(mv_session)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, secure=cookie_secure(request))
    return resp


@app.get("/api/me")
def api_me(user: dict[str, Any] | None = Depends(current_user)) -> dict[str, Any]:
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "user": user}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


@app.post("/api/me/password")
def api_change_own_password(
    request: Request,
    body: ChangePasswordRequest,
    user: dict[str, Any] = Depends(require_user),
) -> JSONResponse:
    """Change own password; allowed even when must_change_password is set.

    Updates the hash, revokes all existing sessions, then issues a fresh session
    cookie so the caller stays logged in.
    """
    if user.get("id") == "token":
        raise HTTPException(status_code=400, detail="API token 账号不能改密")
    full = user_store.get_by_username(user["username"])
    if full is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    from ui.users import verify_password

    if not verify_password(body.current_password, full["_password_hash"], full["_salt"]):
        raise HTTPException(status_code=401, detail="当前密码不正确")
    updated = user_store.update_user(user["id"], password=body.new_password)
    token = user_store.create_session(user["id"])
    _audit(user["username"], "auth.change_password", target_type="user", target_id=user["id"])
    resp = JSONResponse({"ok": True, "user": updated})
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
        max_age=7 * 24 * 3600,
    )
    return resp


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "operator"
    display_name: str = ""


class UserUpdate(BaseModel):
    role: str | None = None
    display_name: str | None = None
    disabled: bool | None = None
    password: str | None = None


@app.get("/api/users", dependencies=[Depends(require_role("admin"))])
def api_list_users() -> dict[str, Any]:
    return {"items": user_store.list_users()}


@app.post("/api/users", dependencies=[Depends(require_role("admin"))])
def api_create_user(
    body: UserCreate,
    actor: dict[str, Any] = Depends(require_role("admin")),
) -> JSONResponse:
    try:
        user = user_store.create_user(
            username=body.username,
            password=body.password,
            role=body.role,
            display_name=body.display_name,
        )
    except KeyError:
        raise HTTPException(status_code=409, detail="用户名已存在")
    _audit(
        actor["username"],
        "user.create",
        target_type="user",
        target_id=user["id"],
        detail={"username": body.username, "role": body.role},
    )
    return JSONResponse(user, status_code=201)


@app.patch("/api/users/{user_id}", dependencies=[Depends(require_role("admin"))])
def api_update_user(
    user_id: str,
    body: UserUpdate,
    actor: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        user = user_store.update_user(user_id, **changes)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    # AuditStore scrubbing redacts password; still pass full changes for completeness.
    _audit(actor["username"], "user.update", target_type="user", target_id=user_id, detail=changes)
    return user


@app.delete("/api/users/{user_id}", dependencies=[Depends(require_role("admin"))])
def api_delete_user(
    user_id: str,
    actor: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    if actor["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能删除当前登录用户")
    try:
        user_store.delete_user(user_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="用户不存在")
    _audit(actor["username"], "user.delete", target_type="user", target_id=user_id)
    return {"ok": True}


@app.get("/api/logs", dependencies=[Depends(require_role("admin", "operator"))])
def api_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: str | None = Query(None),
) -> dict[str, Any]:
    return audit_store.list(limit=limit, offset=offset, action=action)


@app.get("/api/settings")
def api_get_settings(user: dict[str, Any] = Depends(require_active_user)) -> dict[str, Any]:
    return settings_store.get()


@app.put("/api/settings", dependencies=[Depends(require_role("admin"))])
def api_put_settings(
    body: dict[str, Any] = Body(...),
    actor: dict[str, Any] = Depends(require_role("admin")),
) -> dict[str, Any]:
    try:
        updated = settings_store.update(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _audit(actor["username"], "settings.update", target_type="settings", detail=body)
    return updated



@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return engine.status()


@app.get("/api/runs")
def api_runs() -> dict[str, Any]:
    runs = registry.list_runs()
    active = registry.active_run()
    return {
        "items": runs,
        "active_run_id": active["run_id"] if active else None,
    }


@app.post("/api/runs/active", dependencies=[Depends(require_token_or_operator)])
def api_set_active_run(run_id: str = Query(...)) -> dict[str, Any]:
    runs = registry.list_runs()
    match = next((r for r in runs if r["run_id"] == run_id), None)
    if match is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    registry.set_active_run(run_id, Path(match["path"]))
    return {"ok": True, "active_run_id": run_id}


@app.get("/api/mice")
def api_mice(run_id: str | None = Query(None)) -> dict[str, Any]:
    active = registry.active_run()
    rid = run_id or (active["run_id"] if active else None)
    mice = registry.list_mice(run_id=rid)
    items = []
    for m in mice:
        items.append(
            {
                **m,
                "photo_url": f"/api/mice/{m['ordinal']}/photo"
                + (f"?run_id={m['run_id']}" if m.get("run_id") else ""),
                "label": f"第 {int(m['ordinal']):02d} 只",
            }
        )
    return {
        "items": items,
        "next_index": registry.peek_next_ordinal(rid),
        "run_id": rid,
        "cage_id": active.get("cage_id") if active else None,
        "run": active,
    }


@app.get("/api/mice/{index}")
def api_mouse(index: int, run_id: str | None = Query(None)) -> dict[str, Any]:
    mouse = registry.get(index, run_id=run_id)
    if mouse is None:
        return {"error": "not found"}
    mouse["photo_url"] = f"/api/mice/{index}/photo" + (
        f"?run_id={mouse['run_id']}" if mouse.get("run_id") else ""
    )
    mouse["label"] = f"第 {int(mouse['ordinal']):02d} 只"
    return mouse


@app.get("/api/mice/{index}/photo", response_model=None)
def api_mouse_photo(index: int, run_id: str | None = Query(None)):
    mouse = registry.get(index, run_id=run_id)
    if mouse is None:
        return {"error": "not found"}
    path = DEFAULT_OUTPUT / mouse["dir"] / mouse.get("photo", "photo.jpg")
    if not path.exists():
        return {"error": "photo missing"}
    return FileResponse(path)


class StartPlaybackRequest(BaseModel):
    cage_id: str = "C57-023"
    speed: float = Field(1.0, ge=0.25, le=8.0)
    continuous: bool = False
    persist: bool = True
    force: bool = False
    run_id: str | None = None
    ordinal: int | None = None


@app.post("/api/start", dependencies=[Depends(require_token_or_operator)])
def api_start(body: StartPlaybackRequest | None = Body(default=None)) -> Any:
    req = body or StartPlaybackRequest()
    result = engine.start(
        cage_id=req.cage_id,
        speed=req.speed,
        continuous=req.continuous,
        persist=req.persist,
        force=req.force,
        run_id=req.run_id,
        ordinal=req.ordinal,
    )
    if result.get("error") in {"busy", "mouse_not_found", "source_missing"}:
        code = 409 if result.get("error") == "busy" else 404
        return JSONResponse(result, status_code=code)
    return result


@app.post("/api/reset", dependencies=[Depends(require_token_or_operator)])
def api_reset() -> Any:
    """Clear registry and old weighing outputs (keeps debug folders)."""
    if job_store.active_count():
        return JSONResponse(
            {"ok": False, "error": "active_jobs", "message": "仍有上传或分析任务运行中"},
            status_code=409,
        )

    engine.stop()
    removed = 0
    keep = {
        "debug_digits",
        "roi_preview",
        "roi_probe",
        "mice_registry.json",
        "upload_queue.db",
        "jobs.db",
        "jobs.db-shm",
        "jobs.db-wal",
        JOB_UPLOAD_ROOT.name,
    }
    if DEFAULT_OUTPUT.exists():
        for path in list(DEFAULT_OUTPUT.iterdir()):
            if path.name in keep:
                continue
            if not path.is_dir():
                continue
            has_records = (path / "record.json").exists() or any(path.glob("**/record.json"))
            if path.name.startswith("run_") or has_records:
                shutil.rmtree(path)
                removed += 1
    job_store.clear()
    if JOB_UPLOAD_ROOT.exists():
        shutil.rmtree(JOB_UPLOAD_ROOT)
    JOB_UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    if REGISTRY_PATH.exists():
        REGISTRY_PATH.unlink()
    if QUEUE_DB.exists():
        QUEUE_DB.unlink()
    global registry, upload_queue
    registry = MouseRegistry(REGISTRY_PATH, DEFAULT_OUTPUT)
    upload_queue = UploadQueue(QUEUE_DB)
    engine.registry = registry
    engine.upload_queue = upload_queue
    return {
        "ok": True,
        "removed": removed,
        "jobs_cleared": True,
        "next_index": registry.peek_next_ordinal(),
    }


@app.post("/api/stop", dependencies=[Depends(require_token_or_operator)])
def api_stop() -> dict[str, Any]:
    return engine.stop()


@app.get("/api/upload-queue")
def api_upload_queue() -> dict[str, Any]:
    return {"counts": upload_queue.counts(), "pending": upload_queue.list_pending(20)}


@app.get("/api/stream")
def api_stream() -> StreamingResponse:
    def gen():
        while True:
            with engine.lock:
                jpeg = engine.state.frame_jpeg
            if jpeg is None:
                time.sleep(0.05)
                continue
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"
            time.sleep(0.04)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/photo", response_model=None)
def api_photo():
    with engine.lock:
        out = engine.state.output_dir
        mouse_no = engine.state.mouse_no
        run_id = engine.state.run_id
    if mouse_no is not None:
        mouse = registry.get(mouse_no, run_id=run_id)
        if mouse:
            path = DEFAULT_OUTPUT / mouse["dir"] / mouse.get("photo", "photo.jpg")
            if path.exists():
                return FileResponse(path)
    if not out:
        return {"error": "no photo yet"}
    path = Path(out) / "photo.jpg"
    if not path.exists():
        return {"error": "photo missing"}
    return FileResponse(path)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "ui.app:app",
        host=os.getenv("MOUSEVISION_HOST", "127.0.0.1"),
        port=int(os.getenv("MOUSEVISION_PORT", "8766")),
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
