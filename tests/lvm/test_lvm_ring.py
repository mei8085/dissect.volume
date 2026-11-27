from __future__ import annotations

import io
import zlib

import pytest

from dissect.volume.lvm import LVM2
from dissect.volume.lvm.c_lvm2 import SECTOR_SIZE, c_lvm

# ---------------------------------------------------------------------------
# Synthetic on-disk layout
# ---------------------------------------------------------------------------
# The tests below construct complete, in-memory LVM2 physical volumes. The
# layout follows the public LVM2 on-disk format:
#
#   sector 1          label_header -> pv_header -> data/metadata area descriptors
#   sector 8 (0x1000) mda_header followed by the raw_locn descriptor slots
#
# The metadata area is a circular buffer. The volume group text is addressed
# by a raw_locn descriptor whose ``offset`` is relative to the mda_header and
# whose ``size`` covers the text *plus* its single terminating NUL byte. When
# the text crosses the end of the metadata area it continues again at the
# beginning of that same area.
PV_UUID = "11111111222233334444555555555555"

DEVICE_SECTORS = 16384  # 8 MiB disk
EXTENT_SIZE = 64  # sectors per physical extent (32 KiB)
PE_START = 256  # first data sector
PE_COUNT = 4
# ``dev_size`` in the text metadata is expressed in physical extents.
DEV_SIZE_EXTENTS = DEVICE_SECTORS // EXTENT_SIZE

LABEL_OFFSET = SECTOR_SIZE
MDA_OFFSET = 8 * SECTOR_SIZE
DATA_OFFSET = PE_START * SECTOR_SIZE
# The mda_header (40 bytes) plus two raw_locn slots (24 bytes each) occupy the
# first 88 bytes of the metadata area. The circular text buffer begins on the
# following sector.
TEXT_OFFSET = 2 * SECTOR_SIZE
# Wrapped geometry. The 1600-byte payload starts at TEXT_OFFSET. Its first 1596
# bytes occupy [TEXT_OFFSET, ring end) and the remaining 4 bytes wrap to the
# start of the metadata area. That 4-byte head lands exactly on the mda_header
# checksum field, which the parser does not validate; the magic, version, start
# and size fields and both raw_locn descriptor slots (offset 40 and beyond) stay
# intact, so a fresh device scan can still locate and read the metadata.
WRAPPED_RING_SIZE = 2620
WRAPPED_PAYLOAD_SIZE = 1600
WRAPPED_HEAD_SIZE = 4

MDA_MAGIC = b" LVM2 x[5A%r0N>"

ALPHA_MARKER = b"ALPHA-START!!!"
BETA_MARKER = b"BETA-START!!!!"
DECOY_MARKER = b'contents = "Text Format Volume Group"\nversion = 1\n\nvg_DECOY {\nid = "decoy"\n}\n'


def _metadata_text() -> bytes:
    """Build the volume group text, padded so the payload straddles the ring."""
    base = f"""contents = "Text Format Volume Group"
version = 1
description = "ring wrap integration test"
creation_host = "testhost"
creation_time = 1700000000

vg_ring {{
    id = "abcdefghijklmnopqrstuvwxyzabcdef"
    seqno = 3
    status = ["RESIZEABLE", "READ", "WRITE"]
    flags = []
    extent_size = {EXTENT_SIZE}
    max_lv = 0
    max_pv = 0
    metadata_copies = 0

    physical_volumes {{
        pv0 {{
            id = "{PV_UUID}"
            device = "/dev/fake0"
            status = ["ALLOCATABLE"]
            flags = []
            dev_size = {DEV_SIZE_EXTENTS}
            pe_start = {PE_START}
            pe_count = {PE_COUNT}
        }}
    }}

    logical_volumes {{
        lv_alpha {{
            id = "aaaaaaaabbbbccccddddeeeeeeeeeeee"
            status = ["READ", "WRITE", "VISIBLE"]
            flags = []
            creation_host = "testhost"
            segment_count = 1
            segment1 {{
                start_extent = 0
                extent_count = 1
                type = "striped"
                stripe_count = 1
                stripes = ["pv0", 0]
                stripe_size = {EXTENT_SIZE}
            }}
        }}
        lv_beta {{
            id = "ffffffff000011112222333333333333"
            status = ["READ", "WRITE", "VISIBLE"]
            flags = []
            creation_host = "testhost"
            segment_count = 1
            segment1 {{
                start_extent = 0
                extent_count = 1
                type = "striped"
                stripe_count = 1
                stripes = ["pv0", 1]
                stripe_size = {EXTENT_SIZE}
            }}
        }}
    }}
}}
""".encode()

    # Comments are stripped by the text parser, so padding with them changes
    # the on-disk length without altering the parsed volume configuration.
    target_text_length = WRAPPED_PAYLOAD_SIZE - 1
    padding = target_text_length - len(base)
    if padding < 0:
        raise AssertionError("base metadata text is longer than the configured ring payload")

    comment = b"#" + b"p" * (padding - 2) + b"\n"
    assert len(comment) == padding
    return comment + base


