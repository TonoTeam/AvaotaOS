# Avaota A1 GPU 验证记录

2026-10-17；Ubuntu 24.04 ARM64、A1 4GB、BSP 5.15.154-ga464bc4feaff、Mesa 25.2.8、Qt 6.4.2。本记录区分底层硬件验证、应用回归和完整镜像验收；目前仍有阻塞项，PR 保持草稿。

## 已确认的硬件路径

原 kiosk 的 Xorg `AccelMethod=none` 和 App `--software-rendering` 使用 CPU。Panfrost 驱动和固件已在系统中，不需要替换内核。普通用户获得 render 节点访问后，surfaceless EGL 能创建 Mali-G57 GLES 3.1 上下文，编译 GLES 3 着色器并绘制三角形；4×4 像素读回均符合预期 RGBA `(64,128,191,255)`。

启用 glamor 后，还必须让 rootless Xorg 能打开主 DRM 节点，才能向客户端传递 DRI3 fd。只给 render 权限时，Xorg 本身可用 GPU，客户端仍回落到 llvmpipe。将 video/render 仅赋予 kiosk 服务后，普通用户 GLX 报告 `Accelerated: yes` 和 `Mali-G57 (Panfrost)`。试验未永久修改账户的 video/render 组成员身份。

独立 Qt Quick/WebEngine 探针以普通用户运行，初始化前显式设置 OpenGL ES 3 和 xcb_egl；Qt 实际使用 GLES 3.1。Chromium 创建 WebGL2，网页像素读回和最终 Quick 合成画面的 RGBA 均为 `(64,128,191,255)`，调试扩展报告 Mali-G57/Panfrost。没有传入 `--no-sandbox` 或关闭全局沙盒策略。仅指定 xcb_egl 仍会获得桌面 OpenGL，因此 App 增加显式 `--opengl-es`，而非只改变环境变量。

硬件加速用于 Quick、纹理和 WebGL 合成；C 引擎的笔刷计算及 X Shape/RasterSurface 低延迟墨迹仍由 CPU 执行。这些检查不构成端到端手写延迟或内存改善的测量。

## 应用回归

App 基于 main `91a6921e12730c75846500556802e455c8745ca0`，实现提交 `2057556`，Ubuntu ARM64 构建版本 1.1.5。

| 环境 / 检查 | 结果 |
| --- | --- |
| ARM64 构建与 CPack deb | 通过 |
| 渲染选择 QtTest | 4 通过 |
| Wi-Fi QtTest | 13 通过 |
| 保存退出 QtTest | 7 通过 |
| SDK 软件模式，普通窗口 | 38 通过，34 按条件跳过 |
| SDK 软件模式，低延迟全屏 | 72 通过，无跳过 |
| 完整工厂镜像软件模式，普通窗口 | 37 通过，1 个相同冻结 HTML 截图失败，34 按条件跳过 |
| 完整工厂镜像软件模式，低延迟全屏 | 72 通过，无跳过 |
| A1 GLES 实机，普通窗口 | 37 通过，1 失败，34 按条件跳过 |
| A1 GLES 实机，低延迟全屏 | 未完成，不能记为通过 |

实机普通窗口唯一失败为 `frozenHtmlCapture` 在 `(65,195)` 的网页颜色断言；实时 HTML、WebGL 演示与合成上传、原生笔迹、撤销及存取等断言通过。相同截图失败此前在非精简与精简的软件模式镜像中都已有记录，见 [体积验证记录](kiosk-size-validation.md)。SDK 的通过结果不能覆盖实机失败，也未通过放宽断言或跳过测试掩盖问题。短时间延长首帧等待的试验未纳入发布代码，因为最小复现的原始版本也可通过，无法证明修复。

实机全屏测试使用独立临时画板，运行到原生墨迹、多笔一致性和画板往返阶段后，SSH 失联。没有获得最终总计；网络故障与 GPU 的因果关系尚未确定。USB-TTL 没有获得可解析的错误，需恢复设备连接后读取内核及服务日志。日常应用在测试前正常保存；未安装已上传的 1.1.5 包，未修改用户画板或服务器设置。测试设置了自动恢复旧应用的定时任务，但失联后尚不能确认其执行结果。

## 镜像与发布状态

完整镜像使用本 PR 的系统实现、1.1.5 ARM64 包、原内核/设备树修正/固件/启动器。镜像内默认 auto，包含 GLES 预检、硬件/软件 Xorg 两套配置和故障退出的软件重试；只读检查验证服务配置、文件系统、MBR/BootROM、依赖、沙盒能力及维护空间。源码中的会话选择回归覆盖探测失败、硬件退出失败、正常退出、手动模式和错误配置。

工厂镜像不含实机的画板、Wi-Fi 凭据、SSH 密钥或注册身份。镜像实现提交 `e21b054`；原始镜像 1,929,379,840 字节，XZ 394,449,436 字节。只读验证通过，root 可用 302 MiB；XZ 完整解压的 SHA-256 与原始镜像一致。完整镜像的 SHA-256 和构建信息保存在本地 `build_dir/gpu/image/build-info.json`，不把大文件或诊断日志提交到 Git。

发布前还需解决或明确验收冻结 HTML 截图、完成全屏实机回归，验证真实硬件的软件恢复入口，并对新镜像做冷启动、异常重启和保存恢复验收。音频、摄像头、真实触笔及性能对比仍未验证。因此本次产物是待验收的 GPU 镜像，尚不能标记为稳定量产版本，也未烧录 TF 卡。

参考：[Mesa Panfrost](https://docs.mesa3d.org/drivers/panfrost.html)、[Qt QSurfaceFormat](https://doc.qt.io/qt-6/qsurfaceformat.html)、[Qt WebEngine 沙盒要求](https://doc.qt.io/qt-6.5/qtwebengine-platform-notes.html)。
