from __future__ import annotations

import io
import struct
from typing import NamedTuple

import pytest

from dissect.volume.raid.raid import PhysicalDisk, VirtualDisk
from dissect.volume.raid.stream import Level

CHUNK_SIZE = 0x1000
"""Stripe (chunk) size of the synthetic RAID0 sets."""

SIGNATURE_SIZE = 16
"""Size of the provenance record stored at the start of every aligned block of member data."""

SIGNATURE_MAGIC = b"RVOL"

MEMBER_PREFIX = 0x2000
"""Bytes in front of the member data, mimicking the metadata area of a real member disk."""


class Zone(NamedTuple):
    """A single strip zone of a RAID0 set with differently sized members."""

    start: int
    end: int
    members: tuple[int, ...]
    member_start: int


def _signature(member: int, offset: int) -> bytes:
    """Build a value that identifies the member and member offset a block of data belongs to."""
    return struct.pack("<4sIII", SIGNATURE_MAGIC, member, offset, (member * 31 + offset) & 0xFFFFFFFF)


def _read_signature(data: bytes) -> tuple[int, int]:
    """Decode a signature, returning the member and member offset it describes."""
    magic, member, offset, _ = struct.unpack("<4sIII", data)
    assert magic == SIGNATURE_MAGIC
    return member, offset


def _build_member(member: int, size: int) -> bytes:
    """Build member data where every aligned block carries its own provenance."""
    data = bytearray(size)
    for offset in range(0, size, SIGNATURE_SIZE):
        data[offset : offset + SIGNATURE_SIZE] = _signature(member, offset)
    return bytes(data)


def _build_zones(sizes: list[int]) -> list[Zone]:
    """Construct the strip zones for a set of member data sizes.

    Each step in unique member size opens a new zone that starts at the same offset on every
    member that is large enough to reach it. Array data therefore continues in the tail of the
    larger members after the smallest member has been exhausted.
    """
    zones = []
    zone_start = 0
    member_start = 0
    for size in sorted(set(sizes)):
        if size == member_start:
            continue

        members = tuple(idx for idx, member_size in enumerate(sizes) if member_size >= size)
        zone_end = zone_start + (size - member_start) * len(members)
        zones.append(Zone(zone_start, zone_end, members, member_start))
        zone_start = zone_end
        member_start = size

    return zones


def _locate(zones: list[Zone], offset: int) -> tuple[int, int]:
    """Return the member and member offset that back a logical ``offset``."""
    for zone in zones:
        if offset < zone.end:
            local = offset - zone.start
            stripe, offset_in_stripe = divmod(local, CHUNK_SIZE)
            chunk = local // (CHUNK_SIZE * len(zone.members))
            member = zone.members[stripe % len(zone.members)]
            return member, zone.member_start + chunk * CHUNK_SIZE + offset_in_stripe
    raise ValueError(f"Offset {offset:#x} is not backed by any member")


def _expected(members: list[bytes], zones: list[Zone], offset: int, length: int) -> bytes:
    """Collect the member bytes that a correct logical read should return."""
    end = min(offset + length, zones[-1].end)
    result = bytearray()
    while offset < end:
        member, member_offset = _locate(zones, offset)
        size = min(end - offset, CHUNK_SIZE - offset % CHUNK_SIZE)
        result += members[member][member_offset : member_offset + size]
        offset += size
    return bytes(result)


