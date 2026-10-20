# T01 运行依赖

本目录随模块离线提供 ARM64 客户端、JSON 工具与信任证书；运行及安装时无需下载，也不安装 APK 或额外 App。T01 保留 `DingTalk` 默认配置，隔离测试副本选择 `Bark`。正式安装入口与权限设置由 T02 完成。

| 文件 | 固定版本与来源 | SHA-256 |
|---|---|---|
| `curl` | [curl-android v8.18.0 的 tool-release.apk.xz](https://github.com/vvb2060/curl-android/releases/download/v8.18.0/tool-release.apk.xz) 中的 `lib/arm64-v8a/libcurl.so`，原样提取并重命名 | `c78079c0239f0a6c44aa7e9180f97d4c3d175495d1ccf565a8854abd15f68b60` |
| `jq` | [jq 1.8.1 官方源码](https://github.com/jqlang/jq/releases/download/jq-1.8.1/jq-1.8.1.tar.gz)，以固定 musl 工具链构建的精简静态版本，见下文命令 | `e00688ce8c6193773269a65407296b2a1f586eaae499d3630501ebd87a6e556c` |
| `cacert.pem` | [curl 提供的 Mozilla 2026-09-25 CA 快照](https://curl.se/ca/cacert-2026-09-25.pem)，121 个证书，原样保存 | `a41b5d356aea97a529fe27e0f7316d2f9d946d75927476cf9cf1b90637d00505` |

curl 发布包 SHA-256 为 `0aa32165eb4a9cb8a56e13053cd3bc80cccb0c761b4daf9688c3e3667522bb4b`，与 [GitHub 发布资产元数据](https://api.github.com/repos/vvb2060/curl-android/releases/tags/v8.18.0) 一致。jq 源码包 SHA-256 为 `2be64e7129cecb11d5906290eba10af694fb9e3e7f9fc208a311dc33ca837eb0`，与官方 [sha256sum.txt](https://github.com/jqlang/jq/releases/download/jq-1.8.1/sha256sum.txt) 一致。CA 校验值记录本次取回的固定快照。

curl 制品实际报告 `curl 8.18.0-DEV`、`Release-Date: [unreleased]`；不要将它表述为官方稳定版二进制。构建来自 [上游发布工作流](https://github.com/vvb2060/curl-android/actions/runs/20986940124)，curl 源码固定为 `2eebc58c4b8d68c98c8344381a9f6df4cca838fd`。ELF 为 AArch64 PIE，解释器 `/system/bin/linker64`，使用 Android 系统 C 库及 zlib；其余网络库随制品链接。jq 为 AArch64 musl 静态 PIE，本次已在目标 Android 内核实测，不能据此宣称支持其他 Android 设备。

jq 构建使用 [musl.cc aarch64-linux-musl-cross.tgz](https://musl.cc/aarch64-linux-musl-cross.tgz)，下载快照 SHA-256 为 `c909817856d6ceda86aa510894fa3527eac7989f0ef6e87b5721c58737a06c38`，编译器报告 GCC `11.2.1 20211120`。该 URL 没有版本号，复建前必须核对快照散列，不能直接信任后续同名下载。解压工具链和 jq 源码后，在 jq 源码目录执行（`/absolute/toolchain` 替换为工具链解压目录）：

```sh
CC=/absolute/toolchain/bin/aarch64-linux-musl-gcc ./configure \
  --host=aarch64-linux-musl --disable-docs --without-oniguruma \
  --disable-maintainer-mode --enable-static --enable-all-static \
  CFLAGS=-Os LDFLAGS=-s
make -j2
```

将生成的 `jq` 原样复制到本目录；二进制内的构建配置含编译器绝对路径，不承诺换目录构建得到相同散列。不包含 Oniguruma，未提供正则表达式功能；模块仅使用 JSON 编码与解析，不需要该功能。

选择 jq 用于请求 JSON 编码及完整响应解析，避免自写 Shell JSON 解析器。请求内容经标准输入进入 jq/curl，不使用正文文件或含正文/key 的进程参数。curl 固定连接超时 3 秒、总超时 5 秒，使用本目录 CA，`-q` 禁用 curlrc，不跟随重定向，不设置内部重试。外部端点须为 HTTPS；只有显式带端口的 `127.0.0.1` / `localhost` 端点允许 HTTP，供隔离接收服务测试。

源码许可及依赖声明保留在 `licenses/`：

| 组件 | 许可文件 | 取自固定源码 |
|---|---|---|
| curl | `curl.txt`，curl license | [2eebc58/COPYING](https://github.com/curl/curl/blob/2eebc58c4b8d68c98c8344381a9f6df4cca838fd/COPYING) |
| BoringSSL 20251124 | `BoringSSL.txt`，ISC / OpenSSL 等，见完整声明 | [79048f1/LICENSE](https://github.com/google/boringssl/blob/79048f1f1d8e6b7f9ca59b95c24486c8149122a4/LICENSE) |
| nghttp2 1.68.0 | `nghttp2.txt`，MIT | [534b74b/COPYING](https://github.com/nghttp2/nghttp2/blob/534b74b72524e962c18c7146470914632ca7eb2d/COPYING) |
| nghttp3 1.14.0 | `nghttp3.txt`，MIT | [4c54689/COPYING](https://github.com/ngtcp2/nghttp3/blob/4c54689c98d431bfe4d82bdcf2a67b9179a91cca/COPYING) |
| ngtcp2 1.19.0 | `ngtcp2.txt`，MIT | [3a60ab5/COPYING](https://github.com/ngtcp2/ngtcp2/blob/3a60ab56c11c45d1a2e121721b4838adfa55a776/COPYING) |
| 系统 zlib 1.2.8 | `zlib.txt`，zlib | [v1.2.8/README](https://github.com/madler/zlib/blob/v1.2.8/README) |
| jq 1.8.1（含 decNumber 等声明） | `jq.txt`，MIT / ICU / BSD 等 | [jq-1.8.1/COPYING](https://github.com/jqlang/jq/blob/jq-1.8.1/COPYING) |
| jq 静态 C 运行库 musl `1.2.2-git-50-gb76f37fd` | `musl.txt`，MIT 等 | [musl 版权声明](https://git.musl-libc.org/cgit/musl/tree/COPYRIGHT?id=b76f37fd)，运行库来自上述固定工具链 |
| jq 所含 GCC 运行库 | `GCC-runtime.txt`、`GPL-3.0.txt`，GPLv3 + GCC Runtime Library Exception 3.1 | [GCC 11.2 运行库例外](https://github.com/gcc-mirror/gcc/blob/releases/gcc-11.2.0/COPYING.RUNTIME) |
| Mozilla CA 数据 | `MPL-2.0.txt`，MPL 2.0 | [curl CA 提取说明](https://curl.se/docs/caextract.html)、[Mozilla MPL 2.0](https://www.mozilla.org/MPL/2.0/)；证书及来源日期保留在 PEM 中 |

2026-10-20 已在 FRD-AL00、Android 8.0.0、Magisk 25.2 上执行上述制品，完成受信任 HTTPS、无效证书拒绝及合成短信的守护进程检查。复验命令、结果和边界见 [T01 验证记录](../docs/t01-verification.md)。这不代表正式模块已安装或 iPhone 已展示通知。
