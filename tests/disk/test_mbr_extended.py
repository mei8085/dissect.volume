from __future__ import annotations

import io
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from dissect.volume import disk
from dissect.volume.disk.schemes import MBR
from dissect.volume.disk.schemes.mbr import c_mbr

if TYPE_CHECKING:
    from dissect.volume.disk.partition import Partition

# An extended partition chain mixes two different offset bases:
#
# * a logical partition's ``sector_ofs`` is relative to the extended MBR (EBR) sector that describes it, and
# * the link to the next EBR has its ``sector_ofs`` relative to the first EBR, which is the start of the extended
#   partition.
#
# The first EBR is itself the extended partition base, so both bases coincide there and only diverge from the
# second link onwards. The image below makes that divergence visible in the bytes on disk: every partition range is
# labelled at its first and last sector, and a decoy table sits where a link offset would land if it was anchored
# at the EBR that holds it. That decoy's partition covers sectors of a real partition, so a parser using the wrong
# base would hand the same sectors out twice.

# Byte offset of the first partition entry within a table sector
ENTRY_TABLE_OFFSET = 0x1BE

EXTENDED_TYPES = (0x05, 0x0F, 0x85)  # all partition types dissect follows as an extended chain
SECTOR_SIZES = (512, 4096)

# All offsets below are in sectors of whatever sector size the disk uses; only the byte offsets depend on it
SECTOR_COUNT = 4096

PRIMARY_LBA = 256
PRIMARY_SECTORS = 256

EXTENDED_LBA = 768
EXTENDED_SECTORS = 3072


@dataclass(frozen=True, slots=True)
class ChainNode:
    """One link of an extended partition chain, with offsets as they are stored on disk."""

    ebr_lba: int
    data_offset: int  # relative to ebr_lba
    data_sectors: int
    link_offset: int | None  # relative to EXTENDED_LBA

    @property
    def data_lba(self) -> int:
        return self.ebr_lba + self.data_offset

    @property
    def next_ebr_lba(self) -> int | None:
        return None if self.link_offset is None else EXTENDED_LBA + self.link_offset


CHAIN = (
    ChainNode(768, 8, 256, 752),
    ChainNode(1520, 16, 128, 1264),
    ChainNode(2032, 256, 1024, None),
)

# The second link points at ``next_ebr_lba`` when read against the extended base, and here when read against the
# EBR that holds it. The decoy is a valid table whose partition lies inside the last real partition.
DECOY_LINK_LBA = CHAIN[1].ebr_lba + CHAIN[1].link_offset
DECOY_LINK_DATA_SECTORS = 128

# The last logical partition starts at ``data_lba`` when read against its own EBR, and here when read against the
# extended base. This sector lies inside the first logical partition.
DECOY_DATA_LBA = EXTENDED_LBA + CHAIN[2].data_offset


def _marker(tag: str) -> bytes:
    return tag.encode("ascii").ljust(16, b"\x00")


def _label(image: bytearray, lba: int, sectors: int, tag: str, sector_size: int) -> None:
    """Label the first and last sector of a range so a partition stream can be checked against the raw image."""
    start = lba * sector_size
    image[start : start + 16] = _marker(f"HEAD-{tag}")
    end = (lba + sectors) * sector_size
    image[end - 16 : end] = _marker(f"TAIL-{tag}")


def _entry(entry_type: int, sector_ofs: int, sector_size: int, bootable: int = 0) -> c_mbr.part:
    return c_mbr.part(bootable=bootable, type=entry_type, sector_ofs=sector_ofs, sector_size=sector_size)


def _table(entries: list[c_mbr.part], sector_size: int) -> bytes:
    """Build one table sector: the 512-byte MBR structure padded to the sector size."""
    parts = list(entries)
    parts.extend(c_mbr.part() for _ in range(4 - len(parts)))
    data = c_mbr.mbr(bootcode=b"\x00" * 0x1B8, part=parts, bootsig=0xAA55).dumps()
    return data.ljust(sector_size, b"\x00")


