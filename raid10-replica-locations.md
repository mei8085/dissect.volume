# RAID10 副本位置分析：从逻辑偏移到成员盘物理地址的可复算关系

> 分析对象：`dissect/volume/raid/stream.py` 的 `RAID10Stream`、`dissect/volume/raid/raid.py` 的
> `VirtualDisk.open()`，以及元数据侧 `dissect/volume/md/md.py`、`dissect/volume/ddf/ddf.py`。
>
> 取证立场：**一次成功的流读取只证明它实际寻址到的那一份副本的内容；它不证明同一逻辑块的
> 其它近/远副本存在、可读或内容一致。** 本报告给出可独立复算的位置公式，并据此界定现有读取
> 究竟证明了什么、副本缺失或互相矛盾时应得出什么结论。

---

## 1. 结论摘要（先读）

1. `RAID10Stream._read()`（`stream.py:409-432`）对每个逻辑条带**只计算并读取一个位置**——
   即内核语义下的“第 0 个近副本”（first near copy）。它从不枚举 `near_copies * far_copies` 份
   副本，也不做任何副本间比对或故障切换（failover）。
2. 该首选位置的**盘号**（`dev`）在所有布局下都与内核 `__raid10_find_phys` 的第 0 份副本一致；
   但**物理行号**只有在纯近布局（`far_copies == 1`，即 `0x102` 一类）下与内核完全一致。
   一旦存在远副本（`far_copies > 1`，含 far / offset / near+far 混合），流使用的行号公式与
   内核真实寻址不同（见第 6 节量化结果，600 个条带中仅前 `raid_disks/near` 个偶然相等）。
3. 因此，**“首选副本读取成功”不能被表述为“整组副本一致”**。在 far/offset 布局下，它甚至只在
   行号公式成立的前提下定位到内核第 0 副本；其余 `near*far - 1` 份副本从未被触碰。
4. `RAID10Stream` 构造时**不校验成员盘数量**（对比 `LinearStream`/`RAID0Stream`/`RAID456Stream`
   都有 `len(disk_map)` 检查）。当首选盘不在 `disk_map` 中时，`self.disk_map[dev]`
   （`stream.py:420`）直接抛出未捕获的 `KeyError`，而不是回退到同一逻辑块的其它副本。
5. 元数据侧的成员起点（MD 的 `data_offset`、DDF 的 `starting_block`）通过
   `PhysicalDisk.offset` 与 `disk_map` 的 `device_start` 两个独立分量进入最终寻址，
   公式为 `offset_on_disk = device.offset + device_start + stripe*stripe_size + offset_in_stripe`
   （`stream.py:425`）。复算副本位置时两个分量都必须计入，缺一不可。

---

## 2. 布局参数的解码（`RAID10Stream.__init__`，stream.py:380-407）

Linux MD 把 RAID10 几何编码进一个 32 位 `layout` 字，代码注释明确写明“Reference: setup_geo”：

| 字段 | 解码表达式 | 含义 |
|---|---|---|
| `near_copies` | `layout & 0xFF` | 近副本数 `N`：同一逻辑块在相邻盘上的镜像份数 |
| `far_copies` | `(layout >> 8) & 0xFF` | 远副本数 `F`：整段数据序列在盘上重复摆放的份数 |
| `far_offset` | `layout & (1 << 16)` | 远副本是否采用 offset（相邻行）而非整段拼接布局 |
| `use_far_sets` | `layout >> 17` | 0=原始布局；1=“改进但有缺陷”；2=“改进并修复” |

代码还计算了 `far_set_size`、`last_far_set_start`、`last_far_set_size`
（`stream.py:392-405`），**但这些字段在 `_read()` 中从未被使用**——读取路径只依赖
`near_copies`、`far_copies`、`far_offset` 三个量。换言之，代码只实现了原始布局
（`far_set_size == raid_disks`）的寻址，`use_far_sets != 0` 的几何虽然被解析却不参与读。

常见布局字（含 DDF 转换，见 `ddf.py:320-359`）：

