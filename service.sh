#!/system/bin/sh
# 备用机短信转发 - 开机启动服务

BUSYBOX=/data/adb/magisk/busybox
if [ ! -x "$BUSYBOX" ]; then
    echo "启动错误：缺少 Magisk BusyBox" >&2
    exit 1
fi
export ASH_STANDALONE=1

# 1. 必须死等系统完全启动完毕，否则 content 命令会因为 Provider 没加载而报错
until [ "$(getprop sys.boot_completed)" = "1" ]; do
    sleep 3
done

# 2. 额外缓冲 10 秒，确保 Telephony 电话/短信组件完全初始化
sleep 10

# 3. 获取当前模块的绝对路径 (也就是 /data/adb/modules/你的模块名)
MODDIR=${0%/*}
CONFIG_FILE="$MODDIR/core/config.conf"
if [ ! -r "$CONFIG_FILE" ] || ! . "$CONFIG_FILE"; then
    echo "启动错误：无法读取配置文件" >&2
    exit 1
fi
if [ -z "$TMP_DIR" ] || [ ! -d "$TMP_DIR" ] || [ ! -w "$TMP_DIR" ]; then
    echo "启动错误：TMP_DIR 目录不可写" >&2
    exit 1
fi

# 4. 后台静默启动核心守护进程
# 明确使用 Magisk ash；错误写入配置日志并脱离开机入口的输出管道。
"$BUSYBOX" nohup "$BUSYBOX" ash "$MODDIR/core/sms_daemon.sh" > /dev/null 2>>"$TMP_DIR/sms_forward_run.log" &
