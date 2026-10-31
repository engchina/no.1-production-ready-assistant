"""CI の Oracle trace から数値の資源 ID とロックの対応だけを出す。"""

import re
import sys

RESOURCE = re.compile(r"(?:TM|TX)-[0-9A-Fa-f]+(?:-[0-9A-Fa-f]+)+\Z")
FIELD = re.compile(r"(?:[0-9]+|S|X|SS|SX|SSX|PR|PW|EX|CW|CR|NL|CONVERT)\Z")


def summarize(lines: list[str]) -> list[str]:
    rows: set[str] = set()
    for line in lines:
        fields = line.split()
        if (
            5 <= len(fields) <= 12
            and RESOURCE.fullmatch(fields[0])
            and all(FIELD.fullmatch(field) for field in fields[1:])
        ):
            rows.add(" ".join(fields))
    return sorted(rows)


if __name__ == "__main__":
    print("\n".join(summarize(sys.stdin.readlines())))
