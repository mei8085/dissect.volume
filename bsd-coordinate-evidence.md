# BSD disklabel 坐标证据分析报告

> 对象：`dissect.volume` 仓库（HEAD `10a38c6`）
> 关注点：raw 分区（`BSD_PART_RAW`）、上层切片、对外分区范围三者之间的坐标换算，以及每条分区记录能否回溯到原始镜像中的唯一字节位置。
> 方法：逐行阅读实际代码（`dissect/volume/disk/schemes/bsd.py`、`disk.py`、`partition.py`、`gpt.py`、`mbr.py`），并用真实依赖（`dissect.cstruct 4.7`、`dissect.util 3.24`，Python 3.12）构造可控合成镜像驱动库进行实测验证。

---

## 0. 证据来源与可信度声明

- **代码是唯一权威来源**：本报告中所有换算公式均标注了源码行号，可在仓库内复核。
- **测试样本不可直接读取**：`tests/_data/disk/bsd.bin` 与 `bsd64.bin.gz` 在本工作区是 **Git LFS 指针文件**（仅 129 字节，内容为 `version https://git-lfs.github.com/spec/v1 ... size 4608/4174`），且本地无 LFS 对象、`git lfs` 未安装，无法拉取真实样本。因此**没有直接运行仓库自带 `test_bsd.py` 的断言**。
- **替代验证**：依据 `tests/disk/test_bsd.py` 的断言值，构造语义等价的合成镜像，驱动真实库。结果 `test_bsd` 的三条记录（`number/offset/size/type` 与读取到的填充字节）**逐项吻合**，`test_bsd64` 的 GPT 嵌套 64 位路径亦复现。凡下文标注「实测」的结论，均指此类合成镜像实验，而非原始 fixture。

---

## 1. 三层坐标模型

一次 `Disk(fh)` 解析后，涉及的坐标有三层：**镜像绝对字节**、**上层切片/容器内相对字节**、**BSD 记录自身字段**。库通过「`Partition.open()` 返回 `RangeStream`」把这三层串成一条链。

### 1.1 数据流

```
镜像文件 fh(0 基准)
  └─ 主方案(MBR/GPT/APM) 的分区 Partition
        offset = 该分区在镜像中的绝对字节(对 MBR/GPT 而言)
        open() -> RangeStream(fh, offset, size)     # 新流的 0 点 = 切片起点
  └─ Disk.__init__ 发现 type ∈ BSD.TYPES
        BSD(partition.open(), sector_size)          # 把「切片流」交给 BSD
             内部 fh.seek(sector_size, SEEK_CUR)     # 跳过引导扇区，标签在切片 +512
             记录 off = (p_offset - raw.p_offset) * sector_size   # 32 位
             记录 off = p_boffset                               # 64 位(字节)
        Partition.open() -> RangeStream(BSD.fh, 记录off, 记录size)
```

关键点：**BSD 记录的 `offset` 是「切片内相对偏移」，不是镜像绝对偏移**；绝对位置 = 沿 `.disk.fh` 链累加各层基址 + 记录自身 `offset`。

`RangeStream._read` 的实现（`dissect/util/stream.py`，实测打印）：

```python
def _read(self, offset: int, length: int) -> bytes:
    read_length = min(length, self.size - offset) if self.size else length
    self._fh.seek(self.offset + offset)
    return self._fh.read(read_length)
```

即子流的绝对位置 = 父流基址 `self.offset` + 本层 `offset`，逐层叠加。

### 1.2 分层代码依据

| 环节 | 位置 | 行为 |
|---|---|---|
| 嵌套发现 | `disk.py:82-88` | 主方案任一分区 `type in BSD.TYPES` → `self.scheme = BSD(partition.open(), ...)`，并用 BSD 子分区**替换**该容器分区（外层切片对象不进入 `self.partitions`） |
| 切片流起点 | `partition.py:231-233` | `return RangeStream(self.disk.fh, offset=self.offset, size=self.size)`；对 MBR/GPT 分区，`self.offset` 是镜像绝对字节 |
| 标签读取 | `bsd.py:294-302` | `offset = fh.seek(sector_size, io.SEEK_CUR)`（默认 +512），随后读 512 字节判 magic |
| 32 位换算 | `bsd.py:326-348` | 先读 raw 分区(`idx=2`)的 `p_offset` 作 `table_offset`；每条 `offset=(p_offset - table_offset)*sector_size`，`size=p_size*sector_size` |
| 64 位换算 | `bsd.py:350-361` | 每条 `offset=p_boffset`（字节）、`size=p_bsize`（字节），**不做减法** |
| 绝对定位 | `partition.py:233` + `stream.py` | `open()` 的 `RangeStream` 以 BSD 的 `fh`（切片流）为父，故绝对 = 切片基址 + 记录 off |

