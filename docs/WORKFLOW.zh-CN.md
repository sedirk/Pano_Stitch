# 完整操作步骤

以下命令在 **Windows PowerShell、仓库根目录**执行。各阶段先检查报告和预览，再继续下一阶段；外部程序退出失败时不要继续使用未完成的文件。

普通相机使用 A 路线，带完整云台姿态的 DJI JPEG 使用 B 路线。已有正确 PTO 的用户可以直接阅读 C。这里的 Sony 配置针对水平一圈的长条全景；“不是球体”并不自动意味着拍摄足够 360°，其他输入要先判断覆盖范围。

## 0. 安装与目录约定

准备 Python 3.12、Hugin 及所需 CLI，具体列表见 [依赖说明](DEPENDENCIES.md)。

```powershell
py -3.12 -m venv .venv
$Python = (Resolve-Path '.venv\Scripts\python.exe').Path
& $Python -m pip install -r requirements.txt

# 替换成自己的 Hugin 可执行文件目录
$env:HUGIN_BIN = 'C:\Program Files\Hugin\bin'
& $Python -m unittest discover -s Hugin_test\tests -q
```

预期单元测试输出 `Ran 31 tests` 和 `OK`。单元测试不要求本仓库提供原始照片，也不能代替 Hugin 端到端运行。以后重新打开 PowerShell 时，需要重新设置 `$Python` 和 `$env:HUGIN_BIN`。

以下 `D:\Photos\...` 是输入目录示例，应替换为自己的照片目录。`runs` 下全部是本机生成文件，被 Git 忽略；大多数阶段要求输出目录尚不存在，避免覆盖旧结果。

## A. Sony ARW → 原像素圆柱长条

### A1. 检查照片和顺序

```powershell
$Photos = 'D:\Photos\日光岩全景2'
$Run = Join-Path (Get-Location).Path ('Hugin_test\runs\sony_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))

& $Python Hugin_test\prepare_raw.py $Photos --output $Run --inspect-only
```

检查 `$Run\contact_sheet.jpg` 和 `metadata.json`：

- 是否选中了正确的一组照片，文件名排序是否与拍摄顺序一致。
- 是否有明显运动、漏拍、严重视差或不相邻照片混入。
- 焦距是否变化，本案例是 119 / 126 / 129 mm；不要把它们误当成同一焦距。
- 照片是否首尾重叠、确实覆盖一圈。本案例是窄幅水平 360°，不是完整球面。

索引从 **0** 开始，以联系表和排序后的 `metadata.json` 为准。输入变更后重新建立 `$Run`；检查脚本会复用已有 `metadata.json`，不会自动感知用户想改用另一文件夹。

### A2. 统一显影，保留原尺寸源图

```powershell
& $Python Hugin_test\prepare_raw.py $Photos --output $Run
& $Python Hugin_test\prepare_raw.py $Photos --output $Run --sync-metadata
```

产生：

```text
sony_时间戳/
  metadata.json            拍摄信息和文件顺序
  raw_policy.json          统一白平衡、位深和显影策略
  contact_sheet.jpg        带编号的联系表
  embedded/                从 RAW 提取的查看图
  developed/*.tif          原尺寸 16 位显影图，最终输出使用这些图
  developed/*.json         尺寸、路径、原 ARW SHA256 等记录
  alignment/*.jpg          小尺寸配准工作图
```

`--sync-metadata` 把选定光学标签同步到派生 JPEG，不重编码 JPEG 像素。全套 38 张约产生 13.9 GB 显影 TIFF；嵌入小图不参与最终导出。

如需只检查显影策略，可在新工作目录中先使用 `--indices 0,19,28` 显影三张，确认后去掉该参数补齐全部。不要把仅三张的 `alignment` 目录误用于后面的 38 张案例补点命令。

### A3. 建立初始几何工程

```powershell
$Prepare = Join-Path $Run 'prepare'
& $Python Hugin_test\stitch.py (Join-Path $Run 'alignment') --work-dir $Prepare --pose-source none --stop-after prepare
```