| 来源 / 布局字 | near N | far F | far_offset | 说明 |
|---|---|---|---|---|
| `0x102`（md `n2`；DDF RAID1E adjacent、sec>1） | 2 | 1 | 否 | 纯近，经典 RAID1+0 |
| `0x201`（md `f2`；DDF RAID1E **offset**） | 1 | 2 | **否** | 注意 DDF 的 offset 被映射为无 far_offset 位的 far 布局 |
| `0x10201`（md `o2`） | 1 | 2 | 是 | 真正的 offset 布局 |
| `0x10202`（md `n2 o2`） | 2 | 2 | 是 | near+offset 混合，4 份副本 |
| `0x202`（md `n2 f2`） | 2 | 2 | 否 | near+far 混合，4 份副本 |

> 取证注意：DDF `RAID1E_OFFSET` 在 `ddf.py:354-355` 被映射成 `0x201`，**没有置位 bit16**，
> 即 dissect 把它当作非 offset 的 far 布局处理。引用 DDF 镜像时不能仅凭“offset”字样假设
> `far_offset=True`，必须以转换后的布局字为准。

---

## 3. 现有读取代码到底做了什么（stream.py:409-432）

```python
stripe_number, offset_in_stripe = divmod(offset, stripe_size)
stripe, dev = divmod(stripe_number * self.near_copies, self.raid_disks)
if self.far_offset:
    stripe *= self.far_copies
device_start, device = self.disk_map[dev]
...
offset_on_disk = device.offset + device_start + (stripe * stripe_size) + offset_in_stripe
device.fh.seek(offset_on_disk)
result.append(device.fh.read(length))
```

逐行含义：

1. `stripe_number = offset // stripe_size`：逻辑条带（chunk）序号 `c`；`offset_in_stripe` 是条带内偏移。
2. `stripe, dev = divmod(c * N, raid_disks)`：
   - `dev` = 首选盘号；
   - `stripe` = 未缩放的条带行号。
3. **仅当 `far_offset` 为真**才把 `stripe` 乘以 `F`。注意这与内核的乘法条件恰好相反
   （内核在**非** offset 布局下于入口处乘 `far_copies`，见第 5 节）。
4. `self.disk_map[dev]` 取出 `(device_start, PhysicalDisk)`。盘缺失时这里抛 `KeyError`。
5. 最终盘内地址 = 成员数据区基址 `device.offset` + 数据区内起点 `device_start`
   + `stripe * stripe_size` + 条带内偏移。
6. 循环里 `length -= read_length; offset += read_length`，但向底层句柄读取时传的是
   **`length` 而非 `read_length`**（`stream.py:427`，对照 `RAID456Stream` 正确使用 `length`
   之外的 `read_length` 模式）。由于 `AlignedStream` 按 `stripe_size` 对齐调用 `_read`，
   常规对齐读取下两者多数相等，但这是一个真实的隐患：非对齐或跨条带的自定义调用可能读过头。

**关键点：整个函数没有任何对 `far_copies`/`near_copies` 的副本枚举循环，没有第二次
`disk_map` 查找，没有内容比对，也没有 try/except 回退。** 它输出的是且仅是一个物理位置的字节。

---

## 4. 成员起点如何进入寻址（两个独立分量）

`disk_map` 的类型是 `dict[int, tuple[int, PhysicalDisk]]`，键是 RAID 盘角色号，值是
`(data_offset_within_data_section, PhysicalDisk)`。

### 4.1 MD（`md.py`）

- `MDVirtualDisk` 构造 `disk_map = {disk.raid_disk: (0, disk) ...}`（`md.py:53`）：
  **元组里的 `device_start` 恒为 0**。
- 成员盘的元数据/数据分区基址放在 `PhysicalDisk.offset`：
  - 1.x 超级块：`data_offset = sb.data_offset`（扇区），`PhysicalDisk(fh, data_offset*512, data_size*512)`
    （`md.py:109, 141`）；
  - 0.90 超级块：`data_offset = 0`，数据区在超级块之前，`data_size = sb_offset`（`md.py:128-129`）。
