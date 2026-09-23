"""Subtitle style previewer: see position / size / font / outline BEFORE
burning anything.

    python main.py subpreview clips/xyz_clip_01.mp4
    python main.py subpreview video.mp4 --at 42 --text "your caption line"

Opens a small window with a real frame from the video, the caption drawn
on it in the current style, and live controls: position (default/top/
middle/bottom), text size, outline weight, font. "Copy YAML" puts the
exact `subtitles:` block on the clipboard — paste it into config.yaml and
every future render matches. Nothing is rendered or uploaded.

Headless / no display (SSH, CI)? The same command writes
subpreview_top.png / _middle.png / _bottom.png next to the video and
prints the YAML instead. Pure helpers (render_preview, style_yaml,
preview_trio, save_trio) are unit-tested; the tkinter shell is thin,
like the bot lane's PhoneBot.
"""

from __future__ import annotations

import io
from pathlib import Path

SAMPLE_TEXT = "Why zebras have stripes - the REAL reason"
HIGHLIGHT_WORD = "REAL"        # the word shown in karaoke gold

# Font look-up homes, first hit wins. Anything missing falls back to
# Pillow's built-in font, so the preview always renders something.
_FONT_DIRS = [
    Path("C:/Windows/Fonts"),
    Path("/usr/share/fonts"),
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
]
_FONT_SUGGESTIONS = ["Arial", "Verdana", "Impact", "Georgia",
                     "Trebuchet MS", "Comic Sans MS", "Tahoma"]


def _find_font(name: str, size: int):
    """A PIL font for `name` at `size`; built-in default if not installed."""
    from PIL import ImageFont

    clean = (name or "").strip().lower().replace(" ", "")
    candidates = [f"{clean}.ttf", f"{clean}bd.ttf", f"{clean}-bold.ttf",
                  f"{clean}bold.ttf", f"{clean}-regular.ttf"]
    for folder in _FONT_DIRS:
        if not folder.is_dir():
            continue
        try:
            for file in candidates:
                hits = sorted(folder.rglob(file))
                if hits:
                    try:
                        return ImageFont.truetype(str(hits[0]), size)
                    except OSError:
                        continue
        except OSError:  # unreadable font dir -> next home
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1: no scalable default font
        return ImageFont.load_default()


def _wrap(text: str, font, draw, max_w: int) -> list[list[str]]:
    words = text.split()
    lines: list[list[str]] = []
    cur: list[str] = []
    for word in words:
        trial = " ".join(cur + [word])
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = [word]
        else:
            cur.append(word)
    if cur:
        lines.append(cur)
    return lines


def render_preview(frame: bytes, style: dict, text: str = SAMPLE_TEXT,
                   width: int = 405) -> bytes:
    """One frame with the caption burned in the requested style (PNG bytes).

    Pillow approximation of the real burn engines: white words, one word
    in karaoke gold, black outline, and the same placements the .ass
    builder uses (portrait: top 140 / bottom 300 true pixels; landscape
    45/45; "default" = portrait center, landscape bottom).
    """
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(frame)).convert("RGB")
    if img.width > 0 and img.width != width:
        img = img.resize(
            (width, max(1, round(img.height * width / img.width))))
    W, H = img.size
    landscape = W / H > 1.0
    ref_w, ref_h = (1920, 1080) if landscape else (1080, 1920)
    pos = str(style.get("position", "default"))
    eff = pos if pos in ("top", "middle", "bottom") \
        else ("bottom" if landscape else "middle")
    scale = float(style.get("scale", 1.0) or 1.0)
    outline = int(style.get("outline", 0) or 0)
    base_font = 52 if landscape else 88
    top_m, bot_m = (45, 45) if landscape else (140, 300)
    font_px = max(12, round(base_font * scale * W / ref_w))
    font = _find_font(str(style.get("font", "Arial")), font_px)
    draw = ImageDraw.Draw(img)
    lines = _wrap(text, font, draw, int(W * 0.86))
    line_h = round(font_px * 1.25)
    block_h = line_h * len(lines)
    if eff == "top":
        y = round(top_m * H / ref_h)
    elif eff == "bottom":
        y = H - round(bot_m * H / ref_h) - block_h
    else:
        y = (H - block_h) // 2
    stroke = max(1, round(font_px * (0.05 + 0.015 * (outline or 2.5))))
    gold, white, black = (255, 215, 0), (255, 255, 255), (0, 0, 0)
    for line_words in lines:
        total = draw.textlength(" ".join(line_words), font=font)
        x = (W - total) / 2
        for word in line_words:
            color = gold if word.strip(".,-—") == HIGHLIGHT_WORD else white
            draw.text((x, y), word, font=font, fill=color,
                      stroke_width=stroke, stroke_fill=black)
            x += draw.textlength(word + " ", font=font)
        y += line_h
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def style_yaml(style: dict) -> str:
    """The exact config.yaml block for this style (comment included)."""
    pos = str(style.get("position", "default"))
    return (
        "subtitles:\n"
        f'  position: "{pos}"'
        "   # default | top | middle | bottom "
        "(clip runs also take --sub-pos auto)\n"
        f'  font: "{str(style.get("font", "Arial"))}"\n'
        f"  font_scale: {float(style.get('scale', 1.0) or 1.0)}\n"
        f"  outline: {int(style.get('outline', 0) or 0)}\n"
    )


