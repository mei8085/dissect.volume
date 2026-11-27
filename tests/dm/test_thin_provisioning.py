from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING, BinaryIO

import pytest

from dissect.volume.dm.c_dm import SECTOR_SIZE, c_dm
from dissect.volume.dm.thin import ThinPool
from dissect.volume.exceptions import DMError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# Layout of the synthesized pool, expressed in device-mapper thin-provisioning concepts.
#
# Two thin devices (IDs 0 and 1) share a single data pool with a 4096 byte data block size.
#
# Physical data blocks (PB):
#   PB0 - shared by both devices at virtual block 0
#   PB1 - exclusive copy of device 0 after the shared block diverged
#   PB2 - exclusive copy of device 1 after the shared block diverged
#   PB3 - exclusive block, only reachable through device 0
#   PB4 - exclusive block, only reachable through device 1
#
# Per-device virtual block (VB) to physical block mappings:
#   device 0: VB0 -> PB0, VB1 -> PB1,            VB3 -> PB3   (VB2 and VB4 are unprovisioned)
#   device 1: VB0 -> PB0, VB1 -> PB2, VB2 -> PB4              (VB3 and VB4 are unprovisioned)
DATA_BLOCK_SIZE = 4096
METADATA_BLOCK_SIZE = 4096
DEVICE_BLOCKS = 5

# Distinctive fill byte for every physical block so ownership is observable in a single byte.
PHYSICAL_FILL = {0: 0xA0, 1: 0xB0, 2: 0xC0, 3: 0xD0, 4: 0xE0}

# Virtual block fill byte expected when reading each device, 0x00 marks an unprovisioned hole.
DEVICE_VIEW = {
    0: [0xA0, 0xB0, 0x00, 0xD0, 0x00],
    1: [0xA0, 0xC0, 0xE0, 0x00, 0x00],
}

# device_id -> {virtual_block: (data_block, timestamp)}
DEVICE_MAPPINGS: dict[int, dict[int, tuple[int, int]]] = {
    0: {0: (0, 1), 1: (1, 5), 3: (3, 6)},
    1: {0: (0, 1), 1: (2, 5), 2: (4, 6)},
}

LEAF_NODE = c_dm.node_flags.LEAF_NODE.value
THIN_SUPERBLOCK_MAGIC = c_dm.THIN_SUPERBLOCK_MAGIC


def _btree_node(blocknr: int, value_size: int, entries: list[tuple[int, bytes]]) -> bytes:
    """Build a single on-disk btree node in the device-mapper thin metadata format.

    Every node of the two-level data mapping tree is stored as a leaf node; the parser descends
    based on key depth rather than the internal/leaf flag.
    """
    max_entries = (METADATA_BLOCK_SIZE - len(c_dm.node_header) * 2) // (8 + value_size)
    buf = bytearray(METADATA_BLOCK_SIZE)
    header = c_dm.node_header(
        csum=0,
        flags=LEAF_NODE,
        blocknr=blocknr,
        nr_entries=len(entries),
        max_entries=max_entries,
        value_size=value_size,
        padding=0,
    )
    buf[0 : len(header)] = header.dumps()

    key_area = len(c_dm.node_header)
    value_area = key_area + max_entries * 8
    for index, (key, value) in enumerate(entries):
        assert len(value) == value_size
        buf[key_area + index * 8 : key_area + (index + 1) * 8] = key.to_bytes(8, "little")
        buf[value_area + index * value_size : value_area + (index + 1) * value_size] = value
    return bytes(buf)


