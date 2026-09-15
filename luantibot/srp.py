"""SRP-6a client, matching Luanti's bundled csrp-gmp (SHA-256, RFC 5054 2048-bit).

Luanti authenticates with SRP so the password never crosses the wire. The exact
hashing details below mirror src/util/srp.cpp:

    x = H(salt || H(username_verifier || ":" || password))
    k = H(N || PAD(g))
    u = H(PAD(A) || PAD(B))
    S = (B - k * g^x) ^ (a + u*x)  mod N
    K = H(S)
    M = H(H(N) xor H(g) || H(username) || salt || A || B || K)

Note csrp-gmp hashes bare numbers (H(N), H(g), K) in *minimal* big-endian form,
while PAD() in u and k left-pads to the byte length of N.
"""
import hashlib
import os

# RFC 5054, 2048-bit group -- SRP_NG_2048 in Luanti.
N_HEX = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050"
    "A37329CBB4A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50"
    "E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B8"
    "55F97993EC975EEAA80D740ADBF4FF747359D041D5C33EA71D281E446B14773B"
    "CA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748"
    "544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB37861602790"
    "04E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8"
    "E9DBFBB694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F"
    "9E4AFF73"
)
N = int(N_HEX, 16)
g = 2
N_LEN = (N.bit_length() + 7) // 8


def _H(*chunks: bytes) -> bytes:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return h.digest()


def _int2bytes(v: int) -> bytes:
    """Minimal big-endian encoding, as csrp-gmp's mpz_export produces."""
    if v == 0:
        return b"\x00"
    return v.to_bytes((v.bit_length() + 7) // 8, "big")


def _pad(v: int) -> bytes:
    """Left-pad to the byte length of N."""
    return v.to_bytes(N_LEN, "big")


def _bytes2int(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _hash_num(v: int) -> bytes:
    return _H(_int2bytes(v))


def compute_x(salt: bytes, username_verifier: str, password: str) -> int:
    inner = _H(username_verifier.encode("utf-8"), b":", password.encode("utf-8"))
    return _bytes2int(_H(salt, inner))


def compute_verifier(salt: bytes, username_verifier: str, password: str) -> bytes:
    """v = g^x mod N -- needed to register a new account (FIRST_SRP)."""
    return _int2bytes(pow(g, compute_x(salt, username_verifier, password), N))


def create_verifier(username_verifier: str, password: str, salt_len: int = 16):
    salt = os.urandom(salt_len)
    return salt, compute_verifier(salt, username_verifier, password)


class SRPUser:
    """Client side of one SRP exchange."""

    def __init__(self, username: str, password: str, username_verifier=None):
        # Luanti hashes the verifier against the lowercased name but proves
        # identity with the name as typed.
        self.username = username
        self.username_verifier = (username_verifier if username_verifier is not None
                                  else username.lower())
        self.password = password
        self.a = 0
        self.A = 0
        self.M = None
        self.K = None
        self._expected_hamk = None

    def start_authentication(self) -> bytes:
        """Pick a random `a`, return the A bytes to send."""
        while True:
            self.a = _bytes2int(os.urandom(32))
            self.A = pow(g, self.a, N)
            if self.A % N != 0:
                break
        return _int2bytes(self.A)

    def process_challenge(self, salt: bytes, B_bytes: bytes) -> bytes:
        """Consume the server's (s, B); return proof M, or raise on a bad B."""
        B = _bytes2int(B_bytes)
        if B % N == 0:
            raise ValueError("SRP: server sent B ≡ 0 mod N")

        k = _bytes2int(_H(_int2bytes(N), _pad(g)))
        u = _bytes2int(_H(_pad(self.A), _pad(B)))
        if u == 0:
            raise ValueError("SRP: u == 0")

        x = compute_x(salt, self.username_verifier, self.password)
        # S = (B - k*g^x) ^ (a + u*x) mod N
        S = pow((B - k * pow(g, x, N)) % N, self.a + u * x, N)
        self.K = _hash_num(S)

        h_n, h_g = _hash_num(N), _hash_num(g)
        h_xor = bytes(a ^ b for a, b in zip(h_n, h_g))
        h_i = _H(self.username.encode("utf-8"))
        self.M = _H(h_xor, h_i, salt, _int2bytes(self.A), B_bytes, self.K)
        self._expected_hamk = _H(_int2bytes(self.A), self.M, self.K)
        return self.M

    def verify_session(self, hamk: bytes) -> bool:
        """Check the server's H(A|M|K) reply -- proves the server knew v."""
        return self._expected_hamk is not None and hamk == self._expected_hamk