### 1.3 结构体字节布局（实测序列化确认）

- `disklabel`（32 位头）**148 字节 (0x94)**；分区表紧随其后，16 字节/项。
  - 关键字段：`d_secsize@+0x28`、`d_magic2@+0x84`、`d_checksum@+0x88`、`d_npartitions@+0x8A`。
- `partition`（32 位项）**16 字节**：`p_size@+0x0`、`p_offset@+0x4`、`p_fstype@+0xC`。
- `disklabel64`（64 位头）**200 字节 (0xC8)**；分区表紧随其后，64 字节/项。
  - 关键字段：`d_crc@+0x4`、`d_npartitions@+0xC`、`d_total_size@+0x20`、`d_pbase@+0x30`、`d_pstop@+0x38`。
- `partition64`（64 位项）**64 字节**：`p_boffset@+0x0`、`p_bsize@+0x8`、`p_fstype@+0x10`、`p_stor_uuid@+0x30`。

由此，默认 `sector_size=512` 时：
- 32 位：标签头位于 `容器基址+0x200`，分区表位于 `容器基址+0x294`，raw 项(`idx=2`)的 `p_offset` 字段位于 `容器基址+0x2B4`。
- 64 位：标签头位于 `容器基址+0x200`，分区表位于 `容器基址+0x2C8`（每项 64 字节）。

---

## 2. 32 位与 64 位的坐标表达差异（核心）

这是本报告的关键结论：**两种标签用不同的坐标系表达「位置」**。

### 2.1 32 位：扇区单位 + 依赖 raw 分区做原点平移

代码（`bsd.py:326-348`）：

```python
table_offset = 0
if self.type == 32:
    # 读取 raw 分区(索引 2)的 p_offset，作为参照
    self.fh.seek(self._partitions_offset + (c_bsd.BSD_PART_RAW * len(c_bsd.partition)))
    table_offset = c_bsd.partition(self.fh).p_offset
...
if partition.p_offset < table_offset:
    continue                                  # 早于参照点的记录被静默丢弃
offset = (partition.p_offset - table_offset) * self.sector_size
size   = partition.p_size * self.sector_size
```

语义：
- 磁盘上 `p_offset` 按 **扇区** 计数。
- 当 raw 分区（'c' 分区）的 `p_offset` 被正确填写为「该 BSD 切片在整盘中的起始扇区」时，`p_offset` 就是**整盘扇区号**；代码减掉 `table_offset` 得到**切片内相对字节**。
- 当 raw 分区 `p_offset=0`（独立标签或某些工具的习惯）时，减法为恒等，记录本身就是切片相对值。
- **只有 `raw.p_offset` 与切片真实基址一致时，二者才等价**；否则整张表会整体平移。

实测（合成镜像，raw 分区 `p_offset=2048`、子项 `p_offset=2052..`，切片基址 `0x100000`）：

```
n=1 off=0x0000800 size=0x000200 type=7  绝对=0x100800
n=2 off=0x0000c00 ...                   绝对=0x100c00
n=5 off=0x0001000 ...                   绝对=0x101000
```

即 `0x800 = (2052-2048)*512`，**记录值确为切片相对**；加切片基址 `0x100000` 得镜像绝对。等价地，`raw.p_offset*512 = 0x100000 = 切片基址`，说明磁盘上的 `p_offset` 本身是整盘扇区号。

### 2.2 64 位：字节单位 + 天然切片相对，不做减法

代码（`bsd.py:350-361`）：

```python
partition = c_bsd.partition64(self.fh)
if (partition.p_boffset == 0 and partition.p_bsize == 0) or partition.p_fstype == 0:
    continue
offset = partition.p_boffset     # 字节，直接使用
size   = partition.p_bsize
```

结构体注释（`bsd.py:149-155`）明确：

> offsets are relative to the base of the slice, NOT to d_pbase. ... the on-disk format for a 64 bit disklabel remains slice-relative.

