# LVM 取证明得范围分析：元数据容量、镜像字节与未取得介质

本文基于本仓库 `dissect.volume` 的**实际代码**（卷发现 → 物理卷读取 → 逻辑卷装配）分析调查者能够据以认定的证据范围，给出跨层一致的偏移/长度判据，并说明在保持现有读取方式不变的前提下，哪些结果必须附带缺口信息。

分析使用的样本：仓库内 `tests/_data/lvm/*.bin.gz` 是 Git LFS 指针（本地无法取回真实镜像），因此按代码中定义的盘上结构（`dissect/volume/lvm/c_lvm2.py`）合成了 LVM2 PV 镜像，并用系统自带的上游工具 `pvck --dump headers/metadata` 验证其结构可被真实 LVM 识别；单位与偏移语义另与上游源码 `lib/format_text/layout.h`、`lib/format_text/export.c`、`lib/format_text/import_vsn1.c`、`lib/format_text/format-text.c`、`lib/cache/lvmcache.c`、`lib/striped/striped.c` 对照。所有数字均为实测值。

---

## 0. 结论摘要

1. **三层"容量"互不等价，必须分开记录**：
   - *元数据声明*：`pv_header.device_size`（字节，整设备）与文本元数据 `dev_size`（512 字节扇区）、`pe_start`/`pe_count`/`extent_size`（扇区/个数）。
   - *镜像实际保存*：镜像长度 `N` 与 PV 在镜像中的起点 `B`，数据区从 `B + A` 开始（`A` 为数据区描述符偏移，通常 `A = pe_start × 512 ≠ 0`）。
   - *尚未取得*：数据区口径 `gap_data = 声明数据区 − 镜像可得部分`、已分配区口径 `gap_alloc = 已分配 PE − 镜像可得部分`；多 PV 缺失、分区未采、镜像截断、thin 未映射块均属此类。
2. **数据区不从镜像起点开始**：LV 逻辑字节 0 对应镜像 `B + A + pe×E + off`，不是镜像偏移 0；`B`（分区基址）由调用方（`Partition.open()` 的 `RangeStream.offset`）决定，库本身不记录 `B`，必须由取证层外挂。
3. **合法数据区可以正确回查**：对 linear / 单条带（`stripe_count == 1`）LV，在上述公式下逐 extent 的 marker 与镜像绝对偏移完全吻合（见 §3.4）；对 `stripe_count > 1` 的条带 LV，当前代码存在映射缺陷（§4.6），不能保证回查正确，必须加注"映射未验证/不交错"。
4. **现有读取方式存在若干会把"声明"误当"可得"的偏离**（§4）：`PhysicalVolume.size` 单位错误（默认几何下放大 16 倍）、`LVM2Device.open()` 的 runlist 起点/长度写成 `(A, D)` 使跨度成为 `[A, A+D)`、`read_sectors()` 坐标偏置可越过数据区声明末端并读取外来字节、多段/带洞 LV 的 `size` 取末端而非容量、元数据只取 `devices[0]` 的第一个 MDA 的第一个 `raw_locn` 且不校验 CRC/flags/seqno。
5. **兼容策略**：保持现有 API 返回值不变，但每个"字节/长度/偏移"结果都要附带 `乘子/声明来源/实际可得/gap/foreign/映射有效性` 字段（§5）。凡结果区间与 `[N, B+D)`（缺口）或 `[B+D, N)`（外来）相交，或映射类型不受支持，都必须标注，且不得将其断言为原介质内容。

---

## 1. 实际代码路径与各层偏移基准

### 1.1 卷发现（分区层）

`dissect/volume/disk/disk.py` 依次尝试 MBR/GPT/APM/BSD；`dissect/volume/disk/partition.py:231-233`：

```python
def open(self) -> BinaryIO:
    return RangeStream(self.disk.fh, offset=self.offset, size=self.size)
```

`RangeStream`（`dissect/util/stream.py:217`）只把 `offset` 作为读写基址，自身不暴露"分区基址"以外的语义。**由此 `B = partition.offset` 是唯一的原盘回查锚点，且只存在于构造参数中，`LVM2`/`LVM2Device` 都不知道它。**

若调查者把整盘镜像直接交给 `LVM2()`（而非分区流），标签扫描从镜像 0 偏移开始，PV 在分区内的情形会直接失败——不会"猜"到分区位置。

### 1.2 物理卷识别（标签与描述符）

`dissect/volume/lvm/physical.py`：

