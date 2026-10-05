## `td0.py` — TeleDisk (.TD0) inspector and converter

Parses uncompressed (`TD`) TeleDisk images, verifies header / comment /
sector CRCs, reports geometry and read damage, and writes a flat raw
sector image.

### Options

```bash
python3 td0.py info <diskimage.TD0>
python3 td0.py dump <diskimage.TD0> <out.img>
```

`dump` orders sectors by cylinder > head > sector number and fills any
sector missing from the image with `0xF6`, reporting each one.

Advanced-compression (`td`) images are detected and reported, but the LZSS
stage is not implemented.
