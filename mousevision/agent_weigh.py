"""Full-video agent weighing (CPA Gemini) — bypasses frame-by-frame OCR SM.

Production path when ``weight_reader=agent``:
  original (or lightly transcoded) video → generateContent → session list → records.

Training: hardlink/copy source into ``run_*/source.*`` so job_uploads 14d prune
does not drop the training corpus.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("mousevision.agent_weigh")

DEFAULT_BASE_URL = "http://agent.invalid:46450"
DEFAULT_MODEL = "gemini-3-flash"
DEFAULT_MAX_BYTES = 12_000_000  # light-transcode threshold
DEFAULT_MIN_FPS = 8
DEFAULT_MAX_WIDTH = 720
DEFAULT_CRF = 28
DEFAULT_TIMEOUT_S = 420
DEFAULT_REVIEW_CONF = 0.7
# Local evidence attachment defaults (agent path).
DEFAULT_ATTACH_PHOTOS = True
DEFAULT_PHOTO_SAMPLE_INTERVAL_MS = 200
DEFAULT_PHOTO_PAD_S = 1.5
DEFAULT_PHOTO_WEIGHT_TOL = 0.25
DEFAULT_PHOTO_WINDOW_S = 5.0

FULL_VIDEO_PROMPT = """你是实验室小鼠称重视频分析助手。整段视频里有电子秤 LCD 和多只小鼠依次上称/下称。

任务：按时间顺序找出每一次完整称重会话，读出 LCD 上最可信的稳定体重（克）。

规则：
1. 只报告小鼠上称后相对稳定的平台读数；忽略 0.00 空秤、过渡闪烁、手部干扰瞬间。
2. 同一只鼠晃动时取最可信稳定值。
3. ordinal 从 1 递增；看不清则 weight_g=null 并说明。
4. 不要编造不存在的读数。
5. 若你认为一共有 N 只，sessions 长度应为 N。
6. 尽量给出该次称重在视频中的时间锚点（秒，相对于视频起点，允许近似）：
   - t_start_s：小鼠上称开始
   - t_end_s：小鼠下称结束
   - t_stable_s：读数最稳定的瞬间（用于抽取照片证据）
   看不准就填 null，不要编造精确时间。

只输出 JSON（不要 markdown）：
{
  "video": "label",
  "sessions": [
    {
      "ordinal": 1,
      "weight_g": 16.15,
      "confidence": 0.0到1.0,
      "note": "简短依据",
      "t_start_s": 3.2,
      "t_end_s": 7.8,
      "t_stable_s": 5.0
    }
  ],
  "summary": "一句话：共几只、读数是否清晰"
}
"""


class AgentWeighError(RuntimeError):
    """Agent call or parse failure."""


@dataclass
class AgentSession:
    ordinal: int
    weight_g: float | None
    confidence: float
    note: str = ""
    # Time anchors in seconds from video start (nullable — agent may omit).
    t_start_s: float | None = None
    t_end_s: float | None = None
    t_stable_s: float | None = None


@dataclass
class AgentWeighResult:
    sessions: list[AgentSession]
    summary: str = ""
    model: str = DEFAULT_MODEL
    input_mode: str = "original"  # original | light_transcode
    raw: dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0


def resolve_agent_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge YAML ``agent:`` block with env overrides."""
    cfg = cfg or {}
    block = dict(cfg.get("agent") or {}) if isinstance(cfg, dict) else {}
    lt = dict(block.get("light_transcode") or {})

    base = (
        os.environ.get("MOUSEVISION_AGENT_BASE_URL")
        or block.get("base_url")
        or DEFAULT_BASE_URL
    )
    model = (
        os.environ.get("MOUSEVISION_AGENT_MODEL")
        or block.get("model")
        or DEFAULT_MODEL
    )
    key = (
        os.environ.get("MOUSEVISION_AGENT_API_KEY")
        or os.environ.get("CPA_API_KEY")
        or block.get("api_key")
        or ""
    )
    max_bytes = int(
        os.environ.get("MOUSEVISION_AGENT_MAX_BYTES")
        or block.get("max_upload_bytes")
        or DEFAULT_MAX_BYTES
    )
    prefer_original = block.get("prefer_original", True)
    if os.environ.get("MOUSEVISION_AGENT_PREFER_ORIGINAL", "").lower() in {
        "0",
        "false",
        "no",
    }:
        prefer_original = False

    return {
        "base_url": str(base).rstrip("/"),
        "model": str(model),
        "api_key": str(key),
        "prefer_original": bool(prefer_original),
        "max_upload_bytes": max_bytes,
        "timeout_s": int(block.get("timeout_s") or DEFAULT_TIMEOUT_S),
        "review_confidence": float(
            block.get("review_confidence") or DEFAULT_REVIEW_CONF
        ),
        "fallback": str(block.get("fallback") or "none").lower(),
        "light_transcode": {
            "max_width": int(lt.get("max_width") or DEFAULT_MAX_WIDTH),
            "min_fps": int(lt.get("min_fps") or DEFAULT_MIN_FPS),
            "crf": int(lt.get("crf") or DEFAULT_CRF),
        },
        "attach_photos": bool(block.get("attach_photos", DEFAULT_ATTACH_PHOTOS)),
        "photo_sample_interval_ms": float(
            block.get("photo_sample_interval_ms") or DEFAULT_PHOTO_SAMPLE_INTERVAL_MS
        ),
        "photo_pad_s": float(block.get("photo_pad_s") or DEFAULT_PHOTO_PAD_S),
        "photo_weight_tol": float(
            block.get("photo_weight_tol") or DEFAULT_PHOTO_WEIGHT_TOL
        ),
        "photo_window_s": float(block.get("photo_window_s") or DEFAULT_PHOTO_WINDOW_S),
    }


