"""Server-side barcode decoding. Optional: the browser decodes live with zxing-wasm and sends
the values as form fields, so this is a second opinion on the saved frame."""
import io

from PIL import Image


def decode_image(img: Image.Image) -> list[str]:
    try:
        import zxingcpp  # pip install zxing-cpp
    except ImportError:
        return []
    try:
        results = zxingcpp.read_barcodes(img.convert("RGB"))
    except Exception:
        return []
    return [r.text for r in results if r.text]


def decode(data: bytes) -> list[str]:
    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        return []
    return decode_image(img)
