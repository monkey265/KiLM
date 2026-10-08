"""
Helpers for KiCad S-expression library files.
"""

import re
from typing import Optional


def extract_symbol_blocks(text: str) -> list[str]:
    """Extract top-level `(symbol "...")` blocks by tracking paren depth.

    Depth-based (not indentation-based) so it works regardless of whether
    the generator indents with tabs (SamacSys/Mouser) or spaces
    (UltraLibrarian), and regardless of line endings.
    """
    blocks: list[str] = []
    depth = 0
    in_str = False
    block_start: Optional[int] = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            if depth == 1 and text.startswith('(symbol "', i):
                block_start = i
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 1 and block_start is not None:
                blocks.append(text[block_start : i + 1])
                block_start = None
        i += 1
    return blocks


def symbol_name(block: str) -> str:
    m = re.match(r'\(symbol "([^"]+)"', block)
    return m.group(1) if m else ""