- `:22-30` 仅在**前 4 个 512 字节扇区**（`LABEL_SCAN_SECTORS = 4`，`c_lvm2.py:47`）内按 `id == b"LABELONE"` 查找标签；更靠后的标签（如 `pvck --labelsector` 指定过的）不会被发现。
- `:32-36` 跳到 `label_offset + label.offset` 读 `pv_header`，`self.size = self.header.device_size`——**注意这是整设备的字节数，不是数据区长度**。
- `:38-51` 先读数据区 `disk_locn` 列表，再读元数据区 `disk_locn` 列表；列表以全零结构终止（实测：无终止符或越界读会抛 `EOFError`）。
- `:40-42` 当前**只接受恰好 1 个数据区描述符**，否则 `RuntimeError`；上游同样只支持 1 个数据区（`lvmcache.c` 中 "Must be exactly one data area"），因此这是对上游约束的显式化，不是信息丢失。
- `:46-51` 每个 MDA 读 `mda_header` 与 `raw_locn` 列表。

标签字段**不做校验**：`label.crc`、`label.sector`、`label.type`、`pv_uuid` 均未参与判断（实测：修改这些字段仍被接受）。

### 1.3 元数据读取（文本）

`physical.py:64-74`：

```python
mda_header, raw_locn = self._metadata_areas[0]          # 只用第一个 MDA
self.fh.seek(mda_header.start + raw_locn[0].offset)      # 只用第一个 raw_locn
return parse_metadata(self.fh.read(raw_locn[0].size - 1).decode())
```

即：**元数据来源 = `devices[0]` 的 `MDA[0]` 的 `raw_locn[0]`**。实测表明：

- `raw_locn[0].flags` 的 `RAW_LOCN_IGNORED`（`c_lvm2.py:40`）被忽略：把 `locn[0]` 标为 IGNORED 并在 `locn[1]` 放真实文本，库仍选用 `locn[0]`（解出诱饵 VG）。
- `mda_header.magic`/`checksum`、`raw_locn[0].checksum` 均不校验：篡改 magic、CRC 后仍正常解析。
- 不按 `seqno` 选最新副本：把 stale（`seqno=2`）放在 `devices[0]`、新副本（`seqno=9`）放在后，得到的是 stale VG。

因此**元数据派生的容量/映射值只能标为"声明值（未校验、来源单一）"**。

### 1.4 逻辑卷装配

`dissect/volume/lvm/lvm2.py:28-44`：`devices` 以 PV UUID 建索引，`VolumeGroup.attach()` 按 `pv.id.replace("-", "")` 回填 `pv._dev`；**缺失 PV 时 `_dev = None`**，`PhysicalVolume.open()` 抛 `LVM2Error`（`metadata.py:163-164`）。

`metadata.py` 中的长度换算点：

| 位置 | 代码 | 语义 |
|---|---|---|
| `:138-151` | `dev_size * vg.extent_size` | `PhysicalVolume.size` |
| `:151` | `(pe_start + pe_count * extent_size) * 512` | 无 `dev_size` 时的回退 |
| `:158-175` | `runlist = [(area.offset // 512, size // 512)]` | PV 流 |
| `:225-232` | `offset = start_extent*extent_size; size = extent_count*extent_size` | LV 段映射 |
| `:374-393` | `stripe_size = (stripe_size or extent_count) * extent_size` | 条带 |
| `:446-452` | `thin_pool.open(device_id, extent_count*extent_size)` | thin 尺寸提示 |

### 1.5 偏移基准表（关键）

| 坐标 | 基准 | 单位 | 备注 |
|---|---|---|---|
| 镜像绝对偏移 | 镜像文件 0 | 字节 | 需要 `B` 才是原盘偏移 |
| 分区基址 `B` | 原盘 0 | 字节 | `Partition.offset`，库不记录 |
| 数据区偏移 `A` | PV 起点 | 字节 | `disk_locn.offset` |
| 数据区偏移（元数据）`pe_start` | PV 起点 | 512B 扇区 | `pe_start × 512` 应与 `A` 相等 |
| extent 偏移 | 数据区起点 `A` | 字节 | 第 i 个 PE 在 `A + i×E` |
| `device_size` `D` | PV 起点 | 字节 | 整设备，非数据区 |
| `dev_size` `S` | PV 起点 | 512B 扇区 | 整设备，`S×512` 应与 `D` 相等 |
| `raw_locn.offset` | `mda_header.start` | 字节 | 不是 PV 起点 |
| LV 逻辑偏移 | LV 起点 | 字节 | 经段/条带映射到 PE |

---

## 2. 三层容量的定义与实测差异

以默认几何（1 MiB `pe_start`、4 MiB extent、`pe_count=3`）的 16 MiB PV 为例，实测：

