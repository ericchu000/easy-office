"""Model-service adapters used by the offline parsing pipeline.

These adapters intentionally use only the standard library HTTP client so the
offline core does not grow an HTTP dependency. They are synchronous: the
offline pipeline already runs parser work in a worker thread/process, and
Step 5's multiprocessing design batches calls from one owner process.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Sequence

from easy_office.config.models import EmbeddingConfig, ServiceMode, VLMConfig


class ModelServiceError(RuntimeError):
    """Base class for model-service failures."""


class EmbeddingError(ModelServiceError):
    """Raised when an embedding call fails or returns an invalid payload."""


class VisionError(ModelServiceError):
    """Raised when a VLM call fails or returns an unparseable payload."""


def _api_key(value: Any) -> str:
    try:
        return value.get_secret_value()
    except AttributeError:
        return str(value or "")


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _post_json(url: str, payload: dict, *, timeout: float, api_key: str = "") -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers=_headers(api_key), method="POST"
    )
    deadline = time.monotonic() + float(timeout)
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            chunks: list[bytes] = []
            while True:
                if time.monotonic() > deadline:
                    raise ModelServiceError(
                        f"request exceeded the {timeout}s deadline: {url}"
                    )
                chunk = response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            body = b"".join(chunks)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
        except Exception:  # pragma: no cover - defensive
            detail = ""
        raise ModelServiceError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelServiceError(f"cannot reach {url}: {exc}") from exc
    try:
        parsed = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise ModelServiceError(f"service returned invalid JSON from {url}") from exc
    if not isinstance(parsed, dict):
        raise ModelServiceError(f"service returned a non-object JSON payload from {url}")
    return parsed


def _join_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _openai_base(base_url: str, provider: ServiceMode) -> str:
    base = base_url.rstrip("/")
    if provider == ServiceMode.OLLAMA and not base.endswith("/v1"):
        base += "/v1"
    return base


class EmbeddingClient:
    """OpenAI-compatible ``/embeddings`` client with dimension enforcement."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self._config = config
        self._url = _join_url(
            _openai_base(config.base_url, config.provider), "embeddings"
        )

    @property
    def dimension(self) -> int:
        return int(self._config.embedding_dim)

    @property
    def model_name(self) -> str:
        return self._config.model_name

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        items = [str(text or "") for text in texts]
        if not items:
            return []
        vectors: list[list[float]] = []
        batch_size = max(1, int(self._config.batch_size))
        for start in range(0, len(items), batch_size):
            vectors.extend(self._embed_batch(items[start : start + batch_size]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        vectors = self.embed_texts([text])
        return vectors[0]

    def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        payload = {"model": self._config.model_name, "input": list(batch)}
        try:
            parsed = _post_json(
                self._url,
                payload,
                timeout=float(self._config.timeout_seconds),
                api_key=_api_key(self._config.api_key),
            )
        except ModelServiceError as exc:
            # Several providers (e.g. DashScope) cap the number of inputs per
            # request below common client-side batch sizes. Split and retry
            # instead of forcing every deployment to know that limit.
            message = str(exc).lower()
            if len(batch) > 1 and "batch size" in message and (
                "larger than" in message or "invalid" in message
            ):
                midpoint = len(batch) // 2
                return self._embed_batch(batch[:midpoint]) + self._embed_batch(
                    batch[midpoint:]
                )
            raise
        data = parsed.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbeddingError(
                f"embedding service returned {len(data) if isinstance(data, list) else 'no'} "
                f"vectors for {len(batch)} inputs"
            )
        ordered = sorted(
            enumerate(data), key=lambda item: int(item[1].get("index", item[0]))
        )
        result: list[list[float]] = []
        for _, entry in ordered:
            embedding = entry.get("embedding") if isinstance(entry, dict) else None
            if not isinstance(embedding, list):
                raise EmbeddingError("embedding service returned a malformed vector")
            vector = [float(value) for value in embedding]
            if len(vector) != self.dimension:
                raise EmbeddingError(
                    f"embedding dimension mismatch: service returned {len(vector)}, "
                    f"configuration expects {self.dimension}"
                )
            result.append(vector)
        return result


_VISION_SYSTEM_PROMPT = """You analyse images for a local office knowledge base.
Return ONLY a JSON object (no markdown fences) with exactly these keys:
- scene_category: short Chinese label such as 人物肖像照/数据统计图/架构流程图/票据单据/会议白板/自然风光/截图/其他
- background_color: short Chinese label for the dominant background (e.g. 白色/蓝色/复杂自然背景)
- subject_and_portrait: concise description of the main subject and any visible people
- visual_tags: array of 5-15 short open-domain tags in Chinese
- detailed_caption: a factual natural-language description of the image content
If the image contains text, charts or numbers, transcribe them in detailed_caption.
"""


@dataclass
class VisionDescription:
    scene_category: str = ""
    background_color: str = ""
    subject_and_portrait: str = ""
    visual_tags: list[str] = field(default_factory=list)
    detailed_caption: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        return {
            "scene_category": self.scene_category,
            "background_color": self.background_color,
            "subject_and_portrait": self.subject_and_portrait,
            "visual_tags": list(self.visual_tags),
            "detailed_caption": self.detailed_caption,
        }


def _strip_json_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    return cleaned.strip()


class VisionClient:
    """OpenAI-compatible vision chat client (works with Ollama's ``/v1`` API)."""

    def __init__(self, config: VLMConfig) -> None:
        self._config = config
        self._url = _join_url(
            _openai_base(config.base_url, config.provider), "chat/completions"
        )

    @property
    def model_name(self) -> str:
        return self._config.model_name

    def describe_image(
        self,
        image_bytes: bytes,
        *,
        mime_type: str = "image/png",
        extra_instruction: str = "",
    ) -> VisionDescription:
        prompt = _VISION_SYSTEM_PROMPT
        if extra_instruction.strip():
            prompt += "\nAdditional instruction: " + extra_instruction.strip()
        content = self._ask(image_bytes, prompt, mime_type=mime_type)
        try:
            parsed = json.loads(_strip_json_fences(content))
        except ValueError as exc:
            raise VisionError(
                f"VLM returned non-JSON content: {content[:300]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise VisionError("VLM JSON payload is not an object")
        tags = parsed.get("visual_tags", [])
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        if not isinstance(tags, list):
            tags = []
        description = VisionDescription(
            scene_category=str(parsed.get("scene_category", "") or ""),
            background_color=str(parsed.get("background_color", "") or ""),
            subject_and_portrait=str(parsed.get("subject_and_portrait", "") or ""),
            visual_tags=[str(item).strip() for item in tags if str(item).strip()],
            detailed_caption=str(parsed.get("detailed_caption", "") or ""),
            raw=parsed,
        )
        if not description.detailed_caption and not description.scene_category:
            raise VisionError("VLM returned an empty description")
        return description

    def ask_about_image(
        self,
        image_bytes: bytes,
        question: str,
        *,
        mime_type: str = "image/png",
    ) -> str:
        """Free-form visual verification used by Step 7's verification tool."""
        if not question.strip():
            raise ValueError("question must not be empty")
        return self._ask(image_bytes, question.strip(), mime_type=mime_type)

    def _ask(self, image_bytes: bytes, prompt: str, *, mime_type: str) -> str:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self._config.model_name,
            "temperature": 0.0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                        },
                    ],
                }
            ],
        }
        parsed = _post_json(
            self._url,
            payload,
            timeout=float(self._config.timeout_seconds),
            api_key=_api_key(self._config.api_key),
        )
        try:
            content = parsed["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise VisionError("VLM response is missing choices[0].message.content") from exc
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return str(content or "")


def build_embedding_client(config: EmbeddingConfig) -> EmbeddingClient | None:
    """Return a client only when the service is fully configured."""
    if config.provider == ServiceMode.DISABLED:
        return None
    if config.provider == ServiceMode.LOCAL:
        return None
    if not config.base_url or not config.model_name:
        return None
    return EmbeddingClient(config)


def build_vision_client(config: VLMConfig) -> VisionClient | None:
    if config.provider == ServiceMode.DISABLED:
        return None
    if config.provider == ServiceMode.LOCAL:
        return None
    if not config.base_url or not config.model_name:
        return None
    return VisionClient(config)
