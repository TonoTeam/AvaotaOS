# Avaota A1 GPU 验证记录

2026-10-17 初验，2026-10-18 完成实机复验。Ubuntu 24.04 ARM64、A1 4GB、BSP 5.15.154-ga464bc4feaff、Mesa 25.2.8、Qt 6.4.2。GPU 模式已安装并保留，整板正常重启后仍自动进入 GLES 白板；本记录保留初验中的间歇性截图失败及未覆盖项目。

## 硬件路径

原 kiosk 的 Xorg `AccelMethod=none` 和 App `--software-rendering` 使用 CPU。Panfrost 驱动和固件已经存在，无需替换内核。普通用户访问 render 节点后，surfaceless EGL 能创建 Mali-G57 GLES 3.1 上下文，编译 GLES 3 着色器并绘制三角形；4×4 像素读回均符合预期 RGBA `(64,128,191,255)`。

启用 glamor 后，rootless Xorg 还必须能打开主 DRM 节点，才能向客户端传递 DRI3 fd。只有 render 权限时，Xorg 本身可用 GPU，客户端仍回落到 llvmpipe。video/render 仅赋予 kiosk 服务后，普通用户 GLX 报告 `Accelerated: yes` 和 `Mali-G57 (Panfrost)`；账户的全局组成员未增加这些权限。

Qt Quick/WebEngine 探针以普通用户运行，初始化前显式设置 GLES 3 和 xcb_egl，实际创建 GLES 3.1 上下文。Chromium WebGL2 的网页像素读回和最终 Quick 合成画面均为 `(64,128,191,255)`，调试扩展报告 Mali-G57/Panfrost。整板重启后的同一路径仍报告 Mali-G57 GLES 3.1。实际 App 的 Chromium renderer 启用 PID/user 命名空间、`NoNewPrivs: 1`、`Seccomp: 2`。

硬件加速用于 Quick、纹理和 WebGL 合成；C 引擎的笔刷计算及 X Shape/RasterSurface 低延迟墨迹仍由 CPU 执行。这些检查不构成端到端手写延迟、内存或启动速度改善的测量。

## 应用和系统回归

App 基于 main `91a6921e12730c75846500556802e455c8745ca0`，实现提交 `2057556`，Ubuntu ARM64 构建版本 1.1.5。deb 清单只有五个实际应用文件；设备上的 `dpkg --verify tono-canvas` 无差异。单独重新打包后的应用二进制与已测试版本一致。

| 环境 / 检查 | 结果 |
| --- | --- |
| ARM64 构建与 CPack deb | 通过 |
| 渲染选择 QtTest | 4 通过 |
| Wi-Fi QtTest | 13 通过 |
| 保存退出 QtTest | 7 通过 |
| SDK 软件模式，普通窗口 | 38 通过，34 按条件跳过 |
| SDK 软件模式，低延迟全屏 | 72 通过，无跳过 |
| 最终完整工厂镜像软件模式，普通窗口 | 38 通过，34 按条件跳过 |
| 最终完整工厂镜像软件模式，低延迟全屏 | 72 通过，无跳过 |
| A1 GLES 实机，普通窗口复验 | 38 通过，34 按条件跳过 |
| A1 GLES 实机，低延迟全屏 | 72 通过，无跳过 |
| Linux 系统回归 | 31 通过 |

实机覆盖原生笔迹、多笔材质一致性、撤销/重做、存取与画板往返、全屏/F11/Escape/弹窗焦点、中文文字和图片解码、HTML 合成与冻结截图、WebGL 演示和合成上传。测试使用独立临时画板及环回服务，不调用真实 AI 服务。

初验普通窗口的 `frozenHtmlCapture` 曾在 `(65,195)` 出现间歇性颜色断言失败；相同失败此前在非精简和精简的软件镜像中也有记录，见 [体积验证记录](kiosk-size-validation.md)。2026-10-18 工厂镜像和 A1 GLES 完整复验均通过原断言，截图代码和断言未为此改变；本次不声称修复了这个既有间歇问题。

