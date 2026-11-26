from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO
from uuid import UUID

from dissect.volume import disk
from dissect.volume.disk.schemes import GPT
from dissect.volume.disk.schemes.gpt import c_gpt
from dissect.volume.disk.schemes.mbr import c_mbr

if TYPE_CHECKING:
    from dissect.volume.disk.partition import Partition

SECTOR_SIZE = 512
SECTOR_COUNT = 128

# Media ranges of the synthetic hybrid disk below, in sectors from the start of the disk
SHARED_LBA = 64
GPT_ONLY_LBA = 80
MBR_ONLY_LBA = 112

SHARED_START = SHARED_LBA * SECTOR_SIZE
GPT_ONLY_START = GPT_ONLY_LBA * SECTOR_SIZE
MBR_ONLY_START = MBR_ONLY_LBA * SECTOR_SIZE
PROTECTIVE_START = SECTOR_SIZE

SHARED_MARKER = b"SHARED-MEDIA"
GPT_ONLY_MARKER = b"GPT-ONLY-PART"
MBR_ONLY_MARKER = b"MBR-ONLY-PART"

GPT_SIGNATURE = b"EFI PART"
LINUX_DATA = UUID("0fc63daf-8483-4772-8e79-3d69d8477de4")


@dataclass(frozen=True)
class ManifestEntry:
    """One partition as described by one of the two tables of a hybrid disk.

    ``scheme`` is the table the description came from ("mbr" or "gpt"). It is taken from the identity of the scheme
    object that owns the partition, so it does not depend on the partition type or on the numbering.
    """

    scheme: str
    number: int
    offset: int
    size: int
    type: int | UUID

    @property
    def end(self) -> int:
        return self.offset + self.size

    def overlaps(self, other: ManifestEntry) -> bool:
        return self.offset < other.end and other.offset < self.end


def source_scheme(d: disk.Disk, partition: Partition) -> str:
    """Name the table that described ``partition``, based on the scheme that owns it."""
    assert isinstance(d.scheme, GPT)
    if partition.disk is d.scheme.mbr:
        return "mbr"
    assert partition.disk is d.scheme, f"partition {partition.number} lost its source table"
    return "gpt"


def build_manifest(d: disk.Disk) -> list[ManifestEntry]:
    """List every partition of a hybrid disk together with the table its description came from."""
    return [
        ManifestEntry(source_scheme(d, partition), partition.number, partition.offset, partition.size, partition.type)
        for partition in d.partitions
    ]


def entries_at(manifest: list[ManifestEntry], offset: int) -> list[ManifestEntry]:
    return [entry for entry in manifest if entry.offset == offset]


def partition_of(d: disk.Disk, scheme: str, number: int) -> Partition:
    """Return the partition that the description from ``scheme`` with ``number`` refers to."""
    matches = [partition for partition in d.partitions if source_scheme(d, partition) == scheme]
    return next(partition for partition in matches if partition.number == number)


def read_at(fh: BinaryIO, offset: int, length: int) -> bytes:
    """Read the actual bytes at an absolute media offset, bypassing any partition view."""
    fh.seek(offset)
    return fh.read(length)


def _marker(text: bytes) -> bytes:
    return text.ljust(SECTOR_SIZE, b"\x00")


def _gpt_partition(first_lba: int, last_lba: int, guid: UUID, name: str) -> c_gpt.GPT_PARTITION:
    return c_gpt.GPT_PARTITION(
        type_guid=LINUX_DATA.bytes_le,
        partition_guid=guid.bytes_le,
        first_lba=first_lba,
        last_lba=last_lba,
        name=name.encode("utf-16-le").ljust(72, b"\x00"),
    )


