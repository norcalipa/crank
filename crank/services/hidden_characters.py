# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Characters that render as nothing (control, format, default-ignorable code points)."""

from __future__ import annotations

import unicodedata

HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

# Unicode Default_Ignorable_Code_Point (DerivedCoreProperties.txt); the letter and
# mark members are the ones the general category alone does not catch.
_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def is_hidden_character(char: str) -> bool:
    code = ord(char)
    return unicodedata.category(char) in HIDDEN_CATEGORIES or any(
        low <= code <= high for low, high in _DEFAULT_IGNORABLE_RANGES
    )