def _table_at(raw: bytes, lba: int, sector_size: int) -> c_mbr.mbr_s:
    fh = io.BytesIO(raw)
    fh.seek(lba * sector_size)
    return c_mbr.mbr_s(fh)


def build_image(extended_type: int, sector_size: int) -> bytes:
    """Build a disk with one primary partition followed by a chain of three logical partitions."""
    image = bytearray(SECTOR_COUNT * sector_size)

    _label(image, PRIMARY_LBA, PRIMARY_SECTORS, "p1", sector_size)
    for number, node in enumerate(CHAIN, start=2):
        _label(image, node.data_lba, node.data_sectors, f"p{number}", sector_size)

    image[0:sector_size] = _table(
        [
            _entry(0x83, PRIMARY_LBA, PRIMARY_SECTORS, bootable=0x80),
            _entry(extended_type, EXTENDED_LBA, EXTENDED_SECTORS),
        ],
        sector_size,
    )

    # Each EBR holds the logical partition it describes first, and the link to the next EBR (if any) second
    for node in CHAIN:
        entries = [_entry(0x83, node.data_offset, node.data_sectors)]
        if node.link_offset is not None:
            entries.append(_entry(extended_type, node.link_offset, EXTENDED_SECTORS - node.link_offset))
        start = node.ebr_lba * sector_size
        image[start : start + sector_size] = _table(entries, sector_size)

    # A real, readable EBR at the EBR-relative link target, declaring a partition that starts at its own sector
    start = DECOY_LINK_LBA * sector_size
    image[start : start + sector_size] = _table([_entry(0x83, 0, DECOY_LINK_DATA_SECTORS)], sector_size)

    return bytes(image)


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """A partition description that traces back to the raw partition table entry it was read from."""

    number: int
    anchor_lba: int  # the sector the stored offset is relative to
    sector_ofs: int
    sector_bytes: int
    size_sectors: int
    type: int

    @property
    def offset(self) -> int:
        return (self.anchor_lba + self.sector_ofs) * self.sector_bytes

    @property
    def size(self) -> int:
        return self.size_sectors * self.sector_bytes


def _entries(raw: bytes, lba: int, sector_size: int) -> list[tuple[int, int, int]]:
    """Read the four partition entries of a table sector, independently of the parser."""
    base = lba * sector_size + ENTRY_TABLE_OFFSET
    return [(raw[base + index * 16 + 4], *struct.unpack_from("<II", raw, base + index * 16 + 8)) for index in range(4)]


def independent_manifest(raw: bytes, sector_size: int) -> list[ManifestEntry]:
    """Walk the partition tables from the raw bytes only, the way a forensic tool other than the parser would."""
    found: list[ManifestEntry] = []

    def walk(lba: int, num_start: int, base_lba: int | None) -> None:
        for index, (entry_type, sector_ofs, size_sectors) in enumerate(_entries(raw, lba, sector_size)):
            if entry_type == 0x00 or size_sectors == 0:
                continue

            if entry_type in EXTENDED_TYPES:
                if base_lba is None:
                    base_lba = lba + sector_ofs  # the first EBR is the extended partition base
                    target = base_lba
                else:
                    target = base_lba + sector_ofs  # chained links are relative to the first EBR
                walk(target, num_start + index, base_lba)
                continue

            found.append(ManifestEntry(num_start + index + 1, lba, sector_ofs, sector_size, size_sectors, entry_type))

    walk(0, 0, None)
    return found


def _owners(partitions: list[Partition], lba: int, sector_size: int) -> list[int]:
    offset = lba * sector_size
    return [
        partition.number for partition in partitions if partition.offset <= offset < partition.offset + partition.size
    ]


