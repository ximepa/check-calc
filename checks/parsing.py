"""Turn a photo of a paper receipt into structured check data using Claude.

The image is normalised locally (rotated upright, downscaled, re-encoded as
JPEG) and then sent to the Messages API with a JSON schema, so the model
returns validated fields rather than prose we would have to re-parse.
"""

from __future__ import annotations

import base64
import io
import logging
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from django.conf import settings
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Anthropic's vision guidance: images are downscaled server-side past ~1568px
# on the long edge, so sending anything bigger just costs upload time.
MAX_EDGE = 1568
JPEG_QUALITY = 85

SYSTEM_PROMPT = """\
You read photographs of paper receipts and bills and return their contents as \
structured data.

Rules:
- Transcribe only what is printed on the receipt. Never invent a line item, a \
price, or a merchant name. If something is unreadable, leave it out rather \
than guessing.
- Keep item names in the language printed on the receipt; do not translate.
- Every amount is a number in the receipt's own currency, without a currency \
symbol or thousands separator. Use a dot for the decimal separator.
- unit_price is the price of ONE unit and line_total is what that row was \
actually charged. If the receipt only prints a row total for a multi-unit \
row, divide it to get unit_price.
- Skip non-purchase rows: subtotals, tax lines, tips, totals, change due, \
loyalty points, card details. Those belong in the dedicated fields.
- Report tax, tip, discount and total as the amounts printed on the receipt. \
Leave a field null when the receipt does not print it.
- If the image is not a receipt or is too blurred to read, set is_receipt to \
false and explain in reader_notes.\
"""


class ParsedItem(BaseModel):
    """One purchased line from the receipt."""

    name: str = Field(description="Item name as printed on the receipt.")
    quantity: float = Field(default=1, description="Units bought. 1 when not printed.")
    unit_price: float = Field(description="Price of a single unit.")
    line_total: Optional[float] = Field(
        default=None, description="Row total as printed, if the receipt shows one."
    )


class ParsedReceipt(BaseModel):
    """Everything worth lifting off a receipt photo."""

    is_receipt: bool = Field(description="False if the image is not a readable receipt.")
    merchant: Optional[str] = Field(default=None, description="Shop or restaurant name.")
    purchased_on: Optional[str] = Field(
        default=None, description="Date printed on the receipt, as YYYY-MM-DD."
    )
    currency: Optional[str] = Field(default=None, description="ISO code or symbol, if printed.")
    items: List[ParsedItem] = Field(default_factory=list)
    subtotal: Optional[float] = None
    discount: Optional[float] = Field(default=None, description="Total discount, as a positive number.")
    tax_amount: Optional[float] = None
    tip_amount: Optional[float] = None
    total: Optional[float] = Field(default=None, description="Grand total charged.")
    reader_notes: str = Field(
        default="", description="Anything unreadable, ambiguous, or worth a human check."
    )


class ReceiptParseError(RuntimeError):
    """Raised when a receipt could not be turned into structured data."""


def prepare_image(raw: bytes) -> tuple[bytes, str]:
    """Rotate, downscale and re-encode a photo so it is cheap to send.

    Phone photos carry EXIF orientation and are far larger than the model can
    use; both hurt accuracy and cost. Returns (bytes, media_type).
    """
    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image)  # honour the camera's rotation flag
        image = image.convert("RGB")
    except Exception as exc:  # Pillow raises a zoo of errors on bad input
        raise ReceiptParseError(f"Could not read the uploaded image: {exc}") from exc

    if max(image.size) > MAX_EDGE:
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), "image/jpeg"


def get_client():
    """Build an Anthropic client, or explain what is missing."""
    import anthropic

    api_key = getattr(settings, "ANTHROPIC_API_KEY", "") or None
    try:
        # With no explicit key the SDK still resolves ANTHROPIC_AUTH_TOKEN or an
        # `ant auth login` profile, so let it try before declaring defeat.
        return anthropic.Anthropic(
            api_key=api_key,
            timeout=getattr(settings, "RECEIPT_PARSER_TIMEOUT", 120.0),
            max_retries=2,
        )
    except anthropic.AnthropicError as exc:
        raise ReceiptParseError(
            "No Anthropic credentials found. Set ANTHROPIC_API_KEY in the "
            f"environment before uploading receipts. ({exc})"
        ) from exc


def parse_receipt_image(raw: bytes) -> tuple[ParsedReceipt, dict]:
    """Send one receipt photo to Claude and return (parsed receipt, usage)."""
    import anthropic

    image_bytes, media_type = prepare_image(raw)
    client = get_client()
    model = getattr(settings, "RECEIPT_PARSER_MODEL", "claude-opus-5")

    try:
        response = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.standard_b64encode(image_bytes).decode(),
                            },
                        },
                        {
                            "type": "text",
                            "text": "Read this receipt and return its line items and totals.",
                        },
                    ],
                }
            ],
            output_format=ParsedReceipt,
        )
    except anthropic.APIStatusError as exc:
        raise ReceiptParseError(f"Claude API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ReceiptParseError(f"Could not reach the Claude API: {exc}") from exc

    if response.stop_reason == "refusal":
        raise ReceiptParseError("Claude declined to process this image.")

    parsed = response.parsed_output
    if parsed is None:
        raise ReceiptParseError("Claude returned no structured data for this image.")
    if not parsed.is_receipt:
        raise ReceiptParseError(
            parsed.reader_notes or "The image does not look like a readable receipt."
        )
    if not parsed.items:
        raise ReceiptParseError(
            parsed.reader_notes or "No line items could be read from this receipt."
        )

    usage = {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    logger.info("Parsed receipt with %s: %s", model, usage)
    return parsed, usage


def to_decimal(value, default=None):
    """Convert a model-supplied number to a 2dp Decimal, or ``default``."""
    if value is None:
        return default
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return default
