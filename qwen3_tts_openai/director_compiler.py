"""Compile the inspected build-narrated-presentation contract and a strict SSML subset.

No model calls, text rewriting, network access or invented word timestamps here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from qwen3_tts_openai.config import Settings
from qwen3_tts_openai.contracts import NarrationRequest
from qwen3_tts_openai.errors import ServiceError
from qwen3_tts_openai.storage import digest

COMPILER_VERSION = "director-v1"
SSML_NS = "http://www.w3.org/2001/10/synthesis"
MSTTS_NS = "https://www.w3.org/2001/mstts"
INTENTS = {"opening", "context", "explanation", "comparison", "evidence", "case-study", "catalog", "conclusion", "closing"}
STYLES = {"cheerful": "用愉快的语气说话。", "sad": "用悲伤的语气说话。", "angry": "用愤怒的语气说话。", "calm": "用平静的语气说话。", "empathetic": "用关切、体贴的语气说话。", "excited": "用兴奋的语气说话。", "friendly": "用友好的语气说话。"}
NUMERIC_DIRECTION = re.compile(r"语速|音高|音量|停顿|放慢|提速|降速|慢速|快速|快点|慢点|高音|低音|rate|pitch|volume|tempo|speed|pause|slower|faster|louder|quieter", re.I)


@dataclass
class Compilation:
    settings: Settings
    request: NarrationRequest
    capabilities: dict
    resolve_voice: Callable[[str], dict]
    issues: list = field(default_factory=list)
    units: list = field(default_factory=list)
    events: list = field(default_factory=list)
    pages: list = field(default_factory=list)
    source_texts: list = field(default_factory=list)
    ssml_language: str | None = None

    def report(self, code, message, location, *, degradable=True, details=None):
        self.issues.append({"code": code, "message": message, "location": location, "severity": "warning" if degradable and self.request.allow_degraded else "error", "degraded": bool(degradable and self.request.allow_degraded), "details": details})

    def require(self, value, message, location):
        if not value:
            self.report("invalid_input", message, location, degradable=False)

    def unknown(self, obj, allowed, location):
        for key in sorted(set(obj) - set(allowed)):
            self.report("unknown_control", f"Unsupported field {key!r}; it is preserved in source input", f"{location}.{key}")

    def number(self, value, unit, location, *, signed=False, integer=False):
        expression = (r"[+-]" if signed else r"[+-]?") + (r"\d+" if integer else r"\d+(?:\.\d+)?") + re.escape(unit)
        if not isinstance(value, str) or not re.fullmatch(expression, value):
            self.report("invalid_control", f"Expected {'signed ' if signed else ''}numeric {unit} value", location, degradable=False)
            return 0.0
        return float(value[: -len(unit)])

    def pause(self, value, maximum, location):
        if type(value) is not int or not 0 <= value <= maximum:
            self.report("invalid_control", f"Pause must be an integer from 0 to {maximum} ms", location, degradable=False)
            return 0
        return value

    def voice(self, value, location):
        value = self.request.voice_mapping.get(value, value) if value else None
        if not value:
            self.report("voice_mapping_required", "Provide registered voice_id or an explicit voice_mapping for the legacy voice name", location, degradable=False)
            return {"voice_id": "unresolved"}
        try:
            voice = self.resolve_voice(value)
            if self.settings.backend == "qwen-combined-experimental" and voice.get("mode") == "icl":
                self.report("reference_mode_unsupported", "Combined experiment uses x_vector_only, while this voice was registered in ICL mode. Explicit degradation uses embedding only; desired and actual modes remain recorded", location)
            return voice
        except ServiceError as e:
            self.report(e.code, e.message, location, degradable=False)
            return {"voice_id": value}

    def instruction(self, direction, style, location):
        desired = direction or ""
        style_instruction = ""
        if style:
            if style in STYLES:
                style_instruction = STYLES[style]
            else:
                self.report("unsupported_style", f"Cannot map express-as style {style!r}", location)
        effective = " ".join(x for x in (style_instruction, desired) if x)
        if not effective:
            return {"desired": desired, "style": style, "effective": "", "owner": "none"}
        if NUMERIC_DIRECTION.search(desired):
            self.report("instruction_numeric_conflict", "Free-text direction contains timing/pitch/gain requests. Numeric controls belong to postprocessing; with allow_degraded the whole direction is omitted, retaining only mapped style", location)
            effective = style_instruction
        capability = self.capabilities["instruction_control"]
        if capability == "unsupported":
            self.report("instruction_unsupported", "Selected backend cannot execute expression instructions. Explicit allow_degraded runs clone plus numeric controls and records the omitted direction/style", location)
            effective = ""
        if capability == "experimental" and effective and not self.request.allow_experimental:
            self.report("experimental_opt_in", "Reference voice plus expression is unvalidated; set allow_experimental=true to produce listening-test artifacts", location, degradable=False)
        return {"desired": desired, "style": style, "effective": effective, "owner": "model" if effective else "omitted", "scope": "generation-unit"}

    def pronunciations(self, text, rules, location):
        if not rules:
            return text, []
        valid = {}
        for term, raw in rules.items():
            if not isinstance(term, str) or not term:
                self.report("invalid_pronunciation", "Pronunciation keys must be non-empty strings", location, degradable=False)
                continue
            rule = {"alias": raw} if isinstance(raw, str) else raw
            if not isinstance(rule, dict):
                self.report("invalid_pronunciation", "Pronunciation rules must be strings or objects", location, degradable=False)
                continue
            valid[term] = rule
        def expression(term):
            return (r"(?<![A-Za-z0-9])" if re.match(r"[A-Za-z0-9]", term[0]) else "") + re.escape(term) + (r"(?![A-Za-z0-9])" if re.match(r"[A-Za-z0-9]", term[-1]) else "")
        if not valid:
            return text, []
        matcher = re.compile("|".join(expression(t) for t in sorted(valid, key=len, reverse=True)))
        parts, mapping, cursor = [], [], 0
        for match in matcher.finditer(text):
            term, rule = match.group(), valid[match.group()]
            self.unknown(rule, {"alias", "say_as", "interpret_as", "format", "phoneme", "ph", "alphabet"}, f"{location}.{term}")
            spoken = term
            if "alias" in rule:
                if not isinstance(rule["alias"], str) or not rule["alias"].strip():
                    self.report("invalid_pronunciation", "Alias must be non-empty text", f"{location}.{term}", degradable=False)
                else:
                    spoken = rule["alias"]
            elif "phoneme" in rule or "ph" in rule:
                self.report("phoneme_unsupported", "Phoneme input is not supported; use a confirmed alias. Explicit degradation reads original text", f"{location}.{term}")
            elif rule.get("say_as", rule.get("interpret_as")) in {"characters", "spell-out", "alphanumeric", "number_digit"} and "format" not in rule:
                if not re.fullmatch(r"[A-Za-z0-9]+", term):
                    self.report("say_as_unsupported", "Character spelling is implemented only for ASCII letters/digits", f"{location}.{term}")
                else:
                    spoken = " ".join("零一二三四五六七八九"[int(c)] if c.isdigit() else c.upper() for c in term)
            else:
                self.report("say_as_unsupported", "Only ASCII characters/spell-out/alphanumeric/number_digit without format are implemented; use a confirmed alias for semantic numbers", f"{location}.{term}")
            parts.extend([text[cursor:match.start()], spoken])
            mapping.append({"original": term, "spoken": spoken, "source_start": match.start(), "source_end": match.end(), "rule": rule})
            cursor = match.end()
        parts.append(text[cursor:])
        return "".join(parts), mapping

    def split(self, text, location):
        # Keep complete natural sentences/short paragraphs, preserving every character.
        limit = self.settings.max_unit_chars
        if len(text) <= limit:
            return [(0, text)]
        pieces = re.split(r"(?<=[。！？!?；;\n])", text)
        chunks, current, offset = [], "", 0
        for piece in pieces:
            if len(piece) > limit:
                # Clause punctuation is a legitimate fallback; never split by character count.
                clauses = re.split(r"(?<=[，,：:])", piece)
            else:
                clauses = [piece]
            for clause in clauses:
                if len(clause) > limit:
                    self.report("unit_too_long", f"A sentence/clause exceeds MAX_UNIT_CHARS={limit}; add a natural sentence boundary", location, degradable=False)
                if current and len(current) + len(clause) > limit:
                    chunks.append((offset, current))
                    offset += len(current)
                    current = ""
                current += clause
        if current:
            chunks.append((offset, current))
        return chunks

    def add(self, text, page, segment, chapter, voice, controls, instruction, rules, location, *, spoken_override=None, mappings=None):
        if not isinstance(text, str) or not text.strip():
            self.report("empty_text", "Speech text must contain readable content", location, degradable=False)
            return
        self.source_texts.append({"page": page, "segment": segment, "text": text, "sha256": digest(text)})
        for offset, chunk in self.split(text, location):
            spoken, replacement = self.pronunciations(chunk, rules, "voice_profile.pronunciations")
            if spoken_override is not None:
                # Split at source sentence boundaries, projecting explicit alias spans into each unit.
                spans = [m for m in mappings or [] if m["source_start"] < offset + len(chunk) and m["source_end"] > offset]
                parts, replacement, cursor = [], [], 0
                for mapping in spans:
                    start, end = mapping["source_start"] - offset, mapping["source_end"] - offset
                    if start < 0 or end > len(chunk):
                        self.report("ssml_alias_scope", "A sub/say-as span crosses a generation boundary; keep that alias within a natural sentence", location, degradable=False)
                        continue
                    parts.extend([chunk[cursor:start], mapping["spoken"]])
                    replacement.append({**mapping, "source_start": start, "source_end": end})
                    cursor = end
                parts.append(chunk[cursor:])
                spoken = "".join(parts)
            if len(spoken) > self.settings.max_unit_chars * 3:
                self.report("spoken_text_limit", "Pronunciation expansion is too large for one generation unit", location, degradable=False)
            unit_id = f"unit-{len(self.units) + 1:04d}"
            self.units.append({"unit_id": unit_id, "page": page, "segment": segment, "chapter": chapter, "source_start": offset, "source_end": offset + len(chunk), "text": chunk, "spoken_text": spoken, "pronunciation_mapping": replacement, "voice_id": voice["voice_id"], "voice_snapshot": voice, "model_instruction": instruction, "postprocessing": controls, "capability_requirements": {"voice_clone": not voice.get("preset"), "instruction_control": bool(instruction["effective"]), "combined_clone_instruction": bool(instruction["effective"] and not voice.get("preset")), "control_granularity": "generation-unit"}, "control_owners": {"timbre": "model", "expression": instruction["owner"], "tempo": "rubberband-offline", "pitch_semitones": "rubberband-offline-formant-preserved", "gain_db": "ffmpeg-volume", "pause_ms": "timeline-extra-silence"}})
            self.events.append({"kind": "speech", "unit_id": unit_id, "page": page, "segment": segment})

    def silence(self, pause, page, reason):
        if pause:
            self.events.append({"kind": "silence", "pause_ms": pause, "page": page, "reason": reason})

    def director(self):
        director, profile = self.request.narration_director, self.request.voice_profile
        self.unknown(director, {"schema_version", "policy", "pages"}, "narration_director")
        self.require(director.get("schema_version") in {1, 2}, "Director schema_version must be 1 or 2", "narration_director.schema_version")
        self.unknown(profile, {"schema_version", "provider", "voice", "voice_id", "style", "rate", "pitch", "page_break_ms", "pronunciations", "audition"}, "voice_profile")
        if profile.get("schema_version", 1) != 1:
            self.report("schema_version", "voice_profile schema_version must be 1", "voice_profile", degradable=False)
        policy = director.get("policy", {})
        if not isinstance(policy, dict):
            self.report("invalid_input", "policy must be an object", "policy", degradable=False)
            return
        self.unknown(policy, {"visual_sync", "audio_start_ms", "paragraph_per_slide", "animation_window_ms", "chapter_max_seconds", "performance_contract"}, "policy")
        if self.request.compatibility == "build-narrated-presentation":
            self.require(policy.get("visual_sync") == "independent", "policy.visual_sync must be independent", "policy.visual_sync")
            self.require(policy.get("performance_contract") == "rhetorical-v1", "Expected performance_contract rhetorical-v1", "policy.performance_contract")
        global_rate = self.number(profile.get("rate", "+0%"), "%", "voice_profile.rate", signed=True, integer=True)
        global_pitch = self.number(profile.get("pitch", "+0st"), "st", "voice_profile.pitch", signed=True)
        limit = 0.1 if self.request.compatibility == "build-narrated-presentation" else 12
        self.require(abs(global_pitch) <= limit + 1e-6, f"Global pitch must be within ±{limit}st", "voice_profile.pitch")
        page_pause = self.pause(profile.get("page_break_ms", 120), 1000 if limit == 0.1 else 10000, "voice_profile.page_break_ms")
        rules = profile.get("pronunciations", {})
        if not isinstance(rules, dict):
            self.report("invalid_pronunciation", "pronunciations must be an object", "voice_profile.pronunciations", degradable=False)
            rules = {}
        raw_pages = director.get("pages", [])
        if not isinstance(raw_pages, list) or not raw_pages:
            self.report("invalid_input", "pages must be a non-empty array", "pages", degradable=False)
            return
        seen, closed_chapters, current_chapter = set(), set(), None
        for position, row in enumerate(raw_pages):
            location = f"pages[{position}]"
            if not isinstance(row, dict):
                self.report("invalid_input", "page must be an object", location, degradable=False)
                continue
            self.unknown(row, {"page", "chapter", "role", "intent", "direction", "rationale", "target_seconds", "segments", "voice_id"}, location)
            page = row.get("page")
            if type(page) is not int or page <= 0 or page in seen:
                self.report("invalid_input", "Page numbers must be positive and unique", location, degradable=False)
                continue
            seen.add(page)
            if self.request.compatibility == "build-narrated-presentation":
                self.require(page == position + 1, "Original director pages must be consecutive and ordered from 1", location)
            chapter = row.get("chapter", f"page-{page:02d}")
            self.require(isinstance(chapter, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", chapter), "Invalid chapter identifier", location)
            if chapter != current_chapter:
                if current_chapter:
                    closed_chapters.add(current_chapter)
                self.require(chapter not in closed_chapters, "Chapter pages must be contiguous", location)
                current_chapter = chapter
            target = row.get("target_seconds")
            self.require(target is None or type(target) is int and 1 <= target <= 3600, "target_seconds must be 1-3600 or null", location)
            for key in ("role", "direction", "rationale"):
                self.require(isinstance(row.get(key), str) and row[key].strip(), f"{key} must be non-empty text", location)
            self.require(row.get("intent") in INTENTS, "Unknown intent", location)
            instruction = self.instruction(row.get("direction") if isinstance(row.get("direction"), str) else "", profile.get("style"), location + ".direction")
            metadata = {k: row.get(k) for k in ("page", "chapter", "role", "intent", "direction", "rationale", "target_seconds")}
            metadata["chapter"] = chapter
            self.pages.append(metadata)
            self.events.append({"kind": "bookmark", "mark": f"page-{page:02d}", "page": page})
            segments = row.get("segments")
            if not isinstance(segments, list) or not segments:
                self.report("invalid_input", "segments must be a non-empty array", location, degradable=False)
                continue
            for index, segment in enumerate(segments, 1):
                loc = f"{location}.segments[{index-1}]"
                if not isinstance(segment, dict):
                    self.report("invalid_input", "segment must be an object", loc, degradable=False)
                    continue
                self.unknown(segment, {"text", "rate", "pitch", "pause_after_ms", "voice_id", "gain_db", "instructions"}, loc)
                rate = self.number(segment.get("rate", "+5%"), "%", loc + ".rate", signed=True, integer=True)
                pitch = self.number(segment.get("pitch", "+0st"), "st", loc + ".pitch", signed=True)
                final_pitch, tempo = global_pitch + pitch, 1 + (global_rate + rate) / 100
                self.require(abs(pitch) <= limit + 1e-6 and abs(final_pitch) <= limit + 1e-6, f"Local and combined pitch must be within ±{limit}st; never clamped", loc + ".pitch")
                self.require(0.01 <= tempo <= 100, "Combined rate must be > -100%, with tempo in 0.01-100", loc + ".rate")
                gain = segment.get("gain_db", 0)
                self.require(type(gain) in {float, int} and math.isfinite(gain) and -60 <= gain <= 12, "gain_db must be finite and in -60..12", loc + ".gain_db")
                pause = self.pause(segment.get("pause_after_ms", 80), 300 if limit == 0.1 else 10000, loc + ".pause_after_ms")
                voice = self.voice(segment.get("voice_id") or row.get("voice_id") or self.request.voice_id or profile.get("voice_id") or profile.get("voice"), loc + ".voice_id")
                segment_instruction = self.instruction(segment["instructions"], profile.get("style"), loc + ".instructions") if isinstance(segment.get("instructions"), str) else instruction
                if "instructions" in segment and not isinstance(segment["instructions"], str):
                    self.report("invalid_input", "instructions must be text", loc, degradable=False)
                self.add(segment.get("text"), page, index, chapter, voice, {"tempo": tempo, "pitch_semitones": final_pitch, "gain_db": gain, "combined_rate_percent": global_rate + rate, "global_rate_percent": global_rate, "local_rate_percent": rate, "global_pitch_semitones": global_pitch, "local_pitch_semitones": pitch}, segment_instruction, rules, loc)
                self.silence(pause, page, "segment-pause-extra")
            self.silence(page_pause, page, "page-break-extra-including-final-page")

    def plain(self):
        profile = self.request.voice_profile
        self.unknown(profile, {"instructions"}, "voice_profile")
        voice = self.voice(self.request.voice_id, "voice_id")
        instruction = self.instruction(profile.get("instructions", ""), None, "instructions")
        self.pages.append({"page": 1, "chapter": "plain", "target_seconds": None})
        self.events.append({"kind": "bookmark", "mark": "page-01", "page": 1})
        self.add(self.request.text, 1, 1, "plain", voice, {"tempo": self.request.speed or 1, "pitch_semitones": 0, "gain_db": 0}, instruction, {}, "text")

    def ssml(self):
        if self.request.voice_profile:
            self.report("ssml_profile_conflict", "SSML controls are self-contained; use voice_mapping instead of also applying voice_profile", "voice_profile", degradable=False)
        try:
            root = ElementTree.fromstring(self.request.ssml, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        except (ElementTree.ParseError, DefusedXmlException) as e:
            self.report("unsafe_or_invalid_xml", str(e), "ssml", degradable=False)
            return
        def name(tag):
            return tag[1:].split("}", 1) if tag.startswith("{") else ("", tag)
        if name(root.tag)[1] != "speak":
            self.report("invalid_ssml", "SSML root must be speak", "ssml", degradable=False)
            return
        locale = root.get("{http://www.w3.org/XML/1998/namespace}lang")
        if locale is not None:
            languages = {"zh":"Chinese", "en":"English", "ja":"Japanese", "ko":"Korean", "de":"German", "fr":"French", "ru":"Russian", "pt":"Portuguese", "es":"Spanish", "it":"Italian"}
            mapped = languages.get(locale.split("-",1)[0].lower())
            if not mapped:
                self.report("language_unsupported", "Unsupported SSML xml:lang; explicit degradation retains the request language", "ssml.speak.xml:lang")
            elif "language" in self.request.model_fields_set and self.request.language != mapped:
                self.report("language_conflict", "SSML xml:lang conflicts with the explicit request language; explicit degradation retains request language", "ssml.speak.xml:lang")
            else:
                self.ssml_language = mapped
        page, segment, pending = 1, 0, None
        marks = set()
        default = {"voice_name": self.request.voice_id, "tempo": 1.0, "pitch_semitones": 0.0, "gain_db": 0.0, "style": None}
        def flush():
            nonlocal pending, segment
            if pending and pending["text"].strip():
                segment += 1
                ctx = pending["ctx"]
                instruction = self.instruction("", ctx["style"], "ssml.express-as")
                self.add(pending["text"], page, segment, "ssml", self.voice(ctx["voice_name"], "ssml.voice"), {k: ctx[k] for k in ("tempo", "pitch_semitones", "gain_db")}, instruction, {}, "ssml", spoken_override=pending["spoken"] if pending["mapping"] else None, mappings=pending["mapping"])
            pending = None
        def append(text, ctx, spoken=None, mapping=None):
            nonlocal pending
            if not text:
                return
            if pending and pending["ctx"] != ctx:
                if pending["text"].strip() and text.strip() and not re.search(r"[。！？!?；;\n]\s*$", pending["text"]):
                    self.report("inline_alignment_unsupported", "Changing controls/voice inside a sentence requires alignment. Move the control to a full sentence or use an explicit break", "ssml", degradable=False)
                flush()
            if pending is None:
                pending = {"text": "", "spoken": "", "ctx": ctx.copy(), "mapping": []}
            start = len(pending["text"])
            pending["text"] += text
            pending["spoken"] += text if spoken is None else spoken
            if mapping:
                pending["mapping"].append({**mapping, "source_start": start, "source_end": start + len(text)})
        def walk(node, inherited, depth=0):
            nonlocal page, segment
            if depth > 32:
                self.report("ssml_depth", "SSML nesting exceeds 32", "ssml", degradable=False)
                return
            namespace, tag = name(node.tag)
            ctx = inherited.copy()
            if namespace not in {"", SSML_NS, MSTTS_NS} or namespace == MSTTS_NS and tag != "express-as" or tag == "express-as" and namespace != MSTTS_NS:
                self.report("ssml_namespace", f"Unsupported namespace for {tag}", "ssml", degradable=False)
                return
            allowed = {"speak": {"version", "{http://www.w3.org/XML/1998/namespace}lang"}, "voice": {"name"}, "p": set(), "s": set(), "prosody": {"rate", "pitch", "volume"}, "break": {"time"}, "bookmark": {"mark"}, "sub": {"alias"}, "say-as": {"interpret-as"}, "express-as": {"style", "styledegree"}}
            if tag not in allowed:
                self.report("ssml_element_unsupported", f"SSML element {tag!r} is unimplemented", "ssml", degradable=tag not in {"audio", "lexicon", "speak"})
            self.unknown(node.attrib, allowed.get(tag, set()), f"ssml.{tag}")
            if tag in {"p", "s"}:
                flush()
            if tag == "voice":
                ctx["voice_name"] = node.get("name")
            elif tag == "prosody":
                if "rate" in node.attrib:
                    v = node.get("rate")
                    number = self.number(v, "%", "ssml.prosody.rate")
                    ctx["tempo"] = ctx["tempo"] * (1 + number / 100) if v.startswith(("+", "-")) else number / 100
                if "pitch" in node.attrib:
                    v = node.get("pitch")
                    pitch = self.number(v, "st", "ssml.prosody.pitch")
                    ctx["pitch_semitones"] = ctx["pitch_semitones"] + pitch if v.startswith(("+", "-")) else pitch
                if "volume" in node.attrib:
                    v = node.get("volume")
                    gain = self.number(v, "dB", "ssml.prosody.volume")
                    ctx["gain_db"] = ctx["gain_db"] + gain if v.startswith(("+", "-")) else gain
                self.require(0.01 <= ctx["tempo"] <= 100 and -12 <= ctx["pitch_semitones"] <= 12 and -60 <= ctx["gain_db"] <= 12, "SSML numeric controls exceed processing ranges", "ssml.prosody")
            elif tag == "express-as":
                ctx["style"] = node.get("style")
                if node.get("styledegree", "1.0") not in {"1", "1.0"}:
                    self.report("styledegree_unsupported", "Only mapped styledegree=1 is implemented", "ssml.express-as")
            elif tag == "break":
                flush()
                value = node.get("time", "")
                ms = self.number(value, "ms", "ssml.break") if value.endswith("ms") else self.number(value, "s", "ssml.break") * 1000
                self.require(0 <= ms <= 10000, "SSML break must be 0-10000ms", "ssml.break")
                self.silence(round(ms), page, "ssml-break-extra")
                return
            elif tag == "bookmark":
                if pending and pending["text"].strip() and not re.search(r"[。！？!?；;\n]\s*$", pending["text"]):
                    self.report("inline_bookmark_unsupported", "A sentence-internal bookmark needs alignment; put it at a sentence/paragraph boundary", "ssml.bookmark", degradable=False)
                flush()
                mark = node.get("mark", "")
                self.require(bool(mark) and len(mark) <= 100 and mark not in marks, "Bookmark names must be non-empty and unique", "ssml.bookmark")
                marks.add(mark)
                match = re.fullmatch(r"page-(\d+)", mark)
                if match:
                    page, segment = int(match[1]), 0
                    self.require(page > 0, "Bookmark page must be positive", "ssml.bookmark")
                    self.require(page not in {u["page"] for u in self.units}, "Page bookmarks cannot reopen an earlier page", "ssml.bookmark")
                self.events.append({"kind": "bookmark", "mark": mark, "page": page})
                return
            elif tag in {"sub", "say-as"}:
                text = "".join(node.itertext())
                self.require(not list(node), "sub/say-as cannot contain nested controls", "ssml." + tag)
                if tag == "sub":
                    spoken = node.get("alias", "")
                    self.require(bool(spoken.strip()), "sub needs a non-empty alias", "ssml.sub")
                else:
                    spoken, _ = self.pronunciations(text, {text: {"say_as": node.get("interpret-as")}}, "ssml.say-as") if text else (text, [])
                append(text, ctx, spoken, {"original": text, "spoken": spoken, "rule": node.attrib})
                return
            append(node.text, ctx)
            for child in node:
                if name(child.tag)[1] == "speak":
                    self.report("invalid_ssml", "speak cannot be nested", "ssml", degradable=False)
                    continue
                walk(child, ctx, depth + 1)
                append(child.tail, ctx)
            if tag in {"p", "s"}:
                flush()
        walk(root, default)
        flush()
        page_order = list(dict.fromkeys(u["page"] for u in self.units))
        self.pages = [{"page": p, "chapter": "ssml", "target_seconds": None} for p in page_order]

    def compile(self):
        if self.request.narration_director is not None:
            self.director()
        elif self.request.ssml is not None:
            if len(self.request.ssml) > self.settings.max_text_chars * 10:
                self.report("text_limit", "SSML exceeds markup size limit", "ssml", degradable=False)
            else:
                self.ssml()
        else:
            self.plain()
        text_length = sum(len(row["text"]) for row in self.source_texts)
        if not self.units or text_length > self.settings.max_text_chars:
            self.report("text_limit", f"Provide 1-{self.settings.max_text_chars} text characters; no silent truncation", "source", degradable=False)
        if self.capabilities["combined_clone_instruction"] == "experimental" and not self.request.allow_experimental:
            self.report("experimental_opt_in", "Combined backend requires allow_experimental=true, including neutral comparison runs", "backend", degradable=False)
        if self.request.selection:
            wanted_pages = set(self.request.selection["pages"])
            wanted_segments = set(self.request.selection.get("segments", []))
            present_pages = {u["page"] for u in self.units}
            self.require(wanted_pages <= present_pages, "Selected page does not exist", "selection.pages")
            if wanted_segments:
                for p in wanted_pages:
                    self.require(wanted_segments <= {u["segment"] for u in self.units if u["page"] == p}, "Selected segment does not exist on each selected page", "selection.segments")
            self.units = [u for u in self.units if u["page"] in wanted_pages and (not wanted_segments or u["segment"] in wanted_segments)]
            ids = {u["unit_id"] for u in self.units}
            self.events = [e for e in self.events if e["page"] in wanted_pages and (e["kind"] != "speech" or e["unit_id"] in ids)]
            # A segment audition excludes page and unselected segment pauses; preserve selected segment pause only below.
            if wanted_segments:
                self.events = [e for e in self.events if e["kind"] != "silence"]
            self.pages = [p for p in self.pages if p["page"] in wanted_pages]
        return {"version": COMPILER_VERSION, "valid": not any(i["severity"] == "error" for i in self.issues), "source_hash": digest(self.request.model_dump()), "source_texts": self.source_texts, "text_hash": digest(self.source_texts), "units": self.units, "events": self.events, "pages": self.pages, "issues": self.issues, "capabilities": self.capabilities, "compatibility": self.request.compatibility, "generation": self.request.generation.model_dump(), "language": self.ssml_language or self.request.language, "rules": {"director_rate": "global_percent + local_percent, tempo=1+sum/100", "director_pitch": "global_semitones + local_semitones; no clamp", "ssml_rate": "signed percentages relative to inherited tempo; unsigned percentage replaces tempo", "ssml_pitch_volume": "signed st/dB add to inherited value; unsigned replaces", "ssml_language": "root xml:lang maps to model language; explicit request conflicts require opt-in degradation", "pause": "extra silence, including final page break", "target_seconds": "reference only", "generation": "natural sentence/short paragraph, no continuous chapter synthesis", "direction": "expression requirement, numeric free-text conflicts require explicit omission", "policy": "visual timing fields preserved as metadata; never read aloud", "selection": "audition uses selected source positions; whole-page pauses retained, single-segment pauses omitted"}}


def compile_narration(request: NarrationRequest, settings: Settings, capabilities: dict, resolve_voice):
    return Compilation(settings, request, capabilities, resolve_voice).compile()