def _build_thin_metadata(
    mappings: dict[int, dict[int, tuple[int, int]]],
) -> bytes:
    """Synthesize a minimal thin-pool metadata image for the given per-device mappings.

    The superblock and btree nodes use the exact on-disk layout consumed by the parser (leaf value
    bytes match ``thin_restore`` output). The space map roots and checksums are left zeroed: they
    are a separate on-disk subsystem that ``ThinPool`` never reads. Full kernel-tool validity,
    including space maps, is exercised separately by
    ``test_thin_ownership_matches_reference_tool_metadata``.
    """
    device_ids = sorted(mappings)

    top_node = 1
    mapping_nodes = {device_id: 2 + index for index, device_id in enumerate(device_ids)}
    details_node = 2 + len(device_ids)
    nr_metadata_blocks = 256
    metadata = bytearray(nr_metadata_blocks * METADATA_BLOCK_SIZE)

    # Top level of the data mapping tree: device ID -> per-device mapping node.
    metadata[top_node * METADATA_BLOCK_SIZE : (top_node + 1) * METADATA_BLOCK_SIZE] = _btree_node(
        top_node,
        8,
        [(device_id, mapping_nodes[device_id].to_bytes(8, "little")) for device_id in device_ids],
    )

    # Per-device mapping leaves: virtual block -> combined data block / timestamp value.
    for device_id in device_ids:
        node = mapping_nodes[device_id]
        entries = [
            (virtual_block, ((data_block << 24) | timestamp).to_bytes(8, "little"))
            for virtual_block, (data_block, timestamp) in sorted(mappings[device_id].items())
        ]
        metadata[node * METADATA_BLOCK_SIZE : (node + 1) * METADATA_BLOCK_SIZE] = _btree_node(node, 8, entries)

    # Device details tree: device ID -> disk_device_details, required to open a device.
    details_entries = []
    for device_id in device_ids:
        details = c_dm.disk_device_details(
            mapped_blocks=len(mappings[device_id]),
            transaction_id=1,
            creation_time=device_id + 1,
            snapshotted_time=0,
        ).dumps()
        details_entries.append((device_id, details))
    metadata[details_node * METADATA_BLOCK_SIZE : (details_node + 1) * METADATA_BLOCK_SIZE] = _btree_node(
        details_node, 24, details_entries
    )

    superblock = c_dm.thin_disk_superblock(
        csum=0,
        flags=0,
        blocknr=0,
        uuid=b"\x00" * 16,
        magic=THIN_SUPERBLOCK_MAGIC,
        version=2,
        time=1,
        trans_id=1,
        held_root=0,
        data_space_map_root=b"\x00" * 128,
        metadata_space_map_root=b"\x00" * 128,
        data_mapping_root=top_node,
        device_details_root=details_node,
        data_block_size=DATA_BLOCK_SIZE // SECTOR_SIZE,
        metadata_block_size=METADATA_BLOCK_SIZE // SECTOR_SIZE,
        metadata_nr_blocks=nr_metadata_blocks,
        compat_flags=0,
        compat_ro_flags=0,
        incompat_flags=0,
    )
    metadata[0 : len(superblock)] = superblock.dumps()
    return bytes(metadata)


def _build_data_pool(fills: dict[int, int]) -> bytes:
    """Build a data pool image where every physical block has a single fill byte."""
    data = bytearray((max(fills) + 1) * DATA_BLOCK_SIZE)
    for data_block, fill in fills.items():
        start = data_block * DATA_BLOCK_SIZE
        data[start : start + DATA_BLOCK_SIZE] = bytes([fill]) * DATA_BLOCK_SIZE
    return bytes(data)


@pytest.fixture
def shared_thin_pool(tmp_path: Path) -> Iterator[tuple[BinaryIO, BinaryIO]]:
    """Open a synthesized pool where two thin devices share and diverge physical blocks.

    Real file handles are used on purpose: the btree nodes retain memoryviews over data read from
    the handle, matching how the gzipped repository fixtures are consumed.
    """
    metadata_path = tmp_path / "metadata.bin"
    data_path = tmp_path / "data.bin"
    metadata_path.write_bytes(_build_thin_metadata(DEVICE_MAPPINGS))
    data_path.write_bytes(_build_data_pool(PHYSICAL_FILL))

    with metadata_path.open("rb") as metadata_fh, data_path.open("rb") as data_fh:
        yield metadata_fh, data_fh


def _block_byte(stream: ThinPool, device_id: int, virtual_block: int) -> int:
    device = stream.open(device_id, DEVICE_BLOCKS * DATA_BLOCK_SIZE)
    device.seek(virtual_block * DATA_BLOCK_SIZE)
    return device.read(DATA_BLOCK_SIZE)[0]


def test_thin_shared_and_diverged_blocks_keep_ownership(
    shared_thin_pool: tuple[BinaryIO, BinaryIO],
) -> None:
    metadata_fh, data_fh = shared_thin_pool
    pool = ThinPool(metadata_fh, data_fh)

    for device_id, expected_view in DEVICE_VIEW.items():
        for virtual_block, expected_fill in enumerate(expected_view):
            assert _block_byte(pool, device_id, virtual_block) == expected_fill


def test_thin_same_virtual_block_is_not_shared_across_devices(
    shared_thin_pool: tuple[BinaryIO, BinaryIO],
) -> None:
    metadata_fh, data_fh = shared_thin_pool
    pool = ThinPool(metadata_fh, data_fh)

    # Both devices provisioned virtual block 1, but each must reach its own diverged copy.
    assert _block_byte(pool, 0, 1) == PHYSICAL_FILL[1]
    assert _block_byte(pool, 1, 1) == PHYSICAL_FILL[2]
    assert _block_byte(pool, 0, 1) != _block_byte(pool, 1, 1)

    # The shared physical block stays identical regardless of which device mapping reaches it.
    assert _block_byte(pool, 0, 0) == _block_byte(pool, 1, 0) == PHYSICAL_FILL[0]


