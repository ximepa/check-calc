"""Read receipts and bills — photos or PDFs — into structured check data.

The work splits in two:

* :func:`prepare_document` turns whatever was uploaded into something a model
  can read: a downscaled JPEG for photos and scans, or plain text when a PDF
  carries a real text layer (cheaper, and more accurate than reading a picture
  of the same words).
* A *backend* sends that to a model and returns validated fields. Three ship
  here — Claude, Gemini and Ollama — and which one runs is a setting, so a
  free option can be used without touching this app's code.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from django.conf import settings
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

# Vision models downscale past roughly this size anyway, so sending more only
# costs upload time and tokens.
MAX_EDGE = 1568
JPEG_QUALITY = 85
# A PDF page rendered below this much text is treated as a scan, not a document.
MIN_TEXT_CHARS = 40
PDF_TEXT_PAGES = 10
PDF_RENDER_PAGES = 3
PDF_RENDER_SCALE = 2.0

SYSTEM_PROMPT = """\
You read receipts and bills and return their contents as structured data.

Rules:
- Transcribe only what is on the receipt. Never invent a line item, a price, or \
a merchant name. If something is unreadable, leave it out rather than guessing.
- Keep item names in the language printed on the receipt; do not translate.
- Every amount is a number in the receipt's own currency, without a currency \
symbol or thousands separator. Use a dot for the decimal separator.
- unit_price is the price of ONE unit and line_total is what that row was \
actually charged. If only a row total is printed for a multi-unit row, divide \
it to get unit_price.
- Skip non-purchase rows: subtotals, tax lines, tips, totals, change due, \
loyalty points, card details. Those belong in the dedicated fields.
- Report tax, tip, discount and total as the amounts printed on the receipt. \
Leave a field null when the receipt does not print it.
- If this is not a receipt, or is too damaged to read, set is_receipt to false \
and explain in reader_notes.
- Reply with JSON only. No prose, no markdown fences.\
"""

IMAGE_PROMPT = "Read this receipt and return its line items and totals."
TEXT_PROMPT = "Read this receipt text and return its line items and totals:\n\n"


class ParsedItem(BaseModel):
    """One purchased line from the receipt."""

    name: str = Field(description="Item name as printed on the receipt.")
    quantity: float = Field(default=1, description="Units bought. 1 when not printed.")
    unit_price: float = Field(description="Price of a single unit.")
    line_total: Optional[float] = Field(
        default=None, description="Row total as printed, if the receipt shows one."
    )


class ParsedReceipt(BaseModel):
    """Everything worth lifting off a receipt."""

    is_receipt: bool = Field(description="False if this is not a readable receipt.")
    merchant: Optional[str] = Field(default=None, description="Shop or restaurant name.")
    purchased_on: Optional[str] = Field(
        default=None, description="Date printed on the receipt, as YYYY-MM-DD."
    )
    currency: Optional[str] = Field(default=None, description="ISO code or symbol, if printed.")
    items: List[ParsedItem] = Field(default_factory=list)
    subtotal: Optional[float] = None
    discount: Optional[float] = Field(
        default=None, description="Total discount, as a positive number."
    )
    tax_amount: Optional[float] = None
    tip_amount: Optional[float] = None
    total: Optional[float] = Field(default=None, description="Grand total charged.")
    reader_notes: str = Field(
        default="", description="Anything unreadable, ambiguous, or worth a human check."
    )


class ReceiptParseError(RuntimeError):
    """Raised when a receipt could not be turned into structured data."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _inline_refs(node, defs):
    """Resolve ``$ref`` in place — most APIs reject a schema with ``$defs``."""
    if isinstance(node, dict):
        if "$ref" in node:
            return _inline_refs(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
        return {key: _inline_refs(value, defs) for key, value in node.items() if key != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(value, defs) for value in node]
    return node


def receipt_json_schema():
    """The receipt schema as flat JSON Schema, shared by every backend."""
    schema = ParsedReceipt.model_json_schema()
    return _inline_refs(schema, schema.get("$defs", {}))


_GEMINI_TYPES = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "object": "OBJECT",
    "array": "ARRAY",
}


