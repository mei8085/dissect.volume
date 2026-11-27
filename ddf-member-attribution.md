# DDF 虚盘成员归属分析：身份链、路径交换、缺盘与缺口判据

> 范围：`dissect/volume/ddf/`（DDF 元数据解析与虚盘装配）与 `dissect/volume/raid/`（RAID 读流）。
> 方法：静态阅读仓库代码 + 用仓库自带 `c_ddf` 结构构造合成 DDF 镜像做实证（实验代码位于仓库外
> `../ddf-experiments/`，不污染工作区；仓库测试数据为 Git-LFS 指针，沙箱内不可拉取，故未直接复用）。
> 结论中的每一条行为都标注了源码位置，并经合成镜像复现。

---

## 1. 控制器记录 → 虚盘区间的身份链（成员关系到底由什么决定）

DDF（SNIA Common RAID Disk Data Format）在每块物理盘末尾放一个 anchor 头，其余元数据相对
primary/secondary 头按 LBA 区段组织。本仓库解析出的身份链是一条**纯引用（reference）链**，
与“文件句柄的传入顺序”和“物理路径字符串”都没有关系。

### 1.1 链上的五个环节

| 环节 | 结构 / 字段 | 代码位置 | 作用 |
|---|---|---|---|
| ① 阵列归属 | anchor `DDF_Header_GUID` | `ddf.py:39` | 相同 header GUID 的盘被归到同一个 `DDFConfiguration`（`config_map.setdefault(...)`） |
| ② 盘自证身份 | PDD 区段 `Physical_Disk_Data.PD_GUID / PD_Reference` | `ddf.py:164-168`, `ddf.py:195-203` | 每块盘自己声明“我是哪个 PD_Reference、哪个 PD_GUID”。`self.guid/self.reference` 直接取自这里 |
| ③ 控制器名册 | PDR 区段 `Physical_Disk_Entry`（`PD_GUID, PD_Reference, PD_State, Path_Information, Configured_Size, Block_Size`） | `ddf.py:149-150`, `ddf.py:206-219` | 控制器维护的物理盘名册；仅用于反查“我这块盘的 PDR”以取得 `type/state/path/block_size/size`（`ddf.py:170-179`） |
| ④ 虚盘拓扑 | VDCR `VD_Configuration_Record` 变长数组 `Physical_Disk_Sequence[]` + `Starting_Block[]` | `ddf.py:238-258` | 逐元素给出“阵列位置 i 上是哪个 `PD_Reference`，数据从该盘哪个块开始” |
| ⑤ 引用→实例解析 | `pd_map[pd.reference] = pd`，再 `pd_map.get(pds)` | `ddf.py:52`, `ddf.py:67-69` | 把 VDCR 里的引用号解析成实际在场的 `DDFPhysicalDisk` 实例 |

最终落盘的映射是（`ddf.py:60-72`）：

```python
vd_pd_map[sec * count + i] = (starting_block, matched_pd)
# DiskMap = {阵列槽位: (该成员上的起始字节偏移, 物理盘对象)}
```

读的时候再由 `raid/stream.py` 用 RAID 算法把虚盘偏移换算成**槽位号**，用槽位号查这张表，
物理寻址为 `device.offset + device_start + offset_in_device`
（`stream.py:174-175`、`stream.py:367-368`、`stream.py:425`）。

### 1.2 关键事实：读取顺序不是阵列位置

- 句柄列表的顺序**不参与**槽位编号。`DDF.__init__` 只是逐盘构造对象（`ddf.py:34-41`），
  槽位号 `i` 来自 VDCR 数组的**下标**，引用号来自数组**值**。
- 实证（E2）：把三块盘的句柄顺序完全反转，`disk_map` 仍然是
  `slot0→ref ...01 / slot1→ref ...02 / slot2→ref ...03`，读出的首字节不变（`a0a0a0a0`）。
