"""Helpers the pi extension calls inside the sandbox: `zoom` crops a page, `check` validates an answer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/opt")

MAX_LONG_SIDE = 1600  # enlarged crop size; enough detail without blowing up the context
MAX_SCALE = 4.0


def zoom(page: int, box: list[float], out: str) -> str:
    from PIL import Image

    path = Path(f"/task/doc/page-{page:04d}.jpg")
    if not path.is_file():
        raise ValueError(f"page {page} does not exist")
    x0, y0, x1, y1 = (min(1000.0, max(0.0, v)) for v in box)
    if x1 - x0 < 5 or y1 - y0 < 5:
        raise ValueError("region too small: use 0-1000 coordinates with x0<x1, y0<y1")
    with Image.open(path) as image:
        w, h = image.size
        crop = image.crop(
            (round(x0 * w / 1000), round(y0 * h / 1000), round(x1 * w / 1000), round(y1 * h / 1000))
        )
    scale = min(MAX_SCALE, MAX_LONG_SIDE / max(crop.size))
    if scale > 1:
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.Resampling.LANCZOS)
    crop.save(out, "PNG")
    return f"{crop.width}x{crop.height}px, {scale:.1f}x"


def check(question_type: str, answer_format: str, raw: str) -> str:
    from tableqa.schema import normalize_response

    return normalize_response(raw, question_type, answer_format)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "zoom":
            print(zoom(int(sys.argv[2]), [float(v) for v in sys.argv[3:7]], sys.argv[7]))
        else:
            print(check(sys.argv[2], sys.argv[3], sys.stdin.read()))
    except (ValueError, OSError) as exc:  # AnswerError is a ValueError
        print(exc, file=sys.stderr)
        sys.exit(1)
