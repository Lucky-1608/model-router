"""ElevenLabs native audio protocol adapter (TTS, Streaming Audio, STT, Voice Discovery)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from gateway.capability_registry.models import CapabilityFlag, ProtocolType
from gateway.core.canonical import (
    CanonicalInternalRequest,
    CanonicalInternalResponse,
    CanonicalStreamEvent,
)
from gateway.core.exceptions import UnsupportedCapabilityError
from gateway.protocol_adapters.base import BaseProtocolAdapter


class ElevenLabsProtocolAdapter(BaseProtocolAdapter):
    """Protocol adapter for ElevenLabs TTS, STT, streaming audio, and voice discovery."""

    protocol_type = ProtocolType.ELEVENLABS_TTS

    def parse_request(
        self, payload: Mapping[str, Any], **kwargs: Any
    ) -> CanonicalInternalRequest:
        voice_id = str(kwargs.get("voice_id") or payload.get("voice_id") or "").strip()
        if not voice_id:
            raise UnsupportedCapabilityError("voice_id is required", param="voice_id")

        text_val = str(payload.get("text") or "").strip()
        if not text_val:
            raise UnsupportedCapabilityError(
                "Field 'text' is required for ElevenLabs TTS", param="text"
            )

        stream = bool(kwargs.get("stream", False))
        model_id = str(payload.get("model_id") or "eleven_multilingual_v2")
        required_caps: set[CapabilityFlag] = {CapabilityFlag.TTS}
        if stream:
            required_caps.add(CapabilityFlag.STREAMING)

        provider_ext: dict[str, Any] = {"voice_id": voice_id}
        if "voice_settings" in payload:
            provider_ext["voice_settings"] = payload["voice_settings"]
        if kwargs.get("output_format"):
            provider_ext["output_format"] = kwargs["output_format"]

        return CanonicalInternalRequest(
            source_protocol=ProtocolType.ELEVENLABS_TTS,
            requested_model=f"elevenlabs/{model_id}",
            provider_model_id=model_id,
            voice_id=voice_id,
            input_texts=[text_val],
            stream=stream,
            required_capabilities=required_caps,
            provider_extensions=provider_ext,
        )

    def parse_stt_request(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        model_id: str = "scribe_v1",
    ) -> CanonicalInternalRequest:
        if not audio_bytes:
            raise UnsupportedCapabilityError("Audio file bytes cannot be empty", param="file")
        return CanonicalInternalRequest(
            source_protocol=ProtocolType.ELEVENLABS_STT,
            requested_model=f"elevenlabs/{model_id}",
            provider_model_id=model_id,
            audio_bytes=audio_bytes,
            audio_filename=filename,
            required_capabilities={CapabilityFlag.STT},
        )

    def format_response(
        self, response: CanonicalInternalResponse
    ) -> dict[str, Any]:
        return {
            "text": response.output_text,
            "model_id": response.model,
            "characters": response.usage.characters,
        }

    async def format_stream_events(
        self, events: AsyncIterator[CanonicalStreamEvent], model: str
    ) -> AsyncIterator[bytes]:
        async for ev in events:
            if ev.event_type == "audio_chunk" and ev.audio_bytes:
                yield ev.audio_bytes