- `Path_Information`（`c_ddf.py:100`，18 字节）被解析进 `self.path_information`（`ddf.py:176,215`），
  但**全代码库没有任何地方用它做匹配或寻址**。它只是可读属性。
- 因此“路径交换”（线缆/枚举顺序变化、`Path_Information` 改变）在盘内容完好时**不影响归属**：
  归属锚定在 PDD 的 `PD_Reference` 上，而不是路径上。这是设计上正确的一面。

### 1.3 `starting_block` 的语义与单位

- VDCR 每个元素配对一个 `Starting_Block`（`c_ddf.py:246` 注释、`ddf.py:258`），它跟
  `Physical_Disk_Sequence` **按相同下标成对**，描述该成员数据区在该盘上的起点。
- 实证（E14）：给三盘分别配 `0x100/0x200/0x300`，首次读的实际 `seek` 偏移就是 `0x100`（字节），
  代码路径 `offset_on_disk = dd_dev.offset + dd_start + offset_in_device`（`stream.py:367`）
  **不再乘以 `block_size`**。即本实现把 `starting_block` 当作原始字节偏移使用。
- 含义：`starting_block` 是“每元素、随引用走”的权威块范围。一旦槽位与引用错位（见 §3.2），
  被错配的不只是盘，连盘上的块区间也一起被错配——属于双重误归属。

---

## 2. 元数据多副本是怎么合并的（以及为什么不可靠）

每块盘都带一份完整元数据，`DDFConfiguration` 用字典 `update` 做跨盘合并（`ddf.py:51-57`）：

```python
pd_map[pd.reference] = pd                                   # 同 reference：后者覆盖
vde_map.update({vde.guid: vde ...})                         # 同 VD_GUID：后者覆盖
vdcr_map.update({vdcr.guid: vdcr ...})                      # 同 VD_GUID：后者覆盖
vdcr_uniq.update({(vdcr.guid, vdcr.secondary_element_seq): vdcr ...})
```

### 2.1 VDCR 新鲜度不做任何裁决

- 合并键是 `VD_GUID`（以及二级阵列的 `(guid, secondary_element_seq)`），**完全不看**
  `Sequence_Number`、`Timestamp`、`CRC`、`Open_Flag`。
- 实证（E13）：一块盘放 `seq=9/timestamp=999` 的新 VDCR，另两块放 `seq=1/timestamp=100` 的旧副本。
  句柄正序时选中新副本，**把句柄顺序反转后选中旧副本**。选哪份纯粹取决于输入句柄顺序，
  同一份逻辑阵列在不同喂入顺序下会得到不同的拓扑/条带映射 → 非确定性误归属。

### 2.2 CRC 只解析不校验、secondary 头只读取不使用

- 所有结构都有 `CRC` 字段（`c_ddf.py:17,63,107,...`），但 `ddf.py` 全文没有任何 CRC 计算或比对。
  损坏/被篡改的元数据会被静默信任。
- secondary 头在 `ddf.py:138-141` 被读进 `self.secondary_header`，但
  `self.active_header_offset = self.primary_header_offset`（`ddf.py:143`）是写死的，
  后续所有区段都相对 primary 头寻址（`ddf.py:145-164`）。没有按 `Sequence_Number/Open_Flag`
  在 primary/secondary 间选较新或较干净的一份。primary 区损坏而 secondary 完好时，本实现不会自愈。

### 2.3 同一引用号的两块盘：静默丢盘

`pd_map[pd.reference] = pd`（`ddf.py:52`）是后写覆盖。实证（E5）：让两块盘 PDD 都声称
`ref=...01`，最终 `disk_map` 只剩两个槽，先出现的那块物理盘**无任何告警地从映射中消失**。
重复引用可能来自：换板/强制上线（PDD 的 `Forced_Ref_Flag`，`c_ddf.py:284-292`）、元数据损坏、
或把不同阵列的盘混喂。

---