def make_hybrid_image() -> bytes:
    """Create an in-memory disk that both an MBR and a GPT describe.

    The MBR alone describes a partition at ``MBR_ONLY_LBA``, the GPT alone describes one at ``GPT_ONLY_LBA``,
    and both describe the range starting at ``SHARED_LBA``. Each range starts with a distinct marker, so tests can
    tell which bytes a partition resolves to.
    """
    image = bytearray(SECTOR_COUNT * SECTOR_SIZE)

    def put(offset: int, data: bytes) -> None:
        image[offset : offset + len(data)] = data

    put(SHARED_START, _marker(SHARED_MARKER))
    put(GPT_ONLY_START, _marker(GPT_ONLY_MARKER))
    put(MBR_ONLY_START, _marker(MBR_ONLY_MARKER))

    mbr = c_mbr.mbr(
        bootcode=b"\x00" * 0x1B8,
        vol_no=0x11223344,
        part=[
            c_mbr.part(type=0x83, sector_ofs=SHARED_LBA, sector_size=9),
            c_mbr.part(type=0x83, sector_ofs=MBR_ONLY_LBA, sector_size=8),
            c_mbr.part(type=0xEE, sector_ofs=1, sector_size=SECTOR_COUNT - 1),
            c_mbr.part(),
        ],
        bootsig=0xAA55,
    )
    put(0, mbr.dumps())

    header = c_gpt.GPT_HEADER(
        signature=GPT_SIGNATURE,
        revision=0x00010000,
        header_size=92,
        current_lba=1,
        backup_lba=SECTOR_COUNT - 1,
        first_usable_lba=34,
        last_usable_lba=SECTOR_COUNT - 34,
        lba_partition_array=2,
        partition_table_count=128,
        partition_entry_size=128,
    )
    put(PROTECTIVE_START, header.dumps())

    entries = [
        _gpt_partition(SHARED_LBA, SHARED_LBA + 7, UUID(int=1), "shared"),
        _gpt_partition(GPT_ONLY_LBA, GPT_ONLY_LBA + 7, UUID(int=2), "gpt only"),
        c_gpt.GPT_PARTITION(),
        c_gpt.GPT_PARTITION(),
    ]
    for index, entry in enumerate(entries):
        put(2 * SECTOR_SIZE + index * 128, entry.dumps())

    return bytes(image)


def test_hybrid_gpt_sources(gpt_hybrid: BinaryIO) -> None:
    d = disk.Disk(gpt_hybrid)
    manifest = build_manifest(d)

    # All five partitions are attributed to the table that actually describes them
    assert [(entry.scheme, entry.number) for entry in manifest] == [
        ("mbr", 1),
        ("mbr", 2),
        ("gpt", 1),
        ("gpt", 2),
        ("gpt", 3),
    ]

    # The range at 0x100000 is described by both tables: one media location, two source identities
    shared = entries_at(manifest, 0x100000)
    assert [(entry.scheme, entry.number) for entry in shared] == [("mbr", 1), ("gpt", 1)]
    assert shared[0].overlaps(shared[1])
    # The two descriptions of that range are overlapping views, not the same entry: they disagree on its extent
    assert {entry.size for entry in shared} == {0x80200, 0x80000}

    # Same for the range at 0x180200
    shared = entries_at(manifest, 0x180200)
    assert [(entry.scheme, entry.number) for entry in shared] == [("mbr", 2), ("gpt", 3)]
    assert shared[0].overlaps(shared[1])
    assert {entry.size for entry in shared} == {0x7BC00, 0x7BA00}

    # A partition only the GPT knows about keeps its own position instead of disappearing
    assert [(entry.scheme, entry.number) for entry in entries_at(manifest, 0x4400)] == [("gpt", 2)]


def test_hybrid_gpt_protective_not_user_partitions(gpt_hybrid: BinaryIO) -> None:
    d = disk.Disk(gpt_hybrid)
    assert isinstance(d.scheme, GPT)
    manifest = build_manifest(d)

    # The protective entries of the MBR table are GPT bookkeeping, not user partitions
    protective = [partition for partition in d.scheme.mbr.partitions if partition.type == 0xEE]
    assert len(protective) == 2
    assert all(entry.type != 0xEE for entry in manifest)
    for partition in protective:
        assert not entries_at(manifest, partition.offset)

    # The partition list still has every user partition, and nothing else
    assert len(manifest) == len(d.partitions) == len(d.scheme.partitions)