即 `p_boffset` 已是**切片内字节偏移**，代码原样上抛。绝对位置 = 切片基址 + `p_boffset`。

### 2.3 差异对照表

| 维度 | 32 位 `disklabel` | 64 位 `disklabel64` |
|---|---|---|
| 位置字段 | `p_offset` | `p_boffset` |
| 单位 | 扇区（× `sector_size`） | 字节 |
| 磁盘上的参考系 | 整盘扇区号，需减 `raw.p_offset` 归一到切片 | 切片内字节，直接用 |
| 是否依赖 raw 分区 | **是**（用作原点平移） | 否（raw 项仅被跳过） |
| raw 项本身是否输出 | 跳过（`idx==2`） | 跳过（`idx==2`） |
| 大小字段 | `p_size`×扇区 | `p_bsize` 字节 |
| 身份令牌 | 无（`guid=None`） | `p_stor_uuid`（`guid=`） |
| 序列化快捷判据 | `p_offset*ss` 应等于镜像绝对字节 | `p_boffset` 应等于「镜像绝对 − 切片基址」 |

---

## 3. 逐类记录的可追溯性判定

设 `容器基址 B`＝沿 `.disk.fh` 链各层 `RangeStream.offset` 之和（独立文件为 0；MBR 切片为 `sector_ofs*ss`；GPT 切片为 `first_lba*ss`）。下表结论均有合成镜像实测支撑。

| 记录类别 | 记录字段 → 绝对字节 | 能否唯一回溯 | 依据 / 实测 |
|---|---|---|---|
| 独立 32 位（`B=0`，`raw.p_offset=0`） | `p_offset*ss` | **能**，`offset` 即镜像绝对 | 实测 A：`off=0x800` 读到填充 `0x01` |
| 嵌套 32 位（MBR `0xA5`/GPT） | `B + (p_offset − raw.p_offset)*ss` | **能**（前提见 §4-G1/G2） | 实测 B：`B=0x100000`，绝对 `0x100800` |
| 独立 64 位 | `p_boffset` | **能**，`offset` 即镜像绝对 | 实测：`0x1000` 读到 `0x11` |
| 嵌套 64 位（GPT DragonFlyBSD） | `B + p_boffset` | **能** | 实测：`B=0x100000`，`p_boffset=0x100000`，绝对 `0x200000`，读 `0x01` |
| raw 分区（`idx=2`） | 不输出 | **不适用**（仅被用作 32 位平移参照） | `bsd.py:337-339,352-354` |
| `p_fstype==0` / 全零项 | 不输出 | 不适用（视为未使用） | `bsd.py:341,356` |
| 32 位 `p_offset < raw.p_offset` | **静默丢弃** | **不能**（见 G1） | `bsd.py:344-345` |

**要点**：库内 `Partition.offset` 永远只代表**本层容器内**的偏移。「记录能否追溯到镜像字节」取决于**容器基址 B 是否可完全解析**。在标准 `Disk(...)` 解析路径下，B 由父方案分区的 `offset` 提供，链路完整；但记录的裸 `(offset,size)` 元组**本身不携带坐标参考系**，单独看有歧义：

- 同样 `offset=0x100000`：对嵌套 64 位 BSD 记录是切片相对（绝对 `0x200000`）；对同磁盘 GPT 普通分区则是镜像绝对。
- 实测 R3 显示：GPT 项 `offset=0x80000`（绝对），而并列的 BSD 项 `offset=0x100000`（切片相对），二者语义不同。

---

## 4. 证据缺口（无法确证的归属）

以下为代码层面确认的缺口，均以合成镜像实测复现。它们决定「一条记录是否真的指向镜像中的那份数据」。

### G1. 32 位「早于参照点」的记录被静默丢弃
`bsd.py:344-345`：`if partition.p_offset < table_offset: continue`。
实测：`raw.p_offset=100`、某子项 `p_offset=50` → 该子项**不输出**，无告警。若 raw 分区的参照值填写异常（偏大），合法分区会整体消失，取证上表现为「分区不存在」而非「无法定位」。

