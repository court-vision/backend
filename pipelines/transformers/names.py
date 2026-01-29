"""
Name Transformers

Utilities for normalizing player names for matching across data sources.
"""

import unicodedata


def normalize_name(name: str) -> str:
    """
    Normalize a name by removing diacritics and converting to lowercase.

    This allows matching player names across different data sources
    that may have different character encodings or formatting.

    Examples:
        >>> normalize_name("Nikola Jokić")
        'nikola jokic'
        >>> normalize_name("LeBron James")
        'lebron james'
        >>> normalize_name("Luka Dončić")
        'luka doncic'
    """
    # Decompose unicode characters (e.g., é → e + combining accent)
    normalized = unicodedata.normalize("NFD", name)

    # Remove combining diacritical marks
    ascii_name = "".join(c for c in normalized if unicodedata.category(c) != "Mn")

    return ascii_name.lower().strip()
