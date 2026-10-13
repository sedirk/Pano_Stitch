# Hugin 拼接优化实验

> 本文保留各轮实验的过程、旧参数与本机结果，文中的运行目录和照片不随仓库上传。当前安装与复现请从[仓库首页](../README.md)和[完整操作手册](../docs/WORKFLOW.zh-CN.md)开始；普通相机圆柱长条流程在文末，不能套用早期的球面导出命令。

当前最终输出默认为 **Hugin 原图像素密度下的全分辨率 JPG**，质量 97、4:4:4、sRGB；自动地平线矫正默认开启。`run_preview.ps1` 仍明确指定 4096 宽预览。天空颜色补偿入口为 `run_sky_balance.ps1`，已有工程调平用 `run_horizon.ps1`，已有修正工程直接导出用 `run_export.ps1`。没有接入神经网络或局部几何形变。

## 运行

在 PowerShell 中：

```powershell
cd 路径\Pano_Stitch
.\Hugin_test\run_preview.ps1
```

早期运行器优先使用开发环境附带的 Python，工具放在项目内 `.tools/hugin/Hugin/bin`，输出到新的 `Hugin_test/runs/时间戳` 目录。当前普通相机初始化还需要 NumPy/SciPy，完整依赖见 `requirements-color.txt`。独立安装请使用虚拟环境并传 `-PythonExe`，照片和 PTO 也应显式指定。原照片、旧工程和旧 TIFF 保留在本机；`test.py.original` 备份不在公开仓库中。

直接导出本次已完成颜色修正及调平的工程：

```powershell
.\Hugin_test\run_export.ps1
# 快速检查新工程：
.\Hugin_test\run_export.ps1 路径\工程.pto --width 4096
```

常用参数：

| 参数 | 用途 |
|---|---|
| 第一个位置参数 | 输入 JPEG 文件夹，默认本目录下 `001_0546` |
| `--hugin-bin 路径` | 指定另一套 Hugin 工具，也可设置 `HUGIN_BIN` |
| `--width 8192` | 指定 8192×4096；最终导出不指定宽度时用 Hugin AUTO 原图分辨率 |
| `--jpeg-quality 97` | 默认 97，范围 1–100；始终不作色度降采样 |
| `--tile-size 4096` | 工作块核心边长，默认 4096，不改变最终输出尺寸 |
| `--workers 2` | 同时渲染两块以加速，增加内存占用；默认 1 |
| `--threads 6` | 限制匹配和渲染线程数 |
| `--stop-after prepare` | 只读元数据和创建初始工程 |
| `--stop-after optimize` | 只计算几何与误差，不渲染 |
| `--matches-from 工程.pto` | 重用相同图像顺序与尺寸的控制点，重新计算其余步骤；输入文件不能在重用期间改动 |
| `--profile baseline` | 修正基础错误后的默认 cpfind 匹配配置，作为对照 |
| `--pose-source none` | 不使用 DJI 元数据，改用 multirow 匹配；不连通就停止 |
| `--strict` | 即使有姿态先验，也不接受不连通的匹配图 |
| `--optimize-hfov` | 除姿态外拟合共享镜头 HFOV，PowerShell 启动器默认启用 |
| `--level-horizon auto` | 默认开启，几何优化完成后全局调平；天空修正入口也支持 |
| `--level-horizon off` | 关闭自动地平线矫正，保留原有全景朝向 |
| `--celeste` | 尝试排除云层特征，默认关闭，需另做对照 |

普通入口不会自动下载依赖。Hugin、libvips 来源及版本见[公开依赖说明](../docs/DEPENDENCIES.md)；本机另存 `.tools/PROVENANCE.txt`，不随仓库分发。实测环境为 Windows Hugin 2025.0.1、libvips 8.18.7。

## 原实现中的实际错误