def _assert_disjoint(partitions: list[Partition], sector_size: int) -> None:
    """Assert that no sector of the disk is reported as part of two partitions."""
    starts = [partition.offset // sector_size for partition in partitions]
    for index, partition in enumerate(partitions):
        start, end = starts[index], (partition.offset + partition.size) // sector_size
        assert start < end, f"partition {partition.number} does not cover a sector"
        for lba in range(start, end):
            owners = _owners(partitions, lba, sector_size)
            assert owners == [partition.number], (
                f"sector {lba} of partition {partition.number} is also claimed by {owners}"
            )
        for other_start in starts[index + 1 :]:
            assert end <= other_start, f"partition {partition.number} overlaps a partition starting at {other_start}"


def _assert_reads_raw(raw: bytes, partition: Partition, tag: str, sector_size: int) -> None:
    """Assert that a partition's readable range is exactly the raw image range it reports."""
    offset, size = partition.offset, partition.size
    assert offset % sector_size == 0
    assert size > 0
    assert size % sector_size == 0
    assert offset + size <= len(raw)

    data = partition.open().read()
    assert len(data) == size
    assert data == raw[offset : offset + size]
    assert data.startswith(_marker(f"HEAD-{tag}"))
    assert data.endswith(_marker(f"TAIL-{tag}"))

    stream = partition.open()
    stream.seek(size - sector_size)
    assert stream.read(sector_size) == raw[offset + size - sector_size : offset + size]


@pytest.mark.parametrize("sector_size", SECTOR_SIZES)
@pytest.mark.parametrize("extended_type", EXTENDED_TYPES)
def test_extended_chain_partitions_trace_back_to_the_image(extended_type: int, sector_size: int) -> None:
    raw = build_image(extended_type, sector_size)
    d = disk.Disk(io.BytesIO(raw), sector_size=sector_size)

    assert isinstance(d.scheme, MBR)
    assert d.partitions == d.scheme.partitions
    assert all(partition.disk is d.scheme for partition in d.partitions)

    # A valid chain is delivered completely: one primary and all three logical partitions; the container is not one
    assert len(d.partitions) == 1 + len(CHAIN)
    assert all(partition.type not in EXTENDED_TYPES for partition in d.partitions)
    assert [partition.number for partition in d.partitions] == [1, 2, 3, 4]

    # An independent walk of the raw tables describes the same partitions at the same places
    manifest = independent_manifest(raw, sector_size)
    assert [(entry.number, entry.offset, entry.size, entry.type) for entry in manifest] == [
        (partition.number, partition.offset, partition.size, partition.type) for partition in d.partitions
    ]

    # Each range is anchored at the table that describes it, using the offset stored there
    expected = [ManifestEntry(1, 0, PRIMARY_LBA, sector_size, PRIMARY_SECTORS, 0x83)]
    expected.extend(
        ManifestEntry(i + 2, node.ebr_lba, node.data_offset, sector_size, node.data_sectors, 0x83)
        for i, node in enumerate(CHAIN)
    )
    for partition, entry in zip(d.partitions, expected, strict=True):
        assert partition.offset == entry.offset
        assert partition.size == entry.size
        # The stored fields back the reported range: sector_ofs is relative to the EBR that holds it
        assert partition.raw.sector_ofs == entry.sector_ofs
        assert partition.raw.sector_size == entry.size_sectors

    # The chain only moves forward, so the partition list keeps its order across each link hop
    offsets = [partition.offset for partition in d.partitions]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)
    _assert_disjoint(d.partitions, sector_size)

    assert d.partitions[0].flags == 0x80
    assert all(partition.flags == 0 for partition in d.partitions[1:])

    # The bytes a partition reads at its reported start and end are the bytes at those sectors of the image,
    # also when all streams are read interleaved after the chain has been walked
    tags = ["p1", "p2", "p3", "p4"]
    streams = [partition.open() for partition in d.partitions]
    assert [stream.read(16) for stream in streams] == [_marker(f"HEAD-{tag}") for tag in tags]
    for stream in streams:
        stream.seek(-16, io.SEEK_END)
    assert [stream.read(16) for stream in streams] == [_marker(f"TAIL-{tag}") for tag in tags]
    for partition, tag in zip(d.partitions, tags, strict=True):
        _assert_reads_raw(raw, partition, tag, sector_size)

    # The chain metadata is not handed out as partition data
    for node in CHAIN:
        assert _owners(d.partitions, node.ebr_lba, sector_size) == []