def gemini_schema(node=None):
    """Translate the schema into Gemini's OpenAPI subset.

    Gemini takes uppercase type names, expresses optionality as ``nullable``
    rather than a union with null, and rejects the extra keywords Pydantic
    emits.
    """
    node = receipt_json_schema() if node is None else node

    if "anyOf" in node:
        variants = [v for v in node["anyOf"] if v.get("type") != "null"]
        converted = gemini_schema(variants[0]) if variants else {"type": "STRING"}
        if len(variants) != len(node["anyOf"]):
            converted["nullable"] = True
        if node.get("description"):
            converted["description"] = node["description"]
        return converted

    converted = {}
    if node.get("type") in _GEMINI_TYPES:
        converted["type"] = _GEMINI_TYPES[node["type"]]
    if node.get("description"):
        converted["description"] = node["description"]
    if "properties" in node:
        converted["properties"] = {
            name: gemini_schema(value) for name, value in node["properties"].items()
        }
        converted["propertyOrdering"] = list(node["properties"])
        if node.get("required"):
            converted["required"] = node["required"]
    if "items" in node:
        converted["items"] = gemini_schema(node["items"])
    return converted


# ---------------------------------------------------------------------------
# Turning an upload into something a model can read
# ---------------------------------------------------------------------------


@dataclass
class SourceDocument:
    """What we actually send: either one image, or extracted text."""

    kind: str  # "image" or "text"
    image_bytes: bytes = b""
    media_type: str = ""
    text: str = ""
    pages: int = 1

    @property
    def is_image(self):
        return self.kind == "image"

    @property
    def base64_data(self):
        return base64.standard_b64encode(self.image_bytes).decode()


def looks_like_pdf(raw):
    return raw[:5] == b"%PDF-"


