#!/usr/bin/env python3
"""Recover Luanti protocol command ids from the client binary.

Luanti defines its packet tables as arrays indexed by command id whose first
member is the command name, e.g.

    const ToClientCommandHandler toClientCommandTable[TOCLIENT_NUM_MSG_TYPES] = {
        { "TOCLIENT_HELLO", TOSERVER_STATE_NOT_CONNECTED, &Client::handleCommand_Hello },
        ...

so the array order *is* the wire id. This walks the name pointers stored in the
binary and reconstructs the tables exactly, rather than trusting documentation.
"""
import struct, sys, collections, json

PATH = sys.argv[1] if len(sys.argv) > 1 else "/usr/bin/luanti"
d = open(PATH, "rb").read()
assert d[:4] == b"\x7fELF" and d[4] == 2

e_shoff, = struct.unpack_from("<Q", d, 0x28)
e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", d, 0x3a)
secs = []
for i in range(e_shnum):
    o = e_shoff + i * e_shentsize
    nm, st, fl, addr, off, size, link, info, al, ent = struct.unpack_from("<IIQQQQIIQQ", d, o)
    secs.append(dict(nm=nm, st=st, addr=addr, off=off, size=size))
shstr = secs[e_shstrndx]
for s in secs:
    b = shstr["off"] + s["nm"]
    s["name"] = d[b:d.index(b"\0", b)].decode()

# vaddr -> command name  (vaddr == file offset here, but keep it general)
names = {}
for s in secs:
    if s["st"] == 8 or not s["size"]:
        continue
    blob = d[s["off"]:s["off"] + s["size"]]
    for key in (b"TOCLIENT_", b"TOSERVER_"):
        start = 0
        while True:
            i = blob.find(key, start)
            if i < 0:
                break
            end = blob.index(b"\0", i)
            if i == 0 or blob[i - 1] == 0:
                names[s["addr"] + i] = blob[i:end].decode()
            start = end + 1

# locate every stored 8-byte pointer to one of those strings
ptr_at = {}
for va, nm in names.items():
    pat = struct.pack("<Q", va)
    start = 0
    while True:
        j = d.find(pat, start)
        if j < 0:
            break
        if j % 8 == 0:
            ptr_at[j] = nm
        start = j + 1

tables = collections.defaultdict(dict)
for prefix in ("TOCLIENT_", "TOSERVER_"):
    pts = sorted(o for o, nm in ptr_at.items() if nm.startswith(prefix))
    # split into contiguous tables: a gap much larger than the stride starts a new one
    groups, cur = [], [pts[0]]
    for a, b in zip(pts, pts[1:]):
        (cur.append(b) if b - a <= 64 else (groups.append(cur), cur := [b]))
    groups.append(cur)
    for gi, g in enumerate(groups):
        if len(g) < 8:
            continue
        stride = collections.Counter(b - a for a, b in zip(g, g[1:])).most_common(1)[0][0]
        base = g[0]
        t = {(o - base) // stride: ptr_at[o] for o in g if (o - base) % stride == 0}
        tables[f"{prefix}#{gi}"] = dict(stride=stride, table=t)

out = {}
for key, info in tables.items():
    t = info["table"]
    print(f"\n=== {key}  stride={info['stride']}  entries={len(t)}  max_id=0x{max(t):02X} ===")
    for i in sorted(t):
        print(f"  0x{i:02X}  {t[i]}")
    out[key] = {str(k): v for k, v in t.items()}

json.dump(out, open("protocol_ids.json", "w"), indent=1)