@pytest.mark.parametrize("sector_size", SECTOR_SIZES)
@pytest.mark.parametrize("extended_type", EXTENDED_TYPES)
def test_extended_chain_uses_the_right_offset_base(extended_type: int, sector_size: int) -> None:
    raw = build_image(extended_type, sector_size)
    d = disk.Disk(io.BytesIO(raw), sector_size=sector_size)

    # The MBR points at the first EBR, which is the base of the extended partition
    mbr_table = _table_at(raw, 0, sector_size)
    assert mbr_table.part[1].type == extended_type
    assert mbr_table.part[1].sector_ofs == EXTENDED_LBA

    for index, node in enumerate(CHAIN):
        table = _table_at(raw, node.ebr_lba, sector_size)
        partition = d.partitions[index + 1]

        # The logical partition of this EBR is anchored at the EBR sector that describes it
        assert table.part[0].type == 0x83
        assert table.part[0].sector_ofs == node.data_offset
        assert partition.offset == node.data_lba * sector_size

        # Only the first EBR sits on the base, so only there do the two bases coincide
        assert (node.ebr_lba == EXTENDED_LBA) is (index == 0)
        if index > 0:
            assert (EXTENDED_LBA + node.data_offset) != node.data_lba

        if node.link_offset is None:
            assert table.part[1].type == 0x00
            break

        # The link to the next EBR is anchored at the extended base, not at the EBR that holds it
        assert table.part[1].type == extended_type
        assert table.part[1].sector_ofs == node.link_offset
        assert node.next_ebr_lba == CHAIN[index + 1].ebr_lba
        assert _table_at(raw, CHAIN[index + 1].ebr_lba, sector_size).bootsig == 0xAA55
        if index > 0:
            assert (node.ebr_lba + node.link_offset) != node.next_ebr_lba


@pytest.mark.parametrize("sector_size", SECTOR_SIZES)
@pytest.mark.parametrize("extended_type", EXTENDED_TYPES)
def test_extended_chain_anomalous_offsets_do_not_duplicate_sectors(extended_type: int, sector_size: int) -> None:
    raw = build_image(extended_type, sector_size)
    d = disk.Disk(io.BytesIO(raw), sector_size=sector_size)
    partitions = d.partitions
    starts = [partition.offset // sector_size for partition in partitions]

    # The decoys are physically present, so a parser using the other base would really deliver them:
    # a valid table at the EBR-relative link target...
    decoy_table = _table_at(raw, DECOY_LINK_LBA, sector_size)
    assert decoy_table.bootsig == 0xAA55
    assert decoy_table.part[0].type == 0x83
    # ...and the sector the base-relative data offset would point at lies inside the first logical partition
    first_logical = CHAIN[0].data_lba
    assert first_logical < DECOY_DATA_LBA < first_logical + CHAIN[0].data_sectors

    # The link of the second EBR is anchored at the extended base, so the EBR-relative target is never followed
    assert (CHAIN[1].ebr_lba + CHAIN[1].link_offset) == DECOY_LINK_LBA
    assert DECOY_LINK_LBA not in starts
    assert _owners(partitions, DECOY_LINK_LBA, sector_size) == [partitions[3].number]  # the last partition owns it
    assert len(partitions) == 1 + len(CHAIN)

    # The last logical partition is anchored at its own EBR, not moved back to the base-relative sector
    assert starts[3] == CHAIN[2].data_lba
    assert DECOY_DATA_LBA not in starts
    assert _owners(partitions, DECOY_DATA_LBA, sector_size) == [partitions[1].number]

    # Base-relative and EBR-relative readings of the same stored offset point at different, real sectors
    assert (EXTENDED_LBA + CHAIN[2].data_offset) == DECOY_DATA_LBA
    assert (CHAIN[2].ebr_lba + CHAIN[2].data_offset) == CHAIN[2].data_lba
    assert CHAIN[2].data_lba != DECOY_DATA_LBA

    # No sector of the image is attributed to two partitions, and each range still reads its own raw bytes
    _assert_disjoint(partitions, sector_size)
    for partition, tag in zip(partitions, ["p1", "p2", "p3", "p4"], strict=True):
        _assert_reads_raw(raw, partition, tag, sector_size)