def test_hybrid_shared_range_identity() -> None:
    d = disk.Disk(io.BytesIO(make_hybrid_image()))
    manifest = build_manifest(d)

    assert {entry.scheme for entry in manifest} == {"mbr", "gpt"}
    assert len(manifest) == 4

    # Two descriptions of the same media range: recognisable as the same location, distinct in origin
    shared = entries_at(manifest, SHARED_START)
    assert {(entry.scheme, entry.number) for entry in shared} == {("mbr", 1), ("gpt", 1)}
    assert len(shared) == 2
    assert shared[0].overlaps(shared[1])
    assert shared[0].size != shared[1].size

    # Both views resolve to the same actual bytes
    mbr_view = partition_of(d, scheme="mbr", number=1)
    gpt_view = partition_of(d, scheme="gpt", number=1)
    assert mbr_view.offset == gpt_view.offset == SHARED_START
    assert read_at(d.fh, SHARED_START, len(SHARED_MARKER)) == SHARED_MARKER
    assert mbr_view.open().read(len(SHARED_MARKER)) == gpt_view.open().read(len(SHARED_MARKER)) == SHARED_MARKER

    # ...and stay independently readable while their streams are interleaved
    streams = [mbr_view.open(), gpt_view.open()]
    half = len(SHARED_MARKER) // 2
    assert [stream.read(half) for stream in streams] == [SHARED_MARKER[:half], SHARED_MARKER[:half]]
    assert [stream.read(half) for stream in streams] == [SHARED_MARKER[half:], SHARED_MARKER[half:]]


def test_hybrid_one_sided_partitions_resolve() -> None:
    d = disk.Disk(io.BytesIO(make_hybrid_image()))
    manifest = build_manifest(d)

    # A partition only the GPT describes is still located at its own bytes
    assert [(entry.scheme, entry.number) for entry in entries_at(manifest, GPT_ONLY_START)] == [("gpt", 2)]
    gpt_only = partition_of(d, scheme="gpt", number=2)
    assert gpt_only.offset == GPT_ONLY_START
    assert gpt_only.open().read(len(GPT_ONLY_MARKER)) == read_at(d.fh, GPT_ONLY_START, len(GPT_ONLY_MARKER))
    assert read_at(d.fh, GPT_ONLY_START, len(GPT_ONLY_MARKER)) == GPT_ONLY_MARKER

    # A partition only the MBR describes is still located at its own bytes
    assert [(entry.scheme, entry.number) for entry in entries_at(manifest, MBR_ONLY_START)] == [("mbr", 2)]
    mbr_only = partition_of(d, scheme="mbr", number=2)
    assert mbr_only.offset == MBR_ONLY_START
    assert mbr_only.open().read(len(MBR_ONLY_MARKER)) == read_at(d.fh, MBR_ONLY_START, len(MBR_ONLY_MARKER))
    assert read_at(d.fh, MBR_ONLY_START, len(MBR_ONLY_MARKER)) == MBR_ONLY_MARKER


def test_hybrid_protective_entry_is_not_a_partition() -> None:
    d = disk.Disk(io.BytesIO(make_hybrid_image()))
    assert isinstance(d.scheme, GPT)
    manifest = build_manifest(d)

    protective = [partition for partition in d.scheme.mbr.partitions if partition.type == 0xEE]
    assert len(protective) == 1
    entry = protective[0]
    assert entry.offset == PROTECTIVE_START

    # It is present in the MBR table, but it does not appear in the manifest as a user partition
    assert not entries_at(manifest, entry.offset)
    assert all(item.type != 0xEE for item in manifest)
    assert not any(item.offset <= PROTECTIVE_START < item.end for item in manifest)

    # Taking it for a partition would mean serving the GPT header as partition data
    assert entry.open().read(len(GPT_SIGNATURE)) == read_at(d.fh, PROTECTIVE_START, len(GPT_SIGNATURE)) == GPT_SIGNATURE