## 3. 路径交换与缺盘：哪些字节仍可归属，哪些只能是缺口

### 3.1 路径交换（path swap）——身份链成立时，全部字节仍可正确归属

只要满足：anchor header GUID 一致、PDD 的 `PD_Reference` 与 VDCR 的 `Physical_Disk_Sequence`
能对上、且该引用号在 `pd_map` 中唯一，那么无论句柄顺序/`Path_Information` 如何变化：

- 槽位由 VDCR 下标决定，引用解析由 `pd_map.get(pds)` 决定，与枚举顺序无关；
- 全部虚盘字节都能归属到正确的物理盘与正确的 `starting_block` 区间。

但这条保证依赖三个**当前代码并未强制**的前提（见 §4 误归属风险）：引用号唯一、PDD/PDR 的 GUID 一致、
VDCR 副本一致。任何一个前提被破坏，“路径交换也安全”的结论立即失效。

### 3.2 空元素 `0xFFFFFFFF`：当前实现会压缩槽位（高危误归属）

VDCR 用 `0xFFFFFFFF` 表示未使用/空的物理盘序列项（`ddf.py:71` 对此值做了特判）。DDF 的数组下标
本身承担“阵列位置”语义，空槽应当**保留位置**。但当前循环是：

```python
i = 0
for starting_block, pds in zip(vdcr.starting_block, vdcr.physical_disk_sequence, strict=False):
    if matched_pd := pd_map.get(pds):
        vd_pd_map[sec * count + i] = (starting_block, matched_pd)
    if pds != 0xFFFFFFFF:
        i += 1
```

（`ddf.py:66-72`）。注意 `i` 对空元素不自增，于是**后面的存活成员被压缩进前面的空槽**。
实证（E4）：序列 `[ref01, 0xFFFFFFFF, ref03]`、起始块 `[100, 0, 200]` 被解析成
`slot0=(100,ref01)`、`slot1=(200,ref03)`——`ref03` 从阵列位置 2 挪到了位置 1，且它的
`starting_block=200` 跟着一起移动。RAID 几何（条带旋转、奇偶位置）按槽位号计算，
于是该成员承载的所有条带都被算到错误的物理盘和错误的块区间。这是“静默数据错配”，比直接报错更危险。

另外两点：
- `zip(..., strict=False)`（`ddf.py:67`）：若两个变长数组因损坏而长度不一，较短一方被静默截断，
  多出来的成员直接丢弃，不报错。
- `primary_element_count` 只用于切片（`ddf.py:257-258`），不参与槽位保留逻辑。

### 3.3 缺盘：按 RAID 级别分别会发生什么

“缺盘”指 VDCR 引用了某个 `PD_Reference`，但 `pd_map` 里没有（盘没喂进来）。
`pd_map.get(pds)` 取不到时**只是跳过，不写任何占位**（`ddf.py:68-69`），于是 `disk_map` 里出现空洞键。
后续行为按级别分化（均经合成镜像实证）：

| 级别 | 构造/读取行为 | 实证结果 | 字节可归属性 |
|---|---|---|---|
| LINEAR | `LinearStream` 要求 `len(disk_map)==num_disks`，否则 `RAIDError`（`stream.py:59-60`） | E8：缺一盘即整个虚盘打不开 | 整盘不可读；即使其余成员数据完好也拿不到，**没有部分可读** |
| RAID0 | 同样的全等校验（`stream.py:81-82`） | E8：缺一盘即 `RAIDError` | 同上，条带化使缺盘成员的条带本就无法补齐，但当前连幸存条带也一并拒绝 |
| RAID1 | 不校验数量，`next(iter(disk_map))` 取“字典序第一个在场盘”直接当镜像源（`raid.py:72`） | E9：只剩一块也能读；且该盘 PDR `PD_State=FAILED` 仍照读 | 可读，但**不看 `PD_State`、不比较多副本新旧**，可能读到被标记失败/陈旧的镜像 |
| RAID4/5/6 | 仅在“缺太多盘”时构造报错（阈值 `num_disks - max_degraded`，`stream.py:194-195`）；允许降级构造 | E3/E7：构造通过；读到落在缺失槽位的条带时 `self.disk_map[dd_idx]` 抛 **`KeyError`**（`stream.py:362`） | 落在在场槽位的条带可归属；落在缺失槽位的条带直接异常，**不做奇偶/XOR 重构、不合成缺口** |
| RAID10/1E | 构造期不校验盘数；`RAID10Stream._read` 用 `__raid10_find_phys` 只算出**一个** `dev` 就 `disk_map[dev]`（`stream.py:420`） | E15+追加实验：计算出的主槽位缺失时 `KeyError`，即使相邻槽位有相同镜像副本也不会切换 | 主槽位在场的条带可读；主槽位缺失的条带崩溃，**明明有镜像可救却不回退** |

