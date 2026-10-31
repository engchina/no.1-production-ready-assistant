# /// script
# requires-python = ">=3.12,<3.13"
# ///
"""内容の言語の判定（`src/lib/content-lang.ts`）が使う、簡体字だけ・繁体字だけの字の集合を Unihan から作る（#3335）。

使い方（Unihan.zip を展開したディレクトリを渡す。版は Unicode 17.0.0）:

    curl -sSfLO https://www.unicode.org/Public/17.0.0/ucd/Unihan.zip && unzip -o Unihan.zip -d unihan
    uv run platform/packages/ui/scripts/generate-han-script-sets.py unihan > platform/packages/ui/src/lib/han-script-sets.ts

集合の決め方（特定の文書・語に合わせない、文字の符号化の表だけで決める汎用の規則）:

- 簡体字だけの字: GB 2312 の第 1 水準（kGB0 の 16〜55 区。常用の簡体字）にあり、Big5（kBigFive）に無く、
  繁体字の異体（kTraditionalVariant）が別の字で、日本語で使う字（JIS X 0208・JIS X 0213・常用漢字・人名用漢字）に
  無い字。日本語の文に現れないので、1 字でも中国語（簡体）の印になる。
- 繁体字だけの字: Big5 の常用字（A440〜C67E）にあり、GB 2312 に無く、簡体字の異体（kSimplifiedVariant）が別の字で、
  現代の日本語で使う字（JIS X 0208 の第 1 水準・常用漢字・人名用漢字）に無い字。
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

FILES = ("Unihan_OtherMappings.txt", "Unihan_Variants.txt")


def read_fields(directory: Path) -> dict[str, dict[int, str]]:
    fields: dict[str, dict[int, str]] = collections.defaultdict(dict)
    for name in FILES:
        for line in (directory / name).read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            code, field, value = line.split("\t", 2)
            fields[field][int(code[2:], 16)] = value
    return fields


def variants(value: str) -> set[int]:
    return {int(item[2:].split("<")[0], 16) for item in value.split()}


def big5_code(value: str) -> int:
    return int(value.split()[0].rstrip("'"), 16)


def main() -> None:
    fields = read_fields(Path(sys.argv[1]))
    gb, big5 = fields["kGB0"], fields["kBigFive"]
    joyo_jinmeiyo = set(fields["kJoyoKanji"]) | set(fields["kJinmeiyoKanji"])
    japanese = set(fields["kJis0"]) | set(fields["kJIS0213"]) | joyo_jinmeiyo
    jis_level1 = {code for code, value in fields["kJis0"].items() if 16 <= int(value[:2]) <= 47}
    modern_japanese = jis_level1 | joyo_jinmeiyo
    simplified = sorted(
        code
        for code, value in gb.items()
        if 16 <= int(value[:2]) <= 55
        and code not in big5
        and code not in japanese
        and variants(fields["kTraditionalVariant"].get(code, "")) - {code}
    )
    traditional = sorted(
        code
        for code, value in big5.items()
        if 0xA440 <= big5_code(value) <= 0xC67E
        and code not in gb
        and code not in modern_japanese
        and variants(fields["kSimplifiedVariant"].get(code, "")) - {code}
    )
    print("// このファイルは scripts/generate-han-script-sets.py が Unihan（Unicode 17.0.0）から作る。手で直さない（#3335）。")
    print()
    print(f"/** 簡体字だけの字（{len(simplified)} 字。日本語の字・繁体字の表に無い常用の簡体字）。 */")
    print(f'export const SIMPLIFIED_ONLY_HAN = "{"".join(map(chr, simplified))}";')
    print()
    print(f"/** 繁体字だけの字（{len(traditional)} 字。現代の日本語の字・簡体字の表に無い常用の繁体字）。 */")
    print(f'export const TRADITIONAL_ONLY_HAN = "{"".join(map(chr, traditional))}";')


if __name__ == "__main__":
    main()