def preview_trio(frame: bytes, style: dict,
                 text: str = SAMPLE_TEXT) -> dict[str, bytes]:
    """{"top": png, "middle": png, "bottom": png} for side-by-side looks."""
    return {pos: render_preview(frame, {**style, "position": pos}, text)
            for pos in ("top", "middle", "bottom")}


def save_trio(dest_dir: Path, frame: bytes, style: dict,
              text: str = SAMPLE_TEXT) -> list[Path]:
    """Write subpreview_<pos>.png into dest_dir; returns the paths."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for pos, png in preview_trio(frame, style, text).items():
        p = dest_dir / f"subpreview_{pos}.png"
        p.write_bytes(png)
        paths.append(p)
    return paths


def probe_duration(src: Path) -> float:
    """Video duration in seconds, 0.0 when ffprobe cannot answer."""
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(src)],
            check=True, capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:  # noqa: BLE001 - preview must never crash on this
        return 0.0


def build_app(frame: bytes, style: dict, text: str = SAMPLE_TEXT) -> int:
    """The picker window. 0 on close. Raises outside a GUI session."""
    import tkinter as tk
    from tkinter import ttk

    from PIL import Image, ImageTk

    state = dict(style)
    root = tk.Tk()
    root.title("Subtitle preview — paste the result into config.yaml")
    disp_w = 405
    img = Image.open(io.BytesIO(frame))
    if img.width != disp_w:
        img = img.resize((disp_w, round(img.height * disp_w / img.width)))
    panel = tk.Label(root)
    panel.grid(row=0, column=0, rowspan=10, padx=10, pady=10)

    def redraw(*_):
        png = render_preview(frame, state, text, width=disp_w)
        photo = ImageTk.PhotoImage(Image.open(io.BytesIO(png)))
        panel.configure(image=photo)
        panel.image = photo  # keep a reference alive

    row = 0
    tk.Label(root, text="position").grid(row=row, column=1, sticky="w")
    posvar = tk.StringVar(value=str(state.get("position", "default")))
    for pos in ("default", "top", "middle", "bottom"):
        rb = tk.Radiobutton(root, text=pos, variable=posvar, value=pos,
                            command=lambda p=pos: (state.__setitem__(
                                "position", p), redraw()))
        rb.grid(row=row, column=2 + ("default top middle bottom".split()
                                     .index(pos)), sticky="w")
    row += 1
    tk.Label(root, text="font").grid(row=row, column=1, sticky="w")
    fontbox = ttk.Combobox(root, values=_FONT_SUGGESTIONS, width=14,
                           textvariable=tk.StringVar(
                               value=str(state.get("font", "Arial"))))
    fontbox.grid(row=row, column=2, columnspan=2, sticky="we")
    fontbox.bind("<<ComboboxSelected>>",
                 lambda _: (state.__setitem__("font", fontbox.get()), redraw()))
    row += 1
    tk.Label(root, text="size").grid(row=row, column=1, sticky="w")
    scale = tk.Scale(root, from_=0.6, to=2.0, resolution=0.05,
                     orient="horizontal",
                     command=lambda v: (state.__setitem__(
                         "scale", float(v)), redraw()))
    scale.set(float(state.get("scale", 1.0) or 1.0))
    scale.grid(row=row, column=2, columnspan=3, sticky="we")
    row += 1
    tk.Label(root, text="outline").grid(row=row, column=1, sticky="w")
    outline = tk.Scale(root, from_=0, to=4, resolution=1, orient="horizontal",
                       command=lambda v: (state.__setitem__(
                           "outline", int(float(v))), redraw()))
    outline.set(int(state.get("outline", 0) or 0))
    outline.grid(row=row, column=2, columnspan=3, sticky="we")
    row += 2

    def copy_yaml():
        block = style_yaml(state)
        root.clipboard_clear()
        root.clipboard_append(block)
        print(block)
        copy_btn.configure(text="copied — paste into config.yaml")

    copy_btn = tk.Button(root, text="Copy YAML", command=copy_yaml)
    copy_btn.grid(row=row, column=1, columnspan=4, sticky="we", pady=6)
    tk.Label(root, text="approximate rendering — the burn engines\n"
                        "have the final word", fg="#888").grid(
        row=row + 1, column=1, columnspan=4)
    redraw()
    root.mainloop()
    return 0