- 因此 MD 的最终寻址 = `PhysicalDisk.offset`（数据区相对整盘的起点）+ 条带字节偏移，
  `device_start` 分量为 0。

### 4.2 DDF（`ddf.py`）

- `DDFConfiguration` 用 VDCR 的 `starting_block` 与 `physical_disk_sequence` 构造：
  `vd_pd_map[sec * count + i] = (starting_block, matched_pd)`（`ddf.py:67-69`）。
  **这里 `device_start = starting_block`（块），可以非零**，表示该虚拟盘在成员盘数据区内的起点。
- `DDFPhysicalDisk` 自身的 `PhysicalDisk.offset` 传 0（`ddf.py:179`），大小取 PDR 的
  `size * block_size`。
- 因此 DDF 的最终寻址 = `starting_block`（数据区内偏移）+ 条带字节偏移；若成员句柄本身
  还带分区/封装基址，则再叠加 `PhysicalDisk.offset`。

### 4.3 复算公式（统一形式）

对任一 RAID10 逻辑字节偏移 `offset`，流实际读取的物理字节位置为：

```
c   = offset // stripe_size                 # 逻辑 chunk 序号
ois = offset %  stripe_size                 # chunk 内偏移
dev = (c * N) % raid_disks                  # 首选盘号（与内核第 0 副本一致）
stripe_unscaled = (c * N) // raid_disks
stripe = stripe_unscaled * (F if far_offset else 1)     # 流自身的行号
物理字节 = disk_map[dev].PhysicalDisk.offset
         + disk_map[dev].device_start
         + stripe * stripe_size
         + ois
```

已用合成镜像实测验证该复合寻址：当 `PhysicalDisk.offset` 与 `starting_block` 均非零时，
流的 `seek` 位置严格等于三者之和（例如盘 2、`offset=1024`、`start=8192`、stripe 0 →
`seek(9216)`）。

---

## 5. 内核语义下的完整副本位置（可复算的权威关系）

下面的公式按 Linux `drivers/md/raid10.c` 的 `raid10_find_phys` / `__raid10_find_phys`
（原始布局 `far_set_size == raid_disks`）推导，并已用本机 6.8 内核 `raid10.ko` 的反汇编
（`struct geom` 字段偏移取自 BTF）与 `md(4)` 手册的 near/far/offset 布局图交叉验证。

设：逻辑 chunk 序号 `c`，盘数 `R`，近副本 `N`，远副本 `F`，offset 标志 `O`，
卷总 chunk 数 `T`，`stripe_size = S`。

### 5.1 入口 divmod

```
stripe0, dev0 = divmod(c * N, R)
```

`dev0` 即第 0 近副本所在盘；`stripe0` 是未缩放行号。

### 5.2 每个副本 `(fc, nc)` 的盘号（fc=远副本序号，nc=近副本序号）

副本在 `near*far` 个盘上线性推进，盘号用**减法回绕**（内核无取模，仅 `if (d >= R) d -= R`）：

```
disk(fc, nc) = (dev0 + fc*N + nc) ，再对 R 做减法回绕
```

该式保证同一逻辑块的 `N*F` 份副本落在 `N*F` 个**不同**的盘上（对应 `md(4)`：
“a total of 4 copies ... each on a different drive”）。已对 R=3..6、N/F 多种组合、
每卷 600 个 chunk 全量验证“副本数 == 不同盘数”。

特例与直觉：
- 纯近（`F=1`）：`disk = dev0 + nc`，即相邻盘同偏移镜像（奇数盘时会出现空槽，见 `md(4)` 5 盘图）。
- 纯 far（`N=1`）：`disk(fc) = dev0 + fc` 回绕，等价于相对 `dev0` 的循环移位；
  在 4 盘上表现为 `[0,1,2,3] → 下一段 [3,0,1,2]`（后移一位）。
- near+far 混合：远段把整个近组移位，使 4 份副本分布在 4 个不同盘上。