RAID5 的实证条纹（3 盘 left-asymmetric，缺槽位 2）：虚条带 0/1/2/4 正常，条带 3/5 因数据落在
槽位 2 而 `KeyError(2)`。也就是说，**同一虚盘里一部分字节可归属、另一部分只能算缺口**，
但当前实现对缺口的表达方式是“抛异常中断整个读”，而不是返回一段带标记的空洞。

### 3.4 逻辑零值 ≠ 未取得的成员数据（必须区分的两类“全零”）

这是本报告需要明确强调的区分：

1. **逻辑零值（real zero）**：成员盘在场、寻址成功，该区间内容本身就是零（未写入、被清零、
   文件系统空洞、初始化写零）。这是**已取得、可归属**的真实数据，字节就是 `0x00`。
   实证：构造一块数据区全零的在场成员，其条带读出确实是 64KiB 全零，且来源可追溯到具体物理盘。

2. **未取得的成员数据（gap / unavailable）**：目标槽位在 `disk_map` 中根本不存在（缺盘、
   空元素、引用无法解析）。当前代码在这里抛 `KeyError`，**什么字节都没读到**。

二者绝不能混为一谈：

- 若为了“读起来不报错”而把缺口**零填充**，输出的全零缓冲与第 1 类真实零数据**逐字节不可区分**，
  下游取证会把“缺失”误判为“该区域本来就是空的”，这是危险的伪造证据。
- 框架本身具备表达空洞的能力：`MappingStream._read` 遇到没有 run 覆盖的偏移会**短读**
  （`dissect/util/stream.py:349-352`，实证 E10：跨 64 字节空洞的 128 字节读只返回前 64 字节）。
  但 DDF 的 LINEAR 装配（`stream.py:62-65`）只 `add` 在场盘、从不插入空洞 run，
  RAID4/5/6 与 RAID10 则直接用字典下标硬取，因此 DDF 路径上**没有任何一级把缺口显式建模**。
- 正确做法应让缺口以“带偏移/长度的缺口描述 + 短读或哨兵异常”呈现，而不是零填充，也不是让一次
  `KeyError` 中止整个虚盘的顺序读取。

---

## 4. 误归属风险清单

