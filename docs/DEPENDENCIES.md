# 依赖与运行环境

## 已测试组合

| 组件 | 本次使用版本 | 用途 |
|---|---|---|
| Windows x64 | 本机环境 | 唯一完成原生端到端实测的平台 |
| Python | 3.12 | 编排、诊断、颜色模型与导出 |
| Hugin | 2025.0.1.9609d5726ad8 | PTO、特征、几何、光度、重投影 |
| Enblend | 4.3-993fd4822e8d | 接缝选择与多分辨率融合 |
| NumPy | 2.3.5 | 数值计算 |
| SciPy | 1.18.1 | 旋转与稀疏最小二乘 |
| tifffile | 2026.9.20 | TIFF 写入及元数据检查 |
| rawpy | 0.27.1 | LibRaw 显影 Sony ARW |
| OpenCV headless | 5.0.0.93 | 困难重叠的 SIFT、CLAHE 和单应性筛选 |
| pyvips | 3.2.0 | 大图流式拼合和编码 |
| libvips / pyvips-binary | 8.18.7 | 图像处理运行库 |
| Pillow | 见当前 Python 环境 | 联系表、缩小工作图与 ICC |

这是实测记录，不是跨平台兼容性声明。项目使用版本范围声明依赖，未提供逐包锁文件；重新安装可能选择满足范围的新版本。如需复现实验环境，另存 `python -m pip freeze` 和外部工具 `--version` 的输出。

## 安装

```powershell
py -3.12 -m venv .venv
& .venv\Scripts\python.exe -m pip install -r requirements.txt
$env:HUGIN_BIN = 'C:\Program Files\Hugin\bin'
```

`HUGIN_BIN` 应是可执行程序目录，而不是 Hugin 安装根目录。所需程序包括：

```text
pto_gen.exe       cpfind.exe          cpclean.exe
autooptimiser.exe vig_optimize.exe    pano_modify.exe
pano_trafo.exe    nona.exe            enblend.exe
exiftool.exe      hugin_executor.exe
```

`find_bin()` 也会检查项目内 `.tools/hugin/Hugin/bin`、常见 Windows 安装目录及 PATH。RAW 与圆柱项目脚本通过同一查找逻辑寻找工具；不能只在某个早期命令里指定 `--hugin-bin` 后就假定后续脚本也会沿用，统一设置环境变量更直接。

代码允许在项目内 `.tools/python-color` 放置依赖。该目录不会上传；普通使用者应优先使用 `.venv`。不要把为另一 Python 小版本或操作系统安装的本地 wheel 目录混进当前解释器。

## 来源与职责

- [Hugin 官方下载](https://hugin.sourceforge.io/download/)：本项目通过子进程调用其 CLI，不重新分发其安装包。
- [Hugin autooptimiser](https://hugin.sourceforge.io/docs/manual/Autooptimiser.html)：几何优化命令含义。
- [Hugin pano_modify](https://hugin.sourceforge.io/docs/manual/Pano_modify.html)：投影、视场角、尺寸和裁切。
- [Hugin vig_optimize](https://hugin.sourceforge.io/docs/manual/Vig_optimize.html)：曝光、暗角和响应曲线参数。
- [rawpy 参数文档](https://letmaik.github.io/rawpy/api/rawpy.Params.html)：AHD、白平衡、位深与增亮控制。
- [OpenCV SIFT](https://docs.opencv.org/4.x/d7/d60/classcv_1_1SIFT.html)：补充特征检测。
- [pyvips](https://github.com/libvips/pyvips)、[libvips 工作方式](https://www.libvips.org/API/current/how-it-works.html)：按需图像处理和大图输出。
- [tifffile](https://github.com/cgohlke/tifffile)、[NumPy](https://numpy.org/)、[SciPy](https://scipy.org/)、[Pillow](https://python-pillow.github.io/)：数值与文件处理。

许可与第三方声明以各上游发行包为准；本仓库不附带原始照片、第三方工具或模型权重。

## 资源估算

Sony 案例在 32 GB 内存机器上使用 2 个渲染 worker，每个工具 4 个线程。38 张显影 TIFF 共约 13.9 GB；全景工作条带峰值约 8.84 GB；连续 16 位母版约 4.72 GB，另有分段 JPG、临时图层和预览。因此应给这一案例预留数十 GB 可用空间，实际峰值随覆盖和块大小变化。

默认工作块核心为 4096 像素，块大小改变内存和接缝计算行为，但不改变输出画布尺寸。内存不足时先将 `--workers` 改为 `1`；必须缩小画布时，应把结果明确标为缩小版。