```
镜像 N = 0x1000000 (16 MiB)，B = 0
A = 0x100000，D = 0x1000000，S = 32768 扇区 (= 0x1000000 字节)
E = 8192 扇区 = 0x400000 字节，pe_count = 3
```

| 量 | 值 | 来源 |
|---|---|---|
| LV 元数据声明逻辑容量 | `0xC00000` (12 MiB) | `Σ extent_count × E` |
| 数据区声明长度 | `D − A = 0xF00000` (15 MiB) | 整设备 − 数据区起点 |
| 已分配区 | `pe_count × E = 0xC00000` | 元数据 |
| 已声明未分配 | `[0xD00000, 0x1000000)` = 3 MiB | 镜像内但无 LV 归属 |
| 镜像内可得（从 A 起） | `0xF00000`（完整时） | `N − (B+A)` |
| `PhysicalVolume.size`（库） | `0x10000000` (256 MiB) = 16× | `dev_size × extent_size`（单位错，§4.1） |
| `LVM2Device.open()` 跨度 | `[0x100000, 0x1100000)` | runlist `(A, D)`（§4.2），越过设备尾 1 MiB |

**"元数据声明的逻辑容量"是三者的下界，不是可得字节**：未分配区仍保存在镜像里（可能有历史数据），而镜像可能短缺（缺口）或超长（外来字节）。

### 2.1 镜像短缺（缺口）

把同一 PV 截断到 10 MiB（`N = 0xA00000`）：

```
L_data_decl = 0xF00000，L_alloc = 0xC00000，L_img = N − A = 0x900000
gap_data  = 0x600000   （数据区整段缺 6 MiB）
gap_alloc = 0x300000   （已分配 PE 缺 3 MiB：第 3 个 extent 的后 3 MiB）
LV[0x800000] -> 16B (第 3 个 extent 头部，镜像 0x900000，仍可得)
LV[0xBFFFF0] -> 0B  (第 3 个 extent 尾部，落在缺口)
```

库对缺口**静默返回 0 字节**（`RunlistStream` 读到 EOF），不报错、不补零、不标记。若不外挂 gap，读者会把"短读"误认为"文件结束/稀疏零"。

### 2.2 镜像超长（外来字节）

在 16 MiB PV 之后追加 8 MiB `0xEE` 外来字节（`N = 0x1800000`），且把整段流直接交给 `LVM2()`：

```
read_sectors(30720, 1)  -> 512B @ abs 0x1000000，内容 0xEE  (越过设备尾)
pv.open().seek(0xF00000) -> 0xEE…  (越过设备尾)
```

即库可以**把 PV 之外的介质字节当作 PV 内容返回**。是否可达取决于调用方传入流的边界，实测：

| 传入流 | `read_sectors(30720,1)` | `pv.open().seek(0xF00000)` |
|---|---|---|
| 整盘/无界流（PV 后仍有字节） | `0xEE`（外来） | `0xEE`（外来） |
| 分区 `RangeStream(size = PV 长度)` | 0 字节 | 0 字节 |
| 分区 `RangeStream(size = PV + tail)` | `0xEE`（外来） | `0xEE`（外来） |

因此：**只有分区边界被正确传入且恰好等于 PV 长度时，越界读取才会自然截断为 0 字节；否则外来字节会混入。** 任何越界结果都必须标记 foreign，且不得断言为 LVM 数据。注意即使截断为 0 字节，也掩盖了"本应报错"的事实，与 §2.1 的缺口短读无法区分。

### 2.3 从未取得的介质

- **缺失 PV**：多 PV VG 只取到部分设备时，`pv._dev = None`，`StripedSegment.open()` 抛 `LVM2Error`（整卷不可读，无法只读可得部分）；`MirrorSegment.open()` 会 `except Exception: pass` 逐个镜像尝试（`metadata.py:409-418`），可能在未记录的情况下静默改用另一个镜像。
- **未实现的段类型**：`Cache/CachePool/WriteCache/Integrity/Vdo/VdoPool/RAID/Snapshot` 的 `open()` 均未覆写，`Segment.open()` 抛 `NotImplementedError`（`metadata.py:313-314`）。这些 LV 只能给出元数据层的事实（名称、大小、成员、`status`），不能给出字节。
- **thin 未映射块**：`dm/thin.py:78-84` 对 `block_info is None` 返回全零，且不区分"未写入"与"写入了零"；池数据缺字节时 `data_fh.read()` 的短读被直接拼接（`:90-91`）。thin LV 的零块**不能**断言介质上存在零。
- **分区/整盘未采**：若镜像只包含部分分区，或采集的是逻辑卷而非整盘，则 `B` 之前/之后以及未采集设备上的 extent 都属于"未取得介质"，不是零，也不是空洞。

