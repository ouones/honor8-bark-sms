# 荣耀8 SIM1 短信转发至 Bark

V2.3 荣耀8 Bark 迭代，基于原作者「双曲线与圆」的 V2.2 模块，保留模块 ID `hyperos_sms_forwarder`。

正常联网时秒级转发，支持验证码复制、断网恢复补发和重启接续。

本次支持范围为荣耀8 FRD-AL00、Android 8 / EMUI 8、Magisk 25.2，目标接收端为 iPhone 15 Pro 上的 Bark。旧钉钉及通话相关代码保留，其他机型、系统和 Root 管理器不在本次兼容保证范围。秒级转发依据本机正常联网时的用户观察，默认仍为5秒轮询。完整实测状态见 [T07 验收记录](docs/t07-verification.md)。

## 行为与限制

- 只转发 SIM1 收件箱中的新短信，不按验证码关键词过滤。默认每5秒轮询，全部通话监控关闭。
- 目标荣耀8 EMUI 8 的短信 `sub_id` 使用卡槽号；通过 `siminfo` 确认 SIM1 卡槽后按其匹配，不能直接把订阅表 `_id` 当作短信标识。此差异由真机验收发现并修复。
- 每条短信发送一条 Bark 通知，包含发件人、原始接收时间和全文。识别到中文“验证码”或英文 code 附近的4至8位纯数字时，标题突出验证码，Bark 的复制参数只包含验证码；未识别仍转发全文。不承诺 iOS 自动粘贴或自动填充。
- 首次启动成功查询后跳过已有历史；重启使用模块内 `state/sms.cursor` 接续。请勿删除进度文件以尝试补发，删除后会重新建立边界并跳过现有短信。
- 发送失败后等待下轮，从系统收件箱重读。断网恢复可补发仍存在的待发短信；源短信删除后不恢复，不额外保存短信正文、队列或数据库。
- 请求结果丢失、请求成功后进度尚未提交就中断时，可能重复通知，不保证严格恰好一次。非法进度会停止并报告错误，不会自动重置。
- 随包提供 Android ARM64 curl、jq 和 CA 证书，安装时无需联网下载或另装 App。外网接口验证 HTTPS 证书，连接超时3秒、总超时5秒；每次 Provider 查询上限5秒。
- 日志默认单文件上限1 MiB，最多一个备份，Bark 路径不记录正文、验证码或 key。低开销不等于已经测量电池耗电。

## 配置与安装

荣耀8的“验证码安全保护”会使已收到的验证码对外部 Provider 查询不可见。真机上 `settings get system verifition_sms_protect_enable` 为 `1` 时复现，用户允许临时改为 `0` 后记录可见并补发；要转发这类验证码，须先自行选择是否关闭此项。模块不自动更改该系统设置。保留保护开启时，不能保证这类验证码可转发；已存在但不可见的记录也无法由模块恢复。华为对该功能的说明见 [官方支持文档](https://consumer.huawei.com/cn/support/content/zh-cn00797234/)。首次启用前应先完成该设置选择，再建立历史边界。

1. 解压交付 ZIP，编辑 `core/config.conf`，填入以下两项，文件保持 UTF-8 无 BOM、LF 换行：

   ```sh
   PUSH_CHANNEL="Bark"
   BARK_API_URL="https://api.day.app/push"
   BARK_DEVICE_KEY="替换为你自己的设备 key"
   ```

   `BARK_API_URL` 是完整 JSON POST 端点；自建服务或反向代理按实际路径填写。Bark App 的 `https://api.day.app/设备key/` 快捷链接应拆开填写，key 仅进入请求体。Bark 配置无需填写旧 `WEBHOOK_URL`。参数依据 [Bark 官方教程](https://github.com/Finb/Bark/blob/master/docs/tutorial.md)。

2. 保持目标默认值：`OS_TYPE="EMUI"`、`ENABLE_SMS="true"`、三类通话开关均为 `false`、`INTERVAL=5`。安装时 Bark 分支会固定上述系统和功能开关，不进入旧音量键交互。
3. 将 `module.prop`、`service.sh`、`customize.sh`、`core/` 和 `bin/` 等文件直接放在 ZIP 根目录重新压缩，不要多套一层文件夹。保留 `bin/licenses/`。仅在自己的本地安装副本中填写真实配置，不分享含 key 的压缩包。
4. 在 Magisk 25.2 中从本地安装 ZIP 并重启；也可使用 Root 命令 `magisk --install-module /sdcard/Download/模块文件.zip` 后重启。包按 Magisk App 安装格式提供，不包含 Recovery 安装入口，参见 [Magisk 模块指南](https://topjohnwu.github.io/Magisk/guides.html)。空 API 或空 key 的公开交付包会被安装校验拦截，须先完成配置。
5. 等待系统启动完成及额外10秒缓冲，确认进度已初始化，再向荣耀8 SIM1 发送新的测试短信，在 iPhone 检查全文和验证码复制。iPhone Bark 应允许通知；真实展示需在接收端确认。

配置文件安装后权限为 `0600`；`state/` 为 `0700`，进度为 `0600`。保留既有进度可继续重启接续；升级前自行保留设备上配置和进度，卸载、重装或手动清理不承诺进度迁移。公开交付包不包含真实配置、运行日志或状态。

## 排查与验证

Root Shell 中可查看：

```sh
cat /data/adb/modules/hyperos_sms_forwarder/state/sms.cursor
tail -n 30 /data/local/tmp/sms_forward_run.log
```

进度只包含 `date _id` 两个整数。缺配置、缺依赖、查询或网络失败分别报告固定原因；不要把日志中的“Bark 服务确认成功”当作 iPhone 展示证据。空闲轮询不写日志，重复启动只有一个短信处理者。非法进度应先保留现场排查，勿直接清空。

主机回归在 Linux / WSL 执行，需 bash、curl、jq、openssl 和 BusyBox：

```sh
python3 -m unittest discover -s tests -v
```

测试通过真实脚本进程、合成 Provider 输出和本机 HTTP 服务验证，不使用真实 key，也不向系统短信库写入伪造记录。依赖来源、散列和许可见 [bin/README.md](bin/README.md)，历史阶段证据见 [T01](docs/t01-verification.md)、[T02](docs/t02-verification.md) 和 [T06](docs/t06-verification.md)。
