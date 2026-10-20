#!/system/bin/sh
# /data/adb/modules/hyperos_sms_forwarder/core/sms_daemon.sh
# 备用机全方位通信底层转发 (V1 稳定双气泡 + 万能关键词 + 完美日志版)

BASE_DIR=$(dirname "$(readlink -f "$0")")
CONFIG_FILE="$BASE_DIR/config.conf"

# 在派生路径及日志上限生效前读取配置，所有校验均早于进度初始化。
startup_error() {
    printf '启动错误：%s\n' "$1" >&2
    exit 1
}

[ -r "$CONFIG_FILE" ] || startup_error "找不到配置文件"
. "$CONFIG_FILE" || startup_error "配置读取失败"
case "$INTERVAL" in
    ''|0*|*[!0-9]*) startup_error "INTERVAL 必须为正整数" ;;
esac
case "$MAX_LOG_SIZE_KB" in
    ''|0*|*[!0-9]*) startup_error "MAX_LOG_SIZE_KB 必须为正整数" ;;
esac
if [ -z "$TMP_DIR" ] || [ ! -d "$TMP_DIR" ] || [ ! -w "$TMP_DIR" ]; then
    startup_error "TMP_DIR 目录不可写"
fi
case "$OS_TYPE" in
    EMUI|ColorOS|HyperOS|MIUI) ;;
    *) startup_error "OS_TYPE 无效" ;;
esac
for enabled in "$ENABLE_SMS" "$ENABLE_MISSED_CALL" "$ENABLE_INCOMING_CALL" "$ENABLE_OUTGOING_CALL"; do
    case "$enabled" in true|false) ;; *) startup_error "功能开关必须为 true 或 false" ;; esac
done
for tool in awk sed grep head tail tr date wc mv cat touch sleep flock content; do
    command -v "$tool" >/dev/null 2>&1 || startup_error "缺少必需工具：$tool"