- 固定输入 `E:/Hugin_test/001_0546` 和外部工具目录，不能随文件夹迁移。
- `y/p` 的正则要求小数点，无法修改新工程里的 `y0/p0`。
- `re.sub(r'v\d+', 'v30', line)` 把 `v27.774...` 改成 `v30.774...`，还把 `Eev12.614...` 改成 `Eev30.614...`。
- 元数据读取失败时静默返回 `(0, 0)`，无法区分真实零度与缺失信息。
- 每张照片启动一次 ExifTool；现改为一次批量 JSON 调用。
- 不检查命令退出状态、匹配图、优化偏移和输出是否存在。
- `autooptimiser -p` 是从首图开始的成对优化，并非“锁定位置只微调”。新版明确写入待优化变量，用 `-n` 执行。
- Hugin 的自动优化预设会覆盖显式变量；新版同时关闭几何/光度自动预设，确保共享视场角与固定锚点实际生效。
- 默认画布为 46028×23014，约 10.59 亿像素。现在先输出 4096 宽预览。

## 几何与算法改动

1. 保留 EXIF 视场角和镜头链接，仅在元数据完整时初始化相对 yaw、pitch、roll。此次初始 HFOV 为 27.774389°。
2. 完整姿态先验使用 `--prealigned`；无完整姿态先验使用 `--multirow`，二者不混用。
3. 优化配置使用 `rpy` 旋转模型验证、RANSAC 参数 8、至少 12 个匹配点、每个格子最多 2 点。参数的实际语义以当前 Hugin CLI 为准，不把该 RANSAC 参数等同于最终原图误差。
4. `cpclean` 先做图对内检查，随后显式指定姿态和可选 HFOV 做全局优化。每个不连通组件固定一个锚点；孤立照片保持初始姿态。
5. 用 Hugin 原生镜头变换把控制点映射到球面，按视线夹角乘平均原图焦距计算等效像素残差。这样不会因等距柱状图靠近极点而虚增误差。
6. 根据残差中位数/MAD 删除离群点，阈值限制在 8–24 等效原图像素，删除不足 8 个剩余点的图对，然后重新优化。
7. 检查姿态是否偏离元数据过多；输出保留 360° 左右环绕融合。默认保留已处理 JPEG 的曝光外观，避免把 EXIF 曝光值直接当作可靠色彩校准。

这仍是近似纯旋转模型。大平移、鱼眼、显著视差需要其他模型，当前会拒绝非普通透视镜头。`--photometric` 是可选实验分支，仅接受连通图；没有在这组不连通天空素材上启用。

## 结果怎么理解

每次运行保存 `report.json`、逐阶段 `.pto` 和命令日志。报告给出匹配图、无控制点照片编号（从 0 开始）、残差、耗时、警告和最终状态。`preview_with_priors` 表示部分布局来自元数据，并不表示所有照片都经过图像证据验证。

这组数据的第 21–69 张照片主要是低纹理天空。不能通过强迫输出更多控制点来证明这些图已经精确对齐。最终残差是用于优化的控制点残差；它不是独立检查点精度，也不能单独证明接缝画质。预览中的未覆盖天顶保留透明，不生成内容补洞。

测试命令：

```powershell
& .venv\Scripts\python.exe -m unittest discover -s Hugin_test\tests -p test_stitch.py -v
```

测试覆盖曝光误改、整数/负数/科学记数法/链接字段、含空格文件名、缺失姿态、图不连通、无控制点、命令失败和姿态发散。

## 2026-10-13 的实测结果

成功运行目录：`runs/optimized_v2`。包含全部 137 张原图，输出 `panorama.tif`（4096×2048、RGBA，约 19.5 MB）及用于查看的 `preview.jpg`。TIFF 保留透明的未覆盖天顶；JPEG 将该区域显示为深灰色。

| 比较项 | 旧保存工程 | 修正基础错误的默认匹配对照 | 优化配置 |
|---|---:|---:|---:|
| 工程内控制点 | 5364 | 5352 | 9318 |
| 共同 5359 个对应点的残差中位数 | 75.92 px | 7.21 px | 4.08 px |
| 同一批对应点的 P95 | 437.67 px | 31.20 px | 11.14 px |
| 执行状态 | 已有文件，仅做几何诊断 | 天空图滚转偏移超限，停止 | 完成预览，含姿态先验 |

比较使用旧工程中属于主连接组的同一批点，具体数据见 `diagnostics/comparison/comparison.json`。这些点仍包含旧的错误对应（最大残差很大），也不是独立标注的检查点；表格不能解释为实景定位精度。原图尺寸为 4032×3024，指标按原图焦距换算，不是按缩小预览的像素计算。默认匹配对照因第 58 张图片滚转偏移约 8.79°被拒绝，表中保留其停止前的诊断数值，不将其算作成功输出。