### G2. `d_secsize` 被忽略，一律用构造参数 `sector_size`
`bsd.py:294-296` 使用传入的 `sector_size`（默认 512），**从不读取标签内的 `d_secsize@+0x28`**。
实测：标签声明 `d_secsize=4096`、`p_offset=2`，库仍按 512 计算得 `offset=0x400`，而真实数据在 `0x2000`。→ **扇区大小不一致时，记录指向错误字节**，且库无任何提示。这是最容易被忽视、后果最严重的缺口。

### G3. 直接对「已 seek 到非零位置」的文件调用 `BSD(fh)`
`bsd.py:300` 用 `fh.seek(sector_size, SEEK_CUR)`（相对当前位置）读标签，但 `Partition.open()` 的 `RangeStream` 以**同一 fh 的 0 点**为基准。
实测：文件先 `seek(0x1000)` 再 `BSD(fh)`，标签在 `0x1200` 正确读出，但记录 `offset=0x800` 打开时从绝对 `0x800` 读，**错位 0x1000**，读到全零。
→ 仅当 `BSD` 直接接收裸文件且文件头非 0 基准时触发；`Disk` 主流程因使用 `RangeStream`（0 基准切片流）而不受影响，但**直接 API 用法存在系统性偏移**。

### G4. 校验和/CRC 不验证
- 32 位：`d_checksum@+0x88` **从不校验**；仅校验 `d_magic == d_magic2 == BSD_MAGIC`（`bsd.py:309-313`，实测 magic2 不匹配会抛 `DiskError`）。
- 64 位：`d_crc@+0x4` **从不校验**（实测把 `d_crc` 改为 `0xFFFFFFFF` 仍正常解析）。
→ 标签被损坏时仍会产出「看似合法」的坐标，指向的字节未必是真实分区数据。

### G5. `d_npartitions` 未做上限/边界校验
`bsd.py:334` 直接 `range(self.disklabel.d_npartitions)` 连续解析。
实测：字段填 200 且表项非全零 → 产出了 **197 条**（除去 raw 与空项）大量伪记录；字段与物理表长不符时也无告警。

### G6. 无范围/边界校验（越界与截断）
- `p_boffset`/`p_bsize` 不与切片大小、镜像长度比对（实测 `p_boffset=0x900000` 超出 4 MiB 文件仍被接受）。
- 32 位 `size` 同理：`p_size=0xFFFF` 时记录 `end` 越过文件尾。
- `RangeStream` 在 EOF 处**截断而非报错**（`min(length, self.size-offset)`），实测越界读返回不足长度；下游若假设「读满即有效」会误判。

### G7. 多 BSD 切片时 `.scheme` 只保留最后一个
`disk.py:82-86` 对每个 BSD 类型分区**覆写** `self.scheme`。
实测：MBR 内两个 `0xA5` 切片 → `Disk.scheme.fh.offset` 只指向**第二个**（`0x300000`）；但每条子分区仍持有自己的 `.disk`（各自 BSD）与 `.fh.offset`（`0x100000` / `0x300000`），绝对位置各自正确。
→ **不能靠 `Disk.scheme` 推断切片归属**；必须以每条 `partition.disk.fh.offset` 为准，否则会把记录错误归属到另一个切片。此外 `Disk.serial`（`disk.py:91-94`）在嵌套后因 `scheme` 变为 BSD 而返回 `None`。

### G8. 外层切片对象被替换，容器自身范围不可直接获取
`disk.py:82-88` 用 BSD 子分区替换容器分区，**外层 BSD 切片本身不进入 `self.partitions`**（`test_bsd64` 期望 4 条＝1 GPT + 3 BSD，不含 BSD 切片）。→ 切片的 `offset/size` 只能通过 `.scheme.fh.offset`（且受 G7 限制）或重建父方案获得，`d.partitions` 内无该项。

### G9. 仅用公开 API 无法断言「该偏移落在正确的切片内」
记录只给出相对偏移，不含所属容器标识；同一 `(offset,size)` 在两个切片中可能都存在。库不提供「所属切片基址 + 区间」的合并视图，需调用方自行沿 `.disk` 链还原。

---

## 5. 可作为后续取证判据的位置关系

下列关系可在代码/结构体层面确证，建议作为自动化取证校验规则（任一不满足即标记「归属未确证」）。

**C1 — 绝对定位恒等式（最核心）**
对任意 BSD 记录：
`镜像绝对字节 = Σ(沿 .disk.fh 链的 RangeStream.offset) + partition.offset`
且 `partition.open()` 读出的字节应与该绝对位置处镜像内容一致。这是唯一通用的、对 32/64 位、嵌套/独立都成立的回溯式。