def retain_source_video(
    video_path: str | Path,
    run_dir: str | Path,
    *,
    enabled: bool = True,
) -> Path | None:
    """Hardlink (or copy) source into ``run_dir/source.<ext>``.

    Returns retained path or None if disabled / source missing.
    Does not raise on link/copy soft failures after copy fallback fails —
    returns None so analysis can still proceed.
    """
    if not enabled:
        return None
    src = Path(video_path)
    if not src.is_file():
        log.warning("retain_source_video: missing %s", src)
        return None
    run = Path(run_dir)
    run.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower() or ".mp4"
    dest = run / f"source{suffix}"
    if dest.exists():
        return dest
    try:
        os.link(src, dest)
        log.info("retained source hardlink %s -> %s", src, dest)
        return dest
    except OSError:
        try:
            shutil.copy2(src, dest)
            log.info("retained source copy %s -> %s", src, dest)
            return dest
        except OSError as exc:
            log.warning("retain_source_video failed: %s", exc)
            return None


def should_retain_source(cfg: dict[str, Any] | None = None) -> bool:
    env = os.environ.get("MOUSEVISION_RETAIN_SOURCE_VIDEO", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    if cfg and cfg.get("retain_source_video") is not None:
        return bool(cfg.get("retain_source_video"))
    # Default on for agent path; callers can pass enabled= explicitly.
    return True


def _ffmpeg_bin() -> str:
    return os.environ.get("MOUSEVISION_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def light_transcode(
    src: Path,
    dest: Path,
    *,
    max_width: int = DEFAULT_MAX_WIDTH,
    min_fps: int = DEFAULT_MIN_FPS,
    crf: int = DEFAULT_CRF,
) -> Path:
    """Downscale/re-encode while keeping fps >= min_fps (never 1fps default)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # fps filter: min(source, max(min_fps, 8)) is hard; use fps=min_fps as floor sample
    # but allow higher if source is lower by using fps=min_fps only as target rate.
    fps = max(int(min_fps), DEFAULT_MIN_FPS)
    vf = f"scale='min({int(max_width)},iw)':-2,fps={fps}"
    cmd = [
        _ffmpeg_bin(),
        "-y",
        "-i",
        str(src),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        str(int(crf)),
        "-an",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not dest.is_file():
        raise AgentWeighError(
            f"light_transcode failed rc={proc.returncode}: {proc.stderr[-500:]}"
        )
    return dest


def _parse_agent_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            return json.loads(text[s : e + 1])
        raise AgentWeighError(f"agent response not JSON: {text[:300]}")


def _sessions_from_payload(payload: dict[str, Any]) -> list[AgentSession]:
    raw = payload.get("sessions") or []
    if not isinstance(raw, list):
        raise AgentWeighError("agent JSON missing sessions list")
    out: list[AgentSession] = []
    for i, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            continue
        w = row.get("weight_g", row.get("weight"))
        try:
            weight = None if w is None else float(w)
        except (TypeError, ValueError):
            weight = None
        try:
            conf = float(row.get("confidence") if row.get("confidence") is not None else 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        try:
            ord_ = int(row.get("ordinal") or i)
        except (TypeError, ValueError):
            ord_ = i

        def _float_or_none(*keys: str) -> float | None:
            for k in keys:
                if k in row and row[k] is not None:
                    try:
                        return float(row[k])
                    except (TypeError, ValueError):
                        return None
            return None

        t_start = _float_or_none("t_start_s", "start_s")
        t_end = _float_or_none("t_end_s", "end_s")
        t_stable = _float_or_none("t_stable_s", "stable_s")
        out.append(
            AgentSession(
                ordinal=ord_,
                weight_g=weight,
                confidence=conf,
                note=str(row.get("note") or ""),
                t_start_s=t_start,
                t_end_s=t_end,
                t_stable_s=t_stable,
            )
        )
    return out


class AgentWeighClient:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.cfg = resolve_agent_config(config)

    def weigh_video(self, video_path: str | Path, *, label: str = "video") -> AgentWeighResult:
        import time

        src = Path(video_path)
        if not src.is_file():
            raise AgentWeighError(f"video not found: {src}")

        prefer = bool(self.cfg["prefer_original"])
        max_bytes = int(self.cfg["max_upload_bytes"])
        lt = self.cfg["light_transcode"]
        tmp_dir: tempfile.TemporaryDirectory[str] | None = None
        input_mode = "original"
        send_path = src

        try:
            if (not prefer) or src.stat().st_size > max_bytes:
                tmp_dir = tempfile.TemporaryDirectory(prefix="agent_weigh_")
                dest = Path(tmp_dir.name) / "light.mp4"
                send_path = light_transcode(
                    src,
                    dest,
                    max_width=int(lt["max_width"]),
                    min_fps=int(lt["min_fps"]),
                    crf=int(lt["crf"]),
                )
                input_mode = "light_transcode"
                log.info(
                    "agent input light_transcode size=%s->%s",
                    src.stat().st_size,
                    send_path.stat().st_size,
                )

            t0 = time.time()
            try:
                payload = self._generate(send_path, label=label)
            except AgentWeighError as exc:
                msg = str(exc).lower()
                # Config / auth errors should not trigger a useless re-encode.
                if "api_key" in msg or "not set" in msg:
                    raise
                # One retry with light transcode if we sent original
                # (payload/timeout issues often improve after compress).
                if input_mode == "original":
                    log.warning("agent original failed; retry light_transcode: %s", exc)
                    if tmp_dir is None:
                        tmp_dir = tempfile.TemporaryDirectory(prefix="agent_weigh_")
                    dest = Path(tmp_dir.name) / "light_retry.mp4"
                    send_path = light_transcode(
                        src,
                        dest,
                        max_width=int(lt["max_width"]),
                        min_fps=int(lt["min_fps"]),
                        crf=int(lt["crf"]),
                    )
                    input_mode = "light_transcode"
                    payload = self._generate(send_path, label=label)
                else:
                    raise
            latency = time.time() - t0
            sessions = _sessions_from_payload(payload)
            return AgentWeighResult(
                sessions=sessions,
                summary=str(payload.get("summary") or ""),
                model=str(self.cfg["model"]),
                input_mode=input_mode,
                raw=payload,
                latency_s=round(latency, 2),
            )
        finally:
            if tmp_dir is not None:
                tmp_dir.cleanup()

    def _generate(self, video_path: Path, *, label: str) -> dict[str, Any]:
        key = self.cfg["api_key"]
        if not key:
            raise AgentWeighError(
                "MOUSEVISION_AGENT_API_KEY (or CPA_API_KEY) not set"
            )
        b64 = base64.b64encode(video_path.read_bytes()).decode("ascii")
        model = self.cfg["model"]
        base = self.cfg["base_url"]
        url = f"{base}/v1beta/models/{model}:generateContent"
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": FULL_VIDEO_PROMPT.replace('"label"', json.dumps(label))},
                        {
                            "inline_data": {
                                "mime_type": "video/mp4",
                                "data": b64,
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json",
            },
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = int(self.cfg["timeout_s"])
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                code = resp.status
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")[:800]
            raise AgentWeighError(f"agent HTTP {exc.code}: {err}") from exc
        except Exception as exc:  # noqa: BLE001
            raise AgentWeighError(f"agent request failed: {exc}") from exc

        if code != 200:
            raise AgentWeighError(f"agent HTTP {code}: {raw[:500]}")

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentWeighError(f"agent envelope not JSON: {raw[:300]}") from exc

        texts: list[str] = []
        for cand in envelope.get("candidates") or []:
            for part in (cand.get("content") or {}).get("parts") or []:
                if "text" in part:
                    texts.append(str(part["text"]))
        if not texts:
            raise AgentWeighError(f"agent empty candidates: {raw[:400]}")
        return _parse_agent_json("\n".join(texts))


def persist_agent_sessions(
    *,
    result: AgentWeighResult,
    run_dir: Path,
    cage_id: str,
    run_id: str,
    device_id: str,
    project_id: str,
    start_ordinal: int,
    review_confidence: float = DEFAULT_REVIEW_CONF,
    upload_queue: Any | None = None,
    source_video: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Write mouse_NNN records from agent sessions; return record dicts."""
    from datetime import datetime

    from mousevision.recorder import Recorder
    from mousevision.run import bump_record_count
    from mousevision.types import AnalysisResult

    recorder = Recorder(run_dir, device_id)
    records: list[dict[str, Any]] = []
    for i, sess in enumerate(result.sessions):
        ordinal = int(start_ordinal) + i
        weight = sess.weight_g
        conf = float(sess.confidence or 0.0)
        needs = weight is None or conf < float(review_confidence)
        reasons: list[str] = []
        if weight is None:
            reasons.append("agent_null_weight")
        elif conf < float(review_confidence):
            reasons.append("agent_low_confidence")
        analysis = AnalysisResult(
            weight=weight,
            confidence=conf,
            platform_start_ms=0.0,
            platform_end_ms=0.0,
            photo_frame_index=None,
            weight_source="agent_full_video",
            needs_review=needs,
            review_reason=",".join(reasons),
            requires_manual_weight=weight is None,
            guessed_weight=None,
        )
        out = recorder.save(
            cage_id=cage_id,
            ordinal=ordinal,
            run_id=run_id,
            analysis=analysis,
            curve=[],
            photo_frame=None,
            project_id=project_id,
            requested_ordinal=start_ordinal if i == 0 else None,
        )
        record = json.loads((out / "record.json").read_text(encoding="utf-8"))
        record["agent_note"] = sess.note
        record["agent_model"] = result.model
        record["agent_input_mode"] = result.input_mode
        record["agent_summary"] = result.summary
        record["agent_latency_s"] = result.latency_s
        if sess.t_start_s is not None:
            record["agent_t_start_s"] = float(sess.t_start_s)
        if sess.t_end_s is not None:
            record["agent_t_end_s"] = float(sess.t_end_s)
        if sess.t_stable_s is not None:
            record["agent_t_stable_s"] = float(sess.t_stable_s)
        if source_video is not None:
            record["source_video"] = str(source_video)
        (out / "record.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        bump_record_count(run_dir)
        if upload_queue is not None and weight is not None:
            try:
                photo_file = out / "photo.jpg"
                upload_queue.enqueue(
                    record,
                    record_path=out / "record.json",
                    photo_path=photo_file if photo_file.exists() else None,
                    status="Held",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("agent enqueue failed ordinal=%s: %s", ordinal, exc)
        records.append(record)
        log.info(
            "agent session ordinal=%s weight=%s conf=%.3f review=%s",
            ordinal,
            weight,
            conf,
            needs,
        )
    # touch timestamp for debugging empty lists
    if not records:
        log.warning("agent returned zero sessions at %s", datetime.now().isoformat())
    return records