初次全屏测试期间 SSH 中断，但恢复日志后确认该轮实际 72 项全部通过，自动恢复旧应用也执行成功。后续观察到用户专用 Wi-Fi 随临时图形会话退出而出现 `connection-removed`，会话恢复后自动重连；有线维护完成了后续验证。没有发现 GPU 挂死或重置记录。

## 安装、重启及恢复

设备已升级至 1.1.5，以 `--fullscreen --low-latency --opengl-es` 运行。安装前正常保存，配置、旧包和本地数据备份留在设备的受保护目录；安装、服务重启、整板正常重启后的原画板和设置 SHA-256 一致。用户已确认重启后显示、已有画板打开和书写均正常。AI 地址、设备身份、引擎、存档格式和协议未改变。

已在实机验证三条恢复路径：

- `TONO_RENDERER=software` 使用软件 Xorg 配置和软件 App，GLX 报告 llvmpipe、`Accelerated: no`。
- 强制 CPU 渲染使 GPU 预检失败后，auto 模式自动选择软件。
- 在真实会话入口注入应用退出码 42，硬件会话退出后自动重建软件 Xorg，并成功启动软件 App；随后恢复 auto，GPU 模式正常且数据不变。

第三项发现 xinit 会把客户端失败转换为成功退出。会话现在通过私有运行目录记录真实客户端退出码，启动器读取后决定恢复；每次尝试前删除旧结果，正常保存退出不触发降级。Linux 回归包括真实 session 脚本的退出码记录、xinit 返回成功时的客户端失败、重试不复用旧结果和非法状态处理。

## 完整镜像与范围

最终镜像包含上述退出码修正和干净 deb，继续使用原内核、设备树修正、固件及启动器。默认 auto，包含 GLES 预检、硬件/软件 Xorg 两套配置及错误退出的软件重试。精简规则保留带前缀的 `cjson-LICENSE`、`opus-COPYING`；安装 App 前更新旧缓存的 dpkg 规则，规则变化会使基础缓存失效。镜像验证包括只读 FAT/ext4、MBR/BootROM、内核沙盒能力、服务/依赖、App 参数和实际文件校验、维护空间及完整 XZ 解压哈希。

工厂镜像不含实机画板、Wi-Fi 凭据、SSH 密钥或注册身份。最终镜像、SHA-256 和构建提交保存在本地 `build_dir/gpu/final-image/`，大文件和诊断日志不提交 Git。

镜像的系统实现提交为 `9f629e71fd57ba7a32ea1e63a9fa447ecab84b0e`，App 实现提交为 `2057556c2a7c8cd9f0ee6f57c08746b084729f72`。基础 rootfs 按更新后的精简规则重新构建；内核和 SyterKit 使用已验证的缓存。交付文件前缀为 `AvaotaOS-0.3.0.4-noble-kiosk-arm64-avaota-a1-gpu-9f629e7`：

| 文件 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `.img` | 1,929,379,840 | `2d91f959e503e065670844367dee1ce460ceb42ad942979704fa90d98e0d1f8b` |
| `.img.xz` | 391,639,900 | `5956a23d96fcc3a5e1497888881a8ebe003b50f2acb419b464375eecbc874b3b` |

完整 XZ 解压后的 SHA-256 与原始镜像一致。只读镜像校验通过，App 的五项文件校验无差异，根分区剩余 303 MiB；首次启动仍保留自动扩容。对应 deb 的 SHA-256 为 `eed2588b020ac34f5d144567be831e9a3cc2f557e5892976fa5134a15e76d83a`。校验器先读取完整 apt 配置再匹配，避免 `pipefail` 将 `grep -q` 提前结束引起的 SIGPIPE 误判为镜像错误；此校验器修正不影响镜像内容。

已验证现有 SD 系统安装同一 App/会话后的正常重启；未烧录本次工厂镜像做首次扩容或断电冷启动验收。真实触笔、麦克风/扬声器、摄像头及与 GNOME 的量化性能对比未在本次测试中验证，不以容器或注入事件的数据代替。

参考：[Mesa Panfrost](https://docs.mesa3d.org/drivers/panfrost.html)、[Qt QSurfaceFormat](https://doc.qt.io/qt-6/qsurfaceformat.html)、[Qt WebEngine 沙盒要求](https://doc.qt.io/qt-6.5/qtwebengine-platform-notes.html)。