### 5.3 物理 chunk 行号（成员数据区内）

定义每盘每个远区域容纳的物理行数（含奇数盘空槽）：

```
logical_rows = ceil(T * N / R)
```

- **非 offset（far / 近 / near+far 原始布局，`O=False`）**：远区域整段拼接，
  内核入口先把行号乘 `F`，故物理行落在每隔 `F` 行的网格上：

  ```
  row(fc) = fc * F * logical_rows + F * stripe0
  ```

- **offset 布局（`O=True`）**：远副本紧邻排列，行号不乘 `F`，每个远副本向下挪一行：

  ```
  row(fc) = stripe0 + fc
  ```

物理字节位置（针对副本 `(fc, nc)`）：

```
物理字节(fc,nc) = PhysicalDisk(disk(fc,nc)).offset
                + disk_map[disk(fc,nc)].device_start
                + row(fc) * S
                + ois
```

### 5.4 与 `md(4)` 图的对应（已逐格验证）

- near 4 盘 `N=2`：行 0 = `0 0 1 1`，行 1 = `2 2 3 3`；
- near 5 盘 `N=2`：行 0 = `0,0,1,1,2`（盘 0 单元格同时含 chunk 0、2），行 1 盘 0 为空槽；
- far 4 盘 `F=2`：区域 0 行 0 = `0 1 2 3`；区域 1（dense 行 64）= `3 0 1 2`；
- offset 4 盘 `F=2`：行 0 = `0 1 2 3`，行 1 = `3 0 1 2`（与 chunk 4..7 共享单元格）；
- far/offset 5 盘：同样的循环移位规则，dense 行号与手册图一致。

> 说明：`md(4)` 手册中 5 盘 far 图的文字描述与现内核 6.8 反汇编实现的统一循环移位存在
> 细微图示出入；本报告以**本机内核反汇编 + BTF 结构**为准（这是运行中内核实际执行的算法），
> 4 盘等偶数盘图示与实现完全吻合。取证复算应优先采用上面的闭式公式而非对手册图的肉眼推断。

---

## 6. 流读取证明了哪一份内容（量化对比）

把 `RAID10Stream` 的 `(dev, stripe)` 与内核第 0 副本 `(disk(0,0), row(0))` 比较
（每卷 600 个 chunk 全量统计）：

| 布局 (R,N,F,offset) | 盘号一致 | 物理行号完全一致的 chunk 数 |
|---|---|---|
| (4,2,1,否) 纯近 | 全部 600 | **600 / 600** |
| (5,2,1,否) 纯近 | 全部 600 | **600 / 600** |
| (4,1,2,否) far | 全部 600 | 仅 4 / 600 |
| (4,1,2,是) offset | 全部 600 | 仅 4 / 600 |
| (5,1,2,否/是) far/offset | 全部 600 | 仅 5 / 600 |
| (4,2,2,否/是) near+far | 全部 600 | 仅 2 / 600 |

解读：

1. **盘号恒等**：流选出的 `dev` 永远是内核第 0 近副本的盘。因此它读取的“盘”没有选错。
2. **纯近布局完全正确**：`F=1` 时流的行号与内核物理行号逐项相等，读取即内核第 0 副本的真实数据。
   这正是 DDF RAID1E adjacent（`0x102`）与普通 md `n2` 的情形，也是测试镜像
   （`md-raid10-*`、`ddf-raid10-*`）覆盖的主路径。
3. **存在远副本时行号发散**：流在 offset 布局乘 `F`、非 offset 布局不乘，与内核正好相反。
   除卷首少数 chunk 外，流的 `stripe` 与内核 `row(0)` 不相等。这意味着对 far/offset/混合布局，
   流定位到的是**按它自身公式计算的某个盘内偏移**，该偏移不保证等于内核真正存放第 0 副本的位置。
4. **无论哪种布局，都只读到一份**：即使纯近布局行号正确，流也只读取 `dev0` 上的那一份镜像，
   `dev0+1`（近）以及任何远副本的盘和行都从未被访问。