| # | 风险 | 源码位置 | 触发条件 | 后果 |
|---|---|---|---|---|
| R1 | 空元素槽位被压缩，存活成员连同 `starting_block` 一起前移 | `ddf.py:66-72` | VDCR 序列中出现 `0xFFFFFFFF`（成员曾经缺失/扩容/降级） | 阵列位置与物理盘错位，条带/奇偶全盘错配，静默错误数据 |
| R2 | VDCR/VDE 多副本按句柄顺序 `update`，不看 `Sequence_Number/Timestamp/CRC` | `ddf.py:53-57` | 各盘元数据版本不一致（掉电、部分写入、混插） | 同阵列随喂入顺序得到不同拓扑，非确定性误归属（E13） |
| R3 | 重复 `PD_Reference` 被字典覆盖，静默丢一块盘 | `ddf.py:52` | 强制上线/换板/元数据损坏导致两盘同引用 | 一块物理盘从映射消失，槽位指向意外实例（E5） |
| R4 | PDD 自证 GUID 与 PDR 名册 GUID 不做交叉校验 | `ddf.py:167-174` | PDD 被篡改或 PDR/PDD 不同步 | 仅靠引用号匹配，伪造/错位身份被接受（E5） |
| R5 | CRC 全不校验 | `ddf.py` 全文 | 任一元数据区段损坏 | 损坏字段被当权威配置，几何/引用全错且无告警 |
| R6 | secondary 头读而不用，primary 写死为活动头 | `ddf.py:138-145` | primary 损坏、secondary 完好 | 不能利用冗余副本恢复，直接解析失败或采信坏数据 |
| R7 | `zip(strict=False)` 静默截断 | `ddf.py:67` | 两变长数组长度不一致 | 成员被悄悄丢弃 |
| R8 | RAID1 不看 `PD_State`、不比较副本，取字典序第一个 | `raid.py:68-73` | 仅剩失败/陈旧镜像 | 读回失败盘或过期数据（E9） |
| R9 | RAID4/5/6 缺槽位 `KeyError`，无奇偶重构、无缺口建模 | `stream.py:362,367-369` | 降级状态读缺失条带 | 顺序读取整体中断；可重构的数据也不重构（E3/E7） |
| R10 | RAID10 主槽位缺失即 `KeyError`，不回退镜像 | `stream.py:415-425` | 1E/10 降级 | 有镜像副本却拒绝服务 |
| R11 | `_read` 用原始 `length` 而非 `read_length` 读盘 | `stream.py:369` 与 `427` | 非对齐/跨条带直调 `_read` | 跨条带过读，循环按 `read_length` 记账却消费了 `length` 字节，归属范围越界（E11） |
| R12 | anchor header GUID 不同即拆成多个 configuration | `ddf.py:37-41` | 混插外来盘/头 GUID 被改 | 阵列被静默拆成多组，组内各自缺盘（E6） |

---

## 5. 修复判据（满足才算“归属正确、缺口诚实”）

### 5.1 身份链与映射正确性

1. **槽位保位（修复 R1/R7）**：VDCR 映射必须以数组下标为权威阵列位置。对 `0xFFFFFFFF` 空元素，
   保留该槽位为“空缺”，后续元素不得前移；`starting_block` 与引用严格按同下标成对绑定。
   两个变长数组长度不一致时应抛 `DDFError`（改 `strict=True` 或显式校验），不得截断。
2. **引用唯一性校验（R3）**：构造 `pd_map` 时检测到重复 `PD_Reference` 必须报错或显式标记冲突，
   不得静默覆盖。
3. **身份交叉校验（R4）**：PDD 的 `PD_GUID/PD_Reference` 必须与 PDR 中同 `PD_Reference` 条目的
   `PD_GUID` 比对，不一致即标记为身份冲突，不得仅凭引用号采信。
4. **整组一致性（R12）**：同组内 anchor `DDF_Header_GUID` 不一致要作为外来/混插告警，而非默默拆分。

### 5.2 元数据副本裁决

5. **新鲜度优先（R2）**：同一 `VD_GUID`（及 `(guid, secondary_element_seq)`）的多份 VDCR/VDE，
   按 `Sequence_Number` 再按 `Timestamp` 选最新，而不是按句柄顺序；同序号冲突要显式报歧义。
6. **CRC 校验（R5）**：对各结构 `CRC` 做校验，校验失败的副本不得参与裁决，并上报损坏。
7. **primary/secondary 选择（R6）**：比较两头的 `Sequence_Number/Open_Flag/CRC`，选用较新且 CRC
   通过的一份作为 `active_header_offset`，而不是写死 primary。

### 5.3 缺口语义（区分零值与未取得）