def _checksum(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _build_lvm_disk(
    *,
    ring_size: int,
    wrapped: bool,
    zero_wrapped_head: bool = False,
    place_decoy: bool = False,
) -> bytes:
    """Create a synthetic LVM2 physical volume.

    Args:
        ring_size: Size of the circular metadata area in bytes.
        wrapped: Whether the metadata text straddles the end of the ring.
        zero_wrapped_head: When wrapping, replace the head fragment (the bytes
            only reachable after crossing the ring end) with zeros, simulating
            metadata bytes that could not be obtained.
        place_decoy: Write a foreign, self-contained configuration directly
            after the metadata area, in the adjacent region.
    """
    metadata = _metadata_text()
    payload = metadata + b"\x00"
    if len(payload) != WRAPPED_PAYLOAD_SIZE:
        raise AssertionError("unexpected metadata payload size")

    disk = bytearray(DEVICE_SECTORS * SECTOR_SIZE)

    label = c_lvm.label_header(
        id=b"LABELONE",
        sector=LABEL_OFFSET,
        crc=0,
        offset=len(c_lvm.label_header),
        type=b"LVM2 001",
    )
    # dissect exposes pv_header.device_size as a byte size; it must cover the disk.
    pv_header = c_lvm.pv_header(pv_uuid=PV_UUID.encode(), device_size=DEVICE_SECTORS * SECTOR_SIZE)
    data_area = c_lvm.disk_locn(offset=DATA_OFFSET, size=DEVICE_SECTORS * SECTOR_SIZE - DATA_OFFSET)
    metadata_area = c_lvm.disk_locn(offset=MDA_OFFSET, size=ring_size)
    zero_descriptor = c_lvm.disk_locn()
    descriptors = b"".join(
        structure.dumps()
        for structure in (label, pv_header, data_area, zero_descriptor, metadata_area, zero_descriptor)
    )
    disk[LABEL_OFFSET : LABEL_OFFSET + len(descriptors)] = descriptors

    mda = c_lvm.mda_header(checksum=0, magic=MDA_MAGIC, version=1, start=MDA_OFFSET, size=ring_size)
    mda_bytes = bytearray(mda.dumps())
    mda.checksum = _checksum(bytes(mda_bytes[len(c_lvm.uint32) :]))

    raw_template = c_lvm.raw_locn(offset=TEXT_OFFSET, size=len(payload), checksum=0, flags=0)
    raw_location = c_lvm.raw_locn(
        offset=TEXT_OFFSET,
        size=len(payload),
        checksum=_checksum(raw_template.dumps()),
        flags=0,
    )

    disk[MDA_OFFSET : MDA_OFFSET + len(mda)] = mda.dumps()
    descriptor_offset = MDA_OFFSET + len(c_lvm.mda_header)
    disk[descriptor_offset : descriptor_offset + len(raw_location)] = raw_location.dumps()
    descriptor_offset += len(raw_location)
    disk[descriptor_offset : descriptor_offset + len(c_lvm.raw_locn())] = c_lvm.raw_locn().dumps()

    if not wrapped:
        disk[MDA_OFFSET + TEXT_OFFSET : MDA_OFFSET + TEXT_OFFSET + len(payload)] = payload
    else:
        # The first fragment runs from the text offset to the physical end of
        # the ring; the remainder wraps to the very start of the metadata area.
        first_size = ring_size - TEXT_OFFSET
        head_size = len(payload) - first_size
        if head_size != WRAPPED_HEAD_SIZE:
            raise AssertionError("unexpected wrapped head fragment size")
        # The head may only cover the 4-byte checksum field; the rest of the
        # mda_header and the raw_locn descriptor slots must remain readable.
        if head_size > len(c_lvm.uint32):
            raise AssertionError("wrapped head must not overwrite mda header fields or descriptors")
        # First fragment: from the text offset up to the physical end of the ring.
        disk[MDA_OFFSET + TEXT_OFFSET : MDA_OFFSET + ring_size] = payload[:first_size]
        # Head fragment: continuing at the start of the metadata area. It only
        # overwrites part of the mda_header, never the descriptor slots.
        head_fragment = b"\x00" * head_size if zero_wrapped_head else payload[first_size:]
        disk[MDA_OFFSET : MDA_OFFSET + head_size] = head_fragment

    if place_decoy:
        disk[MDA_OFFSET + ring_size : MDA_OFFSET + ring_size + len(DECOY_MARKER)] = DECOY_MARKER

    extent_bytes = EXTENT_SIZE * SECTOR_SIZE
    disk[DATA_OFFSET : DATA_OFFSET + len(ALPHA_MARKER)] = ALPHA_MARKER
    beta_offset = DATA_OFFSET + extent_bytes
    disk[beta_offset : beta_offset + len(BETA_MARKER)] = BETA_MARKER

    return bytes(disk)


def _open_lvm(disk: bytes) -> LVM2:
    return LVM2(io.BytesIO(disk))


def _assert_volume_mapping(lvm: LVM2, metadata: bytes, disk: bytes) -> None:
    """Assert the physical-to-logical correspondence exposed by the contract."""
    volume_group = lvm.vg
    assert volume_group.name == "vg_ring"

    # The complete configuration must be recovered in its original order.
    assert list(volume_group.logical_volumes) == ["lv_alpha", "lv_beta"]
    assert lvm.metadata["_raw"] == metadata.decode()

    physical_volume = volume_group.pv[0]
    assert physical_volume.id == PV_UUID
    extent_bytes = EXTENT_SIZE * SECTOR_SIZE

    expected = {
        "lv_alpha": (0, ALPHA_MARKER),
        "lv_beta": (1, BETA_MARKER),
    }
    for lv_name, (physical_extent, marker) in expected.items():
        logical_volume = volume_group.logical_volumes[lv_name]
        segment = logical_volume.segments[0]
        # The parsed stripe points at the exact physical extent on pv0.
        assert segment.stripes == [("pv0", physical_extent)]
        assert segment.extent_count == 1

        stream = logical_volume.open()
        # The logical volume exposes a single extent of the configured size.
        assert stream.size == extent_bytes

        stream.seek(0)
        assert stream.read(len(marker)) == marker

        # The bytes read through the logical volume are exactly the bytes stored
        # at that physical extent's position on the underlying disk image.
        disk_offset = DATA_OFFSET + physical_extent * extent_bytes
        assert disk[disk_offset : disk_offset + len(marker)] == marker


def test_lvm_metadata_non_wrapped() -> None:
    """Ordinary, non-wrapping metadata keeps its existing parse result."""
    metadata = _metadata_text()
    ring_size = ((TEXT_OFFSET + len(metadata) + 1 + SECTOR_SIZE - 1) // SECTOR_SIZE) * SECTOR_SIZE
    disk = _build_lvm_disk(ring_size=ring_size, wrapped=False)

    lvm = _open_lvm(disk)
    _assert_volume_mapping(lvm, metadata, disk)


def test_lvm_metadata_wraps_around_ring() -> None:
    """Text crossing the ring end still belongs to a single volume configuration."""
    metadata = _metadata_text()
    disk = _build_lvm_disk(ring_size=WRAPPED_RING_SIZE, wrapped=True)

    lvm = _open_lvm(disk)
    _assert_volume_mapping(lvm, metadata, disk)

    # The text is stored in two physical fragments: a long fragment up to the
    # ring end and a short head fragment wrapped to the start of the area.
    device = next(iter(lvm.devices.values()))
    mda_header, raw_locations = device._metadata_areas[0]
    raw_location = raw_locations[0]
    assert raw_location.offset == TEXT_OFFSET
    assert raw_location.size == WRAPPED_PAYLOAD_SIZE

    first_size = WRAPPED_RING_SIZE - TEXT_OFFSET
    # First fragment: TEXT_OFFSET up to the ring end; head: area start.
    device.fh.seek(mda_header.start + TEXT_OFFSET)
    first_fragment = device.fh.read(first_size)
    device.fh.seek(mda_header.start)
    head_fragment = device.fh.read(WRAPPED_HEAD_SIZE)

    # The first fragment holds the beginning of the text and the head fragment
    # holds the remainder including the terminating NUL.
    assert first_fragment == metadata[:first_size]
    assert head_fragment == metadata[first_size:] + b"\x00"
    # Concatenating the fragments in logical order restores the configuration.
    assert first_fragment + head_fragment[:-1] == metadata


def test_lvm_wrapped_metadata_missing_bytes_is_not_a_config() -> None:
    """Unobtained metadata bytes must not be padded into a valid configuration."""
    disk = _build_lvm_disk(ring_size=WRAPPED_RING_SIZE, wrapped=True, zero_wrapped_head=True)

    # The missing head fragment breaks the text structure: parsing can neither
    # split key/value lines nor build a volume group, so no LVM2 volume is formed.
    with pytest.raises((ValueError, KeyError)):
        _open_lvm(disk)


def test_lvm_adjacent_region_is_not_part_of_volume_evidence() -> None:
    """Content in the region adjacent to the ring never enters the volume."""
    metadata = _metadata_text()
    ring_size = ((TEXT_OFFSET + len(metadata) + 1 + SECTOR_SIZE - 1) // SECTOR_SIZE) * SECTOR_SIZE
    disk = _build_lvm_disk(ring_size=ring_size, wrapped=False, place_decoy=True)

    lvm = _open_lvm(disk)
    # The decoy describes a different VG and must be ignored entirely.
    assert list(lvm.vg.logical_volumes) == ["lv_alpha", "lv_beta"]
    assert "vg_DECOY" not in lvm.metadata
    assert lvm.metadata["_raw"] == metadata.decode()
    _assert_volume_mapping(lvm, metadata, disk)


def test_lvm_wrapped_adjacent_region_is_not_part_of_volume_evidence() -> None:
    """A decoy past a wrapping ring is excluded while the wrapped text is recovered."""
    metadata = _metadata_text()
    disk = _build_lvm_disk(ring_size=WRAPPED_RING_SIZE, wrapped=True, place_decoy=True)

    lvm = _open_lvm(disk)
    assert "vg_DECOY" not in lvm.metadata
    _assert_volume_mapping(lvm, metadata, disk)