### 工作示例（R=4, N=1, F=2, far 非 offset, S=4 KiB, T=256）

逻辑 chunk 4（逻辑偏移 `0x4000`）：

| 副本 | 内核盘号 | 内核物理 chunk 行 | 物理字节（数据区内） |
|---|---|---|---|
| 第 0 份 | 0 | 2 | `0x2000` |
| 第 1 份（远） | 1 | 130 | `0x82000`（远区域起始 128 行 + 行 2） |
| **流实际寻址** | 0 | **1（流公式）** | `0x1000` |

流报告“读取成功”时，它只证明盘 0 偏移 `0x1000` 处可读；它**没有**读取盘 1 的远副本
（`0x82000`），也没有证明该偏移与内核第 0 副本（盘 0 `0x2000`）内容相同。在 far 布局下，
流的行号本身就偏离内核语义，取证时必须按第 5 节公式重新定位，不能直接采信流的偏移作为
“权威副本地址”。

---

## 7. 副本未取得 / 互相矛盾时的取证结论与兼容处理

### 7.1 副本未取得（盘缺失、不可读、超界）

- **现象**：首选盘 `dev` 不在 `disk_map` → `stream.py:420` 抛 **`KeyError`**；
  首选盘存在但句柄读取失败 → 底层 `IOError` 原样上抛。两者都不会自动改用同一逻辑块的
  其它近/远副本。
- **根因**：`RAID10Stream` 无成员完整性校验（无 `len(disk_map) == num_disks` 检查，
  对比 `stream.py:59-60, 81-82, 194-195`），也无故障切换逻辑。RAID1 级别在
  `raid.py:68-73` 直接取 `next(iter(disk_map))`，同样不保证取到特定角色盘。
- **取证结论**：
  1. 单次 `KeyError`/读取失败**只能证明首选副本不可用**，不能据此判定整个逻辑块或整卷损坏——
     同一 chunk 还有 `N*F-1` 份独立副本位于第 5 节公式给出的其它盘/行。
  2. 应按第 5 节枚举该 chunk 的全部 `(disk, row)`，跳过缺失盘，依次尝试其余副本；
     只要还有一份可读且通过应用层一致性校验，该逻辑块即可恢复。
  3. 仅当枚举到的**所有**副本都缺失或不可读时，才能判定该逻辑块不可恢复；
     缺失副本数达到 `N*F` 才是数据丢失，少于该数属于可降级恢复。
- **兼容处理建议**（工具层面）：
  - 构造流前先比对 `len(disk_map)` 与 `num_disks`，缺盘时显式标注“降级”而非静默继续；
  - 在 `_read` 外层对 `KeyError`/`IOError` 做副本枚举回退（按第 5 节公式定位备选副本），
    并记录实际使用了哪一份副本（盘号 + 物理偏移），保证取证可追溯；
    注意当前代码不具备此能力，需要调用方自行实现。

### 7.2 副本互相矛盾（多份副本内容不一致）

现有流**永远不会发现矛盾**，因为它只读一份。要检测矛盾，取证方必须：

1. 用第 5 节公式独立计算同一 chunk 的全部 `N*F` 个 `(disk, 物理字节)` 位置；
2. 分别读取并按内容（或哈希）分组；
3. 判定规则：
   - **所有可读副本一致**：可表述为“该逻辑块的现存副本内容一致”，但仍需注明**仅覆盖实际
     成功读取的那些副本**；缺失的副本未被验证，不能声称“整组 N*F 份全部一致”。
   - **副本内容不一致（分裂）**：说明镜像组曾在降级状态下分别写入（split-brain / 不一致写入）。
     此时没有任何一份可仅凭位置宣布为“正确”。应结合元数据事件序号
     （MD 用 `events`，`md.py:52` 选事件最大的盘为 reference disk）、时间戳、文件系统层一致性
     综合判断；无法判定时保留所有分歧副本并在报告中并列，不得任选一份静默覆盖结论。
   - **部分副本一致、其余缺失**：结论限定为“在已取得的 k 份副本间一致（k < N*F）”，
     并明确未取得副本的盘号与预期物理位置。