---

## 3. 跨层一致的偏移与长度判据

### 3.1 定义

对每个 PV（在镜像 `I` 中，长度为 `N`，PV 起点为 `B`）：

```
A      = data_disk_locn.offset                 # 字节，PV 相对
D      = pv_header.device_size                 # 字节，整设备
S      = metadata.pv.dev_size（如存在）         # 512B 扇区
E      = vg.extent_size × 512                  # 字节
P      = metadata.pv.pe_start（512B 扇区）× 512
C      = metadata.pv.pe_count
```

### 3.2 判据

- **C1 设备声明长度（字节）**：`L_dev = S × 512`（`dev_size` 存在时），否则 `L_dev = D`。上游语义：`pv->size = device_size >> 9`，且文本 `dev_size` 会覆盖 PV 头（`import_vsn1.c:231`、`lvmcache.c:2690`）。两者不一致时必须在报告中并列记录，不能只取其一。
- **C2 数据区起点**：`A`；一致性检查 `P == A`（上游要求二者一致，元数据 `pe_start` 会覆盖 PV 头）。不一致时以 `A` 定位、标注 `pe_start_mismatch`（当前库只用 `A`，实测 `pe_start=4096 扇区`、`A=1 MiB` 时库仍读 `A`）。
- **C3 可分配区**：`L_alloc = min(C × E, max(0, L_dev − A))`。
- **C4 数据区声明长度**：`L_data_decl = max(0, L_dev − A)`；若 `data_disk_locn.size > 0`，取 `min(L_data_decl, size)`。**正常 `pvcreate` 写入的 `size` 为 0**（上游 `lvmcache_update_das()` 以 `0` 写入），不能用它当长度上限。
- **C5 镜像可得长度**：`L_img = clamp(N − (B + A), 0, +∞)`。
- **C6 有效取证长度**（自数据区起点起算）：`L_eff = min(L_data_decl, L_img)`。
- **C7 缺口与外来**（两种口径都要报，不能混用）：
  - 数据区口径：`gap_data = max(0, L_data_decl − L_img)`；`foreign = max(0, L_img − L_data_decl)`。
  - 已分配区口径：`gap_alloc = max(0, L_alloc − L_img)`。
  `gap_data` 描述"整段数据区（含未分配）缺了多少"，`gap_alloc` 描述"已分配给 LV/PE 的部分缺了多少"；二者可以不同。实测截断到 10 MiB 时 `gap_data = 0x600000`、`gap_alloc = 0x300000`。
- **C8 LV 逻辑坐标 → 原盘**：对 LV 逻辑字节 `x`，先按段求 `(segment, x_in_seg)`；单条带时 `k = 0`、`img = B + A + pe_start_0 × E + x_in_seg`；多条带（`S = stripe_count`、`ss = stripe_size × 512`、每设备连续 `area_len = extent_count / S` 个 extent）时 `k = (x_in_seg // ss) % S`，`img = B + A + pe_start_k × E + (x_in_seg // (ss × S)) × ss + (x_in_seg % ss)`（`pe_start_k` 是该段第 k 个 `stripes` 项的 PE 偏移；设备内偏移以 `pe_start_k` 为基、连续 `area_len×E`）。仅当 `[x, x+len)` 完整落入某个 segment、该 segment 的映射在 `L_eff` 内、且映射类型受支持时，才能宣称"该逻辑区间已取证"。

### 3.3 原盘回查

`B` 必须由取证层保存（分区表解析结果或采集工具记录）。验证方法：在数据区内植入可识别 marker，检查 `I[B + A + pe×E + off]` 与 LV 读取一致。实测（`B = 0x2000000` 的分区、`A = 0x100000`）：

```
LV 0x0        -> 镜像 0x2100000 = EXT0
LV 0x400000   -> 镜像 0x2500000 = EXT1
LV 0x800000   -> 镜像 0x2900000 = EXT2
```

与 `B + A + pe×E` 完全一致。若省略 `B`，回查会偏到一个分区的长度；若把 `A` 当 0，会偏到 PV 头/标签区。

### 3.4 已验证示例（linear / 单条带）

16 MiB 全镜像、12 MiB LV、三段连续 extent：

