"""Romanian numbers written in words -> digits, before translation (#39).

The translation model got "o sută doisprezece" (112, the emergency number) wrong as 122 in every
recording; a number it only has to copy cannot change. Only unambiguous number words are converted:
"o"/"un" only before "sută"/"mie", "sută"/"mie" only after a count ("la sută" stays), "nouă" only
inside a larger number ("o casă nouă" stays), and "a doua" is the ordinal ("a doua zi", the next day). The Transcript itself is never changed: only the text
sent to the translation.
"""
import re
import unicodedata

_UNITS = {"unu": 1, "doi": 2, "doua": 2, "trei": 3, "patru": 4, "cinci": 5, "sase": 6, "sapte": 7, "opt": 8, "noua": 9}
_TEENS = {"zece": 10, "unsprezece": 11, "doisprezece": 12, "douasprezece": 12, "treisprezece": 13, "paisprezece": 14,
          "cincisprezece": 15, "saisprezece": 16, "saptesprezece": 17, "optsprezece": 18, "nouasprezece": 19}
_TENS = {"douazeci": 20, "treizeci": 30, "patruzeci": 40, "cincizeci": 50, "saizeci": 60, "saptezeci": 70,
         "optzeci": 80, "nouazeci": 90}
_HUNDRED, _THOUSAND = {"suta", "sute"}, {"mie", "mii"}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _key(word: str) -> str:
    return unicodedata.normalize("NFKD", word.lower()).encode("ascii", "ignore").decode()


def _value_words(word: str) -> str | None:
    k = _key(word)
    if k in _UNITS or k in _TEENS or k in _TENS:
        return "number"
    if k in _HUNDRED or k in _THOUSAND:
        return "scale"
    if k in ("o", "un", "una"):
        return "one"
    if k == "si":
        return "and"
    if k == "de":
        return "of"
    return None


def _parse(words: list[str]) -> int | None:
    """Value of a run of number words, or None if it is not a well-formed number."""
    total = current = 0
    last = None
    for word in words:
        k = _key(word)
        if k in ("si", "de"):
            continue
        if k in ("o", "un", "una"):
            current += 1
        elif k in _UNITS:
            current += _UNITS[k]
        elif k in _TEENS:
            current += _TEENS[k]
        elif k in _TENS:
            current += _TENS[k]
        elif k in _HUNDRED:
            if current == 0:
                return None
            current *= 100
        elif k in _THOUSAND:
            if current == 0:
                return None
            total += current * 1000
            current = 0
        last = k
    if last is None:
        return None
    return total + current


def numbers_to_digits(text: str) -> str:
    """Replace each run of Romanian number words by its digits; everything else is kept as written."""
    tokens = [(m.start(), m.end(), m.group(0)) for m in _WORD.finditer(text)]
    out, pos, i = [], 0, 0
    while i < len(tokens):
        ordinal = i > 0 and _key(tokens[i - 1][2]) in ("a", "al") and _key(tokens[i][2]) in ("doua", "doi")
        j = i if ordinal else _run_end(tokens, i, text)  # "a doua zi" is "the next day", not 2
        if j > i:
            start, end = tokens[i][0], tokens[j - 1][1]
            value = _parse([t[2] for t in tokens[i:j]])
            if value is not None:
                out.append(text[pos:start])
                out.append(str(value))
                pos = end
                i = j
                continue
        i += 1
    out.append(text[pos:])
    return "".join(out)


def _run_end(tokens: list[tuple[int, int, str]], i: int, text: str) -> int:
    """Index after the longest run of number words starting at i (i itself when none starts there)."""
    j, seen_number, prev = i, False, None
    while j < len(tokens):
        kind = _value_words(tokens[j][2])
        if j > i and text[tokens[j - 1][1]:tokens[j][0]].strip():  # punctuation between words ends the run
            break
        nxt = _value_words(tokens[j + 1][2]) if j + 1 < len(tokens) else None
        if kind == "one":
            if nxt != "scale":  # "o sută", "un milion"... but "o casă", "un patru" stay
                break
        elif kind == "number":
            if _key(tokens[j][2]) == "noua" and not seen_number and nxt not in ("scale", "and") and prev != "and":
                break  # "nouă" alone is usually the adjective "new"
            seen_number = True
        elif kind == "scale":
            if prev not in ("number", "one", "of"):
                break  # "la sută" is a percent sign, not 100
            seen_number = True
        elif kind == "and":
            if not seen_number or nxt != "number":
                break
        elif kind == "of":
            if not seen_number or nxt != "scale":  # "nouăzeci și cinci de mii", not "cinci de licențe"
                break
        else:
            break
        prev = kind
        j += 1
    while j > i and _value_words(tokens[j - 1][2]) in ("and", "of", "one"):
        j -= 1
    return j if seen_number else i