8. **显式缺口模型（R9/R10）**：`disk_map` 缺失的槽位要在虚盘地址空间里产生一段**带偏移和长度的
   缺口描述**，读取时对该段返回短读或专门的“不可用”哨兵/异常，**严禁零填充**。逻辑零值（在场盘
   读出的真实 `0x00`）与缺口（无盘可读）必须在 API 层面可区分。可复用 `MappingStream` 的 run/空洞
   机制（`dissect/util/stream.py`）承载。
9. **降级读取策略**：
   - RAID4/5/6：缺失槽位若仍满足降级阈值，对数据条带用奇偶/XOR **重构**；无法重构（超过
     `max_degraded`）才报缺口。构造期阈值校验保留（`stream.py:194-195`），但读取期不应让一次
     `KeyError` 中止整盘顺序读。
   - RAID1/RAID10：主副本槽位缺失时，按 near/far 副本关系切换到在场镜像（修复 R10）；
     选副本时结合 `PD_State`（`ONLINE/REBUILD/FAILED/...`，`c_ddf.py:86-92`）与 VDCR 序号，
     优先在线且最新者（修复 R8）。
   - LINEAR/RAID0：缺盘时仍应开放幸存成员对应的虚盘区间，仅把缺失成员对应区间报为缺口，
     而非整盘 `RAIDError`。

### 5.4 读路径正确性

10. **修复过读（R11）**：`RAID456Stream._read` 与 `RAID10Stream._read` 内
    `device.fh.read(length)` 改为 `read(read_length)`，使磁盘实际消费字节数与循环记账一致，
    杜绝跨条带越界归属。
11. **`starting_block` 单位明确化（§1.3）**：确认 DDF `Starting_Block` 的单位约定（字节还是块）。
    若规范单位是块，寻址处需统一乘 `block_size`；无论采用哪种，都要在代码中固定并加测试，
    避免当前“按原始字节直接相加”的隐含假设在 4Kn 盘（`Block_Size != 512`）上出错。

### 5.5 验收测试建议

- 路径交换：随机打乱句柄顺序、改写 `Path_Information`，断言 `disk_map` 槽位→引用映射与读出字节
  完全不变。
- 空元素：构造含中间 `0xFFFFFFFF` 的 VDCR，断言存活成员保留原阵列下标、`starting_block` 不随迁。
- 缺盘分级：对每个 RAID 级别分别缺盘，断言可归属区间正常读、缺口区间返回显式缺口而非零填充或
  裸 `KeyError`；RAID5/6 断言可重构条带被正确重构。
- 零值 vs 缺口：同一虚盘内同时放置“在场全零条带”和“缺盘条带”，断言 API 能区分二者。
- 副本裁决：制造不同 `Sequence_Number/CRC` 的多份 VDCR，断言与句柄顺序无关地选中最新且 CRC 通过者。
- 身份冲突：制造重复引用、PDD/PDR GUID 不一致，断言显式报错而非静默覆盖/采信。

---

## 附：实证实验索引（仓库外 `../ddf-experiments/`）

| 脚本 | 验证结论 |
|---|---|
| `e1_sanity.py` | 合成 3 盘 RAID5 可被本仓库解析、装配、读取（基线） |
| `e2_attribution.py` | E2 路径交换不影响归属；E3 缺盘 `KeyError`；E4 空元素槽位压缩；E5 GUID 不校验/重复引用丢盘；E6 头 GUID 拆组 |
| `e3_gaps.py` | E7 降级条带行为；E8 RAID0/LINEAR 整盘拒绝；E9 RAID1 不看 state；E10 MappingStream 短读空洞；E11 过读 |
| `e4_stripes.py` | 逐条纹定位 RAID5/RAID10 哪些槽位可读、哪些 `KeyError` |
| `e5_freshness.py` | E13 VDCR 新鲜度随句柄顺序；E14 `starting_block` 直达物理 seek；E15 RAID10 不回退镜像 |