| LV 逻辑偏移 | 期望镜像偏移 | 库读取 | 命中 |
|---|---|---|---|
| `0x000000` | `0x100000` | `EXT0!!!!!!!!` | ✅ |
| `0x400000` | `0x500000` | `EXT1!!!!!!!!` | ✅ |
| `0x800000` | `0x900000` | `EXT2!!!!!!!!` | ✅ |
| `0xBFFFF0` | `0xCFFFF0` | 16B 零 | ✅（声明内、真实零） |

截断到 10 MiB 后同一读取第 4 行变为 0 字节（缺口，见 §2.1）。

---

## 4. 现有读取方式与实测偏离

> 下列偏离意味着：某些返回值**看起来**是"容量/可得长度"，实际是"声明值"或"越界值"。报告与下游必须按 §5 标注。

### 4.1 `PhysicalVolume.size` 单位错误（默认几何放大 16 倍）

`metadata.py:148-149`：`dev_size × vg.extent_size`。`dev_size` 与 `extent_size` 都以 **512 字节扇区**计（上游 `export.c` 用 `outsize` 输出，`_sectors_to_units()` 除以 2 得 KiB；`lvmcache.c:2690` `pv->size = device_size >> SECTOR_SHIFT`），正确换算应为 `dev_size × 512`，这里把换算系数 `512` 换成了 `extent_size`，相对正确值 `S×512` 的倍率恰为 `extent_size / 512`（4 MiB extent 时为 16，1 MiB 时为 4）。

实测：`S=32768`、`E=8192` 扇区 → `size = 32768 × 8192 = 0x10000000`（256 MiB），而设备/镜像只有 16 MiB；`dev_size` 缺失时的回退分支 `(pe_start + pe_count × extent_size) × 512` 反而**单位正确**。两个分支的量纲不一致，且仓库测试 `test_lvm_sizes_mismatch` 已把 16× 的值（`0x8000000`）写进断言。

影响：`PhysicalVolume.open()` 用该值放宽 PV 流（§4.3），使"声明可得"被放大 16 倍。

### 4.2 `LVM2Device.open()` 的 runlist 起点/长度

`physical.py:96-101`：

```python
area = self._data_area_descriptors[0]
runlist = [(area.offset // SECTOR_SIZE, self.size // SECTOR_SIZE)]
return RunlistStream(self.fh, runlist, self.size, SECTOR_SIZE)
```

跨度是 `[A, A + D)`，而数据区应为 `[A, D)`（长度 `D − A`）。**越过设备尾恰好 `A` 字节**。实测：`A=0x100000`、`D=0x1000000` → 跨度 `[0x100000, 0x1100000)`。

同处的语义还使 `LVM2Device.size` 被当作数据区可用长度使用，而它本质是**整设备**长度（含 `A` 之前与 `D` 之后的区域）。

### 4.3 `PhysicalVolume.open()` 进一步放大

`metadata.py:158-175`：若 `dev.open().size < self.size`，则把 runlist 重写为 `[(A//512, self.size//512)]` 且 `stream.size = self.size`。由于 `self.size` 被 §4.1 放大，PV 流跨度可达 `[A, A + 16D)`。实测 16 MiB 设备、4 MiB extent：PV 流声明 `0x10000000`（256 MiB），跨度 `[0x100000, 0x10100000)`。

这是仓库为"PV 头容量被调小、需按元数据恢复"而做的放宽（`tests/lvm/test_lvm.py:105-129`），但放大倍率来自单位错误，**不能当作可信的"可得字节"**。另需注意：只有当 `dev.open().size < self.size` 时才走放宽分支；若 PV 头 `device_size` 已大于 `self.size`（例如同一错误的另一方向），`open()` 直接返回 `dev.open()`，不会收窄到数据区长度。

### 4.4 `read_sectors()` 坐标偏置与越界

`physical.py:76-94`：

```python
area_remaining_sectors = (area_size // SECTOR_SIZE) - (sector - area.offset // SECTOR_SIZE)
self.fh.seek(area.offset + sector * SECTOR_SIZE)
```

seek 用 `A + sector×512`（正确），但边界用 `sector − A/512`（把 `A` 也算进剩余量），因此**可多读 `A/512` 个扇区**；越界后继续请求会 `IndexError: list index out of range`，而不是干净的 EOF。

实测（`A=0x100000`、`area.size=0x400000`、设备 16 MiB）：

```
read_sectors(0, 8192)   -> 8192 扇区，abs end 0x500000（= 声明末端）
read_sectors(0, 10240)  -> 10240 扇区，abs end 0x600000（越过声明末端 A = 0x100000）
read_sectors(0, 10241)  -> IndexError
```

且在镜像超长时，越界部分会返回外来字节（§2.2）。

### 4.5 多段/带洞 LV 的 `size` 与洞

