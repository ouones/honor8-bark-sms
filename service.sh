#!/system/bin/sh
# 备用机短信转发 - 开机启动服务

# 1. 必须死等系统完全启动完毕，否则 content 命令会因为 Provider 没加载而报错
until [ "$(getprop sys.boot_completed)" = "1" ]; do
    sleep 3
done

# 2. 额外缓冲 10 秒，确保 Telephony 电话/短信组件完全初始化
sleep 10

# 3. 获取当前模块的绝对路径 (也就是 /data/adb/modules/你的模块名)
MODDIR=${0%/*}

# 4. 后台静默启动核心守护进程
# nohup 和最后的 & 缺一不可，保证它在后台死循环且不阻塞系统开机
nohup $MODDIR/core/sms_daemon.sh > /dev/null 2>&1 &