done
case "$PUSH_CHANNEL" in
    Bark)
        [ -n "$BARK_DEVICE_KEY" ] || startup_error "缺少 BARK_DEVICE_KEY"
        case "$BARK_API_URL" in
            *[[:space:][:cntrl:]]*|https:///*|https://:*) startup_error "BARK_API_URL 格式无效" ;;
        esac
        case "$BARK_API_URL" in
            https://?*/*) ;;
            http://127.0.0.1:*/*|http://localhost:*/*)
                authority=${BARK_API_URL#http://}
                authority=${authority%%/*}
                port=${authority#*:}
                case "$port" in ''|*[!0-9]*) startup_error "Bark 回环端点无效" ;; esac ;;
            *) startup_error "BARK_API_URL 须为完整 HTTPS 端点" ;;
        esac
        if [ ! -x "$BASE_DIR/../bin/curl" ] || [ ! -x "$BASE_DIR/../bin/jq" ] || [ ! -r "$BASE_DIR/../bin/cacert.pem" ]; then
            startup_error "Bark 客户端或证书缺失"
        fi
        ;;
    DingTalk)
        if [ -z "$WEBHOOK_URL" ] || [ "$WEBHOOK_URL" = "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN_HERE" ]; then
            startup_error "缺少 WEBHOOK_URL"
        fi
        command -v curl >/dev/null 2>&1 || startup_error "缺少 curl"
        ;;
    *) startup_error "PUSH_CHANNEL 无效" ;;
esac

# 锁文件保持原 inode，禁止删除；进程退出时由内核释放锁。
umask 077
exec 9>"$BASE_DIR/../sms_daemon.lock" || startup_error "无法打开启动锁"
flock -n 9 || startup_error "守护进程已在运行"
LOG_FILE="$TMP_DIR/sms_forward_run.log"

# ================= 核心工具函数 =================

log_msg() {
    local level=$1
    local msg=$2
    local time=$(date "+%Y-%m-%d %H:%M:%S")
    echo "[$time] [$level] $msg" >> "$LOG_FILE"
    
    # 日志防爆满清理逻辑
    if [ -f "$LOG_FILE" ]; then
        local size_bytes=$(wc -c < "$LOG_FILE" | tr -d ' ')
        local max_bytes=$((MAX_LOG_SIZE_KB * 1024))
        if [ "$size_bytes" -ge "$max_bytes" ]; then
            mv "$LOG_FILE" "${LOG_FILE}.bak"
            echo "[$time] [INFO] 日志体积达标，触发自动清理。" > "$LOG_FILE"
        fi
    fi
}

escape_json() {
    echo "$1" | awk '{gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); printf "%s\\n", $0}' | sed 's/\\n$//'
}

send_to_bark() {
    local client="$BASE_DIR/../bin/curl"
    local json="$BASE_DIR/../bin/jq"
    local ca="$BASE_DIR/../bin/cacert.pem"
    local payload response http_code authority port

    if [ -z "$BARK_DEVICE_KEY" ] || [ ! -x "$client" ] || [ ! -x "$json" ] || [ ! -r "$ca" ]; then
        log_msg "ERROR" "Bark 配置或依赖缺失"
        return 1
    fi
    # 外网仅允许 HTTPS；回环 HTTP 供隔离接收服务验证使用。
    case "$BARK_API_URL" in
        https://*) ;;
        http://127.0.0.1:*/*|http://localhost:*/*)
            authority=${BARK_API_URL#http://}
            authority=${authority%%/*}
            port=${authority#*:}
            case "$port" in
                ''|*[!0-9]*) log_msg "ERROR" "Bark 回环端点无效"; return 1 ;;
            esac ;;
        *) log_msg "ERROR" "Bark 端点须使用 HTTPS"; return 1 ;;
    esac

    # NUL 分隔字段，内容只经过内存和管道，不放入命令参数或载荷文件。
    payload=$(
        { printf '%s\000' "$BARK_DEVICE_KEY" "$1" "$2"; printf '%s' "$3"; } |
            "$json" -Rsc 'split("\u0000") | {device_key: .[0], title: .[1], body: .[2]} + (if .[3] == "" then {} else {copy: .[3]} end)' 2>/dev/null
    ) || { log_msg "ERROR" "Bark JSON 编码失败"; return 1; }

    # -q 禁用外部 curlrc，避免环境配置关闭证书验证或加入内部重试。
    if ! response=$(printf '%s' "$payload" | "$client" -q -s \
        --connect-timeout 3 --max-time 5 --cacert "$ca" --proto '=http,https' \
        -H 'Content-Type: application/json; charset=utf-8' \
        --data-binary @- -w '\n%{http_code}' --url "$BARK_API_URL" 2>/dev/null); then
        log_msg "ERROR" "Bark 客户端传输失败"
        return 1
    fi
    http_code=$(printf '%s\n' "$response" | tail -n 1)
    if [ "$http_code" = "200" ] && printf '%s\n' "$response" | sed '$d' |
        "$json" -es 'length == 1 and (.[0] | type == "object" and .code == 200)' >/dev/null 2>&1; then
        log_msg "SUCCESS" "Bark 服务确认成功"
        return 0
    fi
    log_msg "ERROR" "Bark HTTP 或业务响应失败"
    return 1
}

send_to_webhook() {
    if [ "${PUSH_CHANNEL:-DingTalk}" = "Bark" ]; then
        send_to_bark "$1" "$2" "$3"
        return $?
    fi
    # 强制在标题加入“通知”二字，突破钉钉拦截
    local safe_title=$(escape_json "【通知】$1") 
    local safe_text=$(escape_json "$2")
    
    local PAYLOAD="$TMP_DIR/ding_payload_$RANDOM.json"
    printf '{"msgtype": "markdown", "markdown": {"title": "%s", "text": "%s"}}' "$safe_title" "$safe_text" > "$PAYLOAD"
    
    local response=$(curl -s -w "\n%{http_code}" -H "Content-Type: application/json" -d "@$PAYLOAD" "$WEBHOOK_URL")
    local http_code=$(echo "$response" | tail -n 1)
    
    rm -f "$PAYLOAD"

    if [ "$http_code" = "200" ]; then
        log_msg "SUCCESS" "推送钉钉成功！"
        return 0
    else
        log_msg "ERROR" "推送钉钉失败！HTTP状态码: $http_code"
        return 1
    fi
}

init_timestamp() {
    local uri=$1
    local where_cond=$2
    local record_file=$3
    if [ ! -f "$record_file" ]; then
        local raw_data=""
        if [ -n "$where_cond" ]; then
            raw_data=$(content query --uri "$uri" --projection date --where "$where_cond" --sort "date DESC" 2>/dev/null | head -n 1)
        else
            raw_data=$(content query --uri "$uri" --projection date --sort "date DESC" 2>/dev/null | head -n 1)
        fi
        local init_date=$(echo "$raw_data" | sed -n 's/.*date=\([^,]*\).*/\1/p' | tr -cd '0-9')
        [ -z "$init_date" ] && init_date=0
        echo "$init_date" > "$record_file"
        log_msg "INFO" "初始化时间戳: $record_file -> $init_date"
    fi
}

# ================= 业务逻辑函数 =================

get_sim1_sub_id() {
    local raw_siminfo
    raw_siminfo=$(content query --uri content://telephony/siminfo --projection _id:sim_id 2>/dev/null) || return 1
    printf '%s\n' "$raw_siminfo" | awk -F '[, =]+' '
    {
        id = ""
        slot = ""
        for (i = 1; i < NF; i++) {
            if ($i == "_id") id = $(i + 1)
            if ($i == "sim_id") slot = $(i + 1)
        }
        if (slot == "0" && id ~ /^[0-9]+$/) {
            count++
            selected = id
        }
    }
    END { if (count == 1) print selected }
    '
}

hex_to_utf8() {
    local hex="$1"
    local byte
    case "$hex" in *[!0-9A-Fa-f]*) return 1 ;; esac
    [ $(( ${#hex} % 2 )) -eq 0 ] || return 1
    while [ -n "$hex" ]; do
        byte=${hex%"${hex#??}"}
        hex=${hex#??}
        printf '%b' "\\0$(printf '%03o' "$((0x$byte))")" || return 1
    done
}

check_sms() {
    local sim1_sub_id
    sim1_sub_id=$(get_sim1_sub_id) || return 1
    [ -n "$sim1_sub_id" ] || return 1
    local record_file="$TMP_DIR/last_sms_date.txt"
    local sms_uri="content://sms/inbox"
    local where_cond="sub_id=$sim1_sub_id"
    
    if [ "$OS_TYPE" = "ColorOS" ]; then
        sms_uri="content://sms"
        where_cond="type=1 AND sub_id=$sim1_sub_id"
    fi

    init_timestamp "$sms_uri" "$where_cond" "$record_file"
    local last_date=$(cat "$record_file" | tr -cd '0-9')
    [ -z "$last_date" ] && last_date=0
    
    local raw_sms=""
    raw_sms=$(content query --uri "$sms_uri" --projection "date:sub_id:hex(address) AS address_hex:hex(body) AS body_hex" --where "$where_cond" --sort "date DESC" 2>/dev/null | head -n 1)

    if [ -n "$raw_sms" ]; then
        local address_hex=$(printf '%s\n' "$raw_sms" | sed -n 's/.*address_hex=\([[:xdigit:]]*\).*/\1/p')
        local body_hex=$(printf '%s\n' "$raw_sms" | sed -n 's/.*body_hex=\([[:xdigit:]]*\).*/\1/p')
        local address
        address=$(hex_to_utf8 "$address_hex" && printf '.') || { log_msg "ERROR" "短信发件人解码失败"; return 1; }
        address=${address%.}
        local body
        body=$(hex_to_utf8 "$body_hex" && printf '.') || { log_msg "ERROR" "短信正文解码失败"; return 1; }
        body=${body%.}
        local sms_date=$(printf '%s\n' "$raw_sms" | sed -n 's/.*date=\([0-9][0-9]*\).*/\1/p')
        local sub_id=$(echo "$raw_sms" | sed -n 's/.*sub_id=\([^,]*\).*/\1/p')

        if [ "$sub_id" = "$sim1_sub_id" ] && [ -n "$sms_date" ] && [ "$sms_date" -gt "$last_date" ]; then
            local sim_tag="未知"
            [ "$sub_id" = "1" ] || [ "$sub_id" = "0" ] && sim_tag="卡1"
            [ "$sub_id" = "2" ] && sim_tag="卡2"

            if [ "$PUSH_CHANNEL" = "Bark" ]; then
                log_msg "INFO" "捕获新短信"
            else
                log_msg "INFO" "捕获新短信 -> 号码: $address"
            fi
            
            # 防贪婪精准提取补丁
            local safe_body=" $body"
            local vcode=$(printf '%s\n' "$safe_body" | sed -n 's/.*[^0-9]\([0-9]\{4,8\}\)[^0-9]*验证码.*/\1/p')
            [ -z "$vcode" ] && vcode=$(printf '%s\n' "$safe_body" | sed -n 's/.*验证码[^0-9]*\([0-9]\{4,8\}\)\([^0-9].*\)\{0,1\}$/\1/p')
            [ -z "$vcode" ] && vcode=$(printf '%s\n' "$safe_body" | sed -n 's/.*[Cc]ode[^0-9]*\([0-9]\{4,8\}\)\([^0-9].*\)\{0,1\}$/\1/p')

            if [ "$PUSH_CHANNEL" = "Bark" ]; then
                local received_at
                received_at=$(date -d "@$((sms_date / 1000))" "+%Y-%m-%d %H:%M:%S") || return 1
                received_at="${received_at}.$(printf '%03d' "$((sms_date % 1000))")"
                local bark_title="$KEYWORD_SMS"
                [ -n "$vcode" ] && bark_title="$KEYWORD_SMS $vcode"
                local bark_body="发件人：$address
接收时间：$received_at
短信内容：
$body"
                if send_to_webhook "$bark_title" "$bark_body" "$vcode"; then
                    echo "$sms_date" > "$record_file"
                fi
            elif [ -n "$vcode" ]; then
                local cb='```'
                local detail_msg="### 💬 ${KEYWORD_SMS} 详情\n\n---\n- **卡槽**: ${sim_tag}\n- **发件人**: ${address}\n- **内容**: ${body}"
                send_to_webhook "${KEYWORD_SMS}" "$detail_msg"
                
                local vcode_msg="${cb}text\n${vcode}\n${cb}"
                if send_to_webhook "${KEYWORD_SMS}" "$vcode_msg"; then
                    echo "$sms_date" > "$record_file"
                fi
            else
                local normal_msg="### 💬 ${KEYWORD_SMS}\n\n---\n- **卡槽**: ${sim_tag}\n- **发件人**: ${address}\n- **内容**: ${body}"
                if send_to_webhook "${KEYWORD_SMS}" "$normal_msg"; then
                    echo "$sms_date" > "$record_file"
                fi
            fi
        fi
    fi
}

check_call_log() {
    local call_type=$1
    local keyword=$2
    local record_file="$TMP_DIR/last_call_${call_type}_date.txt"
    
    if [ ! -f "$record_file" ]; then
        local raw_init=$(content query --uri content://call_log/calls --projection date:type --sort "date DESC" 2>/dev/null | head -n 15 | grep "type=${call_type}" | head -n 1)
        local init_date=$(echo "$raw_init" | sed -n 's/.*date=\([^,]*\).*/\1/p' | tr -cd '0-9')
        [ -z "$init_date" ] && init_date=0
        echo "$init_date" > "$record_file"
        log_msg "INFO" "通话初始化: $record_file -> $init_date"
    fi
    
    local last_date=$(cat "$record_file" | tr -cd '0-9')
    [ -z "$last_date" ] && last_date=0
    
    local raw_call=$(content query --uri content://call_log/calls --projection number:date:type --sort "date DESC" 2>/dev/null | head -n 15 | grep "type=${call_type}" | head -n 1)

    if [ -n "$raw_call" ]; then
        local number=$(echo "$raw_call" | sed -n 's/.*number=\([^,]*\).*/\1/p')
        local call_date=$(echo "$raw_call" | sed -n 's/.*date=\([^,]*\).*/\1/p' | tr -cd '0-9')

        if [ -n "$call_date" ] && [ "$call_date" -gt "$last_date" ]; then
            log_msg "INFO" "捕获通话记录(类型$call_type) -> 号码: $number"
            
            local message="### 📞 ${keyword}\n\n---\n- **号码**: ${number}"
            
            if send_to_webhook "${keyword}" "$message"; then
                echo "$call_date" > "$record_file"
            fi
        fi
    fi
}

# ================= 启动准备 =================

touch "$LOG_FILE" || startup_error "无法写入日志"
log_msg "INFO" "========================================="
log_msg "INFO" "通信转发守护进程启动"

# ================= 守护进程主循环 =================

while true; do
    if [ "$ENABLE_SMS" = "true" ]; then check_sms; fi
    if [ "$ENABLE_MISSED_CALL" = "true" ]; then check_call_log "3" "$KEYWORD_MISSED"; fi
    if [ "$ENABLE_INCOMING_CALL" = "true" ]; then check_call_log "1" "$KEYWORD_INCOMING"; fi
    if [ "$ENABLE_OUTGOING_CALL" = "true" ]; then check_call_log "2" "$KEYWORD_OUTGOING"; fi
    sleep "$INTERVAL"
done
