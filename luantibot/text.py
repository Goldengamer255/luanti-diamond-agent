"""Luanti's in-band text escapes.

Server text can carry markup for translation and colour:

    \\x1b(T@textdomain) ... \\x1bE     translation block
    \\x1b(c@#RRGGBB)                  colour
    \\x1bF                            reset

Bots usually want the plain words, so this strips both the parenthesised and
single-character forms.
"""
ESC = "\x1b"


def strip_escapes(text: str) -> str:
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != ESC:
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        if text[i] == "(":                 # \x1b( ... )
            depth = 0
            while i < n:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
        else:
            i += 1                         # \x1b<single char>
    return "".join(out)