`LogicalVolume.open()` 用 `MappingStream`（`metadata.py:225-232`），`MappingStream.add()` 把 `size` 设为**最后一个 run 的末端**（`dissect/util/stream.py:315`），因此：

- LV `size` = `max(start_extent + extent_count) × E`，**包含洞**，不等于 `Σ extent_count × E`。
- 读洞抛 `EOFError("No mapping for offset …")`（实测：segments 在 extent 0 与 extent 2，声明容量 `0x800000`，`lv.open().size = 0xC00000`，读 `0x400000` 抛 `EOFError`）。

因此 `lv.open().size` 既不是逻辑容量，也不是可得字节。

### 4.6 多条带 LV 未交错（映射不可靠）

`StripedSegment.open()`（`metadata.py:374-393`）：

```python
stripe_size = (self.stripe_size or self.extent_count) * extent_size
stream.add(offset, stripe_size, pv_fh, extent_offset * extent_size)
offset += stripe_size
```

两处问题：

1. **单位**：`stripe_size` 元数据以 512 字节扇区存储（上游 `segtype_striped.c` 用 `outsize` 输出、`display_size()` 显示，`_sectors_to_units` 除以 2 得 KiB），这里直接乘 `extent_size`（字节）。实测 `stripe_size=256` 扇区（128 KiB）被算成 `256 × 0x100000 = 0x10000000`（256 MiB），`MappingStream.add` 收到 `size=0x10000000`。
2. **不交错**：每个 stripe 依次映射到 `[k×stripe_size, (k+1)×stripe_size)`，而正确布局是每 `stripe_size` 字节在多个 PV 间循环（`stripe = (x // ss) % S`）。外层 `LogicalVolume.open()` 又按 `extent_count×E` 截断，结果**只读第一个 stripe 所在 PV 的数据**。

实测（`E=1 MiB`、`stripe_size=256` 扇区、`stripe_count=2`、每个 PV 各 1 个 extent、每个 128 KiB 块打标记 `A/B + PE + 块号`）：

```
LV 偏移   正确(交错)   库读取
0x00000   A000        A000   ok
0x20000   B000        A001   DIFF
0x40000   A001        A002   DIFF
0x60000   B001        A003   DIFF
0x80000   A002        A004   DIFF
0xa0000   B002        A005   DIFF
0xc0000   A003        A006   DIFF
0xe0000   B003        A007   DIFF
```

12 个采样点中仅 `0x00000` 偶然一致，其余 11 个不符；`pv1` 的内容在 LV 中完全不可达。

结论：**`stripe_count > 1` 的 LV，当前代码的字节结果不能用于原盘回查**；只能报告元数据事实。`linear`/`stripe_count == 1`/`mirror`（取首个可用镜像）不受此影响。

### 4.7 元数据选择与校验的缺口

- 只用 `devices[0]` 的元数据（`lvm2.py:31`）；第一个设备无 MDA 时 `metadata` 为 `None`，下游 `TypeError: 'NoneType' object is not subscriptable`（实测），而非清晰的"无元数据"错误。
- 只用 `MDA[0]`；`MDA[1]`（PV 尾部的副本）不参与，`MDA[0]` 损坏时不会回退（实测：MDA0 文本损坏 + MDA1 有效 → 解析失败）。
- 只用 `raw_locn[0]`；忽略 `RAW_LOCN_IGNORED`（实测选中诱饵）、不校验 `raw_locn.checksum`。
- 不校验 `mda_header.magic/checksum`、`label.crc`、`label.sector`、`label.type`（实测全部可篡改）。
- 不按 `seqno` 选择最新副本（实测先给 stale 得 stale）。
- 标签只在 4 个扇区扫描（§1.2）。

这些都使"元数据声明"的可信度低于"镜像字节"，报告中必须区分。

### 4.8 多 PV 与部分可得

`StripedSegment.open()` 对每个 stripe 调用 `pv[name].open()`，任一缺失即抛 `LVM2Error`，无法只读可得部分；`MirrorSegment.open()` 按顺序尝试并吞掉所有异常。前者导致"部分采集 = 全不可读"，后者导致"静默换镜像、未记录来源"。

---

## 5. 兼容现有读取方式时，哪些结果必须附带缺口信息

原则：**不改返回值语义**（保持 `size`、`read()`、`runlist` 等现有行为以兼容下游），但所有"声明/长度/偏移/字节"类结果必须外挂一份范围描述。范围描述至少包含：

