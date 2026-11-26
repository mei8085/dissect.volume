from __future__ import annotations

import io
from typing import TYPE_CHECKING, BinaryIO

import pytest

from dissect.volume import disk
from dissect.volume.disk.schemes import APM
from dissect.volume.exceptions import DiskError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dissect.volume.disk.partition import Partition

DDR_SIGNATURE = b"ER"
APM_SIGNATURE = b"PM"

# Logical partition map of the synthetic images: (start block, block count, partition name, partition type)
APM_PARTITIONS: list[tuple[int, int, str, str]] = [
    (1, 63, "Apple", "Apple_partition_map"),
    (64, 8, "Macintosh", "Apple_HFS"),
    (72, 8, "", "Apple_Free"),
]
APM_BLOCK_COUNT = 80
HFS_PAYLOAD = b"HFS_PARTITION_PAYLOAD\n"


def _driver_descriptor(sector_size: int, block_count: int) -> bytes:
    """Build a Driver Descriptor Record (block 0) of an Apple-partitioned disk."""
    sector = bytearray(sector_size)
    sector[0:2] = DDR_SIGNATURE
    # sbBlkSize: the APM parser reuses this field as the byte offset to seek to after the "ER" signature
    sector[2:4] = sector_size.to_bytes(2, "big")
    sector[4:8] = block_count.to_bytes(4, "big")  # sbBlkCount
    sector[16:18] = (1).to_bytes(2, "big")  # sbDrvrCount
    sector[18:22] = (50).to_bytes(4, "big")  # driver entry: ddBlock
    sector[22:24] = (32).to_bytes(2, "big")  # ddSize
    sector[24:26] = (1).to_bytes(2, "big")  # ddType
    return bytes(sector)


def _partition_entry(partition_count: int, start: int, size: int, name: str, ptype: str) -> bytes:
    """Build a single 512-byte Apple partition map entry."""
    sector = bytearray(512)
    sector[0:2] = APM_SIGNATURE
    sector[4:8] = partition_count.to_bytes(4, "big")
    sector[8:12] = start.to_bytes(4, "big")
    sector[12:16] = size.to_bytes(4, "big")
    sector[16:48] = name.encode().ljust(32, b"\x00")
    sector[48:80] = ptype.encode().ljust(32, b"\x00")
    return bytes(sector)


