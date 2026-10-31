# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["fonttools[woff]==4.60.1"]
# ///
"""Web の書体に寸法を合わせた代わりの書体（`tokens/font-fallback.css`）の値を、実際の書体のファイルから求める（#3335）。

使い方（RAG / Agent / NL2SQL のどれかの frontend で `npm ci` した後。OS の書体は Liberation Sans / Mono のファイル）:

    uv run platform/packages/ui/scripts/font-fallback-metrics.py \
      --fontsource rag/frontend/node_modules \
      --liberation-sans /usr/share/fonts/liberation-sans \
      --liberation-mono /usr/share/fonts/liberation-mono

求め方（Next.js の `next/font`・Fontaine と同じ。横幅の平均は capsize の `xWidthAvg` と同じ英文の字の頻度の重み）:

- `size-adjust` = Web の書体の平均の字幅 / 代わりの書体の平均の字幅（どちらも units-per-em で割った値）。
  ラテン文字の範囲だけに使う。かな・漢字は日本語・中国語の書体のどれも全角（1em）なので 100% のまま。
- `ascent-override` / `descent-override` / `line-gap-override` = Web の書体の hhea の値 / units-per-em / `size-adjust`。
  代わりの書体の縦の寸法を Web の書体と同じにし、差し替えで行の中の基線が動かないようにする。
- Liberation Sans / Mono は Arial / Courier New と字幅が同じ（metric compatible）なので、同じ値を Arial /
  Courier New の `local()` にも使う。Noto Sans JP は可変の書体を太さ 400 / 700 の instance にしてから測る。
"""

from __future__ import annotations

import argparse
import io
from dataclasses import dataclass
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

# capsize（@capsizecss/unpack の weightings.ts の latin）の字の頻度の重み。Next.js の next/font も同じ表を使う。
LATIN_WEIGHTS: dict[str, float] = {
    "0": 0.0053, "1": 0.0023, "2": 0.0026, "3": 0.001, "4": 0.0008, "5": 0.0015, "6": 0.0007,
    "7": 0.0005, "8": 0.0007, "9": 0.0006, ",": 0.0083, " ": 0.154, "t": 0.0672, "h": 0.0351,
    "e": 0.0922, "o": 0.0571, "f": 0.017, "P": 0.0023, "p": 0.0163, "l": 0.0304, "'": 0.0014,
    "s": 0.0469, "R": 0.0015, "u": 0.0207, "b": 0.0114, "i": 0.0588, "c": 0.0232, "C": 0.0031,
    "n": 0.0578, "a": 0.0668, "d": 0.0298, "y": 0.0123, "w": 0.011, "B": 0.002, "r": 0.0526,
    "z": 0.0011, "G": 0.0011, "j": 0.0009, "T": 0.0041, ".": 0.0079, "L": 0.0012, "k": 0.0046,
    "m": 0.0181, "]": 0.0007, "J": 0.0009, "F": 0.0015, "v": 0.0076, "g": 0.0155, "A": 0.004,
    "N": 0.0014, "-": 0.0018, "H": 0.0013, "D": 0.0013, "M": 0.0025, "I": 0.0022, "E": 0.0011,
    '"': 0.0012, "S": 0.0041, "(": 0.001, ")": 0.001, "x": 0.0025, "W": 0.0012, "Q": 0.0001,
    "Y": 0.0003, "q": 0.0008, "V": 0.0005, "á": 0.0001, "K": 0.0007, "U": 0.0016, "=": 0.0007,
    "[": 0.0021, "O": 0.0009, "é": 0.0001, "$": 0.0002, ":": 0.0008, "|": 0.0038, "/": 0.0001,
    "%": 0.0001, "Z": 0.0002, ";": 0.0001, "X": 0.0001,
}  # fmt: skip


@dataclass(frozen=True)
class Metrics:
    """書体の寸法（em に対する比）。"""

    ascent: float
    descent: float
    line_gap: float
    avg_width: float
    ideograph_width: float | None