```json
{
  "provenance": {"device": "pv0", "label_sector": 1, "mda_start": 4096,
                 "raw_locn": {"offset": 512, "size": 995,
                              "checksum_verified": false, "flags": 0,
                              "seqno_selected": false}},
  "anchor": {"B": 268435456, "A": 1048576, "A_source": "data_disk_locn.offset",
             "pe_start_bytes": 1048576, "pe_start_matches_A": true},
  "declared": {"D_bytes": 16777216, "S_bytes": 16777216, "extent_bytes": 4194304,
               "pe_count": 3, "data_bytes": 15728640, "allocated_bytes": 12582912},
  "image": {"N": 16777216, "available_from_A": 15728640},
  "accounting": {"effective_bytes": 15728640, "gap_data_bytes": 0, "gap_alloc_bytes": 0,
                 "foreign_bytes": 0, "gap_ranges": [], "foreign_ranges": []},
  "mapping": {"type": "linear", "valid": true},
  "caveats": ["size_semantics=inflated", "runlist_overruns_device_tail"]
}
```

### 5.1 必须附带缺口/范围信息的结果清单

| 结果 / API | 现有语义 | 必须附带 | 触发条件 |
|---|---|---|---|
| `LVM2Device.size` | `pv_header.device_size`（字节） | 声明类型、`D`、`S`（若不同）、`N`、`B`、`A` | 总是 |
| `LVM2Device.open().size/runlist` | 声明 `D` 字节 | 实际跨度 `[A, A+D)`、`tail_overrun=A`、`L_eff`、`gap_data`/`gap_alloc`、`foreign` | 总是；`A+D > D` 恒成立，`A+D > N` 时标"越界声明" |
| `PhysicalVolume.size` | "设备字节数" | `unit_defect=true`、`factor=extent_size/512`、`correct=S×512` | `dev_size` 存在时 |
| `PhysicalVolume.open().size/runlist` | 放大后的长度 | 同 `PhysicalVolume.size` + "非可得字节" | 总是 |
| `read_sectors(sector,count)` 返回值 | ≤ `count×512` 字节 | 坐标基准（`A + sector×512`）、`bias=A`、是否越过 `data_disk_locn.size`、是否落在 `foreign` | `area.size==0` 或 `count×512 > area.size` |
| `LogicalVolume.open().size` | `MappingStream` 末端 | `logical_capacity=Σextent_count×E`、`holes`、`size_semantics=max_end` | 多段/带洞时 |
| LV 读取得到的字节（取证引用） | 逻辑内容 | 映射链（单条带 `x → B+A+pe×E+off`；多条带按 §3.2 C8）、`actual_len`、`short`、`foreign`、`zero_semantics` | 每次引用 |
| `MirrorSegment` 打开结果 | 首个可用镜像 | `used_mirror`、`other_mirrors_unverified`、被吞异常 | `type=="mirror"` |
| `ThinSegment` / `ThinPool` 读取 | 未映射→零 | `unmapped_blocks`、`short_reads`、池物理范围、`zero≠written` | `type=="thin"` |
| 元数据派生值（VG/LV 名、大小、映射、seqno） | 文本解析结果 | `provenance`（MDA[0]/locn[0]）、`checksum_verified=false`、`seqno_selected=false`、`mda1_ignored` | 总是 |
| 缺失 PV / 未实现段类型 | `open()` 失败 | 缺失 `pv.id`/`device`、未取得介质区间、`segment_type_unsupported` | 总是 |
| 条带 LV（`stripe_count>1`）的任何字节结果 | 逻辑内容 | `mapping_valid=false`、`reason=no_interleave/stripe_size_unit` | 总是 |

### 5.2 不得作出的断言（负面清单）

1. **不得**把 `pv.open()`/`LVM2Device.open()` 的 `size` 当作"镜像内可得字节"——它是声明值，且当前实现越界 `A`。
2. **不得**把 PV 设备区间 `[B+A, B+D)` 之外、镜像之内 `[B+D, N)` 的字节当作该 PV 的内容——那是外来（foreign）；镜像不足 `B+D` 时，`[N, B+D)` 是缺口（gap），不是零也不是结束。
3. **不得**把短读（`read()` 返回少于请求）解释为"文件结束"——它可能是缺口、洞或 thin 未映射。
4. **不得**把 thin 的零块断言为"介质上写入了零"。
5. **不得**在缺少 `B` 的情况下给出"原盘偏移"——只能给镜像偏移，或标注 `absolute_unknown`。
6. **不得**把 `lv.open().size` 当作逻辑容量（多段/带洞时为末端值）。
7. **不得**对 `stripe_count>1` 的 LV 断言字节与原盘一致。
8. **不得**把未校验的元数据（无 CRC、单一副本、未按 seqno 选择）当作"最新/权威"。