优化配置最终保留 254 个图对、9318 个控制点；自身拟合残差中位数 3.73 px、P95 为 7.25 px。主组覆盖 88 张照片，另外 49 张天空照片没有控制点，保留元数据姿态。共享 HFOV 从 27.774389°拟合为 28.190928°。没有伪造控制点去连接纯天空。

关键阶段日志耗时：匹配约 158 秒、两次几何优化约 29+23 秒、预览渲染约 100 秒。匹配时同时跑了对照，不能据此作严格的速度比较。第二次运行用 `--matches-from` 重用第一次真实匹配的结果，所有命令和来源都记录在报告中。

已检查预览：主体地形可组成连贯画面，但天空仍有明显的大块颜色过渡。这轮没有解决自动白平衡/曝光不一致，也未在原分辨率验证云层接缝。后续应固定当前几何，在重叠区做受约束的颜色补偿，并单独比较接缝选源；不应以低分辨率预览代替最终画质验证。Enblend 本次因输入重投影图层较小自动切换到 fine mask，相关提示保存在 `12_render.log`。

复现优化结果（重用已生成的匹配，只能用于未修改的这组原图）：

```powershell
.\Hugin_test\run_preview.ps1 --matches-from Hugin_test\runs\optimized_v1\03_matched.pto --level-horizon off
```

诊断对照脚本：

```powershell
& .venv\Scripts\python.exe Hugin_test\compare_runs.py Hugin_test\temp_project.pto Hugin_test\runs\baseline_v2\06_fitted.pto Hugin_test\runs\optimized_v2\08_refined.pto
```