def _build_apm_image(sector_size: int = 512, *, with_driver_descriptor: bool = True) -> bytes:
    """Build a synthetic Apple-partitioned disk image, optionally preceded by a Driver Descriptor Record."""
    entries = b"".join(
        _partition_entry(len(APM_PARTITIONS), start, size, name, ptype) for start, size, name, ptype in APM_PARTITIONS
    )

    image = bytearray(sector_size * APM_BLOCK_COUNT)
    map_offset = sector_size if with_driver_descriptor else 0
    if with_driver_descriptor:
        image[0:sector_size] = _driver_descriptor(sector_size, APM_BLOCK_COUNT)
    image[map_offset : map_offset + len(entries)] = entries

    # Write a recognizable payload into the data partition so content reads can be traced to medium bytes
    data_start, data_size, _, _ = APM_PARTITIONS[1]
    payload = (HFS_PAYLOAD * (data_size * sector_size // len(HFS_PAYLOAD) + 1))[: data_size * sector_size]
    payload_offset = data_start * sector_size
    image[payload_offset : payload_offset + len(payload)] = payload
    return bytes(image)


def _partition_identity(partitions: Sequence[Partition]) -> list[tuple[int, int, int, str, str]]:
    return [
        (partition.number, partition.offset, partition.size, partition.type, partition.name) for partition in partitions
    ]


def test_apm(apm: BinaryIO) -> None:
    d = disk.Disk(apm)

    assert isinstance(d.scheme, APM)
    assert len(d.partitions) == 3
    assert d.scheme.apm.signature == b"PM"

    assert d.partitions[0].number == 1
    assert d.partitions[0].offset == 0x200
    assert d.partitions[0].size == 0x7E00
    assert d.partitions[0].type == "Apple_partition_map"
    assert d.partitions[0].name == "Apple"
    assert repr(d.partitions[0]) == (
        "<Partition number=1 offset=0x200 size=0x7e00 type='Apple_partition_map' name='Apple'>"
    )

    assert d.partitions[1].number == 2
    assert d.partitions[1].offset == 0x8000
    assert d.partitions[1].size == 0x752FF6000
    assert d.partitions[1].type == "Windows_NTFS"
    assert d.partitions[1].name == ""

    assert d.partitions[2].number == 3
    assert d.partitions[2].offset == 0x752FFE000
    assert d.partitions[2].size == 0x2000
    assert d.partitions[2].type == "Apple_Free"
    assert d.partitions[2].name == ""


def test_apm_invalid() -> None:
    buf = io.BytesIO(512 * b"\x00")
    with pytest.raises(disk.DiskError):
        APM(buf)


@pytest.mark.parametrize("sector_size", [512, 4096], ids=["512-byte blocks", "4096-byte blocks"])
def test_apm_driver_descriptor_entries_agree(sector_size: int) -> None:
    # A disk image starting with a Driver Descriptor Record: automatic detection and a direct APM parse
    # are two entry points onto the same medium bytes
    image = _build_apm_image(sector_size)

    # The leading descriptor and the partition map each have their own identity
    assert image[0:2] == DDR_SIGNATURE

    d = disk.Disk(io.BytesIO(image), sector_size=sector_size)
    a = APM(io.BytesIO(image), sector_size=sector_size)

    assert isinstance(d.scheme, APM)
    # The "ER" block must be skipped: both entry points end up parsing the actual "PM" partition map
    assert d.scheme.apm.signature == APM_SIGNATURE
    assert a.apm.signature == APM_SIGNATURE
    assert a.apm.partition_count == len(APM_PARTITIONS)

    # Partition names, count and byte ranges are identical through both entry points
    expected_identity = [
        (index + 1, start * sector_size, size * sector_size, ptype, name)
        for index, (start, size, name, ptype) in enumerate(APM_PARTITIONS)
    ]
    assert len(d.partitions) == len(a.partitions) == len(APM_PARTITIONS)
    assert _partition_identity(d.partitions) == expected_identity
    assert _partition_identity(a.partitions) == expected_identity

    for (disk_partition, apm_partition), (start, size, _, _) in zip(
        zip(d.partitions, a.partitions, strict=True), APM_PARTITIONS, strict=True
    ):
        # The byte ranges still trace back to the raw map fields after the block size changes
        assert disk_partition.raw.partition_start == apm_partition.raw.partition_start == start
        assert disk_partition.raw.partition_size == apm_partition.raw.partition_size == size

        # Both entry points address the same underlying medium bytes
        length = size * sector_size
        medium_bytes = image[start * sector_size : start * sector_size + length]
        assert disk_partition.open().read(length) == medium_bytes
        assert apm_partition.open().read(length) == medium_bytes

    # End-to-end evidence chain: the payload placed on the data partition is readable through the partition
    data_partition = d.partitions[1]
    assert data_partition.name == "Macintosh"
    assert data_partition.open().read(len(HFS_PAYLOAD)) == HFS_PAYLOAD
    assert image[data_partition.offset : data_partition.offset + len(HFS_PAYLOAD)] == HFS_PAYLOAD


def test_apm_without_driver_descriptor_entries_agree() -> None:
    # Without a Driver Descriptor Record the partition map itself is the first structure on the disk
    image = _build_apm_image(512, with_driver_descriptor=False)
    assert image[0:2] == APM_SIGNATURE

    d = disk.Disk(io.BytesIO(image))
    a = APM(io.BytesIO(image))

    assert isinstance(d.scheme, APM)
    assert d.scheme.apm.signature == a.apm.signature == APM_SIGNATURE
    assert len(d.partitions) == len(a.partitions) == len(APM_PARTITIONS)
    assert _partition_identity(d.partitions) == _partition_identity(a.partitions)

    # The data partition payload is reachable through either entry point at the same byte range
    for partitions in (d.partitions, a.partitions):
        data_partition = partitions[1]
        assert data_partition.offset == APM_PARTITIONS[1][0] * 512
        assert data_partition.open().read(len(HFS_PAYLOAD)) == HFS_PAYLOAD


def test_apm_truncated_map_delivers_no_partitions() -> None:
    # The map claims two partitions, but only the first record is present on the medium.
    # No partition with a seemingly valid location may be delivered from the incomplete mapping.
    first_entry = _partition_entry(2, 1, 63, "Apple", "Apple_partition_map")

    with pytest.raises(EOFError):
        APM(io.BytesIO(first_entry))
    with pytest.raises(DiskError):
        disk.Disk(io.BytesIO(first_entry))


def test_apm_driver_descriptor_without_partition_map() -> None:
    # A Driver Descriptor Record whose target block does not hold a "PM" signature is not a usable map
    image = _driver_descriptor(512, APM_BLOCK_COUNT) + b"\x00" * 512

    with pytest.raises(DiskError, match="Invalid APM signature"):
        APM(io.BytesIO(image))
    with pytest.raises(DiskError, match="Unable to detect a valid partition scheme"):
        disk.Disk(io.BytesIO(image))


def test_apm_driver_descriptor_past_end_of_image() -> None:
    # The descriptor points at a block beyond the end of the image: no phantom partition may be fabricated
    descriptor = _driver_descriptor(4096, APM_BLOCK_COUNT)
    image = descriptor[:512]  # only the first 512 bytes of the descriptor sector are present

    with pytest.raises(EOFError):
        APM(io.BytesIO(image))
    with pytest.raises(DiskError):
        disk.Disk(io.BytesIO(image))


def test_apm_empty_image() -> None:
    with pytest.raises(EOFError):
        APM(io.BytesIO(b""))
    with pytest.raises(DiskError):
        disk.Disk(io.BytesIO(b""))