### 5.3 建议的兼容实现（不改现有调用）

- 在 `LVM2Device`/`PhysicalVolume`/`LogicalVolume` 上新增只读属性（如 `extent`、`accounting`、`gaps()`、`map(logical_offset, length)`），返回 §5 的结构；现有 `open()/read()` 行为保持不变。
- `read_sectors()` 保持现签名，但内部记录"本次是否越界/是否 foreign"，通过返回对象或诊断回调暴露；或新增 `read_sectors_checked()`。
- 对条带段，新增 `lenient=False` 模式：默认仍返回现有结果但置 `mapping_valid=false`；`lenient=False` 时拒绝给出字节。
- 记录 `B`：在 `LVM2` 增加可选 `base_offset` 参数（或不侵入地在报告层关联 `Partition.offset`）。

---

## 6. 结论

调查者可据现有代码认定的范围是：

- **元数据事实**（名称、UUID、`status`、段类型/成员、声明大小、`seqno`）：可认定，但需标注来源单一、未校验 CRC、未按 `seqno` 选择。
- **可回查的字节**：仅限落在 `L_eff` 内的 PE 区间——单条带时为 `[B+A+pe×E, +E)`（§3.4 已验证），多条带时按 §3.2 C8 的交错公式（§4.6 表明库自身不按该公式读取）；对缺失 PV / 未实现段类型不可得字节。
- **容量**：`L_dev`（声明设备）、`L_data_decl = L_dev − A`（声明数据区）、`L_alloc = min(pe_count×E, …)`（已分配）、`L_eff = min(L_data_decl, N−(B+A))`（可得）四者必须分别记录；`PhysicalVolume.size` 与 `LVM2Device.open()` 的 `size` 不能替代其中任何一个。
- **缺口**：`gap_data = max(0, L_data_decl − L_img)`、`gap_alloc = max(0, L_alloc − L_img)` 与 `foreign = max(0, L_img − L_data_decl)` 必须随每个长度/字节结果一起给出（两种缺口口径分别标注，不得互相替代）；`read_sectors()` 因 `A` 偏置越界时还要单独标注。

---

## 附录 A：复现与验证

1. 合成镜像：按 `c_lvm2.py` 结构写入 `label_header`（扇区 1）、`pv_header`（`device_size` 以字节）、两段 `disk_locn`、`mda_header`、`raw_locn` 与文本元数据；在 `A + pe×E` 处植入 marker。
2. 结构校验：`pvck --dump headers <img>` 能列出 `label_header/pv_header/disk_locn/mda_header/raw_locn`，并给出 CRC 期望值；`pvck --dump metadata <img>` 能导出文本元数据。据此确认合成结构符合上游布局。
3. 单位校验：用上游 `layout.h`（`device_size_xl` 注释 "Bytes"）、`export.c`（`outsize` 以扇区计）、`lvmcache.c:2690/2780/2795`（`pv->size = device_size >> 9`；数据区 `size` 写 0）、`segtype_striped.c`（`stripe_size` 以扇区计、`area_len /= area_count`）逐项核对。
4. 行为实测：用本仓库 `LVM2` 打开合成镜像，分别在完整/超长/截断/超额声明/多条带/分区基址/损坏元数据等情形下记录 `size`、`runlist`、`read()` 与异常，数值见 §2/§3/§4。

## 附录 B：字段单位速查

| 字段 | 单位 | 正确换算 | 库内用法 |
|---|---|---|---|
| `pv_header.device_size` | 字节 | 直接使用 | `LVM2Device.size`（被当数据区长度用） |
| 文本 `dev_size` | 512B 扇区 | `× 512` | `× extent_size`（错，§4.1） |
| `pe_start` | 512B 扇区 | `× 512` 应等于 `A` | 仅回退分支使用，映射时忽略 |
| `pe_count` | extent 个数 | `× E` | 未作为可读上限 |
| `extent_size` | 512B 扇区 | `× 512` | 正确得到 `E` |
| 数据区 `disk_locn.offset` | 字节 | `+ B` 得原盘偏移 | `A` |
| 数据区 `disk_locn.size` | 字节 | 正常为 0 | `or self.size`（误用整设备长度） |
| `raw_locn.offset/size` | 字节（相对 MDA） | `+ mda_header.start` | 只读 `locn[0]` |
| `stripe_size` | 512B 扇区 | `× 512` | `× extent_size`（错，§4.6） |
| `label.sector` | 512B 扇区 | 标签位置 | 扫描前 4 个扇区 |
