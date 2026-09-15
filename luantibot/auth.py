"""Luanti login: mechanism selection and the legacy password hash."""
import base64
import hashlib

# From src/network/networkprotocol.h
AUTH_MECHANISM_NONE = 0
AUTH_MECHANISM_LEGACY_PASSWORD = 1 << 0   # SRP keyed on the old sha1 hash
AUTH_MECHANISM_SRP = 1 << 1               # SRP keyed on the password itself
AUTH_MECHANISM_FIRST_SRP = 1 << 2         # no account yet: upload a verifier

AUTH_NAMES = {
    AUTH_MECHANISM_LEGACY_PASSWORD: "LEGACY_PASSWORD",
    AUTH_MECHANISM_SRP: "SRP",
    AUTH_MECHANISM_FIRST_SRP: "FIRST_SRP",
}


def translate_password(name: str, password: str, pad: bool = False) -> str:
    """Luanti's legacy hash: base64(sha1(name + password)), unpadded.

    src/util/base64.cpp drops the '=' padding the reference implementation
    emits, so a 20-byte digest encodes to 27 characters rather than 28.
    """
    if not password:
        return ""
    digest = hashlib.sha1((name + password).encode("utf-8")).digest()
    out = base64.b64encode(digest).decode("ascii")
    return out if pad else out.rstrip("=")


def describe_mechs(mask: int) -> str:
    got = [n for bit, n in AUTH_NAMES.items() if mask & bit]
    return "|".join(got) if got else f"NONE(0x{mask:x})"
