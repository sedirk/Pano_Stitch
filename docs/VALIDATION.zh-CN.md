# 验证与常见问题

拼接完成需要同时有几何证据、合理颜色、完整覆盖、可解码文件和原图完整性。某个残差变小或输出体积变大，都不能单独证明结果正确。

## 1. 代码层的回归检查

在仓库根目录执行：

```powershell
& .venv\Scripts\python.exe -m unittest discover -s Hugin_test\tests -v
```

首次发布前 31 项测试通过，覆盖范围如下：

| 测试文件 | 数量 | 主要检查 |
|---|---:|---|
| `test_stitch.py` | 9 | PTO 数值与链接、姿态先验、断图保护、控制点与像素中心、子进程失败 |
| `test_camera_alignment.py` | 1 | 已知旋转、离群点和首尾闭环下的初始化 |
| `test_balance_sky.py` | 6 | 网格拟合、固定颜色锚点、插值一致性、位深、重复校正拒绝 |
| `test_horizon.py` | 7 | 默认开启、关闭不变、鲁棒拍摄环、共同旋转、过大改动拒绝 |
| `test_export_jpg.py` | 6 | 原生尺寸默认值、整数周期位移、拼块尺寸与透明边缘 |
| `test_export_flat.py` | 2 | 保留任意裁切比例、有限边界和不等宽拼合 |

这些测试使用小型合成数据，不能证明任何新照片组一定可拼，也没有覆盖所有 RAW 相机或原生 Hugin 版本。真实 Sony 和 DJI 的端到端结果属于另行完成的本机集成验证，照片和大型产物未放入仓库。

## 2. 几何报告

先看 `status`、`warnings`、`final_graph`，再看 `final_fit_residuals`。

- 无姿态先验的普通相机应全部连通，无孤立图像。严格模式失败意味着需要修复匹配或输入。
- 有 DJI 先验时，纯天空可能没有控制点；`*_with_priors` 表示该部分位置依赖元数据，不能称为图像验证通过。
- 观察每对控制点是否分布在合理重叠内，是否集中在小片重复纹理上。连通只是必要条件，不足以证明每条边可靠。
- 检查焦距、HFOV、镜头组和径向 `b` 是否合理；不要只追求拟合数字更小而放任参数失真。
- 中位数反映主体，P95 与最大值帮助定位局部错误。工作图和原图单位不同，要看 `residual_units`。

调平后查看 `horizon.method`、`fallback_reason` 和 `invariants`。共同旋转检查保证照片间相对姿态不变，却不能证明绝对海平线已测到某个精度。当前自动方式有拍摄环假设，也有 5° 保护阈值。

## 3. 颜色与视觉检查

RAW 路线应检查统一白平衡策略和光度优化结果。`Vb/Vc/Vd` 是亮度模型，几何 `b` 是畸变模型，两者不能互换。重叠不足或几何有错时，光度模型也可能用不合理数值补偿位置误差。

DJI 网格的验证指标是重叠 RGB 三通道平均绝对差，单位为 0–255 色阶。应读取留出样本的前后均值、中位数和 P95，同时检查裁切/饱和记录。它们不代表 ΔE 或严格辐射一致性。

至少检查四种尺度：

1. 整体便览：覆盖是否正确、地平线是否自然、色调是否连贯。
2. 接缝附近：屋顶、岸线、天际线和高反差边缘有无双影或跳变。
3. 首尾边界：仅对水平 360° 项目，应确认周期衔接。
4. 原尺寸局部：远处建筑、细线及运动物体，避免缩小预览掩盖错位。

JPEG 增加位深不能找回已经丢失的色彩信息，RAW 中过曝、失焦、大气扰动和近景视差也不会自动消失。本代码未加入局部形变或生成式补洞。

## 4. 文件完整性与全像素证据

导出器检查输出尺寸并完整解码 JPEG/TIFF；`report.json` 中的 `full_decode_verified: true` 表示这一步通过。只有文件头可读、文件存在或预览能打开，都不足以替代完整解码。

长条超限时还要核对：每段 JPG 的偏移和宽高组成无缝矩形覆盖，母版与报告的原生尺寸相同，单张便览图明确记录了缩放比例。本次四段横坐标区间是：

```text
[0, 54016)
[54016, 108032)
[108032, 162049)
[162049, 216065)
```

四段高度均为 4602。连续 16 位 BigTIFF 是高位深母版；分段 JPG 保留采样尺寸但使用 8 位有损压缩；65000×1384 的 overview 是缩小版。

导出器的 `sources_unchanged` 使用源文件大小和修改时间检查。它不等于逐字节哈希验证。RAW 阶段已为原 ARW 保存 SHA256，可以额外核对：

