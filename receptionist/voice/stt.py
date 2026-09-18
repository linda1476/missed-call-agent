"""Speech-to-text backends.

- WhisperSTT: offline faster-whisper, used by acceptance tests and CI so no
  API key is needed. Supports `hotwords` biasing (used by the P0-7 keyterm
  experiment as the offline analogue of AssemblyAI keyterms prompting).
- TranscriptSTT: passthrough for text-fed simulation (scenario replays).
- The production path is the Voice Agent API WebSocket (session.py), which
  does STT+LLM+TTS server-side; no local STT runs there.
"""


class TranscriptSTT:
    """Feeds pre-written utterances — deterministic call simulation."""

    def __init__(self, utterances: list[str]):
        self._utterances = list(utterances)

    def transcribe(self, path: str | None = None, hotwords=None) -> str:
        return "\n".join(self._utterances)

    def transcribe_turns(self, path: str | None = None) -> list[str]:
        return list(self._utterances)


class WhisperSTT:
    """faster-whisper wrapper. Model is lazy-loaded on first use."""

    def __init__(self, model_size: str = "tiny.en"):
        self.model_size = model_size
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_size, device="cpu",
                                       compute_type="int8")
        return self._model

    def transcribe(self, path: str, hotwords: list[str] | None = None) -> str:
        model = self._load()
        kwargs = {"beam_size": 5, "language": "en"}
        if hotwords:
            kwargs["hotwords"] = ", ".join(hotwords)
        segments, _ = model.transcribe(path, **kwargs)
        return " ".join(s.text.strip() for s in segments).strip()