4. 表述纪律：**禁止把“首选副本读取成功”写成“RAID10 整组副本一致/校验通过”**。
   一次单副本读取至多证明该副本自身可读，不构成对镜像组的一致性证明。

### 7.3 行号公式发散的专项兼容

对 `far_copies > 1` 的卷（far / offset / near+far），现有流的物理行号与内核语义不同
（第 6 节）。兼容处理应：

- 不直接使用 `RAID10Stream` 的寻址结果作为副本物理位置证据；
- 改用第 5 节闭式公式（区分 `far_offset` 两种行号规则，并计入 `logical_rows` 远区域跨度）
  重新计算每一份副本的盘号与物理行；
- 纯近卷（`F=1`，含 DDF adjacent `0x102`）可继续信任流的行号；
- 引用 DDF `RAID1E_OFFSET`（`0x201`）时记住它被映射为**非 offset** far 布局，按 `O=False` 公式处理。

---

## 8. 复算速查表（以 stripe_size = S 为单位）

给定逻辑 chunk `c`、盘数 `R`、近 `N`、远 `F`、offset 标志 `O`、总 chunk 数 `T`：

```
dev0        = (c*N) % R
stripe0     = (c*N) // R
L           = ceil(T*N / R)              # 每远区域物理行数

# 盘号（fc∈[0,F), nc∈[0,N)，减法回绕）
disk(fc,nc) = wrap(dev0 + fc*N + nc, R)

# 物理 chunk 行
O=False : row(fc) = fc*F*L + F*stripe0
O=True  : row(fc) = stripe0 + fc

# 物理字节
phys(fc,nc) = PD(disk(fc,nc)).offset + disk_map[disk(fc,nc)].device_start + row(fc)*S + (offset % S)

# dissect 流实际只取（单一位置）
stream_dev  = dev0
stream_row  = stripe0 * (F if O else 1)
stream_phys = PD(stream_dev).offset + disk_map[stream_dev].device_start + stream_row*S + (offset % S)
```

边界与奇数盘：盘号回绕只用减法（`d >= R` 时减一次 `R`），不用取模；奇数盘在近布局下会产生
空槽（同一物理单元格被两个逻辑 chunk 共享或留空），公式中的 `ceil` 已涵盖该情况。

---

## 9. 验证方法与可复现性

本报告结论由以下独立手段交叉确认（分析脚本位于仓库外的独立目录，未接触测试数据中的
不可信镜像内容，仅用内存中合成 `BytesIO` 与本机内核模块）：

1. **内核反汇编 + BTF**：反汇编本机 `/usr/lib/modules/6.8.0-151-generic/kernel/drivers/md/raid10.ko`
   的 `__raid10_find_phys`、`setup_geo`，结合 `/sys/kernel/btf/raid10` 的 `struct geom`
   字段偏移，确认 `stride = raid_disks * far_copies`、近/远副本盘号推进与回绕分支。
2. **`md(4)` 手册布局图**：对 near/far/offset 在 4 盘与 5 盘下的每个图示单元格，用闭式公式
   重放并逐格比对，全部通过（offset 单元格按手册如实包含两个 chunk）。
3. **合成镜像实测 dissect 流**：构造带非零 `PhysicalDisk.offset` 与 DDF `starting_block` 的
   `VirtualDisk`，monkeypatch `seek` 记录实际物理位置，验证复合寻址公式、缺盘 `KeyError`、
   以及构造期无成员数校验。
4. **全量枚举**：对 R=3..6、N/F 的 12 种布局、每卷 600 chunk，验证副本数 == 不同盘数，
   并统计流与内核第 0 副本在盘号（全一致）与物理行号（仅纯近全一致）上的差异。

未运行仓库自带 `tests/md`、`tests/ddf` 用例：其 `tests/_data/**` 为 Git LFS 指针，
当前环境未拉取实体镜像（`gzip` 打开报 `BadGzipFile`），相关结论不依赖这些二进制夹具。