def prepare_image(raw, max_size=None):
    """Rotate, downscale and re-encode a photo so it is cheap to send.

    ``max_size`` defaults to a square bound, which is right for a single
    photo. A stitched multi-page scan passes a taller bound so each page keeps
    its own resolution instead of the whole strip being squashed to fit.
    """
    from PIL import Image, ImageOps

    max_size = max_size or (MAX_EDGE, MAX_EDGE)
    try:
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image)  # honour the camera's rotation flag
        image = image.convert("RGB")
    except Exception as exc:  # Pillow raises a zoo of errors on bad input
        raise ReceiptParseError(f"Could not read the uploaded image: {exc}") from exc

    if image.width > max_size[0] or image.height > max_size[1]:
        image.thumbnail(max_size, Image.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def pdf_text(raw):
    """Pull the text layer out of a PDF, or return "" when there isn't one."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise ReceiptParseError(f"PDF support needs the pypdf package: {exc}") from exc

    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages[:PDF_TEXT_PAGES]]
    except Exception as exc:
        raise ReceiptParseError(f"Could not read the PDF: {exc}") from exc

    text = "\n".join(pages).strip()
    return text if sum(character.isalnum() for character in text) >= MIN_TEXT_CHARS else ""


def render_pdf(raw):
    """Rasterise a scanned PDF into a single tall image.

    Stitching the pages together keeps every backend on the same one-image
    path, which matters more than page fidelity for a two-page bill.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise ReceiptParseError(f"Scanned PDFs need the pypdfium2 package: {exc}") from exc

    from PIL import Image

    document = None
    try:
        document = pdfium.PdfDocument(raw)
        pages = []
        for index in range(min(len(document), PDF_RENDER_PAGES)):
            page = document[index]
            pages.append(page.render(scale=PDF_RENDER_SCALE).to_pil().convert("RGB"))
            page.close()
    except Exception as exc:
        raise ReceiptParseError(f"Could not render the PDF: {exc}") from exc
    finally:
        if document is not None:
            document.close()

    if not pages:
        raise ReceiptParseError("That PDF has no pages.")

    width = max(page.width for page in pages)
    stitched = Image.new("RGB", (width, sum(page.height for page in pages)), (255, 255, 255))
    offset = 0
    for page in pages:
        stitched.paste(page, (0, offset))
        offset += page.height

    buffer = io.BytesIO()
    stitched.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    image_bytes, media_type = prepare_image(
        buffer.getvalue(), max_size=(MAX_EDGE, MAX_EDGE * len(pages))
    )
    return image_bytes, media_type, len(pages)


def prepare_document(raw, filename=""):
    """Normalise an upload into a :class:`SourceDocument`."""
    if looks_like_pdf(raw) or filename.lower().endswith(".pdf"):
        text = pdf_text(raw)
        if text:
            return SourceDocument(kind="text", text=text)
        image_bytes, media_type, pages = render_pdf(raw)
        return SourceDocument(
            kind="image", image_bytes=image_bytes, media_type=media_type, pages=pages
        )

    image_bytes, media_type = prepare_image(raw)
    return SourceDocument(kind="image", image_bytes=image_bytes, media_type=media_type)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def _validate(payload):
    """Turn a model's JSON into a ParsedReceipt, or explain why we can't."""
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("```"):  # some models fence their JSON regardless
            text = text.split("```")[1].removeprefix("json").strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReceiptParseError(f"The model did not return JSON: {exc}") from exc

    try:
        parsed = ParsedReceipt.model_validate(payload)
    except ValidationError as exc:
        raise ReceiptParseError(f"The model returned unusable fields: {exc}") from exc

    if not parsed.is_receipt:
        raise ReceiptParseError(
            parsed.reader_notes or "That does not look like a readable receipt."
        )
    if not parsed.items:
        raise ReceiptParseError(
            parsed.reader_notes or "No line items could be read from this receipt."
        )
    return parsed


def _post_json(url, payload, headers=None, timeout=None):
    """POST JSON and return the decoded reply, with readable failures."""
    host = urllib.parse.urlsplit(url).hostname or url
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"content-type": "application/json", **(headers or {})},
    )
    # A local model server must not be sent through an outbound proxy.
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if host in {"localhost", "127.0.0.1", "::1"}
        else urllib.request.build_opener()
    )
    try:
        with opener.open(request, timeout=timeout or _timeout()) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise ReceiptParseError(f"{host} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ReceiptParseError(f"Could not reach {host}: {exc.reason}") from exc


def _timeout():
    return getattr(settings, "RECEIPT_PARSER_TIMEOUT", 120.0)


class ParserBackend:
    """A service that can read a prepared document into receipt fields."""

    name = ""
    label = ""

    def is_configured(self):
        raise NotImplementedError

    def parse(self, document):
        raise NotImplementedError


class ClaudeBackend(ParserBackend):
    """Anthropic's Claude. Paid, and the most reliable on creased paper."""

    name = "claude"
    label = "Claude (Anthropic API, paid)"

    def is_configured(self):
        return bool(getattr(settings, "ANTHROPIC_API_KEY", ""))

    def parse(self, document):
        import anthropic

        try:
            client = anthropic.Anthropic(
                api_key=getattr(settings, "ANTHROPIC_API_KEY", "") or None,
                timeout=_timeout(),
                max_retries=2,
            )
        except anthropic.AnthropicError as exc:
            raise ReceiptParseError(
                "No Anthropic credentials found. Set ANTHROPIC_API_KEY, or choose a "
                f"free backend with RECEIPT_PARSER_BACKEND. ({exc})"
            ) from exc

        if document.is_image:
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": document.media_type,
                        "data": document.base64_data,
                    },
                },
                {"type": "text", "text": IMAGE_PROMPT},
            ]
        else:
            content = [{"type": "text", "text": TEXT_PROMPT + document.text}]

        try:
            response = client.messages.parse(
                model=getattr(settings, "RECEIPT_CLAUDE_MODEL", "claude-opus-5"),
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_format=ParsedReceipt,
            )
        except anthropic.APIStatusError as exc:
            raise ReceiptParseError(f"Claude API error ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ReceiptParseError(f"Could not reach the Claude API: {exc}") from exc

        if response.stop_reason == "refusal":
            raise ReceiptParseError("Claude declined to process this document.")
        if response.parsed_output is None:
            raise ReceiptParseError("Claude returned no structured data.")

        return _validate(response.parsed_output.model_dump()), {
            "model": response.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }


class GeminiBackend(ParserBackend):
    """Google Gemini. Has a no-cost free tier — an API key, but no card."""

    name = "gemini"
    label = "Gemini (Google AI Studio free tier)"
    endpoint = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def url(self, model):
        """The endpoint is overridable for gateways, proxies and tests."""
        template = getattr(settings, "RECEIPT_GEMINI_ENDPOINT", "") or self.endpoint
        return template.format(model=model)

    def api_key(self):
        return getattr(settings, "GEMINI_API_KEY", "")

    def is_configured(self):
        return bool(self.api_key())

    def parse(self, document):
        if not self.is_configured():
            raise ReceiptParseError(
                "No Gemini key. Create a free one at aistudio.google.com/apikey "
                "and set GEMINI_API_KEY."
            )

        model = getattr(settings, "RECEIPT_GEMINI_MODEL", "gemini-2.5-flash")
        if document.is_image:
            parts = [
                {
                    "inline_data": {
                        "mime_type": document.media_type,
                        "data": document.base64_data,
                    }
                },
                {"text": IMAGE_PROMPT},
            ]
        else:
            parts = [{"text": TEXT_PROMPT + document.text}]

        body = _post_json(
            self.url(model),
            {
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                    "responseSchema": gemini_schema(),
                },
            },
            headers={"x-goog-api-key": self.api_key()},
        )

        blocked = (body.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise ReceiptParseError(f"Gemini refused the document: {blocked}")

        candidates = body.get("candidates") or []
        if not candidates:
            raise ReceiptParseError("Gemini returned no candidates.")
        finish = candidates[0].get("finishReason")
        if finish not in (None, "STOP"):
            raise ReceiptParseError(f"Gemini stopped early ({finish}).")

        try:
            text = candidates[0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise ReceiptParseError(f"Unexpected Gemini response shape: {exc}") from exc

        usage = body.get("usageMetadata") or {}
        return _validate(text), {
            "model": body.get("modelVersion") or model,
            "input_tokens": usage.get("promptTokenCount"),
            "output_tokens": usage.get("candidatesTokenCount"),
        }


class OllamaBackend(ParserBackend):
    """A model running on your own machine. Free, offline, no key at all."""

    name = "ollama"
    label = "Ollama (local model, free)"

    def host(self):
        return getattr(settings, "OLLAMA_HOST", "http://localhost:11434").rstrip("/")

    def is_configured(self):
        # Nothing to configure — but whether it is *running* is only knowable
        # by asking, so that check belongs in parse().
        return True

    def parse(self, document):
        model = getattr(settings, "RECEIPT_OLLAMA_MODEL", "llama3.2-vision")
        message = {"role": "user", "content": IMAGE_PROMPT}
        if document.is_image:
            message["images"] = [document.base64_data]
        else:
            message["content"] = TEXT_PROMPT + document.text

        try:
            body = _post_json(
                f"{self.host()}/api/chat",
                {
                    "model": model,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT}, message],
                    "format": receipt_json_schema(),
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
        except ReceiptParseError as exc:
            raise ReceiptParseError(
                f"{exc} — is Ollama running? Start it with `ollama serve` and pull a "
                f"vision model with `ollama pull {model}`."
            ) from exc

        content = (body.get("message") or {}).get("content")
        if not content:
            raise ReceiptParseError(f"Ollama returned no content: {str(body)[:200]}")

        return _validate(content), {
            "model": body.get("model") or model,
            "input_tokens": body.get("prompt_eval_count"),
            "output_tokens": body.get("eval_count"),
        }


BACKENDS = {
    backend.name: backend for backend in (ClaudeBackend, GeminiBackend, OllamaBackend)
}
# Cheapest configured option first; Ollama needs no key so it is the fallback.
AUTO_ORDER = ("gemini", "claude", "ollama")


def get_backend(name=None):
    """Resolve the configured backend, or pick one that looks usable."""
    name = (name or getattr(settings, "RECEIPT_PARSER_BACKEND", "auto") or "auto").lower()

    if name == "auto":
        for candidate in AUTO_ORDER:
            backend = BACKENDS[candidate]()
            if backend.is_configured():
                return backend
        return OllamaBackend()

    if name not in BACKENDS:
        raise ReceiptParseError(
            f"Unknown parser backend {name!r}. Choose one of: {', '.join(BACKENDS)}."
        )
    return BACKENDS[name]()


def parse_receipt_document(raw, filename="", backend=None):
    """Read one uploaded receipt. Returns (parsed receipt, usage metadata)."""
    document = prepare_document(raw, filename)
    backend = backend or get_backend()
    parsed, usage = backend.parse(document)
    usage = {"backend": backend.name, "pages": document.pages, **usage}
    logger.info("Parsed receipt via %s: %s", backend.name, usage)
    return parsed, usage


def to_decimal(value, default=None):
    """Convert a model-supplied number to a 2dp Decimal, or ``default``."""
    if value is None:
        return default
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return default
