"""DVD constants the calibration depends on.

A DVD sector holds 2048 user bytes, and the reads the ladder measures always cover one
whole aligned 16-sector ECC block, so that every read ends at the same point of the
error-correction interleave: the completion time then marks the end of a block rather
than somewhere inside one.
"""

from __future__ import annotations

SECTOR_BYTES = 2048
BLOCK_SECTORS = 16