def _open_raid0(sizes: list[int]) -> tuple[VirtualDisk, list[bytes]]:
    """Open a synthetic RAID0 set with member data sizes of ``sizes`` bytes."""
    members = [_build_member(idx, size) for idx, size in enumerate(sizes)]
    disks = {
        idx: (0, PhysicalDisk(io.BytesIO(bytes(MEMBER_PREFIX) + member), MEMBER_PREFIX, len(member)))
        for idx, member in enumerate(members)
    }
    total = sum((len(member) // CHUNK_SIZE) * CHUNK_SIZE for member in members)

    return VirtualDisk("test", "test", total, Level.RAID0, 0, CHUNK_SIZE, len(members), disks), members


SIZES = [
    pytest.param([6 * CHUNK_SIZE, 6 * CHUNK_SIZE, 4 * CHUNK_SIZE], id="one_small_member"),
    pytest.param([6 * CHUNK_SIZE, 5 * CHUNK_SIZE, 4 * CHUNK_SIZE], id="three_distinct_sizes"),
    pytest.param([6 * CHUNK_SIZE, 4 * CHUNK_SIZE, 4 * CHUNK_SIZE], id="two_small_members"),
    pytest.param([8 * CHUNK_SIZE, 6 * CHUNK_SIZE, 4 * CHUNK_SIZE, 4 * CHUNK_SIZE], id="single_large_member"),
]


@pytest.mark.parametrize("sizes", SIZES)
def test_raid0_tail_zone_reads_member_tail(sizes: list[int]) -> None:
    """Bytes in a tail zone come from the tail of the larger members, not from their head."""
    vd, members = _open_raid0(sizes)
    zones = _build_zones(sizes)
    fh = vd.open()

    tail = zones[-1]
    assert tail.member_start > 0

    for offset in range(tail.start, tail.end, CHUNK_SIZE):
        member, member_offset = _locate(zones, offset)
        actual = fh.readoffset(offset, CHUNK_SIZE)

        # The read is exactly what the member stores at the offset the logical offset maps to.
        assert actual == members[member][member_offset : member_offset + CHUNK_SIZE]
        assert _read_signature(actual[:SIGNATURE_SIZE]) == (member, member_offset)

        # The data lives in the tail of the member, so it is not the start of that member.
        # A read that restarts at member offset zero would attribute the wrong bytes to this offset.
        assert member_offset >= CHUNK_SIZE
        assert actual != members[member][:CHUNK_SIZE]


@pytest.mark.parametrize("sizes", SIZES)
def test_raid0_tail_zone_keeps_stripe_order(sizes: list[int]) -> None:
    """Every tail zone continues the stripe order that was written to the members."""
    vd, members = _open_raid0(sizes)
    zones = _build_zones(sizes)
    fh = vd.open()

    for zone in zones[1:]:
        for index, offset in enumerate(range(zone.start, zone.end, CHUNK_SIZE)):
            member = zone.members[index % len(zone.members)]
            member_offset = zone.member_start + (index // len(zone.members)) * CHUNK_SIZE

            expected = members[member][member_offset : member_offset + CHUNK_SIZE]
            assert fh.readoffset(offset, CHUNK_SIZE) == expected
            assert _read_signature(expected[:SIGNATURE_SIZE]) == (member, member_offset)


@pytest.mark.parametrize("sizes", SIZES)
def test_raid0_sequential_read_matches_members(sizes: list[int]) -> None:
    """A full sequential read returns exactly the member bytes, across every zone boundary."""
    vd, members = _open_raid0(sizes)
    zones = _build_zones(sizes)
    fh = vd.open()

    data = fh.read(vd.size)

    assert len(data) == vd.size
    assert data == _expected(members, zones, 0, vd.size)


@pytest.mark.parametrize("sizes", SIZES)
def test_raid0_partial_reads_crossing_zone_boundary(sizes: list[int]) -> None:
    """Unaligned reads that cross a zone boundary return the exact member bytes, unpadded."""
    vd, members = _open_raid0(sizes)
    zones = _build_zones(sizes)
    fh = vd.open()

    for zone in zones[1:]:
        # Start two bytes before the zone boundary and read well into the new zone.
        start = zone.start - 2
        length = min(CHUNK_SIZE + 5, vd.size - start)

        actual = fh.readoffset(start, length)

        assert len(actual) == length
        assert actual == _expected(members, zones, start, length)
        assert _read_signature(actual[2 : 2 + SIGNATURE_SIZE]) == _locate(zones, zone.start)


@pytest.mark.parametrize("sizes", SIZES)
def test_raid0_tail_bytes_are_not_zero_padded(sizes: list[int]) -> None:
    """Tail data is real member content with provenance, not fabricated zeros."""
    vd, members = _open_raid0(sizes)
    zones = _build_zones(sizes)
    fh = vd.open()

    tail = zones[-1]
    actual = fh.readoffset(tail.start, tail.end - tail.start)

    assert len(actual) == tail.end - tail.start
    assert actual == _expected(members, zones, tail.start, len(actual))

    # Every chunk carries a signature, so neither zero padding nor a zero-filled member can
    # satisfy the read: the bytes must equal the member tail and must contain the signature.
    for offset in range(0, len(actual), CHUNK_SIZE):
        chunk = actual[offset : offset + CHUNK_SIZE]
        member, member_offset = _locate(zones, tail.start + offset)
        assert chunk == members[member][member_offset : member_offset + CHUNK_SIZE]
        assert _read_signature(chunk[:SIGNATURE_SIZE]) == (member, member_offset)
        assert chunk != bytes(CHUNK_SIZE)


@pytest.mark.parametrize("sizes", SIZES)
def test_raid0_valid_tail_bytes_are_not_truncated(sizes: list[int]) -> None:
    """The final bytes of the volume are valid member data and are returned in full."""
    vd, members = _open_raid0(sizes)
    zones = _build_zones(sizes)
    fh = vd.open()

    # A read that starts inside the volume but runs past the end only returns the valid bytes.
    start = vd.size - SIGNATURE_SIZE
    actual = fh.readoffset(start, 2 * CHUNK_SIZE)

    assert len(actual) == SIGNATURE_SIZE
    assert actual == _expected(members, zones, start, SIGNATURE_SIZE)
    assert _read_signature(actual) == _locate(zones, start)

    # Reading chunk-wise up to the end never shortens or zero pads a chunk.
    for offset in range(vd.size - CHUNK_SIZE, vd.size, CHUNK_SIZE):
        chunk = fh.readoffset(offset, CHUNK_SIZE)
        assert len(chunk) == CHUNK_SIZE
        assert chunk == _expected(members, zones, offset, CHUNK_SIZE)


@pytest.mark.parametrize("sizes", SIZES)
def test_raid0_reads_beyond_volume_return_nothing(sizes: list[int]) -> None:
    """Reads at or past the end of the volume do not present content as if it were data."""
    vd, _members = _open_raid0(sizes)
    fh = vd.open()

    assert fh.readoffset(vd.size, CHUNK_SIZE) == b""
    assert fh.readoffset(vd.size + CHUNK_SIZE, CHUNK_SIZE) == b""

    fh.seek(vd.size)
    assert fh.read(CHUNK_SIZE) == b""
    assert fh.tell() == vd.size
