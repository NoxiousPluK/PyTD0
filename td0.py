#!/usr/bin/env python3
"""TeleDisk (.TD0) inspector / converter.

Reads an uncompressed ("TD") TeleDisk image, verifies its structure and
CRCs, and optionally writes a raw sector image (.IMG) suitable for
dd-ing to a physical floppy.

Advanced-compression ("td") images are detected and reported but not
decoded (LZSS stage not implemented).

Usage:
    td0.py info  <image.td0>
    td0.py dump  <image.td0> <output.img>
"""

import sys
import struct

SIZECODE = {0: 128, 1: 256, 2: 512, 3: 1024, 4: 2048, 5: 4096, 6: 8192}

DRIVE_TYPE = {
    0: '5.25" 96tpi in 48tpi drive',
    1: '5.25" 48tpi',
    2: '5.25" 96tpi',
    3: '3.5" 135tpi',
    4: '3.5" 135tpi (alt)',
    5: '8"',
    6: '3.5" 135tpi',
}

DATA_RATE = {0: "250 kbps (DD)", 1: "300 kbps (DD in HD drive)", 2: "500 kbps (HD)"}


def crc16(data, crc=0):
    """TeleDisk CRC-16/CCITT, poly 0xA097, init 0."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0xA097) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Reader:
    def __init__(self, buf):
        self.buf = buf
        self.pos = 0

    def take(self, n):
        if self.pos + n > len(self.buf):
            raise EOFError(f"unexpected end of file at {self.pos}")
        chunk = self.buf[self.pos:self.pos + n]
        self.pos += n
        return chunk


def decode_data_block(r, size):
    """Read one sector data block and return `size` bytes."""
    (blen,) = struct.unpack("<H", r.take(2))
    block = r.take(blen)
    method = block[0]
    payload = block[1:]

    if method == 0:                      # raw
        out = payload
    elif method == 1:                     # repeated 2-byte pattern
        count, = struct.unpack("<H", payload[:2])
        out = payload[2:4] * count
    elif method == 2:                     # RLE of literal / pattern runs
        out = bytearray()
        i = 0
        while i < len(payload):
            kind = payload[i]
            if kind == 0:
                n = payload[i + 1]
                out += payload[i + 2:i + 2 + n]
                i += 2 + n
            elif kind == 1:
                n = payload[i + 1]
                out += payload[i + 2:i + 4] * n
                i += 4
            else:
                raise ValueError(f"bad RLE run type {kind}")
        out = bytes(out)
    else:
        raise ValueError(f"unknown data encoding method {method}")

    if len(out) != size:
        raise ValueError(f"decoded {len(out)} bytes, expected {size}")
    return out


def parse(path):
    buf = open(path, "rb").read()
    r = Reader(buf)
    head = r.take(12)
    sig = head[0:2].decode("latin-1")
    if sig not in ("TD", "td"):
        raise ValueError(f"not a TeleDisk image (signature {sig!r})")

    info = {
        "signature": sig,
        "advanced_compression": sig == "td",
        "sequence": head[2],
        "checksig": head[3],
        "version": head[4],
        "data_rate": head[5],
        "drive_type": head[6],
        "stepping": head[7],
        "dos_alloc": head[8],
        "sides": head[9],
        "header_crc_ok": crc16(head[:10]) == struct.unpack("<H", head[10:12])[0],
        "comment": None,
        "date": None,
        "warnings": [],
        "tracks": [],
    }

    if head[7] & 0x80:
        chead = r.take(10)
        ccrc, clen = struct.unpack("<HH", chead[:4])
        body = r.take(clen)
        info["comment_crc_ok"] = crc16(chead[2:] + body) == ccrc
        info["date"] = (1900 + chead[4], chead[5] + 1, chead[6], chead[7], chead[8], chead[9])
        info["comment"] = body.rstrip(b"\x00").replace(b"\x00", b"\n").decode("latin-1")

    if info["advanced_compression"]:
        info["warnings"].append("advanced compression: track data not decoded")
        return info, {}

    sectors = {}   # (cyl, head, sec) -> bytes
    while True:
        th = r.take(4)
        if th[0] == 0xFF:
            break
        nsec, cyl, hd = th[0], th[1], th[2]
        if crc16(th[:3]) & 0xFF != th[3]:
            info["warnings"].append(f"track {cyl}/{hd}: header CRC mismatch")
        track = {"cyl": cyl, "head": hd & 0x3F, "nsec": nsec, "sectors": []}
        for _ in range(nsec):
            sh = r.take(6)
            scyl, shd, snum, scode, flags, scrc = sh
            size = SIZECODE[scode]
            data = None
            if not (flags & 0x30):
                data = decode_data_block(r, size)
                if crc16(data) & 0xFF != scrc:
                    info["warnings"].append(
                        f"sector {scyl}/{shd}/{snum}: data CRC mismatch")
            track["sectors"].append(
                {"cyl": scyl, "head": shd, "sec": snum, "size": size,
                 "flags": flags, "has_data": data is not None})
            if flags & 0x02:
                info["warnings"].append(f"sector {scyl}/{shd}/{snum}: CRC error flag set")
            key = (cyl, hd & 0x3F, snum)
            if key in sectors:
                info["warnings"].append(f"duplicate sector {key}")
            sectors[key] = data
        info["tracks"].append(track)
    info["trailing_bytes"] = len(buf) - r.pos

    # Flag sectors absent from the image entirely (a short track means the
    # original read failed on them).
    cyls = sorted({t["cyl"] for t in info["tracks"]})
    heads = sorted({t["head"] for t in info["tracks"]})
    nsec = max(t["nsec"] for t in info["tracks"])
    info["missing"] = [(c, h, s)
                       for c in cyls for h in heads for s in range(1, nsec + 1)
                       if (c, h, s) not in sectors]
    for key in info["missing"]:
        info["warnings"].append(f"sector {key[0]}/{key[1]}/{key[2]}: absent from image")
    return info, sectors


def show(info, sectors):
    print(f"signature            : {info['signature']}"
          f"{'  (ADVANCED COMPRESSION)' if info['advanced_compression'] else '  (uncompressed)'}")
    print(f"TeleDisk version     : {info['version'] // 10}.{info['version'] % 10}")
    print(f"header CRC           : {'ok' if info['header_crc_ok'] else 'BAD'}")
    print(f"data rate            : {DATA_RATE.get(info['data_rate'] & 3, '?')}")
    print(f"drive type           : {DRIVE_TYPE.get(info['drive_type'], '?')}")
    print(f"sides recorded       : {info['sides']}")
    if info["comment"] is not None:
        d = info["date"]
        print(f"comment CRC          : {'ok' if info['comment_crc_ok'] else 'BAD'}")
        print(f"created              : {d[0]:04d}-{d[1]:02d}-{d[2]:02d} "
              f"{d[3]:02d}:{d[4]:02d}:{d[5]:02d}")
        print(f"comment              : {info['comment']}")
    if not info["tracks"]:
        return

    cyls = sorted({t["cyl"] for t in info["tracks"]})
    heads = sorted({t["head"] for t in info["tracks"]})
    geoms = sorted({(t["nsec"], tuple(sorted({s["size"] for s in t["sectors"]})))
                    for t in info["tracks"]})
    total = sum(s["size"] for t in info["tracks"] for s in t["sectors"])
    print(f"tracks in image      : {len(info['tracks'])}")
    print(f"cylinders            : {len(cyls)} ({cyls[0]}..{cyls[-1]})")
    print(f"heads                : {heads}")
    for nsec, sizes in geoms:
        print(f"geometry             : {nsec} sectors/track of {sizes} bytes")
    print(f"total sector data    : {total} bytes ({total/1024:.0f} KiB)")
    print(f"trailing bytes       : {info['trailing_bytes']}")
    print(f"sectors present      : {len(sectors)}")
    print(f"sectors absent       : {len(info['missing'])}")
    if info["warnings"]:
        print(f"warnings ({len(info['warnings'])}):")
        for w in info["warnings"][:20]:
            print(f"  - {w}")
        if len(info["warnings"]) > 20:
            print(f"  ... {len(info['warnings']) - 20} more")
    else:
        print("warnings             : none")


def dump(info, sectors, out, fill=0xF6):
    keys = sorted(sectors)
    cyls = sorted({c for c, _, _ in keys})
    heads = sorted({h for _, h, _ in keys})
    secs = sorted({s for _, _, s in keys})
    data = bytearray()
    for c in cyls:
        for h in heads:
            for s in secs:
                blk = sectors.get((c, h, s))
                if blk is None:
                    size = 512
                    print(f"  filling absent sector {c}/{h}/{s} "
                          f"with {size} x 0x{fill:02X}")
                    blk = bytes([fill]) * size
                data += blk
    open(out, "wb").write(data)
    print(f"wrote {out}: {len(data)} bytes "
          f"({len(cyls)} cyl x {len(heads)} head x {len(secs)} sec)")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd, path = sys.argv[1], sys.argv[2]
    info, sectors = parse(path)
    if cmd == "info":
        show(info, sectors)
    elif cmd == "dump":
        if len(sys.argv) < 4:
            print("dump needs an output path")
            return 1
        show(info, sectors)
        print()
        dump(info, sectors, sys.argv[3])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