检查 `$Prepare\report.json`、`01_generated.pto`、`02_initial.pto`。本组首图初始 HFOV 约 17.20°，各焦距应形成相应镜头组。如果长焦照片变成约 50°，优先检查焦距 EXIF，不要靠后续优化硬拉回来。

`stitch.py` 的输入是 JPEG，不应直接把 ARW 文件夹传给它。非标准 EXIF Orientation 会被拒绝，需先正确归一化像素和相关坐标。

### A4. 运行全部图像对的匹配

```powershell
$Matched = Join-Path $Run 'matched.pto'
& (Join-Path $env:HUGIN_BIN 'cpfind.exe') --allpairs --ncores=6 --ransacmode=hom --ransacdist=8 --minmatches=8 --sieve2size=3 -o $Matched (Join-Path $Prepare '02_initial.pto')
if ($LASTEXITCODE -ne 0) { throw 'cpfind 失败，请检查命令输出' }
```

这是本次 Sony 的显式匹配方式。一般普通相机入口本身也能自动执行 `--multirow` 匹配；全图像对更耗时，但有利于检查首尾和非相邻重叠。照片很多时应先评估计算量。

### A5. 对本案例的困难海面补点

**仅当仍使用同一组 38 张照片及相同排序时，直接使用下面的编号。** 换照片后应根据联系表和断开的匹配图选择真实重叠图像对；不能复制编号来强行连图。

```powershell
$Supplemented = Join-Path $Run 'supplemented.pto'
& $Python Hugin_test\supplement_matches.py $Matched --output $Supplemented --pairs '24-25,25-26,26-27,27-28'
```

输出新的 PTO 和补点 JSON。CLAHE 只用于检测特征，不会修改显影 TIFF。匹配是否可信还要通过下一步射线旋转模型验证。

如果新照片组原本匹配良好，可以跳过补点，下一步把 `--matches-from $Supplemented` 改成 `--matches-from $Matched`。

### A6. 全局优化，默认自动调平

```powershell
$Geometry = Join-Path $Run 'geometry'
& $Python Hugin_test\stitch.py (Join-Path $Run 'alignment') --work-dir $Geometry --matches-from $Supplemented --pose-source none --optimize-hfov --optimize-distortion --strict --threads 6 --stop-after optimize
```

本步骤重新生成相同输入的工程，复用对应点，执行控制点清理、射线旋转初始化、全局优化、离群点清理与默认调平。需要关注：

| 报告字段 | 正常检查方向 |
|---|---|
| `status` | 本路线应为 `optimized` |
| `has_complete_pose_priors` | 普通相机为 `false` |
| `final_graph.component_count` | 应为 1 |
| `final_graph.isolated_images` | 应为空 |
| `final_fit_residuals` | 看中位数、P95、最大值及具体错位，不只看点数 |
| `horizon.method` | 环拟合或 Hugin straighten 都可能是有效路径 |
| `horizon.invariants` | 应保持相对姿态，旋转未超过保护阈值 |

`--strict` 在普通相机路线中应保留。若出现 `Calibrated rotation matches are disconnected`，根据报告寻找缺少的有效重叠，返回 A5 或补拍，不要绕过检查。

正常输出为 `$Geometry\09_leveled.pto`。关闭调平可传 `--level-horizon off`，但此时不会创建这份已调平文件，应改用该次实际最后几何工程，通常为 `08_refined.pto`。已有工程只想调平时可运行 `horizon.py ... --stop-after level`，不要使用其默认球面渲染分支来导出长条。

**保留 `--stop-after optimize`。** `stitch.py` 默认渲染分支仍会建立完整 2:1 球面画布，普通相机长条接下来应走下面的专用准备和导出步骤。

### A7. 曝光、暗角和原生圆柱画布

```powershell
$Color = Join-Path $Run 'color'
& $Python Hugin_test\prepare_camera_project.py (Join-Path $Geometry '09_leveled.pto') --output $Color --developed (Join-Path $Run 'developed') --preview-width 16000 --brightness-stops 1 --vignetting
```

此处的 `--brightness-stops 1` 是本组照片选择的全局提亮；其他照片可改为 0 或其他合适值。`--vignetting` 启用每镜头组暗角拟合，不加该参数则只拟合曝光，统一白平衡保持不变。