```powershell
# 沿用操作手册中的 $Python 和 $Run；会完整读取原始 ARW
@'
import hashlib
import json
import sys
from pathlib import Path

run = Path(sys.argv[1])
records = sorted((run / 'developed').glob('*.json'))
expected = json.loads((run / 'metadata.json').read_text(encoding='utf-8'))
assert len(records) == len(expected), '显影记录数量不完整'
for record_path in records:
    record = json.loads(record_path.read_text(encoding='utf-8'))
    with Path(record['source']).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert digest == record['sha256'], f"原片内容变化: {record_path.stem}"
print(f'{len(records)} original RAW hashes verified')
'@ | & $Python - $Run
```

本次 Sony 额外检查了工作条带 alpha、圆柱整数位移和提升后的控制点残差。这些是案例诊断，导出器不会为每次运行自动生成同样的 `verification.json`；公开的数值摘录见 [Sony 案例 JSON](examples/sony_riguangyan2_summary.json)。

## 5. 故障排查

| 现象或错误 | 首先检查 | 处理 |
|---|---|---|
| 找不到 Hugin 命令 | `HUGIN_BIN` 是否指向含 exe 的目录、程序是否齐全 | 按依赖说明安装，设置当前会话变量 |
| `At least two JPEG images are required` | 是否把 ARW 文件夹传给 stitch | 先运行 prepare_raw，改用 alignment |
| 长焦初值 HFOV 约 50° | 工作 JPEG 光学 EXIF 是否被正确读取 | 同步元数据，重建初始工程；不要继续沿用错误初值 |
| `Non-normal EXIF orientation` | 图像像素和 EXIF Orientation 是否一致 | 先规范方向；相关控制点/姿态坐标也必须匹配 |
| `Calibrated rotation matches are disconnected` | 报告的分量、拒绝边及联系表 | 找真实重叠补点或补拍，避免强行添加错点 |
| 缓存控制点被拒绝 | 图像数量、顺序、路径、尺寸是否改变 | 为新输入重新匹配；`--matches-from` 不是任意 PTO 迁移器 |
| 调平超过 5° 或非共同旋转 | 初始几何、拍摄覆盖、相机平移 | 修复几何或人工判断朝向；必要时 off，不提高阈值掩盖问题 |
| 天空校色提示 non-neutral 或不支持畸变 | 是否输入已校色/已拟合 b 的项目 | DJI 使用中性原色球面工程；Sony 使用 RAW 光度路线 |
| 光度暗角参数不合理 | 控制点和颜色重叠是否可靠、是否需要三组镜头 | 先修几何，比较不加 `--vignetting` 的新结果 |
| 新预览只剩部分画面 | 是否分别让低分辨率和原生工程自动裁切 | 使用当前 prepare_camera_project，由原生裁切推导预览 |
| 长条变成 2:1 | 是否调用了球面渲染入口 | 普通相机停在 optimize，准备实际 PTO 后用 export_flat |
| 单张 JPG 尺寸超限 | 原生任一边是否超过 65500 | 长条使用分段与 BigTIFF；球面显式缩小或另备分段工程 |
| 内存不足或进程被终止 | worker 数量、条带/临时 TIFF、可用磁盘 | 先降为 1 worker；改块大小要用新目录 |
| `Resume inputs differ` | PTO、源文件、质量、块大小是否变化 | 使用新输出目录，保留旧目录用于诊断 |
| 输出目录已经存在 | 是否重复使用阶段名称 | 创建新目录；仅支持的导出入口用 `--resume` |
| 日志长时间不增长 | 子进程是否仍运行 | Runner 在命令结束后写出捕获日志，不代表中途没计算 |
| Enblend 窄边错误 | 是否属于已识别的 seam/kernel 错误 | 当前导出器会有限重试；其他错误应看日志，不能当成功 |

内存压力时只减少线程不一定足够，多个 worker 会同时保留多份工作图层。不要为了避免报错直接覆盖源图或删掉整个工作目录；各阶段本来就支持在新目录重新运行。

## 6. 复现边界

依赖声明是版本范围，不是锁文件，Hugin/OpenCV/LibRaw 版本变化可能改变特征、裁切、接缝及编码结果。即使随机种子固定，也不承诺跨版本、跨平台产物 SHA256 相同。

公开仓库提供程序、操作逻辑和去除本机路径的案例数值，不包含原片，因此其他人不能只靠一次 clone 就重新生成这一组具体风景。他们可以用自己的输入运行同一方法，或在获得原图后按对应流程复现。