def load(path: Path, weight: int | None = None) -> TTFont:
    font = TTFont(str(path))
    if weight is not None and "fvar" in font:
        font = instantiateVariableFont(font, {"wght": weight})
        # instance を保存し直して、HVAR を反映した hmtx を読む。
        buffer = io.BytesIO()
        font.save(buffer)
        buffer.seek(0)
        font = TTFont(buffer)
    return font


def metrics(font: TTFont) -> Metrics:
    upem = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    hmtx = font["hmtx"]
    total = 0.0
    weights = 0.0
    for char, weight in LATIN_WEIGHTS.items():
        glyph = cmap.get(ord(char))
        if glyph is None:
            continue
        total += hmtx[glyph][0] * weight
        weights += weight
    ideograph = cmap.get(ord("一"))
    hhea = font["hhea"]
    return Metrics(
        ascent=hhea.ascent / upem,
        descent=-hhea.descent / upem,
        line_gap=hhea.lineGap / upem,
        # 頻度の表の字が欠けていても、ある字の重みで割って平均にする（capsize は欠けた字を xAvgCharWidth で補う）。
        avg_width=total / weights / upem,
        ideograph_width=hmtx[ideograph][0] / upem if ideograph else None,
    )


def percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def report(label: str, web: Metrics, fallback: Metrics) -> None:
    size_adjust = web.avg_width / fallback.avg_width
    print(f"/* {label} */")
    print(f"  size-adjust: {percent(size_adjust)};")
    print(f"  ascent-override: {percent(web.ascent / size_adjust)};")
    print(f"  descent-override: {percent(web.descent / size_adjust)};")
    print(f"  line-gap-override: {percent(web.line_gap / size_adjust)};")
    print(
        f"  /* web: ascent {web.ascent:.4f} descent {web.descent:.4f} line-gap {web.line_gap:.4f} "
        f"avg {web.avg_width:.4f} ideograph {web.ideograph_width}; fallback avg {fallback.avg_width:.4f} */"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fontsource", type=Path, required=True, help="@fontsource の入った node_modules")
    parser.add_argument("--liberation-sans", type=Path, required=True)
    parser.add_argument("--liberation-mono", type=Path, required=True)
    args = parser.parse_args()

    jp = args.fontsource / "@fontsource-variable/noto-sans-jp/files/noto-sans-jp-latin-wght-normal.woff2"
    jp_cjk = sorted((args.fontsource / "@fontsource-variable/noto-sans-jp/files").glob("noto-sans-jp-*-wght-normal.woff2"))
    code = args.fontsource / "@fontsource/google-sans-code/files"
    # 太さ 400 / 500 は代わりの書体の標準、600 / 700 は太字に当てる（合成の太字にしない）。
    for weight, sans_file, mono_file in (
        (400, "LiberationSans-Regular.ttf", "LiberationMono-Regular.ttf"),
        (500, "LiberationSans-Regular.ttf", "LiberationMono-Regular.ttf"),
        (600, "LiberationSans-Bold.ttf", "LiberationMono-Bold.ttf"),
        (700, "LiberationSans-Bold.ttf", "LiberationMono-Bold.ttf"),
    ):
        web_sans = metrics(load(jp, weight))
        report(f"Noto Sans JP {weight} / Arial・Liberation Sans（ラテン文字）", web_sans, metrics(load(args.liberation_sans / sans_file)))
        web_mono = metrics(load(code / f"google-sans-code-latin-{weight}-normal.woff2"))
        report(f"Google Sans Code {weight} / Courier New・Liberation Mono", web_mono, metrics(load(args.liberation_mono / mono_file)))
    # かな・漢字の字幅（全角 = 1em）を Noto Sans JP の実際の slice で確かめる。
    widths = set()
    for path in jp_cjk:
        font = load(path)
        cmap = font.getBestCmap()
        upem = font["head"].unitsPerEm
        for code_point in (0x3042, 0x30A2, 0x4E00, 0x6F22):
            glyph = cmap.get(code_point)
            if glyph:
                widths.add(font["hmtx"][glyph][0] / upem)
    print(f"/* Noto Sans JP のかな・漢字の字幅: {sorted(widths)} em（CJK の代わりの書体の size-adjust は 100%） */")


if __name__ == "__main__":
    main()
