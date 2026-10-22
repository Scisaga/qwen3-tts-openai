from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

from qwen3_tts_openai.config import Settings
from qwen3_tts_openai.errors import ServiceError
from qwen3_tts_openai.storage import file_hash

PROCESSING_VERSION = "rubberband-offline-pcm-v2"
REFERENCE_VERSION = "native-rate-mono-v1"


class AudioProcessor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.status = {"state": "unchecked"}

    def run(self, argv: list[str], *, include_stderr=False) -> str:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=self.settings.ffmpeg_timeout_seconds, check=False)
        except FileNotFoundError as e:
            raise ServiceError("audio_tool_missing", "Install ffmpeg/ffprobe with rubberband and libmp3lame, plus rubberband-cli", 503) from e
        except subprocess.TimeoutExpired as e:
            raise ServiceError("ffmpeg_timeout", "Audio processing timed out; shorten the job or increase FFMPEG_TIMEOUT_SECONDS", 504) from e
        if p.returncode:
            raise ServiceError("ffmpeg_failed", "FFmpeg/ffprobe failed; inspect the recorded stderr", 500, {"stderr": p.stderr[-3000:], "returncode": p.returncode})
        return p.stdout + (p.stderr if include_stderr else "")

    def self_check(self):
        try:
            filters = self.run([self.settings.ffmpeg_bin, "-hide_banner", "-filters"])
            encoders = self.run([self.settings.ffmpeg_bin, "-hide_banner", "-encoders"])
            if " rubberband " not in filters or " libmp3lame " not in encoders:
                raise ServiceError("ffmpeg_capability", "FFmpeg needs the rubberband filter and libmp3lame encoder", 503)
            # Exercise both, instead of trusting the capability listing.
            with tempfile.TemporaryDirectory() as temp:
                source, processed, mp3 = (Path(temp) / n for n in ("input.wav", "processed.wav", "out.mp3"))
                sf.write(source, np.sin(np.arange(12000) * 2 * np.pi * 440 / 24000) * 0.1, 24000, subtype="PCM_16")
                # The realtime FFmpeg filter is checked independently; production stretching is offline.
                self.run([self.settings.ffmpeg_bin, "-nostdin", "-v", "error", "-y", "-i", str(source), "-af", "rubberband=tempo=0.9:pitch=1.005792941:formant=preserved", str(processed)])
                self.process(source, processed, 0.9, 0.1, -1)
                self.encode(processed, mp3)
                encoder = self.encoded_info(mp3)
            self.status = {"state": "ready", "version": self.run([self.settings.ffmpeg_bin, "-version"]).splitlines()[0], "rubberband": True, "rubberband_cli": self.run([self.settings.rubberband_bin, "--version"], include_stderr=True).strip(), "mp3_encoder": "libmp3lame", "mp3_bitrate_bps": encoder["bit_rate"], "sample_rate": encoder["sample_rate"], "processing_version": PROCESSING_VERSION}
        except ServiceError as e:
            self.status = {"state": "failed", "error": e.payload()["error"]}
            raise
        return self.status

    def validate_reference(self, source: Path, processed: Path):
        if source.stat().st_size > self.settings.max_upload_bytes:
            raise ServiceError("upload_too_large", "Reference WAV exceeds MAX_UPLOAD_BYTES", 413)
        with source.open("rb") as f:
            magic = f.read(12)
        if magic[:4] != b"RIFF" or magic[8:12] != b"WAVE":
            raise ServiceError("invalid_audio", "Upload a RIFF WAV file; model weights, URLs and archives are not accepted")
        try:
            info = sf.info(source)
            if info.format != "WAV" or info.subtype not in {"PCM_16", "PCM_24", "PCM_32", "FLOAT"}:
                raise ServiceError("invalid_audio", "Reference must be PCM or float WAV")
            if not 1 <= info.duration <= self.settings.max_reference_seconds:
                raise ServiceError("reference_duration", f"Reference duration must be 1-{self.settings.max_reference_seconds:g} seconds")
            if not 8000 <= info.samplerate <= 96000 or not 1 <= info.channels <= 2:
                raise ServiceError("invalid_audio", "Reference requires 8-96kHz, mono or stereo")
            audio, rate = sf.read(source, dtype="float32", always_2d=True)
            if len(audio) != info.frames or not np.isfinite(audio).all() or float(np.max(np.abs(audio))) < 1e-5:
                raise ServiceError("invalid_audio", "Reference is truncated, non-finite or silent")
            sf.write(processed, audio.mean(axis=1), rate, subtype="PCM_16")
            return {"duration_seconds": info.duration, "sample_rate": rate, "channels": info.channels, "original_sha256": file_hash(source), "processed_sha256": file_hash(processed), "preprocessing_version": REFERENCE_VERSION}
        except (RuntimeError, sf.LibsndfileError, ValueError) as e:
            raise ServiceError("invalid_audio", f"Cannot decode reference WAV: {e}") from e

    def convert_reference(self, source: Path, target: Path):
        """Decode a short local recording with fixed demuxers and a bounded output."""
        formats = {".wav": "wav", ".webm": "matroska", ".ogg": "ogg", ".mp3": "mp3",
                   ".m4a": "mov", ".mp4": "mov", ".flac": "flac"}
        demuxer = formats.get(source.suffix.lower())
        if not demuxer:
            raise ServiceError("invalid_audio", "请选择 WAV、WebM、M4A、MP3、OGG、FLAC 或 MP4 录音。", 422)
        limit = min(30, self.settings.max_reference_seconds)
        if not source.stat().st_size:
            raise ServiceError("invalid_audio", "录音为空，请重新录制或选择文件。", 422)
        if source.stat().st_size > self.settings.max_upload_bytes:
            raise ServiceError("upload_too_large", "录音超过上传大小限制。", 413)
        decoded = target.with_suffix(".decoded.wav")
        try:
            # Decode at most limit+1 seconds so duration-less WebM cannot produce an unbounded WAV.
            # An overlong recording is rejected, never silently cut to match a transcript.
            self.run([self.settings.ffmpeg_bin, "-nostdin", "-v", "error", "-y",
                      "-protocol_whitelist", "file,pipe", "-f", demuxer,
                      "-threads", str(self.settings.ffmpeg_threads), "-i", str(source),
                      "-map", "0:a:0", "-vn", "-t", str(limit + 1),
                      "-threads", str(self.settings.ffmpeg_threads), "-filter_threads", str(self.settings.ffmpeg_threads),
                      "-ac", "1", "-ar", str(self.settings.work_sample_rate), "-c:a", "pcm_s16le", str(decoded)])
            info = sf.info(decoded)
            if not 1 <= info.duration <= limit:
                raise ServiceError("reference_duration", f"录音时长应为 1–{limit:g} 秒，请重新录制或裁剪后上传。", 422)
            return self.validate_reference(decoded, target)
        except ServiceError as exc:
            if exc.code == "ffmpeg_failed":
                raise ServiceError("invalid_audio", "无法解码录音，请重新录制或选择有效音频文件。", 422) from exc
            raise
        except (RuntimeError, sf.LibsndfileError, ValueError) as exc:
            raise ServiceError("invalid_audio", "无法读取录音，请重新录制或选择有效音频文件。", 422) from exc
        finally:
            decoded.unlink(missing_ok=True)

    def process(self, source: Path, target: Path, tempo: float, pitch_semitones: float, gain_db: float):
        if not (0.01 <= tempo <= 100 and -12 <= pitch_semitones <= 12 and -60 <= gain_db <= 12):
            raise ServiceError("invalid_control", "Audio controls exceed safe processing ranges")
        if not all(math.isfinite(x) for x in (tempo, pitch_semitones, gain_db)):
            raise ServiceError("invalid_control", "Audio controls must be finite")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp.wav")
        try:
            with tempfile.TemporaryDirectory(dir=target.parent) as workspace:
                normalized, stretched = Path(workspace) / "normalized.wav", Path(workspace) / "stretched.wav"
                self.run([self.settings.ffmpeg_bin, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(source), "-vn", "-threads", str(self.settings.ffmpeg_threads), "-ac", "1", "-ar", str(self.settings.work_sample_rate), "-c:a", "pcm_f32le", str(normalized)])
                if tempo != 1 or pitch_semitones != 0:
                    # Offline Rubber Band handles start padding, algorithm latency and final buffer drain.
                    # The realtime FFmpeg 5.1 filter does not flush these reliably on short utterances.
                    self.run([self.settings.rubberband_bin, "--quiet", "--fine", "--formant", "--no-threads", "--tempo", f"{tempo:.9f}", "--pitch", f"{pitch_semitones:.9f}", str(normalized), str(stretched)])
                    normalized = stretched
                self.run([self.settings.ffmpeg_bin, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(normalized), "-threads", str(self.settings.ffmpeg_threads), "-filter_threads", str(self.settings.ffmpeg_threads), "-af", f"volume={gain_db:.9f}dB", "-ac", "1", "-ar", str(self.settings.work_sample_rate), "-c:a", "pcm_s16le", str(temp)])
            self.frames(temp)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def encode(self, source: Path, target: Path):
        temp = target.with_suffix(".tmp.mp3")
        try:
            self.run([self.settings.ffmpeg_bin, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(source), "-vn", "-threads", str(self.settings.ffmpeg_threads), "-c:a", "libmp3lame", "-b:a", self.settings.mp3_bitrate, "-write_xing", "1", str(temp)])
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def frames(self, path: Path):
        try:
            with wave.open(str(path), "rb") as f:
                if f.getframerate() != self.settings.work_sample_rate or f.getnchannels() != 1 or f.getsampwidth() != 2:
                    raise ServiceError("invalid_pcm", "Processing output is not the configured mono PCM16 format", 500)
                return f.getnframes()
        except (wave.Error, EOFError) as e:
            raise ServiceError("invalid_pcm", "Cannot read processed WAV", 500) from e

    def encoded_info(self, path: Path):
        probe = json.loads(self.run([self.settings.ffprobe_bin, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_format", "-show_streams", "-of", "json", str(path)]))
        # MP3 container includes codec padding; timeline uses the decoded sample count.
        with tempfile.TemporaryDirectory() as temp:
            decoded = Path(temp) / "decoded.wav"
            self.run([self.settings.ffmpeg_bin, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(path), "-threads", str(self.settings.ffmpeg_threads), "-ac", "1", "-ar", str(self.settings.work_sample_rate), "-c:a", "pcm_s16le", str(decoded)])
            frames = self.frames(decoded)
        stream = probe["streams"][0]
        actual_rate, actual_bitrate = int(stream["sample_rate"]), int(stream.get("bit_rate", 0))
        if stream["codec_name"] != "mp3" or actual_rate != self.settings.work_sample_rate or actual_bitrate != int(self.settings.mp3_bitrate[:-1]) * 1000:
            raise ServiceError("encoder_parameter_mismatch", "MP3 encoder changed sample rate or bitrate; inspect WORK_SAMPLE_RATE/MP3_BITRATE", 500, {"sample_rate": actual_rate, "bit_rate": actual_bitrate})
        return {"codec": stream["codec_name"], "sample_rate": actual_rate, "bit_rate": actual_bitrate, "decoded_frames": frames, "decoded_duration_seconds": frames / self.settings.work_sample_rate, "container_duration_seconds": float(probe["format"]["duration"]), "stream_start_time_seconds": float(stream.get("start_time", 0)), "padding_seconds": float(probe["format"]["duration"]) - frames / self.settings.work_sample_rate}

    def assemble(self, events: list[dict], unit_paths: dict[str, Path], target: Path, check):
        frames = 0
        timeline, bookmarks = [], []
        with wave.open(str(target), "wb") as out:
            out.setparams((1, 2, self.settings.work_sample_rate, 0, "NONE", "not compressed"))
            for event in events:
                check()
                if event["kind"] == "bookmark":
                    bookmarks.append({**event, "frame": frames, "seconds": frames / self.settings.work_sample_rate})
                    continue
                start = frames
                if event["kind"] == "speech":
                    with wave.open(str(unit_paths[event["unit_id"]]), "rb") as f:
                        while block := f.readframes(32768):
                            check()
                            out.writeframesraw(block)
                            frames += len(block) // 2
                else:
                    silence = round(event["pause_ms"] * self.settings.work_sample_rate / 1000)
                    for offset in range(0, silence, 32768):
                        check()
                        chunk = min(32768, silence - offset)
                        out.writeframesraw(b"\0\0" * chunk)
                        frames += chunk
                if frames / self.settings.work_sample_rate > self.settings.max_audio_seconds or frames * 2 > self.settings.max_output_bytes:
                    raise ServiceError("output_limit", "Audio exceeds MAX_AUDIO_SECONDS/MAX_OUTPUT_BYTES; split the narration", 413)
                timeline.append({**event, "start_frame": start, "end_frame": frames, "start_seconds": start / self.settings.work_sample_rate, "end_seconds": frames / self.settings.work_sample_rate})
        return {"sample_rate": self.settings.work_sample_rate, "frames": frames, "duration_seconds": frames / self.settings.work_sample_rate, "events": timeline, "bookmarks": bookmarks, "granularity": "generation-unit-and-explicit-silence", "word_alignment": False}