输出 `photo.pto`、`preview.pto`、`native.pto` 和报告。预览引用小工作图，原生工程引用 `developed` 中的原尺寸 TIFF；预览与原生的裁切范围保持一致。

**当前脚本内固定圆柱投影、水平 360° 和 yaw 171°，属于此案例配置。** 如果照片只覆盖半圈，或者更适合其他投影，应在 Hugin/PTO 中设置实际投影、视场角、画布和裁切，或修改这段准备逻辑后再运行；`export_flat.py` 会尊重准备好的 PTO，但不会替用户判断覆盖范围。不要对不足一圈的输入直接套用本命令。

### A8. 先看覆盖一致的预览

```powershell
& $Python Hugin_test\export_flat.py (Join-Path $Color 'preview.pto') --output (Join-Path $Run 'preview') --workers 1 --threads 4
```

打开 `preview\panorama.jpg`，检查是否完整一圈、首尾是否接上、地平线是否自然，建筑和岸线是否明显断开，曝光与天空是否还有跳变。Sony 案例预览为 16000×341，长宽比例很大，需要局部放大查看。

如需再改画布或颜色，在新目录生成和比较；不要把旧 `preview.jpg` 放大后当全像素成品。确认预览后继续全尺寸导出。

### A9. 从全尺寸 TIFF 导出最终结果

```powershell
$Final = Join-Path $Run 'final'
& $Python Hugin_test\export_flat.py (Join-Path $Color 'native.pto') --output $Final --workers 2 --threads 4 --quality 97
```

`--workers 2 --threads 4` 是本次 32 GB 内存机器采用的组合；内存较少先用 `--workers 1`。`--tile-size` 默认 4096，改变块大小不会改变原生画布尺寸，但会改变工作量和接缝处理。

结果若未超过 JPEG 单边限制，输出 `panorama.jpg`。本案例超过限制，输出：

```text
final/
  panorama_full_16bit.tif    连续原像素母版
  panorama_full_01_01.jpg    从左到右第 1 段
  panorama_full_01_02.jpg    第 2 段
  panorama_full_01_03.jpg    第 3 段
  panorama_full_01_04.jpg    第 4 段
  panorama_overview.jpg      单张缩小便览图
  preview.jpg               快速预览
  report.json               尺寸、段偏移、完整解码验证、最终状态
  export_state.json         续算一致性记录
  render.pto                实际渲染工程
```

中断后，输入和设置保持一致时可续算：

```powershell
& $Python Hugin_test\export_flat.py (Join-Path $Color 'native.pto') --output $Final --resume --workers 1 --threads 4 --quality 97
```

续算允许调整并发，但改了源图、PTO、块大小或质量应使用新输出目录。成功后工作条带会清理，`--keep` 可保留；成功完成的目录不再等同于拥有全部可复用中间缓存。

最后确认 `report.json` 为 `exported`、各输出有 `full_decode_verified: true`，并按 [验证手册](VALIDATION.zh-CN.md) 检查分段覆盖、原图哈希及原尺寸细节。

## B. DJI JPEG → 天空校色 → 球面全像素 JPG

### B1. 先配准并检查姿态先验

```powershell
$DjiPhotos = 'D:\Photos\DJI_Panorama'
$DjiRun = Join-Path (Get-Location).Path ('Hugin_test\runs\dji_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
$DjiGeometry = Join-Path $DjiRun 'geometry'

& $Python Hugin_test\stitch.py $DjiPhotos --work-dir $DjiGeometry --pose-source dji --optimize-hfov --width 4096 --threads 6
```

`--pose-source dji` 要求全部照片具有有效 yaw/pitch/roll，缺失就报错，避免误把普通相机当成已知姿态。正常 JPEG 原色路线不要先加 `--photometric`，否则后续天空网格会拒绝重复光度修正。

本例 137 张中有 49 张纯天空没有控制点，它们依靠 DJI 先验定位，报告会明确警告，状态带 `_with_priors`。这不意味着所有照片都已经由图像证据验证。如果任务要求每张都经过特征连接，可以加 `--strict`，但这组数据会按设计停止。