参数依据：[cpfind 官方手册](https://hugin.sourceforge.io/docs/manual/Cpfind.html)、[autooptimiser 官方手册](https://hugin.sourceforge.io/docs/manual/Autooptimiser.html)、[cpclean 官方手册](https://hugin.sourceforge.io/docs/manual/Cpclean.html)，并核对了本机工具的 `--help`。

## 第二轮：天空色差修正

### 原因证据

`diagnostics/color_metadata.json` 保存了全部 137 张源照片的 EXIF/DJI 字段。ExifTool 可读输出确认 `WhiteBalance=0` 是 Auto；所有照片都使用自动白平衡。全组 `WhiteBalanceCCT` 为 **4687–8598 K**；天空的 49 张为 **4995–8598 K**。全组快门为 1/2000–1/80 秒，ISO 为 100–250，光圈固定 f/2.8；天空组快门为约 1/1500–1/160 秒，ISO 为 100–130。

因此这次素材存在逐张变化的自动白平衡和曝光，足以造成大块的颜色/亮度不一致。这个结论针对本次照片，不能推断所有 DJI 机型、固件和全景模式都采用同一种锁定策略。已有 JPEG 还可能包含镜头校正、局部色调处理；目前不能把镜头原生暗角和这些处理可靠地分离。试过的单一共享径向暗角模型没有解决色块，不能据此给出镜头暗角的标定值。

### 实现和运行

```powershell
cd 路径\Pano_Stitch
.\Hugin_test\run_sky_balance.ps1
```

默认读取 `runs/optimized_v2/11_preview.pto`，在新的 `runs/sky_时间戳` 中输出；不会覆盖旧结果或原图。也可把另一份已对齐、光度参数中性的 PTO 作为第一个参数。`--stop-after fit` 只提取重叠样本、拟合并验证，不渲染。`--output 路径` 必须指定尚不存在的新目录。

这一步需要 NumPy、SciPy、Pillow 和 tifffile，依赖列表见 `requirements-color.txt`。本机启动器曾使用开发环境附带的 Python 3.12，缺少的库放在项目内 `.tools/python-color`；这些依赖不随仓库上传。其他环境请安装依赖并指定 `-PythonExe`。早期 DJI 编排仅需标准库，当前普通相机初始化另需 NumPy/SciPy。

处理步骤：

1. 固定已经验证的所有几何参数。按照镜头足迹的最低仰角大于 2°选择天空候选，本组正好是 `PANO_0021.JPG`–`PANO_0069.JPG`。这是针对本组航拍的几何筛选，不是语义天空分割；拍摄高山、树冠、室内或不同镜头模型时需要重新判断。
2. 用 Hugin `vig_optimize -w` 从真实投影重叠中提取 RGB 对应样本。重叠像素不依赖天空控制点，因此可以使用没有纹理匹配点、但有姿态先验的照片。剔除降采样导致坐标落到原图边界外的少量样本。
3. 每张天空照片拟合 **4×4 RGB 加性补偿网格**，在已编码的 JPEG RGB 域做双线性插值。固定地平线和地面照片作颜色参照；以一阶/二阶平滑正则和 Huber IRLS 约束缓慢变化的颜色场。它是外观匹配模型，不是物理白平衡、暗角或线性辐射定标模型。
4. 每个图对最多取 18 个样本训练，其余留出验证，固定随机种子 20261013。检查所有天空图是否能通过实际重叠连接到固定参照、修正幅度是否过大、验证色差是否下降。
5. 在 **4032×3024 原尺寸**上应用网格，保存 16 位 RGB TIFF；其余 88 张仍引用原 JPEG。检查新增饱和裁切，任一图的新增裁切通道比例超过 0.5% 就停止。
6. 默认在颜色补偿之后做全局地平线矫正，生成 `leveled.pto`，相对几何和颜色补偿保持不变；`--level-horizon off` 可关闭。将两种输入位深分组送入 Nona，统一输出 16 位图层，再用 Enblend 做 360° 环绕融合并明确指定完整画布尺寸。生成的混合位深工程由本脚本渲染；直接用 Hugin 单次 Nona 流程会报混合输入位深错误。
7. 保存 `model.npz`、训练/验证样本、`report.json`、命令日志和源文件 SHA256；结束时再次检查原图没有变化。原有未覆盖天顶保持透明，预览 JPEG 用深灰显示。

颜色修正只改变低频颜色，不生成景物，也不改变图像间的相对几何。随后默认启用的调平会整体旋转所有图像。跨图移动的云层、视差及原有配准偏差不由本步骤解决。16 位输出是为了减少后续补偿和融合的量化损失，源 JPEG 本身仍是 8 位。

### 当前结果与复现

正式结果目录为 **`runs/sky_balanced_v2`**。训练使用 2760 个对应样本，验证使用 10413 个未参加求解的样本。

- `preview.jpg`：完整 4096×2048 预览。
- `comparison_sky.jpg`：相同天空区域的前后对比，上方为修正前，下方为修正后。
- `panorama.tif`：4096×2048、RGBA、每通道 16 位，透明覆盖边界仍为 `(0, 391, 4096, 2048)`。
- `corrected/`：49 张原尺寸 16 位补偿图，合计约 2.45 GiB，是工程重新渲染所需的中间素材。

本次端到端约 273 秒，其中重投影约 34 秒、融合约 89 秒。所有源照片 SHA256 检查通过。新增裁切通道值占天空组全部通道值约 0.0098%，集中在朝向日出的第一张天空照片；该图约 0.478%，低于 0.5% 的停止阈值。另用默认入口重新采样验证过，留出色差中位数为 26.18 → 1.05，见 `runs/sky_fresh_sample_check/report.json`。

| 重叠 RGB 差值，0–255 量级 | 修正前 | 修正后 |
|---|---:|---:|
| 中位数 | 26.03 | 1.04 |
| P95 | 63.22 | 8.11 |
| 平均值 | 29.10 | 2.09 |
| 仅天空与固定参照交界的中位数 | 51.84 | 2.93 |

每个样本的指标是两个对应像素的 R/G/B 绝对差取平均。这些验证像素来自同一组照片，并参与了方案比较；不能解释为独立场景的泛化精度或真实色彩准确度。表格记录的是融合前的源图重叠一致性，最终观感还需检查成品。

精确重用本次采样（要求原图未改动）：

```powershell
.\Hugin_test\run_sky_balance.ps1 --samples-from Hugin_test\runs\color_probe\samples.txt --samples-project Hugin_test\runs\color_probe\01_color_variables.pto --level-horizon off
```

默认不加这两个参数时会重新采样，结果可能略有差别。缓存复用会检查图像顺序、路径和几何，但不能证明缓存生成以后原图内容未曾被修改。

完整测试：

```powershell
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest discover -s Hugin_test\tests -v
```

完整测试共 14 项。在原有 8 个回归测试外，新增重复光度补偿保护、空间色偏恢复及离群点、无参照重叠图、边界坐标、模型与实际像素插值一致性、真实 16 位输出、裁切保护和原图不变检查。传统逐张曝光/白平衡增益方案在太阳上方产生亮圈，未作为交付结果；相关试验保留在 `sky_gain_probe`、`sky_sampled` 和 `sky_vignette`。`sky_balanced_v1` 是边界样本校验提前停止的记录，正式脚本会剔除这些越界采样点。

光度工具参数依据：[Hugin vig_optimize 手册](https://hugin.sourceforge.io/docs/manual/Vig_optimize.html)，并核对了本机 `vig_optimize`、`nona`、`enblend` 的帮助及实际输出。

## 第三轮：默认开启自动地平线矫正

`run_preview.ps1`、`run_sky_balance.ps1` 和新的 `run_horizon.ps1` 均默认使用 `--level-horizon auto`。关闭时加 `--level-horizon off`，也可指定 `--level-horizon hugin` 使用 Hugin 自带 Straighten 方法。

已有天空颜色修正结果可以直接调平，无需重新匹配或重新拟合颜色：

```powershell
.\Hugin_test\run_horizon.ps1
```

默认读取 `runs/sky_balanced_v2/corrected.pto`。处理其他已有工程时：

```powershell
.\Hugin_test\run_horizon.ps1 路径\工程.pto
# 只保存调平工程和报告，暂不渲染：
.\Hugin_test\run_horizon.ps1 路径\工程.pto --stop-after level
# 关闭调平，做对照：
.\Hugin_test\run_sky_balance.ps1 --level-horizon off
```

`horizon.py` 的估计和几何校验只使用标准库，渲染使用既有颜色流程的依赖。输出到新的 `runs/leveled_时间戳`；输入 PTO、原图以及之前的补偿 TIFF 均不覆盖。完整天空流程在 `--stop-after fit` 时只进行颜色拟合，不执行后续调平/渲染。

### 方法与适用范围

这组照片中，接近水平拍摄的一圈共有 20 张，覆盖约 341° 方位；优化后的俯仰随方位呈周期性起伏，估计整体倾斜约 **1.092°**。相对匹配正确并不能单独确定全景的重力方向，因此在最后增加整体调平。

自动模式在有控制点支持的主连接组中选取绝对俯仰不超过 10°的图像，要求至少 8 张且覆盖不少于 240°。对相机光轴在单位球面上的位置做稳健平面拟合，保留平面偏置，不强迫全部相机朝向 0°俯仰。只把估计平面的法向调到竖直方向；最终由 Hugin 原生 `pano_modify --rotate` 对**所有照片应用同一个球面旋转**。它不逐张重调匹配，也不对成品做二维旋转或局部拉伸。

该估计假设参考环拍时俯仰大致恒定，不需要 DJI 专用标签。它不是山脊线识别，也不是独立的重力测量；自然山势高低会保留。参考圈不足或不一致时，自动退回 Hugin 原生 `--straighten`，原因记录在报告中。超过 5° 的自动旋转会停止，避免在参照不足时大幅改变全景；可关闭该步骤后检查工程。当前不接受含非零平移参数的工程。

原生工具可能同时调整画布或格式，新实现只接收 y/p/r 旋转结果，保留原有画布、镜头、曝光/白平衡、源文件和控制点。随后对所有相机的旋转矩阵验证共同旋转关系，防止任何一张图发生相对偏移。天空颜色流程的 `geometry_unchanged` 在执行调平时为 false，`relative_geometry_unchanged` 为 true，避免把整体朝向改变误报为几何完全不变。

### 当前结果

交付目录：**`runs/sky_balanced_leveled_v1`**。

- `preview.jpg` / `panorama.tif`：保留上一版天空颜色补偿的调平结果，4096×2048，TIFF 为 16 位 RGBA。
- `comparison_horizon.jpg`：调平前后相同区域；黄色虚线为球面 0° 仰角参考线，不是检测到的山脊。
- `leveled.pto`：可复用的新朝向工程，引用已有源图及补偿图。
- `report.json` / `verification.json`：旋转、参照圈、渲染和几何核验记录。

本次参数为全景 yaw 0°、pitch −0.33086°、roll +1.04094°，组合旋转约 1.09226°。所有 137 张照片满足同一个旋转关系；对 **9318 个控制点**用 Hugin 原生投影重新核验，调平前后残差中位数均为 **3.7238 原图等效像素**、P95 均为 **7.2515 像素**，逐点残差最大差约 **0.000023 像素**。这些是几何不变性检查，不是地平线绝对精度。137 张原 JPEG 的 SHA256 检查通过，镜头/颜色字段、源文件路径和控制点坐标保持一致。

本次核验同时修正了诊断函数中 `pano_trafo` 输出像素中心的半像素偏差：球面换算应使用 `(x+0.5, y+0.5)`。先前报告保留历史数值，没有重写旧报告；该修正只影响残差计算，不改动已有拼接工程或照片。

完整测试现为 **22 项**，覆盖默认开启与关闭、已知球面倾斜恢复、异常姿态、覆盖不足、共同旋转验证、颜色/镜头/画布保护及高纬度像素中心换算。原生 CLI 已在实际工程上完成调平和 16 位渲染验证。

参考：[pano_modify 官方选项](https://hugin.sourceforge.io/docs/manual/Pano_modify.html)、[Hugin Straighten 说明](https://hugin.sourceforge.io/docs/manual/Hugin_Preview_window.html)、[PTO 旋转约定](https://hugin.sourceforge.io/docs/html/Matrix3_8cpp_source.html)。

## 第四轮：默认全分辨率 JPG

最终导出入口：

```powershell
.\Hugin_test\run_export.ps1
# 指定已有 PTO，自动调平后全分辨率导出：
.\Hugin_test\run_export.ps1 路径\工程.pto --output Hugin_test\runs\我的导出
# 小图预览，也可以用于 run_horizon / run_sky_balance：
.\Hugin_test\run_export.ps1 --width 4096
# 原命令意外中断后续算，需保持同一工程、参数和输入文件：
.\Hugin_test\run_export.ps1 --output Hugin_test\runs\我的导出 --resume
# 本机 32 GB 内存可同时处理两块；也可在续算时调整 workers：
.\Hugin_test\run_export.ps1 --output Hugin_test\runs\我的导出 --resume --workers 2
```

默认输入为 `runs/sky_balanced_leveled_v1/leveled.pto`，引用全部 137 张原尺寸素材（88 张原 JPEG、49 张已完成颜色补偿的 16 位 TIFF），不把旧预览放大。`pano_modify --canvas=AUTO` 根据源图和镜头视场角计算画布；本组得到 **50446×25223，共 1,272,399,458 像素**。这是球面全景的输出采样尺寸，不等同于每个方向都有同样的有效细节；源照片未覆盖的天顶仍然缺失。

输出文件：

- `panorama.jpg`：全分辨率、8 位 RGB、JPEG 质量 97、4:4:4，无色度降采样，内嵌 sRGB 和 GPano 全景标记。JPEG 仍是有损压缩；“全像素”指不缩小输出尺寸。
- `preview.jpg`：最长宽度 4096 的便览图，从最终 JPG 缩小得到。
- `export_full.pto`、`leveled.pto`、`report.json`：完整画布工程、全局调平工程及可复现参数。
- `export_state.json`、各阶段日志：输入文件大小/修改时间、工程散列及续算检查；错误不会留下命名为成品的半截 JPG。

JPG 不支持透明，未覆盖区域用与已有预览相同的深灰 `(30,34,39)` 填充，不补画天空。超过本机 libjpeg 的 65500 像素单边限制时会明确停止，要求通过 `--width` 指定缩小尺寸；不会静默降分辨率。

### 内存及接缝处理

大图按默认最大 4096×4096 核心块从原图重投影，Nona 与 Enblend 都保持 16 位处理。每块额外渲染 256 像素保护边，再在相邻块之间保留 512 像素重叠并按 alpha 作交叉融合。各块共同覆盖完整 2:1 画布；最后用 libvips 按行编码 JPG，不把整张十几亿像素 RGB 图一次性载入内存。临时 16 位行条带在导出成功后删除；`--keep-intermediates` 可保留以便诊断。

左右 360° 接口也参与重叠融合：仅在工作 PTO 中给所有相机施加相同的 yaw 偏移，让每块移到画布中央，渲染完再放回整数像素位置。输入 PTO、镜头参数、颜色参数和控制点不改动。当前不接受带相机平移的工程。工作块使用独立的 Enblend 接缝选择，因此与一次性整幅 Enblend 不保证逐像素相同；运动云、视差和原有配准误差仍需在原尺寸下检查。

本轮测试共 **28 项**，新增非整除画布覆盖、左右循环接口、上下条带拼接、alpha 缺口保护、yaw 链接解析、默认全分辨率和参数边界检查。用 Hugin 原生投影另核验了 411 个点，整数画布平移最大误差约 0.000001 像素，纵向误差为 0。实际小图完成 2048×1024 分块重投影、融合、JPG 全量解码和 ExifTool 元数据检查；完整成品另以报告记录尺寸与解码结果。

本次完整成品在 **`runs/full_jpg_v1/panorama.jpg`**：50446×25223，354,607,009 字节（约 355 MB / 338 MiB），已通过整张流式解码。`preview.jpg` 从这张成品缩小为 4096×2048。ExifTool 确认 RGB 8 位、4:4:4、Baseline JPEG、sRGB 和完整 GPano 尺寸。全景、天空条带及原尺寸地面抽查完成，原图散列核验记录见 `verification.json`。

实际渲染中一个高度重叠块触发 Enblend 粗遮罩的全黑接缝错误。导出器现在仅对该明确错误自动回退：优先处理大幅覆盖图像、使用完整分辨率遮罩，并关闭遮罩优化和预组合，继续原生多分辨率融合。本组该块已成功重算，相关失败及恢复日志保留。中间 TIFF 改为不压缩以减少大图反复编码/解码时间，最终 JPG 参数保持上述设置。

参考：[Hugin 输出尺寸说明](https://hugin.sourceforge.io/docs/manual/Hugin_Stitcher_tab.html)、[Nona 源码中的 ROI 筛选](https://hugin.sourceforge.io/docs/html/nona_8cpp_source.html)、[libvips 流式处理](https://www.libvips.org/API/current/how-it-works.html)。

## 第五轮：索尼 A7R V「日光岩全景2」普通相机试拼

输入为「日光岩全景2」的 38 张 ARW，镜头为腾龙 35–150 mm，EXIF 焦距分为 **119 / 126 / 129 mm**。这些照片覆盖水平一圈，但只拍摄很窄的上下范围，因此使用 **360° 圆柱长条**，不扩展成 360×180° 球面，也不添加球面查看器 GPano 标签。

工作目录：`runs/sony_riguangyan2`。最终导出目录：`runs/sony_riguangyan2/final`，确切完成状态、大小和解码验证以该目录 `report.json` 为准。

### 处理与几何

- `prepare_raw.py`：从 ARW 读取元数据与内嵌预览，仅用于检查素材；正式源图通过 LibRaw/AHD 显影为 **9564×6376、16 位 sRGB TIFF**。白平衡采用全组相机系数中位数，关闭逐张自动增亮，使用一致的 sRGB 曲线。原始 ARW 保持不变，逐张 SHA256 保存在 `developed/*.json`，核对结果为 `raw_verification.json`。
- `alignment/` 是约 1/3 尺寸的配准工作图，`developed/` 才是正式渲染来源。元数据通过正确的 ExifIFD 写入；`--sync-metadata` 可无损复制光学标签。中文路径通过 UTF-8 参数流传给 ExifTool。
- 无姿态先验时，`stitch.py` 改用单应性 RANSAC 找候选控制点，然后用 `camera_alignment.py` 将候选点转为相机射线，做旋转 RANSAC 和最大生成树初始化，避免窄海面重叠导致相机翻转。之后仍使用 Hugin 联合优化与残差筛选。
- 海面、薄雾的 4 对困难重叠通过 `supplement_matches.py` 补充 CLAHE 工作图上的 SIFT 匹配。CLAHE 只影响特征检测，不应用于正式图像。
- 新增 `--optimize-distortion`，可与 `--optimize-hfov` 一起拟合各焦距组的视场角和径向畸变 b。此次三组 b 约为 0.0148–0.0156。
- `geometry_v6` 是最终几何：38 张全部连通，保留 1234 个控制点。工作图等效残差中位数 **1.3783 px**、P95 **4.6799 px**；换回原尺寸并使用 Hugin 原生投影核验后，中位数 **4.1513 px**、P95 **14.1165 px**。这是拟合残差，不是独立精度保证。原图薄雾、远景空气扰动及船只运动仍会限制效果。
- 默认自动调平已执行。该组并非固定俯仰的参考圈，因此自动回退至 Hugin straighten，全局旋转约 **1.208°**；保持所有照片相对姿态不变。这不等于测量了重力方向或识别真实海平线。

`prepare_camera_project.py` 在已对齐的工作图重叠区域拟合曝光与可选暗角，白平衡保持统一；本次全局提亮 1 EV。将相机几何、镜头参数、控制点和光度参数换算到全尺寸源图，生成 `color_v1/native.pto`。自动裁掉不齐边缘，保留整圈水平覆盖。低分辨率预览使用从原生裁切框换算的坐标，防止 Hugin 在不同尺寸下选出不同覆盖范围。

### 长条全景导出

```powershell
# 已有本次原生尺寸 PTO，可在新目录重新导出
.\Hugin_test\run_flat_export.ps1 Hugin_test\runs\sony_riguangyan2\color_v1\native.pto --output Hugin_test\runs\sony_repeat --workers 2 --threads 4
# 若同一次导出中断，保持工程和设置一致，追加 --resume
```

`export_flat.py` 接受已设定好投影、尺寸、裁切和调平结果的 PTO，**不会改写为 2:1 球面**。它保留实际裁切范围，区分普通左右边界和 360° 循环边界，支持同样的 16 位分块重投影与 Enblend 融合。

本次原生输出为 **216065×4602，994,331,130 像素**。宽度超过本机 JPEG 编码器的 65500 像素上限，因此同时提供：

- `panorama_full_16bit.tif`：连续、无缩小的 16 位 BigTIFF，Deflate 无损压缩，sRGB。
- `panorama_full_01_01.jpg` 至 `panorama_full_01_04.jpg`：从左到右四段，每段约 54016 像素宽，原生像素、质量 97、4:4:4；精确偏移在 `report.json`。
- `panorama_overview.jpg`：完整水平一圈的单张 JPG，宽 65000 像素，**明确是缩小便览版，不称为全像素**。
- `preview.jpg`：小尺寸预览；分段查看图和原尺寸局部抽查图另保存在输出目录。

实际导出中，Enblend 在一个狭窄边界重叠上触发 `kernel longer than line`。仅对此已识别错误，以及原有的全黑接缝错误，导出器自动使用完整分辨率遮罩、关闭遮罩优化/预组合重试。其他错误仍停止并保留日志。

新增测试覆盖已知相机旋转与错误匹配、有限宽度边界、非球面裁切；总计 **31 项测试通过**。另用原生 `pano_trafo` 检查了 177 个点，圆柱分块的整数水平平移最大误差约 0.000001 像素、纵向误差为 0。数据集相关核验脚本和报告位于 `verification/`。

此轮完成了这组索尼数据的可复现试拼；困难海面匹配补点和圆柱投影选择仍是显式步骤，并不声称已能自动处理所有任意拍摄轨迹。

参考：[rawpy 显影参数](https://letmaik.github.io/rawpy/api/rawpy.Params.html)、[Hugin 光度优化](https://hugin.sourceforge.io/docs/manual/Vig_optimize.html)。

本组最终导出已完成：`panorama_overview.jpg` 为 **65000×1384、33,173,222 字节**；连续 16 位 BigTIFF 为 **4,717,633,212 字节**；四段全像素 JPG 分别为 88,261,472 / 96,108,949 / 87,638,191 / 86,118,721 字节。全部成品通过完整解码；上下工作条带的 alpha 最小值均为 65535，最终裁切框无未覆盖空洞。四段 JPG 的坐标连续并恰好覆盖原生画布。全部 38 张 ARW 的 SHA256 与显影前一致，详见 `final/verification.json`。临时重投影 TIFF 和约 8.84 GB 的行条带已由导出器清理，保留可复用显影 TIFF、PTO、日志及成品。
