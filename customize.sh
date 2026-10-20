#!/system/bin/sh
# 交互式音量键安装脚本

ui_print "*********************************************"
ui_print "   备用机全能通信转发 - 交互式安装向导   "
ui_print "*********************************************"

# --- 1. 按渠道校验配置与离线依赖 ---
CONFIG_FILE="$MODPATH/core/config.conf"
[ -r "$CONFIG_FILE" ] || abort "配置错误：找不到 config.conf"
. "$CONFIG_FILE" || abort "配置错误：无法读取 config.conf"
[ -x /data/adb/magisk/busybox ] || abort "依赖错误：缺少 Magisk BusyBox"
case "$INTERVAL" in ''|0*|*[!0-9]*) abort "配置错误：INTERVAL 必须为正整数" ;; esac
case "$PUSH_CHANNEL" in
    Bark)
        [ -n "$BARK_DEVICE_KEY" ] || abort "配置错误：缺少 BARK_DEVICE_KEY"
        case "$BARK_API_URL" in
            *[[:space:][:cntrl:]]*|https:///*|https://:*) abort "配置错误：BARK_API_URL 格式无效" ;;
        esac
        case "$BARK_API_URL" in https://?*/*) ;; *) abort "配置错误：请填写完整 Bark HTTPS POST 端点" ;; esac
        if [ ! -f "$MODPATH/bin/curl" ] || [ ! -f "$MODPATH/bin/jq" ] || [ ! -r "$MODPATH/bin/cacert.pem" ]; then
            abort "依赖错误：缺少 Bark 客户端或证书"
        fi
        ;;
    DingTalk)
if [ "$WEBHOOK_URL" = "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN_HERE" ] || [ -z "$WEBHOOK_URL" ]; then
    ui_print " ❌ 拦截安装：未配置 Webhook 地址！"
    ui_print "请先解压，在 core/config.conf 中填入你的 Webhook"
    ui_print "然后重新打包为 ZIP 再刷入！"
    abort "安装已终止。"
fi
        ;;
    *) abort "配置错误：未知 PUSH_CHANNEL" ;;
esac

# --- 2. 按键捕获引擎 ---
ui_print "- 正在初始化按键检测环境..."

get_key() {
    # 使用 getevent 监听按键 1 次，提取出是音量上还是音量下
    while true; do
        local key=$(getevent -qlc 1 2>/dev/null | grep -o -E 'KEY_VOLUMEUP|KEY_VOLUMEDOWN' | head -n 1)
        if [ "$key" = "KEY_VOLUMEUP" ]; then
            echo "UP"
            break
        elif [ "$key" = "KEY_VOLUMEDOWN" ]; then
            echo "DOWN"
            break
        fi
    done
}

# 动态修改 config.conf 的小工具
set_config() {
    local key=$1
    local value=$2
    # 使用 sed 正则替换对应配置的值
    sed -i "s/^${key}=.*/${key}=${value}/g" "$CONFIG_FILE"
}

if [ "$PUSH_CHANNEL" = "Bark" ]; then
    ui_print "- 使用荣耀8配置：EMUI、短信开启、全部通话监控关闭。"
    set_config "OS_TYPE" '"EMUI"'
    set_config "ENABLE_SMS" "true"
    set_config "ENABLE_MISSED_CALL" "false"
    set_config "ENABLE_INCOMING_CALL" "false"
    set_config "ENABLE_OUTGOING_CALL" "false"
else
ui_print " "
ui_print "👉 请按一次【音量上】或【音量下】测试按键..."
TEST_KEY=$(get_key)
ui_print "✅ 按键响应正常！进入快速配置环节："
sleep 1

# --- 3. 交互式选择环节 ---

ui_print " "
ui_print "========================================="
ui_print " 📱 [1/5] 选择备用机当前的系统环境"
ui_print "  [音量 +] 小米 HyperOS / 其他通用系统"
ui_print "  [音量 -] OPPO / 一加 ColorOS 特化"
ui_print "========================================="
if [ "$(get_key)" = "DOWN" ]; then
    ui_print " --> 已选择: ColorOS"
    set_config "OS_TYPE" "\"ColorOS\""
else
    ui_print " --> 已选择: HyperOS/通用"
    set_config "OS_TYPE" "\"HyperOS\""
fi
sleep 1

ui_print " "
ui_print "========================================="
ui_print " 💬 [2/5] 是否开启【短信转发】？"
ui_print "  [音量 +] 开启 (推荐)"
ui_print "  [音量 -] 关闭"
ui_print "========================================="
if [ "$(get_key)" = "UP" ]; then
    ui_print " --> 已选择: 开启短信转发"
    set_config "ENABLE_SMS" "true"
else
    ui_print " --> 已选择: 关闭短信转发"
    set_config "ENABLE_SMS" "false"
fi
sleep 1

ui_print " "
ui_print "========================================="
ui_print " 📞 [3/5] 是否开启【未接来电提醒】？"
ui_print "  [音量 +] 开启 (推荐)"
ui_print "  [音量 -] 关闭"
ui_print "========================================="
if [ "$(get_key)" = "UP" ]; then
    ui_print " --> 已选择: 开启未接来电"
    set_config "ENABLE_MISSED_CALL" "true"
else
    ui_print " --> 已选择: 关闭未接来电"
    set_config "ENABLE_MISSED_CALL" "false"
fi
sleep 1

ui_print " "
ui_print "========================================="
ui_print " 📥 [4/5] 是否开启【已接来电记录】？"
ui_print "  [音量 +] 开启"
ui_print "  [音量 -] 关闭 (推荐，防打扰)"
ui_print "========================================="
if [ "$(get_key)" = "UP" ]; then
    ui_print " --> 已选择: 开启已接记录"
    set_config "ENABLE_INCOMING_CALL" "true"
else
    ui_print " --> 已选择: 关闭已接记录"
    set_config "ENABLE_INCOMING_CALL" "false"
fi
sleep 1

ui_print " "
ui_print "========================================="
ui_print " 📤 [5/5] 是否开启【拨出电话记录】？"
ui_print "  [音量 +] 开启"
ui_print "  [音量 -] 关闭 (推荐，防打扰)"
ui_print "========================================="
if [ "$(get_key)" = "UP" ]; then
    ui_print " --> 已选择: 开启拨出记录"
    set_config "ENABLE_OUTGOING_CALL" "true"
else
    ui_print " --> 已选择: 关闭拨出记录"
    set_config "ENABLE_OUTGOING_CALL" "false"
fi
sleep 1

# --- 4. 权限赋予与收尾 ---
fi
ui_print " "
ui_print "- 正在保存配置并应用权限..."
set_perm_recursive "$MODPATH/core" 0 0 0755 0755
set_perm "$CONFIG_FILE" 0 0 0600
if [ "$PUSH_CHANNEL" = "Bark" ]; then
    set_perm_recursive "$MODPATH/bin" 0 0 0755 0644
    set_perm "$MODPATH/bin/curl" 0 0 0755
    set_perm "$MODPATH/bin/jq" 0 0 0755
fi
ui_print " "
ui_print "🎉 安装完成！所有自定义选项已写入。"
ui_print "重启后探针将自动在后台工作。"