本条路线不要加 `--optimize-distortion`；当前天空选择器只接受无畸变、居中的直线投影模型。先检查 4096 宽预览及匹配报告，再进入颜色阶段。

### B2. 训练和验证天空补偿网格

```powershell
$Sky = Join-Path $DjiRun 'sky'
& $Python Hugin_test\balance_sky.py (Join-Path $DjiGeometry '11_render.pto') --output $Sky --width 4096 --threads 6
```

该入口要求完整等距柱状工程以及中性曝光、白平衡、响应和暗角参数。代码固定地面和跨地平线图像，对整张在天上的照片拟合平滑 RGB 偏移，并用未参与拟合的样本验证。

`--width 4096` 只限制此阶段全景预览；`corrected` 中仍写出原尺寸 16 位天空 TIFF，为后续全像素导出保留源分辨率。自动调平默认开启，会生成 `leveled.pto`。

如只想检查模型、不写补偿源图和渲染结果，使用 `--stop-after fit`。这种运行不能直接进入下一步导出，需另用新目录完成渲染流程。

### B3. 导出原生球面 JPG

```powershell
& $Python Hugin_test\export_jpg.py (Join-Path $Sky 'leveled.pto') --output (Join-Path $DjiRun 'final') --level-horizon off --workers 2 --threads 4 --jpeg-quality 97
```

这里关闭再次调平，是因为源项目已经完成该步骤。未指定 `--width` 时使用原尺寸源图计算原生画布；不要把预览 JPEG 当输入。DJI 实测得到 50446×25223、约 12.72 亿像素的完整 2:1 画布。源片没拍到的天顶保留为背景色，不补造图像。

该入口添加 GPano 等距柱状元数据，不适合导出 Sony 圆柱长条。如果完整球面也超过 JPEG 单边限制，它会报错，需显式缩小 `--width`，或另行准备可由长条导出器处理的 PTO 与分段交付。

## C. 已有 PTO 与常用参数

| 目标 | 入口 | 注意 |
|---|---|---|
| 只对既有项目调平 | `horizon.py project.pto --output 新目录 --stop-after level` | 默认 auto，只输出共同旋转后的项目 |
| 完整球面原生 JPG | `export_jpg.py project.pto --output 新目录` | 强制 360×180° 与 2:1 |
| 原投影及裁切导出 | `export_flat.py project.pto --output 新目录` | PTO 必须预先设置好颜色、几何和画布，不额外调平 |
| 对齐后的 DJI 天空校色 | `balance_sky.py project.pto --output 新目录 --width 4096` | 只接受其支持的中性球面工程 |

容易混淆的参数：

| 参数 | 球面入口 | 长条入口 |
|---|---|---|
| JPEG 质量 | `--jpeg-quality 97` | `--quality 97` |
| 保留工作条带 | `--keep-intermediates` | `--keep` |
| 输出尺寸 | `--width` 或原生 AUTO | 读取 PTO 的画布和裁切；没有 `--width` 参数 |
| 自动调平 | 默认 auto，可设 off/hugin | 不调平，使用已准备的 PTO |

PowerShell 包装器 `run_preview.ps1`、`run_sky_balance.ps1`、`run_horizon.ps1`、`run_export.ps1`、`run_flat_export.ps1` 可传 `-PythonExe $Python`。部分包装器保留开发时的默认输入路径；公开仓库不含那些输入，使用时应显式传入照片或 PTO。推荐先按本文直接调用 Python，方便看到每一步输入输出。

## D. 每次运行应保留的记录

保留源照片、`metadata.json`、`raw_policy.json`、显影记录、最终几何 PTO、光度与画布 PTO、`report.json` 和工具日志。若要迁移工程，应同时迁移所引用的 TIFF/JPEG 并修正路径；PTO 本身不包含照片。

不要用“文件大小很大”判断全像素成功。确认源图路径指向 `developed` 或原尺寸修正图，确认输出像素和裁切，完成整文件解码，再检查原尺寸局部。指标和常见故障详见 [验证与常见问题](VALIDATION.zh-CN.md)。