def test_thin_unmapped_blocks_keep_their_logical_position(
    shared_thin_pool: tuple[BinaryIO, BinaryIO],
) -> None:
    metadata_fh, data_fh = shared_thin_pool
    pool = ThinPool(metadata_fh, data_fh)

    # Device 0 leaves virtual block 2 unprovisioned while virtual blocks 1 and 3 are mapped. The
    # hole must read back as zeroes and must not displace the blocks on either side: every virtual
    # block keeps resolving to its own logical offset.
    assert _block_byte(pool, 0, 1) == PHYSICAL_FILL[1]
    assert _block_byte(pool, 0, 2) == 0x00
    assert _block_byte(pool, 0, 3) == PHYSICAL_FILL[3]

    device = pool.open(0, DEVICE_BLOCKS * DATA_BLOCK_SIZE)
    device.seek(2 * DATA_BLOCK_SIZE)
    assert device.read(DATA_BLOCK_SIZE) == b"\x00" * DATA_BLOCK_SIZE

    # Seeking back to a mapped block located after the hole still reaches that block's bytes.
    device.seek(3 * DATA_BLOCK_SIZE)
    assert device.read(DATA_BLOCK_SIZE) == bytes([PHYSICAL_FILL[3]]) * DATA_BLOCK_SIZE


def test_thin_unmapped_region_reads_as_zeroes_without_size(
    shared_thin_pool: tuple[BinaryIO, BinaryIO],
) -> None:
    metadata_fh, data_fh = shared_thin_pool
    pool = ThinPool(metadata_fh, data_fh)

    # Without a size hint the device has no known end, but an unprovisioned block is still a hole
    # that delivers zeroes rather than leaking another device's physical block.
    device = pool.open(0)
    device.seek(2 * DATA_BLOCK_SIZE)
    assert device.read(DATA_BLOCK_SIZE) == b"\x00" * DATA_BLOCK_SIZE


@pytest.mark.xfail(
    reason=(
        "ThinDevice._read returns zeroes for the remainder of a read as soon as it hits an "
        "unprovisioned block, truncating mapped blocks that follow the hole instead of keeping "
        "every reachable byte at its logical offset"
    ),
    strict=True,
)
def test_thin_read_spanning_mapping_states_preserves_order(
    shared_thin_pool: tuple[BinaryIO, BinaryIO],
) -> None:
    metadata_fh, data_fh = shared_thin_pool
    pool = ThinPool(metadata_fh, data_fh)

    # One aligned read covering mapped blocks, a hole and another mapped block must return every
    # reachable byte in logical order. It currently truncates at the first unprovisioned block, so
    # the test is marked as an expected failure until the read path preserves holes.
    device = pool.open(0, DEVICE_BLOCKS * DATA_BLOCK_SIZE)
    full_read = device.read(DEVICE_BLOCKS * DATA_BLOCK_SIZE)
    expected = b"".join(bytes([fill]) * DATA_BLOCK_SIZE for fill in DEVICE_VIEW[0])
    assert full_read == expected


def test_thin_unknown_device_is_rejected(shared_thin_pool: tuple[BinaryIO, BinaryIO]) -> None:
    metadata_fh, data_fh = shared_thin_pool
    pool = ThinPool(metadata_fh, data_fh)

    with pytest.raises(DMError, match="Device ID is not known in pool: 2"):
        pool.open(2)


THIN_RESTORE = shutil.which("thin_restore")
THIN_CHECK = shutil.which("thin_check")


@pytest.mark.skipif(
    THIN_RESTORE is None or THIN_CHECK is None,
    reason="thin-provisioning-tools (thin_restore/thin_check) are required for this format check",
)
def test_thin_ownership_matches_reference_tool_metadata(tmp_path: Path) -> None:
    """Cross-check the ownership semantics against metadata produced by the kernel thin tools."""
    xml_path = tmp_path / "pool.xml"
    xml_path.write_text(
        """
<superblock uuid="" time="1" transaction="1" flags="0" version="2" data_block_size="8" nr_data_blocks="8">
  <device dev_id="0" mapped_blocks="3" transaction="1" creation_time="1" snap_time="0">
    <single_mapping origin_block="0" data_block="0" time="1"/>
    <single_mapping origin_block="1" data_block="1" time="5"/>
    <single_mapping origin_block="3" data_block="3" time="6"/>
  </device>
  <device dev_id="1" mapped_blocks="3" transaction="1" creation_time="2" snap_time="0">
    <single_mapping origin_block="0" data_block="0" time="1"/>
    <single_mapping origin_block="1" data_block="2" time="5"/>
    <single_mapping origin_block="2" data_block="4" time="6"/>
  </device>
</superblock>
""".strip()
    )
    metadata_path = tmp_path / "reference-metadata.bin"
    metadata_path.write_bytes(b"\x00" * (256 * METADATA_BLOCK_SIZE))

    subprocess.run(
        [THIN_RESTORE, "-i", str(xml_path), "-o", str(metadata_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run([THIN_CHECK, str(metadata_path)], check=True, capture_output=True)

    data_path = tmp_path / "reference-data.bin"
    data_path.write_bytes(_build_data_pool(PHYSICAL_FILL))

    with metadata_path.open("rb") as metadata_fh, data_path.open("rb") as data_fh:
        pool = ThinPool(metadata_fh, data_fh)
        for device_id, expected_view in DEVICE_VIEW.items():
            for virtual_block, expected_fill in enumerate(expected_view):
                assert _block_byte(pool, device_id, virtual_block) == expected_fill
