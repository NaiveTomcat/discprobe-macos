"""Random-data DVD image for full-disc timing calibration (`geo`).

Pure single-byte textures make the GH70N retry and change speed, so the ladder needs
a disc that reads like ordinary data. Seeded random bytes everywhere, plus the empty
ISO 9660 header that burning tools require.
"""

from __future__ import annotations

import os
import random

from . import dvd, iso9660

DEFAULT_SECTORS = 2295104  # fits DVD+R and DVD-R single layer
CHUNK_SECTORS = 8192


def write_iso(path: str, sectors: int = DEFAULT_SECTORS, seed: int = 1, log=print) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    rng = random.Random(seed)
    done, next_report = 0, 0.0
    with open(path, "wb") as f:
        while done < sectors:
            n = min(CHUNK_SECTORS, sectors - done)
            f.write(rng.randbytes(n * dvd.SECTOR_BYTES))
            done += n
            if done / sectors >= next_report:
                log(f"  {100 * done / sectors:5.1f} %")
                next_report += 0.2
    iso9660.add(path, sectors, "DISCPROBE_RANDOM")