**C2 — 32 位坐标一致性回代**
`partition.offset + 容器基址` 应等于 `raw分区.p_offset × sector_size`（当 raw 分区按整盘扇区号填写时）。
即 `(p_offset − raw.p_offset)*ss + B == p_offset*ss`，当且仅当 `B == raw.p_offset*ss`。
→ 若两者不等，说明 raw 分区的参照值与真实切片基址不一致，整表坐标存疑（对应 G1）。

**C3 — 64 位原点关系**
`镜像绝对 = 切片基址 + p_boffset`，且 `p_boffset` 应满足 `d_pbase ≤ p_boffset < d_pstop`（`d_pbase@+0x30`、`d_pstop@+0x38`）。库本身不校验，但字段可读（结构体保留），可作外部判据。

**C4 — 单位/对齐规则**
- 32 位：所有绝对字节应为 `sector_size` 的整数倍，且 `sector_size` 应与标签 `d_secsize@+0x28` 一致（不一致即命中 G2，坐标不可信）。
- 64 位：`p_boffset` 应为 `d_align@+0x8` 的整数倍。

**C5 — 区间包含性**
`B ≤ 绝对起点` 且 `绝对起点 + size ≤ B + 容器大小`，并 `≤ 镜像文件长度`。越界或 EOF 截断（读不满 `size`）→ 归属未确证（G6）。

**C6 — 归属切片判据（多切片）**
不得以 `Disk.scheme` 判定所属切片（G7）；应以 `partition.disk.fh.offset` 作为切片基址来源，逐条记录独立计算。

**C7 — 完整性证据**
32 位记录**无唯一身份令牌**（`guid=None`），64 位才有 `p_stor_uuid`。因此 32 位记录只能靠「位置 + 类型 + 内容哈希」拼证据链，无法仅凭记录与外部证据对齐；且因 G4（校验和不验），记录存在本身不能证明标签未被篡改，需辅以 `d_checksum`/`d_crc` 的独立重算。

**C8 — 容器范围注意（GPT）**
GPT 分区 `size=(last_lba−first_lba)*ss`（`gpt.py:110-111`），而 `last_lba` 是**闭区间**（代码注释亦注明 "inclusive"）。→ GPT 容器比真实少一个扇区；BSD 偏移本身不受影响，但任何基于「容器末界」的边界推断需补 +1 扇区，否则可能把紧邻后一扇区的数据判为容器外。

---

## 6. 结论摘要

1. **坐标不同源**：32 位记录 `p_offset` 是「扇区、整盘参考、需减 raw 分区做平移」；64 位记录 `p_boffset` 是「字节、切片参考、直接用」。库把两者都归一为**切片内相对偏移**上抛，绝对位置再由 `RangeStream` 链补上切片基址。
2. **可追溯性有条件**：在标准 `Disk(...)` 路径且 `sector_size` 正确、`raw.p_offset` 自洽时，每条非 raw 记录都能经 C1 唯一回溯到镜像字节；实测 32/64 位、独立/嵌套四种组合均成立。
3. **主要证据缺口**：`d_secsize` 被忽略（G2）、32 位参照点导致静默丢弃（G1）、直接 API 的基准偏移（G3）、无校验和/CRC（G4）、无计数与范围校验（G5/G6）、多切片下 `.scheme` 误导（G7）、外层切片不可见（G8）。这些缺口不影响「在正确前提下的换算」，但会使**异常/对抗性标签**产出错误却看似合理的归属。
4. **判据落地**：以 **C1 绝对定位恒等式**为基石，配合 C2/C3 的原点回代、C4 的单位/对齐、C5 的区间包含、C6 的切片归属、C7/C8 的完整性与边界修正，构成可自动化的取证校验集；任一不满足即应标注「归属未确证」而非默认可信。

---

### 附：复现环境
- `python 3.12.3`，`dissect.cstruct 4.7`、`dissect.util 3.24`（独立 venv 安装，仓库 `pyproject.toml` 约束范围内）。
- 因 fixture 为 LFS 指针不可读，测试以合成镜像驱动真实库完成；`test_bsd` 的三条断言值（`number/offset/size/type` 与 `open().read()`）在等价镜像上**逐项复现**。
